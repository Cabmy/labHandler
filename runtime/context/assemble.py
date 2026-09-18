"""上下文槽位装配。纯函数：同样的输入永远得到同样的 messages。

槽位顺序按「一拍之内还会不会变」排，不变的靠前，利于上游 prompt cache：
system → user → history → state → cards → retrieved → events

system：只读主线跨阶段固定（PRO_SYSTEM）。独立 agent 用自己的完整 prompt。
user：独立 agent 的开场任务；共享线程为空——初始任务已经是 history 的第一条。
history：append-only。共享线程里含初始任务、phase_control、state_update、工具轨迹。
state：本拍最新 NOTES.md / SPEC.md 快照。放在 history 之后，变动只作废后缀缓存。
cards：SPEC 预取的跨 lab 卡片正文，仅 Pro。memory_forget 一拍之内就会变，必须
       排在 history 之后。本次作业要求先入场，旧 lab 经验后到。
retrieved：本 loop 里 memory_search/grep/read、notes_read、load_skill 的追加结果，不进 history。

history 是工具消息的唯一载体，其中已经包含成对的 assistant(tool_calls) +
tool 响应。装配层没有第二个 tool 槽位。

input_tokens = 消息估计 + 本轮 tool schema 估计，与 TokenBudget 用同一套计数。
发出的消息不含 harness 私有字段（如 pinned）。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from runtime.context.budget import count_messages, count_tool_schemas

_PRIVATE_KEYS = frozenset({"pinned"})


@dataclass(frozen=True)
class AgentContext:
    messages: list[dict[str, Any]]
    input_tokens: int
    system_hash: str = ""
    tools_hash: str = ""


def strip_private(message: dict[str, Any]) -> dict[str, Any]:
    """剥掉 harness 私有字段。无此类字段时返回原对象；否则返回不含这些键的新 dict。"""
    if _PRIVATE_KEYS.isdisjoint(message):
        return message
    return {k: v for k, v in message.items() if k not in _PRIVATE_KEYS}


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tools_hash(tool_schemas: list[dict[str, Any]] | None) -> str:
    if not tool_schemas:
        return prompt_hash("")
    return prompt_hash(json.dumps(tool_schemas, ensure_ascii=False, sort_keys=True))


def live_state_block(*, notes: str = "", spec: str = "") -> str:
    """history 之后的动态快照：当前 NOTES.md 与 SPEC.md。空则不装配。"""
    parts: list[str] = []
    if notes:
        parts.append(notes)
    if spec:
        parts.append(f"## SPEC.md\n{spec}")
    return "\n\n".join(parts)


def assemble(
    *,
    system: str,
    user_input: str,
    history: list[dict[str, Any]],
    retrieved: str,
    cards: str = "",
    state: str = "",
    events: list[dict[str, str]],
    tool_schemas: list[dict[str, Any]] | None = None,
) -> AgentContext:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
    ]
    if user_input:
        messages.append({"role": "user", "content": user_input})

    messages.extend(strip_private(m) for m in history)

    if state:
        messages.append({"role": "user", "content": state})
    if cards:
        messages.append({"role": "user", "content": cards})
    if retrieved:
        messages.append({"role": "user", "content": f"## Retrieved knowledge\n{retrieved}"})

    event_lines = [e.get("text", "") for e in events if e.get("text")]
    if event_lines:
        messages.append(
            {"role": "user", "content": "## Execution events\n" + "\n".join(event_lines)}
        )

    tokens = count_messages(messages) + count_tool_schemas(tool_schemas)
    return AgentContext(
        messages=messages,
        input_tokens=tokens,
        system_hash=prompt_hash(system),
        tools_hash=_tools_hash(tool_schemas),
    )


def validate_message_sequence(messages: list[dict[str, Any]]) -> list[str]:
    """检查 OpenAI 兼容协议的消息顺序约束。返回问题列表，空列表表示合法。

    规则：
    1. role=tool 必须处在「带 tool_calls 的 assistant」之后的连续 tool 块里，否则是孤儿。
    2. 带 tool_calls 的 assistant 之后，每个 tool_call id 都要有对应的响应。
    3. tool_call_id 在整份 messages 里不得重复。
    """
    problems: list[str] = []
    pending: dict[str, int] = {}
    owner_index: int | None = None
    seen_ids: set[str] = set()

    def close_block() -> None:
        nonlocal owner_index, pending
        if pending:
            missing = ", ".join(sorted(pending))
            problems.append(
                f"消息 {owner_index} 的 tool_call 没有响应：{missing}"
            )
        pending = {}
        owner_index = None

    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if owner_index is None:
                problems.append(
                    f"消息 {i} 是孤儿 tool：前面没有带 tool_calls 的 assistant"
                )
                continue
            if call_id in seen_ids:
                problems.append(f"消息 {i} 的 tool_call_id 重复：{call_id}")
            seen_ids.add(call_id)
            if call_id not in pending:
                problems.append(
                    f"消息 {i} 的 tool_call_id 未在上游 assistant 中声明：{call_id}"
                )
            else:
                pending.pop(call_id, None)
            continue

        close_block()

        if role == "assistant" and msg.get("tool_calls"):
            owner_index = i
            for tc in msg["tool_calls"]:
                tc_id = str(tc.get("id") or "")
                pending[tc_id] = i

    close_block()
    return problems
