"""fs_tools - 负责宿主机工作空间 (Workspace) 的文件操作与受限命令执行。

该模块提供了在宿主环境下安全操作作业文件的工具集，并通过严格的安全边界机制防止非预期的系统越权。

安全保障机制：
1. 路径沙箱化：所有路径参数均通过 `_safe_path` 解析，强制要求必须位于 `WORKSPACE_DIR` 范围内。
2. 命令白名单：`host_bash` 在执行前会进行命令名白名单校验和路径逃逸正则检查，只允许预定义的基础命令集，
   并禁止包含 `..` 路径回溯、`~` 家目录展开及以 `/` 开头的绝对路径访问，确保命令执行被锁定在工作空间。
3. 超时与权限隔离：所有 Bash 命令均在指定的工作目录中运行，并设有硬性超时限制（30秒）。
4. 错误自纠正：当探测到越权操作时，工具层会拦截异常并返回结构化的错误提示，引导 LLM Agent 
   自动切换为合法的相对路径或转移至沙箱环境执行。

核心功能：
- read_file / write_file / patch_file：对工作空间内的文件进行读、写及局部修补。
- list_dir：查看工作空间目录结构。
- host_bash：在宿主端执行安全的 shell 命令（如 pytest、代码扫描等）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from langchain_core.tools import tool
from config.runtime import get_settings

WORKSPACE_DIR: Path = get_settings().workspace_dir
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# host_bash 命令白名单：只允许以下基础命令
# 原则：白名单优先，命令名不在白名单中直接拒绝；
# 路径逃逸模式作为第二道防线（cwd=WORKSPACE_DIR 是第三道）
_ALLOWED_COMMANDS = frozenset({
    'pytest', 'python', 'python3', 'ls', 'mkdir', 'rm', 'cp', 'mv',
    'cat', 'echo', 'git', 'pip', 'pip3', 'conda', 'chmod', 'touch',
    'head', 'tail', 'wc', 'sort', 'uniq', 'diff', 'which', 'type',
    'docker', 'ln', 'find', 'grep', 'sed', 'awk', 'tree', 'env',
    'python2', 'pypy', 'bash', 'sh',
})

# 路径逃逸防护（无论命令是否在白名单中，都检查逃逸模式）
_ESCAPE_PATTERNS = [
    re.compile(r"(^|\s)\.\.([/\\\s]|$)"),   # 独立 .. token / ../ / ..\\ / ..<EOL>
    re.compile(r"(^|\s)/[a-zA-Z]"),         # 空白后跟 / 开头的绝对路径（cat /etc/x）
    re.compile(r"(^|\s|=)~/"),              # ~/path 家目录展开
    re.compile(r"(^|\s)~($|\s)"),           # 单独 ~ token
]


def _safe_path(p: str) -> Path:
    """把入参 path 解析后校验是否在 WORKSPACE_DIR 之内"""
    if not isinstance(p, str) or not p:
        raise PermissionError(f"非法路径：{p!r}")
    candidate = (WORKSPACE_DIR / p) if not Path(p).is_absolute() else Path(p)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR)
    except ValueError as e:
        raise PermissionError(
            f"越权路径：{p!r} 解析为 {resolved}，不在 {WORKSPACE_DIR} 之下"
        ) from e
    return resolved


def _extract_cmd_names(cmd: str) -> list[str]:
    """从命令字符串中提取所有基础命令名（处理 | && || ; 链式调用）。"""
    names: list[str] = []
    for segment in re.split(r'\s*&&\s*|\s*\|\|\s*|\s*\|\s*|\s*;\s*', cmd):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split()
        if not parts:
            continue
        name = parts[0].lstrip('./')
        if name:
            names.append(name)
    return names


def _check_cmd(cmd: str) -> None:
    """host_bash cmd 字符串白名单预检 + 路径逃逸防护"""
    if not isinstance(cmd, str) or not cmd.strip():
        raise PermissionError(f"非法命令：{cmd!r}")

    cmd_names = _extract_cmd_names(cmd)
    for name in cmd_names:
        if name not in _ALLOWED_COMMANDS:
            raise PermissionError(
                f"host_bash 命令不在白名单中：{name!r}"
                f"（允许的命令：{sorted(_ALLOWED_COMMANDS)}）"
            )

    for pat in _ESCAPE_PATTERNS:
        if pat.search(cmd):
            raise PermissionError(
                f"host_bash 命令包含路径逃逸：{cmd!r}（pattern={pat.pattern}）"
            )


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


# ─── 工具（@tool 装饰器自动产 OpenAI schema） ─────────────────────


@tool
def read_file(path: str) -> str:
    """读 workspace 内的文本文件，返回全文。path 是相对 WORKSPACE_DIR 的路径。"""
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return _perm_msg(e)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if not p.is_file():
        raise IsADirectoryError(f"不是文件：{path}")
    return p.read_text(encoding="utf-8")


@tool
def write_file(path: str, content: str) -> str:
    """写 workspace 内文本文件（覆盖写）。path 是相对 WORKSPACE_DIR 的路径。返回字节数描述。"""
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return _perm_msg(e)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


@tool
def list_dir(path: str = ".") -> list[str] | str:
    """列 workspace 内目录的文件名（一级，不递归）。越界时返回 [ERROR/PermissionError] 字符串。"""
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return _perm_msg(e)
    if not p.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if not p.is_dir():
        raise NotADirectoryError(f"不是目录：{path}")
    return sorted(x.name for x in p.iterdir())


@tool
def patch_file(path: str, old: str, new: str) -> str:
    """在 workspace 内文件做精确字符串替换（old 必须在文件中出现一次，否则报错）。"""
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return _perm_msg(e)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ValueError(f"patch_file: old 字符串未找到 in {path}")
    if count > 1:
        raise ValueError(f"patch_file: old 字符串在 {path} 出现 {count} 次（必须唯一）")
    new_text = text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
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
        _check_cmd(cmd)
    except PermissionError as e:
        return _perm_msg(e)
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}"
    out = result.stdout or ""
    err = result.stderr or ""
    tail = f"\n[exit={result.returncode}]"
    if err:
        return f"{out}\n--- stderr ---\n{err}{tail}"
    return out + tail


# 暴露工具列表给 registry
FS_TOOLS = [read_file, write_file, list_dir, patch_file, host_bash]
