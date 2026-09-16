"""policy - PreToolUse 安全策略层 + 工具审计。

将 fs_tools 中 host 端工具安全检查收拢为独立的策略类，
以分离关注点：
- SecurityPolicy：命令白名单 / 路径逃逸正则 / workspace 路径边界
  三道防线的唯一实现。检查失败抛 PermissionError
  （由工具层翻译成 [ERROR/PermissionError] 观测供 ReAct
  自纠正；危险命令永远到不了 subprocess.run）。
- ToolAuditor：每次工具调用实时追加一行 JSONL 到
  workspace/.labhandler/audit.jsonl（实时审计点，取代从 messages
  事后提取轨迹）。审计失败静默——审计不能拖垮工具。

四种典型越界（/etc/passwd、../../etc、cd .. && ls、~ 展开）
必须始终被拒绝；回归测试见 AGENTS.md 安全边界节。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from config.runtime import get_settings

# PermissionError 后给 LLM 的提示（fs_tools 用于 ReAct 自纠正）
PERM_HINT = (
    "host fs tools may only operate inside WORKSPACE_DIR; host_bash allows only whitelisted commands "
    "(pytest, python, git, etc.) and forbids .., absolute-path prefixes, and ~ expansion. "
    "Use relative paths instead (e.g. 'solution.py' rather than '/workspace/solution.py', "
    "`pytest -q test_x.py` rather than `cd /workspace && pytest`), "
    "or use sandbox_run_python / sandbox_file_operations to access /workspace/* inside the container."
)


def _extract_cmd_names(cmd: str) -> list[str]:
    """从命令字符串提取所有基础命令名（处理 | && || ; 链）。"""
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


class SecurityPolicy:
    """host 端工具安全策略：路径边界 + 命令白名单 + 逃逸正则。"""

    # host_bash 命令白名单：仅允许以下基础命令
    # 原则：白名单优先，不在白名单的命令名直接拒绝；
    # 路径逃逸模式作第二道防线（cwd=WORKSPACE_DIR 是第三道）
    ALLOWED_COMMANDS = frozenset({
        'pytest', 'python', 'python3', 'ls', 'mkdir', 'rm', 'cp', 'mv',
        'cat', 'echo', 'git', 'pip', 'pip3', 'conda', 'chmod', 'touch',
        'head', 'tail', 'wc', 'sort', 'uniq', 'diff', 'which', 'type',
        'docker', 'ln', 'find', 'grep', 'sed', 'awk', 'tree', 'env',
        'python2', 'pypy', 'bash', 'sh',
    })

    # 路径逃逸防护（不论是否在白名单都检查）
    # 边界字符含引号/括号/等号：拦截藏在字符串里的绝对路径
    # （如 python -c "open('/etc/passwd')"）；不再仅依赖 "空白前缀"，
    # 避免命令用带引号的绝对路径 / .. 穿越绕过检测。
    _BOUNDARY = r"(^|[\s'\"=(])"
    ESCAPE_PATTERNS = [
        re.compile(_BOUNDARY + r"\.\.([/\\\s'\")]|$)"),  # 独立 .. token / 路径穿越（含引号、括号内）
        re.compile(_BOUNDARY + r"/[a-zA-Z]"),            # / 前缀绝对路径（cat /etc/x、open('/etc/x')）
        re.compile(r"(?<![\w.])/(?:etc|usr|var|root|proc|sys|bin|sbin|boot|dev|lib|opt|home)(?:/|\b)"),  # 系统绝对路径（任意前缀如 +/etc；foo/etc 这类相对子目录不受影响）
        re.compile(_BOUNDARY + r"~/"),                   # ~/path 家目录展开
        re.compile(_BOUNDARY + r"~($|[\s'\")])"),        # 独立 ~ token
    ]

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def safe_path(self, p: str) -> Path:
        """解析输入路径并验证其在 workspace_dir 内；越界抛 PermissionError。"""
        if not isinstance(p, str) or not p:
            raise PermissionError(f"Illegal path: {p!r}")
        candidate = (self.workspace_dir / p) if not Path(p).is_absolute() else Path(p)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError as e:
            raise PermissionError(
                f"Out-of-bounds path: {p!r} resolves to {resolved}, not under {self.workspace_dir}"
            ) from e
        return resolved

    def check_write(self, p: str, role: str) -> Path:
        """写路径检查：workspace 边界 + acceptance/ 仅 Pro + session 文件仅 Pro。"""
        resolved = self.safe_path(p)
        rel = resolved.relative_to(self.workspace_dir)
        parts = rel.parts
        if role != "pro" and "acceptance" in parts:
            raise PermissionError(
                f"acceptance/ is Pro-only write; role={role!r} cannot write {p!r}"
            )
        if role != "pro" and ".labhandler" in parts and "scripts" not in parts:
            raise PermissionError(
                f"session files under .labhandler/ are Pro/harness-only; role={role!r} cannot write {p!r}"
            )
        return resolved

    def check_command(self, cmd: str) -> None:
        """host_bash cmd 字符串白名单预检 + 路径逃逸巡查；抛 PermissionError。"""
        if not isinstance(cmd, str) or not cmd.strip():
            raise PermissionError(f"Illegal command: {cmd!r}")

        for name in _extract_cmd_names(cmd):
            if name not in self.ALLOWED_COMMANDS:
                raise PermissionError(
                    f"host_bash command not in whitelist: {name!r} "
                    f"(allowed commands: {sorted(self.ALLOWED_COMMANDS)})"
                )

        for pat in self.ESCAPE_PATTERNS:
            if pat.search(cmd):
                raise PermissionError(
                    f"host_bash command contains path escape: {cmd!r} (pattern={pat.pattern})"
                )


class ToolAuditor:
    """实时工具调用审计器：向 workspace/.labhandler/audit.jsonl 追加 JSONL。"""

    _MAX_ARG_CHARS = 300

    def __init__(self, audit_path: Path) -> None:
        self.audit_path = audit_path

    def record(self, tool: str, args: dict[str, Any], outcome: str) -> None:
        """记录一条审计（outcome 取 ok | denied:<reason> | error:<exception>）。失败静默。"""
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool,
                "args": {
                    k: (v if len(s := str(v)) <= self._MAX_ARG_CHARS
                        else s[: self._MAX_ARG_CHARS] + "…")
                    for k, v in args.items()
                },
                "outcome": outcome[:500],
            }
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # 审计不能拖垮工具调用


# ─── 模块级单例 ─────────────────────────────────────────

_policy: SecurityPolicy | None = None
_auditor: ToolAuditor | None = None


def get_policy() -> SecurityPolicy:
    global _policy
    if _policy is None:
        _policy = SecurityPolicy(get_settings().workspace_dir)
    return _policy


def get_auditor() -> ToolAuditor:
    global _auditor
    if _auditor is None:
        _auditor = ToolAuditor(
            get_settings().workspace_dir / ".labhandler" / "audit.jsonl"
        )
    return _auditor
