"""sandbox_tools - AIO Sandbox MCP 封装。

抽象层：Coder 经 get_sandbox_tools 拿到 MCP 工具集（已包路径翻译）
供 ReAct 循环调用；Intake 用 sandbox_convert_to_markdown 解析
作业指导文档；未来切换 CubeSandbox/E2B 时只换
实现层。当前实现用 langchain-mcp-adapters 拿
MCP 工具并调用。

工具名：
- sandbox_execute_code
- sandbox_execute_bash
- sandbox_file_operations
- sandbox_str_replace_editor
- sandbox_convert_to_markdown
- sandbox_get_packages

路径约定：
- host WORKSPACE_DIR 绑定挂载到容器 /workspace（见 infra/sandbox_boot.py）
- agent 传 host 绝对路径时，本模块自动翻译成 /workspace/<rel>
- 已是 /workspace/... 或相对路径或不在 workspace 下的绝对路径 -> 原样转发
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_tools_cache: dict[str, Any] | None = None


# ─── 路径翻译（host -> 容器）────────────────────────────


_SANDBOX_WORKSPACE = "/workspace"


def _translate_path(p: str) -> str:
    """将 host workspace 绝对路径翻译成容器 /workspace/<rel>。

    其他形式（相对路径 / 已是 /workspace/... / 其他绝对路径）
    原样返回。
    """
    if not p or not isinstance(p, str):
        return p
    if p.startswith(_SANDBOX_WORKSPACE):
        return p
    # 相对路径不翻译
    if not p.startswith("/"):
        return p
    try:
        host_ws = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
        target = Path(p).resolve()
        # 只翻译 workspace 内的路径
        try:
            rel = target.relative_to(host_ws)
        except ValueError:
            return p  # 不在 workspace，原样转发让沙箱报错（使 LLM 可见并纠正）
        return f"{_SANDBOX_WORKSPACE}/{rel}".replace("\\", "/")
    except Exception:
        return p


async def _load_sandbox_tools() -> dict[str, Any]:
    """异步从 mcp_client 取工具并按名索引。"""
    global _tools_cache
    if _tools_cache is None:
        from mcp_client import get_tools
        all_tools = await get_tools()
        _tools_cache = {t.name: t for t in all_tools}
    return _tools_cache


# ─── 封装层：给 agent 的 LangChain Tools 调用前翻译路径参数 ──


_PATH_KW = {"path", "file_path"}  # 沙箱工具中表示文件路径的 kwargs


def _translate_kwargs(kwargs: dict[str, Any]) -> None:
    """在已知路径 kwargs 中将 host workspace 路径翻译成容器 /workspace/...。"""
    for k in list(kwargs.keys()):
        if k in _PATH_KW and isinstance(kwargs[k], str):
            kwargs[k] = _translate_path(kwargs[k])


# sandbox_execute_code Jupyter kernel ack-only 提示
_ACK_ONLY_HINT = (
    "\n\n[labhandler] ack-only: the Jupyter kernel dispatched an async ack"
    " (stdout/stderr/exit_code are all null); the code may not have finished executing yet."
    " To get results synchronously, use sandbox_execute_bash to run the command instead."
)


def _annotate_ack_only_if_needed(result: Any) -> Any:
    """检测 sandbox_execute_code 响应是否为 Jupyter kernel ack-only 响应。

    ack-only 形态：status=ok 且 stdout/stderr/exit_code 均为 null。
    命中时在文本块末尾追加提示，引导 LLM 改用
    同步执行工具。
    """
    try:
        if not isinstance(result, list) or not result:
            return result
        new_list: list[Any] = []
        modified = False
        for item in result:
            new_item = item
            if isinstance(item, dict) and item.get("type") == "text":
                txt = item.get("text") or ""
                if isinstance(txt, str) and txt:
                    try:
                        payload = json.loads(txt)
                    except Exception:
                        payload = None
                    if (
                        isinstance(payload, dict)
                        and payload.get("status") == "ok"
                        and payload.get("stdout") is None
                        and payload.get("stderr") is None
                        and payload.get("exit_code") is None
                    ):
                        new_item = dict(item)
                        new_item["text"] = txt + _ACK_ONLY_HINT
                        modified = True
            new_list.append(new_item)
        return new_list if modified else result
    except Exception:
        return result


# ─── 沙箱连续失败检测 ─────────────────────────────

_SANDBOX_MAX_FAILURES = 3
"""沙箱工具连续失败阈值。超过后工具返回
`[SANDBOX_UNREACHABLE]` 致命标记；Coder 节点检测到后终止
当前 step 的 ReAct 循环，不再重试。"""

_sandbox_failures: dict[str, int] = {}
"""tool_name -> 当前连续失败计数（每步开始前由 reset_sandbox_failure_counter() 清零）。"""


def reset_sandbox_failure_counter() -> None:
    """每步开始前调用，重置所有沙箱工具的连续失败计数。"""
    _sandbox_failures.clear()


def _wrap_tool_with_path_translation(orig_tool: Any) -> Any:
    """构造 StructuredTool，调用前对 path/file_path 参数执行 _translate_path。

    不直接改原工具的原因：langchain-mcp-adapters 返回的
    BaseTool 基于 Pydantic v2 BaseModel，不允许给非字段
    属性赋值。所以用工厂方法新建 StructuredTool，
    复用原工具的 name/description/args_schema；路径翻译
    在协程里做，随后转发给原工具的 ainvoke。
    """
    from langchain_core.tools import StructuredTool

    async def acall(**kwargs: Any) -> Any:
        _translate_kwargs(kwargs)
        try:
            result = await orig_tool.ainvoke(kwargs)
        except Exception as e:
            tool_name = orig_tool.name
            _sandbox_failures[tool_name] = _sandbox_failures.get(tool_name, 0) + 1
            count = _sandbox_failures[tool_name]
            if count >= _SANDBOX_MAX_FAILURES:
                return (
                    f"[SANDBOX_UNREACHABLE] sandbox tool {tool_name} failed {count} times in a row"
                    f" ({type(e).__name__}: {e}); the sandbox may be unavailable, aborting the current step"
                )
            return f"[tool_error] {type(e).__name__}: {e}"
        # 成功时重置计数
        _sandbox_failures[orig_tool.name] = 0
        # sandbox_execute_code 用 Jupyter kernel 异步派发，可能只返回 ack。
        # 命中时追加提示引导 LLM 改用同步执行工具。
        if orig_tool.name == "sandbox_execute_code":
            result = _annotate_ack_only_if_needed(result)
        return result

    return StructuredTool(
        name=orig_tool.name,
        description=getattr(orig_tool, "description", "") or "",
        args_schema=getattr(orig_tool, "args_schema", None),
        coroutine=acall,
    )


async def _call(tool_name: str, **kwargs: Any) -> Any:
    """统一调用入口：取工具后 ainvoke。"""
    tools = await _load_sandbox_tools()
    if tool_name not in tools:
        raise RuntimeError(
            f"sandbox tool {tool_name} is not exposed in the MCP server tool list"
            f" (known: {list(tools.keys())[:5]}...)"
        )
    # 翻译路径 kwargs（与 _wrap_tool_with_path_translation 一致）
    _translate_kwargs(kwargs)
    return await tools[tool_name].ainvoke(kwargs)


# ─── 抽象接口 ───────────────────────────


async def sandbox_convert_to_markdown(file_path: str) -> str:
    """在沙箱内部解析 PDF/DOCX/PPT 为 markdown，供 Intake 节点解析作业指导。

    参数名保留 file_path 以兼容现有调用方；内部翻译成
    带 file:// 前缀的容器路径，以 uri= 传给 MCP 工具。
    返回值把 MCP content 数组的所有 text 项拼成单一字符串。
    """
    container_path = _translate_path(file_path)
    if not container_path.startswith(("file://", "http://", "https://", "data:")):
        container_path = f"file://{container_path}"
    result = await _call("sandbox_convert_to_markdown", uri=container_path)
    if isinstance(result, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in result
        )
    return result if isinstance(result, str) else str(result)


# ReAct 循环需要 LangChain Tool 实例供 bind_tools；
# 本接口返回带路径翻译封装的工具列表。
async def get_sandbox_tools() -> list[Any]:
    """返回所有 AIO Sandbox MCP 工具（封装为 LangChain Tools）。

    每个工具含路径翻译层：host workspace 路径 -> /workspace/...
    """
    tools = await _load_sandbox_tools()
    return [_wrap_tool_with_path_translation(t) for t in tools.values()]
