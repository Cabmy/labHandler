"""按 Task.permission 并行或串行派发 Flash worker。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from config.runtime import RuntimeSettings
from runtime.schema_call import synthetic_brief
from runtime.task import Permission, RuntimeTask, TaskStatus


async def run_wave(
    workers: list[RuntimeTask],
    runner: Callable[[RuntimeTask], Awaitable[dict[str, Any]]],
    settings: RuntimeSettings,
) -> list[tuple[RuntimeTask, dict[str, Any]]]:
    """一波 worker。1 个 = write；多个 = 只读并行。plan_invalid 取消兄弟。"""
    if not workers:
        return []
    if len(workers) == 1:
        w = workers[0]
        brief = await runner(w)
        return [(w, brief)]

    sem = asyncio.Semaphore(max(1, settings.max_parallel_readonly_workers))
    results: dict[str, dict[str, Any]] = {}
    tasks: dict[str, asyncio.Task] = {}

    async def _one(w: RuntimeTask) -> None:
        async with sem:
            try:
                results[w.task_id] = await runner(w)
            except Exception as e:
                results[w.task_id] = synthetic_brief(
                    outcome="failed", brief=f"{type(e).__name__}: {e}"
                )

    try:
        async with asyncio.TaskGroup() as tg:
            for w in workers:
                tasks[w.task_id] = tg.create_task(_one(w))
            # 轮询：任一 plan_invalid 则取消其余
            pending = set(tasks.values())
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    if d.cancelled() or d.exception():
                        continue
                if any(
                    (results.get(w.task_id) or {}).get("outcome") == "plan_invalid"
                    for w in workers
                ):
                    for t in pending:
                        t.cancel()
                    break
    except ExceptionGroup:
        pass
    except asyncio.CancelledError:
        pass

    out: list[tuple[RuntimeTask, dict[str, Any]]] = []
    for w in workers:
        brief = results.get(w.task_id)
        if brief is None:
            brief = synthetic_brief(
                outcome="failed",
                brief="Sibling workers were cancelled after plan_invalid.",
            )
            if w.status is TaskStatus.RUNNING:
                try:
                    w.transit(TaskStatus.CANCELLED)
                except ValueError:
                    pass
        out.append((w, brief))
    return out


def permission_for_wave(n: int) -> Permission:
    return Permission.WRITE if n == 1 else Permission.READONLY
