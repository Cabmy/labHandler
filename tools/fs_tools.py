"""fs_tools - 负责宿主机工作空间 (Workspace) 的文件操作与受限命令执行。

该模块提供了在宿主环境下安全操作作业文件的工具集。安全校验统一委托
`tools/policy.py:SecurityPolicy`（P2-2 策略层）：

1. 路径沙箱化：所有路径参数经 `policy.safe_path` 解析，强制位于 `WORKSPACE_DIR` 内。
2. 命令白名单：`host_bash` 执行前经 `policy.check_command` 白名单校验 + 路径逃逸正则检查，
   禁止 `..` 回溯、`~` 展开及 `/` 开头的绝对路径访问。
3. 超时与权限隔离：Bash 命令 cwd 锁定 workspace，硬性超时（默认 30s）。
4. 错误自纠正：越权操作被拦截后返回 `[ERROR/PermissionError]` 结构化提示，
   引导 ReAct Agent 改用合法相对路径或转沙箱执行——危险命令不会到达 subprocess.run。
5. 实时审计：每次调用（含被拒绝的）由 `ToolAuditor` append 到
   workspace/.labhandler/audit.jsonl。

核心功能：
- read_file / write_file / patch_file：对工作空间内的文件进行读、写及局部修补。
- list_dir：查看工作空间目录结构。
- host_bash：在宿主端执行安全的 shell 命令（如 pytest、代码扫描等）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import tool
from config.runtime import get_settings
from tools.policy import get_auditor, get_policy

WORKSPACE_DIR: Path = get_settings().workspace_dir
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


# Coder 看到 [ERROR/PermissionError] 后的统一改写提示（作为 ToolMessage observation）
_PERM_HINT = (
    "host fs 工具只能在 WORKSPACE_DIR 内运行；host_bash 仅允许白名单内命令（pytest、python、git 等），"
    "且禁用 ..、绝对路径前缀、~ 展开。"
    "请改用相对路径（如 'solution.py' 而非 '/workspace/solution.py'，"
    "`pytest -q test_x.py` 而非 `cd /workspace && pytest`），"
    "或改用 sandbox_run_python / sandbox_file_operations 在容器内访问 /workspace/*。"
)


def _perm_msg(e: PermissionError) -> str:
    """把 PermissionError 翻译成给 LLM 看的观测字符串（ReAct 自我纠正用）"""
    return f"[ERROR/PermissionError] {e}\n提示：{_PERM_HINT}"


def _denied(tool_name: str, args: dict, e: PermissionError) -> str:
    """越权统一出口：审计 denied + 返回自纠正观测。"""
    get_auditor().record(tool_name, args, f"denied:{e}")
    return _perm_msg(e)


# ─── 工具（@tool 装饰器自动产 OpenAI schema） ─────────────────────


@tool
def read_file(path: str) -> str:
    """读 workspace 内的文本文件，返回全文。path 是相对 WORKSPACE_DIR 的路径。"""
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("read_file", {"path": path}, e)
    if not p.exists():
        get_auditor().record("read_file", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"文件不存在：{path}")
    if not p.is_file():
        get_auditor().record("read_file", {"path": path}, "error:IsADirectoryError")
        raise IsADirectoryError(f"不是文件：{path}")
    get_auditor().record("read_file", {"path": path}, "ok")
    return p.read_text(encoding="utf-8")


@tool
def write_file(path: str, content: str) -> str:
    """写 workspace 内文本文件（覆盖写）。path 是相对 WORKSPACE_DIR 的路径。返回字节数描述。"""
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("write_file", {"path": path}, e)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    get_auditor().record("write_file", {"path": path, "n_chars": len(content)}, "ok")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


@tool
def list_dir(path: str = ".") -> list[str] | str:
    """列 workspace 内目录的文件名（一级，不递归）。越界时返回 [ERROR/PermissionError] 字符串。"""
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("list_dir", {"path": path}, e)
    if not p.exists():
        get_auditor().record("list_dir", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"目录不存在：{path}")
    if not p.is_dir():
        get_auditor().record("list_dir", {"path": path}, "error:NotADirectoryError")
        raise NotADirectoryError(f"不是目录：{path}")
    get_auditor().record("list_dir", {"path": path}, "ok")
    return sorted(x.name for x in p.iterdir())


@tool
def patch_file(path: str, old: str, new: str) -> str:
    """在 workspace 内文件做精确字符串替换（old 必须在文件中出现一次，否则报错）。"""
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("patch_file", {"path": path}, e)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        get_auditor().record("patch_file", {"path": path}, "error:old_not_found")
        raise ValueError(f"patch_file: old 字符串未找到 in {path}")
    if count > 1:
        get_auditor().record("patch_file", {"path": path}, f"error:old_x{count}")
        raise ValueError(f"patch_file: old 字符串在 {path} 出现 {count} 次（必须唯一）")
    new_text = text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    get_auditor().record("patch_file", {"path": path}, "ok")
    return f"patched {path} (1 occurrence)"


@tool
def host_bash(cmd: str, timeout: int = 30) -> str:
    """在宿主 workspace 目录下执行 bash 命令（cwd=WORKSPACE_DIR）。

    安全约束：cmd 命令名白名单校验（只允许 pytest / python / git / ls 等预定义命令）；
    路径逃逸正则检查（拒 ..、绝对路径前缀、~ 展开等）；cwd 强制为 WORKSPACE_DIR；timeout 默认 30s。

    用例：`pytest -v` / `ls -la` / `python solution.py`
    返回 stdout + stderr 合并文本；越界时**命令不会被执行**，返回
    `[ERROR/PermissionError] ...` 字符串，请改写命令后重试（去掉绝对路径前缀、
    使用相对路径，或改用 sandbox_run_python）。
    """
    try:
        get_policy().check_command(cmd)
    except PermissionError as e:
        return _denied("host_bash", {"cmd": cmd}, e)
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        get_auditor().record("host_bash", {"cmd": cmd}, f"error:timeout_{timeout}s")
        return f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}"
    get_auditor().record("host_bash", {"cmd": cmd}, f"ok:exit={result.returncode}")
    out = result.stdout or ""
    err = result.stderr or ""
    tail = f"\n[exit={result.returncode}]"
    if err:
        return f"{out}\n--- stderr ---\n{err}{tail}"
    return out + tail
