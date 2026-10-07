"""有界并发执行一波 assignment；协程拥有执行生命周期。

每个返回（包括合成的异常结果）立即提交。兄弟取消会形成 blocked brief；
整波被用户取消时只保留此前已提交的结果，其余 assignment 续跑时补做。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from config.runtime import RuntimeSettings
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer, as_tracer
from runtime.loop.parse import synthetic_brief
from runtime.task import Permission, RuntimeTask


def _cancels_wave(brief: dict[str, Any]) -> bool:
    return brief.get("outcome") == "spec_invalid" or bool(brief.get("sandbox_unreachable"))


def permission_for_wave(n: int) -> Permission:
    return Permission.WRITE if n == 1 else Permission.READONLY


async def run_wave(
    workers: list[RuntimeTask],
    runner: Callable[[RuntimeTask], Awaitable[dict[str, Any]]],
    settings: RuntimeSettings,
    *,
    tracer: Tracer | None = None,
    on_result: Callable[[RuntimeTask, dict[str, Any]], None],
) -> list[tuple[RuntimeTask, dict[str, Any]]]:
    tracer = as_tracer(tracer)
    sem = asyncio.Semaphore(settings.max_parallel_readonly_workers)
    results: dict[str, dict[str, Any]] = {}

    def commit(worker: RuntimeTask, brief: dict[str, Any]) -> None:
        on_result(worker, brief)
        results[worker.task_id] = brief

    async def one(worker: RuntimeTask) -> None:
        async with sem:
            try:
                brief = await runner(worker)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tracer.event(S.EV_STEP_ERROR, **{
                    S.ATTR_TASK_ID: worker.task_id,
                    S.ATTR_REASON: f"{type(exc).__name__}: {exc}",
                })
                brief = synthetic_brief(outcome="failed", brief=f"{type(exc).__name__}: {exc}")
            commit(worker, brief)

    tasks = [asyncio.create_task(one(worker)) for worker in workers]
    pending = set(tasks)
    try:
        while pending:
            completed, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                await task  # 持久化失败等控制面异常不得被吞掉。
            if any(_cancels_wave(brief) for brief in results.values()):
                break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for worker in workers:
        if worker.task_id not in results:
            commit(worker, synthetic_brief(
                outcome="blocked",
                brief="Cancelled because a sibling reported spec_invalid or sandbox_unreachable.",
            ))
    return [(worker, results[worker.task_id]) for worker in workers]
