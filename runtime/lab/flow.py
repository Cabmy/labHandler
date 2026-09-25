"""一次 lab 的阶段循环：Remember → SPEC → 逐步派发/Judge → Summary。

LabRunner 只负责入口、取消和 AgentSpec。本模块拿一份 LabState 往下走，
避免 runner.py 再堆成一条 400 行过程。
"""

import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.prompts import render_state_update
from memory.profile import load_profile
from memory.retrieve import prefetch_cards, reconcile_index
from runtime.context.notes import (
    append_forget,
    apply_notes,
    load_cards,
    load_notes,
    write_cards,
    write_long_note,
)
from runtime.lab.accept import AcceptResult, FAIL, NO_HARD_CRITERIA, TEST_INVALID, missing_gate
from runtime.lab.effects import EffectLedger, default_probes
from runtime.lab.helpers import (
    halt_of,
    partition,
    payload_of,
    pin_assignment,
    render_progress,
    resume_spec,
    sanitize_decision,
    worst_gate,
)
from runtime.lab.ingest import ingest
from runtime.lab.journal import Journal, JournalState
from runtime.lab.persist import (
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
from runtime.phase import phase_control_message, phase_of
from runtime.task import RuntimeTask, TaskKind, TaskStatus, TaskTree
from tools.sandbox_tools import is_sandbox_unreachable, reset_sandbox_failure_counter
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
    """一次 run 的可变状态，JOURNAL.jsonl 的投影。

    阶段函数只改这里，不往 runner 回塞局部变量。每个可变字段在变更的同时
    写一条 journal 事件，崩溃后 replay 即可重建到断点；树快照（STATE.json）
    只在树结构变化时经 flush_tree 落盘。
    """

    runner: Any
    question: str
    session_dir: Path
    tree: TaskTree
    on_event: EventSink | None
    resume: bool
    root: RuntimeTask
    journal: Journal
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
    sandbox_down: str = ""
    run_gate: AcceptResult = field(
        default_factory=lambda: AcceptResult(state=NO_HARD_CRITERIA)
    )
    dispatch_step: int = 0
    stage: str = "remember"
    resume_announced: bool = False
    # step 内断点标记：replay 自 journal，续跑时决定从哪个子阶段进入。
    step_dispatch: dict[int, dict[str, Any]] = field(default_factory=dict)
    step_briefs: dict[int, dict[str, dict[str, Any]]
                      ] = field(default_factory=dict)
    step_judges: dict[int, str] = field(default_factory=dict)

    def flush_tree(self) -> None:
        save_tree(self.session_dir, self.tree)

    def _journal_stage(self) -> None:
        self.journal.append(
            "stage",
            {
                "stage": self.stage,
                "dispatch_step": self.dispatch_step,
                "skill": getattr(self.runner._skill, "name", None) or "",
            },
        )

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self._journal_stage()

    def set_step(self, step: int) -> None:
        """dispatch_step 只记已完成的步数，advance 每收尾一步调一次。"""
        self.dispatch_step = step
        self._journal_stage()

    def add_progress(self, aid: str, text: str) -> None:
        self.progress.append((aid, text))
        self.journal.append("progress", {"aid": aid, "text": text})

    def set_halt(self, halt: dict[str, Any]) -> None:
        self.halt = halt
        self.journal.append("halt", halt)

    def set_run_gate(self, gate: AcceptResult) -> None:
        self.run_gate = gate
        self.journal.append(
            "run_gate",
            {
                "state": gate.state,
                "passed": gate.passed,
                "failed": gate.failed,
                "exit_code": gate.exit_code,
                "log": gate.log,
            },
        )

    def record_gate(self, gate_id: str, state: str) -> None:
        """记下某个 assignment 的门禁结论；test_invalid 累计到上限就标为跑不起来。"""
        self.gates[gate_id] = state
        if state != TEST_INVALID:
            self.gate_tries.pop(gate_id, None)
            self.broken_gates.discard(gate_id)
        else:
            tries = self.gate_tries.get(gate_id, 0) + 1
            self.gate_tries[gate_id] = tries
            if tries >= _MAX_GATE_RUNS:
                self.broken_gates.add(gate_id)
        self.journal.append(
            "gate",
            {
                "gate_id": gate_id,
                "state": state,
                "tries": self.gate_tries.get(gate_id),
                "broken": gate_id in self.broken_gates,
            },
        )

    def gate_unrunnable(self) -> bool:
        """所有 test_invalid 的门禁都已判定跑不起来：不该再拦 finish。"""
        stuck = {gid for gid, state in self.gates.items() if state ==
                 TEST_INVALID}
        return bool(stuck) and stuck <= self.broken_gates

    async def emit(self, payload: dict[str, Any]) -> None:
        await self.runner._emit(self.on_event, payload)

    async def emit_halt(self, halt: dict[str, Any]) -> None:
        await self.runner._emit_halt(self.on_event, halt)

    async def mark_sandbox_down(self, reason: str = "") -> None:
        """本场 lab 因沙箱不可达收工；HTTP 服务继续跑。"""
        if self.sandbox_down:
            return
        self.sandbox_down = (
            reason.strip()
            or "Sandbox unreachable after 3 consecutive MCP failures."
        )
        self.journal.append("sandbox_down", {"reason": self.sandbox_down})
        await self.emit(
            {
                "kind": "error",
                "detail": f"沙箱不可达，本场 lab 结束：{self.sandbox_down[:800]}",
            }
        )
        self.set_stage("summary")

    def take_halt(self, submit: dict[str, Any] | None) -> dict[str, Any] | None:
        found = halt_of(submit)
        if found:
            self.set_halt(found)
        return found

    def has_step_markers(self, step: int) -> bool:
        """某一步是否已有 journal 标记：有说明上次崩在这一步内部。"""
        return (
            step in self.step_dispatch
            or step in self.step_briefs
            or step in self.step_judges
        )

    def remember_block(self) -> str:
        rules = self.runner._applied_rules or []
        if not rules:
            return "（本 lab 没有适用的 /remember 规则）"
        return "\n".join(f"{i}. {r}" for i, r in enumerate(rules))

    def restore(self, state: JournalState) -> None:
        """把 replay 结果注入内存，并校准 transcript 的增量落盘起点。"""
        self.pro_history = state.transcript
        self.progress = state.progress
        self.executed_ids = state.executed_ids
        self.gates = state.gates
        self.gate_tries = state.gate_tries
        self.broken_gates = state.broken_gates
        if state.run_gate:
            gate = state.run_gate
            self.run_gate = AcceptResult(
                state=str(gate.get("state") or NO_HARD_CRITERIA),
                passed=int(gate.get("passed") or 0),
                failed=int(gate.get("failed") or 0),
                exit_code=int(gate.get("exit_code") or 0),
                log=str(gate.get("log") or ""),
            )
        self.halt = state.halt
        self.sandbox_down = state.sandbox_down
        self.dispatch_step = state.dispatch_step
        self.stage = state.stage
        self.step_dispatch = state.step_dispatch
        self.step_briefs = state.step_briefs
        self.step_judges = state.step_judges
        if state.skill:
            self.runner._skill.name = state.skill
        self.journal.sync_tx(len(self.pro_history))

    async def drive_pro(self, kind: TaskKind, user: str, label: str) -> dict[str, Any]:
        """跑一拍 Pro。只读主线并入同一条 transcript，并在尾部追加 phase_control。"""
        runner = self.runner
        phase = phase_of(kind)
        permission = phase.node_permission()
        if phase.shares_thread:
            _seed_pro_thread(self)
            tail = [phase_control_message(kind)]
            if user:
                tail.append({"role": "user", "content": user})
            # 续传去重：上次崩在本拍中途时，这批指令已随 transcript 落盘，
            # 再追加一遍只会让 Pro 读到两遍同样的指令。
            if self.pro_history[-len(tail):] == tail:
                incoming = list(self.pro_history)
            else:
                incoming = [*self.pro_history, *tail]
            user_input = ""
            project_spec = read_text(self.session_dir, SPEC_FILE)
        else:
            incoming = []
            user_input = user
            project_spec = ""
        child = self.tree.add_child(
            self.root.task_id,
            kind=kind,
            permission=permission,
            step_budget=runner.settings.pro_step_budget,
            deadline=runner.clock() + runner.settings.task_wall_time_s,
        )
        child.transit(TaskStatus.RUNNING)

        with runner.tracer.span(
            S.TASK,
            kind=S.KIND_AGENT,
            **{
                S.ATTR_TASK_ID: child.task_id,
                S.ATTR_TASK_KIND: kind.value,
                S.ATTR_AGENT: f"pro:{label}",
            },
        ):
            result = await run_loop(
                child,
                runner._agent_spec(kind, permission),
                settings=runner.settings,
                llm=runner.llm,
                registry=runner.registry,
                session_dir=self.session_dir,
                user_input=user_input,
                project_spec=project_spec,
                history=incoming,
                on_event=self.on_event,
                tracer=runner.tracer,
                clock=runner.clock,
                transcript_sink=(
                    self.journal.append_transcript if phase.shares_thread else None
                ),
            )

        if phase.shares_thread:
            # 存回去的 transcript 必须配平：末尾若留着没有 tool 响应的
            # assistant，下一阶段追加的指令会被 drop_dangling_tool_calls 一并截掉。
            self.pro_history[:] = drop_dangling_tool_calls(result.history)

        if result.reason == "sandbox_unreachable":
            await self.mark_sandbox_down(str((result.brief or {}).get("brief") or ""))

        payload = _submit_payload(result.submit)
        append_forget(self.session_dir, str(
            payload.get("forget_append") or ""))
        notes_touched = _notes_touched(payload)
        apply_notes(
            self.session_dir,
            replace=payload.get("notes_replace"),
            remove=payload.get("notes_remove"),
            append=str(payload.get("notes_append") or ""),
        )
        write_long_note(self.session_dir, payload.get("notes_write"))
        if phase.shares_thread and notes_touched:
            _append_state(self, "notes_changed",
                          _notes_snapshot(self.session_dir))

        if result.submit:
            child.transit(TaskStatus.COMPLETED)
            child.brief = result.submit
        else:
            try:
                child.transit(TaskStatus.FAILED)
            except ValueError:
                pass
        self.flush_tree()
        return result.submit or {}


def _seed_pro_thread(st: LabState) -> None:
    """只读主线的固定开场：用户任务 + catalog + 已裁定的 /remember。只写一次。"""
    if st.pro_history:
        return
    st.pro_history = [
        {
            "role": "user",
            "content": (
                f"User request:\n{st.question}\n\n{st.catalog}\n\n"
                f"## Applicable /remember rules\n{st.remember_block()}"
            ),
        }
    ]


def _append_state(st: LabState, kind: str, body: str) -> None:
    st.pro_history.append(
        {"role": "user", "content": render_state_update(kind, body)})
    # 追加即落盘：这条状态消息不该依赖下一次 drive 的 flush 才能活下来。
    st.journal.append_transcript(st.pro_history, False)


def _notes_touched(payload: dict[str, Any]) -> bool:
    return any(
        payload.get(k)
        for k in ("notes_append", "notes_remove", "notes_replace", "notes_write")
    )


def _notes_snapshot(session_dir: Path) -> str:
    notes = load_notes(session_dir)
    return notes.notes.strip() or "(empty)"


def _spec_state_body(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    head = "\n".join(text.strip().splitlines()[:4])
    return f"SPEC version: sha256:{digest}\n{head}"


def _submit_payload(submit: dict[str, Any] | None) -> dict[str, Any]:
    """submit_brief 是裸 payload；其余是 {name, payload}。"""
    if not submit:
        return {}
    inner = submit.get("payload") if submit.get("name") else submit
    return inner if isinstance(inner, dict) else {}


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
    if st.stage != "summary":
        await remember(st)
        failed = await write_spec(st)
        if failed:
            return failed
        paused = await advance(st)
        if paused:
            return paused
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
    journal = Journal.open(session_dir)
    state = journal.replay() if resume else None
    if state and state.question.strip():
        question = state.question
    if state is None or not state.question.strip():
        journal.append("begin", {"question": question})
    if resume and root.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
        root.transit(TaskStatus.RUNNING)
    elif root.status is TaskStatus.PENDING:
        root.transit(TaskStatus.RUNNING)
    # 崩溃时在途的非 root 节点永远等不到结果了：标 CANCELLED，树才反映真实状态。
    for node in tree.nodes.values():
        if node.task_id != tree.root_id and node.status is TaskStatus.RUNNING:
            try:
                node.transit(TaskStatus.CANCELLED)
            except ValueError:
                pass
    save_tree(session_dir, tree)

    reset_sandbox_failure_counter()
    try:
        await reconcile_index(runner.llm, runner.settings)
    except Exception:
        pass
    ledger = EffectLedger(
        default_probes(),
        journal,
        runner.settings.workspace_dir,
        entries=state.effects if state else None,
    )
    catalog = read_text(session_dir, CATALOG_FILE) or await ingest(runner.settings, session_dir)
    st = LabState(
        runner=runner,
        question=question,
        session_dir=session_dir,
        tree=tree,
        on_event=on_event,
        resume=resume,
        root=root,
        journal=journal,
        ledger=ledger,
        catalog=catalog,
    )
    if state:
        st.restore(state)
        await st.emit(
            {
                "kind": "resume_skip",
                "assignments": sorted(st.executed_ids),
                "history_turns": len(st.pro_history),
            }
        )
    return st


async def remember(st: LabState) -> None:
    if os.getenv("EVAL_DISABLE_REMEMBER") == "1":
        st.runner._applied_rules = []
        save_applied(st.session_dir, [])
        return
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
    listed = "\n".join(f"{i}. {rule}" for i, rule in enumerate(catalog))
    submit = await st.drive_pro(
        TaskKind.REMEMBER_JUDGE,
        (
            f"User request:\n{st.question}\n\n{st.catalog}\n\n"
            f"## /remember rules（编号即身份）\n{listed}\n\n"
            "Read the listed files if you need to know what this homework will hand in "
            "(code vs 实验报告 vs essay). Do not write code or 实验报告 here; do not grep for "
            "the rule text. labHandler calling this run a lab does not mean 实验报告. "
            "Default each /remember rule to applies=false. "
            "Then call submit_remember with one verdict per rule, identified by its index "
            "above. Do not retype the rule text as the identifier. Do not write SPEC.md."
        ),
        "remember",
    )
    found = st.take_halt(submit)
    if found:
        st.runner._applied_rules = []
        save_applied(st.session_dir, [])
        await st.emit(
            {"kind": "node_done", "node": "remember_judge",
                "log": [{"halted": True}]}
        )
        await st.emit_halt(found)
        st.set_stage("summary")
        st.flush_tree()
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
    st.set_stage("spec")
    st.flush_tree()


async def write_spec(st: LabState) -> dict[str, Any] | None:
    """写出 SPEC.md。失败返回 spec_failed；halt / 沙箱不可达则落到 Summary。"""
    if st.halt or st.sandbox_down:
        return None

    cards = load_cards(st.session_dir)
    if cards is None:
        cards = await prefetch_cards(st.question, st.runner.llm, settings=st.runner.settings)
        write_cards(st.session_dir, cards)
    await st.emit({"kind": "cards", "n": len(cards)})

    project = resume_spec(st.tree) if st.resume else None
    if st.resume and project is not None:
        await st.emit({"kind": "node_done", "node": "spec", "log": [{"resumed": True}]})
        rendered = project.render()
        write_text(st.session_dir, SPEC_FILE, rendered)
        _seed_pro_thread(st)
        _append_state(st, "spec_created", _spec_state_body(rendered))
        await _mark_resumable(st)
        if st.stage == "remember":
            st.set_stage("advance")
        st.flush_tree()
        return None

    await st.emit({"kind": "node_start", "node": "spec"})
    submit = await st.drive_pro(TaskKind.SPEC, "", "spec")
    found = st.take_halt(submit)
    if found:
        await st.emit_halt(found)
        st.set_stage("summary")
        st.flush_tree()
        return None
    if st.sandbox_down:
        st.set_stage("summary")
        st.flush_tree()
        return None

    project = ProjectSpec.from_payload(payload_of(submit, SUBMIT_SPEC))
    if not project.goal:
        await st.emit({"kind": "error", "detail": "Pro 未能产出 SPEC.md，任务终止"})
        try:
            st.root.transit(TaskStatus.FAILED)
        except ValueError:
            pass
        st.flush_tree()
        return {
            "verdict": "spec_failed",
            "summary": "",
            "knowledge_cards": [],
            "question": st.question,
        }
    await st.emit(
        {"kind": "node_done", "node": "spec", "log": [
            {"milestones": len(project.milestones)}]}
    )
    write_text(st.session_dir, SPEC_FILE, project.render())
    _append_state(st, "spec_created", _spec_state_body(project.render()))
    await _mark_resumable(st)
    st.set_stage("advance")
    st.flush_tree()
    return None


async def _mark_resumable(st: LabState) -> None:
    if not st.resume or st.halt or st.sandbox_down:
        return
    st.resumable = {
        aid for aid in st.ledger.entries if st.ledger.satisfied(aid)}
    have = {aid for aid, _ in st.progress}
    for aid in sorted(st.resumable):
        if aid in have:
            continue
        st.add_progress(aid, "（续跑：产物已存在且未被改动，跳过重跑）")
    if st.resumable:
        await st.emit({"kind": "resume_skip", "assignments": sorted(st.resumable)})


async def advance(st: LabState) -> dict[str, Any] | None:
    """逐步派发。用户停止则暂停（root 仍 RUNNING）；halt / 沙箱不可达 / 收工则落到 Summary。

    dispatch_step 记的是已完成的步数。续跑时若下一步已有 journal 标记，说明
    上次崩在那一步内部，先按标记把它续完，再进主循环。
    """
    if st.halt or st.sandbox_down or st.stage == "summary":
        return None

    revisions = 0
    gate_nudge = ""
    pending = st.dispatch_step + \
        1 if st.has_step_markers(st.dispatch_step + 1) else 0
    while st.dispatch_step < _MAX_STEPS and not st.halt and not st.sandbox_down:
        if st.runner._cancelled():
            st.set_stage("advance")
            st.flush_tree()
            return {"verdict": "paused", "summary": "", "question": st.question}

        step = st.dispatch_step + 1
        if step == pending:
            signal, gate_nudge, revisions = await _resume_step(st, step, gate_nudge, revisions)
        else:
            signal, gate_nudge, revisions = await _one_step(st, step, gate_nudge, revisions)
        st.set_step(step)
        if signal == "break":
            st.set_stage("summary")
            break
    return None


async def _one_step(
    st: LabState, step: int, gate_nudge: str, revisions: int
) -> tuple[str, str, int]:
    """跑一步：dispatch → workers → judge。返回 (break|continue, 下一步 nudge, revisions)。"""
    dispatch, assignments, replay, signal, gate_nudge = await _dispatch_phase(
        st, step, gate_nudge
    )
    if dispatch is None:
        return signal, gate_nudge, revisions
    return await _execute_and_judge(
        st, step, dispatch, assignments, replay, [], gate_nudge, revisions
    )


async def _resume_step(
    st: LabState, step: int, gate_nudge: str, revisions: int
) -> tuple[str, str, int]:
    """把崩在内部的某一步续完：按 journal 标记跳过已完成的子阶段。"""
    marker = st.step_dispatch.get(step)
    if marker is None:
        # dispatch 未提交：重跑整步，dispatch drive 本身是 transcript 级续传。
        return await _one_step(st, step, gate_nudge, revisions)
    dispatch = Dispatch.from_payload(marker.get("payload") or {})
    run_ids = {str(x) for x in marker.get("run") or []}
    replay_ids = {str(x) for x in marker.get("replay") or []}
    pinned = [pin_assignment(a, step, i)
              for i, a in enumerate(dispatch.assignments)]
    done = st.step_briefs.get(step, {})
    done_briefs = [done[a.id] for a in pinned if a.id in done]
    decision = st.step_judges.get(step)
    if decision is not None:
        # judge 已提交：复用 decision，分支里的 drive 仍是 transcript 级续传。
        assignments = [
            a for a in pinned if a.id in run_ids or a.id in replay_ids]
        return await _apply_decision(
            st, decision, dispatch, done_briefs, assignments, step, False,
            gate_nudge, revisions,
        )
    assignments = [a for a in pinned if a.id in run_ids and a.id not in done]
    replay = [a for a in pinned if a.id in replay_ids and a.id not in done]
    if done:
        await st.emit({"kind": "resume_skip", "assignments": sorted(done)})
    return await _execute_and_judge(
        st, step, dispatch, assignments, replay, done_briefs, gate_nudge, revisions
    )


async def _dispatch_phase(
    st: LabState, step: int, gate_nudge: str
) -> tuple[Dispatch | None, list, list, str, str]:
    """dispatch drive + 校验 + partition。

    返回的 dispatch 为 None 表示本步到此为止（break，或带 nudge 的 continue）。
    partition 通过即写 dispatch 提交标记：之后崩了，续跑从 workers 接着走，
    不再重发 dispatch。
    """
    dispatch_user = (
        f"## 已完成的步骤\n{render_progress(st.progress)}\n\n"
        f"这是第 {step} 步（最多 {_MAX_STEPS} 步）。决定接下来这一步做什么，调用 submit_dispatch。"
        "只派这一步给 Flash 的活；Flash 侧全部做完时给空的 assignments。"
    )
    if st.resume and not st.resume_announced:
        st.resume_announced = True
        dispatch_user += (
            "\n\n## Resume\n"
            "The previous process stopped. This conversation is the same thread. "
            "Continue from here. Do not rewrite SPEC. Do not re-do assignments whose "
            "artifacts still match."
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
        return None, [], [], "break", gate_nudge
    if st.sandbox_down:
        return None, [], [], "break", gate_nudge
    if not submit or submit.get("name") != SUBMIT_DISPATCH:
        await st.emit(
            {
                "kind": "error",
                "detail": "Pro 未能产出有效派发（校验失败或步数耗尽），未启动 worker",
            }
        )
        return None, [], [], "break", gate_nudge
    payload = payload_of(submit, SUBMIT_DISPATCH)
    dispatch = Dispatch.from_payload(payload)

    if dispatch.is_empty:
        if st.run_gate.state in {FAIL, TEST_INVALID} and not st.gate_unrunnable():
            msg = (
                f"Gate is still {st.run_gate.state}; empty assignments would skip to SUMMARY "
                "with a broken gate. write_acceptance to fix test_invalid, or re-dispatch the "
                "same product id to fix fail."
            )
            await st.emit(
                {"kind": "node_done", "node": f"dispatch:{step}",
                    "log": [{"rejected": msg[:400]}]}
            )
            return None, [], [], "continue", msg
        await st.emit(
            {"kind": "node_done", "node": f"dispatch:{step}",
                "log": [{"assignments": 0}]}
        )
        return None, [], [], "break", gate_nudge

    gap = missing_gate(st.session_dir, dispatch.assignments)
    if gap:
        await st.emit(
            {"kind": "node_done", "node": f"dispatch:{step}",
                "log": [{"rejected": gap[:400]}]}
        )
        return None, [], [], "continue", gap

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
                None, [], [],
                "continue",
                f"assignment id(s) {skipped} already ran and their gate cannot run in this "
                "sandbox. There is no more Flash work: submit_dispatch with empty assignments.",
            )
        if skipped:
            return (
                None, [], [],
                "continue",
                f"assignment id(s) {skipped} already ran this lab (gate={st.run_gate.state}). "
                "If the product is done, submit_dispatch with empty assignments. "
                "If gate is test_invalid, write_acceptance then resubmit the same id (no new Flash). "
                "If gate is fail, resubmit the same id to re-run Flash.",
            )
        return None, [], [], "continue", gate_nudge

    # 提交点：之后的崩溃从这里续跑，不再重发 dispatch。
    st.journal.append(
        "dispatch",
        {
            "step": step,
            "payload": payload,
            "run": [a.id for a in assignments],
            "replay": [a.id for a in replay],
            "skipped": skipped,
        },
    )
    st.journal.append("executed", {"ids": sorted(st.executed_ids)})
    return dispatch, assignments, replay, "", gate_nudge


async def _execute_and_judge(
    st: LabState,
    step: int,
    dispatch: Dispatch,
    assignments: list,
    replay: list,
    done_briefs: list[dict[str, Any]],
    gate_nudge: str,
    revisions: int,
) -> tuple[str, str, int]:
    """dispatch 提交之后：补跑缺失的 worker / 重跑门禁，然后 judge。

    done_briefs 是续跑时从 journal 还原的已完成 worker brief，直接并入
    judge 的输入，不重跑。
    """
    briefs: list[dict[str, Any]] = list(done_briefs)
    spec_invalid = False
    step_gates: list[AcceptResult] = [
        AcceptResult(state=str((b.get("tests") or {}).get(
            "state") or NO_HARD_CRITERIA))
        for b in done_briefs
    ]
    if assignments:
        new_briefs, spec_invalid, wave_gate, step_halt = await run_step(
            st.runner,
            assignments,
            question=st.question,
            step_goal=dispatch.step_goal,
            session_dir=st.session_dir,
            tree=st.tree,
            root=st.root,
            ledger=st.ledger,
            progress=st.progress,
            on_progress=st.add_progress,
            journal=st.journal,
            step=step,
            on_gate=st.record_gate,
            on_event=st.on_event,
        )
        if step_halt:
            st.set_halt(step_halt)
            return "break", gate_nudge, revisions
        step_gates.append(wave_gate)
        briefs += new_briefs
        if any(b.get("sandbox_unreachable") for b in new_briefs):
            reason = next(
                (str(b.get("brief") or "")
                 for b in new_briefs if b.get("sandbox_unreachable")),
                "",
            )
            await st.mark_sandbox_down(reason)
            return "break", gate_nudge, revisions
        if any(b.get("takeover") for b in new_briefs):
            await _takeover(
                st, dispatch, briefs, assignments or replay, step, reason="validation_exhausted"
            )
            if st.halt or st.sandbox_down:
                return "break", gate_nudge, revisions
            return "continue", gate_nudge, revisions
    if replay:
        extra, extra_gate = await _replay_gates(st, replay, step)
        briefs = briefs + extra
        step_gates.append(extra_gate)
        if st.sandbox_down:
            return "break", gate_nudge, revisions
    last_gate = worst_gate(step_gates)
    st.set_run_gate(last_gate)

    decision = await _judge(st, dispatch, briefs, last_gate)
    if st.halt or st.sandbox_down:
        return "break", gate_nudge, revisions
    st.journal.append("judge", {"step": step, "decision": decision})
    return await _apply_decision(
        st, decision, dispatch, briefs, assignments or replay, step,
        spec_invalid, gate_nudge, revisions,
    )


async def _apply_decision(
    st: LabState,
    decision: str,
    dispatch: Dispatch,
    briefs: list[dict[str, Any]],
    assignments: list,
    step: int,
    spec_invalid: bool,
    gate_nudge: str,
    revisions: int,
) -> tuple[str, str, int]:
    """judge 之后的分支：finish 收工、takeover 接管、revise_spec 改 SPEC。"""
    if decision == "finish":
        return "break", gate_nudge, revisions
    if decision == "takeover":
        await _takeover(st, dispatch, briefs, assignments, step, reason="judge")
        if st.halt or st.sandbox_down:
            return "break", gate_nudge, revisions
        return "continue", gate_nudge, revisions
    if decision == "revise_spec" or spec_invalid:
        revisions = await _revise_spec(st, briefs, revisions)
        if st.halt or st.sandbox_down or revisions < 0:
            return "break", gate_nudge, max(revisions, 0)
    return "continue", gate_nudge, revisions


async def _replay_gates(
    st: LabState, replay: list, step: int
) -> tuple[list[dict[str, Any]], AcceptResult]:
    """同一产品本 run 已派过 Flash，且门禁是 test_invalid：只重跑 pytest。"""
    gates: list[AcceptResult] = []
    briefs: list[dict[str, Any]] = []
    for a in replay:
        gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
        gates.append(gate)
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
        st.journal.append("brief", {"step": step, "aid": a.id, "brief": brief})
        st.add_progress(a.id, str(brief["brief"]))
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
        if is_sandbox_unreachable(gate.log):
            await st.mark_sandbox_down(gate.log)
            break
    return briefs, worst_gate(gates)


def _summary_gate(st: LabState) -> AcceptResult:
    """整次 lab 的门禁：各 assignment 最差态。没有记录则用最近一步。"""
    if not st.gates:
        return st.run_gate
    return worst_gate(AcceptResult(state=state) for state in st.gates.values())


def _gate_line(st: LabState) -> str:
    """整体门禁 + 逐个 assignment 的明细，让收尾那一拍能自己对账。"""
    overall = _summary_gate(st).state
    if not st.gates:
        return overall
    detail = ", ".join(f"{gid}={state}" for gid,
                       state in sorted(st.gates.items()))
    return f"{overall}（{detail}）"


async def _judge(
    st: LabState,
    dispatch: Dispatch,
    briefs: list[dict[str, Any]],
    last_gate: AcceptResult,
) -> str:
    judge_user = (
        f"Step goal: {dispatch.step_goal}\n"
        f"Gate: {last_gate.state}\n"
        f"Briefs:\n{json.dumps(briefs, ensure_ascii=False)[:_BRIEFS_CAP]}\n"
        "Fill rule_verdicts for every applicable /remember rule, identified by the index "
        "in the initial user task. Do not retype the rule text as the identifier. "
        "finish only when all are satisfied. "
        "If the gate is no_hard_criteria you MUST say so in evidence and give a semantic rationale."
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
    if st.sandbox_down:
        return "halt"
    verdict = payload_of(judge_submit, SUBMIT_JUDGE)
    decision = str(verdict.get("decision") or "continue")
    if last_gate.state == TEST_INVALID and dispatch.assignments and not st.gate_unrunnable():
        refreshed: list[AcceptResult] = []
        for a in dispatch.assignments:
            gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
            st.record_gate(a.gate_id, gate.state)
            refreshed.append(gate)
            if is_sandbox_unreachable(gate.log):
                await st.mark_sandbox_down(gate.log)
                return "halt"
        last_gate = worst_gate(refreshed)
        st.run_gate = last_gate
    decision = sanitize_decision(
        decision,
        verdict,
        gate_state=last_gate.state,
        gate_unrunnable=st.gate_unrunnable(),
        applied_rules=st.runner._applied_rules or [],
    )

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
    *,
    reason: str = "judge",
) -> None:
    st.runner._trace_event(
        S.EV_HANDOFF, **{S.ATTR_FROM: "flash", S.ATTR_TO: "pro"})
    await st.emit({"kind": "pro_takeover", "reason": reason})
    cause = (
        "Flash exhausted schema validation; finish the step yourself."
        if reason == "validation_exhausted"
        else "Take over this step yourself."
    )
    take = await st.drive_pro(
        TaskKind.TAKEOVER,
        (
            f"{cause}\n"
            f"Step goal: {dispatch.step_goal}\n"
            f"Briefs: {json.dumps(briefs, ensure_ascii=False)[:8000]}"
        ),
        "takeover",
    )
    found = st.take_halt(take)
    if found:
        await st.emit_halt(found)
        return
    if st.sandbox_down:
        return
    payload = payload_of(take, SUBMIT_BRIEF)
    if payload.get("brief"):
        st.add_progress(f"step{step}-pro", str(payload["brief"]))
        for a in assignments:
            last_gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
            if is_sandbox_unreachable(last_gate.log):
                await st.mark_sandbox_down(last_gate.log)
                break
            if not last_gate.is_pass:
                break
    else:
        st.add_progress(f"step{step}-pro", "Pro 接管未完成本步，该工作仍然待办")


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
        "The current SPEC.md was rejected. Revise it given these briefs:\n"
        + json.dumps(briefs, ensure_ascii=False)[:8000],
        "revise_spec",
    )
    found = st.take_halt(redo)
    if found:
        await st.emit_halt(found)
        return revisions
    if st.sandbox_down:
        return revisions
    project = ProjectSpec.from_payload(payload_of(redo, SUBMIT_SPEC))
    rendered = project.render()
    write_text(st.session_dir, SPEC_FILE, rendered)
    _append_state(st, "spec_created", _spec_state_body(rendered))
    return revisions


async def summarize(st: LabState) -> dict[str, Any]:
    if st.halt:
        summary_user = (
            "The lab halted. A required fact is missing and only the user can provide it. "
            "Do not invent completed homework.\n\n"
            f"## Why it halted\n{st.halt.get('reason')}\n\n"
            f"## Need from the user\n{st.halt.get('need_from_user')}\n\n"
            f"## 完成情况\n{render_progress(st.progress)}\n\n"
            "Call submit_summary. user_summary must explain the situation and what the user "
            "should put in the workspace, in the user's language."
        )
    elif st.sandbox_down:
        summary_user = (
            "The lab stopped because the AIO Sandbox MCP endpoint is unreachable. "
            "Do not invent completed homework. Do not claim tests passed. "
            "The HTTP service is still running; the user can retry after the container is up.\n\n"
            f"## Why it stopped\n{st.sandbox_down}\n\n"
            f"## 完成情况\n{render_progress(st.progress)}\n\n"
            "Call submit_summary. user_summary must say the sandbox was unreachable "
            "and the lab ended without further Flash or Judge work, in the user's language."
        )
    else:
        summary_user = (
            "The lab is complete. Write the wrap-up from this conversation "
            "and the live SPEC.md / NOTES.md snapshots.\n\n"
            f"## 完成情况\n{render_progress(st.progress)}\n\n"
            f"Gate: {_gate_line(st)}\n"
            "Call submit_summary. Report that gate state as-is; it is the harness result, "
            "and the per-assignment breakdown above is what it was computed from. "
            "If it says no_hard_criteria, say so in user_summary."
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
        "verdict": (
            "need_user"
            if st.halt
            else "sandbox_unreachable"
            if st.sandbox_down
            else _summary_gate(st).state
        ),
        "summary": summary_text,
        "knowledge_cards": list(payload.get("knowledge_cards") or []),
        "question": st.question,
    }
