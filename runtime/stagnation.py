"""死循环探测：连续相同 (tool, args_hash, result_hash)。只返回信号。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StagnationSignal(str, Enum):
    NONE = "none"
    REPEAT = "repeat"
    LOOP_CONFIRMED = "loop_confirmed"


def _normalize_args(args: dict[str, Any] | None) -> str:
    payload = args or {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class StagnationTracker:
    threshold: int = 3
    grace_steps: int = 2
    _last_key: tuple[str, str, str] | None = None
    _streak: int = 0
    _nudged: bool = False
    _grace_left: int = 0
    history: list[tuple[str, str, str]] = field(default_factory=list)

    def observe(self, tool_name: str, args: dict[str, Any] | None, result_text: str) -> StagnationSignal:
        key = (tool_name, _hash_text(_normalize_args(args)), _hash_text(result_text or ""))
        self.history.append(key)
        if self._last_key == key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
            self._nudged = False
            self._grace_left = 0

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
    "You have repeated the same action 3 times without changing the state. "
    "Do not repeat it. Re-plan using a different approach."
)
