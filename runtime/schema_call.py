"""submit_* / write_acceptance 的 JSON schema 与校验。"""

from __future__ import annotations

import json
from typing import Any

import jsonschema

from runtime.errors import ErrorClass


SUBMIT_PLAN = "submit_plan"
WRITE_ACCEPTANCE = "write_acceptance"
SUBMIT_BRIEF = "submit_brief"
SUBMIT_JUDGE = "submit_judge"
SUBMIT_SUMMARY = "submit_summary"
SUBMIT_DREAM = "submit_dream"
SUBMIT_SKILL_EDIT = "submit_skill_edit"

SCHEMA_TOOLS = {
    SUBMIT_PLAN,
    WRITE_ACCEPTANCE,
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_SUMMARY,
    SUBMIT_DREAM,
    SUBMIT_SKILL_EDIT,
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["nodes"],
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "name", "desc", "depends_on"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "desc": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "expected_artifacts": {"type": "array", "items": {"type": "string"}},
                    "acceptance_intent": {"type": "string"},
                    "acceptance_files": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
    "additionalProperties": True,
}

MIN_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["nodes"],
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "name", "desc"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "desc": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "brief"],
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "blocked", "plan_invalid", "failed"]},
        "brief": {"type": "string", "minLength": 8},
        "completed": {"type": "array", "items": {"type": "string"}},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
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
            "enum": ["accept", "new_plan", "takeover", "finish"],
        },
        "evidence": {"type": "string", "minLength": 4},
        "notes_append": {"type": "string"},
    },
    "additionalProperties": True,
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
        "merged": {"type": "array"},
        "retire_ids": {"type": "array"},
    },
}

SKILL_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "operations"],
    "properties": {
        "summary": {"type": "string"},
        "operations": {"type": "array"},
    },
}

_FULL = {
    SUBMIT_PLAN: PLAN_SCHEMA,
    WRITE_ACCEPTANCE: ACCEPT_FILE_SCHEMA,
    SUBMIT_BRIEF: BRIEF_SCHEMA,
    SUBMIT_JUDGE: JUDGE_SCHEMA,
    SUBMIT_SUMMARY: SUMMARY_SCHEMA,
    SUBMIT_DREAM: DREAM_SCHEMA,
    SUBMIT_SKILL_EDIT: SKILL_EDIT_SCHEMA,
}

_MIN = {
    SUBMIT_PLAN: MIN_PLAN_SCHEMA,
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
}


def openai_tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def tool_choice_required(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def parse_args(raw: str) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return None, f"invalid json: {e}"
    if not isinstance(data, dict):
        return None, "arguments must be a JSON object"
    return data, ""


def validate_payload(name: str, payload: dict[str, Any], *, degraded: bool = False) -> str:
    schema = (_MIN if degraded else _FULL).get(name)
    if schema is None:
        return f"unknown schema tool: {name}"
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as e:
        return e.message
    if name == SUBMIT_JUDGE:
        evidence = str(payload.get("evidence") or "")
        if "no_hard_criteria" in evidence.lower() and len(evidence) < 24:
            return "no_hard_criteria requires an explicit semantic rationale in evidence"
    if name == SUBMIT_BRIEF:
        brief = str(payload.get("brief") or "").strip()
        if brief in {"已完成", "done", "ok", "计划不行"}:
            return "brief is too generic; explain files, tests, and plan assumptions"
    return ""


def coerce_brief(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome": str(payload.get("outcome") or "failed"),
        "brief": str(payload.get("brief") or ""),
        "completed": list(payload.get("completed") or []),
        "changed_files": list(payload.get("changed_files") or []),
        "tests": payload.get("tests")
        or {"passed": 0, "failed": 0, "exit_code": -1, "log": ""},
        "open_questions": list(payload.get("open_questions") or []),
    }


def synthetic_brief(*, outcome: str, brief: str, changed_files: list[str] | None = None) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "brief": brief,
        "completed": [],
        "changed_files": changed_files or [],
        "tests": {"passed": 0, "failed": 0, "exit_code": -1, "log": ""},
        "open_questions": [],
    }


VALIDATION_TOOL_RESULT = (
    "[ERROR/Validation] arguments failed schema check: {err}. "
    "Resubmit this tool with valid JSON matching the schema."
)


async def oneshot_schema(
    llm,
    *,
    model: str,
    system: str,
    user: str,
    name: str,
    schema: dict[str, Any],
    description: str,
) -> dict[str, Any]:
    from runtime.llm import LLMGateway

    assert isinstance(llm, LLMGateway)
    result = await llm.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=[openai_tool(name, description, schema)],
        tool_choice=tool_choice_required(name),
    )
    if not result.tool_calls:
        raise ValueError("model did not call the required function")
    payload, err = parse_args(result.tool_calls[0]["arguments"])
    if payload is None:
        raise ValueError(err)
    msg = validate_payload(name, payload)
    if msg:
        raise ValueError(msg)
    return payload
