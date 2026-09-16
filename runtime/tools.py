"""Tool registry：可见工具表、执行、以及归一化后的 ToolOutcome。

for_role 产出的收窄 registry 同时是可见性边界和执行边界：不在表里的名字调不动，
模型从 history 里翻出别处的工具名照抄也会被挡下。收窄依据由 runtime.phase 给，
本模块不认识阶段。普通工具的 schema 只用于广告字段和缺参提示，额外字段放行；
submit_* 的结构化约束在 loop 里由 validate_payload 执行。error_class 从正文前缀
或异常类型得出。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.schema_call import SCHEMA_TOOLS, openai_tool
from tools.policy import get_auditor

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


def _fields(*required: str, extra: bool = False, **props: str) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {k: {"type": t} for k, t in props.items()},
    }
    if required:
        schema["required"] = list(required)
    if extra:
        schema["additionalProperties"] = True
    return schema


def _advertise(schema: dict[str, Any]) -> dict[str, Any]:
    advertised = {**schema, "additionalProperties": True}
    advertised.pop("required", None)
    return advertised


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_args(schema: dict[str, Any], args: dict[str, Any]) -> str:
    """按 schema.required / integer 属性给出缺参提示，不拦截额外字段。"""
    miss = [k for k in (schema.get("required") or []) if _blank(args.get(k))]
    if miss:
        return f"[ERROR/Validation] missing {', '.join(miss)}"
    for key, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "integer":
            continue
        raw = args.get(key)
        if raw in (None, ""):
            continue
        try:
            args[key] = int(raw)
        except (TypeError, ValueError):
            return f"[ERROR/Validation] {key} must be an integer"
    return ""


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def for_role(
        self, role: str, submit_tool: str, extra: frozenset[str] = frozenset()
    ) -> "ToolRegistry":
        """本拍实际可见的工具：role 允许的，加 extra 点名的，加唯一出口 submit_tool。

        role / extra 由 runtime.phase 决定，本模块不认识阶段。收窄后的这份
        registry 同时用于 execute，所以不在表里的名字调不动。
        """
        specs = [
            s
            for s in self._specs.values()
            if (role in s.permissions or s.name in extra or s.name == submit_tool)
            and (s.name not in SCHEMA_TOOLS or s.name == submit_tool)
        ]
        return ToolRegistry(specs)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            openai_tool(
                s.name,
                s.description,
                s.input_schema if s.name in SCHEMA_TOOLS else _advertise(s.input_schema),
            )
            for s in self._specs.values()
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
        if tracer is None:
            return await self._execute(name, raw_args, ctx, timeout=timeout, tracer=None)
        with tracer.span(
            S.TOOL,
            kind=S.KIND_TOOL,
            inputs=raw_args if isinstance(raw_args, dict) else str(raw_args)[:2000],
            **{S.ATTR_TOOL: name, S.ATTR_PERMISSION: ctx.role},
        ) as span:
            outcome = await self._execute(name, raw_args, ctx, timeout=timeout, tracer=tracer)
            span.set(**{S.ATTR_ERROR_CLASS: outcome.error_class.value})
            span.output(outcome.text[:2000])
            return outcome

    async def _execute(
        self,
        name: str,
        raw_args: str | dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float,
        tracer: Tracer | None,
    ) -> ToolOutcome:
        spec = self._specs.get(name)
        if spec is None:
            # 本阶段工具表里没有。列出可用名字，避免模型照着 history 里的旧工具反复重试。
            return ToolOutcome(
                f"[ERROR/Validation] tool {name} is not available at this stage; "
                f"available: {', '.join(sorted(self._specs))}",
                ErrorClass.VALIDATION,
            )
        if ctx.role not in spec.permissions and name not in SCHEMA_TOOLS:
            get_auditor().record(name, {}, f"denied:role={ctx.role}")
            if tracer is not None:
                tracer.event(S.GUARDRAIL, **{S.ATTR_TOOL: name, S.ATTR_TOOL_ALLOWED: False})
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
        if name not in SCHEMA_TOOLS:
            hint = _check_args(spec.input_schema, args)
            if hint:
                return ToolOutcome(hint, ErrorClass.VALIDATION)

        ctx.extras["submit_name"] = name
        try:
            text = await asyncio.wait_for(spec.handler(args, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            get_auditor().record(name, args, "error:timeout")
            return ToolOutcome(f"[TIMEOUT after {timeout}s] tool {name}", ErrorClass.TRANSIENT)
        except PermissionError as e:
            get_auditor().record(name, args, f"denied:{e}")
            return ToolOutcome(f"[ERROR/PermissionError] {e}", ErrorClass.PERMISSION)
        except Exception as e:
            get_auditor().record(name, args, f"error:{type(e).__name__}")
            return ToolOutcome(f"[ERROR/{type(e).__name__}] {e}", classify(e))

        if "[SANDBOX_UNREACHABLE]" in text:
            return ToolOutcome(text, ErrorClass.FATAL)
        if text.startswith("[ERROR/PermissionError]"):
            return ToolOutcome(text, ErrorClass.PERMISSION)
        if text.startswith("[ERROR/Validation]"):
            return ToolOutcome(text, ErrorClass.VALIDATION)
        if text.startswith("[TIMEOUT"):
            return ToolOutcome(text, ErrorClass.TRANSIENT)
        if text.startswith("[ERROR/"):
            cls = classify(None, text=text)
            return ToolOutcome(text, ErrorClass.LOGIC if cls is ErrorClass.OK else cls)
        get_auditor().record(name, args, "ok")
        from runtime.context.compact import DumpScope, LIVE_OFFLOAD_CHARS, dump_tool_result

        dump = ctx.extras.get("dump")
        if (
            isinstance(dump, DumpScope)
            and len(text) > LIVE_OFFLOAD_CHARS
        ):
            text = dump_tool_result(
                dump,
                tool=name,
                call_id=str(ctx.extras.get("tool_call_id") or ""),
                body=text,
            )
        return ToolOutcome(text, ErrorClass.OK)


def build_base_specs() -> list[ToolSpec]:
    from memory.retrieve import memory_grep, memory_read, memory_search
    from runtime.accept import write_acceptance_file
    from runtime.schema_call import (
        ACCEPT_FILE_SCHEMA,
        BRIEF_SCHEMA,
        DISPATCH_SCHEMA,
        JUDGE_SCHEMA,
        REMEMBER_SCHEMA,
        SPEC_SCHEMA,
        SUMMARY_SCHEMA,
        SUBMIT_BRIEF,
        SUBMIT_DISPATCH,
        SUBMIT_JUDGE,
        SUBMIT_REMEMBER,
        SUBMIT_SPEC,
        SUBMIT_SUMMARY,
        WRITE_ACCEPTANCE,
    )
    from tools.fs_tools import glob_files, grep_files, host_bash, list_dir, patch_file, read_file, write_file
    from tools.profile_tool import read_profile
    from tools.sandbox_tools import call_sandbox, sandbox_convert_to_markdown
    from tools.search_tool import web_search
    from tools.skill_tool import (
        LOAD_SKILL,
        LOAD_SKILL_REFERENCE,
        USE_SKILL_SCRIPT,
        SkillBind,
        load_skill_reference,
        use_skill_script,
    )

    async def h_read(args: dict[str, Any], ctx: ToolContext) -> str:
        limit = args.get("limit")
        return read_file(
            args["path"],
            int(args.get("offset") or 1),
            None if limit in (None, "") else int(limit),
        )

    async def h_write(args: dict[str, Any], ctx: ToolContext) -> str:
        return write_file(args["path"], args["content"], role=ctx.role)

    async def h_list(args: dict[str, Any], ctx: ToolContext) -> str:
        r = list_dir(args.get("path") or ".")
        return r if isinstance(r, str) else "\n".join(r)

    async def h_patch(args: dict[str, Any], ctx: ToolContext) -> str:
        return patch_file(args["path"], args["old"], args["new"], role=ctx.role)

    async def h_glob(args: dict[str, Any], ctx: ToolContext) -> str:
        return "\n".join(glob_files(args["pattern"], int(args.get("limit") or 80)))

    async def h_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        return grep_files(args["pattern"], args.get("glob") or "**/*", int(args.get("limit") or 40))

    async def h_bash(args: dict[str, Any], ctx: ToolContext) -> str:
        return host_bash(args["cmd"], int(args.get("timeout") or 30))

    async def h_search(args: dict[str, Any], ctx: ToolContext) -> str:
        hits = await web_search(args["query"], int(args.get("max_results") or 5))
        return json.dumps(hits, ensure_ascii=False) if hits else "(no matches)"

    def _bind(ctx: ToolContext) -> SkillBind:
        slot = ctx.extras.get("skill")
        if not isinstance(slot, SkillBind):
            slot = SkillBind()
            ctx.extras["skill"] = slot
        return slot

    async def h_load_skill(args: dict[str, Any], ctx: ToolContext) -> str:
        return _bind(ctx).load(str(args.get("skill_name") or ""))

    async def h_skill_ref(args: dict[str, Any], ctx: ToolContext) -> str:
        err = _bind(ctx).gate(str(args.get("skill_name") or ""))
        if err:
            return err
        return load_skill_reference(args["skill_name"], args["ref_name"])

    async def h_skill_script(args: dict[str, Any], ctx: ToolContext) -> str:
        err = _bind(ctx).gate(str(args.get("skill_name") or ""))
        if err:
            return err
        return use_skill_script(args["skill_name"], args["script_name"])

    async def h_mem_search(args: dict[str, Any], ctx: ToolContext) -> str:
        return await memory_search(args["query"], int(args.get("k") or 5), ctx.llm, ctx.settings)

    async def h_mem_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        from runtime.context.compact import DumpScope

        dump = ctx.extras.get("dump")
        extra = (
            dump.grep_roots(ctx.settings.workspace_dir / ".labhandler")
            if isinstance(dump, DumpScope)
            else None
        )
        return memory_grep(args["pattern"], ctx.settings, extra_roots=extra)

    async def h_mem_read(args: dict[str, Any], ctx: ToolContext) -> str:
        return memory_read(args["path"], ctx.settings)

    async def h_profile(args: dict[str, Any], ctx: ToolContext) -> str:
        return json.dumps(read_profile(), ensure_ascii=False)

    async def h_accept(args: dict[str, Any], ctx: ToolContext) -> str:
        path = write_acceptance_file(
            ctx.session_dir, args["task_id"], args["filename"], args["content"], role=ctx.role
        )
        return f"wrote acceptance file {path.name} for task {args['task_id']}"

    async def h_submit(args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.extras["submit"] = {"name": ctx.extras.get("submit_name"), "payload": args}
        return "submitted"

    async def h_convert(args: dict[str, Any], ctx: ToolContext) -> str:
        return await sandbox_convert_to_markdown(args["file_path"])

    def h_sandbox(tool: str) -> Handler:
        async def run(args: dict[str, Any], ctx: ToolContext) -> str:
            return await call_sandbox(tool, **args)

        return run

    readonly = frozenset({"readonly", "write", "pro"})
    write = frozenset({"write", "pro"})
    pro = frozenset({"pro"})

    specs = [
        ToolSpec(
            "read_file",
            "Read a workspace text file. path is relative to workspace, including offloaded tool_results. "
            "offset is a 1-based line number; limit is max lines. If truncated, call again with next offset.",
            _fields("path", path="string", offset="integer", limit="integer"),
            readonly,
            h_read,
        ),
        ToolSpec("list_dir", "List a workspace directory (non-recursive).", _fields(path="string"), readonly, h_list),
        ToolSpec(
            "glob_files",
            "Glob workspace files. Always supply a tight pattern; results are capped.",
            _fields("pattern", pattern="string", limit="integer"),
            readonly,
            h_glob,
        ),
        ToolSpec(
            "grep_files",
            "Regex search workspace files. Empty result is an observation, not a failure.",
            _fields("pattern", pattern="string", glob="string", limit="integer"),
            readonly,
            h_grep,
        ),
        ToolSpec(
            "web_search",
            "DuckDuckGo search. Empty hits are an observation.",
            _fields("query", query="string", max_results="integer"),
            readonly,
            h_search,
        ),
        ToolSpec(
            "memory_search",
            "Vector-search archived knowledge cards.",
            _fields("query", query="string", k="integer"),
            readonly,
            h_mem_search,
        ),
        ToolSpec(
            "memory_grep",
            "Regex search knowledge cards and allowed dumps (Flash: this assignment only; Pro: whole .labhandler).",
            _fields("pattern", pattern="string"),
            readonly,
            h_mem_grep,
        ),
        ToolSpec(
            "memory_read",
            "Read one card, session note, or tool_results dump.",
            _fields("path", path="string"),
            readonly,
            h_mem_read,
        ),
        ToolSpec(
            LOAD_SKILL,
            "Pull one skill SOP this lab. Skills are mutually exclusive; skip if none fits.",
            _fields("skill_name", skill_name="string"),
            pro,
            h_load_skill,
        ),
        ToolSpec(
            LOAD_SKILL_REFERENCE,
            "Read skills/<name>/references/<ref_name> for the skill already bound by load_skill.",
            _fields("skill_name", "ref_name", skill_name="string", ref_name="string"),
            pro,
            h_skill_ref,
        ),
        ToolSpec("read_profile", "Read profile/me.yaml.", _fields(), readonly, h_profile),
        ToolSpec(
            "write_file",
            "Overwrite a workspace text file.",
            _fields("path", "content", path="string", content="string"),
            write,
            h_write,
        ),
        ToolSpec(
            "patch_file",
            "Exact unique string replacement in a workspace file.",
            _fields("path", "old", "new", path="string", old="string", new="string"),
            write,
            h_patch,
        ),
        ToolSpec(
            "host_bash",
            "Run a whitelisted bash command in the host workspace.",
            _fields("cmd", cmd="string", timeout="integer"),
            write,
            h_bash,
        ),
        ToolSpec(
            USE_SKILL_SCRIPT,
            "Copy a script of the bound skill into workspace/.labhandler/scripts for sandbox execution.",
            _fields("skill_name", "script_name", skill_name="string", script_name="string"),
            pro,
            h_skill_script,
        ),
        ToolSpec(
            "sandbox_convert_to_markdown",
            "Parse PDF/DOCX/PPT inside the sandbox into markdown.",
            _fields("file_path", file_path="string"),
            readonly,
            h_convert,
        ),
        ToolSpec(
            "sandbox_execute_bash",
            "Run a bash command inside the sandbox (/workspace).",
            _fields("command", command="string"),
            write,
            h_sandbox("sandbox_execute_bash"),
        ),
        ToolSpec(
            "sandbox_execute_code",
            "Run code inside the sandbox Jupyter kernel. Prefer bash for sync results.",
            _fields("code", code="string"),
            write,
            h_sandbox("sandbox_execute_code"),
        ),
        ToolSpec(
            "sandbox_file_operations",
            "Sandbox file operations (path translated to /workspace).",
            _fields(path="string", extra=True),
            write,
            h_sandbox("sandbox_file_operations"),
        ),
        ToolSpec(WRITE_ACCEPTANCE, "Write one acceptance test file (Pro only).", ACCEPT_FILE_SCHEMA, pro, h_accept),
        ToolSpec(SUBMIT_SPEC, "Submit SPEC.md: the top-down specification governing the whole task.", SPEC_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_DISPATCH, "Submit the assignments for the next single step. Empty assignments means the work is done.", DISPATCH_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_JUDGE, "Submit the judge decision.", JUDGE_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_REMEMBER, "Submit which /remember rules apply to this lab.", REMEMBER_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_SUMMARY, "Submit SUMMARY.md plus knowledge cards.", SUMMARY_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_BRIEF, "Submit the worker brief. This is the only Flash→Pro exit.", BRIEF_SCHEMA, write | readonly | pro, h_submit),
    ]
    return specs


def build_registry() -> ToolRegistry:
    return ToolRegistry(build_base_specs())
