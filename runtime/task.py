"""RuntimeTask 控制面：树、状态、预算计数、投影事件。"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Self

from runtime.errors import ErrorClass
from runtime.stagnation import StagnationSignal


class TaskKind(str, Enum):
    """节点种类，同时是阶段的唯一身份。

    除 SESSION 外每一项都在 runtime.phase.PHASES 里有一条定义，那里说明本阶段
    由谁执行、看得见哪些工具、从哪个 submit_* 交卷。
    """

    SESSION = "session"
    SPEC = "spec"          # Pro 起草/修订 SPEC.md
    DISPATCH = "dispatch"  # Pro 决定下一步派谁做什么
    WORKER = "worker"      # Flash 执行一份任务书
    TAKEOVER = "takeover"  # Flash 做不动时 Pro 接手本步实现
    JUDGE = "judge"
    REMEMBER_JUDGE = "remember_judge"  # 裁定 /remember 是否适用于本 lab，不写 SPEC/MEMORY
    SUMMARY = "summary"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Permission(str, Enum):
    READONLY = "readonly"
    WRITE = "write"
    PRO = "pro"


_LEGAL = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


@dataclass
class TaskSnapshot:
    task_id: str
    kind: TaskKind
    status: TaskStatus
    permission: Permission
    deadline: float
    step_count: int
    step_budget: int
    tool_failures: int
    transient_count: int
    validation_count: int
    consecutive_logic: int
    last_error_class: str | None
    events: list[dict[str, str]]
    execution_state: dict[str, Any]


@dataclass
class RuntimeTask:
    task_id: str
    parent_id: str | None
    kind: TaskKind
    permission: Permission
    step_budget: int
    deadline: float
    status: TaskStatus = TaskStatus.PENDING
    children: list[str] = field(default_factory=list)
    step_count: int = 0
    tool_failures: int = 0
    transient_count: int = 0
    validation_count: int = 0
    consecutive_logic: int = 0
    execution_state: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, str]] = field(default_factory=list)
    brief: dict[str, Any] | None = None
    node_spec: dict[str, Any] = field(default_factory=dict)

    def transit(self, new_status: TaskStatus) -> None:
        allowed = _LEGAL[self.status]
        if new_status not in allowed and new_status != self.status:
            raise ValueError(f"illegal transition {self.status} -> {new_status}")
        self.status = new_status

    def record(
        self,
        error_class: ErrorClass | None,
        stagnation_signal: StagnationSignal | None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """计数字段的唯一写入口。"""
        self.step_count += 1
        if usage:
            self.execution_state["last_usage"] = usage
        if error_class is not None:
            self.execution_state["last_error_class"] = error_class.value
            if error_class is ErrorClass.TRANSIENT:
                self.transient_count += 1
                self.tool_failures += 1
            elif error_class is ErrorClass.VALIDATION:
                self.validation_count += 1
            elif error_class is ErrorClass.LOGIC:
                self.consecutive_logic += 1
            elif error_class in {ErrorClass.OK, ErrorClass.ACCEPTABLE}:
                self.consecutive_logic = 0
            elif error_class is ErrorClass.LOOP:
                self.events.append(
                    {
                        "text": "You have repeated the same action and the execution loop was stopped."
                    }
                )
            elif error_class is ErrorClass.FATAL:
                self.events.append({"text": "A fatal environment error stopped the task."})
            elif error_class is ErrorClass.AUTH:
                self.events.append(
                    {
                        "text": "Authentication or client configuration failed. Check LLM_API_KEY and whitelist headers."
                    }
                )

        if stagnation_signal is StagnationSignal.REPEAT:
            self.events.append(
                {
                    "text": "You have repeated the same action 3 times without changing the state."
                }
            )
        elif stagnation_signal is StagnationSignal.LOOP_CONFIRMED:
            self.events.append(
                {
                    "text": "Loop detected after the stagnation grace window. The worker will stop."
                }
            )

        if self.step_count >= self.step_budget:
            self.events.append(
                {
                    "text": "Your task was stopped because the execution budget was exhausted."
                }
            )

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=self.task_id,
            kind=self.kind,
            status=self.status,
            permission=self.permission,
            deadline=self.deadline,
            step_count=self.step_count,
            step_budget=self.step_budget,
            tool_failures=self.tool_failures,
            transient_count=self.transient_count,
            validation_count=self.validation_count,
            consecutive_logic=self.consecutive_logic,
            last_error_class=self.execution_state.get("last_error_class"),
            events=list(self.events),
            execution_state=dict(self.execution_state),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_id": self.parent_id,
            "kind": self.kind.value,
            "permission": self.permission.value,
            "step_budget": self.step_budget,
            "deadline": self.deadline,
            "status": self.status.value,
            "children": list(self.children),
            "step_count": self.step_count,
            "tool_failures": self.tool_failures,
            "transient_count": self.transient_count,
            "validation_count": self.validation_count,
            "consecutive_logic": self.consecutive_logic,
            "execution_state": self.execution_state,
            "events": self.events,
            "brief": self.brief,
            "node_spec": self.node_spec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            task_id=data["task_id"],
            parent_id=data.get("parent_id"),
            kind=TaskKind(data["kind"]),
            permission=Permission(data["permission"]),
            step_budget=int(data["step_budget"]),
            deadline=float(data["deadline"]),
            status=TaskStatus(data["status"]),
            children=list(data.get("children") or []),
            step_count=int(data.get("step_count") or 0),
            tool_failures=int(data.get("tool_failures") or 0),
            transient_count=int(data.get("transient_count") or 0),
            validation_count=int(data.get("validation_count") or 0),
            consecutive_logic=int(data.get("consecutive_logic") or 0),
            execution_state=dict(data.get("execution_state") or {}),
            events=list(data.get("events") or []),
            brief=data.get("brief"),
            node_spec=dict(data.get("node_spec") or {}),
        )


class TaskTree:
    """一棵 RuntimeTask 树。"""

    def __init__(self, root: RuntimeTask) -> None:
        self.root_id = root.task_id
        self.nodes: dict[str, RuntimeTask] = {root.task_id: root}

    def get(self, task_id: str) -> RuntimeTask:
        return self.nodes[task_id]

    def add_child(
        self,
        parent_id: str,
        *,
        kind: TaskKind,
        permission: Permission,
        step_budget: int,
        deadline: float,
        node_spec: dict[str, Any] | None = None,
    ) -> RuntimeTask:
        parent = self.nodes[parent_id]
        child = RuntimeTask(
            task_id=f"{kind.value}_{uuid.uuid4().hex[:8]}",
            parent_id=parent_id,
            kind=kind,
            permission=permission,
            step_budget=step_budget,
            deadline=deadline,
            node_spec=node_spec or {},
        )
        self.nodes[child.task_id] = child
        parent.children.append(child.task_id)
        return child

    def children_of(self, task_id: str) -> list[RuntimeTask]:
        node = self.nodes[task_id]
        return [self.nodes[cid] for cid in node.children]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "nodes": {tid: t.to_dict() for tid, t in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        nodes = {
            tid: RuntimeTask.from_dict(payload)
            for tid, payload in (data.get("nodes") or {}).items()
        }
        root_id = data["root_id"]
        tree = cls.__new__(cls)
        tree.root_id = root_id
        tree.nodes = nodes
        return tree


def new_session_task(*, step_budget: int, wall_time_s: float, now: float | None = None) -> RuntimeTask:
    t0 = now if now is not None else time.time()
    return RuntimeTask(
        task_id=f"session_{uuid.uuid4().hex[:10]}",
        parent_id=None,
        kind=TaskKind.SESSION,
        permission=Permission.PRO,
        step_budget=step_budget,
        deadline=t0 + wall_time_s,
    )
