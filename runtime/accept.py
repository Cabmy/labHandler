"""程序性验收：acceptance/<task_id>/*.py 在沙箱内跑 pytest，给出四态结论。

测试只在沙箱容器里执行。沙箱不可达或 pytest 自身崩溃 → test_invalid
（Judge 语义：门禁没跑起来，不据此惩罚 Flash）。没有 .py 文件 →
no_hard_criteria。collect-only 失败 → test_invalid。跑通且 exit 0 → pass，
否则 fail。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from config.runtime import RuntimeSettings
from tools.policy import get_policy
from tools.sandbox_tools import sandbox_workspace_path, sandbox_run

PASS = "pass"
FAIL = "fail"
TEST_INVALID = "test_invalid"
NO_HARD_CRITERIA = "no_hard_criteria"

_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors)")
_LOG_CAP = 4000


@dataclass
class AcceptResult:
    state: str
    passed: int = 0
    failed: int = 0
    exit_code: int = 0
    log: str = ""

    @property
    def is_pass(self) -> bool:
        return self.state == PASS

    def as_tests(self) -> dict:
        return {
            "state": self.state,
            "passed": self.passed,
            "failed": self.failed,
            "exit_code": self.exit_code,
            "log": self.log[-_LOG_CAP:],
        }


def acceptance_dir(session_dir: Path, task_id: str) -> Path:
    return session_dir / "acceptance" / task_id


def write_acceptance_file(
    session_dir: Path, task_id: str, filename: str, content: str, *, role: str
) -> Path:
    """写一个验收测试文件。role 必须是调用方的真实角色。

    acceptance/ 下的内容决定门禁结论，只有 Pro 能写；用字面量 "pro" 调用
    check_write 等于让这道检查自我批准。
    """
    name = Path(filename).name
    if not name.endswith(".py"):
        raise ValueError(f"acceptance file must be a .py test file, got {filename!r}")
    dest = acceptance_dir(session_dir, task_id)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    policy = get_policy()
    policy.check_write(str(path.relative_to(policy.workspace_dir)), role)
    path.write_text(content, encoding="utf-8")
    return path


def parse_pytest_counts(text: str) -> tuple[int, int]:
    """从 pytest 摘要行解析通过/失败数。error 也计入失败。"""
    passed = failed = 0
    for count, kind in _COUNT_RE.findall(text):
        n = int(count)
        if kind == "passed":
            passed = n
        else:
            failed += n
    return passed, failed


async def _pytest(target: str, extra: list[str], timeout: float) -> tuple[int, str]:
    """在沙箱里跑 pytest。返回 (exit_code, 合并日志)。"""
    args = " ".join(extra)
    command = f"cd /workspace && python -m pytest -q --tb=short {args} {target}".strip()
    return await sandbox_run(command, timeout=timeout)


async def sanity_and_run(
    session_dir: Path, task_id: str, *, settings: RuntimeSettings
) -> AcceptResult:
    """四态：no_hard_criteria / test_invalid / pass / fail。"""
    root = acceptance_dir(session_dir, task_id)
    if not root.is_dir() or not any(root.glob("*.py")):
        return AcceptResult(
            state=NO_HARD_CRITERIA,
            log="acceptance/ 下没有 pytest 文件，本节点没有程序性门禁",
        )

    try:
        target = sandbox_workspace_path(root)
    except ValueError as e:
        return AcceptResult(state=TEST_INVALID, exit_code=-1, log=f"验收目录不在 workspace 内：{e}")

    timeout = settings.tool_timeout_s

    code, log = await _pytest(target, ["--collect-only"], timeout)
    if code != 0:
        return AcceptResult(state=TEST_INVALID, exit_code=code, log=log)

    code, log = await _pytest(target, [], timeout)
    if code != 0 and _is_infra_failure(log):
        return AcceptResult(state=TEST_INVALID, exit_code=code, log=log)

    passed, failed = parse_pytest_counts(log)
    if code == 0:
        return AcceptResult(state=PASS, passed=passed, failed=0, exit_code=0, log=log)
    return AcceptResult(
        state=FAIL, passed=passed, failed=max(failed, 1), exit_code=code, log=log
    )


_INFRA_MARKERS = (
    "[sandbox_unreachable]",
    "[timeout",
    "internalerror>",      # pytest 自身崩溃，不是被测代码的问题
    "no such file or directory",
)


def _is_infra_failure(log: str) -> bool:
    """区分「测试判定失败」和「门禁根本没跑起来」。"""
    lowered = log.lower()
    return any(marker in lowered for marker in _INFRA_MARKERS)
