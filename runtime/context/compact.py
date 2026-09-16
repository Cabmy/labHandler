"""上下文压缩：最近若干轮保留原文，更早的由 Flash 收成一段纪要。

切分单位是回合：带 tool_calls 的 assistant 与其全部 tool 响应同属一个不可再分
的单位。拆开会产出协议非法的孤儿 tool 消息。

卸盘按 DumpScope（agent × task_id）分目录，Pro 与各 Flash 互不混放。
FORGET.md 只在 Pro 压缩时作为排除指令并清空；Flash 压缩不碰它。
MEMORY.md 不在 history 里、不经摘要——assemble 每轮从文件重新注入。
是否该压、阈值、usage 校准归 TokenBudget；本模块只切回合、落盘 tool 正文、写纪要。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from runtime.context.budget import TokenBudget, count_messages
from runtime.context.notes import SessionNotes, clear_forget, forget_directive
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer

DUMP_DIR = "tool_results"
OFFLOAD_MARKER = "[offloaded "
PREVIEW_CHARS = 2000
LIVE_OFFLOAD_CHARS = 12_000
_SUMMARY_MAX_TOKENS = 900
_BLOB_CHAR_CAP = 24000
_INLINE_TOOL_CAP = 600

_SUMMARY_SYSTEM = """你在压缩一个 agent 的历史对话，供它后续回合继续使用。

保留：任务约束、已确认的结论、文件名与路径、接口签名、测试结果、
失败原因、仍未解决的问题。
丢弃：寒暄、重复的试探、已被推翻的中间猜想。
不要编造任何未在原文出现的事实。工具的完整输出已另存到磁盘，
需要时可以 grep {dump_hint}。

输出一段紧凑的中文纪要，不要分点堆砌套话。"""


@dataclass
class CompactResult:
    history: list[dict[str, Any]]
    compacted: bool = False
    tokens_before: int = 0
    tokens_after: int = 0
    turns_summarized: int = 0
    turns_kept: int = 0
    forget_cleared: int = 0
    error: str | None = None


@dataclass
class Turn:
    """一个不可再分的对话回合。带 tool_calls 的 assistant 与其全部 tool 响应同属一个 Turn；pinned 的不参与摘要。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    pinned: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.messages


def split_turns(history: list[dict[str, Any]]) -> list[Turn]:
    """把扁平 history 切成回合。能配对的 tool 并入当前 assistant；无法配对的单独成回合。"""
    turns: list[Turn] = []
    current: Turn | None = None

    for msg in history:
        role = msg.get("role")
        if role == "tool" and current is not None and current.messages:
            head = current.messages[0]
            if head.get("role") == "assistant" and head.get("tool_calls"):
                current.messages.append(msg)
                continue
        if current is not None:
            turns.append(current)
        current = Turn(messages=[msg], pinned=bool(msg.get("pinned")))

    if current is not None:
        turns.append(current)
    return [t for t in turns if not t.is_empty]


def _tool_name_map(turn: Turn) -> dict[str, str]:
    head = turn.messages[0]
    out: dict[str, str] = {}
    for tc in head.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict):
            out[str(tc.get("id") or "")] = str(fn.get("name") or "tool")
    return out


def _safe_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:60] or "tool"


@dataclass(frozen=True)
class DumpScope:
    """一次 loop 的卸盘槽。路径：tool_results/<agent>/<task_id>/。"""

    session_dir: Path
    agent: str
    task_id: str

    @property
    def dump_dir(self) -> Path:
        return self.session_dir / DUMP_DIR / _safe_name(self.agent) / _safe_name(self.task_id)

    @property
    def rel_dir(self) -> str:
        return (
            Path(".labhandler")
            / "sessions"
            / self.session_dir.name
            / DUMP_DIR
            / _safe_name(self.agent)
            / _safe_name(self.task_id)
        ).as_posix()

    def grep_roots(self, labhandler: Path) -> list[Path]:
        """Pro 可扫整个 .labhandler；Flash 只扫自己的卸盘目录。"""
        if self.agent == "pro":
            return [labhandler]
        return [self.dump_dir]


