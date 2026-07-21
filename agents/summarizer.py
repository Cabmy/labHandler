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
    """workspace 当前可见的"用户产物"（排除 .hwhandler 内部 + __pycache__ + 自己写的 SUMMARY.md）"""
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


def _build_facts(state: HwState) -> str:
    """把可用事实拼成喂给 LLM 的 user_msg。"""
    intake = state.get("intake_result") or {}
    artifacts = state.get("artifacts") or []
    verifier_runs = state.get("verifier_runs") or []
    progress = state.get("progress_log") or []
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
        "## verifier_runs（全部校验轮次）",
        json.dumps(verifier_runs[-2:] if len(verifier_runs) > 1 else verifier_runs, ensure_ascii=False, default=str)[:2000],
        "",
        "## progress_log（节点摘要）",
        json.dumps(progress, ensure_ascii=False, default=str)[:3000],
        "",
        "## step_outputs（每步执行记录：id / name / error / skipped）",
        json.dumps(step_outputs, ensure_ascii=False, default=str)[:2500],
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
