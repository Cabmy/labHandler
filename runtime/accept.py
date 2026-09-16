"""验收：落盘、sanity collect、Judge 前重跑、四态结果。"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config.runtime import RuntimeSettings
from tools.policy import get_policy


@dataclass
class AcceptResult:
    state: str  # pass | fail | test_invalid | no_hard_criteria
    passed: int = 0
    failed: int = 0
    exit_code: int = 0
    log: str = ""

    def as_tests(self) -> dict:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "exit_code": self.exit_code,
            "log": self.log[-4000:],
        }


def acceptance_dir(session_dir: Path, task_id: str) -> Path:
    return session_dir / "acceptance" / task_id


def write_acceptance_file(
    session_dir: Path, task_id: str, filename: str, content: str
) -> Path:
    name = Path(filename).name
    dest = acceptance_dir(session_dir, task_id)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    # Pro-only: still go through policy if path is under workspace
    get_policy().check_write(str(path.relative_to(get_policy().workspace_dir)), "pro")
    path.write_text(content, encoding="utf-8")
    return path


def _parse_pytest_counts(text: str) -> tuple[int, int]:
    passed = failed = 0
    for token in text.replace(",", " ").split():
        if token.endswith("passed"):
            try:
                passed = int(token.replace("passed", "").strip() or "0")
            except ValueError:
                pass
        if token.endswith("failed"):
            try:
                failed = int(token.replace("failed", "").strip() or "0")
            except ValueError:
                pass
    return passed, failed


async def _run_pytest(path: Path, extra: list[str]) -> subprocess.CompletedProcess[str]:
    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["pytest", "-q", "--tb=short", *extra, str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    return await asyncio.to_thread(_run)


async def sanity_and_run(session_dir: Path, task_id: str) -> AcceptResult:
    root = acceptance_dir(session_dir, task_id)
    if not root.exists() or not any(root.glob("*.py")):
        return AcceptResult(
            state="no_hard_criteria",
            log="no pytest files under acceptance/; no programmatic gate",
        )
    collect = await _run_pytest(root, ["--collect-only"])
    log = (collect.stdout or "") + "\n" + (collect.stderr or "")
    if collect.returncode != 0:
        return AcceptResult(
            state="test_invalid",
            exit_code=collect.returncode,
            log=log,
        )
    run = await _run_pytest(root, [])
    out = (run.stdout or "") + "\n" + (run.stderr or "")
    passed, failed = _parse_pytest_counts(out)
    if run.returncode == 0:
        return AcceptResult(state="pass", passed=passed, failed=0, exit_code=0, log=out)
    return AcceptResult(
        state="fail",
        passed=passed,
        failed=max(failed, 1),
        exit_code=run.returncode,
        log=out,
    )
