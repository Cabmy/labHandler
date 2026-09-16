"""决策器：读 Task 快照 + 信号，输出唯一 Decision。无状态。"""

from dataclasses import dataclass

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass
from runtime.loop.stagnation import StagnationSignal, NUDGE_TEXT
from runtime.task import TaskSnapshot, TaskStatus
from runtime.loop.retry import delay_for


@dataclass(frozen=True)
class Decision:
    kind: str  # continue | retry_tool | nudge | force_brief | stop
    delay: float = 0.0
    text: str = ""
    reason: str = ""

    @property
    def is_stop(self) -> bool:
        return self.kind == "stop"


def decide(
    snap: TaskSnapshot,
    *,
    error_class: ErrorClass,
    stagnation: StagnationSignal,
    settings: RuntimeSettings,
    now: float,
) -> Decision:
    if snap.status in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
        return Decision(kind="stop", reason=snap.status.value.lower())

    if error_class is ErrorClass.AUTH:
        return Decision(kind="stop", reason="auth")
    if error_class is ErrorClass.FATAL:
        return Decision(kind="stop", reason="fatal")
    if error_class is ErrorClass.LOOP or stagnation is StagnationSignal.LOOP_CONFIRMED:
        return Decision(kind="stop", reason="LOOP_DETECTED")

    if now >= snap.deadline:
        return Decision(kind="stop", reason="deadline")

    if snap.step_count >= snap.step_budget:
        return Decision(kind="force_brief", reason="step_budget")

    if stagnation is StagnationSignal.REPEAT:
        return Decision(kind="nudge", text=NUDGE_TEXT)

    if error_class is ErrorClass.TRANSIENT:
        if snap.transient_count >= settings.transient_retry_max:
            return Decision(
                kind="nudge",
                text="Transient retries exhausted. Switch tools or change approach. Do not mark spec_invalid.",
            )
        return Decision(kind="retry_tool", delay=delay_for(snap.transient_count))

    if error_class is ErrorClass.LOGIC:
        if snap.consecutive_logic >= settings.consecutive_logic_failure_max:
            return Decision(kind="stop", reason="logic_exhausted")
        return Decision(kind="continue")

    return Decision(kind="continue")
