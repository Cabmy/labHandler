"""生命周期、取消与崩溃续跑回归；临时 Journal，无真实 LLM / Docker。"""

import asyncio
from types import SimpleNamespace

import pytest

from runtime.lab import flow
from runtime.lab.accept import AcceptResult, FAIL, PASS
from runtime.lab.effects import EffectLedger
from runtime.lab.execution import Attempt, RunPaused
from runtime.lab.journal import JOURNAL_FILE, Journal
from runtime.lab.persist import latest_incomplete, session_dir
from runtime.lab.runner import LabRunner
from runtime.lab.scheduler import run_wave
from runtime.lab.spec import Assignment
from runtime.lab.step import run_step
from runtime.loop.cycle import LoopResult
from runtime.loop.registry import ToolRegistry
from runtime.task import Permission, RuntimeTask, TaskKind


def task(kind=TaskKind.WORKER):
    return RuntimeTask(kind=kind, permission=Permission.PRO, step_budget=5, deadline=100)


def opened(path):
    journal = Journal.open(path)
    journal.append("begin", {"version": 1, "question": "test lab"})
    return journal


def submit(journal, kind, name, payload):
    with Attempt(journal, task(kind)).scope() as attempt:
        attempt.result = LoopResult(None, {"name": name, "payload": payload}, "submit")


@pytest.fixture
def runner(tmp_path):
    settings = SimpleNamespace(workspace_dir=tmp_path, task_wall_time_s=60,
                               pro_step_budget=5, flash_step_budget=5,
                               max_parallel_readonly_workers=2)
    value = LabRunner(settings, object(), registry=ToolRegistry([]))
    value._agent_spec = lambda *args: SimpleNamespace(name="test")
    return value


def test_recovery_never_infers_running_from_disk(tmp_path):
    directory = session_dir(tmp_path, "lab")
    journal = opened(directory)
    journal.append("run_started", {})
    journal.append("attempt_started", {"task_id": "crashed", "kind": "worker"})
    tid, state = latest_incomplete(tmp_path)
    assert tid == "lab"
    assert state.status() == "PAUSED"
    assert state.pause_reason == "interrupted"
    assert state.status(running=True) == "RUNNING"
    # 未结束 attempt 不会让已收尾会话复活。
    journal.append("session_closed", {"result": {"verdict": "need_user"}})
    assert journal.replay().status() == "FINISHED"
    assert latest_incomplete(tmp_path) is None


def test_torn_tail_does_not_eat_next_event(tmp_path):
    journal = opened(tmp_path)
    with journal.path.open("ab") as stream:
        stream.write(b'{"kind":"session_closed","payload":')
    assert journal.replay().resumable
    recovered = Journal.open(tmp_path)
    recovered.append("run_paused", {"reason": "user_stop"})
    assert recovered.replay().pause_reason == "user_stop"
    assert recovered.path.read_bytes().endswith(b"\n")


def test_committed_corruption_and_old_format_are_rejected(tmp_path):
    journal = opened(tmp_path)
    with journal.path.open("ab") as stream:
        stream.write(b'invalid\n')
    with pytest.raises(ValueError):
        journal.replay()
    old = Journal.open(tmp_path / "old")
    old.append("begin", {"question": "old"})
    with pytest.raises(ValueError, match="unsupported"):
        old.replay()


def test_latest_spec_uses_revision_and_ignores_failed_attempt(tmp_path):
    journal = opened(tmp_path)
    submit(journal, TaskKind.SPEC, "submit_spec", {"goal": "old"})
    submit(journal, TaskKind.SPEC, "submit_spec", {"goal": "revised"})
    with pytest.raises(RuntimeError):
        with Attempt(journal, task(TaskKind.SPEC)).scope():
            raise RuntimeError("provider down")
    state = journal.replay()
    assert state.latest_submit("submit_spec") == {"goal": "revised"}
    assert len(state.attempts) == 3
    assert all("end" in attempt for attempt in state.attempts.values())


