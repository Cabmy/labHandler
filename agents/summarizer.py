"""Summarizer agent —— 负责生成最终任务总结报告并蒸馏知识卡片。

输出：
1. SUMMARY.md（面向用户）：生成在 workspace 目录，含完成任务总结、
   产物清单、验证方法与剩余待办。
2. knowledge_cards（系统侧）：从执行轨迹蒸馏结构化知识卡片（lesson/strategy/pattern），
   供 /done 归档后未来 Planner 检索。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from llm import get_llm
from llm.invoke import invoke_llm_json, usage_log_fields
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import SUMMARIZER_SYSTEM
from config.runtime import get_settings
from tools.workspace_utils import iter_workspace_files

WORKSPACE_DIR: Path = get_settings().workspace_dir
SUMMARY_PATH = WORKSPACE_DIR / "SUMMARY.md"


def _list_artifacts_from_workspace() -> list[str]:
    """workspace 当前可见的 '用户产物'（排除 .labhandler 内部 + __pycache__ + 我们自己的 SUMMARY.md）。"""
    out: list[str] = []
    for p in iter_workspace_files(WORKSPACE_DIR):
        rel = p.relative_to(WORKSPACE_DIR)
        # 我们生成的 SUMMARY.md 不算 "用户产物"
        if str(rel) == "SUMMARY.md":
            continue
        out.append(str(rel))
    return sorted(out)


def _build_calibration_block(verifier_runs: list[dict]) -> str:
    """Verifier 事实校准层：最后一轮 coverage 的 covered/missing/suggested_fix。

    这是产物达标情况的权威事实 —— **不截断** —— lesson 对齐 missing
    反面、strategy 对齐实际 pass 路径都靠它；截断会抽掉蒸馏依据。
    """
    if not verifier_runs:
        return "(no verifier run record)"
    last = verifier_runs[-1]
    cov = last.get("coverage") or {}
    lines = [f"verdict: {last.get('verdict', '?')}"]
    covered = cov.get("covered") or []
    lines.append("covered:")
    for c in covered:
        if isinstance(c, dict):
            lines.append(f"  - {c.get('constraint','')} | evidence: {c.get('evidence','')}")
        else:
            lines.append(f"  - {c}")
    if not covered:
        lines.append("  - (none)")
    missing = cov.get("missing") or []
    lines.append("missing:")
    for m in missing:
        if isinstance(m, dict):
            lines.append(f"  - {m.get('constraint','')} | reason: {m.get('reason','')}")
        else:
            lines.append(f"  - {m}")
    if not missing:
        lines.append("  - (none)")
    sf = last.get("suggested_fix") or ""
    if sf:
        lines.append(f"suggested_fix: {sf}")
    return "\n".join(lines)


def _build_evolution_block(state: HwState, max_item_chars: int = 150) -> str:
    """跨轮演化块：按 progress_log 时间线拆轮（planner → coder_steps → verifier）。

    校准层只留最后一轮；多轮 Replan 时 '上轮 missing → 本轮修复动作' 的配对
    是 strategy 卡片的核心素材，所以保留全部轮次（单项截断，轮次不丢）。
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
        lines.append(f"### Round {r.get('iteration', '?')} (DAG {r.get('n_nodes', '?')} nodes)")
        lines.extend(f"- {s}" for s in r["steps"])
        run = r.get("verifier")
        if not run:
            continue
        lines.append(f"- verifier: {run.get('verdict', '?')}")
        for f in run.get("stage1_failures") or []:
            lines.append(f"  - [hard metric] {str(f)[:max_item_chars]}")
        for m in (run.get("coverage") or {}).get("missing") or []:
            if isinstance(m, dict):
                lines.append(
                    f"  - [missing] {str(m.get('constraint', ''))[:max_item_chars]}"
                    f" ({str(m.get('reason', ''))[:max_item_chars]})"
                )
            else:
                lines.append(f"  - [missing] {str(m)[:max_item_chars]}")
        sf = run.get("suggested_fix") or ""
        if sf:
            lines.append(f"  - [fix suggestion] {str(sf)[:max_item_chars]}")
    return "\n".join(lines)


