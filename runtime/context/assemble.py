"""上下文槽位装配。纯函数：同样的输入永远得到同样的 messages。

槽位顺序按「越稳定越靠前」排，利于上游 prompt cache：
system → memory → user → spec → history → retrieved → events

user 槽只承载「本轮之前没有任何对话」的那一段开场指令；为空时整条消息不出现。
跨阶段连续对话的指令由调用方直接写进 history，这样它才排在既有往来之后，
而不是被 user 槽顶到全部历史之前。

history 是工具消息的唯一载体，其中已经包含成对的 assistant(tool_calls) +
tool 响应。装配层没有第二个 tool 槽位：任何旁路注入都会让同一批结果出现
两次，其中一份还会落在 user 消息之后变成协议非法的孤儿消息。

input_tokens = 消息估计 + 本轮 tool schema 估计，与 TokenBudget 用同一套计数。
发出的消息不含 harness 私有字段（如 pinned）。
"""

from dataclasses import dataclass
from typing import Any

from runtime.context.budget import count_messages, count_tool_schemas

# harness 内部标记，不属于 OpenAI 消息结构；发出去会被严格网关拒绝
_PRIVATE_KEYS = frozenset({"pinned"})


@dataclass(frozen=True)
class AgentContext:
    messages: list[dict[str, Any]]
    input_tokens: int


def strip_private(message: dict[str, Any]) -> dict[str, Any]:
    """剥掉 harness 私有字段。无此类字段时返回原对象；否则返回不含这些键的新 dict。"""
    if _PRIVATE_KEYS.isdisjoint(message):
        return message
    return {k: v for k, v in message.items() if k not in _PRIVATE_KEYS}


def assemble(
    *,
    system: str,
    user_input: str,
    project_spec: str,
    history: list[dict[str, Any]],
    retrieved: str,
    memory: str,
    events: list[dict[str, str]],
    working: str,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> AgentContext:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
    ]
    if memory:
        messages.append({"role": "user", "content": memory})
    if user_input:
        messages.append({"role": "user", "content": user_input})
    if project_spec:
        messages.append({"role": "user", "content": f"## SPEC.md\n{project_spec}"})

    messages.extend(strip_private(m) for m in history)

    if retrieved:
        messages.append({"role": "user", "content": f"## Retrieved knowledge\n{retrieved}"})

    event_lines = [e.get("text", "") for e in events if e.get("text")]
    if working:
        event_lines.append(working)
    if event_lines:
        messages.append(
            {"role": "user", "content": "## Execution events\n" + "\n".join(event_lines)}
        )

    tokens = count_messages(messages) + count_tool_schemas(tool_schemas)
    return AgentContext(messages=messages, input_tokens=tokens)


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
