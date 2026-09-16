"""OTel GenAI spans。未配置 OTLP 时导出为空操作，span 仍落 JSONL。"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import runtime.observe.spans as S


class Tracer:
    def __init__(self, jsonl_path: Path | None = None, otlp_endpoint: str | None = None) -> None:
        self.jsonl_path = jsonl_path
        self._otel = None
        if otlp_endpoint:
            try:
                from opentelemetry import trace
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                provider = TracerProvider(
                    resource=Resource.create({"service.name": "labhandler"})
                )
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
                )
                trace.set_tracer_provider(provider)
                self._otel = trace.get_tracer("labhandler")
            except Exception:
                self._otel = None

    def _write_jsonl(self, record: dict[str, Any]) -> None:
        if self.jsonl_path is None:
            return
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
        started = time.time()
        bag: dict[str, Any] = {"name": name, "attrs": dict(attrs)}
        otel_cm = None
        if self._otel is not None:
            try:
                otel_cm = self._otel.start_as_current_span(name)
                otel_span = otel_cm.__enter__()
                for k, v in attrs.items():
                    if v is not None:
                        otel_span.set_attribute(k, v if isinstance(v, (bool, int, float, str)) else str(v))
                bag["_otel"] = otel_span
            except Exception:
                otel_cm = None
        try:
            yield bag
        except Exception as e:
            bag["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            bag["elapsed_ms"] = int((time.time() - started) * 1000)
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "name": name,
                "elapsed_ms": bag["elapsed_ms"],
                "attrs": bag.get("attrs") or {},
                "error": bag.get("error"),
            }
            self._write_jsonl(record)
            if otel_cm is not None:
                try:
                    otel_cm.__exit__(None, None, None)
                except Exception:
                    pass


def bind_task_id(tracer: Tracer, task_id: str) -> None:
    """占位：JSONL 每条 span 自己带 attrs。"""
    _ = (tracer, task_id)


__all__ = ["Tracer", "S", "bind_task_id"]
