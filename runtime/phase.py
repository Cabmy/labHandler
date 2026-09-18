"""阶段定义表：每一拍「谁执行、看得见什么、从哪交卷」的唯一出处。

TaskKind 是阶段的唯一身份。prompt、结构化出口、广告工具表、执行允许集、
节点权限、是否并入 Pro 那条长对话，全部从这里派生；调用方只传 kind。

两条上下文链不要混：
- shares_thread：Pro 主线（SPEC → DISPATCH → JUDGE → TAKEOVER → SUMMARY）。
  固定 PRO_SYSTEM，阶段规则以 phase_control 追加进 history。只读阶段广告表固定；
  TAKEOVER 本拍开放写工具，交卷后下一段仍回只读表。
- 独立 agent：Remember-Judge / Worker。各自完整 system 与工具表，
  只把结构化结果交回 Pro。

两个权限概念不要混：
- permission 是节点权限，进 Task 树与审计，决定文件写检查放不放行。
- 广告表可以宽于执行允许集（只读主线为了前缀缓存）；写检查仍看 permission。
  规划阶段节点仍是 pro，广告里有 write_acceptance / 各 submit_*，执行端按
  allow_tools 拒绝当前阶段不该调的名字。

本模块只描述阶段，不决定阶段顺序——顺序在 runtime.lab.runner 的主流程里。
"""

from dataclasses import dataclass
from typing import Any

from config.prompts import (
    DISPATCH_PHASE_PROMPT,
    FLASH_SYSTEM,
    JUDGE_PHASE_PROMPT,
    PRO_SYSTEM,
    REMEMBER_JUDGE_SYSTEM,
    SPEC_PHASE_PROMPT,
    SUMMARY_PHASE_PROMPT,
    TAKEOVER_PHASE_PROMPT,
    render_phase_control,
)
from runtime.loop.schema import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_HALT,
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

_SKILL_READ = frozenset({LOAD_SKILL, LOAD_SKILL_REFERENCE})
_PRO_SESSION = frozenset({"memory_forget", "notes_read"})
_HALT = frozenset({SUBMIT_HALT})

# 只读主线广告表：顺序稳定，跨 SPEC/DISPATCH/JUDGE/SUMMARY 不变。
PRO_READONLY_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "glob_files",
    "grep_files",
    "web_search",
    "memory_search",
    "memory_grep",
    "memory_read",
    "notes_read",
    "memory_forget",
    LOAD_SKILL,
    LOAD_SKILL_REFERENCE,
    "read_profile",
    "sandbox_convert_to_markdown",
    WRITE_ACCEPTANCE,
    SUBMIT_SPEC,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_SUMMARY,
    SUBMIT_HALT,
)

_PRO_RO_BASE = frozenset(
    {
        "read_file",
        "list_dir",
        "glob_files",
        "grep_files",
        "web_search",
        "memory_search",
        "memory_grep",
        "memory_read",
        "read_profile",
        "sandbox_convert_to_markdown",
    }
    | _SKILL_READ
    | _PRO_SESSION
)


def _pro_allow(*names: str) -> frozenset[str]:
    return _PRO_RO_BASE | frozenset(names)


@dataclass(frozen=True)
class PhaseSpec:
    agent: str
    """pro | flash。决定用哪个模型，以及 profile 按哪个角色注入。"""

    prompt: str
    """共享线程时是 phase_control 正文；独立 agent 时是完整 system。"""

    submit_tool: str
    """本阶段主结构化出口。调别的 submit_* 会被 loop 当校验错误退回。
    submit_halt 经 allow_tools / extra_tools 点名，作为万不得已的第二出口。"""

    permission: Permission | None = None
    """节点权限。留空表示由调用方给——只有 WORKER 如此，它按本波人数决定。"""

    visible: Permission | None = None
    """独立 agent 的工具表按这个角色收窄。留空表示跟随节点权限。只读主线不用它
    做广告过滤（广告表固定），只用来标明本阶段是只读。"""

    extra_tools: frozenset[str] = frozenset()
    """独立 agent：visible 之外额外开放的具体工具名。"""

    allow_tools: frozenset[str] | None = None
    """执行允许集。只读主线必填；Remember-Judge 用来收窄可见范围。"""

    advertise_tools: tuple[str, ...] | None = None
    """非空时 openai 工具表用这一份（顺序即广告顺序）。只读主线固定为
    PRO_READONLY_TOOLS。"""

    shares_thread: bool = False
    """是否并入 Pro 那条贯穿全程的 transcript。"""

    tool_choice: str = "auto"
    """本阶段每拍的 tool_choice。`required` = 必须调某个工具，纯文字回复不成立。
    Remember-Judge 用它：光靠 prompt 拦不住它顺手把作业做了。"""

    def node_permission(self, fallback: Permission | None = None) -> Permission:
        chosen = self.permission or fallback
        if chosen is None:
            raise ValueError(f"phase {self.submit_tool} 需要调用方提供 permission")
        return chosen

    def visible_role(self, node_permission: Permission) -> str:
        return (self.visible or node_permission).value

    def halt_allowed(self) -> bool:
        if self.allow_tools is not None:
            return SUBMIT_HALT in self.allow_tools
        return SUBMIT_HALT in self.extra_tools


