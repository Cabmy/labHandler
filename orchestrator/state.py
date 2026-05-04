"""HwState - LangGraph 主图共享状态（PLAN §11 / STEPS P3.2）

设计要点：
1. TypedDict + total=False（节点只更新自己关心的字段）
2. progress_log 用 Annotated[list, add] 让 LangGraph 自动 reduce 累加
3. messages 用 list[dict]（用户决策；与 OpenAI / langchain-openai 底层一致；
   序列化到 progress_log.jsonl 时 json.dumps 直接过）
4. **reasoning_content 不强制保留**（Phase 0 修正）：建议但不报错；
   state 持久化时不必专门给字段打补丁
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict


class HwState(TypedDict, total=False):
    """hwHandler 主图共享状态"""

    # ─── 基础字段（沿用自 deep_search ResearchState） ────────
    question: str                             # 用户当前请求
    iteration: int                            # 主图迭代计数（Replan 用）
    progress_log: Annotated[list[dict], add]  # 节点完成日志，自动累加

    # ─── 多轮对话 ────────────────────────────────────────────
    messages: list[dict]
    # 每条 dict 形如 {"role": "user"|"assistant"|"tool"|"system",
    #                "content": "...",
    #                "tool_calls": [...]?,
    #                "reasoning_content": "..."?}
    # reasoning_content 建议保留（思维连贯性），漏失也不报错

    # ─── 业务字段 ────────────────────────────────────────────
    intake_result: dict     # P4.1 Intake 输出：{title, type, deliverables, constraints, ...}
    task_dag: dict          # P4.2 Planner 输出：子任务依赖图
    artifacts: Annotated[list[dict], add]   # 产物清单：[{path, kind, ts, ...}]，自动累加
    profile_snapshot: dict  # 当轮加载的 profile（P7.2 改了同步回写 yaml）
    user_constraints: Annotated[list[str], add]  # 用户对话中说的约束（Verifier 语义覆盖比对用，自动累加）
    verifier_runs: Annotated[list[dict], add]    # Verifier 多次运行（Replan 时累加）
    summary: str            # Summarizer 输出 user_summary（写入 workspace/SUMMARY.md 的人话提纲）
    lessons: str            # Summarizer 输出 lessons（archive_task 直接读，不再从 SUMMARY 字符串抽取）


# ─── 序列化辅助 ────────────────────────────────────────────────────


def state_to_jsonable(state: HwState) -> dict[str, Any]:
    """把 state 转成 json.dumps 能直接吃的 dict（messages 就是 list[dict]，无需转换）"""
    return dict(state)


def state_from_jsonable(data: dict[str, Any]) -> HwState:
    """从 dict 还原 HwState（TypedDict 是 runtime 即 dict，所以直接 cast 即可）"""
    return HwState(**data)  # type: ignore[typeddict-item]
