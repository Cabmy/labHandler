"""LabRunner 用的纯函数：payload 抽取、续跑切分、门禁取最差。"""

from dataclasses import replace
from typing import Any

from runtime.lab.accept import AcceptResult, NO_HARD_CRITERIA, PASS
from runtime.lab.spec import Assignment, ProjectSpec
from runtime.loop.schema import SUBMIT_SPEC
from runtime.task import RuntimeTask, TaskKind, TaskStatus, TaskTree

_PROGRESS_CAP = 8000


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
) -> tuple[list[Assignment], list[str]]:
    """把 assignments 分成「本步要跑」和「续跑可跳过」；跑的那组 id 在本次 run 内唯一。

    resumable 命中：产物指纹仍完好，跳过重跑，已落地的文件不被覆盖。
    去重只改 executed 里出现过的 id：账本以 id 为键，给 resumable 候选改名会让
    satisfied() 永远对不上。
    """
    run: list[Assignment] = []
    skipped: list[str] = []
    for i, a in enumerate(assignments):
        original = a.id or f"s{step}_{i}"
        if original in resumable:
            resumable.discard(original)
            skipped.append(original)
            continue

        aid = original
        suffix = 0
        while aid in executed:
            suffix += 1
            aid = f"{original}_s{step}" if suffix == 1 else f"{original}_s{step}_{suffix}"
        executed.add(aid)

        # 改名只影响账本与 span 的键；验收目录仍按 Pro 写测试时用的原名查找
        run.append(replace(a, id=aid, acceptance_id=a.gate_id or original))
    return run, skipped


def render_progress(progress: list[tuple[str, str]]) -> str:
    if not progress:
        return "（还没有完成任何步骤）"
    lines = [f"- **{aid}**：{summary}" for aid, summary in progress if summary]
    return "\n".join(lines)[-_PROGRESS_CAP:] or "（还没有完成任何步骤）"


def worst_gate(a: AcceptResult, b: AcceptResult) -> AcceptResult:
    order = {PASS: 1, NO_HARD_CRITERIA: 2, "test_invalid": 3, "fail": 4}
    return b if order.get(b.state, 0) > order.get(a.state, 0) else a


def step_gate(results: list[tuple[RuntimeTask, dict[str, Any]]]) -> AcceptResult:
    """整步的门禁结论：有 fail 取 fail，全无硬指标取 no_hard_criteria。"""
    states = [
        str((brief.get("tests") or {}).get("state") or NO_HARD_CRITERIA)
        for _, brief in results
    ]
    for priority in ("fail", "test_invalid", PASS):
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
