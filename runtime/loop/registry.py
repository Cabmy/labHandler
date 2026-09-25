"""Tool registry：广告工具表、执行允许集、以及归一化后的 ToolOutcome。

bind 把「模型看见的名字」和「真正能执行的名字」拆开：只读主线广告表跨阶段固定，
执行仍按当前 phase 的 allow 拒绝。独立 agent 广告与执行同一份。本模块不认识阶段，
名字集合由 runtime.phase 给。普通工具的 schema 只用于广告字段和缺参提示，额外
字段放行；submit_* 的结构化约束在 loop 里由 validate_payload 执行。
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
from runtime.loop.parse import check_args, openai_tool
from runtime.loop.schema import SCHEMA_TOOLS
from tools.policy import get_auditor
from tools.sandbox_tools import is_sandbox_unreachable

Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[str]]

def visible_observation(text: str) -> str:
    """成功调用若无正文，补成可见观察。空白会被当成调用失败。"""
    return text if text and text.strip() else "(empty)"


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
    """普通工具：保留 required，额外字段仍放行。不要把 required 藏起来，否则模型会漏必填项。"""
    return {**schema, "additionalProperties": True}


class ToolRegistry:
    def __init__(
        self,
        specs: list[ToolSpec],
        *,
        advertised: list[ToolSpec] | None = None,
        phase: str = "",
        submit_tool: str = "",
    ) -> None:
        self._specs = {s.name: s for s in specs}
        self._advertised = advertised if advertised is not None else list(specs)
        self._advertised_names = {s.name for s in self._advertised}
        self._phase = phase
        self._submit_tool = submit_tool

    def bind(
        self,
        role: str,
        *,
        submit_tool: str,
        extra: frozenset[str] = frozenset(),
        allow: frozenset[str] | None = None,
        advertise: tuple[str, ...] | frozenset[str] | None = None,
        phase: str = "",
    ) -> "ToolRegistry":
        """绑定本拍的广告表与执行允许集。

        advertise 非空：openai 工具表按该顺序输出；execute 只跑 allow
        （缺省为 advertise 全集）。advertise 为空：按 role + extra + submit_tool
        收窄，广告与执行同一份（独立 agent）。
        """
        named = extra | {submit_tool}
        if advertise is not None:
            adv = [self._specs[n] for n in advertise if n in self._specs]
            allow_set = allow if allow is not None else frozenset(s.name for s in adv)
            exec_specs = [s for s in adv if s.name in allow_set]
            return ToolRegistry(
                exec_specs, advertised=adv, phase=phase, submit_tool=submit_tool
            )
        specs = [
            s
            for s in self._specs.values()
            if (role in s.permissions or s.name in named)
            and (s.name not in SCHEMA_TOOLS or s.name in named)
        ]
        if allow is not None:
            keep = allow | named
            specs = [s for s in specs if s.name in keep]
        return ToolRegistry(specs, phase=phase, submit_tool=submit_tool)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            openai_tool(
                s.name,
                s.description,
                s.input_schema if s.name in SCHEMA_TOOLS else _advertise(s.input_schema),
            )
            for s in self._advertised
        ]

    def names(self) -> set[str]:
        return set(self._specs)

    def unavailable_message(self, name: str) -> str:
        phase = self._phase or "this stage"
        exit_tool = self._submit_tool
        if name in self._advertised_names and exit_tool:
            return (
                f"[ERROR/Validation] {name} is unavailable during {phase}. "
                f"The active exit tool is {exit_tool}."
            )
        available = ", ".join(sorted(self._specs))
        return (
            f"[ERROR/Validation] tool {name} is not available at this stage; "
            f"available: {available}"
        )

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
            return ToolOutcome(self.unavailable_message(name), ErrorClass.VALIDATION)
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
            hint = check_args(spec.input_schema, args)
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

        if is_sandbox_unreachable(text):
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
        return ToolOutcome(visible_observation(text), ErrorClass.OK)


