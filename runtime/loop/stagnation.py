"""死循环探测：相同调用，或长度不超过 3 的重复序列。只返回信号。"""

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# 只用于事后诊断，不参与判重；长任务下必须有界
_HISTORY_MAX = 50
# 比这更长的轮换是一条工作流，不是卡死的短循环。
_MAX_PERIOD = 3


class StagnationSignal(str, Enum):
    NONE = "none"
    REPEAT = "repeat"
    LOOP_CONFIRMED = "loop_confirmed"


def _normalize_args(args: dict[str, Any] | None) -> str:
    payload = args or {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cycle_repeats(history: deque[tuple[str, str, str]]) -> int:
    seq = tuple(history)
    n = len(seq)
    best = 1
    for period in range(1, min(_MAX_PERIOD, n) + 1):
        cycle = seq[-period:]
        count, i = 0, n
        while i >= period and seq[i - period : i] == cycle:
            count += 1
            i -= period
        best = max(best, count)
    return best if n else 0


@dataclass
class StagnationTracker:
    threshold: int = 3
    grace_steps: int = 2
    _streak: int = 0
    _nudged: bool = False
    _grace_left: int = 0
    history: deque[tuple[str, str, str]] = field(
        default_factory=lambda: deque(maxlen=_HISTORY_MAX)
    )

    def observe(self, tool_name: str, args: dict[str, Any] | None, result_text: str) -> StagnationSignal:
        key = (tool_name, _hash_text(_normalize_args(args)), _hash_text(result_text or ""))
        self.history.append(key)
        repeats = _cycle_repeats(self.history)
        if repeats <= 1:
            self._streak = 1
            self._nudged = False
            self._grace_left = 0
            return StagnationSignal.NONE
        self._streak = repeats

        if self._nudged:
            if self._streak >= self.threshold and self._grace_left <= 0:
                return StagnationSignal.LOOP_CONFIRMED
            self._grace_left = max(0, self._grace_left - 1)
            if self._streak >= self.threshold:
                return StagnationSignal.REPEAT
            return StagnationSignal.NONE

        if self._streak >= self.threshold:
            self._nudged = True
            self._grace_left = self.grace_steps
            return StagnationSignal.REPEAT
        return StagnationSignal.NONE


NUDGE_TEXT = (
    "You have repeated the same action sequence without changing the state. "
    "Do not repeat it. Re-plan using a different approach."
)