def is_offloaded(content: str) -> bool:
    return content.startswith(OFFLOAD_MARKER)


def dump_rel_path(scope: DumpScope, filename: str) -> str:
    return f"{scope.rel_dir}/{filename}" if filename else scope.rel_dir


def format_stub(rel_path: str, body: str, preview_chars: int = PREVIEW_CHARS) -> str:
    preview = body[:preview_chars]
    lines = [
        f"{OFFLOAD_MARKER}{len(body)} chars → {rel_path}]",
        "Full result: read_file or memory_read that path; search with memory_grep.",
        preview,
    ]
    if len(body) > preview_chars:
        lines.append("…")
    return "\n".join(lines)


def _next_dump_seq(dump_dir: Path) -> int:
    seq = 0
    if not dump_dir.is_dir():
        return seq
    for p in dump_dir.iterdir():
        m = re.match(r"^(\d{4})-", p.name)
        if m:
            seq = max(seq, int(m.group(1)) + 1)
    return seq


def write_tool_dump(scope: DumpScope, *, tool: str, call_id: str, body: str) -> str | None:
    """把全文写入本 loop 的 tool_results 槽，返回文件名。空正文、已是桩、或写盘失败时返回 None。"""
    if not body or is_offloaded(body):
        return None
    dump_dir = scope.dump_dir
    filename = (
        f"{_next_dump_seq(dump_dir):04d}-"
        f"{_safe_name(tool)}-{_safe_name(call_id)[:12]}.txt"
    )
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / filename).write_text(body, encoding="utf-8")
    except OSError:
        return None
    return filename


def dump_tool_result(
    scope: DumpScope,
    *,
    tool: str,
    call_id: str,
    body: str,
    preview_chars: int = PREVIEW_CHARS,
) -> str:
    """把全文写入本 loop 的卸盘槽，返回带相对路径 + preview 的桩。写盘失败则退回原文。"""
    filename = write_tool_dump(scope, tool=tool, call_id=call_id, body=body)
    if filename is None:
        return body
    return format_stub(dump_rel_path(scope, filename), body, preview_chars)


def offload_tool_bodies(turns: list[Turn], scope: DumpScope) -> list[str]:
    """把将被摘要的回合里的 tool 正文落盘，返回已写入的文件名。只写磁盘，不改消息内容。"""
    written: list[str] = []
    for turn in turns:
        names = _tool_name_map(turn)
        for msg in turn.messages:
            if msg.get("role") != "tool":
                continue
            call_id = str(msg.get("tool_call_id") or "")
            filename = write_tool_dump(
                scope,
                tool=names.get(call_id, "tool"),
                call_id=call_id,
                body=str(msg.get("content") or ""),
            )
            if filename:
                written.append(filename)
    return written


def _render_for_summary(turns: list[Turn]) -> str:
    """把待摘要的回合渲染成纯文本。tool 正文只留头部（全文已在磁盘）；整段再截到 _BLOB_CHAR_CAP。"""
    parts: list[str] = []
    for turn in turns:
        names = _tool_name_map(turn)
        for msg in turn.messages:
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "")
            if role == "assistant" and msg.get("tool_calls"):
                calls = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") if isinstance(tc, dict) else {}
                    if isinstance(fn, dict):
                        calls.append(f"{fn.get('name')}({str(fn.get('arguments') or '')[:200]})")
                parts.append(f"[assistant] {content}\n[调用] " + "; ".join(calls))
            elif role == "tool":
                tool = names.get(str(msg.get("tool_call_id") or ""), "tool")
                clipped = content[:_INLINE_TOOL_CAP]
                suffix = " …(全文已落盘)" if len(content) > _INLINE_TOOL_CAP else ""
                parts.append(f"[{tool} 结果] {clipped}{suffix}")
            else:
                parts.append(f"[{role}] {content}")
    return "\n".join(parts)[-_BLOB_CHAR_CAP:]


