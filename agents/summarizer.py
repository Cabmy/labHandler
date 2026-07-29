"""Summarizer agent - 负责生成最终的任务总结报告，并沉淀知识卡片。

输出内容：
1. SUMMARY.md (用户侧)：生成在 workspace 目录下，包含完成的任务摘要、产物清单、验证方法及剩余待办。
2. knowledge_cards (系统侧)：从执行轨迹中蒸馏出 lesson/strategy/pattern 结构化知识卡片，
   供 /done 归档后未来 Planner 检索使用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import SUMMARIZER_SYSTEM, parse_result_json
from config.runtime import get_settings

WORKSPACE_DIR: Path = get_settings().workspace_dir
SUMMARY_PATH = WORKSPACE_DIR / "SUMMARY.md"


def _list_artifacts_from_workspace() -> list[str]:
    """workspace 当前可见的"用户产物"（排除 .labhandler 内部 + __pycache__ + 自己写的 SUMMARY.md）"""
    out: list[str] = []
    for p in sorted(WORKSPACE_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(WORKSPACE_DIR)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        # 自己写的 SUMMARY.md 不算"用户产物"，避免下次再生成时把它当 artifact 引用
        if str(rel) == "SUMMARY.md":
            continue
        out.append(str(rel))
    return out


def _build_calibration_block(verifier_runs: list[dict]) -> str:
    """Verifier 事实校准层：最后一轮 coverage 的 covered/missing/suggested_fix。

    P1-2：这是产物达标情况的权威事实，**不截断**——lesson 对齐 missing 反面、
    strategy 对齐真实通过路径都靠它，截断会让蒸馏失去依据。
    """
    if not verifier_runs:
        return "（无 verifier 运行记录）"
    last = verifier_runs[-1]
    cov = last.get("coverage") or {}
    lines = [f"verdict: {last.get('verdict', '?')}"]
    covered = cov.get("covered") or []
    lines.append("covered:")
    for c in covered:
        if isinstance(c, dict):
            lines.append(f"  - {c.get('constraint','')} | 证据: {c.get('evidence','')}")
        else:
            lines.append(f"  - {c}")
    if not covered:
        lines.append("  - （无）")
    missing = cov.get("missing") or []
    lines.append("missing:")
    for m in missing:
        if isinstance(m, dict):
            lines.append(f"  - {m.get('constraint','')} | 原因: {m.get('reason','')}")
        else:
            lines.append(f"  - {m}")
    if not missing:
        lines.append("  - （无）")
    sf = last.get("suggested_fix") or ""
    if sf:
        lines.append(f"suggested_fix: {sf}")
    return "\n".join(lines)


def _build_evolution_block(state: HwState, max_item_chars: int = 150) -> str:
    """跨轮演化块：按 progress_log 时间线切轮（planner → coder_steps → verifier）。

    校准层只保最后一轮；多轮 Replan 时「上一轮 missing → 本轮修复动作」的配对
    是 strategy 卡片的核心素材，这里全轮保留（单条截断、轮次不丢）。
    """
    verifier_runs = state.get("verifier_runs") or []
    if not verifier_runs:
        return ""

    rounds: list[dict] = []
    cur: dict | None = None
    v_idx = 0
    for e in state.get("progress_log") or []:
        node = e.get("node")
        if node == "planner":
            cur = {
                "iteration": e.get("iteration"),
                "n_nodes": e.get("n_nodes"),
                "steps": [],
                "verifier": None,
            }
            rounds.append(cur)
        elif node == "coder_step" and cur is not None:
            status = e.get("status") or e.get("skipped") or ("error" if e.get("error") else "?")
            desc = f"step {e.get('step_id')} attempt={e.get('attempt', 1)}: {status}"
            excerpt = str(e.get("final_excerpt") or e.get("reason") or e.get("error") or "")[:100]
            if excerpt:
                desc += f" — {excerpt}"
            cur["steps"].append(desc)
        elif node == "verifier" and cur is not None:
            if v_idx < len(verifier_runs):
                cur["verifier"] = verifier_runs[v_idx]
            v_idx += 1

    lines: list[str] = []
    for r in rounds:
        lines.append(f"### 第 {r.get('iteration', '?')} 轮（DAG {r.get('n_nodes', '?')} 节点）")
        lines.extend(f"- {s}" for s in r["steps"])
        run = r.get("verifier")
        if not run:
            continue
        lines.append(f"- verifier: {run.get('verdict', '?')}")
        for f in run.get("stage1_failures") or []:
            lines.append(f"  - [硬指标] {str(f)[:max_item_chars]}")
        for m in (run.get("coverage") or {}).get("missing") or []:
            if isinstance(m, dict):
                lines.append(
                    f"  - [missing] {str(m.get('constraint', ''))[:max_item_chars]}"
                    f"（{str(m.get('reason', ''))[:max_item_chars]}）"
                )
            else:
                lines.append(f"  - [missing] {str(m)[:max_item_chars]}")
        sf = run.get("suggested_fix") or ""
        if sf:
            lines.append(f"  - [fix建议] {str(sf)[:max_item_chars]}")
    return "\n".join(lines)


def _build_core_artifacts_block(
    state: HwState, per_file: int = 1500, total: int = 6000
) -> str:
    """核心产物内容窗口：deliverables / expected_artifacts 命中的文本文件真实内容。

    pattern 卡片的一手素材。
    """
    intake = state.get("intake_result") or {}
    names: set[str] = set()
    for d in intake.get("deliverables") or []:
        if isinstance(d, str) and d.strip():
            names.add(Path(d.strip()).name)
    for n in (state.get("task_dag") or {}).get("nodes") or []:
        for a in n.get("expected_artifacts") or []:
            if isinstance(a, str) and a.strip():
                names.add(Path(a.strip()).name)
    if not names:
        return ""

    exts = {".py", ".md", ".txt", ".cpp", ".c", ".h", ".java"}
    chunks: list[str] = []
    used = 0
    for p in sorted(WORKSPACE_DIR.rglob("*")):
        if used >= total:
            break
        if not p.is_file() or p.name not in names or p.suffix.lower() not in exts:
            continue
        rel = p.relative_to(WORKSPACE_DIR)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        budget = min(per_file, total - used)
        clipped = text[:budget]
        suffix = "\n…(截断)" if len(text) > len(clipped) else ""
        chunks.append(f"### {rel}\n{clipped}{suffix}")
        used += len(clipped)
    return "\n\n".join(chunks)


def _build_lessons_block(step_outputs: list[dict]) -> str:
    """step_lessons 全量块（不截断）：Coder 逐步沉淀的原始教训 + retry 原因。"""
    lines: list[str] = []
    for o in step_outputs:
        sid = o.get("id", "?")
        for lesson in o.get("step_lessons") or []:
            lines.append(f"- [step {sid}] {lesson}")
        if o.get("retry_reason"):
            lines.append(f"- [step {sid} retry] {o['retry_reason']}")
        if o.get("error"):
            lines.append(f"- [step {sid} error] {o['error']}")
    return "\n".join(lines) if lines else "（无）"


def _build_facts(state: HwState) -> str:
    """把可用事实拼成喂给 LLM 的 user_msg。"""
    intake = state.get("intake_result") or {}
    artifacts = state.get("artifacts") or []
    verifier_runs = state.get("verifier_runs") or []
    user_cons = state.get("user_constraints") or []
    step_outputs = state.get("step_outputs") or []
    messages = state.get("messages") or []

    artifact_paths = [str(a.get("path", "")) for a in artifacts if a.get("path")]
    if not artifact_paths:
        artifact_paths = _list_artifacts_from_workspace()

    parts = [
        "## intake",
        json.dumps(intake, ensure_ascii=False, default=str)[:1500],
        "",
        "## messages（用户多轮对话，含纠正/反馈/补充）",
        json.dumps(messages, ensure_ascii=False, default=str)[:2000],
        "",
        "## artifacts（实际 workspace 文件）",
        json.dumps(artifact_paths, ensure_ascii=False, default=str)[:1000],
        "",
        "## user_constraints（用户对话累加约束）",
        json.dumps(user_cons, ensure_ascii=False, default=str)[:1000],
        "",
        "## Verifier 事实校准层（最后一轮 covered/missing/suggested_fix，权威达标事实，不截断）",
        _build_calibration_block(verifier_runs),
        "",
        "## 跨轮演化时间线（每轮 planner→steps→verifier；「上轮 missing → 本轮修复」是 strategy 素材）",
        _build_evolution_block(state) or "（单轮完成，无演化）",
        "",
        "## step_outputs（每步执行记录：id / name / status / error / skipped）",
        json.dumps(step_outputs, ensure_ascii=False, default=str)[:2000],
        "",
        "## step_lessons 全量（Coder 原始教训 + retry/error 原因，不截断，蒸馏 lesson 的一手素材）",
        _build_lessons_block(step_outputs),
        "",
        "## 核心产物内容（deliverables 命中文件的真实内容，pattern 卡片的一手素材）",
        _build_core_artifacts_block(state) or "（无可读核心产物）",
    ]
    return "\n".join(parts)


def _fallback_user_summary(state: HwState, reason: str) -> str:
    """LLM 失败时兜底——纯模板拼装（不依赖 LLM）。"""
    intake = state.get("intake_result") or {}
    title = intake.get("title", "未命名作业")
    artifacts = _list_artifacts_from_workspace()
    runs = state.get("verifier_runs") or []
    last = runs[-1] if runs else {}
    missing = [m.get("constraint") or m for m in (last.get("coverage") or {}).get("missing", [])]

    lines = [
        f"# {title}",
        "",
        f"> 生成时间：{datetime.now().isoformat(timespec='seconds')}（LLM 失败兜底：{reason}）",
        "",
        "## 我做了什么",
        f"完成了「{title}」（type={intake.get('type','?')}）。详细决策见 progress_log。",
        "",
        "## 文件清单",
    ]
    if artifacts:
        for p in artifacts:
            lines.append(f"- `{p}`")
    else:
        lines.append("- （无产物）")
    lines += [
        "",
        "## 怎么验证",
        "- 检查上面文件清单内的产物",
        "- 如有 `test_*.py`，跑 `cd workspace && pytest -q`",
        "",
        "## 待办",
    ]
    if missing:
        for m in missing:
            lines.append(f"- [ ] {m}")
    else:
        lines.append("- （无）")
    return "\n".join(lines)


def run_summarizer(state: HwState) -> dict[str, Any]:
    """一次 LLM 调用产 user_summary + knowledge_cards；user_summary 写到 workspace/SUMMARY.md。"""
    user_msg = _build_facts(state)

    llm = get_llm()
    user_summary = ""
    knowledge_cards: list[dict] = []
    llm_error: str | None = None

    try:
        resp = llm.invoke(
            [
                SystemMessage(content=inject_for_agent("summarizer", SUMMARIZER_SYSTEM)),
                HumanMessage(content=user_msg),
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = parse_result_json(text)
        user_summary = str(data.get("user_summary") or "").strip()
        cards_raw = data.get("knowledge_cards") or []
        if isinstance(cards_raw, list):
            knowledge_cards = [
                {"type": str(c.get("type", "")).strip(), "content": str(c.get("content", "")).strip()}
                for c in cards_raw
                if isinstance(c, dict) and c.get("type") and c.get("content")
            ]
    except Exception as e:
        llm_error = f"{type(e).__name__}: {e}"

    if not user_summary:
        user_summary = _fallback_user_summary(state, llm_error or "LLM 输出空 user_summary")

    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(user_summary, encoding="utf-8")

    log_entry: dict[str, Any] = {
        "node": "summarizer",
        "summary_path": str(SUMMARY_PATH.relative_to(WORKSPACE_DIR)),
        "n_chars": len(user_summary),
        "n_cards": len(knowledge_cards),
    }
    if llm_error:
        log_entry["llm_error"] = llm_error

    return {
        "summary": user_summary,
        "knowledge_cards": knowledge_cards,
        "progress_log": [log_entry],
    }
