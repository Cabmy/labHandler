"""Task 树解释器：ingest → plan → dispatch → judge → summary。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.prompts import (
    FLASH_SYSTEM,
    JUDGE_SYSTEM,
    PLAN_SYSTEM,
    SUMMARY_SYSTEM,
    TAKEOVER_SYSTEM,
)
from config.runtime import RuntimeSettings
from memory.profile import inject_for_agent
from runtime.accept import sanity_and_run
from runtime.context.disclosure import skill_system_block
from runtime.errors import ErrorClass
from runtime.llm import LLMGateway
from runtime.loop import AgentSpec, run_loop
from runtime.observe.tracer import Tracer
from runtime.observe import spans as S
from runtime.persist import (
    append_notes,
    changed_files_from_audit,
    read_text,
    save_tree,
    write_text,
)
from runtime.schema_call import (
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_PLAN,
    SUBMIT_SUMMARY,
    synthetic_brief,
)
from runtime.scheduler import permission_for_wave, run_wave
from runtime.task import (
    Permission,
    RuntimeTask,
    TaskKind,
    TaskStatus,
    TaskTree,
)
from runtime.tools import ToolContext, ToolRegistry, build_registry
from tools.sandbox_tools import reset_sandbox_failure_counter, sandbox_convert_to_markdown
from tools.workspace_utils import iter_workspace_files


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

    def request_stop(self) -> None:
        self._cancel.set()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    async def _emit(self, sink: EventSink | None, payload: dict[str, Any]) -> None:
        if sink:
            await sink(payload)

    async def ingest(self, session_dir: Path) -> str:
        ws = self.settings.workspace_dir
        lines = ["# Materials", ""]
        if not ws.exists():
            write_text(session_dir, "MATERIALS.md", "# Materials\n\n(empty workspace)\n")
            return read_text(session_dir, "MATERIALS.md")
        parseable = {".pdf", ".docx", ".pptx"}
        text_ext = {".md", ".txt", ".py", ".json", ".yml", ".yaml", ".toml", ".csv", ".rst"}
        for p in iter_workspace_files(ws, max_files=40):
            rel = p.relative_to(ws)
            suf = p.suffix.lower()
            lines.append(f"## {rel}")
            if suf in parseable:
                try:
                    md = await sandbox_convert_to_markdown(str(p))
                    lines.append(md[:12000])
                except Exception as e:
                    lines.append(f"(parse failed: {type(e).__name__}: {e})")
            elif suf in text_ext:
                lines.append("```")
                lines.append(p.read_text(encoding="utf-8", errors="replace")[:8000])
                lines.append("```")
            else:
                lines.append(f"(binary, {p.stat().st_size} bytes)")
            lines.append("")
        text = "\n".join(lines)
        write_text(session_dir, "MATERIALS.md", text)
        return text

    def _spec(self, name: str, permission: Permission, system: str, submit: str) -> AgentSpec:
        model = self.settings.pro_model if name == "pro" else self.settings.flash_model
        system = inject_for_agent(name, system)
        skills = skill_system_block()
        if skills:
            system = system + "\n\n" + skills
        return AgentSpec(name=name, model=model, permission=permission, system=system, submit_tool=submit)

    def _ready(self, nodes: list[dict[str, Any]], done: set[str], failed: set[str]) -> list[dict[str, Any]]:
        ready = []
        for n in nodes:
            deps = list(n.get("depends_on") or [])
            if n["id"] in done or n["id"] in failed:
                continue
            if any(d in failed for d in deps):
                failed.add(n["id"])
                continue
            if all(d in done for d in deps):
                ready.append(n)
        return ready

    async def run(
        self,
        question: str,
        session_dir: Path,
        tree: TaskTree,
        *,
        on_event: EventSink | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        root = tree.get(tree.root_id)
        if root.status is TaskStatus.PENDING:
            root.transit(TaskStatus.RUNNING)
        save_tree(session_dir, tree)
        materials = read_text(session_dir, "MATERIALS.md")
        if not materials:
            materials = await self.ingest(session_dir)

        knowledge_cards: list[dict[str, Any]] = []
        summary_text = ""
        last_gate = "no_hard_criteria"

        async def run_pro(system: str, submit: str, user: str, node_kind: TaskKind) -> dict[str, Any]:
            child = tree.add_child(
                root.task_id,
                kind=node_kind,
                permission=Permission.PRO,
                step_budget=self.settings.pro_step_budget,
                deadline=self.clock() + self.settings.task_wall_time_s,
            )
            child.transit(TaskStatus.RUNNING)
            reset_sandbox_failure_counter()
            spec = self._spec("pro", Permission.PRO, system, submit)
            result = await run_loop(
                child,
                spec,
                settings=self.settings,
                llm=self.llm,
                registry=self.registry,
                session_dir=session_dir,
                user_input=user,
                plan=read_text(session_dir, "PLAN.md"),
                notes=read_text(session_dir, "NOTES.md"),
                on_event=on_event,
                tracer=self.tracer,
                clock=self.clock,
            )
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

        # Plan
        plan_user = (
            f"User request:\n{question}\n\n## MATERIALS.md\n{materials[:16000]}\n\n"
            "Call write_acceptance for each test file, then submit_plan. "
            "Nodes that write files must be chained with depends_on so each write wave has size 1."
        )
        await self._emit(on_event, {"kind": "node_start", "node": "plan"})
        plan_submit = await run_pro(PLAN_SYSTEM, SUBMIT_PLAN, plan_user, TaskKind.PLAN)
        payload = plan_submit.get("payload") if plan_submit.get("name") == SUBMIT_PLAN else plan_submit
        nodes = list((payload or {}).get("nodes") or [])
        write_text(
            session_dir,
            "PLAN.md",
            json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2),
        )
        await self._emit(on_event, {"kind": "node_done", "node": "plan", "log": [{"nodes": len(nodes)}]})

        done: set[str] = set()
        failed: set[str] = set()
        # resume: skip completed worker nodes
        if resume:
            for t in tree.nodes.values():
                spec_id = (t.node_spec or {}).get("id")
                if t.kind is TaskKind.WORKER and t.status is TaskStatus.COMPLETED and spec_id:
                    tests_ok = (t.brief or {}).get("tests", {}).get("exit_code") == 0
                    if tests_ok or (t.brief or {}).get("outcome") == "done":
                        done.add(spec_id)

        while True:
            if self._cancelled():
                root.transit(TaskStatus.CANCELLED)
                save_tree(session_dir, tree)
                return {"verdict": "cancelled", "summary": summary_text}

            ready = self._ready(nodes, done, failed)
            if not ready:
                break

            perm = permission_for_wave(len(ready))
            workers: list[RuntimeTask] = []
            for spec in ready:
                w = tree.add_child(
                    root.task_id,
                    kind=TaskKind.WORKER,
                    permission=perm,
                    step_budget=self.settings.flash_step_budget,
                    deadline=self.clock() + self.settings.task_wall_time_s,
                    node_spec=spec,
                )
                workers.append(w)
            save_tree(session_dir, tree)

            async def run_worker(w: RuntimeTask) -> dict[str, Any]:
                if w.status is TaskStatus.PENDING:
                    w.transit(TaskStatus.RUNNING)
                reset_sandbox_failure_counter()
                spec = w.node_spec
                user = (
                    f"Goal: {spec.get('name')}\n{spec.get('desc')}\n"
                    f"Expected artifacts: {spec.get('expected_artifacts')}\n"
                    f"User request: {question}\n"
                    "Finish by calling submit_brief. Transient errors are not plan_invalid."
                )
                flash_spec = self._spec("flash", w.permission, FLASH_SYSTEM, SUBMIT_BRIEF)
                await self._emit(on_event, {"kind": "node_start", "node": f"flash:{spec.get('id')}"})
                try:
                    result = await asyncio.wait_for(
                        run_loop(
                            w,
                            flash_spec,
                            settings=self.settings,
                            llm=self.llm,
                            registry=self.registry,
                            session_dir=session_dir,
                            user_input=user,
                            plan=read_text(session_dir, "PLAN.md"),
                            notes=read_text(session_dir, "NOTES.md"),
                            on_event=on_event,
                            tracer=self.tracer,
                            clock=self.clock,
                        ),
                        timeout=max(1.0, w.deadline - self.clock()),
                    )
                except asyncio.TimeoutError:
                    result_brief = synthetic_brief(
                        outcome="failed",
                        brief="Your task was stopped because the execution budget was exhausted.",
                        changed_files=changed_files_from_audit(self.settings.workspace_dir),
                    )
                    try:
                        w.transit(TaskStatus.CANCELLED)
                    except ValueError:
                        pass
                    w.brief = result_brief
                    return result_brief
                brief = result.brief or synthetic_brief(
                    outcome="failed", brief=result.reason or "no brief"
                )
                changed = brief.get("changed_files") or changed_files_from_audit(
                    self.settings.workspace_dir
                )
                brief["changed_files"] = changed
                gate = await sanity_and_run(session_dir, str(spec.get("id")))
                brief["tests"] = gate.as_tests()
                w.brief = brief
                try:
                    if brief.get("outcome") == "done" and gate.state == "pass":
                        w.transit(TaskStatus.COMPLETED)
                    elif w.status is TaskStatus.RUNNING:
                        w.transit(TaskStatus.FAILED if brief.get("outcome") == "failed" else TaskStatus.COMPLETED)
                except ValueError:
                    pass
                await self._emit(
                    on_event,
                    {
                        "kind": "worker_brief",
                        "task_id": spec.get("id"),
                        "outcome": brief.get("outcome"),
                        "brief": brief.get("brief"),
                        "tests": brief.get("tests"),
                        "gate": gate.state,
                    },
                )
                save_tree(session_dir, tree)
                return brief

            wave = await run_wave(workers, run_worker, self.settings)
            briefs = []
            plan_invalid = False
            for w, brief in wave:
                briefs.append(brief)
                nid = (w.node_spec or {}).get("id")
                if brief.get("outcome") == "plan_invalid":
                    plan_invalid = True
                    if nid:
                        failed.add(nid)
                elif brief.get("outcome") == "done":
                    if nid:
                        done.add(nid)
                else:
                    if nid:
                        failed.add(nid)

            last_gate_obj = await sanity_and_run(
                session_dir, str((ready[0] or {}).get("id") or "root")
            ) if ready else None
            last_gate = last_gate_obj.state if last_gate_obj else "no_hard_criteria"

            judge_user = (
                f"User request: {question}\nGate: {last_gate}\n"
                f"Briefs:\n{json.dumps(briefs, ensure_ascii=False)[:12000]}\n"
                "If gate is no_hard_criteria you MUST say so in evidence and give a semantic rationale."
            )
            await self._emit(on_event, {"kind": "node_start", "node": "judge"})
            judge = await run_pro(JUDGE_SYSTEM, SUBMIT_JUDGE, judge_user, TaskKind.JUDGE)
            jpayload = judge.get("payload") if judge.get("name") == SUBMIT_JUDGE else judge
            decision = str((jpayload or {}).get("decision") or "new_plan")
            append_notes(session_dir, str((jpayload or {}).get("notes_append") or ""))
            await self._emit(
                on_event,
                {"kind": "node_done", "node": "judge", "log": [{"decision": decision}]},
            )

            if decision == "accept" or decision == "finish":
                break
            if decision == "takeover":
                await self._emit(on_event, {"kind": "pro_takeover", "reason": "judge"})
                take_user = (
                    f"Take over the failing work.\nUser: {question}\n"
                    f"Briefs: {json.dumps(briefs, ensure_ascii=False)[:8000]}"
                )
                await run_pro(TAKEOVER_SYSTEM, SUBMIT_BRIEF, take_user, TaskKind.WORKER)
                # mark remaining ready ids done so we re-judge via another loop? treat as done current wave
                for spec in ready:
                    done.add(spec["id"])
                continue
            if decision == "new_plan" or plan_invalid:
                redo = await run_pro(
                    PLAN_SYSTEM,
                    SUBMIT_PLAN,
                    plan_user + "\n\nRevise the plan given the briefs:\n" + json.dumps(briefs, ensure_ascii=False)[:8000],
                    TaskKind.PLAN,
                )
                payload = redo.get("payload") if redo.get("name") == SUBMIT_PLAN else redo
                nodes = list((payload or {}).get("nodes") or nodes)
                write_text(session_dir, "PLAN.md", json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2))
                done.clear()
                failed.clear()
                continue
            break

        sum_user = (
            f"User request: {question}\nPlan:\n{read_text(session_dir, 'PLAN.md')}\n"
            f"Notes:\n{read_text(session_dir, 'NOTES.md')}\n"
            f"Gate: {last_gate}\n"
            "Call submit_summary. If no_hard_criteria, say so in the user_summary."
        )
        await self._emit(on_event, {"kind": "node_start", "node": "summary"})
        summarized = await run_pro(SUMMARY_SYSTEM, SUBMIT_SUMMARY, sum_user, TaskKind.PLAN)
        spayload = summarized.get("payload") if summarized.get("name") == SUBMIT_SUMMARY else summarized
        summary_text = str((spayload or {}).get("user_summary") or "")
        knowledge_cards = list((spayload or {}).get("knowledge_cards") or [])
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
            "verdict": last_gate,
            "summary": summary_text,
            "knowledge_cards": knowledge_cards,
            "question": question,
        }
