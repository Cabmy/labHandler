"""HwState - LangGraph 主图共享状态

设计要点：
1. TypedDict + total=False（节点只更新自己关心的字段）
2. progress_log 用 Annotated[list, add] 由 LangGraph 自动归约累加
3. messages 用 list[dict]（与 OpenAI / langchain-openai 底层格式一致；
   序列化到 progress_log.jsonl 时 json.dumps 直接过），且**同样是归约字段**
   —— 见下方字段注释里的踩坑记录
4. **reasoning_content 非强制**：建议保留，缺失不报错；
   状态持久化时无需专门补字段
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class HwState(TypedDict, total=False):
    """labHandler 主图共享状态。"""

    # ─── 基础字段（继承自 deep_search ResearchState） ────────
    question: str                             # 用户当前请求
    iteration: int                            # 主图迭代计数器（供 Replan）
    progress_log: Annotated[list[dict], add]  # 节点完成日志，自动累加

    # ─── 多轮对话 ────────────────────────────────────────────
    messages: Annotated[list[dict], add]
    # 每个 dict 如 {"role": "user"|"assistant"|"tool"|"system",
    #                "content": "...",
    #                "tool_calls": [...]?,
    #                "reasoning_content": "..."?,
    #                "usage_metadata": {...}?}
    # reasoning_content 建议保留（思维连贯），缺失也不报错
    #
    # **为什么是归约字段**：曾经是覆盖字段，而 coder_step 每步都整体覆写它，
    # 于是一份 messages 被 N 个 step 轮流踩掉，同时产出三个症状 ——
    #   ① compile 落盘的 transcript.jsonl 只剩最后一个 step；
    #   ② session 追加的用户轮次被第一个 coder_step 抹掉（roles[0] 从 user 变 system）；
    #   ③ summarizer 的「用户多轮对话」块实际喂进去的是 Coder 的 system prompt。
    # 改为累加后三者一并消失。配套约束：coder 返回增量时必须剔除 SystemMessage
    # （静态前缀 15k+ 字符，逐 superstep 全量重序列化会把 checkpoint 撑爆），
    # 且 tool 消息内容有字节预算（见 agents/coder.py:_msg_to_dict）。

    # ─── 业务字段 ────────────────────────────────────────────
    intake_result: dict     # Intake 输出：{title, type, deliverables, constraints, ...}
    task_dag: dict          # Planner 输出：子任务依赖图
    artifacts: Annotated[list[dict], add]   # 产物列表：[{path, kind, ts, ...}]，自动累加
    user_constraints: Annotated[list[str], add]  # 来自用户对话的约束（供 Verifier 语义覆盖比对，自动累加）
    verifier_runs: Annotated[list[dict], add]    # Verifier 多次运行（Replan 期间累积）
    summary: str            # Summarizer 输出 user_summary（写入 workspace/SUMMARY.md 的人读纲要）
    knowledge_cards: list[dict]  # Summarizer 输出知识卡片 [{type, content}, ...]（供 /done 归档）

    # ─── Plan-and-Execute Lite（Coder 单步执行循环） ────────
    # current_step_idx：coder_step 节点要执行 task_dag.nodes 中第几个 step（0 基）；
    #   每轮 coder_step 后 +1（Final Answer 报 needs_retry 且 MAX_STEP_RETRY 未耗尽时
    #   保持不变，主图原地重跑同一 step），
    #   step_router 决定回 coder_step 还是进 verifier；
    #   每次 planner 节点执行（含 Replan）reset 为 0
    # step_outputs：每个 step 完成后的简报，用 Annotated[list, add] 累积；
    #   Replan 时不清旧轮条目，Verifier/Summarizer 能看到全部历史。
    #   条目结构 {id, name, iteration, attempt, status, summary, ...}：
    #   - status ∈ done|needs_retry|failed
    #   - iteration：产生该条目时所处的 planner 轮次（= 写入时的 state["iteration"]）
    #   - attempt：该 step 在**本轮内**的第几次尝试（1 基），重试预算的唯一依据
    #   踩坑记录：attempt 曾经不落盘，而是靠「数同 id 条目数」反推。
    #   但 session.prepare_task 在修订路径会把 iteration 重置为 0，
    #   planner 的防撞后缀 _r{iteration+1} 因此会在多次修订间重复生成同一个 id，
    #   撞上历史条目后新 step 的尝试数一开跑就 ≥ MAX_STEP_RETRY，
    #   首次 needs_retry 即被判 failed → step_router 短路 → 本轮剩余 step 全跳过。
    #   现在 attempt 显式落盘、且按 (id, iteration) 配对计数，跨轮撞名不再污染预算。
    current_step_idx: int
    step_outputs: Annotated[list[dict], add]


# ─── 归约字段清单（list-add reducer，外部消费者必须累加不能覆盖） ───
#
# 这些字段在 HwState 里用 Annotated[list, add]，跑主图时 LangGraph 自动累加节点返回增量。
# 但 ui/live_panel.stream_graph 从 stream_mode='updates' 读 diff 并给外部（cli）
# 组装 final_state 时必须用本清单做 list.extend，不能直接 dict 赋值覆盖
# （否则只剩最后一个节点返回的部分，前面的丢失）。
# 新增/改动归约字段时**必须同步本清单**，否则 CLI/Web 的 final_state 会静默丢数据。
REDUCER_LIST_FIELDS: tuple[str, ...] = (
    "progress_log",
    "verifier_runs",
    "artifacts",
    "user_constraints",
    "step_outputs",
    "messages",
)

