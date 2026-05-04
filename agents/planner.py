"""Planner agent - 拆子任务 DAG + 选 skill + 拼历史经验

输出 task_dag（HwState 子结构）：
  {
    "skill":  str,           # 选中的 skill 名（coding/essay/lab_report/other）
    "nodes":  list[dict],    # [{id, name, agent, depends_on, desc}]
    "lessons": list[str],    # archive_search 召回的历史经验摘要
  }

设计要点（PLAN §8.2 / STEPS P4.2）：
1. skill 选择：直接读 intake_result.type；type=other 时跳过 skill 加载
2. archive_search 召回 Top-3 历史相似任务的 lessons 拼进 prompt
3. LLM 一次性输出 JSON DAG（节点带 id / agent / depends_on）
4. 节点 agent 取值：coder / verifier / summarizer（Phase 4 范围内）
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from prompts import PLANNER_SYSTEM, extract_result

ARCHIVE_TOP_K = int(os.getenv("PLANNER_ARCHIVE_TOP_K", "3"))


_PLANNER_SYSTEM = PLANNER_SYSTEM  # 局部引用名兼容


def _format_lessons(lessons: list[dict]) -> str:
    if not lessons:
        return "（无相似历史任务）"
    lines = []
    for i, item in enumerate(lessons, 1):
        title = item.get("task_title", "")
        ttype = item.get("task_type", "")
        lesson = (item.get("lessons") or "").strip()
        if not lesson:
            continue
        lines.append(f"{i}. [{ttype}] {title}：{lesson[:300]}")
    return "\n".join(lines) or "（历史任务存在但未沉淀有效 lessons）"


def _format_intake(intake: dict[str, Any]) -> str:
    title = intake.get("title", "")
    ttype = intake.get("type", "other")
    deliv = intake.get("deliverables") or []
    cons = intake.get("constraints") or []
    parts = [
        f"标题：{title}",
        f"类型：{ttype}",
        f"交付物：{deliv if deliv else '（未明示，需 Coder 推断）'}",
        f"题面约束：{cons if cons else '（无）'}",
    ]
    return "\n".join(parts)


def _parse_json(content: str) -> dict[str, Any]:
    """容错 JSON 解析：先抽 <result> 段，再退化到老路径"""
    text = extract_result(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        l, r = text.find("{"), text.rfind("}")
        if l >= 0 and r > l:
            return json.loads(text[l : r + 1])
        raise


def run_planner(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口。"""
    intake = state.get("intake_result") or {}
    user_constraints = state.get("user_constraints") or []
    skill_name = (intake.get("type") or "other").lower()

    # 召回历史经验
    try:
        from tools.rag_tool import archive_search

        query = intake.get("title") or state.get("question", "")
        lessons = archive_search.invoke({"query": query, "limit": ARCHIVE_TOP_K})
    except Exception:
        lessons = []

    # 加载 skill SOP（other 类型跳过）
    skill_body = ""
    if skill_name in {"coding", "essay", "lab_report"}:
        try:
            from tools.skill_tool import get_skill_body

            skill_body = get_skill_body(skill_name) or ""
        except Exception:
            skill_body = ""

    # 拼 prompt
    user_msg_parts = [
        f"## intake_result\n{_format_intake(intake)}",
        f"## 用户补充约束\n{user_constraints if user_constraints else '（无）'}",
        f"## 历史相似任务\n{_format_lessons(lessons)}",
    ]
    if skill_body:
        user_msg_parts.append(f"## skill SOP（{skill_name}）\n{skill_body[:2000]}")

    # Replan: 把上一轮 Verifier 反馈塞进 prompt
    # ① 让 DAG 真的能基于失败信号调整（否则 2 次 Planner 输出几乎一致）
    # ② 输入与首轮不同，避开 langchain SQLite cache 命中导致 stream_mode='messages'
    #    无 chunk 流出 → live_panel 拿不到 token → CLI 看不到第 2 次 Planner 的思考过程
    verifier_runs = state.get("verifier_runs") or []
    if verifier_runs:
        last = verifier_runs[-1]
        cov = last.get("coverage") or {}
        missing = [m.get("constraint", "") for m in (cov.get("missing") or [])]
        sf = last.get("suggested_fix") or ""
        user_msg_parts.append(
            "## 上一轮 Verifier 反馈\n"
            f"- verdict: {last.get('verdict', '')}\n"
            f"- missing: {missing}\n"
            f"- suggested_fix: {sf}\n"
            "请据此调整 DAG（聚焦 missing 项，改写节点 desc 或增设修复节点）。"
        )

    user_msg = "\n\n".join(user_msg_parts)

    llm = get_llm()
    resp = llm.invoke(
        [SystemMessage(content=inject_for_agent("planner", _PLANNER_SYSTEM)), HumanMessage(content=user_msg)]
    )
    content = resp.content if isinstance(resp.content, str) else str(resp.content)

    try:
        data = _parse_json(content)
    except Exception as e:
        # 兜底 DAG：直接按 type 给一个 2-3 节点的最小流
        data = _fallback_dag(skill_name, intake.get("title", "任务"), str(e))

    nodes = data.get("nodes") or []
    # 标准化节点字段
    cleaned_nodes = []
    for n in nodes:
        cleaned_nodes.append(
            {
                "id": str(n.get("id") or f"n{len(cleaned_nodes)+1}"),
                "name": str(n.get("name") or "未命名节点"),
                "agent": str(n.get("agent") or "coder").lower(),
                "depends_on": list(n.get("depends_on") or []),
                "desc": str(n.get("desc") or ""),
            }
        )

    task_dag = {
        "skill": data.get("skill") or skill_name,
        "nodes": cleaned_nodes,
        "lessons": [_compact_lesson(l) for l in lessons],
    }

    return {
        "task_dag": task_dag,
        "iteration": int(state.get("iteration", 0)) + 1,  # Replan 计数
        "progress_log": [
            {
                "node": "planner",
                "iteration": int(state.get("iteration", 0)) + 1,
                "skill": task_dag["skill"],
                "n_nodes": len(cleaned_nodes),
                "n_lessons": len(task_dag["lessons"]),
            }
        ],
    }


def _compact_lesson(item: dict[str, Any]) -> str:
    return (
        f"[{item.get('task_type','')}] {item.get('task_title','')}："
        f"{(item.get('lessons') or '').strip()[:200]}"
    )


def _fallback_dag(skill: str, title: str, reason: str) -> dict[str, Any]:
    """LLM 解析失败时的兜底 DAG（保证主图能跑下去）

    Verifier / Summarizer 是主图固定的收尾节点，不由 planner 拆——
    fallback 也只产 coder 节点。
    """
    if skill == "coding":
        nodes = [
            {"id": "n1", "name": "实现", "agent": "coder", "depends_on": [],
             "desc": f"在沙箱实现 {title}（文件名按主题命名）"},
            {"id": "n2", "name": "测试", "agent": "coder", "depends_on": ["n1"],
             "desc": "编写并跑通 pytest 测试"},
        ]
    elif skill in {"essay", "lab_report"}:
        nodes = [
            {"id": "n1", "name": "起草", "agent": "coder", "depends_on": [],
             "desc": f"写 {title}"},
        ]
    else:
        nodes = [
            {"id": "n1", "name": "执行", "agent": "coder", "depends_on": [], "desc": title},
        ]
    return {
        "skill": skill,
        "nodes": nodes,
        "_fallback_reason": reason,
    }
