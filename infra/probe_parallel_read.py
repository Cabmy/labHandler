"""探针：测 DeepSeek-V4-Pro 思考模式下 Coder 是否会一次发出多个 read_file tool_calls。

为何需要这个探针
----------------
当前 Coder 走 `langchain.agents.create_agent`，内部 ToolNode 对多 tool_calls 用
`asyncio.gather` 并发 dispatch（`langgraph/prebuilt/tool_node.py:857`）；
ChatOpenAI.bind_tools 也支持 `parallel_tool_calls=True`。

但**实际 LLM 是否会主动一次发出多个 tool_calls** 是模型行为问题，不是框架问题。
DeepSeek-V4-Pro 思考模式倾向"思考一步 → 调一个 tool → 看结果 → 再思考"的串行节奏，
是否会自发并行需要实测。

用法
----
1. 确保 `config/.env` 配好 `PARATERA_API_KEY`（不消耗多少 token，但需要真实 API 调用）
2. 在项目根目录跑：
       python infra/probe_parallel_read.py            # 中性 query（测默认行为）
       python infra/probe_parallel_read.py --guided   # 显式引导 query（测激活效果）

输出
----
列出每条 AIMessage 包含的 read_file tool_calls 个数；
单条 AIMessage 含 ≥2 个 read_file → PARALLEL；
每条只含 1 个 → SERIAL；
据此决定要不要在 CODER_BASE_PROMPT 里加并行引导。

不污染主 workspace
------------------
fixture 写到 `workspace/.parallel_probe/`（`.` 开头，intake._scan_workspace 会排除）。
跑完不删除（方便复跑/二次观测）；要清场可手动 `rm -rf workspace/.parallel_probe`。

token 预算
----------
单次 probe 估计 5-15k tokens（DS 思考模式 reasoning 段较密）；recursion_limit=16
压住 ReAct 循环上限，避免失控。LLM cache 关掉，避免命中历史。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# ─── 路径与 .env 加载（让脚本能从任意 cwd 跑） ────────────────────
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
os.chdir(str(_REPO))

# 探针强制关闭 LLM cache，避免命中历史让结果失真
os.environ["LLM_CACHE_ENABLED"] = "false"

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / "config" / ".env")


# ─── Fixture：5 个明显独立、内容差异化的 markdown 文件 ───────────
PROBE_DIR = _REPO / "workspace" / ".parallel_probe"
PROBE_DIR.mkdir(parents=True, exist_ok=True)
FIXTURES = {
    "alpha.md":   "# Alpha\n第一行内容: ALPHA-VALUE-001\n",
    "beta.md":    "# Beta\n第一行内容: BETA-VALUE-002\n",
    "gamma.md":   "# Gamma\n第一行内容: GAMMA-VALUE-003\n",
    "delta.md":   "# Delta\n第一行内容: DELTA-VALUE-004\n",
    "epsilon.md": "# Epsilon\n第一行内容: EPSILON-VALUE-005\n",
}
for _name, _content in FIXTURES.items():
    (PROBE_DIR / _name).write_text(_content, encoding="utf-8")


# ─── 两种 query 模式 ─────────────────────────────────────────────
USER_QUERY_NEUTRAL = (
    "请读取以下 5 个文件并给出每个文件的第一行内容（用 read_file 工具）：\n"
    "  - .parallel_probe/alpha.md\n"
    "  - .parallel_probe/beta.md\n"
    "  - .parallel_probe/gamma.md\n"
    "  - .parallel_probe/delta.md\n"
    "  - .parallel_probe/epsilon.md\n"
)

USER_QUERY_GUIDED = USER_QUERY_NEUTRAL + (
    "\n**重要**：这 5 个文件互相独立、读取无依赖关系。"
    "**请在同一轮内一次性发出 5 个 read_file tool_calls**（由 ToolNode 并发调度），"
    "不要逐个串行调用浪费 ReAct iter 预算。"
)


async def _build_minimal_coder_no_sandbox():
    """复刻 build_coder_agent 但跳过 sandbox tools 加载。

    理由：
    - probe 关心的是 LLM 行为本身，sandbox tools 多了会让 LLM 选 sandbox_file_operations
      之类替代 read_file，干扰观测
    - sandbox 没起也能跑 probe，降低运行门槛
    """
    from langchain.agents import create_agent

    from llm import get_llm
    from tools.fs_tools import (
        host_bash, list_dir, patch_file, read_file, write_file,
    )
    from tools.profile_tool import read_profile
    from tools.skill_tool import list_skills, load_skill
    from tools.search_tool import web_search

    local_tools = [
        read_file, write_file, list_dir, patch_file, host_bash,
        load_skill, list_skills, read_profile, web_search,
    ]
    return create_agent(get_llm(), local_tools)


async def main(guided: bool) -> int:
    from langchain_core.messages import HumanMessage, SystemMessage

    from config.prompts import CODER_BASE_PROMPT

    query = USER_QUERY_GUIDED if guided else USER_QUERY_NEUTRAL
    mode = "GUIDED" if guided else "NEUTRAL"
    print(f"[probe] mode = {mode}")
    print(f"[probe] fixture dir = {PROBE_DIR.relative_to(_REPO)}")
    print(f"[probe] LLM_CACHE_ENABLED = {os.getenv('LLM_CACHE_ENABLED', '?')}")

    print("[probe] building coder agent (no sandbox)...")
    agent = await _build_minimal_coder_no_sandbox()
    print("[probe] agent ready, dispatching query...\n")

    t0 = time.time()
    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=CODER_BASE_PROMPT),
                HumanMessage(content=query),
            ]
        },
        config={"recursion_limit": 16},
    )
    elapsed = time.time() - t0

    msgs = result.get("messages", [])
    print(f"[probe] elapsed: {elapsed:.2f}s, total messages: {len(msgs)}\n")

    # 收集 read_file tool_calls per AIMessage
    rows: list[tuple[int, str, list[str]]] = []  # (msg_idx, msg_class, file_names)
    for i, m in enumerate(msgs):
        cls = m.__class__.__name__
        if cls not in ("AIMessage", "AIMessageChunk"):
            continue
        tcs = getattr(m, "tool_calls", None) or []
        read_files = [
            tc.get("args", {}).get("path", "?")
            for tc in tcs
            if tc.get("name") == "read_file"
        ]
        if read_files:
            rows.append((i, cls, read_files))

    print("[probe] read_file 分布（每条 AIMessage）:")
    for idx, _cls, files in rows:
        marker = "★" if len(files) > 1 else " "
        print(f"  {marker} msg[{idx:>2}] → {len(files)} call(s): {files}")

    total = sum(len(f) for _, _, f in rows)
    max_per_msg = max((len(f) for _, _, f in rows), default=0)
    n_aimsgs_with_reads = len(rows)

    print(
        f"\n[probe] 汇总: 总 read_file = {total}, "
        f"含 read 的 AIMessage 数 = {n_aimsgs_with_reads}, "
        f"单条 AIMessage 最大 read 数 = {max_per_msg}"
    )

    # ─── verdict ───────────────────────────────────────────────────
    print()
    if max_per_msg >= 2:
        print(
            f"VERDICT: PARALLEL ✓\n"
            f"  单条 AIMessage 最多发出 {max_per_msg} 个 read_file，"
            "LLM 主动并行 — ToolNode 已并发调度。"
        )
    elif max_per_msg == 1 and n_aimsgs_with_reads >= 2:
        print(
            "VERDICT: SERIAL ✗\n"
            "  LLM 每条 AIMessage 只发 1 个 read_file，思考模式串行节奏。\n"
            "  建议：在 CODER_BASE_PROMPT 加并行引导（用 GUIDED 模式再测一次确认）；"
            "  或显式 bind_tools(parallel_tool_calls=True)。"
        )
    elif max_per_msg == 1 and n_aimsgs_with_reads == 1:
        print(
            "VERDICT: ONE_ONLY\n"
            "  LLM 只读了 1 个文件就停了，无法判断并行能力 — "
            "提高 query 明确性或检查模型行为。"
        )
    else:
        print(
            "VERDICT: NO_READS\n"
            "  LLM 没用 read_file 工具（可能选了 host_bash cat 之类）— "
            "看 messages 详情排查。"
        )

    return 0


if __name__ == "__main__":
    use_guided = "--guided" in sys.argv
    sys.exit(asyncio.run(main(guided=use_guided)))