def _build_core_artifacts_block(
    state: HwState, per_file: int = 1500, total: int = 6000
) -> str:
    """核心产物内容窗口：命中 deliverables / expected_artifacts 的文件的真实文本内容。

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
    for p in iter_workspace_files(WORKSPACE_DIR, extensions=exts):
        if used >= total:
            break
        if p.name not in names:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = p.relative_to(WORKSPACE_DIR)
        budget = min(per_file, total - used)
        clipped = text[:budget]
        suffix = "\n…(truncated)" if len(text) > len(clipped) else ""
        chunks.append(f"### {rel}\n{clipped}{suffix}")
        used += len(clipped)
    return "\n\n".join(chunks)


def _build_lessons_block(step_outputs: list[dict]) -> str:
    """step_lessons 全量块（不截断）：Coder 逐步蒸馏的原始教训 + retry 原因。"""
    lines: list[str] = []
    for o in step_outputs:
        sid = o.get("id", "?")
        for lesson in o.get("step_lessons") or []:
            lines.append(f"- [step {sid}] {lesson}")
        if o.get("retry_reason"):
            lines.append(f"- [step {sid} retry] {o['retry_reason']}")
        if o.get("error"):
            lines.append(f"- [step {sid} error] {o['error']}")
    return "\n".join(lines) if lines else "(none)"


def _user_turns(messages: list[dict]) -> list[dict]:
    """从 messages 里挑出**用户真实输入轮次**。

    messages 现在是归约字段，跨 step 累积，里面混着 Coder 单步执行的
    上下文块（role=user 但带 step_id 归属）与工具/助手消息。
    只有 session.append_user_message 追加的那些没有 step_id —— 它们才是
    「用户多轮对话」。

    踩坑记录：本块过去直接 json.dumps 整个 messages 前 2000 字符，而当时
    messages 被每个 coder_step 整体覆写、首条恰好是 Coder 的 system prompt，
    于是喂给 Summarizer 的「用户多轮对话」实际是 CODER_BASE_PROMPT 的前 2000 字符。
    """
    return [
        {"role": m.get("role"), "content": m.get("content")}
        for m in messages or []
        if m.get("role") == "user" and not m.get("step_id")
    ]


def _build_facts(state: HwState) -> str:
    """将可用事实组装为喂给 LLM 的 user_msg。"""
    intake = state.get("intake_result") or {}
    artifacts = state.get("artifacts") or []
    verifier_runs = state.get("verifier_runs") or []
    user_cons = state.get("user_constraints") or []
    step_outputs = state.get("step_outputs") or []
    user_turns = _user_turns(state.get("messages") or [])

    artifact_paths = [str(a.get("path", "")) for a in artifacts if a.get("path")]
    if not artifact_paths:
        artifact_paths = _list_artifacts_from_workspace()

    parts = [
        "## intake",
        json.dumps(intake, ensure_ascii=False, default=str)[:1500],
        "",
        "## messages (user multi-round dialogue, including corrections/feedback/supplements)",
        json.dumps(user_turns, ensure_ascii=False, default=str)[:2000],
        "",
        "## artifacts (actual workspace files)",
        json.dumps(artifact_paths, ensure_ascii=False, default=str)[:1000],
        "",
        "## user_constraints (constraints accumulated from user dialogue)",
        json.dumps(user_cons, ensure_ascii=False, default=str)[:1000],
        "",
        "## Verifier fact calibration layer (last round's covered/missing/suggested_fix; authoritative compliance fact; not truncated)",
        _build_calibration_block(verifier_runs),
        "",
        "## Cross-round evolution timeline (each round's planner→steps→verifier; 'prev round missing → this round fix' is strategy material)",
        _build_evolution_block(state) or "(single-round completion, no evolution)",
        "",
        "## step_outputs (per-step execution record: id / name / status / error / skipped)",
        json.dumps(step_outputs, ensure_ascii=False, default=str)[:2000],
        "",
        "## step_lessons full (Coder raw lessons + retry/error reasons; not truncated; first-hand material for distilling lessons)",
        _build_lessons_block(step_outputs),
        "",
        "## Core artifact content (real content of files hit by deliverables; first-hand material for pattern cards)",
        _build_core_artifacts_block(state) or "(no readable core artifacts)",
    ]
    return "\n".join(parts)


def _fallback_user_summary(state: HwState, reason: str) -> str:
    """LLM 失败时的兜底 —— 纯模板拼装（不依赖 LLM）。"""
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
    """单次 LLM 调用产出 user_summary + knowledge_cards；user_summary 写入 workspace/SUMMARY.md。"""
    user_msg = _build_facts(state)

    llm = get_llm()
    user_summary = ""
    knowledge_cards: list[dict] = []
    llm_error: str | None = None

    try:
        data = invoke_llm_json(
            llm,
            inject_for_agent("summarizer", SUMMARIZER_SYSTEM),
            user_msg,
            agent="summarizer",
        )
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
    # token 真值 + 前缀缓存命中（progress_log 是全任务 token 的单一账本）
    log_entry.update(usage_log_fields("summarizer"))

    return {
        "summary": user_summary,
        "knowledge_cards": knowledge_cards,
        "progress_log": [log_entry],
    }
