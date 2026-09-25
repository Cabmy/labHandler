"""参数解析、类型提示、schema 校验、oneshot 交卷。"""

import json
import os
import re
from typing import Any

import jsonschema

from runtime.lab.spec import MAX_ASSIGNMENTS
from runtime.loop.schema import (
    BRIEF_MAX,
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    _FULL,
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
    return data, ""


def json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


# 正文类 string：模型常把换行拆成 array。标识类 string：必须是单个 token。
_ID_STRING_FIELDS = {
    "task_id": "two_sum",
    "id": "two_sum",
    "filename": "test_two_sum.py",
    "path": "two_sum.py",
    "domain": "coding",
    "decision": "continue",
    "outcome": "done",
    "name": "two_sum",
    "kind": "function",
    "type": "lesson",
    "action": "write",
    "file": "SKILL.md",
}


def type_hint(path: str, expected: Any, raw: Any) -> str:
    """类型对不上时给一句可执行的改法，不改入参。"""
    want = expected[0] if isinstance(expected, list) and len(expected) == 1 else expected
    leaf = path.rsplit(".", 1)[-1]
    if want == "string" and isinstance(raw, list):
        example = _ID_STRING_FIELDS.get(leaf)
        if example is not None:
            return f'{path} must be one string like {example!r}, not an array'
        return (
            f'{path} must be one string, not an array; join lines with \\n '
            f'(example: "{leaf}": "line1\\nline2")'
        )
    if want == "string" and isinstance(raw, dict):
        example = _ID_STRING_FIELDS.get(leaf)
        if example is not None:
            return f'{path} must be one string like {example!r}, not an object'
        keys = ", ".join(str(k) for k in list(raw.keys())[:8])
        extra = f" flatten fields ({keys}) into a paragraph;" if keys else ""
        return (
            f'{path} must be one string, not an object;{extra} '
            f'example: "{leaf}": "encode the 3 samples as pytest via write_acceptance"'
        )
    if want == "array" and isinstance(raw, str):
        return f'{path} must be a JSON array, not a string; pass [...] not a quoted "[...]"'
    if want == "object" and isinstance(raw, str):
        return f'{path} must be a JSON object, not a string; pass {{...}} not a quoted "{{...}}"'
    return f'{path} must be {want}, got {json_type_name(raw)}'


def schema_error_hint(err: jsonschema.ValidationError) -> str:
    path = ".".join(str(p) for p in err.absolute_path) or "payload"
    if err.validator == "type":
        return type_hint(path, err.validator_value, err.instance)
    extra = {
        "enum": f"{path} must be one of {err.validator_value}",
        "minLength": f"{path} is too short (min {err.validator_value})",
        "minItems": f"{path} needs at least {err.validator_value} item(s)",
    }
    return extra.get(err.validator) or err.message or "schema validation failed"


def check_args(schema: dict[str, Any], args: dict[str, Any]) -> str:
    """普通工具：required + 类型提示，不拦截额外字段。integer 字符串仍可转成 int。"""
    miss = [
        k
        for k in (schema.get("required") or [])
        if (v := args.get(k)) is None or (isinstance(v, str) and not v.strip())
    ]
    if miss:
        return f"[ERROR/Validation] missing {', '.join(miss)}"
    for key, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        raw = args.get(key)
        if raw in (None, ""):
            continue
        t = spec.get("type")
        if t == "integer":
            try:
                args[key] = int(raw)
            except (TypeError, ValueError):
                return f"[ERROR/Validation] {type_hint(key, 'integer', raw)}"
        elif t == "string" and not isinstance(raw, str):
            return f"[ERROR/Validation] {type_hint(key, 'string', raw)}"
    return ""


def validate_payload(name: str, payload: dict[str, Any]) -> str:
    schema = _FULL.get(name)
    if schema is None:
        return f"unknown schema tool: {name}"
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as e:
        return schema_error_hint(e)
    if name == SUBMIT_JUDGE:
        evidence = str(payload.get("evidence") or "")
        if "no_hard_criteria" in evidence.lower() and len(evidence) < 24:
            return "no_hard_criteria requires an explicit semantic rationale in evidence"
    if name == SUBMIT_BRIEF:
        brief = str(payload.get("brief") or "").strip()
        if brief in {"已完成", "done", "ok", "做不了"}:
            return "brief is too generic; say what you changed and any errors you hit"
    if name == SUBMIT_SPEC:
        return _validate_spec(payload)
    if name == SUBMIT_DISPATCH:
        return _validate_dispatch(payload)
    if name == SUBMIT_REMEMBER:
        return _validate_remember(payload)
    return ""


# 别的工具的字段名。模型会把 submit_dispatch / write_acceptance 的形状摊平成
# ["step_goal: ...", "goal: ...", "task_id: ..."] 塞进 milestones。
_FOREIGN_FIELD = re.compile(
    r"^\s*(step_goal|goal|spec|task_id|id|domain|testable|filename|content|assignments|"
    r"interfaces|expected_artifacts|acceptance_strategy|acceptance_intent|acceptance_files|"
    r"done_when|constraints|deliverables|overview|verdicts|decision|evidence|applies|rule)"
    r"\s*[:：]",
    re.I,
)


def _validate_spec(payload: dict[str, Any]) -> str:
    """里程碑是句子，不是别的工具的字段。"""
    for raw in payload.get("milestones") or []:
        text = str(raw)
        hit = _FOREIGN_FIELD.match(text)
        if hit:
            return (
                f"milestone {text[:60]!r} starts with {hit.group(1)!r}, which is a field of "
                "another tool, not a milestone. milestones is a list of plain sentences, one per "
                "Flash product step, e.g. \"实现 two_sum.py 中的 two_sum 函数\". One problem is one "
                "milestone. step_goal / goal / spec / task_id belong to submit_dispatch and "
                "write_acceptance in later phases — do not flatten those fields in here."
            )
    return ""


def _validate_remember(payload: dict[str, Any]) -> str:
    """裁定按编号对齐。模型手抄规则文本会抄错，错了就静默丢失那条规则。"""
    seen: set[int] = set()
    for row in payload.get("verdicts") or []:
        if not isinstance(row, dict):
            return "each verdict is an object with index and applies"
        index = row.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            return (
                "each verdict needs index: the integer number printed in front of that rule in "
                "the user message. Do not retype the rule text as the identifier."
            )
        if index in seen:
            return f"rule index {index} judged twice; one verdict per rule"
        seen.add(index)
    return ""


def _validate_dispatch(payload: dict[str, Any]) -> str:
    """派发自洽性：写任务单独成波、测试不交给 Flash、id/领域不重叠、可测要有接口。"""
    assignments = payload.get("assignments") or []
    cap = MAX_ASSIGNMENTS
    raw_cap = os.getenv("EVAL_MAX_ASSIGNMENTS", "").strip()
    if raw_cap.isdigit():
        cap = max(1, int(raw_cap))
    if len(assignments) > cap:
        return f"at most {cap} assignments per dispatch; split into more steps"

    ids = [str(a.get("id") or "") for a in assignments]
    if len(set(ids)) != len(ids):
        return "assignment id must be unique within a dispatch"

    rows = [a for a in assignments if isinstance(a, dict)]
    testable_n = sum(1 for a in rows if a.get("testable"))
    if testable_n and len(assignments) > 1:
        return (
            "a testable assignment must be the only Flash this wave (1 worker = write). "
            "Tests are write_acceptance(task_id=that assignment id), not a second Flash. "
            "Other product files go in later steps."
        )

    flash_tests = _dispatch_asks_flash_for_tests(payload)
    if flash_tests:
        return flash_tests

    gate_job = _dispatch_touches_gate(payload)
    if gate_job:
        return gate_job

    domains = [str(a.get("domain") or "").strip().lower() for a in assignments]
    if len(assignments) > 1 and len(set(domains)) != len(domains):
        return (
            "parallel assignments must own disjoint domains and must all be readonly. "
            "A product write is one assignment this wave; tests are write_acceptance, "
            "not another Flash. If you meant one product, send one assignment."
        )

    for assignment in rows:
        aid = str(assignment.get("id") or "")
        if assignment.get("testable") and not (
            assignment.get("interfaces") or assignment.get("acceptance_files")
        ):
            return (
                f"assignment {aid!r} is testable but declares no interfaces; "
                "acceptance code needs exact names to import and call"
            )
        if _verify_only(assignment):
            return (
                f"assignment {aid!r} only runs tests; the harness gate does that after Flash submits. "
                "If SPEC is done, submit_dispatch with empty assignments. "
                "If a gate is missing, set testable=true and write_acceptance in this dispatch."
            )
    return ""


_VERIFY_ONLY = re.compile(
    r"(运行.{0,8}测试|跑.{0,8}测试|跑一遍|验证通过|run tests|run pytest|verify all|unittest discover)",
    re.I,
)
_FLASH_TEST_JOB = re.compile(
    r"(写(单元)?测试|提供单元测试|编写(单元)?测试|补(充|上)(单元)?测试|"
    r"添加(单元)?测试|自写(单元)?测试|"
    r"write (?:the )?(?:unit )?tests?|add (?:unit )?tests?|provide (?:unit )?tests?)",
    re.I,
)
_ACCEPT_WORKER_ID = re.compile(r"(?:^|[-_])(accept|acceptance|tests?|unittest)$", re.I)
_TEST_PRODUCT = re.compile(r"(?:^|/)test_[^/]+\.py$")
_GATE_JOB = re.compile(
    r"(acceptance test|acceptance file|the gate|pytest gate|门禁|验收(测试|文件|用例)|"
    r"acceptance/)",
    re.I,
)


def _dispatch_touches_gate(payload: dict[str, Any]) -> str:
    """门禁是 Pro 自己的活：不准把「修/改验收测试」写进 step_goal 或 assignment。"""
    rows = [a for a in (payload.get("assignments") or []) if isinstance(a, dict)]
    blob = " ".join(
        [str(payload.get("step_goal") or "")]
        + [f"{a.get('goal')} {a.get('spec')}" for a in rows]
    )
    if not _GATE_JOB.search(blob):
        return ""
    return (
        "The gate belongs to you, not to Flash: no dispatch may mention fixing, updating or "
        "passing the acceptance tests. Call write_acceptance(task_id=<product assignment id>) "
        "yourself, and describe the assignment purely as product work. If the product is done "
        "and only the gate is broken, submit_dispatch with the same product id and no gate talk, "
        "or with empty assignments."
    )


def _named_test_product(assignment: dict[str, Any]) -> bool:
    """作业把 workspace 的 test_*.py 点名为交付物时，Flash 才可以写那个文件。"""
    arts = assignment.get("expected_artifacts") or []
    if not isinstance(arts, list):
        return False
    return any(_TEST_PRODUCT.search(str(p).strip()) for p in arts)


def _dispatch_asks_flash_for_tests(payload: dict[str, Any]) -> str:
    """作业没点名 test 文件时，禁止把写/提供测试派给 Flash。"""
    rows = [a for a in (payload.get("assignments") or []) if isinstance(a, dict)]
    if any(_named_test_product(a) for a in rows):
        return ""
    for assignment in rows:
        aid = str(assignment.get("id") or "")
        if _ACCEPT_WORKER_ID.search(aid):
            return (
                f"assignment {aid!r} looks like a tests worker. "
                "Tests are write_acceptance(task_id=<product assignment id>), not a Flash. "
                "Dispatch one product worker (e.g. two_sum.py)."
            )
    blob = " ".join(
        [str(payload.get("step_goal") or "")]
        + [f"{a.get('id')} {a.get('goal')} {a.get('spec')}" for a in rows]
    )
    if not _FLASH_TEST_JOB.search(blob):
        return ""
    return (
        "Do not dispatch Flash to write or provide unit tests. "
        "Call write_acceptance(task_id=<assignment id>, filename=\"test_foo.py\", "
        "content=<one python string>) then dispatch Flash only for the product file. "
        "step_goal / goal must not say 提供单元测试 unless expected_artifacts names a "
        "test_*.py to hand in."
    )


def _verify_only(assignment: dict[str, Any]) -> bool:
    if assignment.get("testable") or assignment.get("expected_artifacts") or assignment.get("interfaces"):
        return False
    blob = f"{assignment.get('goal') or ''} {assignment.get('spec') or ''}"
    return bool(_VERIFY_ONLY.search(blob))


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
