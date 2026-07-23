"""Live Panel — 任务执行流式状态输出。

将 LangGraph 主图执行过程中的节点事件、LLM 输出与工具调用按时间顺序流式呈现。
完整消息与工具历史由 compile_node 落盘至
workspace/.labhandler/runs/<ts>/transcript.jsonl + tool_history.jsonl。

输出格式（逐行追加，无边框、无重绘）：

    🔎 intake  ✓ title=... · type=... · n_constraints=...
    📋 planner  ✓ iteration=1 · skill=coding · n_nodes=3
    🛠️ coder   <streaming content>
      ⏺ tool_name(key_args)  └─ result_snippet
      ✓ step_id=n1 · step_idx=0 · n_messages=18

设计约束：
1. stream_graph 同时订阅 updates（节点完成 diff）与 messages（LLM/tool 消息），
   updates 用于节点摘要，messages 用于 LLM 输出与 tool_call 配对。
2. Coder 节点流式输出 content/reasoning_content；其余节点采用 dual-segment prompt，
   content 含长段 thinking 文本不输出至终端（完整版已落盘）。
3. tool_call_chunks 按 chunk index 累积，待同一 id 的 ToolMessage 到达后统一 emit，
   避免长 args 占用终端。
4. LangGraph react_agent 子图内部节点（model/tools/agent）统一映射至 coder，
   防止出现错位的中间节点行。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from config.runtime import get_settings
from orchestrator.state import REDUCER_LIST_FIELDS

console = Console()

WORKSPACE_DIR: Path = get_settings().workspace_dir

_NODE_EMOJI = {
    "intake": "🔎",
    "planner": "📋",
    "coder": "🛠️",
    "coder_step": "🛠️",
    "verifier": "🧪",
    "compile": "📦",
    "summarizer": "📝",
}

# 主图节点名 → 终端显示归一化名（coder_step 与子图 coder 共享 label）
_NODE_CANONICAL = {"coder_step": "coder"}

# react_agent 子图内部节点 → 映射至主图 coder
_REACT_INTERNAL_NODES = {"agent", "model", "tools", "call_model", "call_tools"}


def _canonical(node: str) -> str:
    return _NODE_CANONICAL.get(node, node)


def _node_label(node: str) -> str:
    return f"{_NODE_EMOJI.get(node, '•')} {node}"


def print_node_event(node: str, diff: dict[str, Any]) -> None:
    """节点完成事件：提取 diff.progress_log 中对应摘要，缩进输出 ✓ 行。"""
    log_entries = diff.get("progress_log") or []
    summary_bits: list[str] = []
    for entry in log_entries:
        if entry.get("node") == node or entry.get("node") == _canonical(node):
            for k, v in entry.items():
                if k == "node":
                    continue
                summary_bits.append(f"{k}={v}")
    summary = " · ".join(summary_bits) if summary_bits else "done"
    console.print(f"  [green]✓[/] [dim]{summary}[/]")


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


async def stream_graph(graph: Any, state: dict[str, Any], recursion_limit: int = 80) -> dict[str, Any]:
    """执行主图并流式打印事件；返回合并所有 diff 后的最终 state。

    必须为 async：主图含 async 节点（run_coder），LangGraph 不允许从 sync 入口调用
    async 节点。graph.astream 由顶层 asyncio.run 包装（cli.py:_run_task），
    确保所有节点共享同一事件循环，stream_mode='messages' 的 contextvar 可透传至
    所有 LLM（含 Coder 内部的 create_react_agent）。
    """
    final_state: dict[str, Any] = dict(state)
    console.print(f"[bold cyan]labHandler[/] start ··· question = {state.get('question','')!r}")
    t0 = time.time()

    last_node: str | None = None
    # chunk index → {name, args 累积字符串, id}，ToolMessage 到达后按 id 匹配
    chunk_buf: dict[int, dict[str, str]] = {}
    # 当前是否处于流式 content 行（决定节点切换前是否补换行）
    content_open: bool = False

    def _close_content_line() -> None:
        nonlocal content_open
        if content_open:
            console.print()
            content_open = False

    def _resolve_node(parent: str | None, metadata: dict[str, Any] | None) -> str:
        """消息归属：react 子图内部节点统一归至主图 coder。"""
        if parent:
            return parent
        meta = (metadata or {}).get("langgraph_node", "") or ""
        if meta in _REACT_INTERNAL_NODES:
            return "coder"
        return meta or (last_node or "?")

    def _emit_start(node: str) -> None:
        nonlocal last_node
        c = _canonical(node)
        if c != last_node:
            _close_content_line()
            console.print(f"[bold]{_node_label(c)}[/]")
            last_node = c

    def _emit_tool(node: str, name: str, args_raw: Any, content: str) -> None:
        _emit_start(node)
        _close_content_line()
        args_one = _args_one_line(args_raw)
        body = f"  ⏺ [bold]{name}[/]"
        if args_one:
            body += f"([dim]{args_one}[/])"
        console.print(body)
        snippet = _short(content, 140)
        if snippet:
            console.print(f"    [dim]└─ {snippet}[/]")

    def _emit_content_chunk(node: str, text: str, *, style: str) -> None:
        """流式输出 LLM content / reasoning,给用户"模型在动"的可见性。

        所有节点都打:intake/planner/verifier/summarizer 走 dual-segment(thinking + result),
        看起来是一段长文 + 一段 JSON——reasoning 用 dim 自然弱化,不刷屏。
        """
        nonlocal content_open
        if not text:
            return
        _emit_start(node)
        if not content_open:
            console.out("  ", end="", highlight=False)  # 缩进与 ⏺ 对齐
            content_open = True
        console.out(text, end="", style=style, highlight=False)

    try:
        async for chunk in graph.astream(
            state,
            stream_mode=["updates", "messages"],
            config={"recursion_limit": recursion_limit},
            subgraphs=True,  # 获取 create_react_agent 子图事件
        ):
            # subgraphs=True + 多 stream_mode: chunk = (namespace_tuple, mode, payload)
            # namespace 如 ('coder:uuid',)；空 tuple 表示主图层
            if not isinstance(chunk, tuple):
                continue
            if len(chunk) == 3:
                ns, mode, payload = chunk
            elif len(chunk) == 2:
                ns = ()
                mode, payload = chunk
            else:
                continue

            # 主图节点名：从 namespace 提取前缀（"coder:xxx" → "coder"）；空 ns 即主图层
            parent_node = str(ns[0]).split(":", 1)[0] if ns else None

            if mode == "updates":
                # 仅处理主图层（ns 为空）的节点完成事件，子图层跳过以避免重复
                if ns:
                    continue
                if not isinstance(payload, dict):
                    continue
                for node, diff in payload.items():
                    _emit_start(node)  # 确保无 LLM 流的纯 Python 节点（如 compile）也有归属行
                    _close_content_line()
                    print_node_event(node, diff or {})
                    for k, v in (diff or {}).items():
                        # 归约字段（Annotated[list, add]）需累加而非覆盖，
                        # 否则 final_state 只保留最后一个节点 return 的增量，丢失历史。
                        if k in REDUCER_LIST_FIELDS and isinstance(v, list):
                            existing = final_state.get(k)
                            base = existing if isinstance(existing, list) else []
                            final_state[k] = base + v
                        else:
                            final_state[k] = v

            elif mode == "messages":
                # payload = (message_chunk, metadata)
                if not isinstance(payload, tuple) or len(payload) != 2:
                    continue
                msg, metadata = payload
                # react 子图内部节点归一化至主图 coder
                node = _resolve_node(parent_node, metadata)
                cls = msg.__class__.__name__

                if cls in {"AIMessageChunk", "AIMessage"}:
                    _emit_start(node)
                    # content: LLM 工具调用间的思考文字(默认色,跟终端配色)
                    text = getattr(msg, "content", "")
                    if isinstance(text, str) and text:
                        _emit_content_chunk(node, text, style="")
                    # reasoning_content: 思维链输出(dim 弱化,与 content 区分)
                    ak = getattr(msg, "additional_kwargs", None) or {}
                    reasoning = ak.get("reasoning_content") or ""
                    if isinstance(reasoning, str) and reasoning:
                        _emit_content_chunk(node, reasoning, style="dim")
                    # 累积 tool_call_chunks，待 ToolMessage 配对后统一 emit
                    for tc in getattr(msg, "tool_call_chunks", None) or []:
                        idx = int(tc.get("index", 0) or 0)
                        st = chunk_buf.setdefault(idx, {"name": "", "args": "", "id": ""})
                        if tc.get("name"):
                            st["name"] = tc["name"]
                        if tc.get("id"):
                            st["id"] = tc["id"]
                        if tc.get("args"):
                            st["args"] += tc["args"]

                elif cls == "ToolMessage":
                    tid = getattr(msg, "tool_call_id", "")
                    name = getattr(msg, "name", "") or "tool"
                    content = getattr(msg, "content", "")
                    if not isinstance(content, str):
                        content = str(content)
                    # 从 chunk_buf 匹配 args 与 fallback name
                    args_raw: Any = ""
                    matched_idx: int | None = None
                    for idx, st in chunk_buf.items():
                        if st.get("id") == tid:
                            args_raw = st.get("args", "")
                            if name == "tool" and st.get("name"):
                                name = st["name"]
                            matched_idx = idx
                            break
                    _emit_tool(node, name, args_raw, content)
                    if matched_idx is not None:
                        chunk_buf.pop(matched_idx, None)
                # HumanMessage / SystemMessage 通常不出现在 messages 流中，忽略
    finally:
        _close_content_line()

    elapsed = time.time() - t0
    console.print(
        f"[dim]elapsed: {elapsed:.1f}s · 日志见 "
        f"workspace/.labhandler/runs/<latest>/[/]"
    )
    return final_state


def print_completion_panel(state: dict[str, Any]) -> None:
    """任务完成面板：做了什么 / 在哪里"""
    runs = state.get("verifier_runs") or []
    last_verdict = (runs[-1].get("verdict") if runs else "unknown")
    iter_n = state.get("iteration", 0)
    intake = state.get("intake_result") or {}
    title = intake.get("title", "?")

    partial = last_verdict == "fail" and iter_n >= int(os.getenv("MAX_REPLAN_ITER", "2"))

    artifacts = state.get("artifacts") or []
    art_paths = sorted({a.get("path", "") for a in artifacts if a.get("path")})

    summary_path = WORKSPACE_DIR / "SUMMARY.md"

    body = Text()
    body.append("✅ 任务完成：" if not partial else "⚠️  部分完成：", style="bold")
    body.append(f"{title}\n")
    body.append(f"verdict       : {last_verdict}\n")
    body.append(f"iterations    : {iter_n}\n")
    body.append(f"产物 ({len(art_paths)} 件):\n")
    for p in art_paths:
        body.append(f"  • {p}\n", style="green" if not partial else "yellow")
    if summary_path.exists():
        body.append(
            "\n参阅 workspace/SUMMARY.md 了解 agent 做了什么、文件作用与怎么验证。\n",
            style="dim",
        )

    if partial:
        missing = (runs[-1].get("coverage", {}).get("missing") if runs else []) or []
        sf = (runs[-1].get("suggested_fix") if runs else "") or ""
        body.append("\n待办：\n", style="bold red")
        for m in missing:
            body.append(f"  • {m.get('constraint','')}\n", style="red")
        if sf:
            body.append(f"\n建议：{sf}\n", style="yellow")

    console.print(Panel(body, title="labHandler", border_style="green" if not partial else "yellow"))


def print_crash_panel(exc: BaseException, crash_log_path: Path) -> None:
    body = Text()
    body.append("❌ 主图异常退出\n", style="bold red")
    body.append(f"{type(exc).__name__}: {exc}\n", style="red")
    body.append(f"\nCRASH 详情：{crash_log_path}\n", style="dim")
    console.print(Panel(body, title="labHandler crash", border_style="red"))
