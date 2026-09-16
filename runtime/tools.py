"""Tool registry + pipeline：Schema → Validation → Permission → Execution → Normalization。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.observe.tracer import Tracer
from runtime.observe import spans as S
from runtime.schema_call import SCHEMA_TOOLS, openai_tool
from tools.policy import get_auditor, get_policy

Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: frozenset[str]
    handler: Handler


@dataclass
class ToolContext:
    role: str
    settings: RuntimeSettings
    session_dir: Any
    llm: Any
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    text: str
    error_class: ErrorClass = ErrorClass.OK


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def for_role(self, role: str, extra_names: set[str] | None = None) -> "ToolRegistry":
        extra = extra_names or set()
        specs = [
            s
            for s in self._specs.values()
            if role in s.permissions or s.name in extra
        ]
        return ToolRegistry(specs)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            openai_tool(s.name, s.description, s.input_schema) for s in self._specs.values()
        ]

    def names(self) -> set[str]:
        return set(self._specs)

    async def execute(
        self,
        name: str,
        raw_args: str | dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float,
        tracer: Tracer | None = None,
    ) -> ToolOutcome:
        spec = self._specs.get(name)
        if spec is None:
            return ToolOutcome(f"[ERROR/Validation] unknown tool: {name}", ErrorClass.VALIDATION)
        if ctx.role not in spec.permissions and name not in SCHEMA_TOOLS:
            get_auditor().record(name, {}, f"denied:role={ctx.role}")
            if tracer:
                with tracer.span(S.GUARDRAIL, **{S.ATTR_TOOL: name, "allow": False}):
                    pass
            return ToolOutcome(
                f"[ERROR/PermissionError] tool {name} is not allowed for role={ctx.role}",
                ErrorClass.PERMISSION,
            )
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as e:
                return ToolOutcome(f"[ERROR/Validation] invalid json: {e}", ErrorClass.VALIDATION)
        else:
            args = raw_args
        if not isinstance(args, dict):
            return ToolOutcome("[ERROR/Validation] arguments must be an object", ErrorClass.VALIDATION)
        try:
            jsonschema.validate(args, spec.input_schema)
        except jsonschema.ValidationError as e:
            return ToolOutcome(f"[ERROR/Validation] {e.message}", ErrorClass.VALIDATION)

        async def _run() -> str:
            return await spec.handler(args, ctx)

        ctx.extras["submit_name"] = name
        try:
            text = await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError:
            get_auditor().record(name, args, "error:timeout")
            return ToolOutcome(f"[TIMEOUT after {timeout}s] tool {name}", ErrorClass.TRANSIENT)
        except PermissionError as e:
            get_auditor().record(name, args, f"denied:{e}")
            return ToolOutcome(f"[ERROR/PermissionError] {e}", ErrorClass.PERMISSION)
        except Exception as e:
            get_auditor().record(name, args, f"error:{type(e).__name__}")
            cls = classify(e)
            return ToolOutcome(f"[ERROR/{type(e).__name__}] {e}", cls)

        if "[SANDBOX_UNREACHABLE]" in text:
            return ToolOutcome(text, ErrorClass.FATAL)
        if text.startswith("[ERROR/PermissionError]"):
            return ToolOutcome(text, ErrorClass.PERMISSION)
        if text.startswith("[ERROR/Validation]"):
            return ToolOutcome(text, ErrorClass.VALIDATION)
        if text.startswith("[TIMEOUT"):
            return ToolOutcome(text, ErrorClass.TRANSIENT)
        if text.startswith("[ERROR/"):
            return ToolOutcome(text, classify(None, text=text))
        get_auditor().record(name, args, "ok")
        if len(text) > 12_000:
            text = text[:12_000] + "\n[truncated; grep tool_results/ or the file itself for the rest]"
        return ToolOutcome(text, ErrorClass.OK)


def build_base_specs() -> list[ToolSpec]:
    from runtime.accept import write_acceptance_file
    from runtime.schema_call import (
        ACCEPT_FILE_SCHEMA,
        BRIEF_SCHEMA,
        JUDGE_SCHEMA,
        PLAN_SCHEMA,
        SUMMARY_SCHEMA,
        SUBMIT_BRIEF,
        SUBMIT_JUDGE,
        SUBMIT_PLAN,
        SUBMIT_SUMMARY,
        WRITE_ACCEPTANCE,
    )

    async def h_read(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import read_file

        return read_file(args["path"])

    async def h_write(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import write_file

        return write_file(args["path"], args["content"], role=ctx.role)

    async def h_list(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import list_dir

        r = list_dir(args.get("path") or ".")
        return r if isinstance(r, str) else "\n".join(r)

    async def h_patch(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import patch_file

        return patch_file(args["path"], args["old"], args["new"], role=ctx.role)

    async def h_glob(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import glob_files

        return "\n".join(glob_files(args["pattern"], int(args.get("limit") or 80)))

    async def h_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import grep_files

        return grep_files(args["pattern"], args.get("glob") or "**/*", int(args.get("limit") or 40))

    async def h_bash(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.fs_tools import host_bash

        return host_bash(args["cmd"], int(args.get("timeout") or 30))

    async def h_search(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.search_tool import web_search

        hits = await web_search(args["query"], int(args.get("max_results") or 5))
        if not hits:
            return "(no matches)"
        return json.dumps(hits, ensure_ascii=False)

    async def h_skill_ref(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.skill_tool import load_skill_reference

        return load_skill_reference(args["skill_name"], args["ref_name"])

    async def h_skill_script(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.skill_tool import use_skill_script

        return use_skill_script(args["skill_name"], args["script_name"])

    async def h_mem_search(args: dict[str, Any], ctx: ToolContext) -> str:
        from memory.retrieve import memory_search

        return await memory_search(args["query"], int(args.get("k") or 5), ctx.llm, ctx.settings)

    async def h_mem_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        from memory.retrieve import memory_grep

        return memory_grep(args["pattern"], ctx.settings)

    async def h_mem_read(args: dict[str, Any], ctx: ToolContext) -> str:
        from memory.retrieve import memory_read

        return memory_read(args["path"], ctx.settings)

    async def h_profile(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.profile_tool import read_profile

        return json.dumps(read_profile(), ensure_ascii=False)

    async def h_accept(args: dict[str, Any], ctx: ToolContext) -> str:
        path = write_acceptance_file(
            ctx.session_dir, args["task_id"], args["filename"], args["content"]
        )
        return f"wrote acceptance file {path.name} for task {args['task_id']}"

    async def h_submit(args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.extras["submit"] = {"name": ctx.extras.get("submit_name"), "payload": args}
        return "submitted"

    readonly = frozenset({"readonly", "write", "pro"})
    write = frozenset({"write", "pro"})
    pro = frozenset({"pro"})

    specs = [
        ToolSpec(
            "read_file",
            "Read a workspace text file. path is relative to workspace.",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            readonly,
            h_read,
        ),
        ToolSpec(
            "list_dir",
            "List a workspace directory (non-recursive).",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            readonly,
            h_list,
        ),
        ToolSpec(
            "glob_files",
            "Glob workspace files. Always supply a tight pattern; results are capped.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
            readonly,
            h_glob,
        ),
        ToolSpec(
            "grep_files",
            "Regex search workspace files. Empty result is an observation, not a failure.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
            readonly,
            h_grep,
        ),
        ToolSpec(
            "web_search",
            "DuckDuckGo search. Empty hits are an observation.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
            readonly,
            h_search,
        ),
        ToolSpec(
            "memory_search",
            "Vector-search archived knowledge cards.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer"},
                },
                "required": ["query"],
            },
            readonly,
            h_mem_search,
        ),
        ToolSpec(
            "memory_grep",
            "Regex search card markdown, archived sessions, and tool_results.",
            {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
            readonly,
            h_mem_grep,
        ),
        ToolSpec(
            "memory_read",
            "Read one card file or session note by path.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            readonly,
            h_mem_read,
        ),
        ToolSpec(
            "load_skill_reference",
            "Read skills/<name>/references/<ref_name> on demand.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "ref_name": {"type": "string"},
                },
                "required": ["skill_name", "ref_name"],
            },
            readonly,
            h_skill_ref,
        ),
        ToolSpec(
            "read_profile",
            "Read profile/me.yaml.",
            {"type": "object", "properties": {}},
            readonly,
            h_profile,
        ),
        ToolSpec(
            "write_file",
            "Overwrite a workspace text file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            write,
            h_write,
        ),
        ToolSpec(
            "patch_file",
            "Exact unique string replacement in a workspace file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
            write,
            h_patch,
        ),
        ToolSpec(
            "host_bash",
            "Run a whitelisted bash command in the host workspace.",
            {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["cmd"],
            },
            write,
            h_bash,
        ),
        ToolSpec(
            "use_skill_script",
            "Copy a skill script into workspace/.labhandler/scripts for sandbox execution.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "script_name": {"type": "string"},
                },
                "required": ["skill_name", "script_name"],
            },
            write,
            h_skill_script,
        ),
        ToolSpec(WRITE_ACCEPTANCE, "Write one acceptance test file (Pro only).", ACCEPT_FILE_SCHEMA, pro, h_accept),
        ToolSpec(
            SUBMIT_PLAN,
            "Submit the DAG plan. Nodes that must write files MUST be singleton waves via depends_on.",
            PLAN_SCHEMA,
            pro,
            h_submit,
        ),
        ToolSpec(SUBMIT_JUDGE, "Submit the judge decision.", JUDGE_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_SUMMARY, "Submit SUMMARY.md plus knowledge cards.", SUMMARY_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_BRIEF, "Submit the worker brief. This is the only Flash→Pro exit.", BRIEF_SCHEMA, write | readonly | pro, h_submit),
    ]

    # sandbox execute is write; convert_to_markdown is readable
    async def h_convert(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.sandbox_tools import sandbox_convert_to_markdown

        return await sandbox_convert_to_markdown(args["file_path"])

    specs.append(
        ToolSpec(
            "sandbox_convert_to_markdown",
            "Parse PDF/DOCX/PPT inside the sandbox into markdown.",
            {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
            readonly,
            h_convert,
        )
    )

    async def h_sb_bash(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.sandbox_tools import call_sandbox

        return await call_sandbox("sandbox_execute_bash", **args)

    async def h_sb_code(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.sandbox_tools import call_sandbox

        return await call_sandbox("sandbox_execute_code", **args)

    async def h_sb_file(args: dict[str, Any], ctx: ToolContext) -> str:
        from tools.sandbox_tools import call_sandbox

        return await call_sandbox("sandbox_file_operations", **args)

    specs.extend(
        [
            ToolSpec(
                "sandbox_execute_bash",
                "Run a bash command inside the sandbox (/workspace).",
                {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                write,
                h_sb_bash,
            ),
            ToolSpec(
                "sandbox_execute_code",
                "Run code inside the sandbox Jupyter kernel. Prefer bash for sync results.",
                {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
                write,
                h_sb_code,
            ),
            ToolSpec(
                "sandbox_file_operations",
                "Sandbox file operations (path translated to /workspace).",
                {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": True},
                write,
                h_sb_file,
            ),
        ]
    )
    return specs


def build_registry() -> ToolRegistry:
    return ToolRegistry(build_base_specs())