async def _summarize(
    turns: list[Turn],
    *,
    settings: RuntimeSettings,
    llm: Any,
    notes: SessionNotes,
    dump_hint: str,
    apply_forget: bool,
) -> tuple[str, str | None]:
    """用 Flash 把旧回合收成一段纪要。成功返回 (正文, None)；异常或空响应返回 ("", 错误串)。"""
    system = _SUMMARY_SYSTEM.format(dump_hint=dump_hint)
    if apply_forget:
        directive = forget_directive(notes)
        if directive:
            system = system + "\n\n" + directive

    try:
        result = await llm.chat(
            model=settings.flash_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _render_for_summary(turns)},
            ],
            max_tokens=_SUMMARY_MAX_TOKENS,
        )
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"

    text = (result.content or "").strip()
    if not text:
        return "", f"summarizer 未返回内容 (error_class={result.error_class.value})"
    return text, None


async def compact_history(
    *,
    history: list[dict[str, Any]],
    dump: DumpScope,
    settings: RuntimeSettings,
    llm: Any,
    notes: SessionNotes,
    budget: TokenBudget,
    estimate: int,
    apply_forget: bool = False,
    tracer: Tracer | None = None,
) -> CompactResult:
    """超过 TokenBudget 触发阈值才压缩。未触发、或可移动回合不超过保留数时，原样返回且无副作用。

    触发后：旧回合 tool 正文落入 dump 槽 → Flash 总结 → 连续旧片段就地换成一条纪要（pinned 留原位）。
    仅 Pro（apply_forget）读取并清空 FORGET.md。
    摘要失败仍替换 history，纪要位置写失败说明，compacted=True。
    """
    projected = budget.projected(estimate)
    if not budget.should_compact(estimate):
        return CompactResult(history=history, tokens_before=projected, tokens_after=projected)

    turns = split_turns(history)
    movable = [t for t in turns if not t.pinned]
    keep_n = settings.compact_keep_recent_turns

    if len(movable) <= keep_n:
        return CompactResult(history=history, tokens_before=projected, tokens_after=projected)

    old = movable[:-keep_n]
    recent = movable[-keep_n:]
    summarized = {id(t) for t in old}

    span_cm = (
        tracer.span(
            S.COMPACT,
            kind=S.KIND_CHAIN,
            **{
                S.ATTR_TOKENS_BEFORE: projected,
                S.ATTR_TURNS_SUMMARIZED: len(old),
                S.ATTR_TURNS_KEPT: len(recent),
            },
        )
        if tracer
        else None
    )
    span = span_cm.__enter__() if span_cm is not None else None
    try:
        dumped = offload_tool_bodies(old, dump)
        dump_rel = dump.rel_dir
        dump_hint = f"`{dump_rel}/`" + (f"（{len(dumped)} 个文件）" if dumped else "")
        summary, error = await _summarize(
            old,
            settings=settings,
            llm=llm,
            notes=notes,
            dump_hint=dump_hint,
            apply_forget=apply_forget,
        )

        note = summary if summary else f"(摘要生成失败：{error}。)"
        digest = {
            "role": "user",
            "content": (
                f"## 早前对话纪要\n{note}\n\n"
                f"早前工具的完整输出在 {dump_hint}。"
                "需要原文时用 memory_grep 搜索正文，或用 memory_read / read_file 按路径读取。"
            ),
        }

        # 按原顺序重建：连续被摘要的回合只发出一条纪要，pinned 回合留在原位。
        new_history: list[dict[str, Any]] = []
        emitted = False
        for turn in turns:
            if id(turn) in summarized:
                if not emitted:
                    new_history.append(digest)
                    emitted = True
                continue
            new_history.extend(turn.messages)
        if not emitted:
            new_history.insert(0, digest)

        cleared = clear_forget(dump.session_dir) if apply_forget else 0

        after = budget.projected(count_messages(new_history))
        result = CompactResult(
            history=new_history,
            compacted=True,
            tokens_before=projected,
            tokens_after=after,
            turns_summarized=len(old),
            turns_kept=len(recent),
            forget_cleared=cleared,
            error=error,
        )
        if span is not None:
            span.set(
                **{
                    S.ATTR_TOKENS_AFTER: after,
                    S.ATTR_COMPACTED: True,
                    S.ATTR_FORGET_CLEARED: cleared,
                }
            )
            if error:
                span.set(**{S.ATTR_REASON: error})
        return result
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)
