"""阶段定义表：每一拍「谁执行、看得见什么、从哪交卷」的唯一出处。

TaskKind 是阶段的唯一身份。system prompt、结构化出口、工具可见范围、节点权限、
是否并入 Pro 那条长对话，全部从这里派生；调用方只传 kind。

两个权限概念不要混：
- permission 是节点权限，进 Task 树与审计，决定文件写检查放不放行。
- visible 只影响这一拍的工具表里出现哪些名字，取值必须不宽于 permission。
  规划与判决阶段的节点仍是 pro（要调 pro 门槛的 submit_*），但工具表只给只读，
  因此「写 SPEC 时顺手改了代码」在工具表层面就不可能发生。

本模块只描述阶段，不决定阶段顺序——顺序在 orchestrator 的主流程里。
"""

from dataclasses import dataclass

from config.prompts import (
    DISPATCH_SYSTEM,
    FLASH_SYSTEM,
    JUDGE_SYSTEM,
    REMEMBER_JUDGE_SYSTEM,
    SPEC_SYSTEM,
    SUMMARY_SYSTEM,
    TAKEOVER_SYSTEM,
)
from runtime.schema_call import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
    WRITE_ACCEPTANCE,
)
from runtime.task import Permission, TaskKind
from tools.skill_tool import LOAD_SKILL, LOAD_SKILL_REFERENCE

PRO = "pro"
FLASH = "flash"
# 只读 Pro 阶段默认看不见 pro 权限工具；skill 读取经 extra 点名放行。
# Flash 不在 extra 里，因此调不到。
_SKILL_READ = frozenset({LOAD_SKILL, LOAD_SKILL_REFERENCE})


@dataclass(frozen=True)
class PhaseSpec:
    agent: str
    """pro | flash。决定用哪个模型，以及 profile 按哪个角色注入。"""

    system: str
    submit_tool: str
    """本阶段唯一的结构化出口。调别的 submit_* 会被 loop 当校验错误退回。"""

    permission: Permission | None = None
    """节点权限。留空表示由调用方给——只有 WORKER 如此，它按本波人数决定。"""

    visible: Permission | None = None
    """工具表收窄到这个角色。留空表示跟随节点权限。"""

    extra_tools: frozenset[str] = frozenset()
    """visible 之外额外开放的具体工具名。"""

    shares_thread: bool = False
    """是否并入 Pro 那条贯穿全程的 transcript。"""

    def node_permission(self, fallback: Permission | None = None) -> Permission:
        chosen = self.permission or fallback
        if chosen is None:
            raise ValueError(f"phase {self.submit_tool} 需要调用方提供 permission")
        return chosen

    def visible_role(self, node_permission: Permission) -> str:
        return (self.visible or node_permission).value


PHASES: dict[TaskKind, PhaseSpec] = {
    TaskKind.SPEC: PhaseSpec(
        agent=PRO,
        system=SPEC_SYSTEM,
        submit_tool=SUBMIT_SPEC,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        extra_tools=_SKILL_READ,
        shares_thread=True,
    ),
    TaskKind.REMEMBER_JUDGE: PhaseSpec(
        agent=PRO,
        system=REMEMBER_JUDGE_SYSTEM,
        submit_tool=SUBMIT_REMEMBER,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        # 紧接 SPEC：同一条 Pro 对话里裁定哪些 /remember 适用于本 lab。
        shares_thread=True,
    ),
    TaskKind.DISPATCH: PhaseSpec(
        agent=PRO,
        system=DISPATCH_SYSTEM,
        submit_tool=SUBMIT_DISPATCH,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        # 验收测试在派活时预先写好，所以这一拍要开 write_acceptance。
        extra_tools=_SKILL_READ | {WRITE_ACCEPTANCE},
        shares_thread=True,
    ),
    TaskKind.JUDGE: PhaseSpec(
        agent=PRO,
        system=JUDGE_SYSTEM,
        submit_tool=SUBMIT_JUDGE,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        # 门禁已在沙箱跑过，判决只读代码与 briefs；test_invalid 时可重写测试。
        extra_tools=_SKILL_READ | {WRITE_ACCEPTANCE},
        shares_thread=True,
    ),
    TaskKind.TAKEOVER: PhaseSpec(
        agent=PRO,
        system=TAKEOVER_SYSTEM,
        submit_tool=SUBMIT_BRIEF,
        permission=Permission.PRO,
        # visible 留空：接管要真正改文件跑沙箱，拿节点权限的全集。
        shares_thread=True,
    ),
    TaskKind.SUMMARY: PhaseSpec(
        agent=PRO,
        system=SUMMARY_SYSTEM,
        submit_tool=SUBMIT_SUMMARY,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        extra_tools=_SKILL_READ,
        shares_thread=True,
    ),
    TaskKind.WORKER: PhaseSpec(
        agent=FLASH,
        system=FLASH_SYSTEM,
        submit_tool=SUBMIT_BRIEF,
        # permission 由 permission_for_wave 给：独个 Flash 拿写权限，并行则全部只读。
        shares_thread=False,
    ),
}


def phase_of(kind: TaskKind) -> PhaseSpec:
    try:
        return PHASES[kind]
    except KeyError:
        raise ValueError(f"{kind} 不是一个 agent 阶段") from None
