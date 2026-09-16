"""Tool registry：可见工具表、执行、以及归一化后的 ToolOutcome。

for_role 产出的收窄 registry 同时是可见性边界和执行边界：不在表里的名字调不动，
模型从 history 里翻出别处的工具名照抄也会被挡下。收窄依据由 runtime.phase 给，
本模块不认识阶段。普通工具的 schema 只用于广告字段和缺参提示，额外字段放行；
submit_* 的结构化约束在 loop 里由 validate_payload 执行。error_class 从正文前缀
或异常类型得出。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.loop.parse import openai_tool
from runtime.loop.schema import SCHEMA_TOOLS
from tools.policy import get_auditor

Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: frozenset[str]
    handler: Handler


@dataclass
class ToolContext:
    role: str
    settings: RuntimeSettings
    session_dir: Any
    llm: Any
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    text: str
    error_class: ErrorClass = ErrorClass.OK


def _fields(*required: str, extra: bool = False, **props: str) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {k: {"type": t} for k, t in props.items()},
    }
    if required:
        schema["required"] = list(required)
    if extra:
        schema["additionalProperties"] = True
    return schema


def _advertise(schema: dict[str, Any]) -> dict[str, Any]:
    advertised = {**schema, "additionalProperties": True}
    advertised.pop("required", None)
    return advertised


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_args(schema: dict[str, Any], args: dict[str, Any]) -> str:
    """按 schema.required / 类型给出缺参提示，不拦截额外字段。"""
    miss = [k for k in (schema.get("required") or []) if _blank(args.get(k))]
    if miss:
        return f"[ERROR/Validation] missing {', '.join(miss)}"
    for key, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        raw = args.get(key)
        if raw in (None, ""):
            continue
        t = spec.get("type")
        if t == "integer":
            try:
                args[key] = int(raw)
            except (TypeError, ValueError):
                return f"[ERROR/Validation] {key} must be an integer"
        elif t == "string" and not isinstance(raw, str):
            return f"[ERROR/Validation] {key} must be a string"
    return ""


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def for_role(
        self, role: str, submit_tool: str, extra: frozenset[str] = frozenset()
    ) -> "ToolRegistry":
        """本拍实际可见的工具：role 允许的，加 extra 点名的，加唯一出口 submit_tool。

        role / extra 由 runtime.phase 决定，本模块不认识阶段。收窄后的这份
        registry 同时用于 execute，所以不在表里的名字调不动。
        """
        specs = [
            s
            for s in self._specs.values()
            if (role in s.permissions or s.name in extra or s.name == submit_tool)
            and (s.name not in SCHEMA_TOOLS or s.name == submit_tool)
        ]
        return ToolRegistry(specs)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            openai_tool(
                s.name,
                s.description,
                s.input_schema if s.name in SCHEMA_TOOLS else _advertise(s.input_schema),
            )
            for s in self._specs.values()
        ]

    def names(self) -> set[str]:
        return set(self._specs)

    def parallelizable(self, name: str) -> bool:
        """无副作用、且不是结构化出口的工具才可与相邻只读调用并行。"""
        if name in SCHEMA_TOOLS:
            return False
        spec = self._specs.get(name)
        return spec is not None and "readonly" in spec.permissions

    async def execute(
        self,
        name: str,
        raw_args: str | dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float,
        tracer: Tracer | None = None,
    ) -> ToolOutcome:
        if tracer is None:
            return await self._execute(name, raw_args, ctx, timeout=timeout, tracer=None)
        with tracer.span(
            S.TOOL,
            kind=S.KIND_TOOL,
            inputs=raw_args if isinstance(raw_args, dict) else str(raw_args)[:2000],
            **{
                S.ATTR_TOOL: name,
                S.ATTR_PERMISSION: ctx.role,
                S.ATTR_TOOL_CALL_ID: str(ctx.extras.get("tool_call_id") or "") or None,
            },
        ) as span:
            outcome = await self._execute(name, raw_args, ctx, timeout=timeout, tracer=tracer)
            span.set(**{S.ATTR_ERROR_CLASS: outcome.error_class.value})
            span.output(outcome.text[:2000])
            return outcome

    async def _execute(
        self,
        name: str,
        raw_args: str | dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float,
        tracer: Tracer | None,
    ) -> ToolOutcome:
        spec = self._specs.get(name)
        if spec is None:
            # 本阶段工具表里没有。列出可用名字，避免模型照着 history 里的旧工具反复重试。
            return ToolOutcome(
                f"[ERROR/Validation] tool {name} is not available at this stage; "
                f"available: {', '.join(sorted(self._specs))}",
                ErrorClass.VALIDATION,
            )
        if ctx.role not in spec.permissions and name not in SCHEMA_TOOLS:
            get_auditor().record(name, {}, f"denied:role={ctx.role}")
            if tracer is not None:
                tracer.event(S.GUARDRAIL, **{S.ATTR_TOOL: name, S.ATTR_TOOL_ALLOWED: False})
            return ToolOutcome(
                f"[ERROR/PermissionError] tool {name} is not allowed for role={ctx.role}",
                ErrorClass.PERMISSION,
            )
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as e:
                return ToolOutcome(f"[ERROR/Validation] invalid json: {e}", ErrorClass.VALIDATION)
        else:
            args = raw_args
        if not isinstance(args, dict):
            return ToolOutcome("[ERROR/Validation] arguments must be an object", ErrorClass.VALIDATION)
        if name not in SCHEMA_TOOLS:
            hint = _check_args(spec.input_schema, args)
            if hint:
                return ToolOutcome(hint, ErrorClass.VALIDATION)

        ctx.extras["submit_name"] = name
        try:
            text = await asyncio.wait_for(spec.handler(args, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            get_auditor().record(name, args, "error:timeout")
            return ToolOutcome(f"[TIMEOUT after {timeout}s] tool {name}", ErrorClass.TRANSIENT)
        except PermissionError as e:
            get_auditor().record(name, args, f"denied:{e}")
            return ToolOutcome(f"[ERROR/PermissionError] {e}", ErrorClass.PERMISSION)
        except Exception as e:
            get_auditor().record(name, args, f"error:{type(e).__name__}")
            return ToolOutcome(f"[ERROR/{type(e).__name__}] {e}", classify(e))

        if "[SANDBOX_UNREACHABLE]" in text:
            return ToolOutcome(text, ErrorClass.FATAL)
        if text.startswith("[ERROR/PermissionError]"):
            return ToolOutcome(text, ErrorClass.PERMISSION)
        if text.startswith("[ERROR/Validation]"):
            return ToolOutcome(text, ErrorClass.VALIDATION)
        if text.startswith("[TIMEOUT"):
            return ToolOutcome(text, ErrorClass.TRANSIENT)
        if text.startswith("[ERROR/"):
            cls = classify(None, text=text)
            return ToolOutcome(text, ErrorClass.LOGIC if cls is ErrorClass.OK else cls)
        get_auditor().record(name, args, "ok")
        from runtime.context.compact import DumpScope, LIVE_OFFLOAD_CHARS, dump_tool_result

        dump = ctx.extras.get("dump")
        if (
            isinstance(dump, DumpScope)
            and len(text) > LIVE_OFFLOAD_CHARS
        ):
            text = dump_tool_result(
                dump,
                tool=name,
                call_id=str(ctx.extras.get("tool_call_id") or ""),
                body=text,
            )
        return ToolOutcome(text, ErrorClass.OK)


