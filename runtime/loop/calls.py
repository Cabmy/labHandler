"""一轮内的 tool_calls：连续只读并行，写与结构化出口串行。不重排。

history 里 tool 消息仍按模型给出的顺序回填；并行只发生在相邻只读调用上。
写、submit_*、表外名字一律单独一波，避免读到未完成的写入。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass
from runtime.loop.parse import VALIDATION_TOOL_RESULT, parse_args, validate_payload
from runtime.loop.registry import ToolContext, ToolRegistry
from runtime.loop.schema import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_HALT,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
)
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer

# loop 阶段的结构化出口：走 validate_payload，且校验耗尽时按角色升级（cycle.py 分流）。
# 与 schema.SCHEMA_TOOLS 不同——后者还含有各自独立流程的 dream / skill_edit。
SUBMIT_TOOLS = {
    SUBMIT_SPEC,
    SUBMIT_DISPATCH,
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SUMMARY,
    SUBMIT_HALT,
}

T = TypeVar("T")
R = TypeVar("R")
Emit = Callable[..., Awaitable[None]]


@dataclass
class CallResult:
    name: str
    call_id: str
    args: dict[str, Any] | None
    text: str
    error: ErrorClass
    submit_name: str | None = None
    submit_payload: dict[str, Any] | None = None
    ran: bool = False
    exhausted: bool = False


def waves(parallel: Sequence[bool]) -> list[tuple[int, int]]:
    """切成 [start, end) 波。连续 True 合成一波；每个 False 单独一波。"""
    out: list[tuple[int, int]] = []
    i = 0
    n = len(parallel)
    while i < n:
        if not parallel[i]:
            out.append((i, i + 1))
            i += 1
            continue
        j = i + 1
        while j < n and parallel[j]:
            j += 1
        out.append((i, j))
        i = j
    return out


async def run_waves(
    items: Sequence[T],
    parallel: Sequence[bool],
    invoke: Callable[[T], Awaitable[R]],
) -> list[R]:
    """按 waves 调度。并行波用 create_task，让 tracer 的 contextvars 互不覆盖。"""
    if len(items) != len(parallel):
        raise ValueError("items / parallel 长度不一致")
    results: list[Any] = [None] * len(items)
    for start, end in waves(parallel):
        if end - start == 1:
            results[start] = await invoke(items[start])
            continue
        tasks = [asyncio.create_task(invoke(items[i]))
                 for i in range(start, end)]
        try:
            chunk = await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results[start:end] = chunk
    return results


async def run_calls(
    calls: list[dict[str, Any]],
    *,
    submit_tool: str,
    node: str,
    registry: ToolRegistry,
    ctx: ToolContext,
    settings: RuntimeSettings,
    tracer: Tracer | None,
    validation_streak: dict[str, int],
    emit: Emit,
) -> list[CallResult]:
    flags = [registry.parallelizable(c["name"]) for c in calls]

    async def invoke(tc: dict[str, Any]) -> CallResult:
        return await _one(
            tc,
            submit_tool=submit_tool,
            node=node,
            registry=registry,
            ctx=ctx,
            settings=settings,
            tracer=tracer,
            validation_streak=validation_streak,
            emit=emit,
        )

    out: list[CallResult] = []
    for start, end in waves(flags):
        batch = calls[start:end]
        if tracer is not None and end - start > 1:
            with tracer.span(
                S.TOOL_WAVE,
                kind=S.KIND_CHAIN,
                **{
                    S.ATTR_PARALLEL: True,
                    S.ATTR_BATCH_SIZE: end - start,
                    S.ATTR_TOOLS: [c["name"] for c in batch],
                    S.ATTR_AGENT: node,
                },
            ) as wave:
                chunk = await run_waves(batch, flags[start:end], invoke)
                wave.set(**{S.ATTR_OUTCOME: "ok"})
        else:
            chunk = await run_waves(batch, flags[start:end], invoke)
        out.extend(chunk)
    return out


def _track_validation(
    result: CallResult,
    validation_streak: dict[str, int],
    settings: RuntimeSettings,
) -> CallResult:
    """统一的连续校验失败熔断：VALIDATION 累加、其余清零，任何工具连续达上限即打 exhausted。

    出口 submit 与普通工具共用这一条规则；后续分流在 cycle.py——submit 升级（Flash 接管 /
    Pro halt），普通工具只 nudge 换写法。exhausted 只表示「连续失败已到上限」，不含角色语义。
    """
    name = result.name
    if result.error is not ErrorClass.VALIDATION:
        validation_streak.pop(name, None)
        return result
    validation_streak[name] = validation_streak.get(name, 0) + 1
    if validation_streak[name] < settings.validation_retry_max:
        return result
    return replace(result, exhausted=True)


async def _one(
    tc: dict[str, Any],
    *,
    submit_tool: str,
    node: str,
    registry: ToolRegistry,
    ctx: ToolContext,
    settings: RuntimeSettings,
    tracer: Tracer | None,
    validation_streak: dict[str, int],
    emit: Emit,
) -> CallResult:
    name = tc["name"]
    call_id = tc["id"]

    parsed, perr = parse_args(tc["arguments"])
    if parsed is None:
        result = CallResult(
            name, call_id, None, VALIDATION_TOOL_RESULT.format(
                err=perr), ErrorClass.VALIDATION
        )
    elif name in SUBMIT_TOOLS:
        allowed = name == submit_tool or (
            name == SUBMIT_HALT and SUBMIT_HALT in registry.names()
        )
        if not allowed:
            result = CallResult(
                name,
                call_id,
                parsed,
                VALIDATION_TOOL_RESULT.format(
                    err=(
                        f"{name} is unavailable during {node}. "
                        f"The active exit tool is {submit_tool}"
                    )
                ),
                ErrorClass.VALIDATION,
            )
        else:
            err = validate_payload(name, parsed)
            if err:
                result = CallResult(
                    name, call_id, parsed, VALIDATION_TOOL_RESULT.format(
                        err=err), ErrorClass.VALIDATION
                )
            else:
                result = CallResult(
                    name, call_id, parsed, "ok", ErrorClass.OK, submit_name=name, submit_payload=parsed
                )
    else:
        local = replace(ctx, extras={**ctx.extras, "tool_call_id": call_id})
        outcome = await registry.execute(
            name, parsed, local, timeout=settings.tool_timeout_s, tracer=tracer
        )
        result = CallResult(name, call_id, parsed,
                            outcome.text, outcome.error_class, ran=True)

    result = _track_validation(result, validation_streak, settings)

    shown = json.dumps(parsed, ensure_ascii=False)[
        :200] if parsed is not None else tc["arguments"][:200]
    await emit(
        "tool",
        node=node,
        name=name,
        args=shown,
        result=result.text[:200],
        error_class=result.error.value,
    )
    return result
