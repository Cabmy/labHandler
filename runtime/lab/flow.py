"""一次 lab 的阶段循环：Remember → SPEC → 逐步派发/Judge → Summary。

LabRunner 只负责入口、取消和 AgentSpec。本模块拿一份 LabState 往下走，
避免 runner.py 再堆成一条 400 行过程。
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.profile import load_profile
from runtime.context.notes import append_forget, apply_memory
from runtime.lab.accept import AcceptResult, FAIL, NO_HARD_CRITERIA, TEST_INVALID, missing_gate
from runtime.lab.helpers import (
    halt_of,
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
from runtime.loop import drop_dangling_tool_calls, run_loop
from runtime.loop.schema import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
)
from runtime.observe import spans as S
from runtime.phase import phase_of
from runtime.task import RuntimeTask, TaskKind, TaskStatus, TaskTree
from tools.sandbox_tools import reset_sandbox_failure_counter
from tools.skill_tool import SkillBind

EventSink = Callable[[dict[str, Any]], Awaitable[None]]

_BRIEFS_CAP = 12000
_MAX_STEPS = 12
_MAX_SPEC_REVISIONS = 2
# 同一个门禁跑这么多次仍是 test_invalid 就判定跑不起来：再重写不会改变结论，
# 只会让每一步空转。达到上限后不再拦 finish，SUMMARY 如实说门禁没跑。
_MAX_GATE_RUNS = 3


@dataclass
class LabState:
    """一次 run 的可变状态。阶段函数只改这里，不往 runner 回塞局部变量。"""

    runner: Any
    question: str
    session_dir: Path
    tree: TaskTree
    on_event: EventSink | None
    resume: bool
    root: RuntimeTask
    ledger: EffectLedger
    catalog: str
    progress: list[tuple[str, str]] = field(default_factory=list)
    executed_ids: set[str] = field(default_factory=set)
    resumable: set[str] = field(default_factory=set)
    gates: dict[str, str] = field(default_factory=dict)
    gate_tries: dict[str, int] = field(default_factory=dict)
    broken_gates: set[str] = field(default_factory=set)
    pro_history: list[dict[str, Any]] = field(default_factory=list)
    halt: dict[str, Any] | None = None
    run_gate: AcceptResult = field(
        default_factory=lambda: AcceptResult(state=NO_HARD_CRITERIA)
    )
    spec_user: str = ""

    def record_gate(self, gate_id: str, state: str) -> None:
        """记下某个 assignment 的门禁结论；test_invalid 累计到上限就标为跑不起来。"""
        self.gates[gate_id] = state
        if state != TEST_INVALID:
            self.gate_tries.pop(gate_id, None)
            self.broken_gates.discard(gate_id)
            return
        tries = self.gate_tries.get(gate_id, 0) + 1
        self.gate_tries[gate_id] = tries
        if tries >= _MAX_GATE_RUNS:
            self.broken_gates.add(gate_id)

    def gate_unrunnable(self) -> bool:
        """所有 test_invalid 的门禁都已判定跑不起来：不该再拦 finish。"""
        stuck = {gid for gid, state in self.gates.items() if state == TEST_INVALID}
        return bool(stuck) and stuck <= self.broken_gates

    async def emit(self, payload: dict[str, Any]) -> None:
        await self.runner._emit(self.on_event, payload)

    async def emit_halt(self, halt: dict[str, Any]) -> None:
        await self.runner._emit_halt(self.on_event, halt)

    def take_halt(self, submit: dict[str, Any] | None) -> dict[str, Any] | None:
        found = halt_of(submit)
        if found:
            self.halt = found
        return found

    def remember_block(self) -> str:
        rules = self.runner._applied_rules or []
        if not rules:
            return "（本 lab 没有适用的 /remember 规则）"
        return "\n".join(f"- {r}" for r in rules)

    async def drive_pro(self, kind: TaskKind, user: str, label: str) -> dict[str, Any]:
        """跑一拍 Pro：并入同一条 transcript，submit 写回 Task 树。"""
        runner = self.runner
        phase = phase_of(kind)
        permission = phase.node_permission()
        carry = phase.shares_thread
        incoming = self.pro_history + [{"role": "user", "content": user}] if carry else []
        child = self.tree.add_child(
            self.root.task_id,
            kind=kind,
            permission=permission,
            step_budget=runner.settings.pro_step_budget,
            deadline=runner.clock() + runner.settings.task_wall_time_s,
        )
        child.transit(TaskStatus.RUNNING)

        span_cm = (
            runner.tracer.span(
                S.TASK,
                kind=S.KIND_AGENT,
                **{
                    S.ATTR_TASK_ID: child.task_id,
                    S.ATTR_TASK_KIND: kind.value,
                    S.ATTR_AGENT: f"pro:{label}",
                },
            )
            if runner.tracer is not None
            else None
        )
        if span_cm is not None:
            span_cm.__enter__()
        try:
            result = await run_loop(
                child,
                runner._agent_spec(kind, permission),
                settings=runner.settings,
                llm=runner.llm,
                registry=runner.registry,
                session_dir=self.session_dir,
                user_input="" if carry else user,
                project_spec=read_text(self.session_dir, SPEC_FILE),
                history=incoming,
                on_event=self.on_event,
                tracer=runner.tracer,
                clock=runner.clock,
            )
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

        if carry:
            # 存回去的 transcript 必须配平：末尾若留着没有 tool 响应的
            # assistant，下一阶段追加的指令会被 drop_dangling_tool_calls 一并截掉。
            kept = result.history if phase.carry_prose else _strip_prose(result.history)
            self.pro_history[:] = drop_dangling_tool_calls(kept)

        if result.submit:
            child.transit(TaskStatus.COMPLETED)
            child.brief = result.submit
        else:
            try:
                child.transit(TaskStatus.FAILED)
            except ValueError:
                pass
        save_tree(self.session_dir, self.tree)
        return result.submit or {}


def _strip_prose(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉没有 tool_calls 的 assistant 轮。没有 tool 响应依赖它，删了不会破配对。"""
    return [
        m
        for m in history
        if not (m.get("role") == "assistant" and not m.get("tool_calls"))
    ]


