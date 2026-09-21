"""labHandler Web 服务器（FastAPI + SSE）。入口：python -m server。"""

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config.runtime import get_settings
from runtime.lab.persist import latest_incomplete
from runtime.session import LabSession
from tools.policy import is_under_workspace
from tools.workspace_utils import iter_workspace_files

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _ws() -> Path:
    """工作区路径，每次从 settings 获取。"""
    return get_settings().workspace_dir


app = FastAPI(title="labHandler", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html") or request.url.path.endswith(
        (".html", ".js", ".css")
    ):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


_session = LabSession()
_task_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None
_event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()


def _is_running() -> bool:
    return _current_task is not None and not _current_task.done()


@app.on_event("startup")
async def _warm_memory_index() -> None:
    """进程起来时对齐卡片文件与向量表。换 EMBEDDING_MODEL 后必须走这里才会重建。失败不挡服务。"""

    async def _run() -> None:
        try:
            from memory.retrieve import reconcile_index

            await reconcile_index(_session.llm, _session.settings)
        except Exception:
            pass

    asyncio.create_task(_run())


@app.on_event("shutdown")
async def _shutdown() -> None:
    """进程退出前刷出 span、关闭 LLM client，否则最后一段 trace 会丢在缓冲里。"""
    _session.request_stop()
    _session.tracer.shutdown()
    await _session.llm.aclose()


def _safe_workspace_path(name: str) -> Path:
    """解析并校验路径是否在 workspace 内；非法或越界抛 HTTPException(400)。"""
    ws = _ws()
    rel = Path(name)
    if not name or rel.is_absolute() or any(
        not part or part.startswith(".") for part in rel.parts
    ):
        raise HTTPException(status_code=400, detail=f"非法文件名：{name!r}")
    resolved = (ws / rel).resolve()
    if not is_under_workspace(resolved, ws):
        raise HTTPException(status_code=400, detail=f"越界路径：{name!r}")
    return resolved


def _list_workspace_files() -> list[dict[str, Any]]:
    ws = _ws()
    out: list[dict[str, Any]] = []
    if not ws.exists():
        return out
    for p in iter_workspace_files(ws):
        out.append({"name": str(p.relative_to(ws)), "size": p.stat().st_size})
    return sorted(out, key=lambda x: x["name"])


@app.get("/api/files")
async def list_files() -> list[dict[str, Any]]:
    return _list_workspace_files()


@app.post("/api/files")
async def upload_files(files: list[UploadFile]) -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改 workspace")
    ws = _ws()
    ws.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        target = _safe_workspace_path(f.filename or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await f.read())
        saved.append(target.name)
    return {"saved": saved}


@app.get("/api/files/{name:path}")
async def download_file(name: str) -> FileResponse:
    resolved = _safe_workspace_path(name)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(resolved, filename=resolved.name)


@app.delete("/api/files/{name:path}")
async def delete_file(name: str) -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改 workspace")
    resolved = _safe_workspace_path(name)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    bucket = _ws().parent / ".trash" / time.strftime("%Y%m%d_%H%M%S")
    bucket.mkdir(parents=True, exist_ok=True)
    shutil.move(str(resolved), str(bucket / resolved.name))
    return {"deleted": resolved.name, "trashed_to": str(bucket)}


class TaskRequest(BaseModel):
    question: str


async def _run_lab(question: str) -> None:
    """把一次 LabSession.run 写成 final/error 事件并关闭 SSE。

    续跑 vs 新开由 session 是否已 attach tree 决定。
    """

    async def sink(ev: dict[str, Any]) -> None:
        await _event_queue.put(ev)

    started = time.time()
    try:
        result = await _session.run(question, on_event=sink)
        await _event_queue.put(
            {
                "kind": "final",
                "verdict": result.get("verdict", "unknown"),
                "elapsed": round(time.time() - started, 1),
                "artifacts": await asyncio.to_thread(_list_workspace_files),
            }
        )
    except Exception as e:
        await _event_queue.put({"kind": "error", "detail": f"{type(e).__name__}: {e}"})
    finally:
        _session.tracer.flush()
        await _event_queue.put(None)


@app.post("/api/task")
async def submit_task(req: TaskRequest) -> dict[str, Any]:
    global _current_task
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 为空")
    async with _task_lock:
        if _is_running():
            raise HTTPException(status_code=409, detail="已有任务在运行（单任务约束）")
        while not _event_queue.empty():
            _event_queue.get_nowait()
        _current_task = asyncio.create_task(_run_lab(question))
    return {"accepted": True, "question": question, "thread_id": _session.thread_id}


class ResumeRequest(BaseModel):
    continue_lab: bool
    question: str = "继续上次未完成的 lab"


@app.get("/api/resume")
async def resume_info() -> dict[str, Any]:
    peek = _session.peek_resume()
    return {"resumable": peek}


@app.post("/api/resume")
async def resume_lab(req: ResumeRequest) -> dict[str, Any]:
    global _current_task
    async with _task_lock:
        if _is_running():
            raise HTTPException(status_code=409, detail="已有任务在运行")
        peek = await asyncio.to_thread(latest_incomplete, _ws())
        if not peek:
            raise HTTPException(status_code=404, detail="没有未完成的 lab")
        tid, tree = peek
        if not req.continue_lab:
            return await asyncio.to_thread(_session.decline_resume)
        _session.attach_resume(tid, tree)
        while not _event_queue.empty():
            _event_queue.get_nowait()
        _current_task = asyncio.create_task(_run_lab(req.question))
    return {"accepted": True, "thread_id": tid, "resumed": True}


