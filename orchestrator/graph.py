"""LangGraph 主图 - labHandler 编排层（Plan-and-Execute Lite）

节点结构（Coder 单步执行 + step_router 自循环 + Replan loop）：

  START → Intake → Planner → coder_step ──→ step_router
                    ↑                            │
                    │  fail (iter<MAX,            ├ next ─→ coder_step（跑下一个 step）
                    │  reset idx=0)               └ done ─→ Verifier
                    │                                          │
                    └──────────────────────────────────────────┤
                                pass / fail (iter≥MAX)         │
                                       → Compile → Summarizer → END

关键约束：
- coder_step **单步执行**：每次只跑 task_dag.nodes[current_step_idx] 那一个 step
- step_router 控制 step 循环：current_step_idx < len(nodes) → 回 coder_step；否则 → verifier
- **有界原地重试**：Coder Final Answer 报 needs_retry 时不推进 idx（上限 MAX_STEP_RETRY 次），
  step_router 的 "next" 即重跑同一 step；耗尽标 failed 推进并触发 error 短路
- iteration 由 Planner 节点 +1；planner 节点同时 reset current_step_idx=0（含 Replan）
- HwState 多个字段用 Annotated[list, add] 让 LangGraph 自动累加：
  progress_log / verifier_runs / artifacts / user_constraints / step_outputs

路由规则（详见 orchestrator/replan.py）：
  pass                           → "compile"
  fail 且 iteration < MAX_REPLAN → "planner"（reset idx=0 重拆 DAG）
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
    """REPL 入口路由：首轮没 intake_result → intake；后续 REPL 输入 → 直接 planner，
    让累积的 user_constraints + prior verifier_runs + intake_result 作为修订上下文喂给 planner。"""
    return "planner" if state.get("intake_result") else "intake"


def step_router(state: HwState) -> Literal["next", "done"]:
    """coder_step 完成后的条件路由（Plan-and-Execute Lite + 有界原地重试）：

    - "done" → 上一步执行有 error（依赖检查失败/执行异常/重试耗尽标 failed 等），
               跳过剩余 step 直接进 verifier；或全部 step 已跑完
    - "next" → 还有未做的 step 且上一步无 error，回到 coder_step；
               上一步报 needs_retry 时 coder 未推进 idx，"next" 即原地重跑同一 step
    """
    # 上一步有 error 则短路进 verifier，不让下游 step 在空中楼阁上浪费 LLM 调用；
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
    """构建并 compile 主图。checkpointer 可选（get_graph 默认注入 AsyncSqliteSaver）。"""
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
    # 每跑完一轮 coder_step（idx+1）后，step_router 决定：
    # - "next" → 回到 coder_step 跑下一个 step
    # - "done" → 全部 step 完成，进 verifier 校验
    graph.add_conditional_edges(
        "coder_step",
        step_router,
        {"next": "coder_step", "done": "verifier"},
    )

    # 条件边：Verifier 后看 verdict 决定 Replan 或 Compile
    graph.add_conditional_edges(
        "verifier",
        replan_router,
        {
            "planner": "planner",  # fail 且未到 MAX → 重拆 DAG（planner 节点会 reset current_step_idx=0）
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

    AsyncSqliteSaver.__init__ 绑定当前 running loop，因此 get_graph() 首次调用
    **必须发生在目标事件循环内**（CLI 在 REPL 持久 loop 的协程里调、Web uvicorn loop、
    benchmark asyncio.run 均满足）；aiosqlite 连接由 saver.setup() 首次使用时惰性建立。
    主图为 async 流（run_coder_step），必须用 Async 版本 saver。
    """
    global _checkpointer
    if _checkpointer is None:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from config.runtime import get_settings

        db_path = get_settings().checkpoint_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = aiosqlite.connect(str(db_path))
        # aiosqlite 的 worker 线程 daemon 化，否则连接从不显式 close 时会挂住进程退出。
        # checkpoint 每次 put 在图执行内同步完成，daemon 化只影响退出瞬间，无丢数据风险。
        # _thread 是 aiosqlite 私有属性，版本重构可能消失——判空降级，别让主图编译陪葬。
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
