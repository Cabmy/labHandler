"""一次 lab：SPEC → Remember-Judge → 逐步派发 → Judge → Summary。

Pro 全链路共用一份 history。Flash 每次 assignment 新开 loop。
派发粒度固定在「一个 Flash 一步能做完」。空 assignments 或 judge=finish 结束推进。
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from memory.profile import inject_for_agent, load_profile
from runtime.context.disclosure import skill_catalog_block
from runtime.context.notes import append_forget, apply_memory
from runtime.lab.accept import AcceptResult, NO_HARD_CRITERIA
from runtime.lab.helpers import (
    partition,
    payload_of,
    render_progress,
    resume_spec,
    worst_gate,
)
from runtime.lab.ingest import ingest
from runtime.lab.persist import (
    EffectLedger,
    CATALOG_FILE,
    SPEC_FILE,
    read_text,
    save_tree,
    write_text,
)
from runtime.lab.remember import (
    applied_from_payload,
    catalog_rules,
    load_applied,
    rules_satisfied,
    save_applied,
)
from runtime.lab.spec import Dispatch, ProjectSpec
from runtime.lab.step import run_gate as evaluate_gate, run_step
from runtime.llm import LLMGateway
from runtime.loop import AgentSpec, drop_dangling_tool_calls, run_loop
from runtime.loop.schema import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
)
from runtime.loop.tools import ToolRegistry, build_registry
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.phase import PRO, phase_of
from runtime.task import Permission, RuntimeTask, TaskKind, TaskStatus, TaskTree
from tools.sandbox_tools import reset_sandbox_failure_counter
from tools.skill_tool import SkillBind

EventSink = Callable[[dict[str, Any]], Awaitable[None]]

_BRIEFS_CAP = 12000
_MAX_STEPS = 12
_MAX_SPEC_REVISIONS = 2


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
            phase.system,
            rules=self._applied_rules or [],
            include_rules=include_rules,
        )
        if phase.agent == PRO and phase.shares_thread:
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
            tool_extras={"skill": self._skill} if phase.agent == PRO else {},
        )

    # ── 主流程 ──────────────────────────────────────────

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
            result = await self._run_inner(
                question, session_dir, tree, on_event=on_event, resume=resume
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

    async def _run_inner(
        self,
        question: str,
        session_dir: Path,
        tree: TaskTree,
        *,
        on_event: EventSink | None,
        resume: bool,
    ) -> dict[str, Any]:
        self._applied_rules = None
        self._skill = SkillBind()
        root = tree.get(tree.root_id)
        if root.status is TaskStatus.PENDING:
            root.transit(TaskStatus.RUNNING)
        save_tree(session_dir, tree)

        reset_sandbox_failure_counter()
        ledger = EffectLedger.load(session_dir, self.settings.workspace_dir)
        catalog = read_text(session_dir, CATALOG_FILE) or await ingest(self.settings, session_dir)

        last_gate = AcceptResult(state=NO_HARD_CRITERIA)
        # 整个 run 的结论取各步里最差的那个：中间失败的一步不能被后面一步洗白
        run_gate = AcceptResult(state=NO_HARD_CRITERIA)
        # 已完成的步骤：assignment id -> brief 摘要。进入后续 dispatch / summary 的 user 上下文。
        progress: list[tuple[str, str]] = []
        # 本次 run 内真正跑过的 id，用于避免验收目录串号
        executed_ids: set[str] = set()
        # 续跑时产物仍完好的 id：Pro 再派到它就直接跳过
        resumable: set[str] = set()
        # Pro 全链路共用（SPEC / remember_judge / dispatch / judge / 接管 / summary）。
        # Flash 不走这份。
        pro_history: list[dict[str, Any]] = []

        async def run_pro(kind: TaskKind, user: str, label: str) -> dict[str, Any]:
            phase = phase_of(kind)
            permission = phase.node_permission()
            # 并线阶段的指令直接进 transcript，排在既有往来之后；user 槽留空。
            carry = phase.shares_thread
            incoming = pro_history + [{"role": "user", "content": user}] if carry else []
            child = tree.add_child(
                root.task_id,
                kind=kind,
                permission=permission,
                step_budget=self.settings.pro_step_budget,
                deadline=self.clock() + self.settings.task_wall_time_s,
            )
            child.transit(TaskStatus.RUNNING)

            span_cm = (
                self.tracer.span(
                    S.TASK,
                    kind=S.KIND_AGENT,
                    **{
                        S.ATTR_TASK_ID: child.task_id,
                        S.ATTR_TASK_KIND: kind.value,
                        S.ATTR_AGENT: f"pro:{label}",
                    },
                )
                if self.tracer is not None
                else None
            )
            if span_cm is not None:
                span_cm.__enter__()
            try:
                result = await run_loop(
                    child,
                    self._agent_spec(kind, permission),
                    settings=self.settings,
                    llm=self.llm,
                    registry=self.registry,
                    session_dir=session_dir,
                    user_input="" if carry else user,
                    project_spec=read_text(session_dir, SPEC_FILE),
                    history=incoming,
                    on_event=on_event,
                    tracer=self.tracer,
                    clock=self.clock,
                )
            finally:
                if span_cm is not None:
                    span_cm.__exit__(None, None, None)

            if carry:
                # 存回去的 transcript 必须配平：末尾若留着没有 tool 响应的
                # assistant，下一阶段追加的指令会被 drop_dangling_tool_calls 一并截掉。
                pro_history[:] = drop_dangling_tool_calls(result.history)

            if result.submit:
                child.transit(TaskStatus.COMPLETED)
                child.brief = result.submit
            else:
                try:
                    child.transit(TaskStatus.FAILED)
                except ValueError:
                    pass
            save_tree(session_dir, tree)
            return result.submit or {}

        # ── SPEC.md ─────────────────────────────────────
        spec_user = (
            f"User request:\n{question}\n\n{catalog}\n"
            "Use read_file on the listed paths. Write SPEC.md and call submit_spec. "
            "Decompose the work top-down into milestones that a single weak worker can each finish in one assignment."
        )

        project = resume_spec(tree) if resume else None
        if project is not None:
            await self._emit(on_event, {"kind": "node_done", "node": "spec", "log": [{"resumed": True}]})
        else:
            await self._emit(on_event, {"kind": "node_start", "node": "spec"})
            submit = await run_pro(TaskKind.SPEC, spec_user, "spec")
            project = ProjectSpec.from_payload(payload_of(submit, SUBMIT_SPEC))
            if not project.goal:
                # 没有总纲就没有派发依据，继续往下只会让 worker 对着空规格干活
                await self._emit(
                    on_event, {"kind": "error", "detail": "Pro 未能产出 SPEC.md，任务终止"}
                )
                try:
                    root.transit(TaskStatus.FAILED)
                except ValueError:
                    pass
                save_tree(session_dir, tree)
                return {
                    "verdict": "spec_failed",
                    "summary": "",
                    "knowledge_cards": [],
                    "question": question,
                }
            await self._emit(
                on_event,
                {"kind": "node_done", "node": "spec", "log": [{"milestones": len(project.milestones)}]},
            )
        write_text(session_dir, SPEC_FILE, project.render())
        await self._scope_remember(
            question, session_dir, run_pro, on_event, resume=resume
        )

        if resume:
            resumable = {aid for aid in ledger.entries if ledger.satisfied(aid)}
            for aid in sorted(resumable):
                progress.append((aid, "（续跑：产物已存在且未被改动，跳过重跑）"))
            if resumable:
                await self._emit(
                    on_event, {"kind": "resume_skip", "assignments": sorted(resumable)}
                )

        # ── 逐步推进 ────────────────────────────────────
        step = 0
        revisions = 0
        while step < _MAX_STEPS:
            if self._cancelled():
                root.transit(TaskStatus.CANCELLED)
                save_tree(session_dir, tree)
                return {"verdict": "cancelled", "summary": "", "question": question}

            step += 1
            dispatch_user = (
                f"User request: {question}\n\n"
                f"## SPEC.md\n{read_text(session_dir, SPEC_FILE)}\n\n"
                f"## 已完成的步骤\n{render_progress(progress)}\n\n"
                f"这是第 {step} 步（最多 {_MAX_STEPS} 步）。决定接下来这一步做什么，调用 submit_dispatch。"
                "只派这一步的活；全部做完时给空的 assignments。"
            )
            await self._emit(on_event, {"kind": "node_start", "node": f"dispatch:{step}"})
            submit = await run_pro(TaskKind.DISPATCH, dispatch_user, f"dispatch{step}")
            if not submit or submit.get("name") != SUBMIT_DISPATCH:
                # 预算耗尽 / 校验失败时 submit 为空。不能当成「空 assignments = 做完了」。
                await self._emit(
                    on_event,
                    {
                        "kind": "error",
                        "detail": "Pro 未能产出有效派发（校验失败或步数耗尽），未启动 worker",
                    },
                )
                break
            dispatch = Dispatch.from_payload(payload_of(submit, SUBMIT_DISPATCH))

            if dispatch.is_empty:
                await self._emit(
                    on_event,
                    {"kind": "node_done", "node": f"dispatch:{step}", "log": [{"assignments": 0}]},
                )
                break

            assignments, skipped = partition(dispatch.assignments, resumable, executed_ids, step)
            if skipped:
                await self._emit(on_event, {"kind": "resume_skip", "assignments": skipped})
            await self._emit(
                on_event,
                {
                    "kind": "node_done",
                    "node": f"dispatch:{step}",
                    "log": [
                        {
                            "step_goal": dispatch.step_goal,
                            "assignments": len(assignments),
                            "skipped": len(skipped),
                        }
                    ],
                },
            )
            if not assignments:
                # 本步 assignments 全部命中 resumable，progress 已有摘要，不跑 worker
                continue

            briefs, spec_invalid, last_gate = await run_step(
                self,
                assignments,
                question=question,
                step_goal=dispatch.step_goal,
                session_dir=session_dir,
                tree=tree,
                root=root,
                ledger=ledger,
                progress=progress,
                on_event=on_event,
            )
            run_gate = worst_gate(run_gate, last_gate)

            # ── Judge ───────────────────────────────────
            judge_user = (
                f"User request: {question}\nStep goal: {dispatch.step_goal}\n"
                f"Gate: {last_gate.state}\n"
                f"Briefs:\n{json.dumps(briefs, ensure_ascii=False)[:_BRIEFS_CAP]}\n"
                f"## Applicable /remember rules\n{self._remember_block()}\n"
                "If the gate is no_hard_criteria you MUST say so in evidence and give a semantic rationale. "
                "Fill rule_verdicts for every applicable /remember rule. finish only when all are satisfied."
            )
            await self._emit(on_event, {"kind": "node_start", "node": "judge"})
            verdict = payload_of(
                await run_pro(TaskKind.JUDGE, judge_user, "judge"),
                SUBMIT_JUDGE,
            )
            decision = str(verdict.get("decision") or "continue")
            if decision == "finish" and not rules_satisfied(self._applied_rules or [], verdict):
                decision = "continue"

            apply_memory(
                session_dir,
                replace=verdict.get("memory_replace"),
                remove=verdict.get("memory_remove"),
                append=str(verdict.get("memory_append") or ""),
            )
            append_forget(session_dir, str(verdict.get("forget_append") or ""))

            self._trace_event(
                S.EV_DECISION,
                **{
                    S.ATTR_DECISION: decision,
                    S.ATTR_GATE_STATE: last_gate.state,
                    S.ATTR_REASON: str(verdict.get("evidence") or "")[:500],
                },
            )
            await self._emit(
                on_event, {"kind": "node_done", "node": "judge", "log": [{"decision": decision}]}
            )

            if decision == "finish":
                break

            if decision == "takeover":
                self._trace_event(S.EV_HANDOFF, **{S.ATTR_FROM: "flash", S.ATTR_TO: "pro"})
                await self._emit(on_event, {"kind": "pro_takeover", "reason": "judge"})
                take = await run_pro(
                    TaskKind.TAKEOVER,
                    f"Take over this step yourself.\nUser: {question}\n"
                    f"Step goal: {dispatch.step_goal}\n"
                    f"Briefs: {json.dumps(briefs, ensure_ascii=False)[:8000]}",
                    "takeover",
                )
                payload = payload_of(take, SUBMIT_BRIEF)
                if payload.get("brief"):
                    progress.append((f"step{step}-pro", str(payload["brief"])))
                    # 接管也写了文件，门禁必须重跑，不能沿用 worker 那次的结论
                    for a in assignments:
                        last_gate = await evaluate_gate(self, session_dir, a.gate_id)
                        if not last_gate.is_pass:
                            break
                else:
                    # 接管未完成（预算耗尽/被判停）：progress 记成待办，
                    # 否则下一步 dispatch 会把它当成已完成。
                    progress.append(
                        (f"step{step}-pro", "Pro 接管未完成本步，该工作仍然待办")
                    )
                continue

            if decision == "revise_spec" or spec_invalid:
                if revisions >= _MAX_SPEC_REVISIONS:
                    self._trace_event(
                        S.EV_DECISION,
                        **{S.ATTR_DECISION: "stop", S.ATTR_REASON: "spec_revision_budget"},
                    )
                    await self._emit(
                        on_event, {"kind": "spec_revision_exhausted", "limit": _MAX_SPEC_REVISIONS}
                    )
                    break
                revisions += 1
                redo = await run_pro(
                    TaskKind.SPEC,
                    spec_user
                    + "\n\nThe current SPEC.md was rejected. Revise it given these briefs:\n"
                    + json.dumps(briefs, ensure_ascii=False)[:8000],
                    "revise_spec",
                )
                project = ProjectSpec.from_payload(payload_of(redo, SUBMIT_SPEC))
                write_text(session_dir, SPEC_FILE, project.render())
                continue

        # ── Summary ─────────────────────────────────────
        summary_user = (
            f"The lab is complete. You are still Pro — write the wrap-up from this conversation "
            f"and the artifacts below.\n\n"
            f"User request: {question}\n\n## SPEC.md\n{read_text(session_dir, SPEC_FILE)}\n\n"
            f"## 完成情况\n{render_progress(progress)}\n\n"
            f"Gate: {run_gate.state}\n"
            "Call submit_summary. If the gate was no_hard_criteria, say so in user_summary."
        )
        await self._emit(on_event, {"kind": "node_start", "node": "summary"})
        payload = payload_of(
            await run_pro(TaskKind.SUMMARY, summary_user, "summary"),
            SUBMIT_SUMMARY,
        )
        summary_text = str(payload.get("user_summary") or "")
        if summary_text:
            self.settings.workspace_dir.mkdir(parents=True, exist_ok=True)
            (self.settings.workspace_dir / "SUMMARY.md").write_text(summary_text, encoding="utf-8")
        await self._emit(on_event, {"kind": "node_done", "node": "summary", "log": []})

        try:
            root.transit(TaskStatus.COMPLETED)
        except ValueError:
            pass
        save_tree(session_dir, tree)
        return {
            "verdict": run_gate.state,
            "summary": summary_text,
            "knowledge_cards": list(payload.get("knowledge_cards") or []),
            "question": question,
        }

    def _remember_block(self) -> str:
        rules = self._applied_rules or []
        if not rules:
            return "（本 lab 没有适用的 /remember 规则）"
        return "\n".join(f"- {r}" for r in rules)

    async def _scope_remember(
        self,
        question: str,
        session_dir: Path,
        run_pro: Callable[..., Awaitable[dict[str, Any]]],
        on_event: EventSink | None,
        *,
        resume: bool,
    ) -> None:
        catalog = catalog_rules(load_profile())
        if resume:
            existing = load_applied(session_dir)
            if existing is not None:
                self._applied_rules = existing
                return
        if not catalog:
            self._applied_rules = []
            save_applied(session_dir, [])
            return
        await self._emit(on_event, {"kind": "node_start", "node": "remember_judge"})
        listed = "\n".join(f"- {rule}" for rule in catalog)
        submit = await run_pro(
            TaskKind.REMEMBER_JUDGE,
            (
                f"User request:\n{question}\n\n## SPEC.md\n{read_text(session_dir, SPEC_FILE)}\n\n"
                f"## /remember rules\n{listed}\n\nCall submit_remember."
            ),
            "remember",
        )
        self._applied_rules = applied_from_payload(catalog, payload_of(submit, SUBMIT_REMEMBER))
        save_applied(session_dir, self._applied_rules)
        await self._emit(
            on_event,
            {
                "kind": "node_done",
                "node": "remember_judge",
                "log": [{"applicable": len(self._applied_rules)}],
            },
        )