async def run_lab(
    runner: Any,
    question: str,
    session_dir: Path,
    tree: TaskTree,
    *,
    on_event: EventSink | None,
    resume: bool,
) -> dict[str, Any]:
    st = await _begin(runner, question, session_dir, tree, on_event, resume)
    await remember(st)
    failed = await write_spec(st)
    if failed:
        return failed
    cancelled = await advance(st)
    if cancelled:
        return cancelled
    return await summarize(st)


async def _begin(
    runner: Any,
    question: str,
    session_dir: Path,
    tree: TaskTree,
    on_event: EventSink | None,
    resume: bool,
) -> LabState:
    runner._applied_rules = None
    runner._skill = SkillBind()
    root = tree.get(tree.root_id)
    if root.status is TaskStatus.PENDING:
        root.transit(TaskStatus.RUNNING)
    save_tree(session_dir, tree)

    reset_sandbox_failure_counter()
    ledger = EffectLedger.load(session_dir, runner.settings.workspace_dir)
    catalog = read_text(session_dir, CATALOG_FILE) or await ingest(runner.settings, session_dir)
    return LabState(
        runner=runner,
        question=question,
        session_dir=session_dir,
        tree=tree,
        on_event=on_event,
        resume=resume,
        root=root,
        ledger=ledger,
        catalog=catalog,
    )


