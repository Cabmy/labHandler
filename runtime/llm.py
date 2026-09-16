"""LLM 入口：Chat / Embedding 走 OpenAI 兼容网关。

瞬时失败分类后原样返回 ChatResult.error_class；退避与放弃由 control.decide
按 Task 剩余步数和墙钟决定。本层 max_retries=0。

熔断与 sandbox_tools 同构：连续瞬时失败到 llm_breaker_max 就升级成 FATAL。
control.decide 见 FATAL 直接停——网关本身挂了的时候，换工具换思路的 nudge
是无效的，只会把 step_budget 和退避睡眠白烧完。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer, as_tracer


@dataclass
class ChatResult:
    content: str
    reasoning: str
    tool_calls: list[dict[str, str]]
    finish_reason: str | None
    usage: dict[str, int]
    error_class: ErrorClass = ErrorClass.OK

    @property
    def truncated(self) -> bool:
        """输出把剩余窗口写满了。内容和 tool_call 都可能是半截的。"""
        return self.finish_reason == "length"


class LLMGateway:
    """可注入的 LLM 入口。生命周期由调用方持有，与 Tracer / Settings 同寿。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        chat_client: AsyncOpenAI | None = None,
        embed_client: AsyncOpenAI | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self._consecutive_fail = 0
        self.tracer = as_tracer(tracer)
        self.chat_client = chat_client or AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            default_headers=settings.llm_default_headers,
            max_retries=0,
            timeout=120.0,
        )
        same_flash = (
            settings.flash_base_url == settings.llm_base_url
            and settings.flash_api_key == settings.llm_api_key
            and settings.flash_default_headers == settings.llm_default_headers
        )
        self.flash_client = (
            self.chat_client
            if same_flash
            else AsyncOpenAI(
                api_key=settings.flash_api_key,
                base_url=settings.flash_base_url,
                default_headers=settings.flash_default_headers,
                max_retries=0,
                timeout=120.0,
            )
        )
        self.embed_client = embed_client or AsyncOpenAI(
            api_key=settings.embedding_api_key or "empty",
            base_url=settings.embedding_base_url,
            max_retries=0,
            timeout=60.0,
        )

    def _client_for(self, model: str) -> AsyncOpenAI:
        if model == self.settings.flash_model:
            return self.flash_client
        return self.chat_client

    async def aclose(self) -> None:
        for client in {self.chat_client, self.flash_client}:
            close = getattr(client, "close", None)
            if callable(close):
                await close()

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str, bool], Awaitable[None]] | None = None,
        span_name: str = S.LLM,
    ) -> ChatResult:
        with self.tracer.span(
            span_name,
            kind=S.KIND_GENERATION,
            **{S.ATTR_MODEL: model, "messages": len(messages)},
        ) as span:
            try:
                result = await self._openai_chat(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    on_delta=on_delta,
                )
            except Exception as e:
                result = ChatResult(
                    content="",
                    reasoning="",
                    tool_calls=[],
                    finish_reason="error",
                    usage={},
                    error_class=classify(e),
                )
            result.error_class = self._breaker(result.error_class)
            span.set(
                **{
                    S.ATTR_TOKENS_IN: result.usage.get("input_tokens", 0),
                    S.ATTR_TOKENS_OUT: result.usage.get("output_tokens", 0),
                    S.ATTR_TOKENS_REASONING: result.usage.get("reasoning_tokens", 0),
                    S.ATTR_FINISH_REASON: result.finish_reason,
                    S.ATTR_ERROR_CLASS: result.error_class.value,
                }
            )
            span.output(
                {
                    "tool_calls": [tc.get("name") for tc in result.tool_calls],
                    "content_chars": len(result.content),
                    "truncated": result.truncated,
                }
            )
            return result

    def _breaker(self, error_class: ErrorClass) -> ErrorClass:
        """网关级熔断：连续瞬时失败到上限就升级成 FATAL，让 decide 直接停。

        计数跨 Task，因为网关是所有 Task 共用的资源；一次成功就清零。
        """
        if error_class is not ErrorClass.TRANSIENT:
            if error_class is ErrorClass.OK:
                self._consecutive_fail = 0
            return error_class
        self._consecutive_fail += 1
        if self._consecutive_fail < self.settings.llm_breaker_max:
            return error_class
        self.tracer.event(
            S.GUARDRAIL,
            **{S.ATTR_REASON: f"llm gateway failed {self._consecutive_fail} times in a row"},
        )
        return ErrorClass.FATAL

    async def _openai_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        max_tokens: int | None,
        on_delta: Callable[[str, bool], Awaitable[None]] | None,
    ) -> ChatResult:
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
            stream = await self._client_for(model).chat.completions.create(**kwargs)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    details = getattr(u, "completion_tokens_details", None)
                    usage = {
                        "input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                        "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                        "reasoning_tokens": int(getattr(details, "reasoning_tokens", 0) or 0),
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
                        slot = acc.setdefault(tc.index or 0, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        fn = tc.function
                        if fn is not None:
                            if fn.name:
                                slot["name"] += fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments

        return ChatResult(
            content=content,
            reasoning=reasoning,
            tool_calls=[acc[i] for i in sorted(acc) if acc[i].get("name")],
            finish_reason=finish_reason,
            usage=usage,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._sem:
            resp = await self.embed_client.embeddings.create(
                model=self.settings.embedding_model,
                input=texts,
            )
        return [list(item.embedding) for item in resp.data]
