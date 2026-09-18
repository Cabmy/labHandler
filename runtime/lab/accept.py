"""程序性验收：acceptance/<task_id>/*.py 在沙箱内跑 pytest，给出四态结论。

测试只在沙箱容器里执行。沙箱不可达或 pytest 自身崩溃 → test_invalid
（Judge 语义：门禁没跑起来，不据此惩罚 Flash）。没有 .py 文件 →
no_hard_criteria。collect-only 失败 → test_invalid。跑通且 exit 0 → pass，
否则 fail。
"""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from config.runtime import RuntimeSettings
from tools.policy import get_policy
from tools.sandbox_tools import is_sandbox_unreachable, sandbox_workspace_path, sandbox_run

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


def has_gate(session_dir: Path, task_id: str) -> bool:
    root = acceptance_dir(session_dir, task_id)
    return root.is_dir() and any(root.glob("*.py"))


def _existing_gate_ids(session_dir: Path) -> list[str]:
    root = session_dir / "acceptance"
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and any(p.glob("*.py"))
    )


def missing_gate(session_dir: Path, assignments: list) -> str:
    """testable 的 assignment 必须已经有 write_acceptance 文件，否则不启动 Flash。"""
    missing = [
        a.id
        for a in assignments
        if getattr(a, "testable", False) and not has_gate(session_dir, a.gate_id)
    ]
    if not missing:
        return ""
    found = _existing_gate_ids(session_dir)
    hint = (
        f"testable assignment(s) {missing} have no files in acceptance/<id>/. "
        "THIS TURN: write_acceptance(task_id=<that assignment id>, "
        'filename="test_foo.py", content=<one python string, join lines with \\n>) '
        "then submit_dispatch with the SAME product assignment. "
        "filename is only the .py name, not acceptance/<id>/.... "
        "Do not add a second Flash to write tests."
    )
    if found:
        hint += f" Existing acceptance/ dirs: {found}. task_id must equal the assignment id."
    return hint


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
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        raise FileExistsError(
            f"acceptance/{task_id}/{name} already has exactly this content; nothing changed. "
            "Rewriting identical bytes cannot change the gate result. If the gate keeps "
            "reporting test_invalid, the sandbox could not run it — that is not a content "
            "problem. Judge finish instead of rewriting."
        )
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


_NO_PYTEST = "no module named pytest"


async def _pytest(target: str, extra: list[str], timeout: float) -> tuple[int, str]:
    """在沙箱里跑 pytest。PYTHONPATH=/workspace，避免验收目录抢掉产品模块的导入。

    容器里没装 pytest 时装一次再重跑：门禁跑不起来不是 Pro 能靠改测试修好的，
    否则每一步都会退回 test_invalid，Pro 只能反复重写同一份验收文件。
    """
    # -p no:cacheprovider：rootdir 是 /workspace，别把 .pytest_cache 留进交付目录。
    flags = shlex.join(
        [
            "--tb=short",
            "--import-mode=importlib",
            "--rootdir=/workspace",
            "-p",
            "no:cacheprovider",
            *extra,
        ]
    )
    command = (
        "cd /workspace && PYTHONPATH=/workspace "
        f"python -m pytest -q {flags} {shlex.quote(target)}"
    )
    code, log = await sandbox_run(command, timeout=timeout)
    if code == 0 or _NO_PYTEST not in log.lower():
        return code, log
    install, install_log = await sandbox_run(
        "python -m pip install -q pytest", timeout=timeout
    )
    if install != 0:
        return code, f"{log}\n[labhandler] pip install pytest 失败：{install_log}"
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
    "[timeout",
    "internalerror>",      # pytest 自身崩溃，不是被测代码的问题
    "no such file or directory",
    "no module named 'pytest'",
)


def _is_infra_failure(log: str) -> bool:
    """区分「测试判定失败」和「门禁根本没跑起来」。"""
    if is_sandbox_unreachable(log):
        return True
    lowered = log.lower()
    return any(marker in lowered for marker in _INFRA_MARKERS)