async def remember(st: LabState) -> None:
    catalog = catalog_rules(load_profile())
    if st.resume:
        existing = load_applied(st.session_dir)
        if existing is not None:
            st.runner._applied_rules = existing
            return
    if not catalog:
        st.runner._applied_rules = []
        save_applied(st.session_dir, [])
        return
    await st.emit({"kind": "node_start", "node": "remember_judge"})
    listed = "\n".join(f"- {rule}" for rule in catalog)
    submit = await st.drive_pro(
        TaskKind.REMEMBER_JUDGE,
        (
            f"User request:\n{st.question}\n\n{st.catalog}\n\n"
            f"## /remember rules\n{listed}\n\n"
            "Read the listed files if you need to know what this homework will hand in "
            "(code vs 实验报告 vs essay). Do not write code or 实验报告 here; do not grep for "
            "the rule text. labHandler calling this run a lab does not mean 实验报告. "
            "Default each /remember rule to applies=false. "
            "Then call submit_remember. Do not write SPEC.md."
        ),
        "remember",
    )
    found = st.take_halt(submit)
    if found:
        st.runner._applied_rules = []
        save_applied(st.session_dir, [])
        await st.emit(
            {"kind": "node_done", "node": "remember_judge", "log": [{"halted": True}]}
        )
        await st.emit_halt(found)
        return
    st.runner._applied_rules = applied_from_payload(
        catalog, payload_of(submit, SUBMIT_REMEMBER)
    )
    save_applied(st.session_dir, st.runner._applied_rules)
    await st.emit(
        {
            "kind": "node_done",
            "node": "remember_judge",
            "log": [{"applicable": len(st.runner._applied_rules)}],
        }
    )


async def write_spec(st: LabState) -> dict[str, Any] | None:
    """写出 SPEC.md。失败返回 spec_failed；halt 则留下 st.halt，继续走 Summary。"""
    st.spec_user = (
        f"User request:\n{st.question}\n\n{st.catalog}\n"
        "Remember-Judge already ran in this conversation. Call submit_spec now; do not ask "
        "the user. Files not in the catalog do not exist yet — they are Flash deliverables, "
        "not a reason to stall. FileNotFound is expected. "
        "milestones = a few meaningful Flash product units (a function, a problem, a file), "
        "not a checklist of reading/design/tests/polish. One function homework = one milestone. "
        "Put reading notes in overview; put how you will write the gate in "
        "acceptance_strategy — those are not milestones. "
        "Encode only applicable /remember rules from the profile. "
        "applies=false omits that rule; it does not skip SPEC and does not make the homework a report. "
        "submit_halt only if a required fact is missing and only the user can supply it."
    )
    if st.halt:
        return None

    project = resume_spec(st.tree) if st.resume else None
    if project is not None:
        await st.emit({"kind": "node_done", "node": "spec", "log": [{"resumed": True}]})
        write_text(st.session_dir, SPEC_FILE, project.render())
        await _mark_resumable(st)
        return None

    await st.emit({"kind": "node_start", "node": "spec"})
    submit = await st.drive_pro(TaskKind.SPEC, st.spec_user, "spec")
    found = st.take_halt(submit)
    if found:
        await st.emit_halt(found)
        return None

    project = ProjectSpec.from_payload(payload_of(submit, SUBMIT_SPEC))
    if not project.goal:
        await st.emit({"kind": "error", "detail": "Pro 未能产出 SPEC.md，任务终止"})
        try:
            st.root.transit(TaskStatus.FAILED)
        except ValueError:
            pass
        save_tree(st.session_dir, st.tree)
        return {
            "verdict": "spec_failed",
            "summary": "",
            "knowledge_cards": [],
            "question": st.question,
        }
    await st.emit(
        {"kind": "node_done", "node": "spec", "log": [{"milestones": len(project.milestones)}]}
    )
    write_text(st.session_dir, SPEC_FILE, project.render())
    await _mark_resumable(st)
    return None


async def _mark_resumable(st: LabState) -> None:
    if not st.resume or st.halt:
        return
    st.resumable = {aid for aid in st.ledger.entries if st.ledger.satisfied(aid)}
    for aid in sorted(st.resumable):
        st.progress.append((aid, "（续跑：产物已存在且未被改动，跳过重跑）"))
    if st.resumable:
        await st.emit({"kind": "resume_skip", "assignments": sorted(st.resumable)})


async def advance(st: LabState) -> dict[str, Any] | None:
    """逐步派发。cancelled 提前返回；halt / 收工则落到 Summary。"""
    if st.halt:
        return None

    step = 0
    revisions = 0
    gate_nudge = ""
    while step < _MAX_STEPS and not st.halt:
        if st.runner._cancelled():
            st.root.transit(TaskStatus.CANCELLED)
            save_tree(st.session_dir, st.tree)
            return {"verdict": "cancelled", "summary": "", "question": st.question}

        step += 1
        signal, gate_nudge, revisions = await _one_step(st, step, gate_nudge, revisions)
        if signal == "break":
            break
    return None


