"""执行一步里的 Flash worker：1 个串行可写，多个只读并行。

任一 worker 的 outcome=spec_invalid 时取消尚未完成的兄弟，并给它们合成
failed brief。并发上限为 settings.max_parallel_readonly_workers。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from config.runtime import RuntimeSettings
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.loop.parse import synthetic_brief
from runtime.task import Permission, RuntimeTask, TaskStatus


def permission_for_wave(n: int) -> Permission:
    """单个 worker 可写；多个并行一律只读，避免并发写冲突。"""
    return Permission.WRITE if n == 1 else Permission.READONLY


async def run_wave(
    workers: list[RuntimeTask],
    runner: Callable[[RuntimeTask], Awaitable[dict[str, Any]]],
    settings: RuntimeSettings,
    *,
    tracer: Tracer | None = None,
) -> list[tuple[RuntimeTask, dict[str, Any]]]:
    if not workers:
        return []
    if len(workers) == 1:
        w = workers[0]
        return [(w, await runner(w))]

    sem = asyncio.Semaphore(settings.max_parallel_readonly_workers)
    results: dict[str, dict[str, Any]] = {}

    async def one(w: RuntimeTask) -> None:
        async with sem:
            try:
                results[w.task_id] = await runner(w)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if tracer is not None:
                    tracer.event(
                        S.EV_STEP_ERROR,
                        **{S.ATTR_TASK_ID: w.task_id, S.ATTR_REASON: f"{type(e).__name__}: {e}"},
                    )
                results[w.task_id] = synthetic_brief(
                    outcome="failed", brief=f"{type(e).__name__}: {e}"
                )

    tasks = {w.task_id: asyncio.ensure_future(one(w)) for w in workers}
    pending = set(tasks.values())
    try:
        while pending:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(
                (results.get(w.task_id) or {}).get("outcome") == "spec_invalid" for w in workers
            ):
                for t in pending:
                    t.cancel()
                break
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()
        outcomes = await asyncio.gather(*tasks.values(), return_exceptions=True)
        # one() 已把普通异常写成 failed brief。此处剩余的是未捕获异常，记进 span。
        for (task_id, _), outcome in zip(tasks.items(), outcomes):
            if isinstance(outcome, Exception) and tracer is not None:
                tracer.event(
                    S.EV_STEP_ERROR,
                    **{
                        S.ATTR_TASK_ID: task_id,
                        S.ATTR_REASON: f"unhandled {type(outcome).__name__}: {outcome}",
                    },
                )

    out: list[tuple[RuntimeTask, dict[str, Any]]] = []
    for w in workers:
        brief = results.get(w.task_id)
        if brief is None:
            brief = synthetic_brief(
                outcome="failed",
                brief="Sibling workers were cancelled after spec_invalid.",
            )
            if w.status is TaskStatus.RUNNING:
                try:
                    w.transit(TaskStatus.CANCELLED)
                except ValueError:
                    pass
        out.append((w, brief))
    return out
