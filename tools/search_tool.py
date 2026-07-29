"""search_tool - DDG 检索包装（参考 deep_search/search.py 拷造）

设计要点：
1. 用 ddgs 库（P0 验过）；PROXY 走 .env，可空
2. 带最简重试（2 次，3s 间隔）；async 实现：阻塞的 DDGS 调用丢线程池、
   重试间隔用 asyncio.sleep，不占事件循环（Web 端 SSE 并发下不卡流）
3. @tool 装饰输出 OpenAI schema 给 bind_tools
"""

from __future__ import annotations

import asyncio
import os
import warnings

from langchain_core.tools import tool

warnings.filterwarnings("ignore")

PROXY = os.getenv("PROXY") or None
MAX_RETRIES = int(os.getenv("SEARCH_MAX_RETRIES", "2"))
RETRY_DELAY = int(os.getenv("SEARCH_RETRY_DELAY", "3"))


def _search_once(query: str, max_results: int) -> list[dict]:
    """单次同步检索（在线程池里跑）。"""
    from ddgs import DDGS  # 懒 import 避免 import 阶段拖累

    results = []
    with DDGS(proxy=PROXY) as ddgs:
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
    """DuckDuckGo 网络检索。返回 [{title, url, snippet}] 列表（最多 max_results 条）。

    用例：Researcher 节点查 API 文档 / 论文。Coder 节点偶尔查报错信息。
    """
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            results = await asyncio.to_thread(_search_once, query, max_results)
            if results:
                return results
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    if last_err:
        return [{"title": "", "url": "", "snippet": f"[搜索失败: {type(last_err).__name__}]"}]
    return []