def test_stop_cancels_current_work_and_can_resume(runner, tmp_path, monkeypatch):
    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def work(runner, question, directory, journal, state, **kwargs):
            with Attempt(journal, task(TaskKind.SPEC)).scope():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        monkeypatch.setattr("runtime.lab.runner.run_lab", work)
        running = asyncio.create_task(runner.run("test", tmp_path))
        await entered.wait()
        assert runner.running
        with pytest.raises(RuntimeError, match="already running"):
            await runner.run("duplicate", tmp_path)
        runner.request_stop()
        runner.request_stop()  # 重复 stop 不打断清理。
        result = await asyncio.wait_for(running, 1)
        assert cleaned.is_set()
        assert result["verdict"] == "paused"
        assert not runner.running
        state = Journal(tmp_path / JOURNAL_FILE).replay()
        assert state.resumable and state.pause_reason == "user_stop"
        assert next(iter(state.attempts.values()))["end"]["error"] == "cancelled"

        async def finish(*args, **kwargs):
            assert kwargs["resume"]
            return {"verdict": "fail", "summary": "Honest report"}

        monkeypatch.setattr("runtime.lab.runner.run_lab", finish)
        assert (await runner.run("ignored", tmp_path, resume=True))["verdict"] == "fail"
        assert Journal(tmp_path / JOURNAL_FILE).replay().status() == "FINISHED"
        with pytest.raises(ValueError, match="not resumable"):
            await runner.run("again", tmp_path, resume=True)

    asyncio.run(run())


