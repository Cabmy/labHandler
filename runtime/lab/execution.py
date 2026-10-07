"""Agent 一次执行的边界：开始/返回/异常/取消统一记入 Journal。

不复制业务 verdict，也不维护可复活的节点状态。排队中的 assignment 尚未
开始执行，没有 attempt；恢复只重派缺失的业务提交，每次创建新的 RuntimeTask。
"""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterator

from runtime.lab.journal import Journal
from runtime.loop.cycle import LoopResult
from runtime.task import RuntimeTask


class RunPaused(Exception):
    """流程尚未完成，退出本次运行并保留 Journal 断点。"""


class Attempt:
    def __init__(self, journal: Journal, task: RuntimeTask) -> None:
        self.journal = journal
        self.task = task
        self.result: LoopResult | None = None

    @contextmanager
    def scope(self) -> Iterator["Attempt"]:
        self.journal.append("attempt_started", {
            "task_id": self.task.task_id,
            "kind": self.task.kind.value,
            "assignment": self.task.node_spec,
        })
        ending = {}
        try:
            yield self
            if self.result is None:
                raise RuntimeError("agent attempt exited without a LoopResult")
            ending = {
                "reason": self.result.reason,
                "submit": self.result.submit,
                "brief": self.result.brief,
            }
        except asyncio.CancelledError:
            ending = {"error": "cancelled"}
            raise
        except Exception as exc:
            ending = {"error": f"{type(exc).__name__}: {exc}"}
            raise
        finally:
            self.journal.append("attempt_ended", {
                "task_id": self.task.task_id,
                **ending,
                "metrics": asdict(self.task.snapshot()),
            })
