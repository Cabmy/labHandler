"""DuckDuckGo 网页搜索。

web_search 在线程里调 DDGS；max_results 夹到 [1, 8]。空结果会按
search_max_retries 重试；耗尽后若有异常返回一条 [search failed: ...] snippet，
无异常则空列表。代理取 settings.proxy。
"""

import asyncio
import warnings

from config.runtime import get_settings

warnings.filterwarnings("ignore", category=DeprecationWarning, module="duckduckgo_search")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="ddgs")


def _search_once(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    settings = get_settings()
    results = []
    with DDGS(proxy=settings.proxy) as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
            )
    return results


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    settings = get_settings()
    last_err: Exception | None = None
    cap = min(max(1, max_results), 8)
    for attempt in range(settings.search_max_retries + 1):
        try:
            results = await asyncio.to_thread(_search_once, query, cap)
            if results:
                return results
        except Exception as e:
            last_err = e
            if attempt < settings.search_max_retries:
                await asyncio.sleep(settings.search_retry_delay)
    if last_err:
        return [{"title": "", "url": "", "snippet": f"[search failed: {type(last_err).__name__}]"}]
    return []
