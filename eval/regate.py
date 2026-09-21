#!/usr/bin/env python3
"""只重跑外部门禁，不重跑 lab。

用途：门禁本身写错（第一版 two_sum_variant 的 test_negatives 期望值算错）或门禁
超时被记成质量失败（minikv_variant 冷跑 exit_code=-1）时，交付物是好的，没必要
再烧一遍 lab。本脚本按 result.json 里的 workspace 重跑 gate，就地更新
external_passed / external_exit_code / external_log，并留一条 regate 记录。
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from eval import (
    copy_gate_to_workspace,
    pytest_in_sandbox,
    reset_all_singletons,
)


async def _mcp_reconnect_runner() -> tuple[int, str]:
    """带 MCP 重连重试的沙箱运行器。

    recreate_sandbox 只轮询端口，端口通了 MCP 的 streamable_http 端点还可能没起来，
    首次 initialize 会 ReadError。重试并在每次之间重置客户端缓存。
    重建后的容器没装 pytest，兜底安装一次。
    """
    from mcp_client import reset_mcp_client
    from tools.sandbox_tools import sandbox_run

    async def run(cmd: str, timeout: float) -> tuple[int, str]:
        last = (-1, "")
        for attempt in range(5):
            try:
                code, log = await sandbox_run(cmd, timeout=timeout)
                lowered = (log or "").lower()
                if "no module named pytest" in lowered:
                    install, ilog = await sandbox_run(
                        "python -m pip install -q pytest", timeout=timeout
                    )
                    if install != 0:
                        return code, f"{log}\n[regate] pip install pytest 失败：{ilog}"
                    code, log = await sandbox_run(cmd, timeout=timeout)
                    lowered = (log or "").lower()
                if "[sandbox_unreachable]" not in lowered:
                    return int(code), log or ""
                last = (int(code), (log or ""))
            except Exception as exc:  # noqa: BLE001 - 首轮 MCP 未就绪
                last = (-1, f"{type(exc).__name__}: {exc}")
            reset_mcp_client()
            await asyncio.sleep(3 * (attempt + 1))
        return last

    return run


async def rerun_one(result_path: Path, timeout: float) -> dict[str, Any]:
    row = json.loads(result_path.read_text(encoding="utf-8"))
    case = str(row.get("case") or "")
    workspace = Path(str(row.get("workspace_dir") or ""))
    if not workspace.is_dir():
        return {"run_id": row.get("run_id"), "skipped": "missing_workspace"}

    dest = copy_gate_to_workspace(case, workspace)
    runner = await _mcp_reconnect_runner()
    result = await pytest_in_sandbox(dest, timeout, runner=runner)
    code = result["exit_code"]
    log = result["log"]
    before = {
        "external_passed": row.get("external_passed"),
        "external_exit_code": row.get("external_exit_code"),
    }
    row["external_passed"] = code == 0
    row["external_exit_code"] = int(code)
    row["external_log"] = (log or "")[-4000:]
    history = row.get("regate") or []
    history.append(
        {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "before": before,
            "after": {"external_passed": row["external_passed"], "external_exit_code": int(code)},
        }
    )
    row["regate"] = history
    result_path.write_text(
        json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "run_id": row.get("run_id"),
        "before": before["external_passed"],
        "after": row["external_passed"],
        "exit_code": int(code),
    }


async def main_async(runs_dir: Path, only: list[str], recreate: bool) -> None:
    """一个进程只处理一个 run。

    沙箱挂载的是 WORKSPACE_DIR，每个 run 的 workspace 不同，所以必须换环境变量再
    重建容器。但 mcp_client 缓存的 session 绑在旧容器上，同进程内反复重建会让
    streamable_http 的 cancel scope 跨 task 退出而炸。所以循环交给外层 shell，
    本函数只做一个。

    --no-recreate 用于容器已经挂在目标 workspace 上的情况：跳过重建，直接跑。
    重建后 MCP 端点要几秒才真正可用，第一次 initialize 常常 ReadError。
    """
    import os

    from config.runtime import get_settings
    from infra.sandbox_boot import ensure_sandbox, recreate_sandbox

    paths = sorted(runs_dir.glob("*/result.json"))
    targets = [p for p in paths if not only or p.parent.name in only]
    if not targets:
        print("[regate] no matching run", flush=True)
        return
    if len(targets) > 1:
        names = " ".join(p.parent.name for p in targets)
        print(f"[regate] 一次只能跑一个 run，请分别执行：{names}", flush=True)
        return

    path = targets[0]
    ws = json.loads(path.read_text(encoding="utf-8")).get("workspace_dir")
    os.environ["WORKSPACE_DIR"] = str(Path(str(ws)).resolve())
    reset_all_singletons()
    if recreate:
        recreate_sandbox(log=lambda m: None)
        # 端口通了不等于 MCP 端点可用，给它一点时间。
        await asyncio.sleep(5)
    else:
        ensure_sandbox(log=lambda m: None)
    out = await rerun_one(path, timeout=get_settings().tool_timeout_s)
    print(f"[regate] {json.dumps(out, ensure_ascii=False)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="只重跑 eval 外部门禁")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--only", nargs="*", default=[],
                        help="限定 run_id（一次一个）")
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="容器已挂在目标 workspace 上时跳过重建（避免 MCP session 失效）",
    )
    args = parser.parse_args()
    asyncio.run(
        main_async(Path(args.runs).resolve(), args.only,
                   recreate=not args.no_recreate)
    )


if __name__ == "__main__":
    main()
