"""labHandler Web 服务器（FastAPI + SSE）。

让用户不碰代码即可使用：浏览器上传作业材料
（等效放入 workspace/）-> 输入任务 -> SSE 实时执行进度 ->
下载产物 / 查看 SUMMARY -> 归档收尾；
另提供 skill 编辑（等效 CLI /edit_skill：提案 -> diff 确认 -> 落盘）。

架构约定：
- 复用 orchestrator/session.py:TaskSession（与 CLI 同一套会话逻辑，单进程单任务）
- 复用 ui/events.py:iter_graph_events（与终端渲染同一套事件流，序列化为 SSE）
- 单任务锁：同一时刻只跑一个任务（与单进程单 HwState
  架构一致）；执行期间提交返回 409
- 仅本地使用（默认 127.0.0.1:8000，无鉴权）；如需暴露局域网请自行加反向代理 + token

启动：python -m server
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# .env 集中于 config/ 目录管理（与 cli.py 同路径约定）
load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env")

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402

from config.runtime import get_settings  # noqa: E402
from orchestrator.session import TaskSession  # noqa: E402
from tools.workspace_utils import iter_workspace_files  # noqa: E402

WORKSPACE_DIR: Path = get_settings().workspace_dir
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="labHandler", docs_url=None, redoc_url=None)

# ─── 会话与任务运行时（模块级单例：单进程单任务） ───

_session = TaskSession()
_task_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None
# SSE 消费队列：任务运行期间事件推入此处；None 哨兵标记流结束
_event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()


def _is_running() -> bool:
    return _current_task is not None and not _current_task.done()


# ─── 文件管理（上传等效放入 workspace/） ────────────


def _safe_workspace_path(name: str) -> Path:
    """防上传/删除路径越界：拒绝隐藏段与绝对路径，
    解析后路径须仍在 WORKSPACE_DIR 内。

    保留相对子路径（src/x.py）：delete 端点若 basename 化会误指
    根目录下同名文件。"""
    rel = Path(name)
    if not name or rel.is_absolute() or any(
        not part or part.startswith(".") for part in rel.parts
    ):
        raise HTTPException(status_code=400, detail=f"非法文件名：{name!r}")
    resolved = (WORKSPACE_DIR / rel).resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"越界路径：{name!r}")
    return resolved


def _list_workspace_files() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not WORKSPACE_DIR.exists():
        return out
    for p in iter_workspace_files(WORKSPACE_DIR):
        out.append({"name": str(p.relative_to(WORKSPACE_DIR)), "size": p.stat().st_size})
    return sorted(out, key=lambda x: x["name"])


@app.get("/api/files")
async def list_files() -> list[dict[str, Any]]:
    return _list_workspace_files()


@app.post("/api/files")
async def upload_files(files: list[UploadFile]) -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改 workspace")
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        target = _safe_workspace_path(f.filename or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await f.read())
        saved.append(target.name)
    return {"saved": saved}


@app.get("/api/files/{name:path}")
async def download_file(name: str) -> FileResponse:
    # 下载允许一级子目录产物（src/x.py 等）：先按相对路径解析再验边界
    resolved = (WORKSPACE_DIR / name).resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="越界路径")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(resolved, filename=resolved.name)


@app.delete("/api/files/{name:path}")
async def delete_file(name: str) -> dict[str, Any]:
    """删除单个已上传文件：mv 到 .trash/<ts>/（与 clear_workspace 同约定），非真删。"""
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改 workspace")
    resolved = _safe_workspace_path(name)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    bucket = WORKSPACE_DIR.parent / ".trash" / time.strftime("%Y%m%d_%H%M%S")
    bucket.mkdir(parents=True, exist_ok=True)
    shutil.move(str(resolved), str(bucket / resolved.name))
    return {"deleted": resolved.name, "trashed_to": str(bucket)}


# ─── 任务执行 + SSE 进度流 ────────────────────────────────


class TaskRequest(BaseModel):
    question: str


async def _run_graph_task(state: dict[str, Any]) -> None:
    """后台执行主图，序列化 GraphEvents 推入 SSE 队列。"""
    from orchestrator import get_graph
    from ui.events import iter_graph_events

    try:
        graph = get_graph()
        async for ev in iter_graph_events(graph, state, config=_session.run_config()):
            if ev.kind == "final":
                final_state = ev.payload.get("state") or {}
                _session.state = final_state
                runs = final_state.get("verifier_runs") or []
                await _event_queue.put({
                    "kind": "final",
                    "verdict": runs[-1].get("verdict", "unknown") if runs else "unknown",
                    "iteration": final_state.get("iteration", 0),
                    "elapsed": round(float(ev.payload.get("elapsed", 0)), 1),
                    "artifacts": _list_workspace_files(),
                })
            elif ev.kind == "content":
                await _event_queue.put({
                    "kind": "content", "node": ev.node,
                    "text": ev.payload.get("text", ""),
                    "reasoning": bool(ev.payload.get("reasoning")),
                })
            elif ev.kind == "tool":
                await _event_queue.put({
                    "kind": "tool", "node": ev.node,
                    "name": ev.payload.get("name", ""),
                    "args": str(ev.payload.get("args", ""))[:200],
                    "result": str(ev.payload.get("content", ""))[:200],
                })
            elif ev.kind == "node_start":
                await _event_queue.put({"kind": "node_start", "node": ev.node})
            elif ev.kind == "node_done":
                await _event_queue.put({
                    "kind": "node_done", "node": ev.node,
                    "log": ev.payload.get("log") or [],
                })
    except Exception as e:
        await _event_queue.put({
            "kind": "error", "detail": f"{type(e).__name__}: {e}",
        })
    finally:
        await _event_queue.put(None)  # 流结束哨兵


@app.post("/api/task")
async def submit_task(req: TaskRequest) -> dict[str, Any]:
    global _current_task
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 为空")
    async with _task_lock:
        if _is_running():
            raise HTTPException(status_code=409, detail="已有任务在运行（单任务约束）")
        # 清掉上个任务残留的未消费事件
        while not _event_queue.empty():
            _event_queue.get_nowait()
        state = _session.prepare_task(question)
        _current_task = asyncio.create_task(_run_graph_task(state))
    return {"accepted": True, "question": question}


@app.get("/api/task/stream")
async def task_stream() -> EventSourceResponse:
    """SSE：推送当前任务的 GraphEvent 流；final/error 后关闭。"""
    async def _gen():
        while True:
            try:
                item = await asyncio.wait_for(_event_queue.get(), timeout=300)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            if item is None:
                break
            yield {"event": item.get("kind", "message"),
                   "data": json.dumps(item, ensure_ascii=False, default=str)}
    return EventSourceResponse(_gen())


# ─── 状态 / SUMMARY / 归档 ────────────────────────────────────────


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    state = _session.state
    intake = state.get("intake_result") or {}
    runs = state.get("verifier_runs") or []
    return {
        "running": _is_running(),
        "question": state.get("question", ""),
        "iteration": state.get("iteration", 0),
        "intake_type": intake.get("type", "-"),
        "intake_title": intake.get("title", "-"),
        "n_messages": len(state.get("messages") or []),
        "verdicts": [r.get("verdict") for r in runs],
    }


@app.get("/api/summary")
async def get_summary() -> JSONResponse:
    sp = WORKSPACE_DIR / "SUMMARY.md"
    text = sp.read_text(encoding="utf-8") if sp.exists() else ""
    return JSONResponse({"summary": text})


@app.post("/api/dream")
async def dream() -> dict[str, Any]:
    """离线知识治理（等效 CLI /dream）：LLM 合并/去重/淘汰归档卡片 + 重建索引。

    同步 LLM 调用慢，卸载到线程池避免阻塞事件循环；
    任务执行期间不允许治理。
    """
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，先等它结束")
    from memory.dream import run_dream

    try:
        return await asyncio.to_thread(run_dream)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


# ─── 用户偏好（等效 CLI /profile 与 /remember） ───────────


class RememberRequest(BaseModel):
    rule: str


@app.get("/api/profile")
async def get_profile_api() -> dict[str, Any]:
    """当前 profile（me.yaml）全文（等效 CLI /profile）。"""
    from memory.profile import load_profile

    return load_profile() or {}


@app.post("/api/remember")
async def remember(req: RememberRequest) -> dict[str, Any]:
    """向 preferences.style_rules 追加一条长期偏好规则（等效 CLI /remember，不调 LLM）。"""
    from memory.profile import append_rule

    try:
        prof = append_rule(req.rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rules = (prof.get("preferences") or {}).get("style_rules") or []
    return {"style_rules": rules}


# ─── Skill 编辑（等效 CLI /edit_skill：提案 -> diff 确认 -> 落盘） ──

# 待确认提案单槽位：单进程单用户，新提案覆盖旧提案；
# 确认需 edit_id 匹配且未超时
_PENDING_EDIT: dict[str, Any] | None = None
_EDIT_PENDING_TTL = 600  # 秒


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
    """生成 skill 编辑提案（不落盘）；返回 diff 供前端展示，经 /api/edit_skill/apply 确认。"""
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
        proposal = await asyncio.to_thread(propose_edit, skill_name, instruction)
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
    """确认/取消待确认编辑提案；确认时整批落盘并清槽位。"""
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
    """等效 CLI /done：归档 -> 清理 -> 强制重建沙箱 -> 会话复位（逻辑等效进程重启）。

    复位序列在 orchestrator/session.py:reset（与 CLI 共用）；沙箱重建
    同步耗时可达几十秒，卸载到线程池避免阻塞事件循环。
    """
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，先等它结束")
    return await asyncio.to_thread(_session.reset, lambda m: None)


# 静态单页（最后注册，避免吞掉 /api/*）
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
