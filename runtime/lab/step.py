"""一步内的 Flash 派发：组波、跑 worker、跑门禁。

worker 完成即把 brief 写进 journal，门禁结论经 on_gate 回调记帐：崩在波次
中间时，已完成的 assignment 有据可查，续跑只需补跑缺失的那几个。
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from runtime.lab.accept import AcceptResult, NO_HARD_CRITERIA, PASS, TEST_INVALID, sanity_and_run
from runtime.lab.effects import EffectLedger
from runtime.lab.helpers import step_gate
from runtime.lab.journal import Journal
from runtime.lab.persist import (
    SPEC_FILE,
    audit_offset,
    changed_files_from_audit,
    read_text,
)
from runtime.lab.scheduler import permission_for_wave, run_wave
from runtime.lab.spec import Assignment, render_assignment
from runtime.loop import run_loop
from runtime.loop.parse import synthetic_brief
from runtime.observe import spans as S
from runtime.task import RuntimeTask, TaskKind
from runtime.lab.execution import Attempt
from tools.sandbox_tools import is_sandbox_unreachable

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


async def run_gate(runner: Any, session_dir: Path, assignment_id: str) -> AcceptResult:
    with runner.tracer.span(
        S.ACCEPT, kind=S.KIND_EVALUATOR, **{S.ATTR_ASSIGNMENT_ID: assignment_id}
    ) as span:
        gate = await sanity_and_run(session_dir, assignment_id, settings=runner.settings)
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


async def run_step(
    runner: Any,
    assignments: list[Assignment],
    *,
    question: str,
    step_goal: str,
    session_dir: Path,
    ledger: EffectLedger,
    progress: list[tuple[str, str]],
    on_progress: Callable[[str, str], None],
    journal: Journal,
    step: int,
    on_gate: Callable[[str, str], None],
    on_event: EventSink | None,
) -> tuple[list[dict[str, Any]], bool, AcceptResult]:
    permission = permission_for_wave(len(assignments))
    workers = [
        RuntimeTask(
            kind=TaskKind.WORKER,
            permission=permission,
            step_budget=runner.settings.flash_step_budget,
            deadline=runner.clock() + runner.settings.task_wall_time_s,
            node_spec=a.to_dict(),
        )
        for a in assignments
    ]

    def commit(worker: RuntimeTask, brief: dict[str, Any]) -> None:
        assignment = Assignment.from_payload(worker.node_spec)
        gate_state = (brief.get("tests") or {}).get("state")
        if brief.get("outcome") == "done" and gate_state in {PASS, NO_HARD_CRITERIA}:
            ledger.record(assignment.id, brief)
        else:
            ledger.drop(assignment.id)
        if gate_state:
            on_gate(assignment.gate_id, gate_state)
        journal.append("brief", {"step": step, "aid": assignment.id, "brief": brief})
        on_progress(assignment.id, str(brief.get("brief") or ""))

    domains = [a.domain or a.goal for a in assignments]
    done_so_far = list(progress)

    with runner.tracer.span(
        S.STEP,
        kind=S.KIND_CHAIN,
        **{
            S.ATTR_WAVE_SIZE: len(workers),
            S.ATTR_PERMISSION: permission.value,
            S.ATTR_STEP_GOAL: step_goal,
        },
    ):
        results = await run_wave(
            workers,
            lambda w: run_worker(
                runner,
                w,
                question=question,
                step_goal=step_goal,
                session_dir=session_dir,
                done_so_far=done_so_far,
                domains=domains,
                journal=journal,
                on_event=on_event,
            ),
            runner.settings,
            tracer=runner.tracer,
            on_result=commit,
        )

    briefs = []
    spec_invalid = False
    for worker, brief in results:
        briefs.append(brief)
        aid = str((worker.node_spec or {}).get("id") or "")
        if not aid:
            continue
        if brief.get("outcome") == "spec_invalid":
            spec_invalid = True

    return briefs, spec_invalid, step_gate(results)


async def run_worker(
    runner: Any,
    worker: RuntimeTask,
    *,
    question: str,
    step_goal: str,
    session_dir: Path,
    done_so_far: list[tuple[str, str]],
    domains: list[str],
    journal: Journal,
    on_event: EventSink | None,
) -> dict[str, Any]:
    worker.deadline = runner.clock() + runner.settings.task_wall_time_s
    audit_mark = audit_offset(runner.settings.workspace_dir)

    assignment = Assignment.from_payload(worker.node_spec or {})
    others = [d for d in domains if d and d !=
              (assignment.domain or assignment.goal)]
    prompt = render_assignment(
        assignment,
        user_request=question,
        project_spec=read_text(session_dir, SPEC_FILE),
        step_goal=step_goal,
        done_so_far=done_so_far,
        parallel_domains=others if len(domains) > 1 else None,
    )

    with runner.tracer.span(
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
    ) as task_span:
        await runner._emit(on_event, {"kind": "node_start", "node": f"flash:{assignment.id}"})
        with Attempt(journal, worker).scope() as attempt:
            result = attempt.result = await run_loop(
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
        return brief
