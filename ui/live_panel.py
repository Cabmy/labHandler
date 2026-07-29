"""Live Panel — 任务执行流式状态输出（GraphEvent 的终端渲染器）。

astream 解析统一在 ui/events.py:iter_graph_events（Web SSE 与终端共用同一事件流）；
本模块只负责把 GraphEvent 渲染成 rich 终端输出。
完整消息与工具历史由 compile_node 落盘至
workspace/.labhandler/runs/<ts>/transcript.jsonl + tool_history.jsonl。

输出格式（逐行追加，无边框、无重绘，视觉语言对标 Codex CLI 转录范式）：

    › 请按 README 完成作业
    • intake
      ✓ title=... · type=... · n_constraints=...
    • planner
      ✓ iteration=1 · skill=coding · n_nodes=3
    • coder
      <streaming content / dim italic reasoning>
      • tool_name(key_args)
        └ result_snippet
      ✓ step_id=n1 · step_idx=0 · n_messages=18
    ── worked for 32.1s ──
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from config.runtime import get_settings
from orchestrator.replan import is_partial

console = Console()

WORKSPACE_DIR: Path = get_settings().workspace_dir


def print_node_event(node: str, log_entries: list[dict[str, Any]]) -> None:
    """节点完成事件：从属于该节点的 progress_log 条目拼摘要，缩进输出 ✓ 行。"""
    summary_bits: list[str] = []
    for entry in log_entries or []:
        for k, v in entry.items():
            if k == "node":
                continue
            summary_bits.append(f"{k}={v}")
    summary = " · ".join(summary_bits) if summary_bits else "done"
    console.print(f"  [dim green]✓[/] [dim]{summary}[/]")


# ─── 工具调用 / 结果短摘要 ─────────────────────────────────────


def _args_one_line(raw: Any) -> str:
    """工具参数单行摘要，截断至 80 字符。

    优先提取字段：path / file_path / cmd / command / action / code；
    均不存在则取首个 k=v 对；解析失败 fallback 至原文截断。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except Exception:
            one = raw.replace("\n", " ")
            return one[:80] + ("…" if len(one) > 80 else "")
    else:
        d = raw
    if not isinstance(d, dict):
        s = str(d).replace("\n", " ")
        return s[:80] + ("…" if len(s) > 80 else "")
    for k in ("path", "file_path", "cmd", "command", "action", "code"):
        if k in d and d[k] not in (None, "", [], {}):
            v = str(d[k]).replace("\n", " ")
            return f"{k}={v[:60]}" + ("…" if len(v) > 60 else "")
    if d:
        k, v = next(iter(d.items()))
        v_short = str(v).replace("\n", " ")
        return f"{k}={v_short[:60]}" + ("…" if len(v_short) > 60 else "")
    return ""


def _short(text: str, n: int = 140) -> str:
    """工具结果单行摘要：折叠换行并截断至 n 字符。"""
    if not text:
        return ""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= n else one_line[:n] + "…"


# ─── 主入口 ────────────────────────────────────────────────────


