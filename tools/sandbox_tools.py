"""sandbox_tools - AIO Sandbox MCP 包装

**抽象层目的（PLAN §14）**：
1. agent 代码通过本模块的 6 个函数调沙箱，未来切 CubeSandbox/E2B 时只换实现层
2. 当前实现走 langchain-mcp-adapters 拿 MCP tool 后调用
3. **当前阶段不实连**：函数体都用懒加载 + 异步包装，import 不报错；
   真实跑要在 Phase 4 Coder（容器已起）时才会发生

实测工具名（Phase 0 P0.4，AIO Sandbox 33 tools 子集）：
- sandbox_execute_code
- sandbox_execute_bash
- sandbox_file_operations
- sandbox_str_replace_editor
- sandbox_convert_to_markdown
- sandbox_get_packages

**路径约定**（Fix 2B）：
- 宿主 WORKSPACE_DIR 通过 `-v` 挂载到容器 /workspace（见 infra/sandbox_boot.py）
- agent 给 sandbox 工具的 path / file_path 形参可以是宿主绝对路径，本模块自动翻译为 /workspace/<rel>
- 已经是 /workspace/... 或相对路径或非 workspace 下的绝对路径 → 原样转发
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

# 工具名常量（Phase 0 实测；如沙箱版本变化需要在这里调）
SANDBOX_TOOL_NAMES = [
    "sandbox_execute_code",
    "sandbox_execute_bash",
    "sandbox_file_operations",
    "sandbox_str_replace_editor",
    "sandbox_convert_to_markdown",
    "sandbox_get_packages",
]


_tools_cache: Optional[dict[str, Any]] = None


# ─── 路径翻译（host → container） ─────────────────────────────────


_SANDBOX_WORKSPACE = "/workspace"


def _translate_path(p: str) -> str:
    """把宿主 workspace 下的绝对路径翻译成容器 /workspace/<rel>。

    其他形态（相对路径 / 已经是 /workspace/... / 其他绝对路径）原样返回。
    """
    if not p or not isinstance(p, str):
        return p
    # 已是容器路径
    if p.startswith(_SANDBOX_WORKSPACE):
        return p
    # 非绝对路径（agent 用相对路径时也常发生）→ 不翻译
    if not p.startswith("/"):
        return p
    try:
        host_ws = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
        target = Path(p).resolve()
        # 仅当 path 在 host_ws 内时翻译；is_relative_to 在 py3.9+ 可用
        try:
            rel = target.relative_to(host_ws)
        except ValueError:
            return p  # 不在 workspace 内，原样转发，让沙箱报错（让 LLM 看到并改正）
        return f"{_SANDBOX_WORKSPACE}/{rel}".replace("\\", "/")
    except Exception:
        return p


async def _load_sandbox_tools() -> dict[str, Any]:
    """异步从 mcp_client 拿 tools 并按名字索引。"""
    global _tools_cache
    if _tools_cache is None:
        from mcp_client import get_tools
        all_tools = await get_tools()
        _tools_cache = {t.name: t for t in all_tools}
    return _tools_cache


# ─── 包装层：Agent 拿到的 LangChain Tool 在调用前先翻译 path 形参 ──


_PATH_KW = {"path", "file_path"}  # 沙箱工具中代表"文件路径"的形参名


def _wrap_tool_with_path_translation(orig_tool: Any) -> Any:
    """构造一个新 StructuredTool，调用前对 path / file_path 形参跑 _translate_path。

    为什么要构造新 tool 而不是改原 tool：langchain-mcp-adapters 给的 BaseTool 是 Pydantic v2
    BaseModel，不允许给非 field 赋值（如 `tool.ainvoke = ...` 会 ValueError）。所以我们用
    工厂法新建一个 StructuredTool，name/description/args_schema 复用原 tool（让 LLM 看到的
    schema 不变），coroutine 实现里翻译完 path 再转发到原 tool 的 ainvoke。
    """
    from langchain_core.tools import StructuredTool

    async def acall(**kwargs: Any) -> Any:
        for k in list(kwargs.keys()):
            if k in _PATH_KW and isinstance(kwargs[k], str):
                kwargs[k] = _translate_path(kwargs[k])
        try:
            return await orig_tool.ainvoke(kwargs)
        except Exception as e:
            # 单次工具失败不爆 Coder 节点：把错误当 Observation 回灌，让 ReAct 的
            # LLM 看到错误并自适应（换工具 / 换站点 / 跳过）。`[tool_error]` 前缀
            # 让 LLM 一眼识别。覆盖范围：33 个 sandbox MCP 工具的偶发失败
            # （browser_* ERR_CONNECTION_REFUSED / execute_code timeout / 等）。
            return f"[tool_error] {type(e).__name__}: {e}"

    return StructuredTool(
        name=orig_tool.name,
        description=getattr(orig_tool, "description", "") or "",
        args_schema=getattr(orig_tool, "args_schema", None),
        coroutine=acall,
    )


async def _call(tool_name: str, **kwargs: Any) -> Any:
    """统一调用入口：拿到 tool 后 ainvoke。"""
    tools = await _load_sandbox_tools()
    if tool_name not in tools:
        raise RuntimeError(
            f"沙箱工具 {tool_name} 未在 MCP server 暴露的工具列表里"
            f"（已知：{list(tools.keys())[:5]}...）"
        )
    # 翻译 path 形参（与 agent 走 react 的 wrap 逻辑保持一致）
    for k in list(kwargs.keys()):
        if k in _PATH_KW and isinstance(kwargs[k], str):
            kwargs[k] = _translate_path(kwargs[k])
    return await tools[tool_name].ainvoke(kwargs)


# ─── 6 个抽象接口（Phase 4 Coder 主调） ───────────────────────────


async def sandbox_execute_code(code: str, language: str = "python") -> str:
    """在沙箱内执行代码片段。返回 stdout/stderr 合并文本。"""
    return await _call("sandbox_execute_code", code=code, language=language)


async def sandbox_execute_bash(cmd: str) -> str:
    """在沙箱内执行 bash 命令（如 pytest / pip install）。"""
    return await _call("sandbox_execute_bash", cmd=cmd)


async def sandbox_file_operations(action: str, path: str, **kwargs: Any) -> Any:
    """沙箱内文件操作（read / write / mkdir / delete 等，具体动作看 MCP schema）。"""
    return await _call("sandbox_file_operations", action=action, path=path, **kwargs)


async def sandbox_str_replace_editor(
    command: str, path: str, **kwargs: Any
) -> Any:
    """沙箱内字符串替换编辑器（精确改代码用）。"""
    return await _call("sandbox_str_replace_editor", command=command, path=path, **kwargs)


async def sandbox_convert_to_markdown(file_path: str) -> str:
    """把沙箱内 PDF/DOCX/PPT 解析为 markdown。Intake 节点解析作业指导用。

    MCP 工具 schema 要求参数名是 `uri`（且需要 file:/http:/https:/data: URI 形态）。
    形参名保留 file_path 是为了向后兼容（Intake 直调 sandbox_convert_to_markdown(host_path) 不变）。
    内部翻译：host 路径 → /workspace/<rel>（_translate_path）→ 加 file:// 前缀 → uri= 传 MCP。

    返回值：MCP server 实际返回 content 数组形如 [{'type':'text','text':'...'}]；
    本函数把所有 text 拼成一个 str 返回，方便上层（Intake）直接喂给 LLM。
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


async def sandbox_get_packages() -> list[str]:
    """列沙箱内已装 python 包（version 信息可能在 stdout 文本里）。"""
    return await _call("sandbox_get_packages")


# Phase 4 ReAct 循环希望直接 bind_tools 时拿到 LangChain Tool
# 所以提供一个 async helper 返回 raw tool 列表
async def get_sandbox_tools() -> list[Any]:
    """返回 AIO Sandbox 暴露的所有 MCP tools（已归一为 LangChain Tool）。

    每个工具被包了一层 path 翻译：宿主 workspace 路径 → /workspace/...
    """
    tools = await _load_sandbox_tools()
    return [_wrap_tool_with_path_translation(t) for t in tools.values()]
