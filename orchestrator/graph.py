"""LangGraph 主图 - labHandler 编排层（Plan-and-Execute Lite）

节点结构（Coder 单步执行 + step_router 自循环 + Replan 循环）：

  START → Intake → Planner → coder_step ──→ step_router
                    ↑                            │
                    │  fail (iter<MAX,            ├ next ─→ coder_step（跑下一步）
                    │  reset idx=0)               └ done ─→ Verifier
                    │                                          │
                    └──────────────────────────────────────────┤
                                pass / fail (iter≥MAX)         │
                                       → Compile → Summarizer → END

关键约束：
- coder_step **单步执行**：每次只跑 task_dag.nodes[current_step_idx] 那一个 step
- step_router 控制 step 循环：current_step_idx < len(nodes) → 回 coder_step；否则 → verifier
- **有界原地重试**：Coder Final Answer 报 needs_retry 时 idx 不推进
  （上限 MAX_STEP_RETRY 次），step_router 的 'next' 意味着重跑同一 step；
  耗尽后标为 failed 并推进，触发 error 短路
- iteration 由 Planner 节点 +1；planner 节点同时 reset current_step_idx=0（含 Replan）
- HwState 多个字段用 Annotated[list, add] 由 LangGraph 自动累加：
  progress_log / verifier_runs / artifacts / user_constraints / step_outputs

路由规则（详见 orchestrator/replan.py）：
  pass                           → "compile"
  fail 且 iteration < MAX_REPLAN → "planner"（reset idx=0，重拆 DAG）
  fail 且 iteration ≥ MAX_REPLAN → "compile"（标 partial=true）
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from agents.coder import run_coder_step
from agents.intake import run_intake
from agents.planner import run_planner
from agents.summarizer import run_summarizer
from agents.verifier import run_verifier
from orchestrator.compile_node import run_compile
from orchestrator.replan import replan_router
from orchestrator.state import HwState


def _entry_router(state: HwState) -> Literal["intake", "planner"]:
    """REPL 入口路由：首轮无 intake_result → intake；后续 REPL 输入 → 直接 planner，
    让累积的 user_constraints + 历史 verifier_runs + intake_result 作为 planner 的修订上下文。"""
    return "planner" if state.get("intake_result") else "intake"


def step_router(state: HwState) -> Literal["next", "done"]:
    """coder_step 完成后的条件路由（Plan-and-Execute Lite + 有界原地重试）：

    - "done" → 上一步出错（依赖检查失败 / 执行异常 / 重试耗尽标 failed 等），
               跳过剩余 step 直接进 verifier；或者所有 step 已跑完
    - "next" → 还有未完成 step 且上一步无错误，回 coder_step；
               上一步报 needs_retry 时 coder 未推进 idx，'next' 意味着原地重跑同一 step
    """
    # 上一步出错则短路到 verifier，不让下游 step 在空地基上浪费 LLM 调用；
    # needs_retry 不算 error（属于有界重试的正常路径，防御性排除）
    step_outputs = state.get("step_outputs") or []
    if step_outputs:
        last = step_outputs[-1]
        if last.get("error") and last.get("status") != "needs_retry":
            return "done"

    nodes = (state.get("task_dag") or {}).get("nodes") or []
    idx = int(state.get("current_step_idx", 0))
    return "next" if idx < len(nodes) else "done"


def build_graph(checkpointer: Any | None = None) -> Any:
    """构建并编译主图。checkpointer 可选（get_graph 默认注入 AsyncSqliteSaver）。"""
    graph = StateGraph(HwState)

    # 节点
    graph.add_node("intake", run_intake)
    graph.add_node("planner", run_planner)
    graph.add_node("coder_step", run_coder_step)
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
    graph.add_edge("planner", "coder_step")

    # ★ Plan-and-Execute Lite：coder_step 自循环
    # 每轮 coder_step（idx+1）后由 step_router 决定：
    # - "next" → 回 coder_step 跑下一个 step
    # - "done" → 所有 step 完成，进 verifier 验证
    graph.add_conditional_edges(
        "coder_step",
        step_router,
        {"next": "coder_step", "done": "verifier"},
    )

    # 条件边：Verifier 后检查 verdict 决定 Replan 还是 Compile
    graph.add_conditional_edges(
        "verifier",
        replan_router,
        {
            "planner": "planner",  # fail 且未达 MAX → 重拆 DAG（planner 节点会 reset current_step_idx=0）
            "compile": "compile",  # pass / fail 超限 → 收尾
        },
    )

    graph.add_edge("compile", "summarizer")
    graph.add_edge("summarizer", END)

    return graph.compile(checkpointer=checkpointer)


# 全局单例（首次访问时编译）
_compiled_graph: Any = None
_checkpointer: Any = None


def _get_checkpointer() -> Any:
    """AsyncSqliteSaver 单例。

    AsyncSqliteSaver.__init__ 绑定当前 running loop，所以 get_graph() 首次调用
    **必须发生在目标事件循环内**（CLI 在常驻 REPL loop 的协程里调、
    Web uvicorn loop、benchmark asyncio.run 均满足）；aiosqlite 连接由
    saver.setup() 首次使用时懒建立。主图是异步流（run_coder_step），
    必须用 Async 版 saver。
    """
    global _checkpointer
    if _checkpointer is None:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from config.runtime import get_settings

        db_path = get_settings().checkpoint_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = aiosqlite.connect(str(db_path))
        # aiosqlite worker 线程设为 daemon，否则永不显式关闭的连接会挂住进程退出。
        # checkpoint put 在图执行内同步完成，daemon 化只影响退出时刻，
        # 无丢数风险。_thread 是 aiosqlite 私有属性，版本重构可能移除——
        # 判 None 优雅降级，不让主图编译受连累。
        worker = getattr(conn, "_thread", None)
        if worker is not None:
            worker.daemon = True
        _checkpointer = AsyncSqliteSaver(conn)
    return _checkpointer


def get_graph() -> Any:
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph(checkpointer=_get_checkpointer())
    return _compiled_graph
