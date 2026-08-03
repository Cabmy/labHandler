"""search_tool - DDG 搜索封装（改编自 deep_search/search.py）。

设计要点：
1. 用 ddgs 库（P0 已验证）；PROXY 来自 .env，可为空。
2. 最小重试（2 次，间隔 3s）；异步实现：阻塞的 DDGS 调用
   卸到线程池，重试间隔用 asyncio.sleep 避免阻塞
   事件循环（Web 并发下不卡 SSE 流）。
3. @tool 装饰器输出 OpenAI schema 供 bind_tools。
"""

from __future__ import annotations

import asyncio
import warnings

from langchain_core.tools import tool

from config.runtime import get_settings

warnings.filterwarnings("ignore", category=DeprecationWarning, module="duckduckgo_search")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="ddgs")


def _search_once(query: str, max_results: int) -> list[dict]:
    """单次同步搜索（跑在线程池内）。"""
    from ddgs import DDGS  # 延迟导入避免模块加载开销

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


@tool
async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo web search. Returns a [{title, url, snippet}] list (up to max_results items).

    Usage: the Researcher node looks up API docs / papers; the Coder node
    occasionally looks up error messages.
    """
    settings = get_settings()
    last_err: Exception | None = None
    for attempt in range(settings.search_max_retries + 1):
        try:
            results = await asyncio.to_thread(_search_once, query, max_results)
            if results:
                return results
        except Exception as e:
            last_err = e
            if attempt < settings.search_max_retries:
                await asyncio.sleep(settings.search_retry_delay)
    if last_err:
        return [{"title": "", "url": "", "snippet": f"[search failed: {type(last_err).__name__}]"}]
    return []
