"""Coder agent - ReAct in AIO Sandbox（prebuilt create_react_agent）

设计要点（PLAN §8.3 / STEPS P4.3）：
1. 用 langgraph.prebuilt.create_react_agent（用户决策）
2. 工具集 = 本地 fs_tools/skill_tool/profile_tool/rag_tool 子集 + sandbox MCP tools（异步加载）
3. 入参 messages 拆三段（DeepSeek 前缀缓存友好 + Replan 反馈传递）：
   - SystemMessage：CODER_BASE_PROMPT + 学术诚信 + skill SOP + profile 注入（**静态/半静态**）
   - HumanMessage（context）：intake / task_dag.nodes / user_constraints / lessons /
     上一轮 Verifier 反馈 / 上一轮 Coder 终稿（**动态**，多轮中变）
   - HumanMessage（question）：state.question
4. 思考模式参数顶层传 + extra_body（DS_V4_PRO_KWARGS 已在 llm.provider 全局注入）
5. 第一次跑要起容器；后续可常驻

⚠️ Phase 4 范围：
- 异步 entry：build_coder_agent() 拿到 react agent；用法：await agent.ainvoke({"messages": [...]})
- 同步 wrapper run_coder() 用 asyncio.run，方便 LangGraph 节点直调（langgraph 节点本身可同步）
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from prompts import ACADEMIC_INTEGRITY_PROMPT, CODER_BASE_PROMPT

MAX_REACT_ITER = int(os.getenv("MAX_REACT_ITER", "6"))


_CODER_BASE_PROMPT = CODER_BASE_PROMPT  # 局部引用名兼容
_ACADEMIC_INTEGRITY_PROMPT = ACADEMIC_INTEGRITY_PROMPT


def _build_system_prompt(state: HwState) -> str:
    """构建 Coder system prompt — 只放**静态/半静态**内容，让 DeepSeek 前缀缓存命中。

    内容（按调用 stable）：
      1. CODER_BASE_PROMPT          — 纯静态
      2. ACADEMIC_INTEGRITY_PROMPT  — 按 skill 条件追加（同任务内 stable）
      3. skill SOP body             — 按 skill 加载（同任务内 stable）
      4. profile inject             — 按用户 stable

    动态内容（intake / user_constraints / lessons / verifier 反馈 / task_dag）放到
    _build_context_user_message，作为第一条 HumanMessage 发出去。
    """
    intake = state.get("intake_result") or {}
    task_dag = state.get("task_dag") or {}

    parts = [_CODER_BASE_PROMPT]

    # 学术诚信约束（写作类启用）
    skill = (task_dag.get("skill") or intake.get("type") or "").lower()
    if skill in {"essay", "lab_report"}:
        parts.append(_ACADEMIC_INTEGRITY_PROMPT)

    # skill SOP
    if skill in {"coding", "essay", "lab_report"}:
        try:
            from tools.skill_tool import get_skill_body
            body = get_skill_body(skill) or ""
            if body:
                parts.append(f"## 当前 skill SOP（{skill}）\n{body}")
        except Exception:
            pass

    base = "\n\n".join(parts)
    # profile 注入（identity + writing_style + coding_style）
    return inject_for_agent("coder", base)


def _last_coder_final_answer(messages: list[dict[str, Any]], max_chars: int = 800) -> str:
    """从 state.messages 倒序找最后一条非空 assistant 消息（上一轮 Coder Final Answer）。

    Replan 第二轮 Coder 用，让它知道自己上次说了什么、避免重复犯错。
    无效消息（无 content / 仅 tool_calls）跳过；找不到返回 ""。
    """
    if not messages:
        return ""
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content") or ""
        if isinstance(content, str) and content.strip():
            text = content.strip()
            return text if len(text) <= max_chars else text[:max_chars] + "…(截断)"
    return ""


def _format_dag_nodes(task_dag: dict[str, Any]) -> str:
    """Planner 拆出来的 DAG 节点列表（id / agent / desc）"""
    nodes = task_dag.get("nodes") or []
    if not nodes:
        return "（Planner 未产出 DAG）"
    lines = []
    for n in nodes:
        deps = n.get("depends_on") or []
        deps_s = f" ← {deps}" if deps else ""
        lines.append(
            f"- **{n.get('id')}** [{n.get('agent')}] {n.get('name','')}{deps_s}：{n.get('desc','')}"
        )
    return "\n".join(lines)


def _format_verifier_feedback(verifier_runs: list[dict[str, Any]]) -> str:
    """上一轮 Verifier 反馈（Replan 时让 Coder 看到具体失败点）"""
    if not verifier_runs:
        return ""
    last = verifier_runs[-1]
    parts = [f"- verdict: {last.get('verdict','')}"]
    s1 = last.get("stage1_failures") or []
    if s1:
        parts.append("- 阶段 1 硬指标失败：")
        parts.extend(f"  - {x}" for x in s1)
    cov = last.get("coverage") or {}
    missing = cov.get("missing") or []
    if missing:
        parts.append("- 未覆盖约束：")
        for m in missing:
            c = m.get("constraint", "") if isinstance(m, dict) else str(m)
            r = m.get("reason", "") if isinstance(m, dict) else ""
            parts.append(f"  - {c}（{r}）" if r else f"  - {c}")
    sf = last.get("suggested_fix") or ""
    if sf:
        parts.append(f"- 修复建议：{sf}")
    return "\n".join(parts)


def _build_context_user_message(state: HwState) -> str:
    """构建 Coder 第一条 HumanMessage — 含**动态**任务上下文 + Replan 反馈。

    DeepSeek 前缀缓存：因为这部分动态、且 user_constraints 多轮会增长，必须放在
    user message 而非 system，让 system 段保持稳定前缀给缓存命中。
    """
    intake = state.get("intake_result") or {}
    user_constraints = state.get("user_constraints") or []
    task_dag = state.get("task_dag") or {}
    verifier_runs = state.get("verifier_runs") or []

    parts: list[str] = []

    # 任务概要
    parts.append(
        "## 任务概要\n"
        f"- 标题：{intake.get('title','')}\n"
        f"- 类型：{intake.get('type','')}\n"
        f"- 交付物：{intake.get('deliverables') or '（未明示，请合理推断）'}\n"
        f"- 题面约束：{intake.get('constraints') or '（无）'}"
    )

    # Planner 拆解（task_dag.nodes）
    parts.append("## Planner 拆解（task_dag）\n" + _format_dag_nodes(task_dag))

    # 用户补充约束
    if user_constraints:
        parts.append("## 用户补充约束\n- " + "\n- ".join(user_constraints))

    # 历史经验
    lessons = task_dag.get("lessons") or []
    if lessons:
        parts.append("## 相似历史任务经验\n- " + "\n- ".join(lessons))

    # Replan 时附加：上一轮 Verifier 反馈 + 上一轮 Coder 终稿
    if verifier_runs:
        fb = _format_verifier_feedback(verifier_runs)
        if fb:
            parts.append(
                "## 上一轮 Verifier 反馈（你失败了，请基于此调整本轮实现）\n" + fb
            )
        last_final = _last_coder_final_answer(state.get("messages") or [])
        if last_final:
            parts.append(
                "## 上一轮 Coder 终稿（避免重复同样的实现）\n" + last_final
            )

    return "\n\n".join(parts)


async def build_coder_agent(state: HwState) -> Any:
    """构建 prebuilt React Agent。异步：要 await sandbox_tools.get_sandbox_tools()。"""
    from langgraph.prebuilt import create_react_agent

    # 本地工具子集（只挑 Coder 用得上的）
    from tools.fs_tools import (
        host_bash, list_dir, patch_file, read_file, write_file,
    )
    from tools.skill_tool import list_skills, load_skill
    from tools.profile_tool import read_profile
    from tools.search_tool import web_search

    local_tools = [
        read_file, write_file, list_dir, patch_file, host_bash,
        load_skill, list_skills, read_profile,
        # web_search：host 端 ddgs，继承宿主 shell 的代理（HTTPS_PROXY / ALL_PROXY），
        # 比容器内 browser_* 更稳（容器无翻墙能力，撞墙站点会 ERR_CONNECTION_REFUSED）
        web_search,
    ]

    # 沙箱 tools（异步加载）
    sandbox_tools: list[Any] = []
    try:
        from tools.sandbox_tools import get_sandbox_tools
        sandbox_tools = await get_sandbox_tools()
    except Exception as e:
        # 沙箱没起也允许构建（agent 调到时会失败抛回 LLM）
        print(f"[Coder] sandbox 工具加载失败：{type(e).__name__}: {e}")

    all_tools = local_tools + sandbox_tools
    llm = get_llm()
    return create_react_agent(llm, all_tools)


async def _run_coder_async(state: HwState) -> dict[str, Any]:
    agent = await build_coder_agent(state)
    system_prompt = _build_system_prompt(state)
    context_block = _build_context_user_message(state)
    user_question = state.get("question") or "请按任务概要完成作业。"

    try:
        result = await agent.ainvoke(
            {
                "messages": [
                    # 静态前缀（跨 REPL / Replan 多轮命中 DeepSeek 前缀缓存）
                    SystemMessage(content=system_prompt),
                    # 动态任务上下文（含 Replan 时的 Verifier 反馈 + 上轮 Final Answer）
                    HumanMessage(content=context_block),
                    # 当前 question
                    HumanMessage(content=user_question),
                ]
            },
            # ReAct 1 iter ≈ 2 graph step（assistant + tool）；预留 N×8 让 LLM 有余裕收尾
            config={"recursion_limit": MAX_REACT_ITER * 8},
        )
        msgs = result.get("messages", [])
        # 最后一条非 tool 消息当 final
        final = ""
        for m in reversed(msgs):
            cls = m.__class__.__name__
            if cls in {"AIMessage", "AIMessageChunk"}:
                final = m.content if isinstance(m.content, str) else str(m.content)
                break
        return {
            "messages": [_msg_to_dict(m) for m in msgs],
            "progress_log": [
                {
                    "node": "coder",
                    "n_messages": len(msgs),
                    "final_excerpt": (final or "")[:200],
                }
            ],
        }
    except Exception as e:
        return {
            "progress_log": [
                {"node": "coder", "error": f"{type(e).__name__}: {e}"}
            ],
        }


def _msg_to_dict(m: Any) -> dict[str, Any]:
    """LangChain BaseMessage → plain dict（HwState.messages 序列化用）"""
    cls = m.__class__.__name__
    role_map = {
        "SystemMessage": "system",
        "HumanMessage": "user",
        "AIMessage": "assistant",
        "AIMessageChunk": "assistant",
        "ToolMessage": "tool",
    }
    role = role_map.get(cls, "assistant")
    out: dict[str, Any] = {"role": role}
    content = getattr(m, "content", "")
    out["content"] = content if isinstance(content, str) else str(content)
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        out["tool_calls"] = tcs
    ak = getattr(m, "additional_kwargs", None) or {}
    if ak.get("reasoning_content"):
        out["reasoning_content"] = ak["reasoning_content"]
    if cls == "ToolMessage":
        out["tool_call_id"] = getattr(m, "tool_call_id", "")
        out["name"] = getattr(m, "name", "")
    return out


async def run_coder(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口（async）。

    必须是 async：LangGraph 用 stream_mode="messages" 时，依赖 contextvar 把内层 LLM 的 token
    chunk 透到父图。如果用 asyncio.run 包成同步，会创建隔离的事件循环，contextvar 跨不过去，
    Coder 阶段就完全看不到流式输出（看似"卡住"，实际 agent 在跑）。
    """
    return await _run_coder_async(state)
