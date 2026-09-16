"""SPEC 模型：ProjectSpec（总纲）与 Assignment（单份 Flash 任务书）。

SPEC.md 由 Pro 维护，是后续每一步派发的共同依据。一份 Assignment 自包含，
且 domain 在同一次派发内互不重叠。一次派发最多 MAX_ASSIGNMENTS 个 Flash；
多于 1 个时全部降只读。
"""

import json
from dataclasses import dataclass, field
from typing import Any

# 一次派发最多几个 Flash。n>1 时 permission 全部为 readonly。
MAX_ASSIGNMENTS = 3


@dataclass(frozen=True)
class InterfaceItem:
    """一个 Flash 必须精确实现的可被调用/被检查的名字。"""

    name: str
    kind: str = "function"  # function | class | cli | file | http
    signature: str = ""
    description: str = ""

    @classmethod
    def from_payload(cls, raw: Any) -> "InterfaceItem | None":
        if isinstance(raw, str):
            return cls(name=raw.strip()) if raw.strip() else None
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            kind=str(raw.get("kind") or "function").strip() or "function",
            signature=str(raw.get("signature") or "").strip(),
            description=str(raw.get("description") or "").strip(),
        )

    def render(self) -> str:
        head = f"- `{self.signature or self.name}`（{self.kind}）"
        return f"{head}：{self.description}" if self.description else head

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "signature": self.signature,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProjectSpec:
    """SPEC.md 的结构化形式：整个任务的总纲，由 Pro 维护。"""

    goal: str
    overview: str = ""
    interfaces: list[InterfaceItem] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_strategy: str = ""
    milestones: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "ProjectSpec":
        return cls(
            goal=str(raw.get("goal") or "").strip(),
            overview=str(raw.get("overview") or "").strip(),
            interfaces=_parse_interfaces(raw.get("interfaces")),
            deliverables=_str_list(raw.get("deliverables")),
            constraints=_str_list(raw.get("constraints")),
            acceptance_strategy=str(raw.get("acceptance_strategy") or "").strip(),
            milestones=_str_list(raw.get("milestones")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "overview": self.overview,
            "interfaces": [i.to_dict() for i in self.interfaces],
            "deliverables": list(self.deliverables),
            "constraints": list(self.constraints),
            "acceptance_strategy": self.acceptance_strategy,
            "milestones": list(self.milestones),
        }

    def render(self) -> str:
        """渲染成 SPEC.md。这份文件是后续每一步派发的共同依据。"""
        parts = ["# SPEC", "", "## 总目标", self.goal or "(未填写)"]
        if self.overview:
            parts += ["", "## 方案概述", self.overview]
        if self.interfaces:
            parts += [
                "",
                "## 全局接口契约",
                "以下名字在整个任务范围内固定，任何一步都不得改名或改签名。",
                *[i.render() for i in self.interfaces],
            ]
        parts += _section("交付物", self.deliverables)
        parts += _section("全局约束", self.constraints)
        parts += _section("里程碑（自顶向下的推进顺序）", self.milestones)
        if self.acceptance_strategy:
            parts += ["", "## 验收策略", self.acceptance_strategy]
        return "\n".join(parts).strip() + "\n"


@dataclass(frozen=True)
class Assignment:
    """一次派发中交给某一个 Flash 的任务书。"""

    id: str
    domain: str
    goal: str
    # 验收文件落盘时用的 id。Pro 写 write_acceptance 时用的是它原本给的 id，
    # 之后 id 可能因为重名被改写，门禁必须仍按原名去找测试。
    acceptance_id: str = ""
    spec: str = ""
    expected_artifacts: list[str] = field(default_factory=list)
    interfaces: list[InterfaceItem] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    testable: bool = False
    acceptance_intent: str = ""
    acceptance_files: list[str] = field(default_factory=list)

    @property
    def gate_id(self) -> str:
        """去 acceptance/ 下找测试时用的目录名。"""
        return self.acceptance_id or self.id

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "Assignment":
        return cls(
            id=str(raw.get("id") or "").strip(),
            domain=str(raw.get("domain") or "").strip(),
            goal=str(raw.get("goal") or "").strip(),
            acceptance_id=str(raw.get("acceptance_id") or "").strip(),
            spec=str(raw.get("spec") or "").strip(),
            expected_artifacts=_str_list(raw.get("expected_artifacts")),
            interfaces=_parse_interfaces(raw.get("interfaces")),
            constraints=_str_list(raw.get("constraints")),
            done_when=_str_list(raw.get("done_when")),
            testable=bool(raw.get("testable")),
            acceptance_intent=str(raw.get("acceptance_intent") or "").strip(),
            acceptance_files=_str_list(raw.get("acceptance_files")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "goal": self.goal,
            "acceptance_id": self.gate_id,
            "spec": self.spec,
            "expected_artifacts": list(self.expected_artifacts),
            "interfaces": [i.to_dict() for i in self.interfaces],
            "constraints": list(self.constraints),
            "done_when": list(self.done_when),
            "testable": self.testable,
            "acceptance_intent": self.acceptance_intent,
            "acceptance_files": list(self.acceptance_files),
        }


@dataclass(frozen=True)
class Dispatch:
    """Pro 对"下一步做什么"的一次决定。assignments 为空表示无事可做。"""

    step_goal: str
    assignments: list[Assignment] = field(default_factory=list)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "Dispatch":
        items = raw.get("assignments") or []
        if isinstance(items, str):
            try:
                parsed = json.loads(items)
            except json.JSONDecodeError:
                parsed = []
            items = parsed if isinstance(parsed, list) else []
        if not isinstance(items, list):
            items = []
        return cls(
            step_goal=str(raw.get("step_goal") or "").strip(),
            assignments=[
                Assignment.from_payload(a) for a in items if isinstance(a, dict)
            ],
        )

    @property
    def is_empty(self) -> bool:
        return not self.assignments


def render_assignment(
    assignment: Assignment,
    *,
    user_request: str,
    project_spec: str,
    step_goal: str,
    done_so_far: list[tuple[str, str]] | None = None,
    parallel_domains: list[str] | None = None,
) -> str:
    """渲染成这个 Flash 本轮唯一的任务输入。"""
    parts: list[str] = [
        f"# 任务书 {assignment.id}",
        "",
        f"**负责领域**：{assignment.domain or assignment.goal}",
        "",
        "## 本步目标",
        assignment.goal,
    ]
    if assignment.spec:
        parts += ["", "## 详细说明", assignment.spec]

    parts += [
        "",
        "## 这一步在整体中的位置",
        f"当前步骤要推进的是：{step_goal}" if step_goal else "（未说明）",
        "",
        "本任务书只覆盖上面这一小段，不要越界去做整个项目。"
        "后续步骤由 Pro 调度；brief 只写你做了什么和碰到的错误，不要写本步没做的事。",
    ]

    if done_so_far:
        lines = "\n".join(f"- **{aid}**：{summary}" for aid, summary in done_so_far if summary)
        if lines:
            parts += ["", "## 已经完成的部分", lines]

    if parallel_domains:
        others = "、".join(parallel_domains)
        parts += [
            "",
            "## 同时进行的其它领域",
            f"本轮还有其它 worker 分别负责：{others}。不要碰它们的领域。",
        ]

    if assignment.interfaces:
        parts += [
            "",
            "## 接口契约（名字必须完全一致）",
            "验收代码会直接按这些名字导入和调用。改名、换签名、换文件位置都会导致验收失败。",
            *[i.render() for i in assignment.interfaces],
        ]

    parts += _section("预期产物", assignment.expected_artifacts)
    parts += _section("约束", assignment.constraints)
    parts += _section("完成判据", assignment.done_when)

    if assignment.testable:
        files = "、".join(assignment.acceptance_files) or "已就绪的验收文件"
        parts += [
            "",
            "## 验收方式",
            f"本步有可量化指标，测试已预先写好（{files}）。{assignment.acceptance_intent}",
            "你提交 brief 后 harness 会自动在沙箱里跑这些测试，结果不由你填写。",
        ]
    else:
        parts += [
            "",
            "## 验收方式",
            "本步没有可量化的程序性指标，不会跑自动测试。"
            "请在 brief 里给出可核查的事实（改了哪些文件、依据是什么），供 Judge 语义判断。",
        ]

    if project_spec:
        parts += ["", "## 总纲 SPEC.md（供对齐，不要重复实现）", project_spec]

    parts += [
        "",
        "## 原始用户诉求",
        user_request.strip(),
        "",
        "完成后调用 `submit_brief` 结束。瞬时错误不算 spec_invalid。",
    ]
    return "\n".join(parts).strip() + "\n"


def _section(title: str, items: list[str]) -> list[str]:
    body = [f"- {x}" for x in items if str(x).strip()]
    return ["", f"## {title}", *body] if body else []


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _parse_interfaces(raw: Any) -> list[InterfaceItem]:
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in (InterfaceItem.from_payload(x) for x in raw)
        if item is not None
    ]
