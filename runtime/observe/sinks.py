"""Span sink 接口与两个实现：JSONL 与 Langfuse。

Tracer 只依赖 Sink 协议。JSONL 是本地兜底；Langfuse 构造失败时由
build_sinks 跳过，主流程不受影响。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class SpanRecord:
    """一个已结束（或刚开始）的 span 的完整描述。"""

    name: str
    kind: str
    span_id: str
    trace_id: str
    parent_id: str | None
    started_at: float
    attrs: dict[str, Any] = field(default_factory=dict)
    inputs: Any = None
    output: Any = None
    elapsed_ms: int = 0
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class Sink(Protocol):
    """span 导出后端。所有方法必须吞掉自身异常，观测不能拖垮主流程。"""

    def open(self, record: SpanRecord) -> Any: ...

    def close(self, record: SpanRecord, handle: Any) -> None: ...

    def event(
        self, record: SpanRecord | None, handle: Any, name: str, attrs: dict[str, Any]
    ) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class JsonlSink:
    """把每个结束的 span 追加成一行 JSON。无外部依赖。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def open(self, record: SpanRecord) -> Any:
        return None

    def close(self, record: SpanRecord, handle: Any) -> None:
        self._write(
            {
                "type": "span",
                "name": record.name,
                "kind": record.kind,
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                "parent_id": record.parent_id,
                "elapsed_ms": record.elapsed_ms,
                "attrs": record.attrs,
                "error": record.error,
                "events": record.events,
            }
        )

    def event(
        self, record: SpanRecord | None, handle: Any, name: str, attrs: dict[str, Any]
    ) -> None:
        if record is not None:
            # 已挂在 span 上，随 close 一起落盘。这里再写一行会让统计重复计数。
            return
        self._write({"type": "event", "name": name, "trace_id": None, "span_id": None, "attrs": attrs})

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


# Langfuse as_type 只接受固定枚举，越界会抛。
_LANGFUSE_KINDS = {
    "span",
    "agent",
    "chain",
    "tool",
    "generation",
    "embedding",
    "retriever",
    "evaluator",
    "guardrail",
}

# 这些 attr 直接映射到 Langfuse 的一等字段，而非 metadata。
_ATTR_MODEL = "gen_ai.request.model"
_ATTR_TOKENS_IN = "gen_ai.usage.input_tokens"
_ATTR_TOKENS_OUT = "gen_ai.usage.output_tokens"
_ATTR_THREAD_ID = "labhandler.thread_id"
_META_MAX = 200
_IO_CAP = 4000


def _cap(value: Any, limit: int = _IO_CAP) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 3] + "..."
    return value


def sanitize_metadata(attrs: dict[str, Any]) -> dict[str, str]:
    """Langfuse v4 要求 metadata 为 dict[str,str] 且值 ≤200 字；密钥类字段丢掉。"""
    out: dict[str, str] = {}
    for key, raw in attrs.items():
        if raw is None:
            continue
        name = str(key)
        low = name.lower()
        if any(tok in low for tok in ("api_key", "secret", "password", "token", "authorization")):
            continue
        text = str(raw)
        if len(text) > _META_MAX:
            text = text[: _META_MAX - 3] + "..."
        out[name[:_META_MAX]] = text
    return out


class LangfuseSink:
    """Langfuse 后端。构造失败或未配置密钥时由 build_sinks 跳过。

    启动时不做 auth_check；密钥由 .env 原样使用，导出失败只记日志，主流程不中断。
    """

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str,
        release: str | None = None,
        environment: str = "development",
        **_: object,
    ) -> None:
        from langfuse import Langfuse

        self.base_url = (host or "").strip().rstrip("/") or "https://cloud.langfuse.com"
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self.base_url,
            host=self.base_url,
            release=release,
            environment=environment,
            tracing_enabled=True,
        )

    def open(self, record: SpanRecord) -> Any:
        try:
            from langfuse import propagate_attributes

            kind = record.kind if record.kind in _LANGFUSE_KINDS else "span"
            meta = sanitize_metadata(record.attrs)
            kwargs: dict[str, Any] = {
                "name": record.name,
                "as_type": kind,
                "metadata": meta,
            }
            if record.inputs is not None:
                kwargs["input"] = _cap(record.inputs)
            model = record.attrs.get(_ATTR_MODEL)
            if kind == "generation" and model:
                kwargs["model"] = str(model)
            cm = self._client.start_as_current_observation(**kwargs)
            span = cm.__enter__()
            session_id = str(record.attrs.get(_ATTR_THREAD_ID) or "")[:200]
            prop_kw: dict[str, Any] = {"metadata": meta, "tags": ["labhandler"]}
            if session_id:
                prop_kw["session_id"] = session_id
            if record.parent_id is None:
                prop_kw["trace_name"] = record.name
            prop = propagate_attributes(**prop_kw)
            prop.__enter__()
            return (cm, prop, span)
        except Exception:
            return None

    def close(self, record: SpanRecord, handle: Any) -> None:
        if handle is None:
            return
        cm, prop, span = handle
        try:
            update: dict[str, Any] = {"metadata": sanitize_metadata(record.attrs)}
            if record.output is not None:
                update["output"] = _cap(record.output)
            if record.error:
                update["level"] = "ERROR"
                update["status_message"] = str(record.error)[:500]
            usage = {
                k: int(record.attrs[v])
                for k, v in (("input", _ATTR_TOKENS_IN), ("output", _ATTR_TOKENS_OUT))
                if isinstance(record.attrs.get(v), int)
            }
            if usage:
                update["usage_details"] = usage
            span.update(**update)
        except Exception:
            pass
        try:
            prop.__exit__(None, None, None)
        except Exception:
            pass
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass

    def event(
        self, record: SpanRecord | None, handle: Any, name: str, attrs: dict[str, Any]
    ) -> None:
        try:
            if handle is not None:
                _, _, span = handle
                span.create_event(name=name, metadata=sanitize_metadata(attrs))
                return
            self._client.start_observation(
                name=name, as_type="span", metadata=sanitize_metadata(attrs)
            ).end()
        except Exception:
            pass

    def trace_id(self) -> str | None:
        try:
            return self._client.get_current_trace_id()
        except Exception:
            return None

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.flush()


def build_sinks(
    *,
    jsonl_path: Path | None,
    langfuse_public_key: str,
    langfuse_secret_key: str,
    langfuse_host: str,
    release: str | None = None,
    environment: str = "development",
    log=None,
) -> list[Sink]:
    """按配置组装 sink 列表。Langfuse 不可用时只降级，不抛。"""
    sinks: list[Sink] = []
    if jsonl_path is not None:
        sinks.append(JsonlSink(jsonl_path))
    if langfuse_public_key and langfuse_secret_key:
        try:
            sinks.append(
                LangfuseSink(
                    public_key=langfuse_public_key,
                    secret_key=langfuse_secret_key,
                    host=langfuse_host,
                    release=release,
                    environment=environment,
                    log=log,
                )
            )
        except Exception as e:
            if log:
                log(f"[observe] Langfuse 初始化失败，已降级为本地 JSONL：{type(e).__name__}: {e}")
    return sinks
