"""policy - PreToolUse 安全策略层 + 工具审计。

把 host 端工具的安全校验从 fs_tools 收拢到独立策略类，职责分离：
- SecurityPolicy：命令白名单 / 路径逃逸正则 / workspace 路径边界 三道防线的唯一实现。
  校验失败抛 PermissionError（由工具层翻译成 [ERROR/PermissionError] 观测让 ReAct 自纠正，
  危险命令**不会**到达 subprocess.run）。
- ToolAuditor：每次工具调用实时 append 一行 JSONL 到 workspace/.labhandler/audit.jsonl
  （实时审计点，替代事后从 messages 抠轨迹）。审计失败静默——审计不能反过来弄挂工具。

四种典型越界（/etc/passwd、../../etc、cd .. && ls、~ 展开）必须始终被拒绝，
回归用例见 AGENTS.md 安全边界节。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from config.runtime import get_settings


class SecurityPolicy:
    """host 端工具安全策略：路径边界 + 命令白名单 + 逃逸正则。"""

    # host_bash 命令白名单：只允许以下基础命令
    # 原则：白名单优先，命令名不在白名单中直接拒绝；
    # 路径逃逸模式作为第二道防线（cwd=WORKSPACE_DIR 是第三道）
    ALLOWED_COMMANDS = frozenset({
        'pytest', 'python', 'python3', 'ls', 'mkdir', 'rm', 'cp', 'mv',
        'cat', 'echo', 'git', 'pip', 'pip3', 'conda', 'chmod', 'touch',
        'head', 'tail', 'wc', 'sort', 'uniq', 'diff', 'which', 'type',
        'docker', 'ln', 'find', 'grep', 'sed', 'awk', 'tree', 'env',
        'python2', 'pypy', 'bash', 'sh',
    })

    # 路径逃逸防护（无论命令是否在白名单中，都检查逃逸模式）
    # 边界类含引号/括号/等号：拦截藏在字符串里的绝对路径（如 python -c "open('/etc/passwd')"），
    # 不再仅靠"空白前缀"，避免命令用引号包裹绝对路径 / .. 回溯绕过检测。
    _BOUNDARY = r"(^|[\s'\"=(])"
    ESCAPE_PATTERNS = [
        re.compile(_BOUNDARY + r"\.\.([/\\\s'\")]|$)"),  # 独立 .. token / 路径回溯（含引号、括号内）
        re.compile(_BOUNDARY + r"/[a-zA-Z]"),            # / 开头的绝对路径（cat /etc/x、open('/etc/x')）
        re.compile(r"(?<![\w.])/(?:etc|usr|var|root|proc|sys|bin|sbin|boot|dev|lib|opt|home)(?:/|\b)"),  # 系统绝对路径（任意前缀，如 +/etc；相对子目录 foo/etc 不误伤）
        re.compile(_BOUNDARY + r"~/"),                   # ~/path 家目录展开
        re.compile(_BOUNDARY + r"~($|[\s'\")])"),        # 单独 ~ token
    ]

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def safe_path(self, p: str) -> Path:
        """把入参 path 解析后校验是否在 workspace_dir 之内；越界抛 PermissionError。"""
        if not isinstance(p, str) or not p:
            raise PermissionError(f"非法路径：{p!r}")
        candidate = (self.workspace_dir / p) if not Path(p).is_absolute() else Path(p)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError as e:
            raise PermissionError(
                f"越权路径：{p!r} 解析为 {resolved}，不在 {self.workspace_dir} 之下"
            ) from e
        return resolved

    @staticmethod
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

    def check_command(self, cmd: str) -> None:
        """host_bash cmd 字符串白名单预检 + 路径逃逸防护；违规抛 PermissionError。"""
        if not isinstance(cmd, str) or not cmd.strip():
            raise PermissionError(f"非法命令：{cmd!r}")

        for name in self._extract_cmd_names(cmd):
            if name not in self.ALLOWED_COMMANDS:
                raise PermissionError(
                    f"host_bash 命令不在白名单中：{name!r}"
                    f"（允许的命令：{sorted(self.ALLOWED_COMMANDS)}）"
                )

        for pat in self.ESCAPE_PATTERNS:
            if pat.search(cmd):
                raise PermissionError(
                    f"host_bash 命令包含路径逃逸：{cmd!r}（pattern={pat.pattern}）"
                )


class ToolAuditor:
    """工具调用实时审计：append JSONL 到 workspace/.labhandler/audit.jsonl。"""

    _MAX_ARG_CHARS = 300

    def __init__(self, audit_path: Path) -> None:
        self.audit_path = audit_path

    def record(self, tool: str, args: dict[str, Any], outcome: str) -> None:
        """记一条审计（outcome ∈ ok | denied:<原因> | error:<异常>）。失败静默。"""
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
            pass  # 审计不能反过来弄挂工具调用


# ─── 模块级单例 ────────────────────────────────────────────────────

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
