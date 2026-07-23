"""sandbox_tools - AIO Sandbox MCP 包装

抽象层：Coder 通过 get_sandbox_tools 取得 MCP 工具集（已包装路径翻译）在 ReAct 循环内调用，
Intake 通过 sandbox_convert_to_markdown 解析作业指导文档；未来切 CubeSandbox/E2B 时只换实现层。
当前实现走 langchain-mcp-adapters 拿 MCP tool 后调用。

工具名：
- sandbox_execute_code
- sandbox_execute_bash
- sandbox_file_operations
- sandbox_str_replace_editor
- sandbox_convert_to_markdown
- sandbox_get_packages

路径约定：
- 宿主 WORKSPACE_DIR 通过 -v 挂载到容器 /workspace（见 infra/sandbox_boot.py）
- agent 传入宿主绝对路径时，本模块自动翻译为 /workspace/<rel>
- 已经是 /workspace/... 或相对路径或非 workspace 下的绝对路径 → 原样转发
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


_tools_cache: Optional[dict[str, Any]] = None


# ─── 路径翻译（host → container） ─────────────────────────────────


_SANDBOX_WORKSPACE = "/workspace"


def _translate_path(p: str) -> str:
    """把宿主 workspace 下的绝对路径翻译成容器 /workspace/<rel>。

    其他形态（相对路径 / 已经是 /workspace/... / 其他绝对路径）原样返回。
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
        # 仅 workspace 内路径翻译
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


# sandbox_execute_code Jupyter kernel ack-only 提示
_ACK_ONLY_HINT = (
    "\n\n[labhandler] ack-only: Jupyter kernel 异步派发 ack"
    "（stdout/stderr/exit_code 均 null），代码可能尚未执行完毕。"
    "如需同步获取结果，改用 sandbox_execute_bash 执行命令。"
)


def _annotate_ack_only_if_needed(result: Any) -> Any:
    """检测 sandbox_execute_code 返回是否为 Jupyter kernel ack-only 响应。

    ack-only 形态：status=ok 且 stdout/stderr/exit_code 均为 null。
    命中时在 text 块末尾追加提示，引导 LLM 切换至同步执行工具。
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


# ─── 沙箱连续失败检测 ──────────────────────────────────────────────

_SANDBOX_MAX_FAILURES = 3
"""沙箱工具连续失败阈值。超过此阈值后工具返回 `[SANDBOX_UNREACHABLE]` 致命标记，
Coder 节点检测到此标记后终止当前 step 的 ReAct 循环，不再继续重试。"""

_sandbox_failures: dict[str, int] = {}
"""tool_name → 当前连续失败次数（每步开始前由 reset_sandbox_failure_counter() 清空）。"""


def reset_sandbox_failure_counter() -> None:
    """每步开始前调用，重置所有沙箱工具的连续失败计数。"""
    _sandbox_failures.clear()


def _wrap_tool_with_path_translation(orig_tool: Any) -> Any:
    """构造 StructuredTool，调用前对 path/file_path 形参执行 _translate_path。

    不直接修改原 tool 的原因：langchain-mcp-adapters 返回的 BaseTool 基于
    Pydantic v2 BaseModel，不允许对非 field 赋值。因此通过工厂法新建
    StructuredTool，复用原 tool 的 name/description/args_schema，
    coroutine 中完成路径翻译后转发至原 tool 的 ainvoke。
    """
    from langchain_core.tools import StructuredTool

    async def acall(**kwargs: Any) -> Any:
        for k in list(kwargs.keys()):
            if k in _PATH_KW and isinstance(kwargs[k], str):
                kwargs[k] = _translate_path(kwargs[k])
        try:
            result = await orig_tool.ainvoke(kwargs)
        except Exception as e:
            tool_name = orig_tool.name
            _sandbox_failures[tool_name] = _sandbox_failures.get(tool_name, 0) + 1
            count = _sandbox_failures[tool_name]
            if count >= _SANDBOX_MAX_FAILURES:
                return (
                    f"[SANDBOX_UNREACHABLE] 沙箱工具 {tool_name} 连续 {count} 次失败"
                    f"（{type(e).__name__}: {e}），沙箱可能已不可用，终止当前 step"
                )
            return f"[tool_error] {type(e).__name__}: {e}"
        # 成功后重置计数器
        _sandbox_failures[orig_tool.name] = 0
        # sandbox_execute_code 走 Jupyter kernel 异步派发，可能仅返回 ack。
        # 命中时追加提示，引导 LLM 切换至同步执行工具。
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
    """统一调用入口：拿到 tool 后 ainvoke。"""
    tools = await _load_sandbox_tools()
    if tool_name not in tools:
        raise RuntimeError(
            f"沙箱工具 {tool_name} 未在 MCP server 暴露的工具列表里"
            f"（已知：{list(tools.keys())[:5]}...）"
        )
    # 翻译 path 形参（与 _wrap_tool_with_path_translation 逻辑一致）
    for k in list(kwargs.keys()):
        if k in _PATH_KW and isinstance(kwargs[k], str):
            kwargs[k] = _translate_path(kwargs[k])
    return await tools[tool_name].ainvoke(kwargs)


# ─── 抽象接口 ───────────────────────────


async def sandbox_convert_to_markdown(file_path: str) -> str:
    """将沙箱内 PDF/DOCX/PPT 解析为 markdown，供 Intake 节点解析作业指导。

    形参名保留 file_path 以兼容既有调用方；内部翻译为容器路径并加 file:// 前缀，
    以 uri= 传入 MCP 工具。返回值将 MCP content 数组中所有 text 拼合为单个字符串。
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


# ReAct 循环通过 bind_tools 需要 LangChain Tool 实例，
# 此接口返回已包装路径翻译的 tool 列表。
async def get_sandbox_tools() -> list[Any]:
    """返回 AIO Sandbox 暴露的所有 MCP tools（已包装为 LangChain Tool）。

    每个工具包含路径翻译层：宿主 workspace 路径 → /workspace/...
    """
    tools = await _load_sandbox_tools()
    return [_wrap_tool_with_path_translation(t) for t in tools.values()]
