"""search_tool - DDG 检索包装（参考 deep_search/search.py 拷造）

设计要点：
1. 用 ddgs 库（P0 验过）；PROXY 走 .env，可空
2. 带最简重试（2 次，3s 间隔）
3. @tool 装饰输出 OpenAI schema 给 bind_tools
"""

from __future__ import annotations

import os
import time
import warnings

from langchain_core.tools import tool

warnings.filterwarnings("ignore")

PROXY = os.getenv("PROXY") or None
MAX_RETRIES = int(os.getenv("SEARCH_MAX_RETRIES", "2"))
RETRY_DELAY = int(os.getenv("SEARCH_RETRY_DELAY", "3"))


@tool
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo 网络检索。返回 [{title, url, snippet}] 列表（最多 max_results 条）。

    用例：Researcher 节点查 API 文档 / 论文。Coder 节点偶尔查报错信息。
    """
    from ddgs import DDGS  # 懒 import 避免 import 阶段拖累

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
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
            if results:
                return results
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    if last_err:
        return [{"title": "", "url": "", "snippet": f"[搜索失败: {type(last_err).__name__}]"}]
    return []
