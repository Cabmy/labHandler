"""统一的 LLM 调用辅助函数。

封装所有 LLM 调用点共用的通用模式：
1. 构建 [SystemMessage, HumanMessage] 列表
2. 调用 LLM（同步）
3. 从响应中提取字符串内容
4. 从 <result> 块解析 JSON
5. 按 agent 记账 token 用量（供节点写进自己的 progress_log 条目）
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from config.prompts import parse_result_json

# ─── token 用量记账 ────────────────────────────────────────────
#
# 每个 agent 节点调用一次 LLM 就在这里累加一次真值用量，节点收尾时用
# take_usage(agent) 取走并清零、写进自己的 progress_log 条目。
# progress_log 由此成为**全任务 token 的单一账本**（coder 侧汇总
# messages 的 usage_metadata 写同一个 key），benchmark 直接读它，
# 不必在 messages 与 progress_log 之间做去重。
#
# 按 agent 名分桶而不是用一个全局计数器：/dream 与 /edit_skill 走同一个
# invoke_llm_json 但**不在主图里跑**，它们不传 agent 就不记账，
# 避免其用量被下一次任务的节点误领。
_USAGE: dict[str, dict[str, int]] = {}


def _blank_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "n_llm_calls": 0,
    }


def record_usage(agent: str, resp: Any) -> None:
    """把一次 LLM 响应的 token 真值累加到该 agent 账上。

    agent 为空 -> 不记账（离线工具路径）。
    响应没带 usage_metadata（provider 未开 stream_usage，见 llm/provider.py）
    时只累加调用次数，token 保持 0，下游据此判定"真值不可用"。
    """
    if not agent:
        return
    bucket = _USAGE.setdefault(agent, _blank_usage())
    bucket["n_llm_calls"] += 1
    usage = getattr(resp, "usage_metadata", None) or {}
    bucket["prompt_tokens"] += int(usage.get("input_tokens") or 0)
    bucket["completion_tokens"] += int(usage.get("output_tokens") or 0)
    bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
    # 前缀缓存命中量：OpenAI 兼容端点在 input_token_details.cache_read 回传
    # 「本次 prompt 里有多少 input token 是从缓存读的」。
    # 命中率 = cached_tokens / prompt_tokens，是判断"静态前缀是否稳定"的唯一直接证据。
    details = usage.get("input_token_details") or {}
    bucket["cached_tokens"] += int(details.get("cache_read") or 0)


def take_usage(agent: str) -> dict[str, int]:
    """取走并清零某 agent 的累计用量（节点写 progress_log 前调用）。

    一个节点内多次调用 LLM（如 Verifier 的分批覆盖判官）会被合并成一条。
    """
    return _USAGE.pop(agent, _blank_usage())


def usage_log_fields(agent: str) -> dict[str, int]:
    """取走用量并转成 progress_log 字段；无真值时返回空 dict（调用方直接 update）。

    progress_log 是全任务 token 的**单一账本**，三个字段构成完整口径：
      tokens         总量（prompt + completion）
      prompt_tokens  输入侧总量
      cached_tokens  其中命中前缀缓存的部分
    """
    u = take_usage(agent)
    if not u["total_tokens"]:
        return {}
    return {
        "tokens": u["total_tokens"],
        "prompt_tokens": u["prompt_tokens"],
        "cached_tokens": u["cached_tokens"],
    }


def invoke_llm_json(
    llm: Any,
    system_prompt: str,
    user_content: str,
    *,
    agent: str = "",
) -> dict[str, Any]:
    """以 system+human 消息调用 LLM 并解析 JSON 响应。

    处理以下通用模式：
    1. 构建 [SystemMessage, HumanMessage] 列表
    2. 通过同步 ``llm.invoke(...)`` 调用 LLM
    3. 记账 token 用量（传了 agent 时）
    4. 从响应中提取字符串内容
    5. 通过 ``parse_result_json`` 从 ``<result>`` 块解析 JSON

    参数：
        llm：要调用的 LangChain Chat 模型实例。
        system_prompt：系统提示词字符串。
        user_content：用户/human 消息内容。
        agent：记账用的 agent 名（如 "planner"）。留空表示不记账
            —— 离线路径（/dream、/edit_skill）应留空，它们不属于任何一次主图运行。

    返回：
        从 LLM 响应解析出的 JSON dict。

    异常：
        ``llm.invoke`` 或 ``parse_result_json`` 抛出的任何异常。
        注意记账发生在解析**之前**：JSON 解析失败也已经花掉了 token，
        账要照记。
    """
    resp = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
    )
    record_usage(agent, resp)
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    return parse_result_json(content)


def extract_response_content(resp: Any) -> str:
    """从 LLM 响应消息中提取字符串内容。

    供需要原始文本（如自定义重试逻辑）的调用方使用，
    无需走 JSON 解析。
    """
    return resp.content if isinstance(resp.content, str) else str(resp.content)
