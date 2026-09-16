"""上下文槽位装配。稳定内容在前。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    messages: list[dict[str, Any]]
    estimated_tokens: int
    retrieved: str = ""


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def assemble(
    *,
    system: str,
    tools_note: str,
    user_input: str,
    plan: str,
    history: list[dict[str, Any]],
    retrieved: str,
    notes: str,
    events: list[dict[str, str]],
    working: str,
    tool_messages: list[dict[str, Any]],
) -> AgentContext:
    sys_parts = [system]
    if tools_note:
        sys_parts.append(tools_note)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n".join(sys_parts)},
        {"role": "user", "content": user_input or "(no additional user text)"},
    ]
    if plan:
        messages.append({"role": "user", "content": f"## Plan\n{plan}"})
    messages.extend(history)
    if retrieved:
        messages.append({"role": "user", "content": f"## Retrieved knowledge\n{retrieved}"})
    if notes:
        messages.append({"role": "user", "content": f"## Agent notes\n{notes}"})
    event_lines = [e.get("text", "") for e in events if e.get("text")]
    if working:
        event_lines.append(working)
    if event_lines:
        messages.append(
            {
                "role": "user",
                "content": "## Execution events\n" + "\n".join(event_lines),
            }
        )
    messages.extend(tool_messages)
    blob = "\n".join(str(m.get("content") or "") for m in messages)
    return AgentContext(messages=messages, estimated_tokens=estimate_tokens(blob), retrieved=retrieved)
