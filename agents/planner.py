"""Planner agent - 负责任务拆解，将复杂作业转化为可执行的有向无环图 (DAG)，并召回相关经验。

该节点通过分析 Intake 提取的任务信息，构建一个包含多个步骤的 task_dag。每个步骤指定了依赖关系、
验收标准及预期产出。此外，它还会从历史库中检索相似任务的知识卡片（lesson/strategy/pattern）。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import PLANNER_SYSTEM, parse_result_json
from config.runtime import get_settings

ARCHIVE_TOP_K = get_settings().planner_archive_top_k


def _format_cards(items: list[dict]) -> str:
    """格式化知识卡片列表为 prompt 文本。"""
    if not items:
        return "（无相关历史经验卡片）"
    lines = []
    for i, item in enumerate(items, 1):
        card_type = item.get("card_type", "")
        content = item.get("content", "")
        title = item.get("task_title", "")
        ttype = item.get("task_type", "")
        type_label = {"lesson": "教训", "strategy": "策略", "pattern": "模式"}.get(card_type, card_type)
        lines.append(f"{i}. [{type_label}] [{ttype}] {title}")
        lines.append(f"   {content[:300]}")
    return "\n".join(lines)


def _compact_card(item: dict[str, Any]) -> str:
    """压缩卡片为单行管道字符串（写进 task_dag 给下游）。"""
    card_type = item.get("card_type", "")
    content = (item.get("content") or "").strip()[:200]
    title = item.get("task_title", "")
    ttype = item.get("task_type", "")
    return f"[{card_type}][{ttype}] {title} | {content}"


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


def run_planner(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口。"""
    intake = state.get("intake_result") or {}
    user_constraints = state.get("user_constraints") or []
    skill_name = (intake.get("type") or "other").lower()

    # 召回历史经验卡片
    try:
        from tools.rag_tool import archive_search

        query = intake.get("title") or state.get("question", "")
        card_types = ["lesson", "strategy"]
        if skill_name == "coding":
            card_types.append("pattern")

        retrieved_cards: list[dict] = archive_search.invoke({
            "query": query,
            "limit": ARCHIVE_TOP_K,
            "card_types": card_types,
            "task_type": None,
        })
    except Exception:
        retrieved_cards = []

    # 加载 skill SOP
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
        f"## 相关历史经验卡片\n{_format_cards(retrieved_cards)}",
    ]
    if skill_body:
        user_msg_parts.append(f"## skill SOP（{skill_name}）\n{skill_body[:2000]}")

    # Replan: 加上 Verifier 反馈 + 旧 DAG + workspace 产物
    verifier_runs = state.get("verifier_runs") or []
    replan_query = query  # 默认用首轮 query
    if verifier_runs:
        last = verifier_runs[-1]
        cov = last.get("coverage") or {}
        missing = [m.get("constraint", "") for m in (cov.get("missing") or [])]
        sf = last.get("suggested_fix") or ""
        stage1 = last.get("stage1_failures") or []
        evidence = last.get("evidence") or {}
        feedback_parts = [
            "## 上一轮 Verifier 反馈",
            f"- verdict: {last.get('verdict', '')}",
            f"- stage1_failures: {stage1}",
            f"- missing: {missing}",
            f"- suggested_fix: {sf}",
        ]
        pytest_tail = evidence.get("pytest_output_tail", "")
        if pytest_tail:
            feedback_parts.append("- pytest_output（末段）:\n```\n" + pytest_tail[:500] + "\n```")
        user_msg_parts.append("\n".join(feedback_parts))

        # Replan 时也用缺漏信息重新检索卡片
        try:
            replan_query = f"{query} {' '.join(missing)} {sf}"
            replan_cards = archive_search.invoke({
                "query": replan_query,
                "limit": ARCHIVE_TOP_K,
                "card_types": card_types,
                "task_type": None,
            })
            if replan_cards:
                retrieved_cards = replan_cards  # 替换为更相关的卡片
        except Exception:
            pass

        # 旧 DAG
        prev_dag = state.get("task_dag") or {}
        prev_nodes = prev_dag.get("nodes") or []
        if prev_nodes:
            try:
                dag_brief = json.dumps(
                    [{"id": n.get("id"), "name": n.get("name"),
                      "depends_on": n.get("depends_on") or [],
                      "expected_artifacts": n.get("expected_artifacts") or []}
                     for n in prev_nodes],
                    ensure_ascii=False,
                )
            except Exception:
                dag_brief = str(prev_nodes)
            user_msg_parts.append(f"## 上一轮 task_dag（节点摘要）\n{dag_brief[:1500]}")

        # workspace 现有产物
        try:
            ws = get_settings().workspace_dir
            files = []
            for p in ws.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(ws)
                if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                    continue
                files.append(str(rel))
            if files:
                files.sort()
                user_msg_parts.append(
                    "## workspace 现有产物\n" + "\n".join(f"- {f}" for f in files[:50])
                )
        except Exception:
            pass

    user_msg = "\n\n".join(user_msg_parts)

    llm = get_llm()
    resp = llm.invoke(
        [SystemMessage(content=inject_for_agent("planner", PLANNER_SYSTEM)), HumanMessage(content=user_msg)]
    )
    content = resp.content if isinstance(resp.content, str) else str(resp.content)

    try:
        data = parse_result_json(content)
    except Exception as e:
        data = _fallback_dag(skill_name, intake.get("title", "任务"), str(e))

    nodes = data.get("nodes") or []
    cleaned_nodes = []
    for n in nodes:
        cleaned_nodes.append({
            "id": str(n.get("id") or f"n{len(cleaned_nodes)+1}"),
            "name": str(n.get("name") or "未命名节点"),
            "agent": str(n.get("agent") or "coder").lower(),
            "depends_on": list(n.get("depends_on") or []),
            "desc": str(n.get("desc") or ""),
            "acceptance_criteria": list(n.get("acceptance_criteria") or []),
            "expected_artifacts": list(n.get("expected_artifacts") or []),
            "suggested_tools": list(n.get("suggested_tools") or []),
            # 当前 step 相关的 pattern/strategy 卡片（给 Coder 参考）
            "context_cards": [_compact_card(c) for c in retrieved_cards
                             if c.get("card_type") in ("pattern", "strategy")],
        })

    # 保留所有卡片（lesson 给 Verifier 参考）
    all_compact = [_compact_card(c) for c in retrieved_cards]

    task_dag = {
        "skill": data.get("skill") or skill_name,
        "nodes": cleaned_nodes,
        "retrieved_cards": all_compact,
    }

    return {
        "task_dag": task_dag,
        "iteration": int(state.get("iteration", 0)) + 1,
        "current_step_idx": 0,
        "progress_log": [{
            "node": "planner",
            "iteration": int(state.get("iteration", 0)) + 1,
            "skill": task_dag["skill"],
            "n_nodes": len(cleaned_nodes),
            "n_cards": len(all_compact),
        }],
    }


def _fallback_dag(skill: str, title: str, reason: str) -> dict[str, Any]:
    _empty = {"acceptance_criteria": [], "expected_artifacts": [], "suggested_tools": []}
    if skill == "coding":
        nodes = [
            {"id": "n1", "name": "实现", "agent": "coder", "depends_on": [],
             "desc": f"在沙箱实现 {title}（文件名按主题命名）", **_empty},
            {"id": "n2", "name": "测试", "agent": "coder", "depends_on": ["n1"],
             "desc": "编写并跑通 pytest 测试", **_empty},
        ]
    elif skill in {"essay", "lab_report"}:
        nodes = [
            {"id": "n1", "name": "起草", "agent": "coder", "depends_on": [],
             "desc": f"写 {title}", **_empty},
        ]
    else:
        nodes = [
            {"id": "n1", "name": "执行", "agent": "coder", "depends_on": [], "desc": title, **_empty},
        ]
    return {"skill": skill, "nodes": nodes, "_fallback_reason": reason}