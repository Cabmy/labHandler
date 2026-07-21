"""Verifier 之后的重规划路由逻辑。

分支规则：
- verdict == "pass"：进入 compile
- verdict == "fail" 且 iteration < MAX_REPLAN_ITER：回到 planner
- verdict == "fail" 且 iteration >= MAX_REPLAN_ITER：进入 compile（后续按部分完成处理）

iteration 表示已执行的 planner 轮次，首次进入 planner 后记为 1。
"""

from __future__ import annotations

import os
from typing import Literal

from orchestrator.state import HwState

MAX_REPLAN_ITER = int(os.getenv("MAX_REPLAN_ITER", "2"))


def replan_router(state: HwState) -> Literal["planner", "compile"]:
    """用于 LangGraph conditional_edges 的路由函数。"""
    runs = state.get("verifier_runs") or []
    if not runs:
        # 异常状态：缺少 verifier 结果时直接收敛到 compile。
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