async def _one_step(
    st: LabState, step: int, gate_nudge: str, revisions: int
) -> tuple[str, str, int]:
    """跑一步：dispatch → workers → judge。返回 (break|continue, 下一步 nudge, revisions)。"""
    dispatch_user = (
        f"User request: {st.question}\n\n"
        f"## SPEC.md\n{read_text(st.session_dir, SPEC_FILE)}\n\n"
        f"## 已完成的步骤\n{render_progress(st.progress)}\n\n"
        f"这是第 {step} 步（最多 {_MAX_STEPS} 步）。决定接下来这一步做什么，调用 submit_dispatch。"
        "只派这一步给 Flash 的活；Flash 侧全部做完时给空的 assignments。"
    )
    if gate_nudge:
        dispatch_user += f"\n\n## Harness rejected last dispatch\n{gate_nudge}"
        gate_nudge = ""
    if st.run_gate.state == TEST_INVALID and st.gate_unrunnable():
        dispatch_user += (
            "\n\n## Gate cannot run in this sandbox\n"
            f"The gate stayed test_invalid across {_MAX_GATE_RUNS} runs, so it is an "
            "environment problem, not a content problem. Stop rewriting write_acceptance and "
            "stop re-dispatching the same product. If the product files exist, submit_dispatch "
            "with empty assignments; SUMMARY will report that the gate never ran."
        )
    elif st.run_gate.state == TEST_INVALID:
        dispatch_user += (
            "\n\n## Gate is test_invalid\n"
            "Your pytest gate could not be collected or crashed. "
            "Do not start another Flash for that product. "
            "write_acceptance(task_id=<original assignment id>, filename=\"test_foo.py\", "
            "content=<one Python string, not an array>) then submit_dispatch with the SAME id. "
            "Harness re-runs the gate only. Rewriting identical content is rejected."
        )
    elif st.run_gate.state == FAIL:
        dispatch_user += (
            "\n\n## Gate is fail\n"
            "Re-dispatch the same product assignment so Flash can fix the implementation. "
            "Do not add a Flash whose job is tests. Rewrite write_acceptance only if the tests were wrong."
        )
    await st.emit({"kind": "node_start", "node": f"dispatch:{step}"})
    submit = await st.drive_pro(TaskKind.DISPATCH, dispatch_user, f"dispatch{step}")
    found = st.take_halt(submit)
    if found:
        await st.emit_halt(found)
        return "break", gate_nudge, revisions
    if not submit or submit.get("name") != SUBMIT_DISPATCH:
        await st.emit(
            {
                "kind": "error",
                "detail": "Pro 未能产出有效派发（校验失败或步数耗尽），未启动 worker",
            }
        )
        return "break", gate_nudge, revisions
    dispatch = Dispatch.from_payload(payload_of(submit, SUBMIT_DISPATCH))

    if dispatch.is_empty:
        if st.run_gate.state in {FAIL, TEST_INVALID} and not st.gate_unrunnable():
            msg = (
                f"Gate is still {st.run_gate.state}; empty assignments would skip to SUMMARY "
                "with a broken gate. write_acceptance to fix test_invalid, or re-dispatch the "
                "same product id to fix fail."
            )
            await st.emit(
                {"kind": "node_done", "node": f"dispatch:{step}", "log": [{"rejected": msg[:400]}]}
            )
            return "continue", msg, revisions
        await st.emit(
            {"kind": "node_done", "node": f"dispatch:{step}", "log": [{"assignments": 0}]}
        )
        return "break", gate_nudge, revisions

    gap = missing_gate(st.session_dir, dispatch.assignments)
    if gap:
        await st.emit(
            {"kind": "node_done", "node": f"dispatch:{step}", "log": [{"rejected": gap[:400]}]}
        )
        return "continue", gap, revisions

    assignments, skipped, replay = partition(
        dispatch.assignments,
        st.resumable,
        st.executed_ids,
        step,
        st.gates,
        st.broken_gates,
    )
    if skipped:
        await st.emit({"kind": "resume_skip", "assignments": skipped})
    await st.emit(
        {
            "kind": "node_done",
            "node": f"dispatch:{step}",
            "log": [
                {
                    "step_goal": dispatch.step_goal,
                    "assignments": len(assignments),
                    "skipped": len(skipped),
                    "replay_gate": len(replay),
                }
            ],
        }
    )
    if not assignments and not replay:
        if skipped and st.gate_unrunnable():
            return (
                "continue",
                f"assignment id(s) {skipped} already ran and their gate cannot run in this "
                "sandbox. There is no more Flash work: submit_dispatch with empty assignments.",
                revisions,
            )
        if skipped:
            return (
                "continue",
                f"assignment id(s) {skipped} already ran this lab (gate={st.run_gate.state}). "
                "If the product is done, submit_dispatch with empty assignments. "
                "If gate is test_invalid, write_acceptance then resubmit the same id (no new Flash). "
                "If gate is fail, resubmit the same id to re-run Flash.",
                revisions,
            )
        return "continue", gate_nudge, revisions

    briefs: list[dict[str, Any]] = []
    spec_invalid = False
    last_gate = AcceptResult(state=NO_HARD_CRITERIA)
    if assignments:
        briefs, spec_invalid, last_gate, step_halt = await run_step(
            st.runner,
            assignments,
            question=st.question,
            step_goal=dispatch.step_goal,
            session_dir=st.session_dir,
            tree=st.tree,
            root=st.root,
            ledger=st.ledger,
            progress=st.progress,
            on_event=st.on_event,
        )
        if step_halt:
            st.halt = step_halt
            return "break", gate_nudge, revisions
        for a, brief in zip(assignments, briefs):
            state = str((brief.get("tests") or {}).get("state") or "")
            if state:
                st.record_gate(a.gate_id, state)
    if replay:
        extra, extra_gate = await _replay_gates(st, replay)
        briefs = briefs + extra
        last_gate = worst_gate(last_gate, extra_gate)
    st.run_gate = last_gate

    decision = await _judge(st, dispatch, briefs, last_gate)
    if st.halt:
        return "break", gate_nudge, revisions
    if decision == "finish":
        return "break", gate_nudge, revisions
    if decision == "takeover":
        await _takeover(st, dispatch, briefs, assignments or replay, step)
        return "continue", gate_nudge, revisions
    if decision == "revise_spec" or spec_invalid:
        revisions = await _revise_spec(st, briefs, revisions)
        if st.halt or revisions < 0:
            return "break", gate_nudge, max(revisions, 0)
    return "continue", gate_nudge, revisions


