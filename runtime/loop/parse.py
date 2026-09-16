"""submit_* 参数解析、schema 校验、oneshot 交卷。"""

import json
from typing import Any

import jsonschema

from runtime.lab.spec import MAX_ASSIGNMENTS
from runtime.loop.schema import (
    BRIEF_MAX,
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    _FULL,
    _MIN,
)

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
    return _coerce_nested_json(data), ""


def _coerce_nested_json(data: dict[str, Any]) -> dict[str, Any]:
    """部分模型会把 assignments 等数组字段再 JSON 编码成字符串。能解成 list/dict 就解开。"""
    out = dict(data)
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        stripped = value.lstrip()
        if not stripped[:1] in "[{":
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (list, dict)):
            out[key] = parsed
    return out


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
        if brief in {"已完成", "done", "ok", "做不了"}:
            return "brief is too generic; say what you changed and any errors you hit"
    if name == SUBMIT_DISPATCH and not degraded:
        return _validate_dispatch(payload)
    return ""


def _validate_dispatch(payload: dict[str, Any]) -> str:
    """派发自洽性：id 唯一、领域不重叠、可测的必须给接口契约。"""
    assignments = payload.get("assignments") or []
    if len(assignments) > MAX_ASSIGNMENTS:
        return f"at most {MAX_ASSIGNMENTS} assignments per dispatch; split into more steps"

    ids = [str(a.get("id") or "") for a in assignments]
    if len(set(ids)) != len(ids):
        return "assignment id must be unique within a dispatch"

    domains = [str(a.get("domain") or "").strip().lower() for a in assignments]
    if len(assignments) > 1 and len(set(domains)) != len(domains):
        return (
            "parallel assignments must own disjoint domains; "
            "give each worker a distinct domain or dispatch them in separate steps"
        )

    for assignment in assignments:
        aid = str(assignment.get("id") or "")
        if assignment.get("testable") and not (
            assignment.get("interfaces") or assignment.get("acceptance_files")
        ):
            return (
                f"assignment {aid!r} is testable but declares no interfaces; "
                "acceptance code needs exact names to import and call"
            )
    return ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] or text[:limit]


def coerce_brief(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome": str(payload.get("outcome") or "failed"),
        "brief": _clip(str(payload.get("brief") or ""), BRIEF_MAX),
        "changed_files": list(payload.get("changed_files") or []),
    }


def synthetic_brief(*, outcome: str, brief: str, changed_files: list[str] | None = None) -> dict[str, Any]:
    return coerce_brief({"outcome": outcome, "brief": brief, "changed_files": changed_files or []})


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
