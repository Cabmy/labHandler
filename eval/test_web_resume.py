"""新建与续跑是不同入口；续跑字段仅为可选 instruction，不提供旧字段兼容。"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import server.app as web
from runtime.lab.journal import Journal
from runtime.lab.persist import session_dir


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "_session", SimpleNamespace(thread_id="new-session"))
    monkeypatch.setattr(web, "_ws", lambda: tmp_path)
    monkeypatch.setattr(web, "_current_task", None)
    monkeypatch.setattr(web, "_task_lock", asyncio.Lock())
    monkeypatch.setattr(web, "_event_queue", asyncio.Queue())

    async def run_lab(text, *, thread_id=None):
        calls.append((text, thread_id))
        await asyncio.Event().wait()

    monkeypatch.setattr(web, "_run_lab", run_lab)
    return tmp_path, calls


def paused(workspace):
    journal = Journal.open(session_dir(workspace, "existing-lab"))
    journal.append("begin", {"version": 1, "question": "original request"})
    journal.append("stage", {"stage": "spec"})
    journal.append("run_paused", {"reason": "user_stop"})
    return journal


def test_new_task_rejects_paused_lab_instead_of_compatibility_resume(endpoint):
    workspace, calls = endpoint
    paused(workspace)

    async def exercise():
        with pytest.raises(web.HTTPException) as exc:
            await web.submit_task(web.TaskRequest(question="继续"))
        assert exc.value.status_code == 409

    asyncio.run(exercise())
    assert not calls
    assert web._current_task is None


@pytest.mark.parametrize("instruction", ["", "改用 C++，保留现有说明。"])
def test_resume_forwards_instruction_to_same_lab_and_rejects_overlap(endpoint, instruction):
    workspace, calls = endpoint
    journal = paused(workspace)

    async def exercise():
        for _ in range(2):
            response = await web.resume_lab(web.ResumeRequest(continue_lab=True, instruction=instruction))
            assert response["thread_id"] == "existing-lab"
            await asyncio.sleep(0)
            with pytest.raises(web.HTTPException) as exc:
                await web.resume_lab(web.ResumeRequest(continue_lab=True))
            assert exc.value.status_code == 409
            web._current_task.cancel()
            await asyncio.gather(web._current_task, return_exceptions=True)

    asyncio.run(exercise())
    assert calls == [(instruction, "existing-lab")] * 2
    assert journal.replay().question == "original request"


def test_resume_rejects_old_question_field():
    with pytest.raises(ValidationError):
        web.ResumeRequest(continue_lab=True, question="旧字段")


@pytest.mark.parametrize("closed", [False, True])
def test_new_task_without_incomplete_journal(endpoint, closed):
    workspace, calls = endpoint
    if closed:
        journal = paused(workspace)
        journal.append("session_closed", {"result": {"verdict": "pass"}})

    async def exercise():
        assert (await web.submit_task(web.TaskRequest(question="new request")))["accepted"]
        await asyncio.sleep(0)
        web._current_task.cancel()
        await asyncio.gather(web._current_task, return_exceptions=True)

    asyncio.run(exercise())
    assert calls == [("new request", None)]


def test_web_run_routes_instruction_to_session_resume(monkeypatch):
    calls = []

    async def start(text, **kwargs):
        calls.append(("start", text))
        return {"verdict": "paused"}

    async def resume(thread_id, *, instruction, on_event):
        calls.append((thread_id, instruction))
        return {"verdict": "paused"}

    monkeypatch.setattr(web, "_session", SimpleNamespace(run=start, resume=resume, tracer=SimpleNamespace(flush=lambda: None)))
    monkeypatch.setattr(web, "_event_queue", asyncio.Queue())
    monkeypatch.setattr(web, "_list_workspace_files", lambda: [])

    async def exercise():
        await web._run_lab("补充指令", thread_id="existing-lab")
        await web._run_lab("新 lab")

    asyncio.run(exercise())
    assert calls == [("existing-lab", "补充指令"), ("start", "新 lab")]
