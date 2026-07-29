"""GraphEvent 事件流 — LangGraph astream 解析的唯一入口。

把主图执行过程解析成结构化事件序列（AsyncIterator[GraphEvent]），
供两类消费者复用同一套解析逻辑：
- ui/live_panel.stream_graph  → 渲染 rich 终端输出
- server/app.py               → 序列化为 SSE 推给 Web 前端

事件类型（kind）：
- node_start : 进入新节点（node 为归一化名，coder_step/react 子图统一为 coder）
- content    : LLM 流式文字块（payload: text, reasoning=bool）
- tool       : 工具调用完成（payload: name, args, content；args 为累积的原始 JSON 串）
- node_done  : 节点完成（payload: log = diff.progress_log 中属于该节点的条目）
- final      : 主图结束（payload: state = 合并全部 diff 后的最终 state, elapsed 秒）

解析约定（沿袭 live_panel 旧实现）：
1. 同时订阅 updates（节点完成 diff）与 messages（LLM/tool 消息）；subgraphs=True
   以获取 create_react_agent 子图事件，react 内部节点（model/tools 等）归一到 coder。
2. tool_call_chunks 按 chunk index 累积 args，待同一 id 的 ToolMessage 到达后统一发 tool 事件。
3. 归约字段（REDUCER_LIST_FIELDS）在合并 final state 时做 list 累加而非覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from orchestrator.state import REDUCER_LIST_FIELDS

# 主图节点名 → 归一化显示名（coder_step 与子图 coder 共享 label）
_NODE_CANONICAL = {"coder_step": "coder"}

# react_agent 子图内部节点 → 映射至主图 coder
_REACT_INTERNAL_NODES = {"agent", "model", "tools", "call_model", "call_tools"}


def canonical_node(node: str) -> str:
    return _NODE_CANONICAL.get(node, node)


@dataclass
class GraphEvent:
    kind: str                                   # node_start | content | tool | node_done | final
    node: str = ""                              # 归一化节点名（final 事件为空）
    payload: dict[str, Any] = field(default_factory=dict)


async def iter_graph_events(
    graph: Any,
    state: dict[str, Any] | None,
    recursion_limit: int = 80,
    config: dict[str, Any] | None = None,
) -> AsyncIterator[GraphEvent]:
    """执行主图并产出 GraphEvent 序列；最后一个事件恒为 final（含合并后的最终 state）。

    必须在 async 环境消费：主图含 async 节点（run_coder_step），且
    stream_mode='messages' 依赖同一事件循环的 contextvar 透传。
    config 可携带 checkpointer 的 {"configurable": {"thread_id": ...}}；
    recursion_limit 会被合并进 config。
    state=None 为断点续跑输入（graph.astream(None, config) 从 checkpoint 继续），
    此时 final 事件的 state 只含续跑期间的 diff——权威全量 state 请在续跑结束后
    用 graph.aget_state(config) 取。
    """
    final_state: dict[str, Any] = dict(state) if state else {}
    t0 = time.time()

    run_config: dict[str, Any] = dict(config or {})
    run_config["recursion_limit"] = run_config.get("recursion_limit", recursion_limit)

    last_node: str | None = None
    # chunk index → {name, args 累积字符串, id}，ToolMessage 到达后按 id 匹配
    chunk_buf: dict[int, dict[str, str]] = {}

    def _start_events(node: str) -> list[GraphEvent]:
        """节点切换时补 node_start 事件（按归一化名去重）。"""
        nonlocal last_node
        c = canonical_node(node)
        if c != last_node:
            last_node = c
            return [GraphEvent(kind="node_start", node=c)]
        return []

    def _resolve_node(parent: str | None, metadata: dict[str, Any] | None) -> str:
        """消息归属：react 子图内部节点统一归至主图 coder。"""
        if parent:
            return parent
        meta = (metadata or {}).get("langgraph_node", "") or ""
        if meta in _REACT_INTERNAL_NODES:
            return "coder"
        return meta or (last_node or "?")

    async for chunk in graph.astream(
        state,
        stream_mode=["updates", "messages"],
        config=run_config,
        subgraphs=True,  # 获取 create_react_agent 子图事件
    ):
        # subgraphs=True + 多 stream_mode: chunk = (namespace_tuple, mode, payload)
        # namespace 如 ('coder:uuid',)；空 tuple 表示主图层
        if not isinstance(chunk, tuple):
            continue
        if len(chunk) == 3:
            ns, mode, payload = chunk
        elif len(chunk) == 2:
            ns = ()
            mode, payload = chunk
        else:
            continue

        # 主图节点名：从 namespace 提取前缀（"coder:xxx" → "coder"）；空 ns 即主图层
        parent_node = str(ns[0]).split(":", 1)[0] if ns else None

        if mode == "updates":
            # 仅处理主图层（ns 为空）的节点完成事件，子图层跳过以避免重复
            if ns or not isinstance(payload, dict):
                continue
            for node, diff in payload.items():
                for ev in _start_events(node):
                    yield ev
                # 属于该节点的 progress_log 条目（渲染层拼 ✓ 摘要用）
                log_entries = [
                    e for e in (diff or {}).get("progress_log") or []
                    if e.get("node") in (node, canonical_node(node))
                ]
                yield GraphEvent(
                    kind="node_done",
                    node=canonical_node(node),
                    payload={"log": log_entries},
                )
                for k, v in (diff or {}).items():
                    # 归约字段（Annotated[list, add]）需累加而非覆盖，
                    # 否则 final_state 只保留最后一个节点 return 的增量，丢失历史。
                    if k in REDUCER_LIST_FIELDS and isinstance(v, list):
                        existing = final_state.get(k)
                        base = existing if isinstance(existing, list) else []
                        final_state[k] = base + v
                    else:
                        final_state[k] = v

        elif mode == "messages":
            # payload = (message_chunk, metadata)
            if not isinstance(payload, tuple) or len(payload) != 2:
                continue
            msg, metadata = payload
            node = canonical_node(_resolve_node(parent_node, metadata))
            cls = msg.__class__.__name__

            if cls in {"AIMessageChunk", "AIMessage"}:
                for ev in _start_events(node):
                    yield ev
                text = getattr(msg, "content", "")
                if isinstance(text, str) and text:
                    yield GraphEvent(
                        kind="content", node=node,
                        payload={"text": text, "reasoning": False},
                    )
                ak = getattr(msg, "additional_kwargs", None) or {}
                reasoning = ak.get("reasoning_content") or ""
                if isinstance(reasoning, str) and reasoning:
                    yield GraphEvent(
                        kind="content", node=node,
                        payload={"text": reasoning, "reasoning": True},
                    )
                # 累积 tool_call_chunks，待 ToolMessage 配对后统一 emit
                for tc in getattr(msg, "tool_call_chunks", None) or []:
                    idx = int(tc.get("index", 0) or 0)
                    st = chunk_buf.setdefault(idx, {"name": "", "args": "", "id": ""})
                    if tc.get("name"):
                        st["name"] = tc["name"]
                    if tc.get("id"):
                        st["id"] = tc["id"]
                    if tc.get("args"):
                        st["args"] += tc["args"]

            elif cls == "ToolMessage":
                tid = getattr(msg, "tool_call_id", "")
                name = getattr(msg, "name", "") or "tool"
                content = getattr(msg, "content", "")
                if not isinstance(content, str):
                    content = str(content)
                args_raw: Any = ""
                matched_idx: int | None = None
                for idx, st in chunk_buf.items():
                    if st.get("id") == tid:
                        args_raw = st.get("args", "")
                        if name == "tool" and st.get("name"):
                            name = st["name"]
                        matched_idx = idx
                        break
                for ev in _start_events(node):
                    yield ev
                yield GraphEvent(
                    kind="tool", node=node,
                    payload={"name": name, "args": args_raw, "content": content},
                )
                if matched_idx is not None:
                    chunk_buf.pop(matched_idx, None)
            # HumanMessage / SystemMessage 通常不出现在 messages 流中，忽略

    yield GraphEvent(
        kind="final",
        payload={"state": final_state, "elapsed": time.time() - t0},
    )
