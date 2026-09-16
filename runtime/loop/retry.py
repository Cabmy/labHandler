"""退避延迟：delay_for(attempt) 的秒数，指数增长、封顶 MAX_WAIT。

重试决策只由 control.decide 产出（它能看见 Task 的步数、墙钟和瞬时失败计数）。
LLMGateway / SDK 的 max_retries=0，避免多层退避把 wall-clock 预算耗在等待上。
"""

import asyncio

MIN_WAIT = 0.5
MAX_WAIT = 8.0


def delay_for(attempt: int, *, min_wait: float = MIN_WAIT, max_wait: float = MAX_WAIT) -> float:
    """第 attempt 次瞬时失败后应等待的秒数（指数退避，封顶 max_wait）。"""
    return min(max_wait, min_wait * (2 ** max(0, attempt - 1)))


async def sleep_delay(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)
