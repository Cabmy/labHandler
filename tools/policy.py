"""host 端工具的 PreToolUse 策略与实时审计。

SecurityPolicy 是 host 路径边界、host_bash 命令白名单、路径逃逸正则
的唯一实现。检查失败抛 PermissionError；fs_tools 译成
[ERROR/PermissionError] 观测供 ReAct 自纠正。危险命令到不了 subprocess.run。

ToolAuditor 把每次调用追加一行 JSONL 到
workspace/.labhandler/audit.jsonl。审计失败静默，不阻断工具。

不变量：/etc/passwd、../../etc、cd .. && ls、~ 展开一律拒绝。
回归见 AGENTS.md 安全边界节。
"""

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

    # host_bash 白名单：只放行下列基础命令；不在集合内的命令名直接拒绝。
    # 路径逃逸正则是第二道；cwd=WORKSPACE_DIR 是第三道。
    #
    # bash / sh / docker 不在白名单：载荷是字符串，逃逸正则挡不住
    # `bash -c` 里编码过的路径；docker 还能把宿主机挂进容器。
    # 复杂 shell 走 sandbox_execute_bash（容器隔离）。
    ALLOWED_COMMANDS = frozenset({
        'pytest', 'python', 'python3', 'ls', 'mkdir', 'rm', 'cp', 'mv',
        'cat', 'echo', 'git', 'pip', 'pip3', 'chmod', 'touch',
        'head', 'tail', 'wc', 'sort', 'uniq', 'diff', 'which',
        'ln', 'find', 'grep', 'sed', 'awk', 'tree',
    })

    # 路径逃逸防护（不论是否在白名单都检查）
    # 边界字符含引号/括号/等号，以便拦截藏在字符串里的绝对路径
    # （如 python -c "open('/etc/passwd')"）：只认空白前缀的话，
    # 带引号的绝对路径和 .. 穿越都能绕过。
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

    def check_glob(self, pattern: str) -> str:
        """glob/grep 的 pattern 校验：必须相对 workspace，禁止 /、~、..。

        glob/grep 不走 safe_path；pathlib 会展开 `../`，`root.glob('../**/*')`
        能列出 workspace 外文件。非法 pattern 抛 PermissionError。
        """
        if not isinstance(pattern, str) or not pattern.strip():
            raise PermissionError(f"Illegal glob pattern: {pattern!r}")
        if pattern.startswith("/") or pattern.startswith("~"):
            raise PermissionError(
                f"glob pattern must be relative to workspace: {pattern!r}"
            )
        if ".." in Path(pattern).parts:
            raise PermissionError(
                f"glob pattern may not traverse outside workspace: {pattern!r}"
            )
        return pattern

    def contains(self, path: Path) -> bool:
        """路径是否落在 workspace 内。用于过滤 glob 结果，不抛异常。"""
        try:
            path.resolve().relative_to(self.workspace_dir)
        except (ValueError, OSError):
            return False
        return True

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
