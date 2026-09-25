"""任务外打分。隐藏测试只在评分目录里出现，不写回 agent 的 workspace。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def hidden_passed(workspace: Path, hidden: Path, *, timeout: float = 60) -> tuple[bool, bool, str]:
    """返回 (passed, infra, log)。infra 表示 pytest 没把用例跑起来。"""
    grade = workspace.parent / "_grade"
    if grade.exists():
        shutil.rmtree(grade)
    shutil.copytree(workspace, grade)
    dest = grade / "_hidden"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(hidden, dest)
    return _pytest(grade, "_hidden", timeout=timeout)


def smoke_passed(workspace: Path, *, timeout: float = 60) -> tuple[bool, bool, str]:
    smoke = workspace / "smoke"
    if not smoke.is_dir():
        return False, True, "no smoke dir"
    return _pytest(workspace, "smoke", timeout=timeout)


def _pytest(root: Path, target: str, *, timeout: float) -> tuple[bool, bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=line", target],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return False, True, f"[timeout] {exc}"
    log = (proc.stdout or "") + (proc.stderr or "")
    lowered = log.lower()
    infra = (
        "no module named pytest" in lowered
        or "internalerror" in lowered
        or proc.returncode < 0
    )
    return proc.returncode == 0, infra, log[-4000:]
