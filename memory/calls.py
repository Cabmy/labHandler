"""记忆服务的 I/O 重试边界；格式错误和永久失败不重试。"""

import asyncio

from config.runtime import RuntimeSettings


async def retry_transient(operation, settings: RuntimeSettings):
    from runtime.errors import ErrorClass, LLMCallError, classify

    attempts = settings.transient_retry_max
    for attempt in range(attempts):
        try:
            async with asyncio.timeout(settings.memory_timeout_s / attempts):
                return await operation()
        except Exception as e:
            error_class = e.error_class if isinstance(e, LLMCallError) else classify(e)
            if error_class is not ErrorClass.TRANSIENT or attempt + 1 == attempts:
                raise
            await asyncio.sleep(0.2 * 2 ** attempt)
