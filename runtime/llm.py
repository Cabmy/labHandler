"""AsyncOpenAI 双客户端：Chat 走 AgentRouter，Embedding 走 Paratera。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.retry import with_backoff
from runtime.observe.tracer import Tracer
from runtime.observe import spans as S


@dataclass
class ChatResult:
    content: str
    reasoning: str
    tool_calls: list[dict[str, str]]
    finish_reason: str | None
    usage: dict[str, int]
    error_class: ErrorClass = ErrorClass.OK


class LLMGateway:
    """可注入的 LLM 入口。不用模块级单例。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        chat_client: AsyncOpenAI | None = None,
        embed_client: AsyncOpenAI | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(max(1, settings.llm_max_concurrency))
        self.chat_client = chat_client or AsyncOpenAI(
            api_key=settings.llm_api_key or "empty",
            base_url=settings.llm_base_url,
            default_headers=settings.llm_default_headers,
            max_retries=2,
            timeout=120.0,
        )
        self.embed_client = embed_client or AsyncOpenAI(
            api_key=settings.embedding_api_key or "empty",
            base_url=settings.embedding_base_url,
            max_retries=2,
            timeout=60.0,
        )
        self.tracer = tracer

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str, bool], Awaitable[None]] | None = None,
    ) -> ChatResult:
        async def _once() -> ChatResult:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if tools:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens

            content = ""
            reasoning = ""
            acc: dict[int, dict[str, str]] = {}
            usage: dict[str, int] = {}
            finish_reason: str | None = None

            async with self._sem:
                stream = await self.chat_client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        u = chunk.usage
                        usage = {
                            "input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                            "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                            "reasoning_tokens": int(
                                getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0)
                                or 0
                            ),
                        }
                    if not chunk.choices:
                        continue
                    ch = chunk.choices[0]
                    finish_reason = ch.finish_reason or finish_reason
                    delta = ch.delta
                    if delta is None:
                        continue
                    if delta.content:
                        content += delta.content
                        if on_delta:
                            await on_delta(delta.content, False)
                    extra = getattr(delta, "reasoning_content", None)
                    if not extra:
                        extra = (getattr(delta, "model_extra", None) or {}).get("reasoning_content")
                    if extra:
                        reasoning += extra
                        if on_delta:
                            await on_delta(extra, True)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            slot = acc.setdefault(
                                tc.index or 0, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.id:
                                slot["id"] = tc.id
                            fn = tc.function
                            if fn is not None:
                                if fn.name:
                                    slot["name"] += fn.name
                                if fn.arguments:
                                    slot["arguments"] += fn.arguments

            tool_calls = [
                acc[i] for i in sorted(acc) if acc[i].get("name")
            ]
            return ChatResult(
                content=content,
                reasoning=reasoning,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )

        try:
            result = await with_backoff(
                _once, attempts=self.settings.transient_retry_max
            )
        except Exception as e:
            cls = classify(e)
            return ChatResult(
                content="",
                reasoning="",
                tool_calls=[],
                finish_reason="error",
                usage={},
                error_class=cls,
            )
        if self.tracer:
            with self.tracer.span(
                S.LLM,
                **{
                    S.ATTR_MODEL: model,
                    S.ATTR_TOKENS_IN: result.usage.get("input_tokens", 0),
                    S.ATTR_TOKENS_OUT: result.usage.get("output_tokens", 0),
                    S.ATTR_TOKENS_REASONING: result.usage.get("reasoning_tokens", 0),
                },
            ):
                pass
        return result

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        async def _once() -> list[list[float]]:
            async with self._sem:
                resp = await self.embed_client.embeddings.create(
                    model=self.settings.embedding_model,
                    input=texts,
                )
            return [list(item.embedding) for item in resp.data]

        return await with_backoff(_once, attempts=self.settings.transient_retry_max)
