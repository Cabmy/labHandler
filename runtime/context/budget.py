"""Token 计量与压缩预算。

本地估计：CJK 1 字 1 token，ASCII 约 4 字符 1 token，其余约 2 字符 1 token。
中文必须按字计，否则窗口会被低估约 2 倍，压缩阈值偏小。一条消息的估计必须
覆盖 content、tool_calls.arguments，以及每轮随请求发出的 tool schema。

估计全程本地完成。发请求前只有这份数字；发后若 usage 带回 input_tokens，
TokenBudget 记下 actual/estimate 比例，下一轮投影到真实窗口再决定压不压。
窗口、输出预留、触发比、校准比例都只活在这一个对象里。
"""

import json
from dataclasses import dataclass
from typing import Any, Self

from config.runtime import RuntimeSettings

# 每条消息的角色/分隔符固定开销
_MESSAGE_OVERHEAD = 4
# 每个 tool_call 的包装开销（id、type、function 外壳）
_TOOL_CALL_OVERHEAD = 8


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角
        or 0x3040 <= code <= 0x30FF   # 假名
        or 0xAC00 <= code <= 0xD7AF   # 谚文
    )


def count_text(text: str) -> int:
    """单段文本的 token 估计。CJK 1 字 1 token，ASCII 4 字符 1 token，其余 2 字符 1 token。"""
    if not text:
        return 0
    cjk = 0
    ascii_like = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ch.isascii():
            ascii_like += 1
        else:
            other += 1
    return cjk + (ascii_like + 3) // 4 + (other + 1) // 2


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def count_message(message: dict[str, Any]) -> int:
    """一条 message 的 token 估计：角色开销 + content + 每个 tool_call 的外壳/名/arguments；
    tool 响应另加 tool_call_id 开销。"""
    total = _MESSAGE_OVERHEAD + count_text(_content_text(message.get("content")))
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        total += _TOOL_CALL_OVERHEAD
        total += count_text(str(fn.get("name") or ""))
        total += count_text(str(fn.get("arguments") or ""))
    if message.get("tool_call_id"):
        total += 4
    return total


def count_messages(messages: list[dict[str, Any]]) -> int:
    return sum(count_message(m) for m in messages)


def count_tool_schemas(tools: list[dict[str, Any]] | None) -> int:
    """工具定义每轮都随请求发送，必须计入输入预算，否则压缩阈值会偏小。"""
    if not tools:
        return 0
    return count_text(json.dumps(tools, ensure_ascii=False))


# 单次 usage 可能被缓存计数、网关少报等带偏，比例钳在这个区间。
_SCALE_MIN = 0.25
_SCALE_MAX = 4.0


@dataclass
class TokenBudget:
    """一次 agent loop 的输入窗口账本。

    input_limit = window - output_reserve（至少 1024）。trigger = input_limit × trigger_ratio。
    projected = 本地估计 × scale。scale 默认 1.0；observe 仅在 usage.input_tokens 与估计
    都为正时更新，并钳在 [_SCALE_MIN, _SCALE_MAX]。should_compact 在 projected ≥ trigger 时为真。
    """

    window: int
    output_reserve: int
    trigger_ratio: float
    scale: float = 1.0

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> Self:
        return cls(
            window=settings.context_budget_tokens,
            output_reserve=settings.output_reserve_tokens,
            trigger_ratio=settings.compact_trigger_ratio,
        )

    @property
    def input_limit(self) -> int:
        return max(1024, self.window - self.output_reserve)

    @property
    def trigger(self) -> int:
        return int(self.input_limit * self.trigger_ratio)

    def projected(self, estimate: int) -> int:
        return max(0, int(estimate * self.scale))

    def should_compact(self, estimate: int) -> bool:
        return self.projected(estimate) >= self.trigger

    def observe(self, estimate: int, usage: dict[str, Any] | None) -> None:
        actual = int((usage or {}).get("input_tokens") or 0)
        if estimate <= 0 or actual <= 0:
            return
        self.scale = min(_SCALE_MAX, max(_SCALE_MIN, actual / estimate))
