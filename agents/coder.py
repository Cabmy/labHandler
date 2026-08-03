"""Coder agent —— 通过 ReAct 工具调用循环，在沙箱环境中执行具体任务步骤。

本节点采用 "Plan-and-Execute Lite" 架构：每次调用只处理 task_dag 中 current_step_idx
指向的单个节点。主图控制 idx 推进，实现有序的多步执行。

核心机制：
1. 混合工具链：本地文件系统工具 + AIO Sandbox MCP 工具，保证执行隔离。
2. 上下文隔离：针对 DeepSeek 前缀缓存优化 —— 静态 prompt（SOP、学术诚信、
   profile）与动态任务状态（当前 step、历史简报、反馈）解耦。
3. 状态推进：每次执行后强制推进索引；完整性校验交给下游 Verifier 节点。
4. 故障感知：Replan 循环期间 Coder 会收到上一轮失败详情与终稿，避免重复同样的错误。
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from agents.errors import SandboxFatalError
from memory.profile import inject_for_agent
from orchestrator.state import HwState
from config.prompts import ACADEMIC_INTEGRITY_PROMPT, CODER_BASE_PROMPT
from config.runtime import get_settings
from tools.sandbox_tools import reset_sandbox_failure_counter


def _build_system_prompt(state: HwState) -> str:
    """构建 Coder system prompt —— 只含**静态/半静态**内容，以命中 DeepSeek 前缀缓存。

    内容（每次调用稳定）：
      1. CODER_BASE_PROMPT          —— 纯静态
      2. ACADEMIC_INTEGRITY_PROMPT  —— 按 skill 条件追加（任务内稳定）
      3. skill SOP 正文             —— 按 skill 加载（任务内稳定）
      4. profile 注入               —— 按用户稳定

    动态内容（intake / user_constraints / lessons / verifier 反馈 / task_dag）
    放进 _build_context_user_message，作为第一条 HumanMessage 发送。
    """
    intake = state.get("intake_result") or {}
    task_dag = state.get("task_dag") or {}

    parts = [CODER_BASE_PROMPT]

    # 学术诚信约束（写作类 skill 启用）
    skill = (task_dag.get("skill") or intake.get("type") or "").lower()
    if skill in {"essay", "lab_report"}:
        parts.append(ACADEMIC_INTEGRITY_PROMPT)

    # skill SOP
    if skill in {"coding", "essay", "lab_report"}:
        try:
            from tools.skill_tool import get_skill_body
            body = get_skill_body(skill) or ""
            if body:
                parts.append(f"## Current skill SOP ({skill})\n{body}")
        except Exception:
            pass

    base = "\n\n".join(parts)
    # Profile 注入（identity + writing_style + coding_style）
    return inject_for_agent("coder", base)


def _parse_step_status(final: str) -> tuple[str, str]:
    """从 Final Answer 开头解析 step 状态。

    格式契约（CODER_BASE_PROMPT）：`step <id> <status>: <一句话简报>`，
    status 取值 {done, needs_retry}。
    返回：(status, reason)；无匹配时默认 ("done", "")，以兼容旧版
    （旧 prompt 只要求 'done' 前缀；缺失产物仍由 Verifier 捕获）。
    """
    m = re.search(r"step\s+\S+\s+(done|needs_retry)\s*[:：]\s*(.*)", final or "")
    if not m:
        return "done", ""
    status = m.group(1)
    reason = m.group(2).strip().splitlines()[0][:200] if m.group(2).strip() else ""
    return status, reason


def _prev_round_final_answer(
    step_outputs: list[dict[str, Any]],
    current_iteration: int,
    max_chars: int = 800,
) -> str:
    """取**上一轮**（Replan 之前）最后一个 step 的 Final Answer 摘要。

    供 Replan 后的 Coder 了解上轮实现长什么样，避免重复同一种写法。
    找不到（首轮 / 无更早条目）返回 ""。

    踩坑记录：本函数原先逆序扫 state["messages"] 找最后一条 assistant 消息。
    但 messages 在同一轮内会被前一个 coder_step 写入，于是从本轮第 2 个 step 起，
    注入给 Coder 的「上一轮终稿（避免重复实现）」实际是**本轮上一步刚正确写完**
    的东西——语义正好反了，等于主动投毒。改为按 iteration 严格取更早轮次的
    step_output.summary。
    """
    for o in reversed(step_outputs or []):
        if int(o.get("iteration", 0)) >= current_iteration:
            continue  # 本轮（及更晚）的条目不是"上一轮终稿"
        text = str(o.get("summary") or "").strip()
        if text:
            return text if len(text) <= max_chars else text[:max_chars] + "...(截断)"
    return ""


def _attempt_no(state: HwState, step_id: str) -> int:
    """该 step 在**本轮内**的尝试序号（1 基）。

    只数 iteration 与当前轮相同的历史条目：planner 的防撞后缀在修订路径
    （session.prepare_task 把 iteration 重置为 0）可能跨轮重复生成同一个 id，
    若按全历史计数，新 step 的尝试数一开跑就可能 ≥ MAX_STEP_RETRY，
    首次 needs_retry 即被判 failed → step_router 短路 → 本轮剩余 step 全跳过。
    """
    cur_iter = int(state.get("iteration", 0))
    return 1 + sum(
        1
        for o in (state.get("step_outputs") or [])
        if o.get("id") == step_id and int(o.get("iteration", 0)) == cur_iter
    )


def _format_dag_with_focus(
    task_dag: dict[str, Any],
    step_outputs: list[dict[str, Any]],
    current_idx: int,
    current_iteration: int,
) -> str:
    """全局视野 + 高亮当前 step + 已完成 step 标 done。

    Plan-and-Execute Lite：供单步 Coder 执行用的 task_dag 视图。
    标签规则：
      - i == current_idx               -> [>> current]
      - n.id 有**本轮** status=done 的 step_output -> [done]
      - 其他                            -> [pending]

    done 判定按 (id, iteration) 配对：step_outputs 跨轮累积，只按 id 匹配时，
    上一轮同名 step 的 done 记录会把本轮尚未执行的节点误标成 [done]，
    让 Coder 以为该步已完成而跳过。
    """
    nodes = task_dag.get("nodes") or []
    if not nodes:
        return "## task_dag global view\n(Planner produced no DAG)"
    done_ids = {
        o.get("id")
        for o in step_outputs
        if o.get("status") == "done" and int(o.get("iteration", 0)) == current_iteration
    }
    lines = ["## task_dag global view"]
    for i, n in enumerate(nodes):
        if i == current_idx:
            tag = "[▶ current]"
        elif n.get("id") in done_ids:
            tag = "[done]"
        else:
            tag = "[pending]"
        deps = n.get("depends_on") or []
        deps_s = f" ← {deps}" if deps else ""
        lines.append(
            f"- {tag} **{n.get('id')}** {n.get('name','')}{deps_s}: {n.get('desc','')}"
        )
    return "\n".join(lines)


def _format_current_step_detail(node: dict[str, Any]) -> str:
    """当前 step 详情（acceptance_criteria + expected_artifacts + suggested_tools）。

    告诉 Coder：这轮做什么 + 满足什么条件才算完成 + 优先用哪些工具。
    """
    parts = [
        f"## This round you only do step {node.get('id')} ({node.get('name','')})",
        f"### Description\n{node.get('desc','')}",
    ]
    ac = node.get("acceptance_criteria") or []
    if ac:
        parts.append(
            "### Completion criteria (acceptance_criteria; send Final Answer only when ALL are met)\n"
            + "\n".join(f"- {c}" for c in ac)
        )
    art = node.get("expected_artifacts") or []
    if art:
        parts.append("### Expected artifact files\n" + "\n".join(f"- {f}" for f in art))
    st = node.get("suggested_tools") or []
    if st:
        parts.append(f"### Suggested priority tools\n{', '.join(st)}")
    return "\n\n".join(parts)


def _format_completed_step_outputs(step_outputs: list[dict[str, Any]]) -> str:
    """已完成 step 的简报（仅供参考，不要重做）。"""
    if not step_outputs:
        return ""
    lines = ["## Briefs of completed steps (reference only, do not redo)"]
    for o in step_outputs:
        summ = (o.get("summary") or "").strip().splitlines()
        first = summ[0] if summ else ""
        first = first[:200]
        err = o.get("error") or ""
        if o.get("status") == "needs_retry":
            # 原地重试：让该 step 的下一轮 Coder 看到上次的卡点
            lines.append(
                f"- **{o.get('id')}** {o.get('name','')}: "
                f"[retry] {o.get('retry_reason', '') or first}"
            )
        elif err:
            lines.append(
                f"- **{o.get('id')}** {o.get('name','')}: [error] {err[:120]}"
            )
        else:
            lines.append(
                f"- **{o.get('id')}** {o.get('name','')}: {first}"
            )
    return "\n".join(lines)


def _format_verifier_feedback(verifier_runs: list[dict[str, Any]]) -> str:
    """上一轮 Verifier 反馈（让 Coder 在 Replan 时看到具体失败点）。"""
    if not verifier_runs:
        return ""
    last = verifier_runs[-1]
    parts = [f"- verdict: {last.get('verdict','')}"]
    s1 = last.get("stage1_failures") or []
    if s1:
        parts.append("- Stage 1 hard metric failures:")
        parts.extend(f"  - {x}" for x in s1)
    cov = last.get("coverage") or {}
    missing = cov.get("missing") or []
    if missing:
        parts.append("- Uncovered constraints:")
        for m in missing:
            c = m.get("constraint", "") if isinstance(m, dict) else str(m)
            r = m.get("reason", "") if isinstance(m, dict) else ""
            parts.append(f"  - {c} ({r})" if r else f"  - {c}")
    sf = last.get("suggested_fix") or ""
    if sf:
        parts.append(f"- Suggested fix: {sf}")
    return "\n".join(parts)


def _check_dependency_artifacts(
    current_node: dict[str, Any],
    all_nodes: list[dict[str, Any]],
    step_outputs: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """检查当前 step 的 depends_on 依赖。返回：(blockers, warnings)。

    - blockers（硬跳过）：依赖 step 未在 DAG 中定义 / 依赖 step 报 error。
      调用方跳过当前 step，避免 Coder 在沙上建塔上浪费 ReAct 迭代。
    - warnings（仅提示）：依赖 step 已 done 但声明的 expected_artifacts 不在盘上。
      depends_on 现在是单链；LLM 输出的命名漂移很常见。硬跳过会连锁杀死所有
      下游 step，烧掉一整轮 Replan。改为注入上下文，让 Coder 先核实实际文件名。
    """
    deps = current_node.get("depends_on") or []
    if not deps:
        return [], []

    node_map = {n.get("id"): n for n in all_nodes}
    output_map = {o.get("id"): o for o in step_outputs}
    ws = get_settings().workspace_dir
    blockers: list[str] = []
    warnings: list[str] = []

    for dep_id in deps:
        dep_node = node_map.get(dep_id)
        if not dep_node:
            blockers.append(f"依赖 step {dep_id} 未在 DAG 中定义")
            continue

        dep_out = output_map.get(dep_id)
        if dep_out and dep_out.get("error"):
            blockers.append(
                f"{dep_id} 执行失败（{dep_out['error']}），"
                f"当前 step {current_node.get('id')} 依赖其产出"
            )
            # 有 error 说明文件一定不存在；无需查盘
            continue

        # 预期产物不在盘上 -> 注入上下文的警告（不阻塞）
        for art in dep_node.get("expected_artifacts") or []:
            if not (ws / art).exists():
                warnings.append(f"preceding step {dep_id} declared artifact {art} not found on disk")

    return blockers, warnings


def _detect_sandbox_fatal(messages: list[Any]) -> str:
    """扫描 agent 消息列表中的 `[SANDBOX_UNREACHABLE]` 致命标记。
    返回含该标记的 ToolMessage content（空字符串 = 未触发）。"""
    marker = "[SANDBOX_UNREACHABLE]"
    for m in messages:
        content = getattr(m, "content", "") or ""
        if isinstance(content, str) and marker in content:
            first_line = content.split("\n")[0][:200]
            return first_line
    return ""


def _build_context_user_message(state: HwState, current_idx: int) -> str:
    """构建 Coder 的第一条 HumanMessage —— Plan-and-Execute 单步视野。

    给 Coder 全局可见性（看到所有 step）但**只允许做 current_idx**：
    - 全局视野：每个 step 打 [done] / [>> current] / [pending] 标签
    - 当前 step 详情：desc + acceptance_criteria + expected_artifacts + suggested_tools
    - 已完成 step 简报：仅供历史参考（不要重做）

    DeepSeek 前缀缓存：动态部分（current_idx / step_outputs / user_constraints /
    verifier 反馈）放进 user message，system 段保持稳定前缀。
    """
    intake = state.get("intake_result") or {}
    user_constraints = state.get("user_constraints") or []
    task_dag = state.get("task_dag") or {}
    step_outputs = state.get("step_outputs") or []
    verifier_runs = state.get("verifier_runs") or []
    current_iteration = int(state.get("iteration", 0))
    nodes = task_dag.get("nodes") or []
    current_node = nodes[current_idx] if 0 <= current_idx < len(nodes) else {}

    parts: list[str] = []

    # 任务概要
    parts.append(
        "## Task summary\n"
        f"- Title: {intake.get('title','')}\n"
        f"- Type: {intake.get('type','')}\n"
        f"- Deliverables: {intake.get('deliverables') or '(not specified, please infer reasonably)'}\n"
        f"- Problem constraints: {intake.get('constraints') or '(none)'}"
    )

    # 全局视野 + 当前 step 高亮
    parts.append(
        _format_dag_with_focus(task_dag, step_outputs, current_idx, current_iteration)
    )

    # 当前 step 详情（必含：acceptance_criteria / expected_artifacts / suggested_tools）
    if current_node:
        parts.append(_format_current_step_detail(current_node))

    # 已完成 step 简报（仅供参考）
    completed = _format_completed_step_outputs(step_outputs)
    if completed:
        parts.append(completed)

    # 用户补充约束
    if user_constraints:
        parts.append("## User supplementary constraints\n- " + "\n- ".join(user_constraints))

    # 历史经验卡片（与当前 step 相关的 pattern/strategy）
    step_cards = current_node.get("context_cards") or []
    if step_cards:
        parts.append("## Relevant experience cards\n- " + "\n- ".join(step_cards))

    # Replan 附件：上一轮 Verifier 反馈 + 上一轮 Coder 终稿
    if verifier_runs:
        fb = _format_verifier_feedback(verifier_runs)
        if fb:
            parts.append(
                "## Previous Verifier feedback (the whole task failed and was Replanned, a new DAG was re-decomposed; "
                "this block is only for you to understand the original pain points, not feedback on the current step)\n" + fb
            )
        last_final = _prev_round_final_answer(step_outputs, current_iteration)
        if last_final:
            parts.append(
                "## Previous Coder final draft (avoid repeating the same implementation)\n" + last_final
            )

    return "\n\n".join(parts)


# 模块级缓存：避免每个 coder step 都重建 agent（并重加载 MCP 工具）。
_cached_agent: Any | None = None


async def get_coder_agent() -> Any:
    """返回缓存的 React agent；首次调用或 reset 后构建。"""
    global _cached_agent
    if _cached_agent is None:
        _cached_agent = await build_coder_agent()
    return _cached_agent


def reset_coder_agent() -> None:
    """失效缓存的 agent（沙箱重建时调用）。"""
    global _cached_agent
    _cached_agent = None


async def build_coder_agent() -> Any:
    """构建 React Agent（langchain.agents.create_agent）。异步：需 await sandbox_tools.get_sandbox_tools()。"""
    from langchain.agents import create_agent

    # 本地工具子集（仅 Coder 需要的）
    from tools.fs_tools import (
        host_bash, list_dir, patch_file, read_file, write_file,
    )
    from tools.skill_tool import (
        list_skills, load_skill, load_skill_reference, use_skill_script,
    )
    from tools.profile_tool import read_profile
    from tools.search_tool import web_search

    local_tools = [
        read_file, write_file, list_dir, patch_file, host_bash,
        load_skill, list_skills, load_skill_reference, use_skill_script, read_profile,
        # web_search：host 端 ddgs，继承宿主 shell 代理（HTTPS_PROXY / ALL_PROXY），
        # 比容器内 browser_* 更稳（容器不能翻墙，被墙站点会 ERR_CONNECTION_REFUSED）
        web_search,
    ]

    # 沙箱工具（异步加载）
    sandbox_tools: list[Any] = []
    try:
        from tools.sandbox_tools import get_sandbox_tools
        sandbox_tools = await get_sandbox_tools()
    except Exception as e:
        # 沙箱不可用；agent 会在首次工具调用时优雅失败
        pass

    all_tools = local_tools + sandbox_tools
    llm = get_llm()
    return create_agent(llm, all_tools)


async def _prepare_coder_execution(
    state: HwState, idx: int
) -> tuple[Any, str, str, str] | dict[str, Any]:
    """预检 + 构建 agent 调用前所需的全部输入。

    返回二者之一：
      - dict  → 提前退出（越界 / 依赖阻塞）；调用方直接返回它
      - tuple → (agent, system_prompt, context_block, user_question)
    """
    nodes = (state.get("task_dag") or {}).get("nodes") or []

    # 安全：越界防护（step_router 也会查，但纵深防御）
    if idx >= len(nodes):
        return {
            "current_step_idx": idx,
            "progress_log": [
                {"node": "coder_step", "skipped": "idx_out_of_range",
                 "idx": idx, "n_nodes": len(nodes)}
            ],
        }

    current = nodes[idx]

    # 依赖预检：存在 blockers 时跳过 step（上游 error / 节点缺失）
    blockers, dep_warnings = _check_dependency_artifacts(
        current, nodes, state.get("step_outputs") or []
    )
    if blockers:
        reason = "；".join(blockers)
        step_id = current.get("id", f"n{idx+1}")
        return {
            "current_step_idx": idx + 1,
            "step_outputs": [{
                "id": step_id,
                "name": current.get("name", ""),
                "iteration": int(state.get("iteration", 0)),
                "attempt": _attempt_no(state, step_id),
                "status": "failed",
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

    # 重置沙箱连续失败计数（每个 step 独立计数）
    reset_sandbox_failure_counter()

    agent = await get_coder_agent()  # 异步：首次调用加载沙箱 MCP 工具
    system_prompt = _build_system_prompt(state)
    context_block = _build_context_user_message(state, current_idx=idx)
    if dep_warnings:
        context_block += (
            "\n\n## Dependency artifact warning (preceding step is done but the following declared artifacts were not found on disk; "
            "most likely filename drift, please list_dir first to verify actual filenames before continuing)\n"
            + "\n".join(f"- {w}" for w in dep_warnings)
        )
    user_question = (
        f"Please complete the current step (id={current.get('id','?')}, name={current.get('name','')}). "
        "Remember the Final Answer must start with 'step <id> done: <one line>'; "
        "when not meeting the bar, honestly start with 'step <id> needs_retry: <reason>'; do not pretend done."
    )
    return agent, system_prompt, context_block, user_question


def _sum_usage(msg_dicts: list[dict[str, Any]]) -> dict[str, int]:
    """汇总本步 ReAct 循环的 token 真值，转成 progress_log 字段。

    Coder 不走 llm.invoke（它由 create_agent 内部调用 LLM），所以账要从每条
    AIMessage 携带的 usage_metadata 里加出来，字段名与 llm.invoke.usage_log_fields
    保持一致 —— progress_log 是全任务 token 的**单一账本**，benchmark 只读它，
    不必在 messages 与 progress_log 之间去重。
    无真值时返回空 dict（调用方直接 update）。
    """
    total = prompt = cached = 0
    for m in msg_dicts:
        u = m.get("usage_metadata") or {}
        total += int(u.get("total_tokens") or 0)
        prompt += int(u.get("input_tokens") or 0)
        cached += int((u.get("input_token_details") or {}).get("cache_read") or 0)
    if not total:
        return {}
    return {"tokens": total, "prompt_tokens": prompt, "cached_tokens": cached}


def _parse_coder_result(
    msgs: list[Any],
    final_answer: str,
    current_step: dict[str, Any],
    idx: int,
    state: HwState,
) -> dict[str, Any]:
    """将 agent 调用结果解析为 state diff dict。

    提取 lessons，检查沙箱致命错误，判定 step 状态
    （done / needs_retry / 重试耗尽后 failed），并构建
    step_outputs + progress_log 条目。

    沙箱不可达时抛 SandboxFatalError（向 CLI 传播）。
    """
    # 从 Final Answer 提取 Lessons 段
    step_lessons: list[str] = []
    if "Lessons:" in final_answer:
        parts = final_answer.split("Lessons:")
        if len(parts) > 1:
            lesson_block = parts[-1].strip()
            for line in lesson_block.split("\n"):
                line = line.strip().lstrip("-").strip()
                if line:
                    step_lessons.append(line)

    # 沙箱致命检查：连续失败 → 抛专用异常给 CLI
    sandbox_fatal = _detect_sandbox_fatal(msgs)
    if sandbox_fatal:
        raise SandboxFatalError(sandbox_fatal)

    # 有界原地重试：解析 Final Answer 状态决定推进还是重跑
    step_id = current_step.get("id", f"n{idx+1}")
    cur_iter = int(state.get("iteration", 0))
    status, retry_reason = _parse_step_status(final_answer)
    attempt = _attempt_no(state, step_id)      # 本轮内第几次尝试（1 基）
    prior_attempts = attempt - 1
    max_retry = get_settings().max_step_retry
    if status == "needs_retry" and prior_attempts >= max_retry:
        # 重试预算耗尽：标 failed，推进，残留缺口 → Verifier
        status = "failed"

    step_out: dict[str, Any] = {
        "id": step_id,
        "name": current_step.get("name", ""),
        "iteration": cur_iter,
        "attempt": attempt,
        "status": status,
        "summary": (final_answer or "(no final answer)")[:500],
        "iter_messages": len(msgs),
        "step_lessons": step_lessons,
    }
    if status == "needs_retry":
        step_out["retry_reason"] = retry_reason or "Coder 报本步未达标"
    elif status == "failed":
        step_out["error"] = f"重试 {prior_attempts} 次后仍未达标：{retry_reason or '(无原因)'}"

    # messages 是归约字段，这里返回**本步增量**：
    # - 剔除 SystemMessage：15k+ 字符的静态前缀，每步各留一份会逐 superstep
    #   全量重序列化进 checkpoint，而下游（transcript / tool_history / summarizer）
    #   没有任何消费者需要它
    # - 每条打 step_id / iteration 归属标记：messages 现在跨 step 累积，
    #   没有归属就无法区分「用户真实输入」与「Coder 单步上下文块」，
    #   也无法在 transcript 里定位某次工具调用属于哪一步
    msg_increment = [
        {**_msg_to_dict(m), "step_id": step_id, "iteration": cur_iter}
        for m in msgs
        if m.__class__.__name__ != "SystemMessage"
    ]

    next_idx = idx if status == "needs_retry" else idx + 1
    step_log: dict[str, Any] = {
        "node": "coder_step",
        "step_id": step_id,
        "step_idx": idx,
        "status": status,
        "attempt": attempt,
        "n_messages": len(msgs),
        "final_excerpt": (final_answer or "")[:200],
    }
    step_log.update(_sum_usage(msg_increment))

    return {
        "messages": msg_increment,
        "current_step_idx": next_idx,
        "step_outputs": [step_out],
        "progress_log": [step_log],
    }


async def _run_coder_async(state: HwState) -> dict[str, Any]:
    """Plan-and-Execute Lite 单步入口。

    每次调用只跑 task_dag.nodes[current_step_idx]。主图的 step_router
    决定是回环还是推进到 verifier。

    重试策略（有界原地）：
    - Final Answer 报 done → idx+1
    - needs_retry 且 attempts < MAX_STEP_RETRY → 同 idx 重跑
    - needs_retry 但重试耗尽 → 标 failed，idx+1（缺口 → Verifier/Replan）

    编排：prepare → execute → parse。
    """
    idx = int(state.get("current_step_idx", 0))

    # 阶段 1：预检 + 构建输入
    prepared = await _prepare_coder_execution(state, idx)
    if isinstance(prepared, dict):
        return prepared  # 提前退出（越界 / 依赖阻塞）
    agent, system_prompt, context_block, user_question = prepared

    # 阶段 2：调用 agent
    try:
        result = await agent.ainvoke(
            {
                "messages": [
                    # 静态前缀（跨 REPL/Replan 命中前缀缓存）
                    SystemMessage(content=system_prompt),
                    # 动态任务上下文（DAG 视野 + 当前 step + 历史 + replan 反馈）
                    HumanMessage(content=context_block),
                    # 当前 step 触发问题
                    HumanMessage(content=user_question),
                ]
            },
            config={"recursion_limit": get_settings().max_react_iter * 8},
        )
        msgs = result.get("messages", [])
        final = ""
        for m in reversed(msgs):
            cls = m.__class__.__name__
            if cls in {"AIMessage", "AIMessageChunk"}:
                final = m.content if isinstance(m.content, str) else str(m.content)
                break

        # 阶段 3：将结果解析为 state diff
        nodes = (state.get("task_dag") or {}).get("nodes") or []
        current = nodes[idx]
        return _parse_coder_result(msgs, final, current, idx, state)

    except SandboxFatalError:
        # 致命错误直接传播到 CLI（图节点不能杀进程）
        raise
    except Exception as e:
        # 异常也推进（防无限循环）；error 记录下来交 Verifier
        nodes = (state.get("task_dag") or {}).get("nodes") or []
        current = nodes[idx]
        step_id = current.get("id", f"n{idx+1}")
        return {
            "current_step_idx": idx + 1,
            "step_outputs": [{
                "id": step_id,
                "name": current.get("name", ""),
                "iteration": int(state.get("iteration", 0)),
                "attempt": _attempt_no(state, step_id),
                "status": "failed",
                "summary": "",
                "error": f"{type(e).__name__}: {e}",
            }],
            "progress_log": [
                {"node": "coder_step", "step_id": step_id,
                 "step_idx": idx, "error": f"{type(e).__name__}: {e}"}
            ],
        }


# tool 消息内容写进 state 前的字节预算。
# messages 是归约字段，逐 superstep 全量重序列化进 checkpoint；不设上限时
# read_file / 沙箱原样转发的长输出会让 checkpoint 体积随 step 数二次膨胀。
# 8000 足够事后读懂一次工具返回在做什么；完整内容仍能在实时事件流里看到，
# 越界与拒绝记录另有 workspace/.labhandler/audit.jsonl。
_TOOL_CONTENT_BUDGET = 8000


def _msg_to_dict(m: Any) -> dict[str, Any]:
    """LangChain BaseMessage -> 纯 dict（供 HwState.messages 序列化）。"""
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
    text = content if isinstance(content, str) else str(content)
    if role == "tool" and len(text) > _TOOL_CONTENT_BUDGET:
        dropped = len(text) - _TOOL_CONTENT_BUDGET
        text = text[:_TOOL_CONTENT_BUDGET] + f"\n…[truncated {dropped} chars]"
    out["content"] = text
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        out["tool_calls"] = tcs
    ak = getattr(m, "additional_kwargs", None) or {}
    if ak.get("reasoning_content"):
        out["reasoning_content"] = ak["reasoning_content"]
    # token 真值（langchain 标准字段 AIMessage.usage_metadata）。
    # 没有它，benchmark 只能按 len/4 估算（tokens_exact=False，实测低估约一个数量级）；
    # 前提是 provider 侧开了 stream_usage —— 见 llm/provider.py。
    usage = getattr(m, "usage_metadata", None)
    if usage:
        out["usage_metadata"] = dict(usage)
    if cls == "ToolMessage":
        out["tool_call_id"] = getattr(m, "tool_call_id", "")
        out["name"] = getattr(m, "name", "")
    return out


async def run_coder_step(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口（异步）—— Plan-and-Execute Lite 单步执行。
    
    必须异步：LangGraph 用 stream_mode='messages' 时，依赖 contextvar 把内部 LLM
    token 块传给父图。用 asyncio.run 包裹会创建隔离的事件循环，contextvar 跨不过去，
    Coder 阶段就完全没有流式输出（看似'卡住'但 agent 其实在跑）。
    
    主图的 step_router（orchestrator/graph.py）在每次运行后决定是回环到 coder_step
    跑下一个 step 还是推进到 verifier；本函数只跑 task_dag.nodes[current_step_idx]。
    """
    return await _run_coder_async(state)
