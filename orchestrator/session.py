"""任务会话层 — HwState 生命周期管理（CLI 与 Web Server 共用）。

从 cli.py 抽出的会话逻辑（进程常驻、会话级隔离）：
- 一个 lab 周期内共享一份 HwState；每条 user 输入累加到 messages / user_constraints
- 第二次起的输入由 graph 入口路由直接进 planner（复用 prior intake_result 做修订），
  此时 iteration 重置回 0，让 planner.iteration+=1 后仍在 MAX_REPLAN_ITER 预算内
- reset（等效 /done）：归档 → 清场 → 重建沙箱 → 会话复位，逻辑等效进程重启；
  隔离边界是会话而非进程，复位序列单点收拢在本层（CLI/Web 共用同一入口）
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from config.runtime import get_settings


def _new_thread_id() -> str:
    """checkpointer thread_id：时间戳 + 短随机后缀（同秒内多任务防撞）。"""
    return f"task_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def new_state(question: str = "") -> dict[str, Any]:
    return {
        "question": question,
        "iteration": 0,
        "progress_log": [],
        "messages": [],
        "user_constraints": [],
        "verifier_runs": [],
        "artifacts": [],
    }


class TaskSession:
    """单进程单任务会话：持有 HwState + 归档/清场操作。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.state: dict[str, Any] = new_state()
        # checkpointer thread：**每次任务**一个 thread_id（prepare_task 里刷新）。
        # 不能整个会话复用同一 thread：归约字段（progress_log 等 Annotated[list, add]）
        # 会把作为输入传入的全量 state 再叠加到 checkpoint 已有值上，产生重复。
        self.thread_id: str = _new_thread_id()

    # ─── state 管理 ────────────────────────────────────────────

    def append_user_message(self, text: str) -> None:
        """user 输入累加到 messages / user_constraints；question 始终是最新输入。

        重复约束在 Verifier 语义判官那一步会被 LLM 自然 dedup。
        """
        self.state.setdefault("messages", []).append({"role": "user", "content": text})
        self.state["question"] = text
        self.state.setdefault("user_constraints", []).append(text)

    def prepare_task(self, text: str) -> dict[str, Any]:
        """一轮任务前的 state 准备：追加输入 + refine 路径重置 iteration + 新 thread_id。"""
        self.append_user_message(text)
        # refine 路径（已有 prior 任务的 verifier_runs）：iteration 重置回 0，
        # 否则首轮跑满后第二次输入会立刻撞 MAX_REPLAN_ITER 顶。
        if self.state.get("verifier_runs"):
            self.state["iteration"] = 0
        self.thread_id = _new_thread_id()
        return self.state

    def run_config(self) -> dict[str, Any]:
        """主图执行 config（checkpointer 按 thread_id 定位本会话的 checkpoint）。"""
        return {"configurable": {"thread_id": self.thread_id}}

    # ─── 归档 + 清场（等效 /done） ─────────────────────────────

    def archive(self) -> dict[str, Any]:
        """归档当前任务：create_task → create_cards → 向量/BM25 索引。

        Returns: {task_id, card_ids, indexed, failed, errors} 或 {error}
        """
        from memory.archive import get_task_archive
        from rag.archive_retriever import index_cards

        intake = self.state.get("intake_result") or {}
        title = intake.get("title") or self.state.get("question") or "未命名任务"
        ttype = intake.get("type") or "other"
        summary_text = self.state.get("summary") or ""
        knowledge_cards = self.state.get("knowledge_cards") or []

        try:
            archive = get_task_archive()
            task_id = archive.create_task(title, ttype, summary_text[:4000])
            card_ids = archive.create_cards(task_id, knowledge_cards, title, ttype)
            result: dict[str, Any] = {
                "task_id": task_id, "card_ids": card_ids,
                "indexed": 0, "failed": 0, "errors": [],
            }
            if card_ids:
                idx = index_cards(card_ids)
                result["indexed"] = idx.get("indexed", 0)
                result["failed"] = idx.get("failed", 0)
                result["errors"] = idx.get("errors", [])
            return result
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def clear_workspace(self) -> tuple[Path, list[str]]:
        """workspace 内容 mv 到 .trash/<ts>/。Returns: (bucket 路径, 移动的文件名列表)。"""
        ws = self.settings.workspace_dir
        trash_dir = ws.parent / ".trash"
        ts = time.strftime("%Y%m%d_%H%M%S")
        bucket = trash_dir / ts
        bucket.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for p in ws.iterdir():
            shutil.move(str(p), str(bucket / p.name))
            moved.append(p.name)
        return bucket, moved

    def _purge_checkpoints(self) -> str:
        """/done 后清空 checkpoint db（直连 sqlite，同步安全，不碰 saver 的 loop）。

        lab 已归档清场，历史 checkpoint 既无续跑价值（残留会让 find_resumable_thread
        在已清空的 workspace 上误续跑），也避免长跑膨胀（数据可丢弃原则）。
        """
        try:
            import sqlite3
            db = self.settings.checkpoint_db_path
            if not db.exists():
                return "absent"
            with sqlite3.connect(str(db)) as conn:
                for table in ("checkpoints", "writes"):
                    try:
                        conn.execute(f"DELETE FROM {table}")
                    except sqlite3.OperationalError:
                        pass  # 表不存在（schema 差异）忽略
                conn.commit()
            return "purged"
        except Exception as e:
            return f"failed: {type(e).__name__}: {e}"

    def reset(self, log=print) -> dict[str, Any]:
        """/done 语义：归档 → 清场 → 强制重建沙箱 → 清 checkpoint → 会话复位
        （逻辑等效进程重启）。

        沙箱重建不可选：容器污染（pip 全局包 / /tmp / 长跑进程）是跨 lab 干扰
        最现实的来源；recreate_sandbox 内部会一并复位 MCP client / sandbox_tools 缓存。

        Returns: {archive, trashed_to, moved, sandbox, checkpoints}
        """
        archive_result = self.archive()
        bucket, moved = self.clear_workspace()
        try:
            from infra.sandbox_boot import recreate_sandbox
            sandbox = "ok" if recreate_sandbox(log=log) else "not_ready"
        except Exception as e:
            sandbox = f"failed: {type(e).__name__}: {e}"
        checkpoints = self._purge_checkpoints()
        self.state = new_state()
        self.thread_id = _new_thread_id()
        return {
            "archive": archive_result,
            "trashed_to": str(bucket),
            "moved": moved,
            "sandbox": sandbox,
            "checkpoints": checkpoints,
        }


# ─── 断点续跑 ─────────────────────────────────


async def find_resumable_thread(graph: Any) -> tuple[str, list[str]] | None:
    """检测最近一个 thread 的 checkpoint 是否停在非 END 节点（崩溃/中断残留）。

    Returns: (thread_id, next_nodes) 可续跑；无 checkpoint 或已跑完则 None。
    依赖 saver.alist(None) 全局按 checkpoint_id 降序（uuid6 时间有序），
    首条即最近一次写入的 checkpoint。
    """
    saver = getattr(graph, "checkpointer", None)
    if saver is None:
        return None
    try:
        latest_tid: str | None = None
        async for ckpt in saver.alist(None, limit=1):
            latest_tid = (ckpt.config.get("configurable") or {}).get("thread_id")
        if not latest_tid:
            return None
        snap = await graph.aget_state({"configurable": {"thread_id": latest_tid}})
        if snap and snap.next:
            return latest_tid, list(snap.next)
    except Exception:
        # checkpoint db 损坏/schema 变更等：静默降级为"无可续跑"
        return None
    return None
