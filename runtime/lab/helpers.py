"""LabRunner 用的纯函数：payload 抽取、续跑切分、门禁取最差。"""

from dataclasses import replace
from typing import Any

from runtime.lab.accept import AcceptResult, FAIL, NO_HARD_CRITERIA, PASS, TEST_INVALID
from runtime.lab.spec import Assignment, ProjectSpec
from runtime.loop.schema import SUBMIT_HALT, SUBMIT_SPEC
from runtime.task import RuntimeTask, TaskKind, TaskStatus, TaskTree

_PROGRESS_CAP = 8000


def halt_of(submit: dict[str, Any] | None) -> dict[str, Any] | None:
    """submit_halt 的 payload；名字对不上返回 None。"""
    if not submit or submit.get("name") != SUBMIT_HALT:
        return None
    payload = submit.get("payload")
    if isinstance(payload, dict) and str(payload.get("reason") or "").strip():
        return payload
    return None


def payload_of(submit: dict[str, Any], name: str) -> dict[str, Any]:
    """名字对得上才取出 payload；对不上返回空 dict，上层不会读到错阶段字段。"""
    if not submit:
        return {}
    if "name" not in submit:
        return submit
    return submit.get("payload", {}) if submit["name"] == name else {}


def partition(
    assignments: list[Assignment],
    resumable: set[str],
    executed: set[str],
    step: int,
    gates: dict[str, str] | None = None,
    broken: set[str] | None = None,
) -> tuple[list[Assignment], list[str], list[Assignment]]:
    """分成「本步跑 Flash」「续跑跳过」「只重跑门禁」。

    resumable：产物指纹仍完好，跳过。
    同一 id 本 run 已跑过：test_invalid → 只重跑 pytest（Pro 该改门禁，不要再派 Flash）；
    fail → 再派同一个 id 让 Flash 改产物；pass / 其它 → 跳过，避免打磨循环。
    broken：门禁已判定跑不起来，重跑只会再拿一次 test_invalid，直接跳过。
    """
    by_gate = gates or {}
    dead = broken or set()
    run: list[Assignment] = []
    skipped: list[str] = []
    replay: list[Assignment] = []
    for i, a in enumerate(assignments):
        original = a.id or f"s{step}_{i}"
        gate_id = a.gate_id or original
        pinned = replace(a, id=original, acceptance_id=gate_id)
        if original in resumable:
            resumable.discard(original)
            skipped.append(original)
            continue
        if original in executed:
            state = by_gate.get(gate_id) or by_gate.get(original) or ""
            if state == TEST_INVALID and gate_id not in dead and original not in dead:
                replay.append(pinned)
            elif state == FAIL:
                run.append(pinned)
            else:
                skipped.append(original)
            continue
        executed.add(original)
        run.append(pinned)
    return run, skipped, replay


def render_progress(progress: list[tuple[str, str]]) -> str:
    if not progress:
        return "（还没有完成任何步骤）"
    lines = [f"- **{aid}**：{summary}" for aid, summary in progress if summary]
    return "\n".join(lines)[-_PROGRESS_CAP:] or "（还没有完成任何步骤）"


def worst_gate(a: AcceptResult, b: AcceptResult) -> AcceptResult:
    order = {PASS: 1, NO_HARD_CRITERIA: 2, TEST_INVALID: 3, FAIL: 4}
    return b if order.get(b.state, 0) > order.get(a.state, 0) else a


def step_gate(results: list[tuple[RuntimeTask, dict[str, Any]]]) -> AcceptResult:
    """整步的门禁结论：有 fail 取 fail，全无硬指标取 no_hard_criteria。"""
    states = [
        str((brief.get("tests") or {}).get("state") or NO_HARD_CRITERIA)
        for _, brief in results
    ]
    for priority in (FAIL, TEST_INVALID, PASS):
        if priority in states:
            return AcceptResult(state=priority)
    return AcceptResult(state=NO_HARD_CRITERIA)


def resume_spec(tree: TaskTree) -> ProjectSpec | None:
    """从已 COMPLETED 的 SPEC 节点取出 payload。没有可用 goal 时返回 None，走新起草路径。"""
    for task in tree.nodes.values():
        if task.kind is not TaskKind.SPEC or task.status is not TaskStatus.COMPLETED:
            continue
        brief = task.brief or {}
        payload = brief.get("payload") if brief.get("name") == SUBMIT_SPEC else brief
        if payload and payload.get("goal"):
            return ProjectSpec.from_payload(payload)
    return None
