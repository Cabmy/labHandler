"""lab 生命周期：一次会话对应一个 thread_id、一棵 Task 树、一个会话目录。

新任务永远新开会话。续跑只通过 attach_resume 显式挂上未完成的树，
并恢复 CHECKPOINT.json 里的 Pro 对话与阶段进度。
done() 归档知识卡、把 workspace 挪进 .trash、重建沙箱，并换一棵空 runner。
"""

import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings, get_settings
from runtime.llm import LLMGateway
from runtime.observe.sinks import build_sinks
from runtime.observe.tracer import Tracer
from runtime.lab.runner import LabRunner
from runtime.lab.persist import latest_incomplete, session_dir as make_session_dir
from runtime.task import TaskTree, new_session_task
from runtime.loop.tools import build_registry


def _new_thread_id() -> str:
    return f"lab_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class LabSession:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        llm: LLMGateway | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or get_settings()
        tracer = Tracer(
            build_sinks(
                jsonl_path=self.settings.traces_path if self.settings.trace_jsonl_enabled else None,
                langfuse_public_key=self.settings.langfuse_public_key,
                langfuse_secret_key=self.settings.langfuse_secret_key,
                langfuse_host=self.settings.langfuse_host,
                environment=self.settings.langfuse_environment,
                log=print,
            )
        )
        self.llm = llm or LLMGateway(self.settings, tracer=tracer)
        self.tracer = tracer
        self.clock = clock
        self.runner = LabRunner(
            self.settings,
            self.llm,
            registry=build_registry(),
            tracer=tracer,
            clock=clock,
        )
        self.thread_id = _new_thread_id()
        self.tree: TaskTree | None = None
        self.last_result: dict[str, Any] = {}
        self._resuming = False

    @property
    def session_path(self) -> Path:
        return make_session_dir(self.settings.workspace_dir, self.thread_id)

    def peek_resume(self) -> dict[str, Any] | None:
        found = latest_incomplete(self.settings.workspace_dir)
        if not found:
            return None
        tid, tree = found
        root = tree.get(tree.root_id)
        return {"thread_id": tid, "status": root.status.value}

    def attach_resume(self, thread_id: str, tree: TaskTree) -> None:
        """挂上一个未完成的会话。续跑是显式动作，只在这里发生。"""
        self.thread_id = thread_id
        self.tree = tree
        self._resuming = True

    def decline_resume(self) -> dict[str, Any]:
        found = latest_incomplete(self.settings.workspace_dir)
        if found:
            self.thread_id, self.tree = found[0], found[1]
        return self.done(log=lambda m: None)

    def request_stop(self) -> None:
        self.runner.request_stop()

    async def run(self, question: str, on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> dict[str, Any]:
        resume = self._resuming
        if not resume:
            # 每个新任务都是一条独立会话：新的 thread_id、新的 Task 树、新的目录。
            # 复用上一轮的树会让新任务读到上一轮的 SPEC.md 和材料。
            self.thread_id = _new_thread_id()
            self.tree = TaskTree(
                new_session_task(
                    step_budget=self.settings.pro_step_budget + self.settings.flash_step_budget,
                    wall_time_s=self.settings.task_wall_time_s * 4,
                    now=self.clock(),
                )
            )
        assert self.tree is not None
        self._resuming = False
        self.tracer.new_trace()
        self.session_path.mkdir(parents=True, exist_ok=True)
        result = await self.runner.run(
            question,
            self.session_path,
            self.tree,
            on_event=on_event,
            resume=resume,
        )
        self.last_result = result
        return result

    def archive(self) -> dict[str, Any]:
        from memory.archive import get_task_archive
        from memory.retrieve import index_card_ids
        import asyncio

        cards = self.last_result.get("knowledge_cards") or []
        title = self.last_result.get("question") or "未命名任务"
        summary = self.last_result.get("summary") or ""
        ttype = "other"
        if not cards:
            return {
                "task_id": None,
                "card_ids": [],
                "indexed": 0,
                "failed": 0,
                "errors": [],
                "skipped": "no_cards",
            }
        try:
            archive = get_task_archive()
            if not archive.has_new_cards(cards):
                return {
                    "task_id": None,
                    "card_ids": [],
                    "indexed": 0,
                    "failed": 0,
                    "errors": [],
                    "skipped": "no_cards",
                }
            task_id = archive.create_task(title, ttype, summary[:4000])
            card_ids = archive.create_cards(task_id, cards, title, ttype)
            result: dict[str, Any] = {
                "task_id": task_id,
                "card_ids": card_ids,
                "indexed": 0,
                "failed": 0,
                "errors": [],
            }
            if card_ids:

                async def _idx() -> dict[str, Any]:
                    return await index_card_ids(card_ids, self.llm, self.settings)

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    result["index_pending"] = True
                else:
                    idx = asyncio.run(_idx())
                    result.update(idx)
            return result
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def clear_workspace(self) -> tuple[Path, list[str]]:
        ws = self.settings.workspace_dir
        trash_dir = ws.parent / ".trash"
        ts = time.strftime("%Y%m%d_%H%M%S")
        bucket = trash_dir / ts
        bucket.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        if ws.exists():
            for p in ws.iterdir():
                shutil.move(str(p), str(bucket / p.name))
                moved.append(p.name)
        ws.mkdir(parents=True, exist_ok=True)
        return bucket, moved

    def done(self, log=print) -> dict[str, Any]:
        self.request_stop()
        archive_result = self.archive()
        bucket, moved = self.clear_workspace()
        try:
            from infra.sandbox_boot import recreate_sandbox

            sandbox = "ok" if recreate_sandbox(log=log) else "not_ready"
        except Exception as e:
            sandbox = f"failed: {type(e).__name__}: {e}"
        self.tree = None
        self.last_result = {}
        self.thread_id = _new_thread_id()
        self.runner = LabRunner(
            self.settings,
            self.llm,
            registry=build_registry(),
            tracer=self.tracer,
            clock=self.clock,
        )
        return {
            "archive": archive_result,
            "trashed_to": str(bucket),
            "moved": moved,
            "sandbox": sandbox,
        }
