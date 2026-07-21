"""端到端探针：测 Plan-and-Execute Lite step loop 真的逐步执行。

跑法（在项目根目录）：
    python infra/probe_step_loop.py

前置：
    - workspace/README.md 已经放好作业说明（probe 脚本不会动它）
    - config/.env 配好 PARATERA_API_KEY
    - aio-sandbox 容器在跑（probe 不自动起，手动跑 `python cli.py` 起一次即可）

输出：
    - 流式渲染主图节点事件（rich UI）
    - 完成后打印关键 verification 检查：
      * coder_step 节点执行次数 == task_dag.nodes 数（每步独立 1 次）
      * step_outputs 累加，每个 id 对应一条简报
      * task_dag.nodes 含 acceptance_criteria / expected_artifacts / suggested_tools
      * verifier 最终 verdict + missing
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
os.chdir(str(_REPO))

# 关闭 LLM cache，确保每次跑的是真实首发 LLM 行为
os.environ["LLM_CACHE_ENABLED"] = "false"

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / "config" / ".env")


async def main() -> int:
    from orchestrator.graph import get_graph
    from ui.live_panel import print_completion_panel, stream_graph

    user_q = "请按 README 实现"
    state = {
        "question": user_q,
        "iteration": 0,
        "progress_log": [],
        "messages": [{"role": "user", "content": user_q}],
        "user_constraints": [user_q],
        "verifier_runs": [],
        "artifacts": [],
    }

    print("\n=== 启动主图 (Plan-and-Execute Lite step loop) ===\n")
    t0 = time.time()
    g = get_graph()
    final_state = await stream_graph(g, state)
    elapsed = time.time() - t0
    print(f"\n=== 主图完成 elapsed: {elapsed:.1f}s ===\n")

    print_completion_panel(final_state)

    # ─── Step loop verification ────────────────────────────────────
    print("\n" + "=" * 70)
    print("Step loop behavior check")
    print("=" * 70)

    nodes = (final_state.get("task_dag") or {}).get("nodes") or []
    progress = final_state.get("progress_log") or []
    coder_step_logs = [p for p in progress if p.get("node") == "coder_step"]
    so = final_state.get("step_outputs") or []
    runs = final_state.get("verifier_runs") or []
    arts = final_state.get("artifacts") or []

    # 1. coder_step 节点执行次数
    print(f"\n[1] task_dag.nodes 数: {len(nodes)}")
    for n in nodes:
        ac = n.get("acceptance_criteria") or []
        ea = n.get("expected_artifacts") or []
        st = n.get("suggested_tools") or []
        print(
            f"    • {n.get('id')} {n.get('name','')!r:<25} "
            f"acceptance={len(ac)} 条 artifacts={ea} tools={st}"
        )

    print(f"\n[2] coder_step 节点执行次数: {len(coder_step_logs)}")
    if len(coder_step_logs) == len(nodes):
        print(f"    ✓ 等于 task_dag.nodes 数 ({len(nodes)})，每步独立执行 1 次")
    else:
        print(
            f"    ✗ 不等于 task_dag.nodes 数 ({len(nodes)})，"
            f"差 {abs(len(coder_step_logs) - len(nodes))} 次"
        )
    for log in coder_step_logs:
        print(
            f"    - step_idx={log.get('step_idx')} step_id={log.get('step_id')} "
            f"n_messages={log.get('n_messages', '?')} "
            f"final={(log.get('final_excerpt') or '')[:80]!r}"
        )

    # 3. step_outputs 累加
    print(f"\n[3] step_outputs 累加: {len(so)} 条")
    for o in so:
        summ = (o.get("summary") or "").strip().splitlines()
        first = summ[0] if summ else ""
        print(f"    - id={o.get('id')} name={o.get('name','')!r}")
        print(f"      summary[0]={first[:120]!r}")
        if o.get("error"):
            print(f"      error={o['error'][:120]!r}")

    # 4. verifier verdict
    if runs:
        last = runs[-1]
        print(f"\n[4] verifier 最终 verdict: {last.get('verdict','?')}")
        print(f"    iteration: {final_state.get('iteration')}")
        cov = last.get("coverage") or {}
        miss = cov.get("missing") or []
        cov_list = cov.get("covered") or []
        print(f"    covered: {len(cov_list)} 条")
        print(f"    missing: {len(miss)} 条")
        for m in miss[:5]:
            c = m.get("constraint", "?") if isinstance(m, dict) else str(m)
            r = m.get("reason", "") if isinstance(m, dict) else ""
            print(f"      - {c[:60]} ({r[:40]})")
    else:
        print("\n[4] verifier_runs 为空（异常情况）")

    # 5. artifacts
    art_paths = sorted({a.get("path", "") for a in arts if a.get("path")})
    print(f"\n[5] artifacts 实际产物: {len(art_paths)} 件")
    for p in art_paths:
        print(f"    - {p}")

    # 6. 综合 verdict
    print("\n" + "=" * 70)
    if (
        len(coder_step_logs) == len(nodes)
        and len(so) >= len(nodes)
        and runs
        and (runs[-1].get("verdict") in ("pass", "fail"))
    ):
        print("✓ 步骤 loop 行为符合设计")
    else:
        print("✗ 步骤 loop 异常：见上面具体项")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
