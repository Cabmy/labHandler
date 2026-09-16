"""Tracer：真实计时 + 父子嵌套 + 贯穿一次 run 的 trace_id。

span 必须包住真正的操作体：事后 `with tracer.span(...): pass` 会使
elapsed_ms 恒为 0 且丢掉因果。父子关系用 contextvars 维护；
asyncio.create_task 会拷贝当前 context，并行 worker 挂在所在 step 的 span 下。
Sink 自身异常被吞掉，观测失败不改变主流程。
"""

import contextvars
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from runtime.observe import spans as S
from runtime.observe.sinks import Sink, SpanRecord

_CURRENT: contextvars.ContextVar["SpanHandle | None"] = contextvars.ContextVar(
    "labhandler_current_span", default=None
)


class SpanHandle:
    """一个活跃 span。用 set/output/event 往上挂数据，结束由 Tracer 负责。"""

    __slots__ = ("record", "_handles", "_tracer")

    def __init__(self, record: SpanRecord, handles: list[Any], tracer: "Tracer") -> None:
        self.record = record
        self._handles = handles
        self._tracer = tracer

    @property
    def trace_id(self) -> str:
        return self.record.trace_id

    @property
    def span_id(self) -> str:
        return self.record.span_id

    def set(self, **attrs: Any) -> "SpanHandle":
        for k, v in attrs.items():
            if v is not None:
                self.record.attrs[k] = v
        return self

    def output(self, value: Any) -> "SpanHandle":
        self.record.output = value
        return self

    def event(self, name: str, **attrs: Any) -> None:
        payload = {k: v for k, v in attrs.items() if v is not None}
        self.record.events.append({"name": name, **payload})
        for sink, handle in zip(self._tracer.sinks, self._handles):
            try:
                sink.event(self.record, handle, name, payload)
            except Exception:
                pass


class _NullSpan(SpanHandle):
    """未启用观测时的占位，保持调用点无分支。"""

    def __init__(self) -> None:  # noqa: D107 - 不调用父类
        self.record = SpanRecord(
            name="", kind=S.KIND_SPAN, span_id="", trace_id="", parent_id=None, started_at=0.0
        )
        self._handles = []
        self._tracer = None  # type: ignore[assignment]

    def set(self, **attrs: Any) -> "SpanHandle":
        return self

    def output(self, value: Any) -> "SpanHandle":
        return self

    def event(self, name: str, **attrs: Any) -> None:
        return None


_NULL = _NullSpan()


class Tracer:
    def __init__(self, sinks: list[Sink] | None = None, *, trace_id: str | None = None) -> None:
        self.sinks = sinks or []
        self.trace_id = trace_id or uuid.uuid4().hex

    @property
    def enabled(self) -> bool:
        return bool(self.sinks)

    def new_trace(self, trace_id: str | None = None) -> str:
        """开一条新 trace（一次 lab = 一条）。"""
        self.trace_id = trace_id or uuid.uuid4().hex
        return self.trace_id

    def current(self) -> SpanHandle | None:
        return _CURRENT.get()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = S.KIND_SPAN,
        inputs: Any = None,
        **attrs: Any,
    ) -> Iterator[SpanHandle]:
        """包住操作体。elapsed_ms 是真实耗时，异常会记进 span 并继续上抛。"""
        if not self.sinks:
            yield _NULL
            return

        parent = _CURRENT.get()
        record = SpanRecord(
            name=name,
            kind=kind,
            span_id=uuid.uuid4().hex[:16],
            trace_id=self.trace_id,
            parent_id=parent.span_id if parent else None,
            started_at=time.perf_counter(),
            attrs={S.ATTR_RUN_ID: self.trace_id, **{k: v for k, v in attrs.items() if v is not None}},
            inputs=inputs,
        )
        handles = []
        for sink in self.sinks:
            try:
                handles.append(sink.open(record))
            except Exception:
                handles.append(None)

        handle = SpanHandle(record, handles, self)
        token = _CURRENT.set(handle)
        try:
            yield handle
        except BaseException as e:
            record.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            _CURRENT.reset(token)
            record.elapsed_ms = int((time.perf_counter() - record.started_at) * 1000)
            for sink, h in zip(self.sinks, handles):
                try:
                    sink.close(record, h)
                except Exception:
                    pass

    def event(self, name: str, **attrs: Any) -> None:
        """挂在当前 span 上的点事件：重试、停滞、handoff、决策。"""
        if not self.sinks:
            return
        current = _CURRENT.get()
        if current is not None:
            current.event(name, **attrs)
            return
        payload = {k: v for k, v in attrs.items() if v is not None}
        for sink in self.sinks:
            try:
                sink.event(None, None, name, payload)
            except Exception:
                pass

    def flush(self) -> None:
        for sink in self.sinks:
            try:
                sink.flush()
            except Exception:
                pass

    def shutdown(self) -> None:
        for sink in self.sinks:
            try:
                sink.shutdown()
            except Exception:
                pass


NULL_TRACER = Tracer([])
"""未启用观测时的 Tracer：span 让出 _NULL，event / flush / shutdown 都是空操作。

调用点因此永远可以直接 `with tracer.span(...) as span:`，不必写 None 分支，
也不必手工 __enter__ / __exit__。可选参数在入口用 as_tracer 归一。
"""


def as_tracer(tracer: Tracer | None) -> Tracer:
    return tracer if tracer is not None else NULL_TRACER


__all__ = ["Tracer", "SpanHandle", "NULL_TRACER", "as_tracer", "S"]
