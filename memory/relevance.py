"""向量候选的相关性筛选。只作裁定，不读写文件、不管理索引。"""

import json

from config.prompts import MEMORY_SELECT_SYSTEM
from config.runtime import RuntimeSettings
from memory.calls import retry_transient


async def select_relevant(query: str, cards: list[dict], llm,
                          settings: RuntimeSettings) -> list[int]:
    # 复用 runtime 的 function-calling 边界；延迟导入避免 runtime 入口加载时形成环。
    from runtime.loop.parse import oneshot_schema
    from runtime.loop.schema import MEMORY_SELECT_SCHEMA, SUBMIT_MEMORY_SELECT

    payload = await retry_transient(lambda: oneshot_schema(
        llm, model=settings.flash_model, role="flash", system=MEMORY_SELECT_SYSTEM,
        user=json.dumps({"request": query, "candidates": [
            {"index": i, "task_title": c.get("task_title", ""),
             "task_type": c.get("task_type", ""), "content": c["content"]}
            for i, c in enumerate(cards)
        ]}, ensure_ascii=False),
        name=SUBMIT_MEMORY_SELECT, schema=MEMORY_SELECT_SCHEMA,
        description="Judge each candidate's relevance to the actual task",
    ), settings)
    verdicts = payload["verdicts"]
    indices = [v["index"] for v in verdicts]
    if sorted(indices) != list(range(len(cards))):
        raise ValueError("相关性裁定必须完整覆盖候选 index，不能遗漏、重复或越界")
    return sorted(v["index"] for v in verdicts if v["relevant"])
