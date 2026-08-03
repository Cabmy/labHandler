"""Planner agent —— 负责任务拆解，将复杂作业转化为可执行 DAG 并召回相关经验。

本节点分析 Intake 提取的任务信息，构建含多个 step 的 task_dag。
每个 step 指明依赖、验收标准与预期产出。
它还会从历史归档中检索知识卡片（lesson/strategy/pattern）。
"""

from __future__ import annotations

import json
from typing import Any

from llm import get_llm
from llm.invoke import invoke_llm_json, usage_log_fields
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import PLANNER_SYSTEM
from config.runtime import get_settings

ARCHIVE_TOP_K = get_settings().planner_archive_top_k


def _format_cards(items: list[dict]) -> str:
    """将知识卡片列表格式化为 prompt 文本。"""
    if not items:
        return "(No relevant historical experience cards)"
    lines = []
    for i, item in enumerate(items, 1):
        card_type = item.get("card_type", "")
        content = item.get("content", "")
        title = item.get("task_title", "")
        ttype = item.get("task_type", "")
        type_label = {"lesson": "Lesson", "strategy": "Strategy", "pattern": "Pattern"}.get(card_type, card_type)
        lines.append(f"{i}. [{type_label}] [{ttype}] {title}")
        lines.append(f"   {content[:300]}")
    return "\n".join(lines)


def _compact_card(item: dict[str, Any]) -> str:
    """压缩卡片为单行管道串（写入 task_dag 供下游使用）。"""
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
        f"Title: {title}",
        f"Type: {ttype}",
        f"Deliverables: {deliv if deliv else '(Not specified, Coder must infer)'}",
        f"Constraints: {cons if cons else '(None)'}",
    ]
    return "\n".join(parts)


def _build_replan_context(
    state: HwState,
    query: str,
    card_types: list[str],
    retrieved_cards: list[dict],
) -> tuple[list[str], list[dict]]:
    """为 replan 场景构建额外的 message 部分。

    当 verifier_runs 存在时（即失败后重新规划），向 user_msg_parts 追加
    Verifier 反馈、执行轨迹、重新检索的卡片、上一轮 DAG 摘要与
    workspace 产物列表。

    返回 (additional_parts, possibly_replaced_cards)。
    """
    extra_parts: list[str] = []
    verifier_runs = state.get("verifier_runs") or []
    if not verifier_runs:
        return extra_parts, retrieved_cards

    last = verifier_runs[-1]
    cov = last.get("coverage") or {}
    missing = [m.get("constraint", "") for m in (cov.get("missing") or [])]
    sf = last.get("suggested_fix") or ""
    stage1 = last.get("stage1_failures") or []
    evidence = last.get("evidence") or {}
    feedback_parts = [
        "## Previous Verifier Feedback",
        f"- verdict: {last.get('verdict', '')}",
        f"- stage1_failures: {stage1}",
        f"- missing: {missing}",
        f"- suggested_fix: {sf}",
    ]
    pytest_tail = evidence.get("pytest_output_tail", "")
    if pytest_tail:
        feedback_parts.append("- pytest_output (tail):\n```\n" + pytest_tail[:500] + "\n```")
    extra_parts.append("\n".join(feedback_parts))

    # 执行轨迹摘要（复用 Verifier 的构建器）：告诉修补节点哪个
    # step 失败/跳过及原因，避免重复依赖错误
    from agents.verifier import build_execution_trace
    trace_block = build_execution_trace(state)
    if trace_block:
        extra_parts.append(trace_block.rstrip())

    # replan 时用 gap 信息重新检索卡片
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

    # 上一轮 DAG 摘要
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
        extra_parts.append(f"## Previous task_dag (node summary)\n{dag_brief[:1500]}")

    # 现有 workspace 产物
    try:
        from tools.workspace_utils import iter_workspace_files
        ws = get_settings().workspace_dir
        files = sorted(str(p.relative_to(ws)) for p in iter_workspace_files(ws))
        if files:
            extra_parts.append(
                "## Existing workspace artifacts\n" + "\n".join(f"- {f}" for f in files[:50])
            )
    except Exception:
        pass

    return extra_parts, retrieved_cards


