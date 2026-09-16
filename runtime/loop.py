"""一轮 Agent Loop：assemble → llm → tools → record → switch(Decision)。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from config.runtime import RuntimeSettings
from runtime.control import Decision, decide
from runtime.context.assemble import assemble, estimate_tokens
from runtime.context.compact import compact_if_needed
from runtime.errors import ErrorClass
from runtime.llm import LLMGateway
from runtime.observe.tracer import Tracer
from runtime.observe import spans as S
from runtime.retry import sleep_delay
from runtime.schema_call import (
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_PLAN,
    SUBMIT_SUMMARY,
    coerce_brief,
    parse_args,
    synthetic_brief,
    tool_choice_required,
    validate_payload,
    VALIDATION_TOOL_RESULT,
)
from runtime.stagnation import NUDGE_TEXT, StagnationTracker, StagnationSignal
from runtime.task import Permission, RuntimeTask, TaskStatus
from runtime.tools import ToolContext, ToolRegistry


@dataclass
class AgentSpec:
    name: str
    model: str
    permission: Permission
    system: str
    submit_tool: str


@dataclass
class LoopResult:
    brief: dict[str, Any] | None
    submit: dict[str, Any] | None
    reason: str


async def run_loop(
    task: RuntimeTask,
    spec: AgentSpec,
    *,
    settings: RuntimeSettings,
    llm: LLMGateway,
    registry: ToolRegistry,
    session_dir,
    user_input: str,
    plan: str,
    notes: str,
    history: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    tracer: Tracer | None = None,
    clock: Callable[[], float] = time.time,
) -> LoopResult:
    role = spec.permission.value
    tools_reg = registry.for_role(role, extra_names={spec.submit_tool})
    openai_tools = tools_reg.openai_tools()
    tracker = StagnationTracker(
        threshold=settings.stagnation_repeat_threshold,
        grace_steps=settings.stagnation_grace_steps,
    )
    history = list(history or [])
    tool_messages: list[dict[str, Any]] = []
    tool_dumps: list[tuple[int, str, str]] = []
    retrieved = ""
    force_brief = False
    last_error = ErrorClass.OK
    last_stag = StagnationSignal.NONE
    validation_streak: dict[str, int] = {}

    async def emit(kind: str, **payload: Any) -> None:
        if on_event:
            await on_event({"kind": kind, **payload})

    remaining = max(1.0, task.deadline - clock())

    async def one_turn() -> LoopResult | None:
        nonlocal force_brief, last_error, last_stag, history, retrieved, tool_messages

        working = (
            f"completed={task.execution_state.get('completed', [])} "
            f"changed_files={task.execution_state.get('changed_files', [])}"
        )
        ctx_obj = assemble(
            system=spec.system,
            tools_note="",
            user_input=user_input,
            plan=plan,
            history=history,
            retrieved=retrieved,
            notes=notes,
            events=task.events,
            working=working,
            tool_messages=tool_messages,
        )
        history, _ = await compact_if_needed(
            history=history,
            tool_dumps=tool_dumps,
            session_dir=session_dir,
            settings=settings,
            llm=llm,
            estimated_tokens=ctx_obj.estimated_tokens,
        )
        if tracer:
            with tracer.span(
                S.CONTEXT_BUILD,
                **{S.ATTR_TASK_ID: task.task_id, "tokens": ctx_obj.estimated_tokens},
            ):
                pass

        choice: Any = "auto"
        if force_brief:
            choice = tool_choice_required(spec.submit_tool)

        async def on_delta(text: str, reasoning: bool) -> None:
            await emit("content", node=spec.name, text=text, reasoning=reasoning)

        result = await llm.chat(
            model=spec.model,
            messages=ctx_obj.messages,
            tools=openai_tools,
            tool_choice=choice,
            on_delta=on_delta,
        )
        last_error = result.error_class
        if result.error_class is ErrorClass.AUTH:
            task.record(result.error_class, StagnationSignal.NONE, result.usage)
            return LoopResult(None, None, "auth")
        if result.error_class is ErrorClass.TRANSIENT:
            task.record(result.error_class, StagnationSignal.NONE, result.usage)
            return None

        if not result.tool_calls:
            # 无 tool：记一轮并继续，或强制 brief
            task.record(ErrorClass.OK, StagnationSignal.NONE, result.usage)
            if result.content:
                history.append({"role": "assistant", "content": result.content})
            if force_brief:
                return LoopResult(
                    synthetic_brief(outcome="failed", brief="model did not call submit tool"),
                    None,
                    "force_brief_failed",
                )
            return None

        tctx = ToolContext(
            role=role,
            settings=settings,
            session_dir=session_dir,
            llm=llm,
            on_event=on_event,
        )
        submit_payload = None
        submit_name = None
        tool_messages = []
        assistant_tc = {
            "role": "assistant",
            "content": result.content or None,
            "tool_calls": [
                {
                    "id": tc["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for i, tc in enumerate(result.tool_calls)
            ],
        }
        history.append(assistant_tc)

        for i, tc in enumerate(result.tool_calls):
            name = tc["name"]
            await emit("tool", node=spec.name, name=name, args=tc["arguments"][:200], result="")
            degraded = validation_streak.get(name, 0) >= settings.validation_retry_max
            parsed, perr = parse_args(tc["arguments"])
            if parsed is None:
                validation_streak[name] = validation_streak.get(name, 0) + 1
                msg = VALIDATION_TOOL_RESULT.format(err=perr)
                last_error = ErrorClass.VALIDATION
                tool_messages.append(
                    {"role": "tool", "tool_call_id": tc["id"] or f"call_{i}", "content": msg}
                )
                continue

            if name in {SUBMIT_PLAN, SUBMIT_BRIEF, SUBMIT_JUDGE, SUBMIT_SUMMARY}:
                err = validate_payload(name, parsed, degraded=degraded)
                if err:
                    validation_streak[name] = validation_streak.get(name, 0) + 1
                    last_error = ErrorClass.VALIDATION
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"] or f"call_{i}",
                            "content": VALIDATION_TOOL_RESULT.format(err=err),
                        }
                    )
                    continue
                validation_streak[name] = 0
                submit_name, submit_payload = name, parsed
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"] or f"call_{i}",
                        "content": "ok",
                    }
                )
                continue

            outcome = await tools_reg.execute(
                name,
                parsed,
                tctx,
                timeout=settings.tool_timeout_s,
                tracer=tracer,
            )
            last_error = outcome.error_class
            last_stag = tracker.observe(name, parsed, outcome.text)
            tool_dumps.append((task.step_count, name, outcome.text))
            if name in {"memory_search", "memory_grep", "memory_read", "load_skill_reference"}:
                retrieved = (retrieved + "\n" + outcome.text)[-6000:]
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{i}",
                    "content": outcome.text,
                }
            )
            await emit(
                "tool",
                node=spec.name,
                name=name,
                args=str(parsed)[:200],
                result=outcome.text[:200],
                error_class=outcome.error_class.value,
            )
            if tracer:
                with tracer.span(
                    S.TOOL,
                    **{S.ATTR_TOOL: name, S.ATTR_ERROR_CLASS: outcome.error_class.value},
                ):
                    pass

        history.extend(tool_messages)
        task.record(last_error, last_stag, result.usage)

        if submit_payload is not None:
            if submit_name == SUBMIT_BRIEF:
                return LoopResult(coerce_brief(submit_payload), submit_payload, "submit_brief")
            return LoopResult(None, {"name": submit_name, "payload": submit_payload}, "submit")
        return None

    try:
        while True:
            remaining = max(1.0, task.deadline - clock())
            if task.status is TaskStatus.PENDING:
                task.transit(TaskStatus.RUNNING)
            finished = await asyncio.wait_for(one_turn(), timeout=remaining)
            snap = task.snapshot()
            decision = decide(
                snap,
                error_class=last_error,
                stagnation=last_stag,
                settings=settings,
                now=clock(),
            )
            if finished is not None:
                return finished
            if decision.kind == "retry_tool":
                await sleep_delay(decision.delay)
                continue
            if decision.kind == "nudge":
                history.append(
                    {"role": "user", "content": decision.text or NUDGE_TEXT, "pinned": True}
                )
                last_stag = StagnationSignal.NONE
                continue
            if decision.kind == "force_brief":
                if force_brief:
                    return LoopResult(
                        synthetic_brief(
                            outcome="failed",
                            brief="forced submit_brief failed schema or was not called",
                        ),
                        None,
                        "force_brief_failed",
                    )
                force_brief = True
                continue
            if decision.kind == "stop":
                return LoopResult(
                    synthetic_brief(
                        outcome="failed",
                        brief=f"stopped: {decision.reason}",
                    ),
                    None,
                    decision.reason,
                )
            # continue
    except asyncio.TimeoutError:
        return LoopResult(
            synthetic_brief(outcome="failed", brief="wall-clock deadline reached"),
            None,
            "deadline",
        )
