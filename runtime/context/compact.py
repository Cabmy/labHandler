"""上下文压缩：超阈值时卸载旧 tool 正文 + Pro 摘要旧对话。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from runtime.context.assemble import estimate_tokens
from runtime.llm import LLMGateway


async def compact_if_needed(
    *,
    history: list[dict[str, Any]],
    tool_dumps: list[tuple[int, str, str]],
    session_dir: Path,
    settings: RuntimeSettings,
    llm: LLMGateway,
    estimated_tokens: int,
) -> tuple[list[dict[str, Any]], bool]:
    """tool_dumps: (turn, tool_name, full_text) still inlined in history.

    返回 (new_history, did_compact)。
    """
    budget = settings.context_budget_tokens
    if estimated_tokens < budget * settings.compact_trigger_ratio:
        return history, False

    dump_dir = session_dir / "tool_results"
    dump_dir.mkdir(parents=True, exist_ok=True)
    for turn, tool_name, text in tool_dumps:
        path = dump_dir / f"{turn}-{tool_name}.txt"
        path.write_text(text, encoding="utf-8")

    pinned = [m for m in history if m.get("pinned")]
    compactable = [m for m in history if not m.get("pinned")]
    keep_tail = compactable[-6:] if len(compactable) > 6 else compactable
    old = compactable[:-6] if len(compactable) > 6 else []
    summary = ""
    if old:
        blob = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in old)[:12000]
        try:
            result = await llm.chat(
                model=settings.pro_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize the following agent conversation for later turns. "
                            "Keep constraints, file names, test outcomes, and open questions. "
                            "Do not invent facts."
                        ),
                    },
                    {"role": "user", "content": blob},
                ],
                max_tokens=800,
            )
            summary = (result.content or "").strip()
        except Exception:
            summary = ""
        if not summary:
            summary = "(compaction LLM failed; older turns dropped; tool bodies are on disk under tool_results/)"

    new_history: list[dict[str, Any]] = list(pinned)
    if summary:
        new_history.append(
            {
                "role": "user",
                "content": f"## Compacted earlier conversation\n{summary}",
            }
        )
    new_history.extend(keep_tail)
    return new_history, True