PHASES: dict[TaskKind, PhaseSpec] = {
    TaskKind.SPEC: PhaseSpec(
        agent=PRO,
        prompt=SPEC_PHASE_PROMPT,
        submit_tool=SUBMIT_SPEC,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        allow_tools=_pro_allow(SUBMIT_SPEC, SUBMIT_HALT),
        advertise_tools=PRO_READONLY_TOOLS,
        shares_thread=True,
    ),
    TaskKind.REMEMBER_JUDGE: PhaseSpec(
        agent=PRO,
        prompt=REMEMBER_JUDGE_SYSTEM,
        submit_tool=SUBMIT_REMEMBER,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        extra_tools=_HALT,
        allow_tools=frozenset({"read_file", "list_dir", "glob_files"}),
        tool_choice="required",
    ),
    TaskKind.DISPATCH: PhaseSpec(
        agent=PRO,
        prompt=DISPATCH_PHASE_PROMPT,
        submit_tool=SUBMIT_DISPATCH,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        allow_tools=_pro_allow(SUBMIT_DISPATCH, WRITE_ACCEPTANCE, SUBMIT_HALT),
        advertise_tools=PRO_READONLY_TOOLS,
        shares_thread=True,
    ),
    TaskKind.JUDGE: PhaseSpec(
        agent=PRO,
        prompt=JUDGE_PHASE_PROMPT,
        submit_tool=SUBMIT_JUDGE,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        allow_tools=_pro_allow(SUBMIT_JUDGE, WRITE_ACCEPTANCE, SUBMIT_HALT),
        advertise_tools=PRO_READONLY_TOOLS,
        shares_thread=True,
    ),
    TaskKind.TAKEOVER: PhaseSpec(
        agent=PRO,
        prompt=TAKEOVER_PHASE_PROMPT,
        submit_tool=SUBMIT_BRIEF,
        permission=Permission.PRO,
        extra_tools=_HALT,
        shares_thread=True,
    ),
    TaskKind.SUMMARY: PhaseSpec(
        agent=PRO,
        prompt=SUMMARY_PHASE_PROMPT,
        submit_tool=SUBMIT_SUMMARY,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        allow_tools=_pro_allow(SUBMIT_SUMMARY),
        advertise_tools=PRO_READONLY_TOOLS,
        shares_thread=True,
    ),
    TaskKind.WORKER: PhaseSpec(
        agent=FLASH,
        prompt=FLASH_SYSTEM,
        submit_tool=SUBMIT_BRIEF,
        extra_tools=_HALT,
        # permission 由 permission_for_wave 给：独个 Flash 拿写权限，并行则全部只读。
    ),
}


def phase_of(kind: TaskKind) -> PhaseSpec:
    try:
        return PHASES[kind]
    except KeyError:
        raise ValueError(f"{kind} 不是一个 agent 阶段") from None


def phase_control_message(kind: TaskKind) -> dict[str, Any]:
    """Pro 主线追加到 history 尾部的宿主阶段控制消息。"""
    phase = phase_of(kind)
    permission = (phase.visible or phase.permission or Permission.READONLY).value
    return {
        "role": "user",
        "content": render_phase_control(
            phase=kind.value,
            instructions=phase.prompt,
            submit_tool=phase.submit_tool,
            permission=permission,
        ),
    }


def system_for(kind: TaskKind) -> str:
    """这一拍真正发给模型的 system：Pro 主线永远是 PRO_SYSTEM。"""
    phase = phase_of(kind)
    return PRO_SYSTEM if phase.shares_thread else phase.prompt