def run_planner(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口。"""
    intake = state.get("intake_result") or {}
    user_constraints = state.get("user_constraints") or []
    skill_name = (intake.get("type") or "other").lower()

    # 召回历史经验卡片
    query = intake.get("title") or state.get("question", "")
    card_types = ["lesson", "strategy"]
    if skill_name == "coding":
        card_types.append("pattern")
    try:
        from tools.rag_tool import archive_search

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

    # 组装 prompt
    user_msg_parts = [
        f"## intake_result\n{_format_intake(intake)}",
        f"## User supplementary constraints\n{user_constraints if user_constraints else '(None)'}",
        f"## Relevant historical experience cards\n{_format_cards(retrieved_cards)}",
    ]
    if skill_body:
        user_msg_parts.append(f"## skill SOP ({skill_name})\n{skill_body[:2000]}")

    # Replan：追加 Verifier 反馈 + 执行轨迹 + 旧 DAG + workspace 产物
    replan_parts, retrieved_cards = _build_replan_context(
        state, query, card_types, retrieved_cards,
    )
    user_msg_parts.extend(replan_parts)

    user_msg = "\n\n".join(user_msg_parts)

    llm = get_llm()
    try:
        data = invoke_llm_json(
            llm,
            inject_for_agent("planner", PLANNER_SYSTEM),
            user_msg,
            agent="planner",
        )
    except Exception as e:
        data = _fallback_dag(skill_name, intake.get("title", "任务"), str(e))

    nodes = data.get("nodes") or []
    cleaned_nodes = []
    for n in nodes:
        cleaned_nodes.append({
            "id": str(n.get("id") or f"n{len(cleaned_nodes)+1}"),
            "name": str(n.get("name") or "Unnamed node"),
            "desc": str(n.get("desc") or ""),
            "acceptance_criteria": list(n.get("acceptance_criteria") or []),
            "expected_artifacts": list(n.get("expected_artifacts") or []),
            "suggested_tools": list(n.get("suggested_tools") or []),
            # context_cards：与当前 step 相关的 pattern/strategy 卡片（供 Coder 参考）
            "context_cards": [_compact_card(c) for c in retrieved_cards
                             if c.get("card_type") in ("pattern", "strategy")],
        })

    # id 唯一性防护：新节点 id 既不能与历史 step_outputs 撞名，同一 DAG 内也不能自撞。
    # 撞名后果：Coder 的 [done] 标记与重试预算都按 (id, iteration) 配对，
    # 同轮内自撞会让第二个同名节点一出生就背着前一个的完成标记与尝试次数。
    # 后缀用 while 递增探测，而不是固定的 _r{iteration+1}：
    # session.prepare_task 在修订路径会把 iteration 重置为 0，
    # 固定后缀会在多次修订之间重复生成同一个 id，重命名等于没做。
    seen_ids: set[str] = {
        str(o.get("id")) for o in state.get("step_outputs") or [] if o.get("id")
    }
    for n in cleaned_nodes:
        if n["id"] in seen_ids:
            base, k = n["id"], 1
            while f"{base}_r{k}" in seen_ids:
                k += 1
            n["id"] = f"{base}_r{k}"
        seen_ids.add(n["id"])

    # 执行模型：严格串行（列表顺序 = 执行顺序），depends_on 统一为
    # 单链：LLM 无法表达与列表顺序冲突的拓扑（ phantom failure /
    # 空中楼阁场景从根源消除）
    for i, n in enumerate(cleaned_nodes):
        n["depends_on"] = [cleaned_nodes[i - 1]["id"]] if i else []

    # 保留全部卡片（lessons 供 Verifier 参考）
    all_compact = [_compact_card(c) for c in retrieved_cards]

    task_dag = {
        "skill": data.get("skill") or skill_name,
        "nodes": cleaned_nodes,
        "retrieved_cards": all_compact,
    }

    log_entry: dict[str, Any] = {
        "node": "planner",
        "iteration": int(state.get("iteration", 0)) + 1,
        "skill": task_dag["skill"],
        "n_nodes": len(cleaned_nodes),
        "n_cards": len(all_compact),
    }
    # token 真值 + 前缀缓存命中（progress_log 是全任务 token 的单一账本）
    log_entry.update(usage_log_fields("planner"))

    return {
        "task_dag": task_dag,
        "iteration": int(state.get("iteration", 0)) + 1,
        "current_step_idx": 0,
        "progress_log": [log_entry],
    }


def _fallback_dag(skill: str, title: str, reason: str) -> dict[str, Any]:
    """LLM 解析失败时的兜底 DAG（depends_on 由 run_planner 清理覆写为单链）。"""
    _empty = {"acceptance_criteria": [], "expected_artifacts": [], "suggested_tools": []}
    if skill == "coding":
        nodes = [
            {"id": "n1", "name": "Implementation",
             "desc": f"Implement {title} in sandbox (filename per topic naming)", **_empty},
            {"id": "n2", "name": "Testing",
             "desc": "Write and run pytest tests", **_empty},
        ]
    elif skill in {"essay", "lab_report"}:
        nodes = [
            {"id": "n1", "name": "Drafting", "desc": f"Write {title}", **_empty},
        ]
    else:
        nodes = [
            {"id": "n1", "name": "Execution", "desc": title, **_empty},
        ]
    return {"skill": skill, "nodes": nodes, "_fallback_reason": reason}