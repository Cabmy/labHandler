"""一轮 Agent Loop：发出去的上下文、模型响应、工具结果与控制面决策。

硬不变量：
1. history 是工具消息的唯一载体。装配层只有这一个入口，同一批结果因此只会
   出现一次，且始终紧跟在它的 assistant 之后。
2. 压缩一旦发生，发给模型的永远是重新装配后的那一份；否则触发压缩的那一轮
   仍会把超长上下文发出去。
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from runtime.context.assemble import AgentContext, assemble, validate_message_sequence
from runtime.context.budget import TokenBudget
from runtime.context.compact import DumpScope, compact_history
from runtime.context.notes import load_notes
from runtime.loop.control import decide
from runtime.errors import ErrorClass
from runtime.llm import LLMGateway
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.phase import PRO
from runtime.loop.retry import sleep_delay
from runtime.loop.calls import run_calls
from runtime.loop.parse import coerce_brief, synthetic_brief, tool_choice_required
from runtime.loop.schema import SUBMIT_BRIEF
from runtime.loop.stagnation import NUDGE_TEXT, StagnationSignal, StagnationTracker
from runtime.task import Permission, RuntimeTask, TaskStatus
from runtime.loop.registry import ToolContext, ToolRegistry
from tools.skill_tool import LOAD_SKILL, LOAD_SKILL_REFERENCE

_RETRIEVAL_TOOLS = {
    "memory_search",
    "memory_grep",
    "memory_read",
    "notes_read",
    LOAD_SKILL,
    LOAD_SKILL_REFERENCE,
}
_RETRIEVED_CAP = 6000

# 一轮里多个工具调用时，取最严重的那个错误类别记账。
# 只留最后一个会让「先失败后成功」的一轮被记成全绿，熔断器永远攒不够计数。
_ERROR_SEVERITY = {
    ErrorClass.OK: 0,
    ErrorClass.ACCEPTABLE: 1,
    ErrorClass.VALIDATION: 2,
    ErrorClass.TRANSIENT: 3,
    ErrorClass.ENVIRONMENT: 4,
    ErrorClass.PERMISSION: 4,
    ErrorClass.LOGIC: 5,
    ErrorClass.LOOP: 6,
    ErrorClass.AUTH: 7,
    ErrorClass.FATAL: 8,
}
_STAGNATION_SEVERITY = {
    StagnationSignal.NONE: 0,
    StagnationSignal.REPEAT: 1,
    StagnationSignal.LOOP_CONFIRMED: 2,
}


def _worse_error(a: ErrorClass, b: ErrorClass) -> ErrorClass:
    return b if _ERROR_SEVERITY.get(b, 0) > _ERROR_SEVERITY.get(a, 0) else a


def _worse_stagnation(a: StagnationSignal, b: StagnationSignal) -> StagnationSignal:
    return b if _STAGNATION_SEVERITY[b] > _STAGNATION_SEVERITY[a] else a


@dataclass
class AgentSpec:
    """一拍 agent 的全部参数，由 runtime.phase 解析好后传进来。

    permission 是节点权限（ctx.role，决定写检查放不放行）；visible_role、
    extra_tools、allow_tools 只管工具表里出现哪些名字，由阶段定义给出。
    tool_extras 原样进 ToolContext（例如全程共用的 SkillBind）。
    """

    name: str
    model: str
    permission: Permission
    system: str
    submit_tool: str
    visible_role: str
    extra_tools: frozenset[str] = frozenset()
    allow_tools: frozenset[str] | None = None
    tool_choice: str = "auto"
    tool_extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    brief: dict[str, Any] | None
    submit: dict[str, Any] | None
    reason: str
    history: list[dict[str, Any]] = field(default_factory=list)


def drop_dangling_tool_calls(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉末尾没有 tool 响应的 assistant。

    上一轮如果在工具执行途中被墙钟或取消打断，history 会以一个悬空的
    tool_calls 结尾，下一次请求必然被网关拒。
    """
    if not history:
        return history
    last_owner = -1
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_owner = i
    if last_owner < 0:
        return history
    answered = {
        str(m.get("tool_call_id") or "")
        for m in history[last_owner + 1 :]
        if m.get("role") == "tool"
    }
    expected = {str(tc.get("id") or "") for tc in history[last_owner]["tool_calls"]}
    if expected <= answered:
        return history
    return history[:last_owner]


