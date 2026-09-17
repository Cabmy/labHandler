"""submit_* / write_acceptance 的 JSON schema 常量。校验见 runtime.loop.parse。"""

from typing import Any

from runtime.context.notes import NOTES_ENTRY_MAX
from runtime.lab.spec import MAX_ASSIGNMENTS

# 短句用 notes_append；长文用 notes_write，NOTES.md 只留文件名指针。
NOTES_WRITE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "文件名，如 ttl.md。NOTES.md 只留这一条指针"},
        "content": {"type": "string", "description": "较长的必须记住的正文。短句请用 notes_append"},
    },
}
FORGET_APPEND = {
    "type": "string",
    "description": "无关噪声（已放弃的路径、死胡同探测）。下次 compact 时 Flash 会从摘要里去掉。热卡片请用 memory_forget。",
}

# Flash brief 只含本步实际改动与碰到的错误，长度上限 BRIEF_MAX。
BRIEF_MAX = 600

SUBMIT_SPEC = "submit_spec"
SUBMIT_DISPATCH = "submit_dispatch"
WRITE_ACCEPTANCE = "write_acceptance"
SUBMIT_BRIEF = "submit_brief"
SUBMIT_JUDGE = "submit_judge"
SUBMIT_REMEMBER = "submit_remember"
SUBMIT_SUMMARY = "submit_summary"
SUBMIT_HALT = "submit_halt"
SUBMIT_DREAM = "submit_dream"
SUBMIT_SKILL_EDIT = "submit_skill_edit"

# 结构化出口：无副作用，能否调用由 for_role 的工具表决定。
# write_acceptance 写文件，不在此集合，必须走 check_write。
SCHEMA_TOOLS = {
    SUBMIT_SPEC,
    SUBMIT_DISPATCH,
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SUMMARY,
    SUBMIT_HALT,
    SUBMIT_DREAM,
    SUBMIT_SKILL_EDIT,
}

INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "验收代码会按这个名字导入/调用，必须精确"},
        "kind": {"type": "string", "enum": ["function", "class", "cli", "file", "http"]},
        "signature": {"type": "string", "description": "如 solve(nums: list[int]) -> int"},
        "description": {"type": "string"},
    },
}

SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "milestones"],
    "properties": {
        "goal": {"type": "string", "minLength": 8, "description": "整个任务的总目标"},
        "overview": {
            "type": "string",
            "description": "方案，以及你已从材料读到的要点。不要把「去读文件」写成里程碑",
        },
        "interfaces": {
            "type": "array",
            "items": INTERFACE_SCHEMA,
            "description": "全局固定的名字，任何一步都不得改动",
        },
        "deliverables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "最终交给用户的文件。报告仅当作业主交付物是报告时才列",
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance_strategy": {
            "type": "string",
            "description": "一段话：你将如何用 write_acceptance 写门禁。必须是一个字符串，不要传 JSON 对象",
        },
        "milestones": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": "少量有意义的 Flash 产品块（一题/一文件/一函数），不是阅读/设计/测试/打磨清单。一道题一条即可",
        },
        "forget_append": FORGET_APPEND,
        "notes_append": {
            "type": "string",
            "maxLength": NOTES_ENTRY_MAX,
            "description": "新增一条不超过 80 字符的不变量。空则不写。",
        },
        "notes_write": NOTES_WRITE,
    },
    "additionalProperties": True,
}

MIN_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string"},
        "milestones": {"type": "array", "items": {"type": "string"}},
    },
}

ASSIGNMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "domain", "goal", "testable"],
    "properties": {
        "id": {
            "type": "string",
            "description": "本次派发内唯一的一个字符串。write_acceptance 的 task_id 必须与此相同。不要用 *-acceptance 当第二工人",
        },
        "domain": {
            "type": "string",
            "description": "这个 worker 负责的领域。同一次派发中各不相同、互不重叠",
        },
        "goal": {
            "type": "string",
            "minLength": 8,
            "description": "产品工作。不要写「提供单元测试」；测试是 write_acceptance，除非 expected_artifacts 点名了要上交的 test_*.py",
        },
        "spec": {
            "type": "string",
            "description": "写给这个 worker 的详细任务书。它的对话是空的，把本步需要的事实写在这里，不要派它去读 catalog",
        },
        "expected_artifacts": {"type": "array", "items": {"type": "string"}},
        "interfaces": {
            "type": "array",
            "items": INTERFACE_SCHEMA,
            "description": "该 worker 必须实现的确切名字。testable 为 true 时必填",
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "done_when": {"type": "array", "items": {"type": "string"}},
        "testable": {
            "type": "boolean",
            "description": "有可量化指标且产物可被程序检验时为 true",
        },
        "acceptance_intent": {"type": "string"},
        "acceptance_files": {"type": "array", "items": {"type": "string"}},
    },
}

DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["step_goal", "assignments"],
    "properties": {
        "step_goal": {
            "type": "string",
            "description": "这一步的产品目标。不要写「并提供单元测试」；门禁是你写的 write_acceptance",
        },
        "assignments": {
            "type": "array",
            "maxItems": MAX_ASSIGNMENTS,
            "items": ASSIGNMENT_SCHEMA,
            "description": f"本步派出的 Flash，最多 {MAX_ASSIGNMENTS} 个。Flash 侧全部完成时给空数组",
        },
        "forget_append": FORGET_APPEND,
        "notes_append": {
            "type": "string",
            "maxLength": NOTES_ENTRY_MAX,
            "description": "新增一条不超过 80 字符的不变量。空则不写。",
        },
        "notes_write": NOTES_WRITE,
    },
    "additionalProperties": True,
}

MIN_DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["assignments"],
    "properties": {
        "step_goal": {"type": "string"},
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "goal"],
                "properties": {"id": {"type": "string"}, "goal": {"type": "string"}},
            },
        },
    },
}

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "brief"],
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "blocked", "spec_invalid", "failed"]},
        "brief": {
            "type": "string",
            "minLength": 8,
            "maxLength": BRIEF_MAX,
            "description": "本步实际做了什么、改了哪些文件、碰到什么错误。短。不要写没做的后续步骤。",
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

MIN_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "brief"],
    "properties": {
        "outcome": {"type": "string"},
        "brief": {"type": "string"},
    },
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "evidence"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["continue", "revise_spec", "takeover", "finish"],
        },
        "evidence": {"type": "string", "minLength": 4},
        "notes_append": {
            "type": "string",
            "maxLength": NOTES_ENTRY_MAX,
            "description": "新增一条不超过 80 字符的不变量。空则不写。禁止贴实现细节或复述 SPEC.md。",
        },
        "notes_remove": {
            "type": "array",
            "items": {"type": "string"},
            "description": "删除 NOTES.md 中正文等于或包含该字符串的条目。过时了就删，不要只追加。",
        },
        "notes_replace": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["old", "new"],
                "properties": {
                    "old": {"type": "string", "description": "要改的那条：全文或能唯一定位的子串"},
                    "new": {
                        "type": "string",
                        "maxLength": NOTES_ENTRY_MAX,
                        "description": "改写后的短不变量；空字符串表示删除",
                    },
                },
            },
            "description": "改已有条目。事实变了就改，不要另起一行让旧事实继续常驻。",
        },
        "notes_write": NOTES_WRITE,
        "forget_append": FORGET_APPEND,
        "rule_verdicts": {
            "type": "array",
            "description": "对本 lab 已裁定适用的每条 /remember 规则给出对照结论。按用户消息里的编号对齐。finish 时必须全部 satisfied。",
            "items": {
                "type": "object",
                "required": ["index", "satisfied", "note"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "用户消息里印在该规则前面的编号。身份靠它对齐，不要靠抄规则原文",
                    },
                    "satisfied": {"type": "boolean"},
                    "note": {"type": "string"},
                    "rule": {
                        "type": "string",
                        "description": "可选，仅作可读标注；抄错不影响对齐",
                    },
                },
            },
        },
    },
    "additionalProperties": True,
}

REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "description": "每条 /remember 规则一个裁定，按用户消息里的编号对齐",
            "items": {
                "type": "object",
                "required": ["index", "applies"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "用户消息里印在该规则前面的编号。身份靠它对齐，不要靠抄规则原文",
                    },
                    "applies": {"type": "boolean"},
                    "rule": {
                        "type": "string",
                        "description": "可选，仅作可读标注；抄错不影响对齐",
                    },
                },
            },
        },
    },
}

HALT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["reason", "need_from_user"],
    "properties": {
        "reason": {
            "type": "string",
            "minLength": 24,
            "description": "缺了什么、为何材料/工具/合理默认都拿不到。禁止用来跳过能做完的作业。",
        },
        "need_from_user": {
            "type": "string",
            "minLength": 8,
            "description": "用户必须补充的信息或文件",
        },
    },
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["user_summary"],
    "properties": {
        "user_summary": {"type": "string", "minLength": 8},
        "knowledge_cards": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "content"],
                "properties": {
                    "type": {"type": "string", "enum": ["lesson", "strategy", "pattern"]},
                    "content": {"type": "string"},
                },
            },
        },
    },
}

ACCEPT_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "filename", "content"],
    "properties": {
        "task_id": {
            "type": "string",
            "description": "一个字符串，必须等于即将派发的 assignment id（如 two_sum）。不是数组。文件落在 acceptance/<task_id>/",
        },
        "filename": {
            "type": "string",
            "description": "只要 .py 文件名（test_two_sum.py）。不要写成 acceptance/<id>/test_....py",
        },
        "content": {
            "type": "string",
            "description": (
                "整份测试文件的一个字符串。行与行用 \\n 连接，不要传字符串数组。"
                "只断言材料或 SPEC 明确要求的行为：不要自己发明错误处理（如空输入抛异常），"
                "也不要断言题目没规定的那一个答案（多解时校验结果是否成立，不要写死下标）"
            ),
        },
    },
}

DREAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merged": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["content", "source_ids"],
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "source_ids": {
                        "type": "array",
                        "minItems": 2,
                        "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                    },
                },
            },
        },
        "retire_ids": {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        },
    },
}

SKILL_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "operations"],
    "properties": {
        "summary": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "file"],
                "properties": {
                    "action": {"type": "string", "enum": ["write", "delete"]},
                    "file": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
            },
        },
    },
}

_FULL = {
    SUBMIT_SPEC: SPEC_SCHEMA,
    SUBMIT_DISPATCH: DISPATCH_SCHEMA,
    WRITE_ACCEPTANCE: ACCEPT_FILE_SCHEMA,
    SUBMIT_BRIEF: BRIEF_SCHEMA,
    SUBMIT_JUDGE: JUDGE_SCHEMA,
    SUBMIT_REMEMBER: REMEMBER_SCHEMA,
    SUBMIT_SUMMARY: SUMMARY_SCHEMA,
    SUBMIT_HALT: HALT_SCHEMA,
    SUBMIT_DREAM: DREAM_SCHEMA,
    SUBMIT_SKILL_EDIT: SKILL_EDIT_SCHEMA,
}

_MIN = {
    SUBMIT_SPEC: MIN_SPEC_SCHEMA,
    SUBMIT_DISPATCH: MIN_DISPATCH_SCHEMA,
    SUBMIT_BRIEF: MIN_BRIEF_SCHEMA,
    SUBMIT_JUDGE: {
        "type": "object",
        "required": ["decision", "evidence"],
        "properties": {
            "decision": {"type": "string"},
            "evidence": {"type": "string"},
        },
    },
    SUBMIT_SUMMARY: {
        "type": "object",
        "required": ["user_summary"],
        "properties": {"user_summary": {"type": "string"}},
    },
    SUBMIT_REMEMBER: {
        "type": "object",
        "required": ["verdicts"],
        "properties": {"verdicts": {"type": "array"}},
    },
    SUBMIT_HALT: {
        "type": "object",
        "required": ["reason", "need_from_user"],
        "properties": {
            "reason": {"type": "string"},
            "need_from_user": {"type": "string"},
        },
    },
}


