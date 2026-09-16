"""退避执行器。只负责重试瞬时失败，不持策略计数。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from runtime.errors import ErrorClass, classify

T = TypeVar("T")


def _is_transient(exc: BaseException) -> bool:
    return classify(exc) == ErrorClass.TRANSIENT


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    min_wait: float = 0.5,
    max_wait: float = 8.0,
) -> T:
    """指数退避执行协程。attempts 含首次。"""
    if attempts <= 1:
        return await fn()
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=min_wait, min=min_wait, max=max_wait),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    ):
        with attempt:
            return await fn()
    raise RuntimeError("with_backoff: unreachable")


def delay_for(attempt: int, *, min_wait: float = 0.5, max_wait: float = 8.0) -> float:
    return min(max_wait, min_wait * (2 ** max(0, attempt - 1)))


async def sleep_delay(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)
