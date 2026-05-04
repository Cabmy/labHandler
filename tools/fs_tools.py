"""fs_tools - host workspace 文件操作 + host_bash

**安全边界（PLAN §14 / STEPS P3.1）**：
1. 所有路径入参先 `_safe_path(p)` 解析 + `is_relative_to(WORKSPACE_DIR)` 校验
2. host_bash 用 `subprocess.run(["bash","-c",cmd], cwd=WORKSPACE_DIR, timeout=30)`
3. host_bash 命令字符串 regex 黑名单：
   - 含 `..` token（`/^|\s\.\.[/\\\s]` 形式）→ 拒
   - 含 `~` 展开 → 拒
   - 含以 `/` 开头的可能绝对路径（如 `cat /etc/passwd`）→ 拒
4. 越界一律 raise PermissionError（不静默）

**越权 4 用例（DoD）**：
  ① read_file("/etc/passwd")          → PermissionError
  ② read_file("../../etc/passwd")     → PermissionError
  ③ host_bash("cat /etc/passwd")      → PermissionError
  ④ host_bash("cd .. && ls")          → PermissionError
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from langchain_core.tools import tool

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# host_bash 命令黑名单（regex）
# 原则：粗粒度拒绝；越界靠 cwd=WORKSPACE_DIR 兜底，黑名单防止 cmd 字符串显式越界
_BAD_CMD_PATTERNS = [
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


def _check_cmd(cmd: str) -> None:
    """host_bash cmd 字符串 regex 黑名单预检"""
    if not isinstance(cmd, str) or not cmd.strip():
        raise PermissionError(f"非法命令：{cmd!r}")
    for pat in _BAD_CMD_PATTERNS:
        if pat.search(cmd):
            raise PermissionError(
                f"host_bash 命令命中越界黑名单：{cmd!r}（pattern={pat.pattern}）"
            )


# ─── 工具（@tool 装饰器自动产 OpenAI schema） ─────────────────────


@tool
def read_file(path: str) -> str:
    """读 workspace 内的文本文件，返回全文。path 是相对 WORKSPACE_DIR 的路径。"""
    p = _safe_path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if not p.is_file():
        raise IsADirectoryError(f"不是文件：{path}")
    return p.read_text(encoding="utf-8")


@tool
def write_file(path: str, content: str) -> str:
    """写 workspace 内文本文件（覆盖写）。path 是相对 WORKSPACE_DIR 的路径。返回字节数描述。"""
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


@tool
def list_dir(path: str = ".") -> list[str]:
    """列 workspace 内目录的文件名（一级，不递归）。"""
    p = _safe_path(path)
    if not p.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if not p.is_dir():
        raise NotADirectoryError(f"不是目录：{path}")
    return sorted(x.name for x in p.iterdir())


@tool
def patch_file(path: str, old: str, new: str) -> str:
    """在 workspace 内文件做精确字符串替换（old 必须在文件中出现一次，否则报错）。"""
    p = _safe_path(path)
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

    安全约束：cmd 字符串预检黑名单（拒 ..、绝对路径前缀、~ 展开等）；
    cwd 强制为 WORKSPACE_DIR；timeout 默认 30s。

    用例：`pytest -v` / `ls -la` / `python solution.py`
    返回 stdout + stderr 合并文本。
    """
    _check_cmd(cmd)
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
