"""暂停后的用户补充是持久化的请求修订：先到 Pro，再到新派发的 Flash。"""

import asyncio
from types import SimpleNamespace

import pytest

from config.prompts import render_user_instruction
from runtime.context.notes import write_cards
from runtime.lab import flow
from runtime.lab.accept import AcceptResult, PASS
from runtime.lab.execution import Attempt, RunPaused
from runtime.lab.journal import JOURNAL_FILE, Journal
from runtime.lab.runner import LabRunner
from runtime.lab.persist import session_dir
from runtime.loop.cycle import LoopResult
from runtime.loop.registry import ToolRegistry
from runtime.task import Permission, RuntimeTask, TaskKind
from runtime.session import LabSession


@pytest.fixture
def runner(tmp_path):
    settings = SimpleNamespace(workspace_dir=tmp_path, task_wall_time_s=60,
                               pro_step_budget=5, flash_step_budget=5,
                               max_parallel_readonly_workers=2)
    value = LabRunner(settings, object(), registry=ToolRegistry([]))
    value._agent_spec = lambda *args: SimpleNamespace(name="test")
    return value


def opened(tmp_path):
    journal = Journal.open(tmp_path)
    journal.append("begin", {"version": 1, "question": "完成这个 lab"})
    journal.append_transcript([{"role": "user", "content": "完成这个 lab"}], False)
    return journal


def spec(journal, goal):
    task = RuntimeTask(kind=TaskKind.SPEC, permission=Permission.PRO, step_budget=5, deadline=100)
    with Attempt(journal, task).scope() as attempt:
        attempt.result = LoopResult(None, {"name": "submit_spec", "payload": {"goal": goal}}, "submit")


def test_instruction_replay_preserves_history_but_invalidates_old_plan(tmp_path):
    journal = opened(tmp_path)
    spec(journal, "old")
    journal.append("stage", {"stage": "advance", "dispatch_step": 2})
    journal.append("progress", {"aid": "a", "text": "already produced"})
    journal.append("dispatch", {"step": 3, "payload": {"stale": True}})
    journal.append("brief", {"step": 3, "aid": "a", "brief": {"outcome": "done"}})
    journal.append("judge", {"step": 3, "decision": "finish"})
    journal.append("executed", {"ids": ["a"]})
    journal.append("effect_record", {"node_id": "a", "prints": {"file_write": {"files": {"x": "old"}}}})
    journal.append("gate", {"gate_id": "a", "state": "pass"})
    journal.append("run_gate", {"state": "pass"})
    journal.append("halt", {"reason": "missing information"})
    product = tmp_path / "product.txt"
    product.write_text("keep this")
    journal.append("user_instruction", {"text": "改用 C++"})
    state = journal.replay()
    assert state.question == "完成这个 lab"
    assert state.instructions == ["改用 C++"]
    assert state.transcript[-1] == {"role": "user", "content": render_user_instruction("改用 C++"), "pinned": True}
    assert state.stage == "spec" and state.dispatch_step == 0
    assert state.latest_submit("submit_spec") is None
    assert not any([state.step_dispatch, state.step_briefs, state.step_judges, state.executed_ids, state.effects, state.gates, state.halt, state.run_gate])
    assert state.progress == [("a", "already produced")]
    assert product.read_text() == "keep this"
    assert len(state.attempts) == 1  # historical attempts remain auditable
    spec(journal, "new")
    assert journal.replay().latest_submit("submit_spec") == {"goal": "new"}


def test_multiple_instructions_survive_snapshot_and_remain_ordered(tmp_path):
    journal = opened(tmp_path)
    journal.append("user_instruction", {"text": "改用 C++"})
    state = journal.replay()
    journal.sync_tx(len(state.transcript))
    journal.append_transcript(state.transcript, True)
    journal.append("user_instruction", {"text": "输出 JSON"})
    state = journal.replay()
    assert state.instructions == ["改用 C++", "输出 JSON"]
    assert state.plan_revision == 2
    assert state.current_request.index("完成这个 lab") < state.current_request.index("改用 C++") < state.current_request.index("输出 JSON")
    assert len(state.transcript) == 3
    assert sum("改用 C++" in message["content"] for message in state.transcript) == 1
    assert sum("输出 JSON" in message["content"] for message in state.transcript) == 1


def test_cancel_before_agent_receives_update_then_empty_resume_delivers_once(runner, tmp_path, monkeypatch):
    journal = opened(tmp_path)
    captured = []

    async def exercise():
        entered = asyncio.Event()

        async def pause(runner, question, directory, journal, state, **kwargs):
            assert state.instructions == ["改用 C++"]
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("runtime.lab.runner.run_lab", pause)
        task = asyncio.create_task(runner.resume(tmp_path, instruction="  改用 C++\n"))
        await entered.wait()
        runner.request_stop()
        assert (await task)["verdict"] == "paused"

        async def receive(runner, question, directory, journal, state, **kwargs):
            captured.append((question, state.transcript))
            raise RunPaused("test_pause")

        monkeypatch.setattr("runtime.lab.runner.run_lab", receive)
        assert (await runner.resume(tmp_path))["verdict"] == "paused"

    asyncio.run(exercise())
    assert "改用 C++" in captured[0][0]
    assert sum("改用 C++" in message["content"] for message in captured[0][1]) == 1
    rows = [kind for kind, _ in journal._iter_events()]
    assert rows.count("user_instruction") == 1