async def _replay_gates(
    st: LabState, replay: list
) -> tuple[list[dict[str, Any]], AcceptResult]:
    """同一产品本 run 已派过 Flash，且门禁是 test_invalid：只重跑 pytest。"""
    last = AcceptResult(state=NO_HARD_CRITERIA)
    briefs: list[dict[str, Any]] = []
    for a in replay:
        gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
        last = worst_gate(last, gate)
        st.record_gate(a.gate_id, gate.state)
        brief = {
            "assignment_id": a.id,
            "outcome": "done",
            "brief": (
                "No new Flash: this assignment already ran this lab. "
                "Harness re-ran the gate after your latest write_acceptance."
            ),
            "changed_files": [],
            "tests": gate.as_tests(),
        }
        briefs.append(brief)
        st.progress.append((a.id, str(brief["brief"])))
        await st.emit(
            {
                "kind": "worker_brief",
                "assignment_id": a.id,
                "outcome": "replay_gate",
                "brief": brief["brief"],
                "tests": brief["tests"],
                "gate": gate.state,
            }
        )
    return briefs, last


def _summary_gate(st: LabState) -> AcceptResult:
    """整次 lab 的门禁：各 assignment 最差态。没有记录则用最近一步。"""
    if not st.gates:
        return st.run_gate
    acc = AcceptResult(state=NO_HARD_CRITERIA)
    for state in st.gates.values():
        acc = worst_gate(acc, AcceptResult(state=state))
    return acc


