"""一次 agent 执行的控制面：身份、权限、预算和语义事件。

生命周期归执行协程所有；此对象不持久化，也不复活或跨尝试复用。
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime.errors import ErrorClass
from runtime.loop.stagnation import StagnationSignal


class TaskKind(str, Enum):
    """节点种类，同时是阶段的唯一身份。

    每一项都在 runtime.phase.PHASES 里有一条定义，那里说明本阶段
    由谁执行、看得见哪些工具、从哪个 submit_* 交卷。
    """

    SPEC = "spec"          # Pro 起草/修订 SPEC.md
    DISPATCH = "dispatch"  # Pro 决定下一步派谁做什么
    WORKER = "worker"      # Flash 执行一份任务书
    TAKEOVER = "takeover"  # Flash 做不动时 Pro 接手本步实现
    JUDGE = "judge"
    REMEMBER_JUDGE = "remember_judge"  # 裁定 /remember 是否适用于本 lab，不写 SPEC/NOTES
    SUMMARY = "summary"


class Permission(str, Enum):
    READONLY = "readonly"
    WRITE = "write"
    PRO = "pro"


@dataclass
class TaskSnapshot:
    task_id: str
    kind: TaskKind
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
    kind: TaskKind
    permission: Permission
    step_budget: int
    deadline: float
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    step_count: int = 0
    tool_failures: int = 0
    transient_count: int = 0
    """连续瞬时失败数，一次成功清零。tool_failures 才是本节点的累计失败数。"""
    validation_count: int = 0
    consecutive_logic: int = 0
    execution_state: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, str]] = field(default_factory=list)
    node_spec: dict[str, Any] = field(default_factory=dict)

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
                self.transient_count = 0
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