async def run_loop(
    task: RuntimeTask,
    spec: AgentSpec,
    *,
    settings: RuntimeSettings,
    llm: LLMGateway,
    registry: ToolRegistry,
    session_dir: Path,
    user_input: str,
    project_spec: str,
    history: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    tracer: Tracer | None = None,
    clock: Callable[[], float] = time.time,
) -> LoopResult:
    role = spec.permission.value
    tools_reg = registry.for_role(
        spec.visible_role,
        submit_tool=spec.submit_tool,
        extra=spec.extra_tools,
        allow=spec.allow_tools,
    )
    openai_tools = tools_reg.openai_tools()
    tracker = StagnationTracker(
        threshold=settings.stagnation_repeat_threshold,
        grace_steps=settings.stagnation_grace_steps,
    )
    history = list(history or [])
    retrieved = ""
    force_brief = False
    last_error = ErrorClass.OK
    last_stag = StagnationSignal.NONE
    validation_streak: dict[str, int] = {}
    tokens = TokenBudget.from_settings(settings)
    dump = DumpScope(session_dir=session_dir, agent=spec.name, task_id=task.task_id)

    def done(
        brief: dict[str, Any] | None,
        submit: dict[str, Any] | None,
        reason: str,
    ) -> LoopResult:
        return LoopResult(brief, submit, reason, history=list(history))

    async def emit(kind: str, **payload: Any) -> None:
        if on_event:
            await on_event({"kind": kind, **payload})

    def trace_event(name: str, **attrs: Any) -> None:
        if tracer is not None:
            tracer.event(name, **attrs)

    async def build_context() -> AgentContext:
        """返回本轮实际发给模型的上下文。压缩一旦发生，history 已换成压缩后的版本。"""
        nonlocal history
        history = drop_dangling_tool_calls(history)
        notes = load_notes(session_dir)
        working = ""

        def build() -> AgentContext:
            return assemble(
                system=spec.system,
                user_input=user_input,
                project_spec=project_spec,
                history=history,
                retrieved=retrieved,
                notes=notes.notes_block,
                cards=notes.cards_block if spec.name == PRO else "",
                events=task.events,
                working=working,
                tool_schemas=openai_tools,
            )

        ctx = build()
        result = await compact_history(
            history=history,
            dump=dump,
            settings=settings,
            llm=llm,
            notes=notes,
            budget=tokens,
            estimate=ctx.input_tokens,
            apply_forget=spec.name == PRO,
            tracer=tracer,
        )
        if result.compacted:
            history = result.history
            ctx = build()
            await emit(
                "compact",
                node=spec.name,
                before=result.tokens_before,
                after=tokens.projected(ctx.input_tokens),
                summarized=result.turns_summarized,
                forget_cleared=result.forget_cleared,
                scale=round(tokens.scale, 3),
            )
        return ctx

    async def one_turn() -> LoopResult | None:
        nonlocal force_brief, last_error, last_stag, history, retrieved

        span_cm = (
            tracer.span(
                S.TURN,
                kind=S.KIND_CHAIN,
                **{
                    S.ATTR_TASK_ID: task.task_id,
                    S.ATTR_AGENT: spec.name,
                    S.ATTR_STEP: task.step_count,
                    S.ATTR_STEP_BUDGET: task.step_budget,
                },
            )
            if tracer is not None
            else None
        )
        turn_span = span_cm.__enter__() if span_cm is not None else None
        try:
            ctx = await build_context()
            if turn_span is not None:
                turn_span.set(**{S.ATTR_TOKENS_IN: ctx.input_tokens})

            problems = validate_message_sequence(ctx.messages)
            if problems:
                trace_event(S.EV_DECISION, reason="protocol_violation", detail="; ".join(problems))

            choice: Any = (
                tool_choice_required(spec.submit_tool) if force_brief else spec.tool_choice
            )

            async def on_delta(text: str, reasoning: bool) -> None:
                await emit("content", node=spec.name, text=text, reasoning=reasoning)

            result = await llm.chat(
                model=spec.model,
                messages=ctx.messages,
                tools=openai_tools,
                tool_choice=choice,
                on_delta=on_delta,
            )
            last_error = result.error_class
            tokens.observe(ctx.input_tokens, result.usage)

            if result.error_class is ErrorClass.AUTH:
                task.record(result.error_class, StagnationSignal.NONE, result.usage)
                return done(None, None, "auth")
            if result.error_class is ErrorClass.TRANSIENT:
                task.record(result.error_class, StagnationSignal.NONE, result.usage)
                return None
            if result.error_class is not ErrorClass.OK and not result.tool_calls:
                # 网关报错却被记成 OK 时，会空转把 step_budget 耗尽（SPEC 长时间不动）。
                task.record(result.error_class, StagnationSignal.NONE, result.usage)
                return None
            if result.truncated:
                # finish_reason=length：content / tool_calls 可能是半截。events 注入截断提示。
                trace_event(S.EV_DECISION, reason="output_truncated")
                task.events.append(
                    {"text": "Your previous reply was cut off by the output limit. Be more concise."}
                )

            if not result.tool_calls:
                task.record(ErrorClass.OK, StagnationSignal.NONE, result.usage)
                if result.content:
                    history.append({"role": "assistant", "content": result.content})
                    task.events.append(
                        {
                            "text": (
                                f"This phase only finishes by calling {spec.submit_tool}. "
                                "A schema error is not a dead end: resubmit with the type the hint "
                                "asked for (a string field is one string, not an object or array). "
                                "Do not say you cannot complete the task."
                            )
                        }
                    )
                if force_brief:
                    return done(
                        synthetic_brief(
                            outcome="failed", brief="model did not call the required submit tool"
                        ),
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
                extras={**spec.tool_extras, "dump": dump},
            )
            submit_name: str | None = None
            submit_payload: dict[str, Any] | None = None
            tool_messages: list[dict[str, Any]] = []

            calls = [
                {**tc, "id": tc["id"] or f"call_{i}"} for i, tc in enumerate(result.tool_calls)
            ]
            history.append(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in calls
                    ],
                }
            )

            outcomes = await run_calls(
                calls,
                submit_tool=spec.submit_tool,
                node=spec.name,
                registry=tools_reg,
                ctx=tctx,
                settings=settings,
                tracer=tracer,
                validation_streak=validation_streak,
                emit=emit,
            )

            turn_error = ErrorClass.OK
            turn_stag = StagnationSignal.NONE
            for out in outcomes:
                tool_messages.append(
                    {"role": "tool", "tool_call_id": out.call_id, "content": out.text}
                )
                turn_error = _worse_error(turn_error, out.error)
                if out.submit_payload is not None:
                    submit_name, submit_payload = out.submit_name, out.submit_payload
                if out.args is None:
                    continue
                if not out.ran and out.error is not ErrorClass.VALIDATION:
                    continue
                signal = tracker.observe(out.name, out.args, out.text)
                turn_stag = _worse_stagnation(turn_stag, signal)
                if signal is not StagnationSignal.NONE:
                    trace_event(
                        S.EV_STAGNATION,
                        **{S.ATTR_SIGNAL: signal.value, S.ATTR_TOOL: out.name},
                    )
                if out.name in _RETRIEVAL_TOOLS:
                    retrieved = (retrieved + "\n" + out.text)[-_RETRIEVED_CAP:]

            history.extend(tool_messages)
            last_error, last_stag = turn_error, turn_stag
            task.record(last_error, last_stag, result.usage)

            if submit_payload is not None:
                if turn_span is not None:
                    turn_span.set(**{S.ATTR_OUTCOME: submit_name})
                if submit_name == SUBMIT_BRIEF:
                    return done(coerce_brief(submit_payload), submit_payload, "submit_brief")
                return done(None, {"name": submit_name, "payload": submit_payload}, "submit")
            return None
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    try:
        while True:
            remaining = max(1.0, task.deadline - clock())
            if task.status is TaskStatus.PENDING:
                task.transit(TaskStatus.RUNNING)

            finished = await asyncio.wait_for(one_turn(), timeout=remaining)
            if finished is not None:
                return finished

            decision = decide(
                task.snapshot(),
                error_class=last_error,
                stagnation=last_stag,
                settings=settings,
                now=clock(),
            )
            trace_event(
                S.EV_DECISION,
                **{S.ATTR_DECISION: decision.kind, S.ATTR_REASON: decision.reason},
            )

            if decision.kind == "retry_tool":
                trace_event(
                    S.EV_RETRY,
                    **{
                        S.ATTR_ATTEMPT: task.transient_count,
                        S.ATTR_DELAY_S: decision.delay,
                        S.ATTR_ERROR_CLASS: last_error.value,
                    },
                )
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
                    return done(
                        synthetic_brief(
                            outcome="failed",
                            brief="forced submit failed schema or was not called",
                        ),
                        None,
                        "force_brief_failed",
                    )
                force_brief = True
                continue

            if decision.kind == "stop":
                return done(
                    synthetic_brief(outcome="failed", brief=f"stopped: {decision.reason}"),
                    None,
                    decision.reason,
                )
    except asyncio.TimeoutError:
        return done(
            synthetic_brief(outcome="failed", brief="wall-clock deadline reached"),
            None,
            "deadline",
        )