async def _judge(
    st: LabState,
    dispatch: Dispatch,
    briefs: list[dict[str, Any]],
    last_gate: AcceptResult,
) -> str:
    judge_user = (
        f"User request: {st.question}\nStep goal: {dispatch.step_goal}\n"
        f"Gate: {last_gate.state}\n"
        f"Briefs:\n{json.dumps(briefs, ensure_ascii=False)[:_BRIEFS_CAP]}\n"
        f"## Applicable /remember rules\n{st.remember_block()}\n"
        "If the gate is no_hard_criteria you MUST say so in evidence and give a semantic rationale. "
        "Fill rule_verdicts for every applicable /remember rule. finish only when all are satisfied."
    )
    if last_gate.state == TEST_INVALID and st.gate_unrunnable():
        judge_user += (
            f"\n\nGate stayed test_invalid across {_MAX_GATE_RUNS} runs: the sandbox cannot run "
            "it. Do NOT rewrite write_acceptance again (identical content is rejected) and do NOT "
            "dispatch Flash again. Judge the product semantically from the briefs and files; "
            "finish if it satisfies SPEC, and say in evidence that the gate never executed."
        )
        if last_gate.log:
            judge_user += f"\npytest log:\n{last_gate.log[-1500:]}"
    elif last_gate.state == TEST_INVALID:
        judge_user += (
            "\n\nGate test_invalid: rewrite write_acceptance this turn "
            "(task_id=original assignment id, filename=test_xxx.py, content=one string). "
            "Rewriting byte-identical content is rejected — change it or leave it alone. "
            "Then finish if all Flash work is done; else continue. Do not punish Flash. "
            "Do not dispatch Flash again for this product."
        )
        if last_gate.log:
            judge_user += f"\npytest log:\n{last_gate.log[-1500:]}"
    await st.emit({"kind": "node_start", "node": "judge"})
    judge_submit = await st.drive_pro(TaskKind.JUDGE, judge_user, "judge")
    found = st.take_halt(judge_submit)
    if found:
        await st.emit_halt(found)
        return "halt"
    verdict = payload_of(judge_submit, SUBMIT_JUDGE)
    decision = str(verdict.get("decision") or "continue")
    if last_gate.state == TEST_INVALID and dispatch.assignments and not st.gate_unrunnable():
        refreshed = AcceptResult(state=NO_HARD_CRITERIA)
        for a in dispatch.assignments:
            gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
            st.record_gate(a.gate_id, gate.state)
            refreshed = worst_gate(refreshed, gate)
        last_gate = refreshed
        st.run_gate = last_gate
    if decision == "finish" and not rules_satisfied(st.runner._applied_rules or [], verdict):
        decision = "continue"
    if decision == "finish" and (
        last_gate.state == FAIL
        or (last_gate.state == TEST_INVALID and not st.gate_unrunnable())
    ):
        decision = "continue"
    if decision == "takeover" and last_gate.state == TEST_INVALID:
        decision = "continue"

    apply_memory(
        st.session_dir,
        replace=verdict.get("memory_replace"),
        remove=verdict.get("memory_remove"),
        append=str(verdict.get("memory_append") or ""),
    )
    append_forget(st.session_dir, str(verdict.get("forget_append") or ""))

    st.runner._trace_event(
        S.EV_DECISION,
        **{
            S.ATTR_DECISION: decision,
            S.ATTR_GATE_STATE: last_gate.state,
            S.ATTR_REASON: str(verdict.get("evidence") or "")[:500],
        },
    )
    await st.emit({"kind": "node_done", "node": "judge", "log": [{"decision": decision}]})
    return decision


