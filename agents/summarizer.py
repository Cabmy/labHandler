"""Summarizer agent - 双轨：用户面 SUMMARY.md + archive 面 lessons

输出（B 改造，PLAN/STEPS 已不再准确）：
- 写 `workspace/SUMMARY.md`（用户能直接看的人话提纲，4 节：我做了什么/文件清单/怎么验证/待办）
- 返回 `{summary, lessons}` 写入 HwState；`/done` 时 archive_task 直接读 state.lessons
  （不再从 SUMMARY.md 字符串中抽 "## 6. 教训与心得" 章节）

ground truth 来源：
- intake_result（题面）
- artifacts / workspace 实际产物
- verifier_runs[-1]（最后一次校验，含 missing）
- progress_log（节点执行日志摘要）

一次 LLM 调用产两段，由 prompts.SUMMARIZER_SYSTEM 强制 JSON schema：
  {"user_summary": "# ...完整 markdown...", "lessons": "- bullet1\n- bullet2"}
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import SUMMARIZER_SYSTEM, extract_result

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
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

    artifact_paths = [str(a.get("path", "")) for a in artifacts if a.get("path")]
    if not artifact_paths:
        artifact_paths = _list_artifacts_from_workspace()

    last_verifier = verifier_runs[-1] if verifier_runs else {}

    parts = [
        "## intake",
        json.dumps(intake, ensure_ascii=False, default=str)[:1500],
        "",
        "## artifacts（实际 workspace 文件）",
        json.dumps(artifact_paths, ensure_ascii=False, default=str)[:1000],
        "",
        "## user_constraints（用户对话累加约束）",
        json.dumps(user_cons, ensure_ascii=False, default=str)[:1000],
        "",
        "## verifier_runs[-1]（最后一次校验：verdict / missing / suggested_fix）",
        json.dumps(last_verifier, ensure_ascii=False, default=str)[:2000],
        "",
        "## progress_log（节点摘要，仅供参考；不要原文回填）",
        json.dumps(progress, ensure_ascii=False, default=str)[:2500],
    ]
    return "\n".join(parts)


def _parse_json(content: str) -> dict[str, Any]:
    """从 <result> 抽 JSON；失败时退化到首末花括号。"""
    text = extract_result(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        l, r = text.find("{"), text.rfind("}")
        if l >= 0 and r > l:
            return json.loads(text[l : r + 1])
        raise


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
    """一次 LLM 调用产 user_summary + lessons；user_summary 写到 workspace/SUMMARY.md。"""
    user_msg = _build_facts(state)

    llm = get_llm()
    user_summary = ""
    lessons = ""
    llm_error: str | None = None

    try:
        resp = llm.invoke(
            [
                SystemMessage(content=inject_for_agent("summarizer", SUMMARIZER_SYSTEM)),
                HumanMessage(content=user_msg),
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = _parse_json(text)
        user_summary = str(data.get("user_summary") or "").strip()
        lessons = str(data.get("lessons") or "").strip()
    except Exception as e:
        llm_error = f"{type(e).__name__}: {e}"

    if not user_summary:
        user_summary = _fallback_user_summary(state, llm_error or "LLM 输出空 user_summary")
    if not lessons:
        lessons = "（本次执行较顺利，无特殊教训。）"

    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(user_summary, encoding="utf-8")

    log_entry: dict[str, Any] = {
        "node": "summarizer",
        "summary_path": str(SUMMARY_PATH.relative_to(WORKSPACE_DIR)),
        "n_chars": len(user_summary),
        "lessons_chars": len(lessons),
    }
    if llm_error:
        log_entry["llm_error"] = llm_error

    return {
        "summary": user_summary,
        "lessons": lessons,
        "progress_log": [log_entry],
    }
