"""Live Panel - 用 graph.stream 节点事件 + Rich Console 渲染（用户决策）

Fix 4B：从 stream_mode="updates" 升级到 ["updates", "messages"]，token 级流式渲染。
Fix C：拿掉 rich.live.Live，改 plain incremental console.out（避免 Live overflow=visible
       导致整段内容被反复重写到 scrollback 的双重打印问题）

设计要点：
1. updates 模式：节点完成时打 ✅ 一行（保持原行为）
2. messages 模式：节点内 LLM 边产 token 边渲染：
   - 首次产 token 时打头：╭─ thinking · 📋 planner ─...
   - AIMessageChunk.content                    → 绿色（console.out, end=""）
   - AIMessageChunk.tool_call_chunks (args)    → 青色，增量拼
   - ToolMessage.content (Observation)         → 黄色短摘
   - additional_kwargs.reasoning_content       → 灰色斜体
   - 节点切换 / 节点完成时打尾：╰─ 📋 planner ─

不再使用 rich.live.Live：纯 incremental 打印，自然写入 scrollback 不会重复。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()

_NODE_EMOJI = {
    "intake": "🔎",
    "planner": "📋",
    "coder": "🛠️",
    "verifier": "🧪",
    "compile": "📦",
    "summarizer": "📝",
}


def _node_label(node: str) -> str:
    return f"{_NODE_EMOJI.get(node, '•')} {node}"


def print_node_event(node: str, diff: dict[str, Any]) -> None:
    """打印一个节点完成事件（取 state diff 关键信息一行）"""
    label = _node_label(node)
    log_entries = diff.get("progress_log") or []
    summary_bits: list[str] = []
    for entry in log_entries:
        if entry.get("node") == node:
            for k, v in entry.items():
                if k == "node":
                    continue
                summary_bits.append(f"{k}={v}")
    summary = " · ".join(summary_bits) if summary_bits else ""
    console.print(f"  ✅ [bold]{label}[/]   [dim]{summary}[/]")


# ─── messages 模式：streaming 渲染 helpers（Fix C: 去 Live） ─────


class _NodeStreamPanel:
    """单节点的 token 流渲染器：plain incremental print。

    使用方式：
        panel = _NodeStreamPanel(node_name)
        panel.append_content("...")  # 多次
        panel.append_tool_chunk(idx, name, args_delta)
        panel.stop()  # 节点切换或终止时

    不维护 buffer，不使用 Live；首次产 token 时打 head，每次 append 立刻 console.out
    （end="" 不换行），节点结束打 tail。自然写入 scrollback，不会重复。
    """

    def __init__(self, node: str) -> None:
        self.node = node
        self._opened = False
        # 当前正在拼的 tool_call 状态（按 index 区分）
        self._tool_state: dict[int, dict[str, str]] = {}
        # 已打了 "🔧 name(" 头的 idx 集合（之后的 args_delta 直接续）
        self._tool_head_printed: set[int] = set()

    def _ensure_head(self) -> None:
        if not self._opened:
            console.print(f"[dim]╭─ thinking · {_node_label(self.node)} ─[/]")
            self._opened = True

    def append_content(self, text: str) -> None:
        if not text:
            return
        self._ensure_head()
        # console.out: rich 流式打字符不渲染 markup（highlight=False 防把 [xxx] 误当样式）
        console.out(text, end="", style="green", highlight=False)

    def append_reasoning(self, text: str) -> None:
        if not text:
            return
        self._ensure_head()
        console.out(text, end="", style="dim italic", highlight=False)

    def append_tool_chunk(self, idx: int, name: str, args_delta: str) -> None:
        """tool_call_chunks 边到边增量。

        idx 是 chunk.index；同一 index 的多次回调代表同一个 tool_call 的不同片段。
        """
        st = self._tool_state.setdefault(idx, {"name": "", "args": ""})
        if name:
            st["name"] = name
        if args_delta:
            st["args"] += args_delta

        # 第一次见到 name → 在新行上打 "🔧 name("，之后只追加 args_delta
        if idx not in self._tool_head_printed and st["name"]:
            self._ensure_head()
            console.print()  # 换行（拉到新一行打工具调用）
            console.out(f"🔧 {st['name']}(", style="cyan", end="", highlight=False)
            self._tool_head_printed.add(idx)
        if args_delta:
            self._ensure_head()
            console.out(args_delta, style="cyan", end="", highlight=False)

    def append_tool_observation(self, name: str, content: str) -> None:
        """ToolMessage 回填（沙箱执行结果）"""
        if not content:
            return
        snippet = content if len(content) <= 400 else content[:400] + "…"
        self._ensure_head()
        console.print()
        console.out(f"  ⤷ {name}: ", style="yellow", end="", highlight=False)
        console.out(snippet, style="dim yellow", end="", highlight=False)

    def stop(self) -> None:
        """节点结束：补 tool 调用括号 + 换行 + 打尾"""
        if not self._opened:
            return
        # 给所有已打的工具调用补 ")"
        if self._tool_head_printed:
            for _idx in self._tool_head_printed:
                console.out(")", style="cyan", end="", highlight=False)
        console.print()  # 收尾换行
        console.print(f"[dim]╰─ {_node_label(self.node)} ─[/]")
        self._opened = False


# ─── 主入口 ─────────────────────────────────────────────────────


async def stream_graph(graph: Any, state: dict[str, Any], recursion_limit: int = 80) -> dict[str, Any]:
    """跑主图并把节点事件 + token 实时打印；返回最终 state（合并所有 diff）。

    必须 async：主图含 async 节点（run_coder），LangGraph 不允许从 sync 入口（graph.stream）
    调 async 节点。改用 graph.astream + 顶层 asyncio.run 包装（在 cli.py:_run_task 处）。
    这样所有节点共享同一个事件循环，stream_mode='messages' 的 contextvar 透传给所有
    LLM（包括 Coder 内部的 create_react_agent）。
    """
    final_state: dict[str, Any] = dict(state)
    console.print(f"[bold cyan]hwHandler[/] start ··· question = {state.get('question','')!r}")
    t0 = time.time()

    current_panel: _NodeStreamPanel | None = None

    def _switch_panel(node: str) -> _NodeStreamPanel:
        nonlocal current_panel
        if current_panel is not None and current_panel.node != node:
            current_panel.stop()
            current_panel = None
        if current_panel is None:
            current_panel = _NodeStreamPanel(node)
        return current_panel

    def _close_panel() -> None:
        nonlocal current_panel
        if current_panel is not None:
            current_panel.stop()
            current_panel = None

    try:
        async for chunk in graph.astream(
            state,
            stream_mode=["updates", "messages"],
            config={"recursion_limit": recursion_limit},
            subgraphs=True,  # 关键：让父流拿到 create_react_agent 子图（Coder 内层）的事件
        ):
            # subgraphs=True + 多 stream_mode：chunk 形如 (namespace_tuple, mode, payload)
            # namespace 是 ('coder:abc-uuid',) 之类的子图路径；空 tuple 表示主图层
            if not isinstance(chunk, tuple):
                continue
            if len(chunk) == 3:
                ns, mode, payload = chunk
            elif len(chunk) == 2:
                # 兼容某些版本只返二元组（无 namespace）
                ns = ()
                mode, payload = chunk
            else:
                continue

            # 主图节点名：从 namespace 推断（"coder:xxx" → "coder"）；空 ns 表示在主图层
            parent_node = str(ns[0]).split(":", 1)[0] if ns else None

            if mode == "updates":
                # 仅在主图层（ns 为空）打 ✅ 节点摘要 + 合并 diff；
                # 子图层（agent/tools 循环）的 updates 全部跳过——避免刷屏 + label 错位
                if ns:
                    continue
                _close_panel()
                if not isinstance(payload, dict):
                    continue
                for node, diff in payload.items():
                    print_node_event(node, diff or {})
                    for k, v in (diff or {}).items():
                        final_state[k] = v

            elif mode == "messages":
                # payload = (message_chunk, metadata)
                if not isinstance(payload, tuple) or len(payload) != 2:
                    continue
                msg, metadata = payload
                # 子图层 token：用 parent_node 做 label，让所有 react 子节点的流式输出
                # 都汇聚到主图节点（如 🛠️ coder）的同一 panel，连贯不切碎
                node = parent_node or (metadata or {}).get("langgraph_node", "?")
                cls = msg.__class__.__name__

                if cls in {"AIMessageChunk", "AIMessage"}:
                    panel = _switch_panel(node)
                    content = getattr(msg, "content", "")
                    if isinstance(content, str) and content:
                        panel.append_content(content)

                    tcs = getattr(msg, "tool_call_chunks", None) or []
                    for tc in tcs:
                        idx = int(tc.get("index", 0) or 0)
                        panel.append_tool_chunk(
                            idx,
                            tc.get("name", "") or "",
                            tc.get("args", "") or "",
                        )

                    ak = getattr(msg, "additional_kwargs", None) or {}
                    reasoning = ak.get("reasoning_content") or ""
                    if reasoning:
                        panel.append_reasoning(reasoning)

                elif cls == "ToolMessage":
                    panel = _switch_panel(node)
                    name = getattr(msg, "name", "") or "tool"
                    content = getattr(msg, "content", "")
                    if not isinstance(content, str):
                        content = str(content)
                    panel.append_tool_observation(name, content)
                # 其他类型（HumanMessage / SystemMessage 等）通常不在 messages 流中出现，忽略
    finally:
        _close_panel()

    elapsed = time.time() - t0
    console.print(f"[dim]elapsed: {elapsed:.1f}s[/]")
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

    console.print(Panel(body, title="hwHandler", border_style="green" if not partial else "yellow"))


def print_crash_panel(exc: BaseException, crash_log_path: Path) -> None:
    body = Text()
    body.append("❌ 主图异常退出\n", style="bold red")
    body.append(f"{type(exc).__name__}: {exc}\n", style="red")
    body.append(f"\nCRASH 详情：{crash_log_path}\n", style="dim")
    console.print(Panel(body, title="hwHandler crash", border_style="red"))
