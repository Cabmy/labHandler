"""eval 模块公共常量与工具函数。

集中存放跨脚本共享的常量（RULE_IDS、REPO、CASES_DIR）和公共逻辑
（单例重置、gate 复制、沙箱 pytest 执行），避免 run_case / report / regate
各自重复定义。
"""

import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# 项目根目录，供各脚本定位导入路径与默认输出位置
REPO = Path(__file__).resolve().parent.parent

# eval case 目录
CASES_DIR = Path(__file__).resolve().parent / "cases"

# Remember-Judge 规则标识，与 prompts.py 中规则顺序一一对应
RULE_IDS = ("screenshot", "blockquote", "filename")


def reset_all_singletons() -> None:
    """重置跨 case 的全局单例，确保每次跑 case 前状态干净。

    包括：配置缓存、策略/审计单例、归档默认实例。
    """
    from config.runtime import get_settings

    get_settings.cache_clear()

    import memory.archive as archive_mod
    import tools.policy as policy_mod

    archive_mod._default_archive = None
    policy_mod._policy = None
    policy_mod._auditor = None


def copy_gate_to_workspace(case: str, workspace: Path) -> Path:
    """把 case 的 gate 目录复制到 workspace/_eval_gate，返回目标路径。

    如果目标已存在则先清除再复制。run_case 与 regate 都需要这一步。
    """
    dest = workspace / "_eval_gate"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CASES_DIR / case / "gate", dest)
    return dest


# 沙箱 pytest 运行器类型：接受命令与超时，返回 (exit_code, log)
SandboxRunner = Callable[[str, float], Awaitable[tuple[int, str]]]


async def pytest_in_sandbox(
    gate_dest: Path,
    timeout: float,
    runner: Optional[SandboxRunner] = None,
) -> dict[str, Any]:
    """在沙箱里跑 gate 的 pytest 并收集结果。

    参数:
        gate_dest: 宿主机上 gate 目录的绝对路径（如 workspace/_eval_gate），
                   会通过 sandbox_workspace_path 映射为容器内路径
        timeout: 单次 pytest 超时秒数
        runner: 可选的自定义运行器，用于 regate 的 MCP 重连重试等场景；
                默认走 sandbox_run 直跑。

    返回:
        {"exit_code": int, "passed": bool, "log": str}
    """
    if runner is None:
        from tools.sandbox_tools import sandbox_run

        async def runner(cmd: str, to: float) -> tuple[int, str]:
            code, log = await sandbox_run(cmd, timeout=to)
            return int(code), log or ""

    # 将宿主机绝对路径映射为容器内 /workspace/... 路径
    from tools.sandbox_tools import sandbox_workspace_path

    container_path = sandbox_workspace_path(gate_dest)
    command = (
        f"cd /workspace && PYTHONPATH=/workspace python -m pytest -q {container_path}"
    )
    code, log = await runner(command, timeout)
    return {
        "exit_code": int(code),
        "passed": code == 0,
        "log": (log or "")[-4000:],
    }