def test_empty_resume_keeps_existing_spec_and_dispatch(runner, tmp_path, monkeypatch):
    journal = opened(tmp_path)
    spec(journal, "old")
    journal.append("stage", {"stage": "advance"})
    journal.append("dispatch", {"step": 1, "payload": {"existing": True}})

    async def receive(runner, question, directory, journal, state, **kwargs):
        assert state.stage == "advance"
        assert state.latest_submit("submit_spec") == {"goal": "old"}
        assert state.step_dispatch[1]["payload"] == {"existing": True}
        assert not state.instructions
        raise RunPaused("test_pause")

    monkeypatch.setattr("runtime.lab.runner.run_lab", receive)
    asyncio.run(runner.resume(tmp_path, instruction=" \n "))


def test_session_resume_persists_update_without_creating_another_thread(runner, tmp_path, monkeypatch):
    directory = session_dir(tmp_path, "original-lab")
    journal = opened(directory)
    session = object.__new__(LabSession)
    session.runner = runner
    session.settings = runner.settings
    session.tracer = runner.tracer

    async def receive(runner, question, directory, journal, state, **kwargs):
        assert state.instructions == ["改用 C++"]
        raise RunPaused("test_pause")

    monkeypatch.setattr("runtime.lab.runner.run_lab", receive)
    result = asyncio.run(session.resume("original-lab", instruction="改用 C++"))
    assert result["verdict"] == "paused"
    assert session.thread_id == "original-lab"
    assert journal.replay().instructions == ["改用 C++"]
    assert [path.name for path in directory.parent.iterdir()] == ["original-lab"]


def test_update_before_initial_pro_thread_is_in_seeded_request(runner, tmp_path, monkeypatch):
    journal = Journal.open(tmp_path)
    journal.append("begin", {"version": 1, "question": "完成 lab"})
    journal.append("user_instruction", {"text": "改用 C++"})
    state = journal.replay()
    assert not state.transcript
    (tmp_path / "CATALOG.md").write_text("catalog")
    st = asyncio.run(flow._begin(runner, state.current_request, tmp_path, journal, state, None, True))
    flow._seed_pro_thread(st)
    assert "完成 lab" in st.pro_history[0]["content"]
    assert "改用 C++" in st.pro_history[0]["content"]


def test_paused_worker_update_replans_and_reaches_pro_and_flash(runner, tmp_path, monkeypatch):
    (tmp_path / "CATALOG.md").write_text("catalog")
    write_cards(tmp_path, [])
    monkeypatch.setattr(flow, "load_profile", lambda: {})
    monkeypatch.setattr(flow, "catalog_rules", lambda profile: [])
    calls = []
    product = tmp_path / "solution.txt"

    async def exercise():
        entered = asyncio.Event()
        revised = False
        dispatches = 0

        async def loop(node, agent, **kwargs):
            nonlocal dispatches
            calls.append(node.kind)
            history = kwargs.get("history") or []
            if revised and node.kind is TaskKind.SPEC:
                assert sum("改为新格式" in message.get("content", "") for message in history) == 1
                assert product.read_text() == "old format"
            if node.kind is TaskKind.WORKER:
                if not revised and node.node_spec["id"] == "pending":
                    entered.set()
                    await asyncio.Event().wait()
                if revised:
                    assert "改为新格式" in kwargs["user_input"]
                    assert "完成这个 lab" in kwargs["user_input"]
                product.write_text("new format" if revised else "old format")
                brief = {"outcome": "done", "brief": "done", "changed_files": ["solution.txt"]}
                return LoopResult(brief, brief, "submit_brief")
            if node.kind is TaskKind.SPEC:
                payload = {"goal": "new format" if revised else "old format", "milestones": ["produce output"]}
            elif node.kind is TaskKind.DISPATCH:
                dispatches += 1
                aid = "a" if revised or dispatches == 1 else "pending"
                payload = {"step_goal": "produce", "assignments": [{"id": aid, "goal": "produce", "domain": "product"}]}
            elif node.kind is TaskKind.JUDGE:
                payload = {"decision": "finish" if revised else "continue", "rule_verdicts": []}
            else:
                payload = {"user_summary": "done"}
            return LoopResult(None, {"name": f"submit_{node.kind.value}", "payload": payload}, "submit", history=history)

        async def gate(*args):
            return AcceptResult(state=PASS)

        monkeypatch.setattr(flow, "run_loop", loop)
        monkeypatch.setattr("runtime.lab.step.run_loop", loop)
        monkeypatch.setattr("runtime.lab.step.run_gate", gate)
        running = asyncio.create_task(runner.run("完成这个 lab", tmp_path))
        await asyncio.wait_for(entered.wait(), 2)
        runner.request_stop()
        assert (await running)["verdict"] == "paused"
        before = Journal(tmp_path / JOURNAL_FILE).replay()
        assert before.effects and before.step_dispatch[2]
        count = len(calls)
        revised = True
        result = await runner.resume(tmp_path, instruction="改为新格式")
        assert result["verdict"] == "pass"
        assert calls[count:] == [TaskKind.SPEC, TaskKind.DISPATCH, TaskKind.WORKER, TaskKind.JUDGE, TaskKind.SUMMARY]
        assert product.read_text() == "new format"
        assert Journal(tmp_path / JOURNAL_FILE).replay().instructions == ["改为新格式"]

    asyncio.run(exercise())