def test_external_cancellation_propagates_after_cleanup(runner, tmp_path, monkeypatch):
    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def work(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr("runtime.lab.runner.run_lab", work)
        outer = asyncio.create_task(runner.run("test", tmp_path))
        await entered.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert cleaned.is_set()
        assert not runner.running
        assert Journal(tmp_path / JOURNAL_FILE).replay().pause_reason == "cancelled"

    asyncio.run(run())


@pytest.mark.parametrize("exception", [RunPaused("spec_failed"), RuntimeError("broken")])
def test_all_incomplete_exits_stay_resumable(runner, tmp_path, monkeypatch, exception):
    async def broken(*args, **kwargs):
        raise exception

    monkeypatch.setattr("runtime.lab.runner.run_lab", broken)
    if isinstance(exception, RunPaused):
        assert asyncio.run(runner.run("test", tmp_path))["verdict"] == "paused"
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(runner.run("test", tmp_path))
    state = Journal(tmp_path / JOURNAL_FILE).replay()
    assert state.resumable and state.result is None
    assert not runner.running


def test_wave_commits_exceptions_and_cancels_queued_work():
    async def run():
        workers = [task() for _ in range(3)]
        called, committed = [], []

        async def work(worker):
            called.append(worker.task_id)
            if worker is workers[0]:
                raise RuntimeError("tool crashed")
            if worker is workers[1]:
                return {"outcome": "spec_invalid"}
            await asyncio.Event().wait()

        results = await asyncio.wait_for(run_wave(
            workers, work, SimpleNamespace(max_parallel_readonly_workers=1),
            on_result=lambda worker, brief: committed.append((worker.task_id, brief)),
        ), 1)
        assert len(results) == len(committed) == 3
        assert results[0][1]["outcome"] == "failed"
        assert results[1][1]["outcome"] == "spec_invalid"
        assert results[2][1]["outcome"] == "blocked"

    asyncio.run(run())


def test_cancel_wave_drains_children_without_committing_unfinished_work():
    async def run():
        workers = [task() for _ in range(4)]
        entered, cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        committed, calls = [], []

        async def work(worker):
            calls.append(worker.task_id)
            if worker is workers[0]:
                return {"outcome": "done"}
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()

        wave = asyncio.create_task(run_wave(
            workers, work, SimpleNamespace(max_parallel_readonly_workers=1),
            on_result=lambda worker, brief: committed.append(worker.task_id),
        ))
        await entered.wait()
        wave.cancel()
        await cleanup_started.wait()
        assert not wave.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await wave
        assert committed == [workers[0].task_id]
        assert calls == [workers[0].task_id, workers[1].task_id]

    asyncio.run(run())


def test_commit_failure_propagates_instead_of_faking_worker_failure():
    async def work(worker):
        return {"outcome": "done"}

    def commit(*args):
        raise OSError("journal is full")

    with pytest.raises(OSError, match="journal is full"):
        asyncio.run(run_wave([task()], work, SimpleNamespace(max_parallel_readonly_workers=1), on_result=commit))


@pytest.mark.parametrize("gate_state", [PASS, FAIL])
def test_worker_execution_return_and_gate_result_are_separate(runner, tmp_path, monkeypatch, gate_state):
    journal = opened(tmp_path)

    async def loop(*args, **kwargs):
        brief = {"outcome": "done", "brief": "produced", "changed_files": []}
        return LoopResult(brief, brief, "submit_brief")

    async def gate(*args):
        return AcceptResult(state=gate_state)

    monkeypatch.setattr("runtime.lab.step.run_loop", loop)
    monkeypatch.setattr("runtime.lab.step.run_gate", gate)
    results = asyncio.run(run_step(
        runner, [Assignment.from_payload({"id": "a", "goal": "work"})],
        question="test", step_goal="work", session_dir=tmp_path,
        ledger=EffectLedger({}, journal, tmp_path), progress=[],
        on_progress=lambda *args: None, journal=journal, step=1,
        on_gate=lambda *args: None, on_event=None,
    ))
    state = journal.replay()
    ending = next(iter(state.attempts.values()))["end"]
    assert ending["reason"] == "submit_brief" and "error" not in ending
    assert state.step_briefs[1]["a"]["tests"]["state"] == gate_state
    assert results[2].state == gate_state


def test_summary_failure_resumes_summary_and_closes_only_after_file_write(runner, tmp_path, monkeypatch):
    calls = []
    summary_ok = False

    async def begin(runner, question, directory, journal, state, on_event, resume):
        st = flow.LabState(runner=runner, question=question, session_dir=directory,
                           on_event=on_event, resume=resume, journal=journal,
                           ledger=EffectLedger({}, journal, tmp_path), catalog="")
        if state:
            st.restore(state)
        return st

    async def skip(st):
        calls.append("product")

    async def drive(st, kind, user, label):
        calls.append(kind.value)
        if not summary_ok:
            return {}
        return {"name": "submit_summary", "payload": {"user_summary": "完成情况"}}

    monkeypatch.setattr(flow, "_begin", begin)
    monkeypatch.setattr(flow, "remember", skip)
    monkeypatch.setattr(flow, "write_spec", skip)
    monkeypatch.setattr(flow, "advance", skip)
    monkeypatch.setattr(flow.LabState, "drive_pro", drive)
    result = asyncio.run(runner.run("test", tmp_path))
    assert result["verdict"] == "paused" and result["reason"] == "summary_failed"
    state = Journal(tmp_path / JOURNAL_FILE).replay()
    assert state.resumable and state.stage == "summary"
    assert not (tmp_path / "SUMMARY.md").exists()
    summary_ok = True
    calls.clear()
    asyncio.run(runner.run("test", tmp_path, resume=True))
    assert calls == ["summary"]
    assert (tmp_path / "SUMMARY.md").read_text() == "完成情况"
    assert Journal(tmp_path / JOURNAL_FILE).replay().status() == "FINISHED"


def test_completed_summary_is_recovered_without_calling_model(runner, tmp_path, monkeypatch):
    journal = opened(tmp_path)
    submit(journal, TaskKind.SUMMARY, "submit_summary", {"user_summary": "saved"})
    st = flow.LabState(runner=runner, question="test", session_dir=tmp_path,
                       on_event=None, resume=True, journal=journal,
                       ledger=EffectLedger({}, journal, tmp_path), catalog="", stage="summary")

    async def unexpected(*args):
        pytest.fail("completed summary must not call the model again")

    monkeypatch.setattr(flow.LabState, "drive_pro", unexpected)
    assert asyncio.run(flow.summarize(st))["summary"] == "saved"
    assert (tmp_path / "SUMMARY.md").read_text() == "saved"


@pytest.mark.parametrize("kind", [TaskKind.SPEC, TaskKind.DISPATCH, TaskKind.JUDGE,
                                 TaskKind.TAKEOVER, TaskKind.REMEMBER_JUDGE, TaskKind.SUMMARY])
def test_pro_failure_pauses_at_execution_boundary(runner, tmp_path, monkeypatch, kind):
    journal = opened(tmp_path)
    st = flow.LabState(runner=runner, question="test", session_dir=tmp_path,
                       on_event=None, resume=False, journal=journal,
                       ledger=EffectLedger({}, journal, tmp_path), catalog="")

    async def failed(*args, **kwargs):
        return LoopResult(None, None, "auth")

    monkeypatch.setattr(flow, "run_loop", failed)
    with pytest.raises(RunPaused, match=f"{kind.value}:auth"):
        asyncio.run(st.drive_pro(kind, "test", "test"))
    state = journal.replay()
    assert next(iter(state.attempts.values()))["end"]["reason"] == "auth"
    assert state.result is None


def test_summary_write_failure_does_not_close_session(runner, tmp_path, monkeypatch):
    journal = opened(tmp_path)
    journal.append("stage", {"stage": "summary"})
    submit(journal, TaskKind.SUMMARY, "submit_summary", {"user_summary": "saved"})
    (tmp_path / "CATALOG.md").write_text("catalog")
    original = flow.write_text

    def fail_summary(directory, name, content):
        if name == "SUMMARY.md":
            raise OSError("disk full")
        return original(directory, name, content)

    monkeypatch.setattr(flow, "write_text", fail_summary)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(runner.run("test", tmp_path, resume=True))
    assert journal.replay().resumable
    monkeypatch.setattr(flow, "write_text", original)
    assert asyncio.run(runner.run("test", tmp_path, resume=True))["summary"] == "saved"
    assert not journal.replay().resumable


def test_pipeline_resume_keeps_dispatch_and_retries_only_interrupted_worker(runner, tmp_path, monkeypatch):
    from runtime.context.notes import write_cards
    (tmp_path / "CATALOG.md").write_text("catalog")
    write_cards(tmp_path, [])
    monkeypatch.setattr(flow, "load_profile", lambda: {})
    monkeypatch.setattr(flow, "catalog_rules", lambda profile: [])
    calls = []

    async def run():
        entered = asyncio.Event()
        resumed = False

        async def loop(node, spec, **kwargs):
            calls.append(node.kind)
            if node.kind is TaskKind.WORKER:
                if not resumed:
                    entered.set()
                    await asyncio.Event().wait()
                brief = {"outcome": "done", "brief": "done", "changed_files": []}
                return LoopResult(brief, brief, "submit_brief")
            payloads = {
                TaskKind.SPEC: {"goal": "produce", "milestones": ["produce output"]},
                TaskKind.DISPATCH: {"step_goal": "produce", "assignments": [
                    {"id": "a", "goal": "produce", "domain": "product"}]},
                TaskKind.JUDGE: {"decision": "finish", "rule_verdicts": []},
                TaskKind.SUMMARY: {"user_summary": "done"},
            }
            return LoopResult(None, {"name": f"submit_{node.kind.value}",
                                     "payload": payloads[node.kind]}, "submit",
                              history=kwargs.get("history") or [])

        async def gate(*args):
            return AcceptResult(state=PASS)

        monkeypatch.setattr(flow, "run_loop", loop)
        monkeypatch.setattr("runtime.lab.step.run_loop", loop)
        monkeypatch.setattr("runtime.lab.step.run_gate", gate)
        running = asyncio.create_task(runner.run("test", tmp_path))
        await asyncio.wait_for(entered.wait(), 1)
        runner.request_stop()
        assert (await running)["verdict"] == "paused"
        before = Journal(tmp_path / JOURNAL_FILE).replay()
        assert 1 in before.step_dispatch and not before.step_briefs
        resumed = True
        assert (await runner.run("continue", tmp_path, resume=True))["verdict"] == "pass"
        assert calls == [TaskKind.SPEC, TaskKind.DISPATCH, TaskKind.WORKER,
                         TaskKind.WORKER, TaskKind.JUDGE, TaskKind.SUMMARY]
        state = Journal(tmp_path / JOURNAL_FILE).replay()
        assert state.result["summary"] == "done"
        assert len(state.attempts) == 6
        assert all("end" in attempt for attempt in state.attempts.values())
        assert not (tmp_path / "STATE.json").exists()

    asyncio.run(run())