async def stream_graph(
    graph: Any,
    state: dict[str, Any] | None,
    recursion_limit: int = 80,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行主图并流式打印事件；返回合并所有 diff 后的最终 state。

    解析逻辑在 ui/events.py:iter_graph_events；本函数只做终端渲染。
    必须为 async：主图含 async 节点（run_coder），graph.astream 由顶层
    asyncio loop 包装（cli.py:_run_task），确保 stream_mode='messages' 的
    contextvar 可透传至所有 LLM（含 Coder 内部的 create_react_agent）。
    config 透传给主图（checkpointer 的 thread_id 等）。
    state=None 为断点续跑（从 checkpoint 继续），返回值只含续跑期间 diff。
    """
    from ui.events import iter_graph_events

    q = (state or {}).get("question", "")
    if state is None:
        console.print("[dim]› resume from checkpoint[/]")
    else:
        console.print(f"[bold]› {q}[/]")
    t0 = time.time()

    final_state: dict[str, Any] = dict(state) if state else {}
    elapsed: float = 0.0
    # 当前是否处于流式 content 行（决定节点切换前是否补换行）
    content_open: bool = False

    def _close_content_line() -> None:
        nonlocal content_open
        if content_open:
            console.print()
            content_open = False

    try:
        async for ev in iter_graph_events(
            graph, state, recursion_limit=recursion_limit, config=config
        ):
            if ev.kind == "node_start":
                _close_content_line()
                console.print(f"\n[bold]• {ev.node}[/]")

            elif ev.kind == "content":
                # 流式输出 LLM content / reasoning，给用户"模型在动"的可见性；
                # reasoning 用 dim italic 弱化（Codex thinking 同款层级），不刷屏
                text = ev.payload.get("text", "")
                if not text:
                    continue
                if not content_open:
                    console.out("  ", end="", highlight=False)  # 缩进与工具行对齐
                    content_open = True
                style = "dim italic" if ev.payload.get("reasoning") else ""
                console.out(text, end="", style=style, highlight=False)

            elif ev.kind == "tool":
                _close_content_line()
                name = ev.payload.get("name", "tool")
                args_one = _args_one_line(ev.payload.get("args"))
                body = f"  • [bold]{name}[/]"
                if args_one:
                    body += f"([dim]{args_one}[/])"
                console.print(body)
                snippet = _short(ev.payload.get("content", ""), 140)
                if snippet:
                    console.print(f"    [dim]└ {snippet}[/]")

            elif ev.kind == "node_done":
                _close_content_line()
                print_node_event(ev.node, ev.payload.get("log") or [])

            elif ev.kind == "final":
                final_state = ev.payload.get("state", final_state)
                elapsed = float(ev.payload.get("elapsed", 0.0))
    finally:
        _close_content_line()

    console.print()
    console.print(Rule(
        f"[dim]worked for {elapsed or (time.time() - t0):.1f}s[/]",
        style="dim", characters="─",
    ))
    console.print("[dim]日志见 workspace/.labhandler/runs/<latest>/[/]")
    return final_state


def print_completion_panel(state: dict[str, Any]) -> None:
    """任务完成面板：做了什么 / 在哪里"""
    runs = state.get("verifier_runs") or []
    last_verdict = (runs[-1].get("verdict") if runs else "unknown")
    iter_n = state.get("iteration", 0)
    intake = state.get("intake_result") or {}
    title = intake.get("title", "?")

    partial = is_partial(state)

    artifacts = state.get("artifacts") or []
    art_paths = sorted({a.get("path", "") for a in artifacts if a.get("path")})

    summary_path = WORKSPACE_DIR / "SUMMARY.md"

    body = Text()
    body.append("任务完成  " if not partial else "部分完成  ",
                style="bold green" if not partial else "bold yellow")
    body.append(f"{title}\n\n", style="bold")
    body.append("  verdict     ", style="dim")
    body.append(f"{last_verdict}\n", style="green" if last_verdict == "pass" else "yellow")
    body.append("  iterations  ", style="dim")
    body.append(f"{iter_n}\n")
    body.append(f"  产物 ({len(art_paths)} 件)\n", style="dim")
    for p in art_paths:
        body.append("    • ", style="dim")
        body.append(f"{p}\n")

    if partial:
        missing = (runs[-1].get("coverage", {}).get("missing") if runs else []) or []
        sf = (runs[-1].get("suggested_fix") if runs else "") or ""
        body.append("\n  待办\n", style="bold")
        for m in missing:
            body.append("    • ", style="dim")
            body.append(f"{m.get('constraint','')}\n", style="red")
        if sf:
            body.append("  建议  ", style="dim")
            body.append(f"{sf}\n", style="yellow")

    console.print(Panel(
        body, title="labHandler", title_align="left",
        border_style="dim", padding=(1, 2),
    ))

    # SUMMARY 全文直接渲染到终端（跑完即交付，不让用户再去 cat 文件）
    if summary_path.exists():
        console.print(Panel(
            Markdown(summary_path.read_text(encoding="utf-8")),
            title="workspace/SUMMARY.md", title_align="left",
            border_style="dim", padding=(1, 2),
        ))


def print_crash_panel(exc: BaseException, crash_log_path: Path) -> None:
    body = Text()
    body.append("主图异常退出\n\n", style="bold red")
    body.append(f"  {type(exc).__name__}: {exc}\n", style="red")
    body.append("  CRASH 详情  ", style="dim")
    body.append(f"{crash_log_path}\n", style="dim")
    console.print(Panel(
        body, title="labHandler crash", title_align="left",
        border_style="red dim", padding=(1, 2),
    ))
