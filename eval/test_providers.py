"""供应商配置与角色路由回归；MockTransport 检查实际 HTTP 请求。"""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from config.runtime import ConfigError, get_settings
from runtime.llm import LLMGateway


@pytest.fixture
def configured(monkeypatch):
    values = {
        "LLM_BASE_URL": "https://planner.example/v1", "LLM_API_KEY": "planner-key",
        "PRO_MODEL": "same-model", "FLASH_MODEL": "same-model",
        "FLASH_BASE_URL": "https://worker.example/v1", "FLASH_API_KEY": "worker-key",
        "LLM_DEFAULT_HEADERS": '{"x-provider":"planner"}',
        "FLASH_DEFAULT_HEADERS": '{"x-provider":"worker"}',
        "EMBEDDING_BASE_URL": "https://vectors.example/v1", "EMBEDDING_API_KEY": "vector-key",
        "EMBEDDING_MODEL": "custom-vector-model", "EMBEDDING_DEFAULT_HEADERS": '{"x-provider":"vectors"}',
        "PRO_NATIVE_FORCED_TOOLS": "false", "FLASH_NATIVE_FORCED_TOOLS": "true",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.mark.parametrize("name", ["PRO_MODEL", "FLASH_MODEL", "LLM_BASE_URL", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL"])
def test_required_provider_configuration_has_no_vendor_fallback(configured, monkeypatch, name):
    monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match=name):
        get_settings()


def test_separate_endpoint_does_not_inherit_credentials(configured, monkeypatch):
    monkeypatch.setenv("FLASH_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="FLASH_API_KEY"):
        get_settings()


def test_header_defaults_are_provider_neutral(configured, monkeypatch):
    for name in ["LLM_DEFAULT_HEADERS", "FLASH_DEFAULT_HEADERS", "EMBEDDING_DEFAULT_HEADERS"]:
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_default_headers == settings.flash_default_headers == settings.embedding_default_headers == {}
    monkeypatch.setenv("LLM_DEFAULT_HEADERS", '{"x-tenant":"planner"}')
    get_settings.cache_clear()
    assert get_settings().flash_default_headers == {}
    monkeypatch.setenv("FLASH_BASE_URL", "")
    monkeypatch.setenv("FLASH_API_KEY", "")
    get_settings.cache_clear()
    shared = get_settings()
    assert shared.flash_base_url == shared.llm_base_url
    assert shared.flash_api_key == shared.llm_api_key
    assert shared.flash_default_headers == {"x-tenant": "planner"}
    monkeypatch.setenv("FLASH_DEFAULT_HEADERS", "{}")
    get_settings.cache_clear()
    assert get_settings().flash_default_headers == {}


def test_capabilities_do_not_depend_on_model_name(configured, monkeypatch):
    for model in ["arbitrary-model", "deepseek-custom", "another-provider/model"]:
        monkeypatch.setenv("FLASH_MODEL", model)
        get_settings.cache_clear()
        assert get_settings().flash_native_forced_tools is True
    monkeypatch.setenv("FLASH_NATIVE_FORCED_TOOLS", "false")
    get_settings.cache_clear()
    assert get_settings().flash_native_forced_tools is False


def test_custom_endpoints_headers_and_same_model_role_routing(configured, monkeypatch):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"object": "list", "model": body["model"],
                "data": [{"object": "embedding", "index": 0, "embedding": [1., 0.]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        delta = {"content": "OK"}
        if body.get("tools"):
            delta = {"tool_calls": [{"index": 0, "id": "call", "type": "function",
                "function": {"name": body["tools"][0]["function"]["name"], "arguments": "{}"}}]}
        event = {"id": "chunk", "object": "chat.completion.chunk", "created": 0,
                 "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

    def client(**kwargs):
        return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), **kwargs)

    monkeypatch.setattr("runtime.llm.AsyncOpenAI", client)

    async def run():
        gateway = LLMGateway(configured)
        try:
            assert gateway._client_for("pro") is not gateway._client_for("flash")
            for role in ["pro", "flash"]:
                result = await gateway.chat(model="same-model", role=role, messages=[{"role": "user", "content": "hello"}])
                assert result.content == "OK"
            assert await gateway.embed(["document"]) == [[1., 0.]]
            tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                     for name in ["submit_result", "other_tool"]]
            for role in ["pro", "flash"]:
                result = await gateway.chat(model="same-model", role=role, messages=[], tools=tools,
                    tool_choice={"type": "function", "function": {"name": "submit_result"}})
                assert result.tool_calls[0]["name"] == "submit_result"
        finally:
            await gateway.aclose()

    asyncio.run(run())
    assert [r.url.host for r, _ in requests] == ["planner.example", "worker.example", "vectors.example", "planner.example", "worker.example"]
    assert [r.headers["authorization"] for r, _ in requests[:3]] == ["Bearer planner-key", "Bearer worker-key", "Bearer vector-key"]
    assert [r.headers["x-provider"] for r, _ in requests[:3]] == ["planner", "worker", "vectors"]
    assert all("x-app" not in request.headers for request, _ in requests)
    assert requests[3][1]["tool_choice"] == "auto"
    assert len(requests[3][1]["tools"]) == 1
    assert requests[4][1]["tool_choice"]["function"]["name"] == "submit_result"
    assert len(requests[4][1]["tools"]) == 2
