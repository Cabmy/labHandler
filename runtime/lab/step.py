"""一步内的 Flash 派发：组波、跑 worker、跑门禁。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from runtime.lab.accept import AcceptResult, NO_HARD_CRITERIA, PASS, TEST_INVALID, sanity_and_run
from runtime.lab.helpers import halt_of, step_gate
from runtime.lab.persist import (
    EffectLedger,
    SPEC_FILE,
    audit_offset,
    changed_files_from_audit,
    read_text,
    save_tree,
)
from runtime.lab.scheduler import permission_for_wave, run_wave
from runtime.lab.spec import Assignment, render_assignment
from runtime.loop import run_loop
from runtime.loop.parse import synthetic_brief
from runtime.loop.schema import SUBMIT_HALT
from runtime.observe import spans as S
from runtime.task import RuntimeTask, TaskKind, TaskStatus, TaskTree
from tools.sandbox_tools import is_sandbox_unreachable

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


async def run_gate(runner: Any, session_dir: Path, assignment_id: str) -> AcceptResult:
    span_cm = (
        runner.tracer.span(
            S.ACCEPT, kind=S.KIND_EVALUATOR, **{S.ATTR_ASSIGNMENT_ID: assignment_id}
        )
        if runner.tracer is not None
        else None
    )
    span = span_cm.__enter__() if span_cm is not None else None
    try:
        gate = await sanity_and_run(session_dir, assignment_id, settings=runner.settings)
        if span is not None:
            span.set(
                **{
                    S.ATTR_GATE_STATE: gate.state,
                    S.ATTR_GATE_PASSED: gate.passed,
                    S.ATTR_GATE_FAILED: gate.failed,
                    S.ATTR_GATE_EXIT: gate.exit_code,
                }
            )
            span.output(gate.log[-1000:])
        return gate
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)


async def run_step(
    runner: Any,
    assignments: list[Assignment],
    *,
    question: str,
    step_goal: str,
    session_dir: Path,
    tree: TaskTree,
    root: RuntimeTask,
    ledger: EffectLedger,
    progress: list[tuple[str, str]],
    on_event: EventSink | None,
) -> tuple[list[dict[str, Any]], bool, AcceptResult, dict[str, Any] | None]:
    permission = permission_for_wave(len(assignments))
    workers = [
        tree.add_child(
            root.task_id,
            kind=TaskKind.WORKER,
            permission=permission,
            step_budget=runner.settings.flash_step_budget,
            deadline=runner.clock() + runner.settings.task_wall_time_s,
            node_spec=a.to_dict(),
        )
        for a in assignments
    ]
    save_tree(session_dir, tree)

    domains = [a.domain or a.goal for a in assignments]
    done_so_far = list(progress)

    span_cm = (
        runner.tracer.span(
            S.STEP,
            kind=S.KIND_CHAIN,
            **{
                S.ATTR_WAVE_SIZE: len(workers),
                S.ATTR_PERMISSION: permission.value,
                S.ATTR_STEP_GOAL: step_goal,
            },
        )
        if runner.tracer is not None
        else None
    )
    if span_cm is not None:
        span_cm.__enter__()
    try:
        results = await run_wave(
            workers,
            lambda w: run_worker(
                runner,
                w,
                question=question,
                step_goal=step_goal,
                session_dir=session_dir,
                tree=tree,
                ledger=ledger,
                done_so_far=done_so_far,
                domains=domains,
                on_event=on_event,
            ),
            runner.settings,
            tracer=runner.tracer,
        )
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)

    briefs = []
    spec_invalid = False
    halt: dict[str, Any] | None = None
    for worker, brief in results:
        briefs.append(brief)
        found = halt_of(brief)
        if found:
            halt = found
        aid = str((worker.node_spec or {}).get("id") or "")
        if not aid:
            continue
        if brief.get("outcome") == "spec_invalid":
            spec_invalid = True
        progress.append((aid, str(brief.get("brief") or (found or {}).get("reason") or "")))

    return briefs, spec_invalid, step_gate(results), halt


async def run_worker(
    runner: Any,
    worker: RuntimeTask,
    *,
    question: str,
    step_goal: str,
    session_dir: Path,
    tree: TaskTree,
    ledger: EffectLedger,
    done_so_far: list[tuple[str, str]],
    domains: list[str],
    on_event: EventSink | None,
) -> dict[str, Any]:
    if worker.status is TaskStatus.PENDING:
        worker.transit(TaskStatus.RUNNING)
    audit_mark = audit_offset(runner.settings.workspace_dir)

    assignment = Assignment.from_payload(worker.node_spec or {})
    others = [d for d in domains if d and d != (assignment.domain or assignment.goal)]
    prompt = render_assignment(
        assignment,
        user_request=question,
        project_spec=read_text(session_dir, SPEC_FILE),
        step_goal=step_goal,
        done_so_far=done_so_far,
        parallel_domains=others if len(domains) > 1 else None,
    )

    span_cm = (
        runner.tracer.span(
            S.TASK,
            kind=S.KIND_AGENT,
            inputs=prompt,
            **{
                S.ATTR_TASK_ID: worker.task_id,
                S.ATTR_TASK_KIND: TaskKind.WORKER.value,
                S.ATTR_ASSIGNMENT_ID: assignment.id,
                S.ATTR_DOMAIN: assignment.domain,
                S.ATTR_AGENT: f"flash:{assignment.id}",
                S.ATTR_PERMISSION: worker.permission.value,
            },
        )
        if runner.tracer is not None
        else None
    )
    task_span = span_cm.__enter__() if span_cm is not None else None
    try:
        await runner._emit(on_event, {"kind": "node_start", "node": f"flash:{assignment.id}"})
        try:
            result = await asyncio.wait_for(
                run_loop(
                    worker,
                    runner._agent_spec(TaskKind.WORKER, worker.permission),
                    settings=runner.settings,
                    llm=runner.llm,
                    registry=runner.registry,
                    session_dir=session_dir,
                    user_input=prompt,
                    project_spec="",
                    on_event=on_event,
                    tracer=runner.tracer,
                    clock=runner.clock,
                ),
                timeout=max(1.0, worker.deadline - runner.clock()),
            )
            brief = result.brief or synthetic_brief(
                outcome="failed", brief=result.reason or "no brief"
            )
            if result.reason == "sandbox_unreachable":
                brief["sandbox_unreachable"] = True
                brief["outcome"] = "failed"
            if result.reason == "validation_takeover":
                brief["takeover"] = True
                brief["outcome"] = "failed"
            halted = halt_of(result.submit)
            if halted:
                payload = {
                    "name": SUBMIT_HALT,
                    "payload": halted,
                    "outcome": "halt",
                    "brief": halted.get("reason"),
                }
                worker.brief = payload
                try:
                    worker.transit(TaskStatus.COMPLETED)
                except ValueError:
                    pass
                ledger.drop(assignment.id)
                save_tree(session_dir, tree)
                if task_span is not None:
                    task_span.set(**{S.ATTR_OUTCOME: "halt"})
                    task_span.output(str(halted.get("reason") or "")[:2000])
                await runner._emit(
                    on_event,
                    {
                        "kind": "halt",
                        "reason": str(halted.get("reason") or "")[:800],
                        "need_from_user": str(halted.get("need_from_user") or "")[:800],
                        "assignment_id": assignment.id,
                    },
                )
                return payload
        except asyncio.TimeoutError:
            brief = synthetic_brief(
                outcome="failed",
                brief="Your assignment was stopped because the execution budget was exhausted.",
                changed_files=changed_files_from_audit(
                    runner.settings.workspace_dir, since=audit_mark
                ),
            )
            try:
                worker.transit(TaskStatus.CANCELLED)
            except ValueError:
                pass
            worker.brief = brief
            ledger.drop(assignment.id)
            save_tree(session_dir, tree)
            return brief

        brief["changed_files"] = brief.get("changed_files") or changed_files_from_audit(
            runner.settings.workspace_dir, since=audit_mark
        )
        if brief.get("sandbox_unreachable"):
            gate = AcceptResult(
                state=TEST_INVALID,
                exit_code=-1,
                log=str(brief.get("brief") or "sandbox unreachable"),
            )
        else:
            gate = await run_gate(runner, session_dir, assignment.gate_id)
            if is_sandbox_unreachable(gate.log):
                brief["sandbox_unreachable"] = True
        brief["tests"] = gate.as_tests()
        worker.brief = brief

        succeeded = brief.get("outcome") == "done" and gate.state in {PASS, NO_HARD_CRITERIA}
        if succeeded:
            ledger.record(assignment.id, brief["changed_files"])
        else:
            ledger.drop(assignment.id)

        try:
            if succeeded:
                worker.transit(TaskStatus.COMPLETED)
            elif worker.status is TaskStatus.RUNNING:
                worker.transit(TaskStatus.FAILED)
        except ValueError:
            pass

        if task_span is not None:
            task_span.set(
                **{S.ATTR_OUTCOME: brief.get("outcome"), S.ATTR_GATE_STATE: gate.state}
            )
            task_span.output(str(brief.get("brief") or "")[:2000])

        await runner._emit(
            on_event,
            {
                "kind": "worker_brief",
                "assignment_id": assignment.id,
                "domain": assignment.domain,
                "outcome": brief.get("outcome"),
                "brief": brief.get("brief"),
                "tests": brief.get("tests"),
                "gate": gate.state,
            },
        )
        save_tree(session_dir, tree)
        return brief
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)