async def _takeover(
    st: LabState,
    dispatch: Dispatch,
    briefs: list[dict[str, Any]],
    assignments: list,
    step: int,
) -> None:
    st.runner._trace_event(S.EV_HANDOFF, **{S.ATTR_FROM: "flash", S.ATTR_TO: "pro"})
    await st.emit({"kind": "pro_takeover", "reason": "judge"})
    take = await st.drive_pro(
        TaskKind.TAKEOVER,
        f"Take over this step yourself.\nUser: {st.question}\n"
        f"Step goal: {dispatch.step_goal}\n"
        f"Briefs: {json.dumps(briefs, ensure_ascii=False)[:8000]}",
        "takeover",
    )
    found = st.take_halt(take)
    if found:
        await st.emit_halt(found)
        return
    payload = payload_of(take, SUBMIT_BRIEF)
    if payload.get("brief"):
        st.progress.append((f"step{step}-pro", str(payload["brief"])))
        for a in assignments:
            last_gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
            if not last_gate.is_pass:
                break
    else:
        st.progress.append((f"step{step}-pro", "Pro 接管未完成本步，该工作仍然待办"))


async def _revise_spec(
    st: LabState, briefs: list[dict[str, Any]], revisions: int
) -> int:
    """成功改 SPEC 返回新 revisions；预算耗尽返回 -1；halt 时 st.halt 已设。"""
    if revisions >= _MAX_SPEC_REVISIONS:
        st.runner._trace_event(
            S.EV_DECISION,
            **{S.ATTR_DECISION: "stop", S.ATTR_REASON: "spec_revision_budget"},
        )
        await st.emit({"kind": "spec_revision_exhausted", "limit": _MAX_SPEC_REVISIONS})
        return -1
    revisions += 1
    redo = await st.drive_pro(
        TaskKind.SPEC,
        st.spec_user
        + "\n\nThe current SPEC.md was rejected. Revise it given these briefs:\n"
        + json.dumps(briefs, ensure_ascii=False)[:8000],
        "revise_spec",
    )
    found = st.take_halt(redo)
    if found:
        await st.emit_halt(found)
        return revisions
    project = ProjectSpec.from_payload(payload_of(redo, SUBMIT_SPEC))
    write_text(st.session_dir, SPEC_FILE, project.render())
    return revisions


async def summarize(st: LabState) -> dict[str, Any]:
    if st.halt:
        summary_user = (
            "The lab halted. A required fact is missing and only the user can provide it. "
            "Do not invent completed homework.\n\n"
            f"User request: {st.question}\n\n"
            f"## Why it halted\n{st.halt.get('reason')}\n\n"
            f"## Need from the user\n{st.halt.get('need_from_user')}\n\n"
            f"## SPEC.md so far\n{read_text(st.session_dir, SPEC_FILE) or '（尚未写出 SPEC.md）'}\n\n"
            f"## 完成情况\n{render_progress(st.progress)}\n\n"
            "Call submit_summary. user_summary must explain the situation and what the user "
            "should put in the workspace, in the user's language."
        )
    else:
        summary_user = (
            f"The lab is complete. You are still Pro — write the wrap-up from this conversation "
            f"and the artifacts below.\n\n"
            f"User request: {st.question}\n\n## SPEC.md\n{read_text(st.session_dir, SPEC_FILE)}\n\n"
            f"## 完成情况\n{render_progress(st.progress)}\n\n"
            f"Gate: {_summary_gate(st).state}\n"
            "Call submit_summary. If the gate was no_hard_criteria, say so in user_summary."
        )
    await st.emit({"kind": "node_start", "node": "summary"})
    payload = payload_of(
        await st.drive_pro(TaskKind.SUMMARY, summary_user, "summary"),
        SUBMIT_SUMMARY,
    )
    summary_text = str(payload.get("user_summary") or "")
    if summary_text:
        st.runner.settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        (st.runner.settings.workspace_dir / "SUMMARY.md").write_text(
            summary_text, encoding="utf-8"
        )
    await st.emit({"kind": "node_done", "node": "summary", "log": []})

    try:
        st.root.transit(TaskStatus.COMPLETED)
    except ValueError:
        pass
    save_tree(st.session_dir, st.tree)
    return {
        "verdict": "need_user" if st.halt else _summary_gate(st).state,
        "summary": summary_text,
        "knowledge_cards": list(payload.get("knowledge_cards") or []),
        "question": st.question,
    }
