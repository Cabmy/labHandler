"""一次 lab 的入口：取消、AgentSpec、把 run 交给 flow。

阶段循环在 runtime/lab/flow.py，Flash 一波在 runtime/lab/step.py。
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from memory.profile import inject_for_agent
from runtime.context.disclosure import skill_catalog_block
from runtime.lab.flow import run_lab
from runtime.llm import LLMGateway
from runtime.loop import AgentSpec
from runtime.loop.tools import ToolRegistry, build_registry
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer, as_tracer
from runtime.phase import PRO, phase_of, system_for
from runtime.task import Permission, TaskKind
from runtime.lab.journal import Journal
from runtime.lab.execution import RunPaused
from tools.skill_tool import LOAD_SKILL, SkillBind

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class LabRunner:
    def __init__(
        self,
        settings: RuntimeSettings,
        llm: LLMGateway,
        *,
        registry: ToolRegistry | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry or build_registry()
        # 归一后 tracer 永不为 None：调用点直接 with self.tracer.span(...)，无 None 分支。
        self.tracer = as_tracer(tracer)
        self.clock = clock
        self._active: asyncio.Task | None = None
        self._applied_rules: list[str] | None = None
        self._skill = SkillBind()

    @property
    def running(self) -> bool:
        return self._active is not None

    def request_stop(self) -> None:
        # 不另存取消标记：运行协程就是执行与取消的唯一控制源。
        if self.running and not self._active.cancelling():
            self._active.cancel()

    async def _emit(self, sink: EventSink | None, payload: dict[str, Any]) -> None:
        if sink:
            await sink(payload)

    async def _emit_halt(self, sink: EventSink | None, halt: dict[str, Any]) -> None:
        await self._emit(
            sink,
            {
                "kind": "halt",
                "reason": str(halt.get("reason") or "")[:800],
                "need_from_user": str(halt.get("need_from_user") or "")[:800],
            },
        )

    def _trace_event(self, name: str, **attrs: Any) -> None:
        self.tracer.event(name, **attrs)

    def _agent_spec(self, kind: TaskKind, permission: Permission) -> AgentSpec:
        """把阶段定义解析成这一拍的 AgentSpec：模型、注入后的 system、工具可见范围。"""
        phase = phase_of(kind)
        model = self.settings.pro_model if phase.agent == PRO else self.settings.flash_model
        include_rules = self._applied_rules is not None
        system = inject_for_agent(
            phase.agent,
            system_for(kind),
            rules=self._applied_rules or [],
            include_rules=include_rules,
        )
        # 目录只给能 load_skill 的拍。Remember-Judge 看不见 skill，避免用目录反推规则。
        if phase.agent == PRO and (
            LOAD_SKILL in (phase.allow_tools or frozenset())
            or LOAD_SKILL in phase.extra_tools
            or phase.visible is None
        ):
            catalog = skill_catalog_block()
            if catalog:
                system = f"{system}\n\n{catalog}"
        return AgentSpec(
            name=phase.agent,
            model=model,
            permission=permission,
            system=system,
            submit_tool=phase.submit_tool,
            visible_role=phase.visible_role(permission),
            extra_tools=phase.extra_tools,
            allow_tools=phase.allow_tools,
            advertise_tools=phase.advertise_tools,
            tool_choice=phase.tool_choice,
            tool_extras={"skill": self._skill} if phase.agent == PRO else {},
            shares_thread=phase.shares_thread,
            phase=kind.value,
        )

    async def run(
        self,
        question: str,
        session_dir: Path,
        *,
        on_event: EventSink | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("lab is already running")
        journal = Journal.open(session_dir)
        state = journal.replay()
        if resume:
            if not state.resumable:
                raise ValueError("session is not resumable")
            question = state.question
        else:
            if state.begun:
                raise ValueError("new lab requires a new session directory")
            journal.append("begin", {"version": 1, "question": question})
        journal.append("run_started", {})
        try:
            with self.tracer.span(
                S.RUN,
                kind=S.KIND_AGENT,
                inputs=question,
                **{S.ATTR_THREAD_ID: session_dir.name},
            ) as run_span:
                self._active = asyncio.create_task(run_lab(
                    self, question, session_dir, journal, state if resume else None,
                    on_event=on_event, resume=resume,
                ))
                try:
                    result = await self._active
                except asyncio.CancelledError:
                    # await 会将调用者取消传递给子协程；外部取消继续传播，
                    # request_stop 只取消子协程，转换成明确的暂停结果。
                    external = bool(asyncio.current_task().cancelling())
                    reason = "cancelled" if external else "user_stop"
                    journal.append("run_paused", {"reason": reason})
                    if external:
                        raise
                    return {"verdict": "paused", "reason": reason,
                            "summary": "", "question": question}
                except RunPaused as exc:
                    journal.append("run_paused", {"reason": str(exc)})
                    return {"verdict": "paused", "reason": str(exc),
                            "summary": "", "question": question}
                except Exception as exc:
                    journal.append("run_paused", {"reason": f"{type(exc).__name__}: {exc}"})
                    raise
                # run_lab 只有成功写出 Summary 才正常收尾；不检查业务 verdict。
                journal.append("session_closed", {"result": result})
                run_span.set(**{S.ATTR_OUTCOME: result.get("verdict")})
                run_span.output(result.get("summary", "")[:2000])
                return result
        finally:
            self._active = None
            self.tracer.flush()
