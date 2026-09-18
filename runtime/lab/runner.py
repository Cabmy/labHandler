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
from runtime.observe.tracer import Tracer
from runtime.phase import PRO, phase_of, system_for
from runtime.task import Permission, TaskKind, TaskTree
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
        self.tracer = tracer
        self.clock = clock
        self._cancel = asyncio.Event()
        self._applied_rules: list[str] | None = None
        self._skill = SkillBind()

    def request_stop(self) -> None:
        self._cancel.set()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _arm(self) -> None:
        """每次 run 开始时解除上一次的停止信号，否则停一次之后永远停着。"""
        self._cancel.clear()

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
        if self.tracer is not None:
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
        tree: TaskTree,
        *,
        on_event: EventSink | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        tracer = self.tracer
        span_cm = (
            tracer.span(
                S.RUN,
                kind=S.KIND_AGENT,
                inputs=question,
                **{S.ATTR_THREAD_ID: session_dir.name, S.ATTR_TASK_ID: tree.root_id},
            )
            if tracer is not None
            else None
        )
        run_span = span_cm.__enter__() if span_cm is not None else None
        self._arm()
        try:
            result = await run_lab(
                self, question, session_dir, tree, on_event=on_event, resume=resume
            )
            if run_span is not None:
                run_span.set(**{S.ATTR_OUTCOME: result.get("verdict")})
                run_span.output(result.get("summary", "")[:2000])
            return result
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)
            if tracer is not None:
                tracer.flush()
