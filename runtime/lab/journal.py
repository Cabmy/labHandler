"""会话的 append-only 事件日志：JOURNAL.jsonl 是一次 lab 的唯一事实源。

每行一个事件 {"seq", "kind", "payload"}，append 即写盘 + fsync。replay 逐行
解析完整记录，尾部半行就是崩溃点；已提交的坏行拒绝恢复。LabState 是日志的投影：恢复时 replay
重建全部可变状态；transcript 由最后一个 tx_snapshot 加其后的 tx_delta 重建，
因此 Pro 崩在任意一轮都能从已落盘的前缀续传，而不是整阶段重跑。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

JOURNAL_FILE = "JOURNAL.jsonl"

_STAGES = {"remember", "spec", "advance", "summary"}


@dataclass
class JournalState:
    """replay 的产物：一份可直接注入 LabState 的还原快照。"""

    begun: bool = False
    question: str = ""
    # 有 session_closed 才终结；无 live 协程的 open 会话始终可恢复。
    result: dict[str, Any] | None = None
    pause_reason: str = "interrupted"
    attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    stage: str = "remember"
    dispatch_step: int = 0
    skill: str = ""
    progress: list[tuple[str, str]] = field(default_factory=list)
    executed_ids: set[str] = field(default_factory=set)
    gates: dict[str, str] = field(default_factory=dict)
    gate_tries: dict[str, int] = field(default_factory=dict)
    broken_gates: set[str] = field(default_factory=set)
    run_gate: dict[str, Any] | None = None
    halt: dict[str, Any] | None = None
    sandbox_down: str = ""
    # step 内断点标记：dispatch 提交点、逐 worker 的 brief、judge 决定。
    step_dispatch: dict[int, dict[str, Any]] = field(default_factory=dict)
    step_briefs: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    step_judges: dict[int, str] = field(default_factory=dict)
    # 副作用账本：node_id -> {probe: fingerprint}。
    effects: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def resumable(self) -> bool:
        return self.begun and self.result is None

    def status(self, *, running: bool = False) -> str | None:
        """运行状态是视图；磁盘从不声称有一个活着的协程。"""
        if running:
            return "RUNNING"
        if not self.begun:
            return None
        return "FINISHED" if self.result is not None else "PAUSED"

    def latest_submit(self, name: str) -> dict[str, Any] | None:
        for attempt in reversed(list(self.attempts.values())):
            submit = (attempt.get("end") or {}).get("submit") or {}
            if submit.get("name") == name:
                return submit["payload"]
        return None


class Journal:
    """单个会话目录的事件日志。单写者：一次 run 只有一个 Journal 实例在写。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = 0
        self._tx_len = 0  # 已落盘的 transcript 前缀长度

    @classmethod
    def open(cls, sdir: Path) -> "Journal":
        sdir.mkdir(parents=True, exist_ok=True)
        journal = cls(sdir / JOURNAL_FILE)
        if journal.path.is_file():
            # 只有以换行结尾的记录提交成功；先移除崩溃留下的半行再追加，
            # 否则下一条事件会粘到坏行后面一起丢失。
            with journal.path.open("r+b") as f:
                data = f.read()
                boundary = data.rfind(b"\n") + 1
                if boundary < len(data):
                    f.truncate(boundary)
                journal._seq = data[:boundary].count(b"\n")
        return journal

    def sync_tx(self, length: int) -> None:
        """replay 之后调用：让增量落盘从还原出的 transcript 长度接着算。"""
        self._tx_len = max(0, length)

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        self._seq += 1
        line = json.dumps(
            {"seq": self._seq, "kind": kind, "payload": payload},
            ensure_ascii=False,
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def append_transcript(self, history: list[dict[str, Any]], compacted: bool) -> None:
        """transcript 增量落盘。压缩或长度回退（末尾被裁）时改为全量快照。"""
        if compacted or len(history) < self._tx_len:
            self.append("tx_snapshot", {"messages": history})
        else:
            delta = history[self._tx_len:]
            if not delta:
                return
            self.append("tx_delta", {"messages": delta})
        self._tx_len = len(history)

    def _iter_events(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """只重放完整的已提交记录，拒绝静默跳过损坏的生命周期事实。"""
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_bytes().splitlines(keepends=True)
        except FileNotFoundError:
            return
        for line in lines:
            if not line.endswith(b"\n"):
                break
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise ValueError("journal event must be an object")
            kind = rec.get("kind")
            payload = rec.get("payload")
            if isinstance(kind, str) and isinstance(payload, dict):
                yield kind, payload
            else:
                raise ValueError("invalid journal event")

    def replay(self) -> JournalState:
        # 延迟导入：cycle 经 context.compact 回头依赖 persist，顶层导入会成环。
        from runtime.loop.cycle import drop_dangling_tool_calls

        st = JournalState()
        tx: list[dict[str, Any]] = []
        for kind, payload in self._iter_events():
            if kind == "begin":
                if payload.get("version") != 1:
                    raise ValueError("unsupported journal format")
                st.begun = True
                st.question = str(payload.get("question") or "")
            elif kind == "run_started":
                st.pause_reason = "interrupted"
            elif kind == "run_paused":
                st.pause_reason = str(payload["reason"])
            elif kind == "session_closed":
                st.result = dict(payload["result"])
            elif kind == "attempt_started":
                st.attempts[payload["task_id"]] = dict(payload)
            elif kind == "attempt_ended":
                st.attempts[payload["task_id"]]["end"] = dict(payload)
            elif kind == "tx_snapshot":
                messages = payload.get("messages")
                tx = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
            elif kind == "tx_delta":
                messages = payload.get("messages")
                if isinstance(messages, list):
                    tx.extend(m for m in messages if isinstance(m, dict))
            elif kind == "stage":
                stage = str(payload.get("stage") or "")
                if stage in _STAGES:
                    st.stage = stage
                st.dispatch_step = int(payload.get("dispatch_step") or 0)
                skill = str(payload.get("skill") or "").strip()
                if skill:
                    st.skill = skill
            elif kind == "progress":
                aid, text = str(payload.get("aid") or ""), str(payload.get("text") or "")
                if aid:
                    st.progress.append((aid, text))
            elif kind == "executed":
                ids = payload.get("ids")
                if isinstance(ids, list):
                    st.executed_ids = {str(x) for x in ids if x}
            elif kind == "gate":
                self._fold_gate(st, payload)
            elif kind == "run_gate":
                if payload.get("state"):
                    st.run_gate = dict(payload)
            elif kind == "halt":
                if str(payload.get("reason") or "").strip():
                    st.halt = dict(payload)
            elif kind == "sandbox_down":
                st.sandbox_down = str(payload.get("reason") or "")
            elif kind == "dispatch":
                step = int(payload.get("step") or 0)
                if step > 0:
                    st.step_dispatch[step] = dict(payload)
            elif kind == "brief":
                step = int(payload.get("step") or 0)
                aid = str(payload.get("aid") or "")
                brief = payload.get("brief")
                if step > 0 and aid and isinstance(brief, dict):
                    st.step_briefs.setdefault(step, {})[aid] = brief
            elif kind == "judge":
                step = int(payload.get("step") or 0)
                decision = str(payload.get("decision") or "")
                if step > 0 and decision:
                    st.step_judges[step] = decision
            elif kind == "effect_record":
                node_id = str(payload.get("node_id") or "")
                prints = payload.get("prints")
                if node_id and isinstance(prints, dict) and prints:
                    st.effects[node_id] = dict(prints)
            elif kind == "effect_drop":
                st.effects.pop(str(payload.get("node_id") or ""), None)
        st.transcript = drop_dangling_tool_calls(tx)
        return st

    @staticmethod
    def _fold_gate(st: JournalState, payload: dict[str, Any]) -> None:
        """gate 事件记的是结算后的结果（tries/broken 已算好），replay 只做末值覆盖。"""
        gid = str(payload.get("gate_id") or "")
        state = str(payload.get("state") or "")
        if not gid or not state:
            return
        st.gates[gid] = state
        tries = payload.get("tries")
        if tries is None:
            st.gate_tries.pop(gid, None)
        else:
            st.gate_tries[gid] = int(tries)
        if payload.get("broken"):
            st.broken_gates.add(gid)
        else:
            st.broken_gates.discard(gid)
