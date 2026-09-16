"""submit_* / write_acceptance 的 JSON schema 常量。校验见 runtime.loop.parse。"""

from typing import Any

from runtime.context.notes import MEMORY_ENTRY_MAX
from runtime.lab.spec import MAX_ASSIGNMENTS

# Flash brief 只含本步实际改动与碰到的错误，长度上限 BRIEF_MAX。
BRIEF_MAX = 600

SUBMIT_SPEC = "submit_spec"
SUBMIT_DISPATCH = "submit_dispatch"
WRITE_ACCEPTANCE = "write_acceptance"
SUBMIT_BRIEF = "submit_brief"
SUBMIT_JUDGE = "submit_judge"
SUBMIT_REMEMBER = "submit_remember"
SUBMIT_SUMMARY = "submit_summary"
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
        "overview": {"type": "string", "description": "打算怎么做，方案层面的概述"},
        "interfaces": {
            "type": "array",
            "items": INTERFACE_SCHEMA,
            "description": "全局固定的名字，任何一步都不得改动",
        },
        "deliverables": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance_strategy": {"type": "string", "description": "整体打算怎么验收"},
        "milestones": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": "自顶向下的推进顺序，每条是一小步，不是整个项目",
        },
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
        "id": {"type": "string", "description": "本次派发内唯一"},
        "domain": {
            "type": "string",
            "description": "这个 worker 负责的领域。同一次派发中各不相同、互不重叠",
        },
        "goal": {"type": "string", "minLength": 8, "description": "一个 worker 一步能做完的量"},
        "spec": {"type": "string", "description": "写给这个 worker 的详细任务书正文"},
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
        "step_goal": {"type": "string", "description": "这一步要推进什么"},
        "assignments": {
            "type": "array",
            "maxItems": MAX_ASSIGNMENTS,
            "items": ASSIGNMENT_SCHEMA,
            "description": f"本步派出的 worker，最多 {MAX_ASSIGNMENTS} 个。全部完成时给空数组",
        },
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
        "memory_append": {
            "type": "string",
            "maxLength": MEMORY_ENTRY_MAX,
            "description": "新增一条不超过 80 字符的不变量。空则不写。禁止贴实现细节或复述 SPEC.md。",
        },
        "memory_remove": {
            "type": "array",
            "items": {"type": "string"},
            "description": "删除 MEMORY.md 中正文等于或包含该字符串的条目。过时了就删，不要只追加。",
        },
        "memory_replace": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["old", "new"],
                "properties": {
                    "old": {"type": "string", "description": "要改的那条：全文或能唯一定位的子串"},
                    "new": {
                        "type": "string",
                        "maxLength": MEMORY_ENTRY_MAX,
                        "description": "改写后的短不变量；空字符串表示删除",
                    },
                },
            },
            "description": "改已有条目。事实变了就改，不要另起一行让旧事实继续常驻。",
        },
        "forget_append": {
            "type": "string",
            "description": "已确认与任务无关的杂乱上下文描述。压缩总结时会被刻意忽略。",
        },
        "rule_verdicts": {
            "type": "array",
            "description": "对本 lab 已裁定适用的每条 /remember 规则给出对照结论。finish 时必须全部 satisfied。",
            "items": {
                "type": "object",
                "required": ["rule", "satisfied", "note"],
                "properties": {
                    "rule": {"type": "string"},
                    "satisfied": {"type": "boolean"},
                    "note": {"type": "string"},
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
            "items": {
                "type": "object",
                "required": ["rule", "applies"],
                "properties": {
                    "rule": {"type": "string"},
                    "applies": {"type": "boolean"},
                },
            },
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
        "task_id": {"type": "string"},
        "filename": {"type": "string"},
        "content": {"type": "string"},
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
}


