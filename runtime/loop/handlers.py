"""工具表广告与 handler。可见性边界在 ToolRegistry.for_role。"""

import asyncio
import json
from typing import Any

from runtime.loop.registry import Handler, ToolContext, ToolRegistry, ToolSpec, _fields


def build_base_specs() -> list[ToolSpec]:
    from memory.retrieve import memory_grep, memory_read, memory_search
    from runtime.context.notes import forget_card, read_long_note
    from runtime.lab.accept import write_acceptance_file
    from runtime.loop.schema import (
        ACCEPT_FILE_SCHEMA,
        BRIEF_SCHEMA,
        DISPATCH_SCHEMA,
        HALT_SCHEMA,
        JUDGE_SCHEMA,
        REMEMBER_SCHEMA,
        SPEC_SCHEMA,
        SUMMARY_SCHEMA,
        SUBMIT_BRIEF,
        SUBMIT_DISPATCH,
        SUBMIT_HALT,
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
        return await asyncio.to_thread(
            read_file,
            args["path"],
            int(args.get("offset") or 1),
            None if limit in (None, "") else int(limit),
            ctx.role,
        )

    async def h_write(args: dict[str, Any], ctx: ToolContext) -> str:
        return await asyncio.to_thread(write_file, args["path"], args["content"], ctx.role)

    async def h_list(args: dict[str, Any], ctx: ToolContext) -> str:
        r = await asyncio.to_thread(list_dir, args.get("path") or ".", ctx.role)
        return r if isinstance(r, str) else "\n".join(r)

    async def h_patch(args: dict[str, Any], ctx: ToolContext) -> str:
        return await asyncio.to_thread(patch_file, args["path"], args["old"], args["new"], ctx.role)

    async def h_glob(args: dict[str, Any], ctx: ToolContext) -> str:
        return "\n".join(await asyncio.to_thread(glob_files, args["pattern"], int(args.get("limit") or 80)))

    async def h_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        return await asyncio.to_thread(
            grep_files, args["pattern"], args.get("glob") or "**/*", int(args.get("limit") or 40)
        )

    async def h_bash(args: dict[str, Any], ctx: ToolContext) -> str:
        return await asyncio.to_thread(host_bash, args["cmd"], int(args.get("timeout") or 30))

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
        return await asyncio.to_thread(
            lambda: memory_grep(args["pattern"], ctx.settings, extra_roots=extra)
        )

    async def h_mem_read(args: dict[str, Any], ctx: ToolContext) -> str:
        return await asyncio.to_thread(memory_read, args["path"], ctx.settings)

    async def h_notes_read(args: dict[str, Any], ctx: ToolContext) -> str:
        return await asyncio.to_thread(read_long_note, ctx.session_dir, args["path"])

    async def h_mem_forget(args: dict[str, Any], ctx: ToolContext) -> str:
        from memory.retrieve import retire_cards_matching

        card = str(args.get("card") or "").strip()
        if not card:
            return "[ERROR/Validation] need a card filename or a topic substring"
        n = await asyncio.to_thread(forget_card, ctx.session_dir, card)
        retired = await asyncio.to_thread(retire_cards_matching, card, ctx.settings)
        msg = f"dropped {n} card(s) from the next turn; recorded in FORGET.md"
        if retired:
            msg += f"; retired {retired} from the archive"
        return msg

    async def h_profile(args: dict[str, Any], ctx: ToolContext) -> str:
        return json.dumps(await asyncio.to_thread(read_profile), ensure_ascii=False)

    async def h_accept(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            path = write_acceptance_file(
                ctx.session_dir, args["task_id"], args["filename"], args["content"], role=ctx.role
            )
        except FileExistsError as e:
            return f"[ERROR/Validation] {e}"
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
            "Read a workspace text file. path is relative to the workspace root (`foo.py`, never `workspace/foo.py`). "
            "offset is a 1-based line number; limit is max lines. If truncated, call again with next offset.",
            _fields("path", path="string", offset="integer", limit="integer"),
            readonly,
            h_read,
        ),
        ToolSpec(
            "list_dir",
            "List a workspace directory (non-recursive). path is relative to the workspace root; `.` is that root.",
            _fields(path="string"),
            readonly,
            h_list,
        ),
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
            "Vector-search archived cards; returns filename+type pointers, not bodies. memory_read to open.",
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
            "Read one archived card (4.md) or a tool_results dump.",
            _fields("path", path="string"),
            readonly,
            h_mem_read,
        ),
        ToolSpec(
            "notes_read",
            "Read a long note written by notes_write (filename in NOTES.md, e.g. ttl.md).",
            _fields("path", path="string"),
            pro,
            h_notes_read,
        ),
        ToolSpec(
            "memory_forget",
            "Retire a card that is false, outdated, or contradicted by this homework: gone from "
            "the next turn, dropped from the archive so later labs will not retrieve it. "
            "card = its filename or a unique substring of its body. Do not use this merely "
            "because a still-true card is off-topic for this assignment.",
            _fields("card", card="string"),
            pro,
            h_mem_forget,
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
            "Overwrite a text file relative to the workspace root (`foo.py`, never `workspace/foo.py`).",
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
            "Run a bash command inside the sandbox. cwd is already `/workspace`. Param is `cmd`, same as host_bash.",
            _fields("cmd", cmd="string"),
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
        ToolSpec(
            WRITE_ACCEPTANCE,
            "Write one pytest gate file under acceptance/<task_id>/ (Pro only). "
            "All three args are required strings, never arrays: "
            'task_id is the assignment id (e.g. "two_sum"); '
            'filename is only "test_foo.py"; '
            r'content is the whole file with lines joined by \n '
            r'(e.g. "import pytest\nfrom two_sum import two_sum\n\ndef test_ex():\n    assert two_sum([2,7],9)==[0,1]\n").',
            ACCEPT_FILE_SCHEMA,
            pro,
            h_accept,
        ),
        ToolSpec(SUBMIT_SPEC, "Submit SPEC.md: the top-down specification governing the whole task.", SPEC_SCHEMA, pro, h_submit),
        ToolSpec(
            SUBMIT_DISPATCH,
            "Submit the next Flash wave. One testable write assignment per wave. "
            "Tests are write_acceptance, not a Flash. Empty assignments = Flash work is done.",
            DISPATCH_SCHEMA,
            pro,
            h_submit,
        ),
        ToolSpec(SUBMIT_JUDGE, "Submit the judge decision.", JUDGE_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_REMEMBER, "Submit which /remember rules apply to this lab.", REMEMBER_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_SUMMARY, "Submit SUMMARY.md plus knowledge cards.", SUMMARY_SCHEMA, pro, h_submit),
        ToolSpec(
            SUBMIT_HALT,
            "Last resort: halt the lab because a required fact is missing and only the user can "
            "supply it. Skips remaining work and jumps to SUMMARY. Do not use for missing product "
            "files, inapplicable /remember, or a homework you can complete with a reasonable default.",
            HALT_SCHEMA,
            pro | write | readonly,
            h_submit,
        ),
        ToolSpec(SUBMIT_BRIEF, "Submit the worker brief. This is the only Flash→Pro exit.", BRIEF_SCHEMA, write | readonly | pro, h_submit),
    ]
    return specs


def build_registry() -> ToolRegistry:
    return ToolRegistry(build_base_specs())
