"""一轮 Agent：assemble → llm → 按序执行 tool_calls → record → decide。"""

__all__ = ["AgentSpec", "LoopResult", "drop_dangling_tool_calls", "run_loop"]


def __getattr__(name: str):
    if name in __all__:
        from runtime.loop.cycle import (
            AgentSpec,
            LoopResult,
            drop_dangling_tool_calls,
            run_loop,
        )

        return {
            "AgentSpec": AgentSpec,
            "LoopResult": LoopResult,
            "drop_dangling_tool_calls": drop_dangling_tool_calls,
            "run_loop": run_loop,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
