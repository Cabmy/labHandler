"""LangGraph 主图 - hwHandler 编排层（P5.1）

节点结构（用户决策"单 Coder 节点 ReAct 全包"）：
  START → Intake → Planner → Coder → Verifier → [conditional]
                                                  ├ replan ─ → Planner（loop）
                                                  └ done   ─ → Compile → Summarizer → END

路由规则（详见 orchestrator/replan.py）：
  pass                           → "compile"
  fail 且 iteration < MAX_REPLAN → "planner"
  fail 且 iteration ≥ MAX_REPLAN → "compile"（标 partial=true）

关键约束：
- iteration 由 Planner 节点 +1（每次进 Planner 都计数）
- HwState 多个字段用 Annotated[list, add] 让 LangGraph 自动累加（progress_log / verifier_runs / artifacts / user_constraints）
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from agents.coder import run_coder
from agents.intake import run_intake
from agents.planner import run_planner
from agents.summarizer import run_summarizer
from agents.verifier import run_verifier
from orchestrator.compile_node import run_compile
from orchestrator.replan import replan_router
from orchestrator.state import HwState


def _entry_router(state: HwState) -> Literal["intake", "planner"]:
    """REPL 入口路由：首轮没 intake_result → intake；后续 REPL 输入 → 直接 planner，
    让累积的 user_constraints + prior verifier_runs + intake_result 作为修订上下文喂给 planner。"""
    return "planner" if state.get("intake_result") else "intake"


def build_graph() -> Any:
    """构建并 compile 主图。"""
    graph = StateGraph(HwState)

    # 节点
    graph.add_node("intake", run_intake)
    graph.add_node("planner", run_planner)
    graph.add_node("coder", run_coder)
    graph.add_node("verifier", run_verifier)
    graph.add_node("compile", run_compile)
    graph.add_node("summarizer", run_summarizer)

    # START 条件路由：首轮 intake，后续直接 planner（带累积上下文）
    graph.add_conditional_edges(
        START,
        _entry_router,
        {"intake": "intake", "planner": "planner"},
    )
    graph.add_edge("intake", "planner")
    graph.add_edge("planner", "coder")
    graph.add_edge("coder", "verifier")

    # 条件边：Verifier 后看 verdict 决定 Replan 或 Compile
    graph.add_conditional_edges(
        "verifier",
        replan_router,
        {
            "planner": "planner",  # fail 且未到 MAX → 重拆 DAG
            "compile": "compile",  # pass / fail 超限 → 收尾
        },
    )

    graph.add_edge("compile", "summarizer")
    graph.add_edge("summarizer", END)

    return graph.compile()


# 全局单例（首次访问时编译）
_compiled_graph: Any = None


def get_graph() -> Any:
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph
