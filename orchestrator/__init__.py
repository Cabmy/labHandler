"""labHandler 编排模块入口。

注意：graph 相关采用延迟导入，避免与 agents 循环引用
（agents/coder.py 使用 'from orchestrator.state import HwState' 会触发本 __init__.py 加载；
 若在顶层导入 .graph，.graph 又会导入 agents.coder → 循环依赖死锁）
"""

from typing import Any

from .compile_node import run_compile
from .replan import is_partial, replan_router
from .state import HwState


def build_graph() -> Any:
    """延迟导入以避免循环引用。"""
    from .graph import build_graph as _impl
    return _impl()


def get_graph() -> Any:
    """延迟导入以避免循环引用。"""
    from .graph import get_graph as _impl
    return _impl()


__all__ = [
    "HwState",
    "build_graph",
    "get_graph",
    "replan_router",
    "is_partial",
    "run_compile",
]
