"""Coder agent - 负责在沙箱环境中执行具体的任务步骤，通过 ReAct 循环调用工具完成实现。

该节点采用 "Plan-and-Execute Lite" 架构，每次调用仅负责执行 task_dag 中 current_step_idx 指向的单一节点。
主图通过控制 idx 的推进来实现多步骤的有序执行。

核心机制：
1. 混合工具链：集成本地文件系统工具与 AIO Sandbox MCP 工具，确保执行过程与宿主环境隔离。
2. 上下文隔离：针对 DeepSeek 前缀缓存优化，将静态提示词（SOP、学术诚信、Profile）与动态任务状态（当前 Step、历史简报、反馈）解耦。
3. 状态推进：每次执行完成后强制推进索引，将完整性校验留给后续的 Verifier 节点。
4. 故障感知：在 Replan 周期内，Coder 会接收到上一轮的失败细节与最终草稿，以避免陷入重复错误的死循环。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import ACADEMIC_INTEGRITY_PROMPT, CODER_BASE_PROMPT
from config.runtime import get_settings
from tools.sandbox_tools import reset_sandbox_failure_counter

MAX_REACT_ITER = int(os.getenv("MAX_REACT_ITER", "6"))


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

    parts = [CODER_BASE_PROMPT]

    # 学术诚信约束（写作类启用）
    skill = (task_dag.get("skill") or intake.get("type") or "").lower()
    if skill in {"essay", "lab_report"}:
        parts.append(ACADEMIC_INTEGRITY_PROMPT)

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


def _format_dag_with_focus(
    task_dag: dict[str, Any],
    step_outputs: list[dict[str, Any]],
    current_idx: int,
) -> str:
    """全局视野 + 当前 step 高亮 + 已完成 step 标 done。

    Plan-and-Execute Lite：Coder 单步执行时的 task_dag 视图。
    标记规则：
      - i == current_idx               → [▶ 当前]
      - n.id 已出现在 step_outputs[].id → [done]
      - 否则                            → [pending]
    """
    nodes = task_dag.get("nodes") or []
    if not nodes:
        return "## task_dag 全局视野\n（Planner 未产出 DAG）"
    done_ids = {o.get("id") for o in step_outputs}
    lines = ["## task_dag 全局视野"]
    for i, n in enumerate(nodes):
        if i == current_idx:
            tag = "[▶ 当前]"
        elif n.get("id") in done_ids:
            tag = "[done]"
        else:
            tag = "[pending]"
        deps = n.get("depends_on") or []
        deps_s = f" ← {deps}" if deps else ""
        lines.append(
            f"- {tag} **{n.get('id')}** {n.get('name','')}{deps_s}：{n.get('desc','')}"
        )
    return "\n".join(lines)


def _format_current_step_detail(node: dict[str, Any]) -> str:
    """当前 step 详情（acceptance_criteria + expected_artifacts + suggested_tools）。

    告诉 Coder「你这一轮该做什么 + 满足什么条件就能收尾 + 优先用什么工具」。
    """
    parts = [
        f"## 你这轮只做 step {node.get('id')}（{node.get('name','')}）",
        f"### 描述\n{node.get('desc','')}",
    ]
    ac = node.get("acceptance_criteria") or []
    if ac:
        parts.append(
            "### 完成判定（acceptance_criteria，全部满足才发 Final Answer）\n"
            + "\n".join(f"- {c}" for c in ac)
        )
    art = node.get("expected_artifacts") or []
    if art:
        parts.append("### 预期产出文件\n" + "\n".join(f"- {f}" for f in art))
    st = node.get("suggested_tools") or []
    if st:
        parts.append(f"### 建议优先用的工具\n{', '.join(st)}")
    return "\n\n".join(parts)


def _format_completed_step_outputs(step_outputs: list[dict[str, Any]]) -> str:
    """已完成 step 的简报（仅参考，不要重做）"""
    if not step_outputs:
        return ""
    lines = ["## 已完成 step 的简报（仅参考，不要重做）"]
    for o in step_outputs:
        summ = (o.get("summary") or "").strip().splitlines()
        first = summ[0] if summ else ""
        first = first[:200]
        err = o.get("error") or ""
        if err:
            lines.append(
                f"- **{o.get('id')}** {o.get('name','')}: [error] {err[:120]}"
            )
        else:
            lines.append(
                f"- **{o.get('id')}** {o.get('name','')}: {first}"
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


def _check_dependency_artifacts(
    current_node: dict[str, Any],
    all_nodes: list[dict[str, Any]],
    step_outputs: list[dict[str, Any]],
) -> list[str]:
    """检查当前 step 的 depends_on 依赖是否满足（预期产物存在 + 依赖 step 无 error）。

    返回缺失原因列表（空 = 全部满足）。不满足时 _run_coder_async 会跳过当前 step，
    不让 Coder 在空中楼阁上浪费 ReAct 迭代。
    """
    deps = current_node.get("depends_on") or []
    if not deps:
        return []

    node_map = {n.get("id"): n for n in all_nodes}
    output_map = {o.get("id"): o for o in step_outputs}
    ws = get_settings().workspace_dir
    missing: list[str] = []

    for dep_id in deps:
        dep_node = node_map.get(dep_id)
        if not dep_node:
            missing.append(f"依赖 step {dep_id} 未在 DAG 中定义")
            continue

        dep_out = output_map.get(dep_id)
        if dep_out and dep_out.get("error"):
            missing.append(
                f"{dep_id} 执行失败（{dep_out['error']}），"
                f"当前 step {current_node.get('id')} 依赖其产出"
            )
            # 有 error 就不用再查文件了（必然不存在）
            continue

        # 检查预期产物文件是否存在
        artifacts = dep_node.get("expected_artifacts") or []
        for art in artifacts:
            if not (ws / art).exists():
                missing.append(
                    f"{dep_id} 的预期产物 {art} 不存在"
                )

    return missing


def _detect_sandbox_fatal(messages: list[Any]) -> str:
    """扫描 agent 返回消息列表，查找 `[SANDBOX_UNREACHABLE]` 致命标记。
    返回标记所在的 ToolMessage 内容（空字符串 = 未触发）。"""
    marker = "[SANDBOX_UNREACHABLE]"
    for m in messages:
        content = getattr(m, "content", "") or ""
        if isinstance(content, str) and marker in content:
            first_line = content.split("\n")[0][:200]
            return first_line
    return ""


def _build_context_user_message(state: HwState, current_idx: int) -> str:
    """构建 Coder 第一条 HumanMessage — Plan-and-Execute 单步执行视图。

    给 Coder 全局视野（看到所有 step），但**只允许做 current_idx 那一个**：
    - 全局视野：[done] / [▶ 当前] / [pending] 标记每个 step
    - 当前 step 详情段：desc + acceptance_criteria + expected_artifacts + suggested_tools
    - 已完成 step 简报：作为历史参考（不重做）

    DeepSeek 前缀缓存：动态部分（current_idx / step_outputs / user_constraints / verifier 反馈）
    都放 user message，让 system 段保持稳定前缀。
    """
    intake = state.get("intake_result") or {}
    user_constraints = state.get("user_constraints") or []
    task_dag = state.get("task_dag") or {}
    step_outputs = state.get("step_outputs") or []
    verifier_runs = state.get("verifier_runs") or []
    nodes = task_dag.get("nodes") or []
    current_node = nodes[current_idx] if 0 <= current_idx < len(nodes) else {}

    parts: list[str] = []

    # 任务概要
    parts.append(
        "## 任务概要\n"
        f"- 标题：{intake.get('title','')}\n"
        f"- 类型：{intake.get('type','')}\n"
        f"- 交付物：{intake.get('deliverables') or '（未明示，请合理推断）'}\n"
        f"- 题面约束：{intake.get('constraints') or '（无）'}"
    )

    # 全局视野 + 当前 step 高亮
    parts.append(_format_dag_with_focus(task_dag, step_outputs, current_idx))

    # 当前 step 详情（必填段，含 acceptance_criteria / expected_artifacts / suggested_tools）
    if current_node:
        parts.append(_format_current_step_detail(current_node))

    # 已完成 step 简报（仅参考）
    completed = _format_completed_step_outputs(step_outputs)
    if completed:
        parts.append(completed)

    # 用户补充约束
    if user_constraints:
        parts.append("## 用户补充约束\n- " + "\n- ".join(user_constraints))

    # 历史经验卡片（当前 step 相关的 pattern/strategy）
    step_cards = current_node.get("context_cards") or []
    if step_cards:
        parts.append("## 相关经验卡片\n- " + "\n- ".join(step_cards))

    # Replan 时附加：上一轮 Verifier 反馈 + 上一轮 Coder 终稿
    if verifier_runs:
        fb = _format_verifier_feedback(verifier_runs)
        if fb:
            parts.append(
                "## 上一轮 Verifier 反馈（整个任务 fail 已 Replan，新 DAG 已重拆；"
                "本块仅供你了解原始痛点，不代表当前 step 的反馈）\n" + fb
            )
        last_final = _last_coder_final_answer(state.get("messages") or [])
        if last_final:
            parts.append(
                "## 上一轮 Coder 终稿（避免重复同样的实现）\n" + last_final
            )

    return "\n\n".join(parts)


async def build_coder_agent() -> Any:
    """构建 React Agent（langchain.agents.create_agent）。异步：要 await sandbox_tools.get_sandbox_tools()。"""
    from langchain.agents import create_agent

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
    return create_agent(llm, all_tools)


async def _run_coder_async(state: HwState) -> dict[str, Any]:
    """Plan-and-Execute Lite 单步入口。

    每次只跑 task_dag.nodes[current_step_idx] 这一个 step。step_router（在 graph 里）
    检查 current_step_idx vs len(nodes) 决定回 coder_step 还是进 verifier。

    强制推进策略：每跑完一轮就 idx+1，不论 LLM 是否真说"step done"——
    产物缺失留给 Verifier 阶段 1 硬指标抓 → fail → Replan。
    """
    nodes = (state.get("task_dag") or {}).get("nodes") or []
    idx = int(state.get("current_step_idx", 0))

    # safety：超界（由 step_router 兜底，但加防御）
    if idx >= len(nodes):
        return {
            "current_step_idx": idx,
            "progress_log": [
                {"node": "coder_step", "skipped": "idx_out_of_range",
                 "idx": idx, "n_nodes": len(nodes)}
            ],
        }

    current = nodes[idx]

    # 依赖前置检查：如果当前 step 的 depends_on 尚未满足，跳过 agent 调用，
    # 不让 Coder 在空中楼阁上浪费 ReAct 迭代。缺失信息会随 step_outputs
    # 传给 Verifier / Replan，促使下次 Replan 调整 DAG 顺序或合并步骤。
    missing_deps = _check_dependency_artifacts(
        current, nodes, state.get("step_outputs") or []
    )
    if missing_deps:
        reason = "；".join(missing_deps)
        return {
            "current_step_idx": idx + 1,
            "step_outputs": [{
                "id": current.get("id", f"n{idx+1}"),
                "name": current.get("name", ""),
                "summary": "",
                "error": f"依赖前置不满足，跳过：{reason}",
            }],
            "progress_log": [{
                "node": "coder_step",
                "step_id": current.get("id", f"n{idx+1}"),
                "step_idx": idx,
                "skipped": "dependency_not_met",
                "reason": reason,
            }],
        }

    # 重置沙箱连续失败计数（每步独立计数）
    reset_sandbox_failure_counter()

    agent = await build_coder_agent()
    system_prompt = _build_system_prompt(state)
    context_block = _build_context_user_message(state, current_idx=idx)
    user_question = (
        f"请完成当前 step（id={current.get('id','?')}, name={current.get('name','')}）。"
        "记得 Final Answer 以 'step <id> done: <一句话>' 起头。"
    )

    try:
        result = await agent.ainvoke(
            {
                "messages": [
                    # 静态前缀（跨 REPL / Replan 多轮命中 DeepSeek 前缀缓存）
                    SystemMessage(content=system_prompt),
                    # 动态任务上下文（全局视野 + 当前 step + 已完成简报 + Replan 反馈）
                    HumanMessage(content=context_block),
                    # 当前 step 触发 question
                    HumanMessage(content=user_question),
                ]
            },
            # 单 step 通常 ≤3 ReAct iter；× 8 给 LLM 余裕收尾
            config={"recursion_limit": MAX_REACT_ITER * 8},
        )
        msgs = result.get("messages", [])
        final = ""
        for m in reversed(msgs):
            cls = m.__class__.__name__
            if cls in {"AIMessage", "AIMessageChunk"}:
                final = m.content if isinstance(m.content, str) else str(m.content)
                break

        # 从 Final Answer 中提取 Lessons 段（格式：Final Answer...\nLessons:\n- ...）
        step_lessons: list[str] = []
        if "Lessons:" in final:
            parts = final.split("Lessons:")
            if len(parts) > 1:
                lesson_block = parts[-1].strip()
                for line in lesson_block.split("\n"):
                    line = line.strip().lstrip("-").strip()
                    if line:
                        step_lessons.append(line)

        # 沙箱致命错误检测：连续 N 次失败后工具返回 [SANDBOX_UNREACHABLE] 标记
        # → 直接退出进程，等用户修复沙箱后重试
        sandbox_fatal = _detect_sandbox_fatal(msgs)
        if sandbox_fatal:
            print(
                "\n" + "=" * 60
                + "\n[SANDBOX_FATAL] 沙箱连续不可用，任务无法继续。"
                + "\n[SANDBOX_FATAL] 请检查容器状态：docker ps -a | grep aio-sandbox"
                + "\n[SANDBOX_FATAL] 重启命令：docker rm -f aio-sandbox && python cli.py"
                + f"\n[SANDBOX_FATAL] 失败详情：{sandbox_fatal}"
                + "\n" + "=" * 60
            )
            sys.exit(1)

        return {
            "messages": [_msg_to_dict(m) for m in msgs],
            "current_step_idx": idx + 1,
            "step_outputs": [{
                "id": current.get("id", f"n{idx+1}"),
                "name": current.get("name", ""),
                "summary": (final or "(no final answer)")[:500],
                "iter_messages": len(msgs),
                "step_lessons": step_lessons,
            }],
            "progress_log": [{
                "node": "coder_step",
                "step_id": current.get("id", f"n{idx+1}"),
                "step_idx": idx,
                "n_messages": len(msgs),
                "final_excerpt": (final or "")[:200],
            }],
        }
    except Exception as e:
        # 异常也推进（防死循环）；error 记进 step_outputs，让 Verifier 看到
        return {
            "current_step_idx": idx + 1,
            "step_outputs": [{
                "id": current.get("id", f"n{idx+1}"),
                "name": current.get("name", ""),
                "summary": "",
                "error": f"{type(e).__name__}: {e}",
            }],
            "progress_log": [
                {"node": "coder_step", "step_id": current.get("id", f"n{idx+1}"),
                 "step_idx": idx, "error": f"{type(e).__name__}: {e}"}
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


async def run_coder_step(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口（async）— Plan-and-Execute Lite 单步执行。

    必须是 async：LangGraph 用 stream_mode="messages" 时，依赖 contextvar 把内层 LLM 的 token
    chunk 透到父图。如果用 asyncio.run 包成同步，会创建隔离的事件循环，contextvar 跨不过去，
    Coder 阶段就完全看不到流式输出（看似"卡住"，实际 agent 在跑）。

    主图通过 step_router（orchestrator/graph.py）判断每跑完一次本节点是回到 coder_step
    跑下一 step 还是进 verifier；本函数只跑 task_dag.nodes[current_step_idx] 那一个。
    """
    return await _run_coder_async(state)