@app.get("/api/task/stream")
async def task_stream() -> EventSourceResponse:
    async def _gen():
        while True:
            try:
                item = await asyncio.wait_for(_event_queue.get(), timeout=300)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            if item is None:
                break
            yield {
                "event": item.get("kind", "message"),
                "data": json.dumps(item, ensure_ascii=False, default=str),
            }

    return EventSourceResponse(_gen())


@app.post("/api/stop")
async def stop_task() -> dict[str, Any]:
    if not _is_running():
        return {"stopped": False}
    _session.request_stop()
    return {"stopped": True}


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    peek = _session.peek_resume()
    return {
        "running": _is_running(),
        "thread_id": _session.thread_id,
        "resumable": peek,
        "verdict": (_session.last_result or {}).get("verdict"),
    }


@app.get("/api/summary")
async def get_summary() -> JSONResponse:
    sp = _ws() / "SUMMARY.md"
    text = sp.read_text(encoding="utf-8") if sp.exists() else ""
    return JSONResponse({"summary": text})


@app.post("/api/dream")
async def dream() -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，先等它结束")
    from memory.dream import run_dream

    try:
        return await run_dream(_session.llm)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


class RememberRequest(BaseModel):
    rule: str


@app.get("/api/profile")
async def get_profile_api() -> dict[str, Any]:
    from memory.profile import load_profile

    return load_profile() or {}


@app.post("/api/remember")
async def remember(req: RememberRequest) -> dict[str, Any]:
    from memory.profile import append_rule

    try:
        prof = append_rule(req.rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rules = (prof.get("preferences") or {}).get("style_rules") or []
    return {"style_rules": rules}


_PENDING_EDIT: dict[str, Any] | None = None
_EDIT_PENDING_TTL = 600


class EditSkillRequest(BaseModel):
    skill_name: str
    instruction: str


class EditApplyRequest(BaseModel):
    edit_id: str
    confirm: bool = True


@app.get("/api/skills")
async def list_skills_api() -> list[dict[str, str]]:
    from tools.skill_tool import list_skill_meta

    return list_skill_meta()


@app.post("/api/edit_skill")
async def edit_skill(req: EditSkillRequest) -> dict[str, Any]:
    global _PENDING_EDIT
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，先等它结束")
    from skills.editor import existing_skill_names, propose_edit

    available = existing_skill_names()
    skill_name = (req.skill_name or "").strip()
    instruction = (req.instruction or "").strip()
    if skill_name not in available:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在：{skill_name!r}（仅支持编辑现有 skill：{available}，不支持新增）",
        )
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction 为空")
    try:
        proposal = await propose_edit(skill_name, instruction)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    edit_id = f"edit_{int(time.time() * 1000)}"
    _PENDING_EDIT = {"edit_id": edit_id, "created_at": time.time(), **proposal}
    return {
        "edit_id": edit_id,
        "skill_name": skill_name,
        "summary": proposal["summary"],
        "diffs": proposal["diffs"],
        "style_samples": proposal["style_samples"],
        "sample_failures": proposal["sample_failures"],
        "n_operations": len(proposal["operations"]),
    }


@app.post("/api/edit_skill/apply")
async def edit_skill_apply(req: EditApplyRequest) -> dict[str, Any]:
    global _PENDING_EDIT
    if _PENDING_EDIT is None:
        raise HTTPException(status_code=404, detail="没有待确认的编辑提案")
    if req.edit_id != _PENDING_EDIT["edit_id"]:
        raise HTTPException(status_code=409, detail="edit_id 不匹配（提案已被新提案覆盖）")
    if time.time() - _PENDING_EDIT["created_at"] > _EDIT_PENDING_TTL:
        _PENDING_EDIT = None
        raise HTTPException(status_code=409, detail="提案已超时（10 分钟），请重新发起")

    pending = _PENDING_EDIT
    _PENDING_EDIT = None
    if not req.confirm:
        return {"status": "cancelled"}
    from skills.editor import apply_edit

    try:
        report = await asyncio.to_thread(
            apply_edit, pending["skill_name"], pending["operations"]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return {"status": "applied", **report}


@app.post("/api/done")
async def done() -> dict[str, Any]:
    if _is_running():
        _session.request_stop()
        if _current_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(_current_task), timeout=15)
            except Exception:
                pass
    from memory.retrieve import index_card_ids

    cards = (_session.last_result or {}).get("knowledge_cards") or []
    # 卡片先入档并向量索引，再 _session.done 复位；反序会丢掉未索引卡片。
    archive_extra: dict[str, Any] = {}
    if cards:
        from memory.archive import get_task_archive

        title = (_session.last_result or {}).get("question") or "未命名任务"
        summary = (_session.last_result or {}).get("summary") or ""
        archive = get_task_archive()
        if archive.has_new_cards(cards):
            task_id = archive.create_task(title, "other", summary[:4000])
            card_ids = archive.create_cards(task_id, cards, title, "other")
            if card_ids:
                archive_extra = await index_card_ids(card_ids, _session.llm, _session.settings)
                archive_extra["task_id"] = task_id
                archive_extra["card_ids"] = card_ids
        if _session.last_result:
            # 已处理，清空以免 _session.done 再建空 task
            _session.last_result["knowledge_cards"] = []
    result = await asyncio.to_thread(_session.done, lambda m: None)
    if archive_extra:
        result["archive"] = {**(result.get("archive") or {}), **archive_extra}
    return result


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
