"""Replan 路由 - Verifier 之后的条件分支

设计要点（PLAN §15 / STEPS P5.2）：
- Verifier 出 verdict 后调用 replan_router 判分支：
  - "pass"     → "compile"（正常通过，进 Compile→Summarizer）
  - "fail" 且 iteration < MAX_REPLAN_ITER → "planner"（重拆 DAG）
  - "fail" 且 iteration ≥ MAX_REPLAN_ITER → "compile"（标"部分完成"，进 Summarizer）

iteration 含义：当前已运行过的 Planner 次数（首次进 Planner 后变 1）。
"""

from __future__ import annotations

import os
from typing import Literal

from orchestrator.state import HwState

MAX_REPLAN_ITER = int(os.getenv("MAX_REPLAN_ITER", "2"))


def replan_router(state: HwState) -> Literal["planner", "compile"]:
    """LangGraph conditional_edges 用的路由函数。"""
    runs = state.get("verifier_runs") or []
    if not runs:
        # 罕见：没跑过 Verifier 就到这——保守走 compile 收尾
        return "compile"

    verdict = runs[-1].get("verdict", "fail")
    iteration = int(state.get("iteration", 0))

    if verdict == "fail" and iteration < MAX_REPLAN_ITER:
        return "planner"
    return "compile"


def is_partial(state: HwState) -> bool:
    """判定当前是否为"部分完成"状态（Compile / Summarizer 用）"""
    runs = state.get("verifier_runs") or []
    if not runs:
        return False
    last_verdict = runs[-1].get("verdict", "fail")
    iteration = int(state.get("iteration", 0))
    return last_verdict == "fail" and iteration >= MAX_REPLAN_ITER
