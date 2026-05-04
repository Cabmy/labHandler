"""hwHandler orchestrator 模块入口

注意：graph 相关延迟 import 避免与 agents 形成环引用
（agents/coder.py 用 from orchestrator.state import HwState 会触发本 __init__.py 加载，
 若顶层 import .graph 则 .graph 会反过来 import agents.coder → 环依赖死锁）
"""

from typing import Any

from .compile_node import run_compile
from .replan import MAX_REPLAN_ITER, is_partial, replan_router
from .state import HwState, state_from_jsonable, state_to_jsonable


def build_graph() -> Any:
    """延迟 import 避免环引用"""
    from .graph import build_graph as _impl
    return _impl()


def get_graph() -> Any:
    """延迟 import 避免环引用"""
    from .graph import get_graph as _impl
    return _impl()


__all__ = [
    "HwState",
    "state_to_jsonable",
    "state_from_jsonable",
    "build_graph",
    "get_graph",
    "replan_router",
    "is_partial",
    "MAX_REPLAN_ITER",
    "run_compile",
]
