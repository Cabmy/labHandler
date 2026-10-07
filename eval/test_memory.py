"""真实 sqlite-vec + 临时数据库，embedding 用可控桩验证故障和索引边界。"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from config.runtime import get_settings
from memory.archive import TaskArchive
from memory.cards import card_path, content_sha256, read_card, write_card_file
from memory.db import connect
from memory.retrieve import index_card_ids, memory_search, prefetch_cards, reconcile_index, search_cards
from memory.vectors import VectorIndex


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ("LLM_API_KEY", "FLASH_API_KEY", "EMBEDDING_API_KEY"):
        monkeypatch.setenv(name, "test-only")
    monkeypatch.delenv("EVAL_DISABLE_PREFETCH", raising=False)
    get_settings.cache_clear()
    result = replace(get_settings(), memory_db_path=tmp_path / "memory.db",
                     cards_dir=tmp_path / "cards", workspace_dir=tmp_path / "workspace",
                     embedding_base_url="https://embed.test/v1", embedding_model="test",
                     memory_min_score=0.25)
    result.workspace_dir.mkdir()
    return result


class Embedder:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    async def embed(self, texts):
        self.calls.extend(texts)
        if self.fail and any(self.fail in text for text in texts):
            raise ConnectionError("test outage")
        return [[1., 0.] for _ in texts]

    async def chat(self, **kwargs):
        candidates = json.loads(kwargs["messages"][-1]["content"])["candidates"]
        return SimpleNamespace(tool_calls=[{
            "name": "submit_memory_select",
            "arguments": json.dumps({"verdicts": [
                {"index": c["index"], "relevant": True, "reason": "test fixture"}
                for c in candidates
            ]}),
        }])


def card(settings, cid, body="WAL 崩溃重放", **meta):
    return write_card_file({"card_id": cid, "task_id": 1, "card_type": "lesson",
                            "task_title": "MiniKV", "task_type": "coding", "content": body,
                            **meta}, settings)


def test_sqlite_vec_exact_scores_and_filters(settings):
    index = VectorIndex(settings)
    index.upsert("a", "a", [1., 0.])
    index.upsert("b", "b", [0.8, 0.6])
    index.upsert("c", "c", [-1., 0.])
    index.upsert("wrong-dim", "d", [1., 0., 0.])
    fp = {p: content_sha256(t) for p, t in [("a", "a"), ("b", "b"), ("c", "c"), ("wrong-dim", "d")]}
    hits = index.search([1., 0.], fp, 0.25)
    assert [p for p, _ in hits] == ["a", "b"]
    assert [s for _, s in hits] == pytest.approx([1., .8])
    fp["a"] = content_sha256("edited")
    assert [p for p, _ in index.search([1., 0.], fp, .25)] == ["b"]
    assert VectorIndex(replace(settings, embedding_model="different")).search([1., 0.], fp, .25) == []
    assert VectorIndex(replace(settings, embedding_base_url="https://else.test")).search([1., 0.], fp, .25) == []


@pytest.mark.parametrize("vector", [[], [0., 0.], [float("nan"), 1.], [float("inf"), 1.]])
def test_invalid_vectors_rejected(settings, vector):
    with pytest.raises(ValueError):
        VectorIndex(settings).upsert("bad", "bad", vector)


def test_metadata_changes_reindex_and_failure_drops_stale(settings):
    async def run():
        path = card(settings, 1)
        embedder = Embedder()
        assert (await reconcile_index(embedder, settings))["rebuilt"] == 1
        assert (await reconcile_index(embedder, settings))["rebuilt"] == 0
        card(settings, 1, task_title="changed title")
        assert (await reconcile_index(embedder, settings))["rebuilt"] == 1
        assert "changed title" in embedder.calls[-1]
        card(settings, 1, "FAIL updated")
        report = await reconcile_index(Embedder("FAIL"), settings)
        assert report["errors"] and report["dropped"] == 1
        assert str(path) not in VectorIndex(settings).all_rows()
        assert (await reconcile_index(Embedder(), settings))["rebuilt"] == 1
    asyncio.run(run())


def test_bad_card_does_not_block_cleanup_or_other_cards(settings):
    async def run():
        bad = card(settings, 1, "FAIL")
        good = card(settings, 2, "good")
        deleted = card(settings, 3, "delete")
        await reconcile_index(Embedder(), settings)
        deleted.unlink()
        bad.write_text("broken YAML", encoding="utf-8")
        card(settings, 2, "updated good")
        report = await reconcile_index(Embedder(), settings)
        assert report["errors"] and report["rebuilt"] == 1 and report["dropped"] == 3
        assert set(VectorIndex(settings).all_rows()) == {str(good)}
    asyncio.run(run())


def test_failed_embedding_continues_with_later_card(settings):
    async def run():
        card(settings, 1, "FAIL")
        good = card(settings, 2, "good")
        report = await reconcile_index(Embedder("FAIL"), settings)
        assert report["errors"] and report["rebuilt"] == 1
        assert set(VectorIndex(settings).all_rows()) == {str(good)}
        result = await prefetch_cards("query", Embedder("FAIL"), settings)
        assert result.errors and len(result.cards) == 1
    asyncio.run(run())


def test_empty_index_skips_embedding_and_failure_is_not_empty(settings):
    async def run():
        embedder = Embedder("query")
        result = await prefetch_cards("query", embedder, settings)
        assert not result.cards and not result.errors and not embedder.calls
        card(settings, 1)
        result = await prefetch_cards("query", embedder, settings)
        assert result.errors and not result.cards
        assert "ERROR/MemoryRetrieval" in await memory_search("query", 3, embedder, settings)
        result = await prefetch_cards("query", Embedder(), settings)
        assert result.cards and not result.errors
    asyncio.run(run())


def test_dedup_fills_top_k_beyond_first_eight(settings):
    async def run():
        for cid in range(1, 10):
            card(settings, cid, "same")
        card(settings, 10, "second")
        card(settings, 11, "third")
        result = await search_cards("query", 3, Embedder(), settings)
        assert not result.errors
        assert {c["content"] for _, _, c in result.hits} == {"same", "second", "third"}
    asyncio.run(run())


def test_manual_search_and_prefetch_share_threshold(settings):
    class Orthogonal(Embedder):
        async def embed(self, texts):
            return [[0., 1.] if t == "query" else [1., 0.] for t in texts]
    async def run():
        card(settings, 1)
        assert (await prefetch_cards("query", Orthogonal(), settings)).cards == []
        assert await memory_search("query", 8, Orthogonal(), settings) == "(no matches)"
    asyncio.run(run())


@pytest.mark.parametrize("action", ["delete", "edit"])
def test_inflight_embedding_cannot_publish_old_snapshot(settings, action):
    path = card(settings, 1)
    class Changing(Embedder):
        async def embed(self, texts):
            if action == "delete":
                path.unlink()
            else:
                card(settings, 1, "new")
            return [[1., 0.]]
    report = asyncio.run(reconcile_index(Changing(), settings))
    assert report["errors"]
    assert not VectorIndex(settings).all_rows()


def test_query_wait_filters_deleted_and_edited_cards(settings):
    async def run():
        path = card(settings, 1)
        await reconcile_index(Embedder(), settings)
        class Changing(Embedder):
            async def embed(self, texts):
                path.unlink()
                return [[1., 0.]]
        assert (await search_cards("query", 3, Changing(), settings)).hits == []
    asyncio.run(run())


def test_archive_uses_files_for_governance_dedup_and_reindex(settings):
    archive = TaskArchive(settings)
    task = archive.create_task("task", "coding", "summary")
    ids = archive.create_cards(task, [{"type": "lesson", "content": "old"}], "title: with colon", "coding")
    path = card_path(ids[0], settings)
    current = read_card(path)
    assert current["task_title"] == "title: with colon"
    write_card_file({**current, "content": "edited"}, settings)
    assert archive.get_all_active_cards()[0]["content"] == "edited"
    assert not archive.has_new_cards([{"type": "lesson", "content": "edited"}])
    assert archive.has_new_cards([{"type": "lesson", "content": "old"}])
    assert asyncio.run(index_card_ids(ids, Embedder(), settings))["indexed"] == 1
    assert read_card(path)["content"] == "edited"
    with connect(settings.memory_db_path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(archive_cards)")}
    assert not columns & {"content", "search_text", "content_hash", "vector_error"}
    assert archive.retire_cards(ids) == 1
    assert archive.get_all_active_cards() == []
    assert asyncio.run(prefetch_cards("query", Embedder(), settings)).cards == []


def test_material_context_contains_documents_and_conversion_but_not_control(settings, tmp_path):
    from runtime.lab.ingest import material_context
    ws = settings.workspace_dir
    (ws / "TASK.md").write_text("实现带 TTL 的键值存储", encoding="utf-8")
    (ws / "acceptance").mkdir()
    (ws / "acceptance" / "test_private.py").write_text("PRIVATE_GATE", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE", encoding="utf-8")
    (ws / "link.md").symlink_to(outside)
    session = ws / ".labhandler" / "session"
    converted = session / "converted"
    converted.mkdir(parents=True)
    (converted / "manual.pdf.md").write_text("PDF_REQUIREMENTS", encoding="utf-8")
    context = material_context(settings, session)
    assert "TTL" in context and "PDF_REQUIREMENTS" in context
    assert "OUTSIDE" not in context and "PRIVATE_GATE" not in context
    assert len(context) <= 6000


def test_failed_prefetch_is_not_cached_and_next_attempt_recovers(settings, tmp_path):
    from runtime.lab.flow import write_spec
    from runtime.lab.ingest import MATERIAL_CONTEXT_FILE
    from runtime.context.notes import load_cards
    class StopBeforePro(Exception):
        pass
    async def run():
        card(settings, 1)
        session = tmp_path / "session"
        session.mkdir()
        (session / MATERIAL_CONTEXT_FILE).write_text("TTL material", encoding="utf-8")
        events = []
        async def emit(event):
            events.append(event)
        async def drive(*args):
            raise StopBeforePro()
        runner = SimpleNamespace(settings=settings, llm=Embedder("query"))
        st = SimpleNamespace(halt=None, sandbox_down="", session_dir=session, question="query",
                             runner=runner, resume=False, emit=emit, drive_pro=drive, pro_history=[],
                             catalog="TASK.md", remember_block=lambda: "",
                             journal=SimpleNamespace(append_transcript=lambda *args: None))
        with pytest.raises(StopBeforePro):
            await write_spec(st)
        assert load_cards(session) is None
        assert events[0]["errors"]
        assert "User request:\nquery" in st.pro_history[0]["content"]
        assert "TASK.md" in st.pro_history[0]["content"]
        runner.llm = Embedder()
        with pytest.raises(StopBeforePro):
            await write_spec(st)
        assert len(load_cards(session)) == 1
        assert any("query" in t and "TTL material" in t for t in runner.llm.calls)
    asyncio.run(run())


def test_prefetch_timeout_releases_lock_for_retry(settings):
    class Hanging(Embedder):
        async def embed(self, texts):
            await asyncio.Event().wait()
    async def run():
        card(settings, 1)
        result = await prefetch_cards("query", Hanging(), replace(settings, tool_timeout_s=.02))
        assert result.errors and not result.cards
        result = await prefetch_cards("query", Embedder(), settings)
        assert not result.errors and result.cards
    asyncio.run(run())


def test_dream_reads_edited_files_and_preserves_sources_when_write_fails(settings, monkeypatch):
    import memory.dream as dream
    archive = TaskArchive(settings)
    task = archive.create_task("task", "coding", "summary")
    ids = archive.create_cards(task, [{"type": "lesson", "content": "one"},
                                     {"type": "lesson", "content": "two"}], "task", "coding")
    path = card_path(ids[0], settings)
    write_card_file({**read_card(path), "content": "edited fact"}, settings)
    seen = []
    async def judge(llm, ctype, ttype, cards):
        seen.extend(c["content"] for c in cards)
        return [{"type": "lesson", "content": "merged", "source_ids": ids}], ids[:], None
    def broken_create(*args):
        raise OSError("disk full")
    monkeypatch.setattr(dream, "get_settings", lambda: settings)
    monkeypatch.setattr(dream, "get_task_archive", lambda: archive)
    monkeypatch.setattr(dream, "_judge_group", judge)
    monkeypatch.setattr(archive, "create_cards", broken_create)
    report = asyncio.run(dream.run_dream(Embedder()))
    assert "edited fact" in seen and "one" not in seen
    assert report["errors"] and report["retired"] == 0
    assert all(card_path(cid, settings).is_file() for cid in ids)


def test_embedding_response_is_aligned_by_index(settings):
    from runtime.llm import LLMGateway
    from unittest.mock import AsyncMock
    async def run():
        gateway = object.__new__(LLMGateway)
        gateway.settings = settings
        gateway._sem = asyncio.Semaphore(1)
        create = AsyncMock(return_value=SimpleNamespace(data=[
            SimpleNamespace(index=1, embedding=[0., 1.]),
            SimpleNamespace(index=0, embedding=[1., 0.]),
        ]))
        gateway.embed_client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        assert await gateway.embed(["a", "b"]) == [[1., 0.], [0., 1.]]
        create.return_value = SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[1., 0.])])
        with pytest.raises(ValueError):
            await gateway.embed(["a"])
    asyncio.run(run())


def test_symlink_card_cannot_be_indexed(settings, tmp_path):
    card(settings, 1)
    path = card_path(1, settings)
    outside = tmp_path / "outside.md"
    path.replace(outside)
    path.symlink_to(outside)
    report = asyncio.run(reconcile_index(Embedder(), settings))
    assert report["errors"] and report["rebuilt"] == 0
    assert not VectorIndex(settings).all_rows()


def test_direct_indexing_rejects_symlink(settings, tmp_path):
    path = card(settings, 1)
    outside = tmp_path / "outside.md"
    path.replace(outside)
    path.symlink_to(outside)
    embedder = Embedder()
    report = asyncio.run(index_card_ids([1], embedder, settings))
    assert report["failed"] == 1 and not embedder.calls


def test_min_score_must_be_finite(monkeypatch):
    from config.runtime import ConfigError, _env_float
    monkeypatch.setenv("MEMORY_MIN_SCORE", "nan")
    with pytest.raises(ConfigError):
        _env_float("MEMORY_MIN_SCORE", .25, minimum=-1., maximum=1.)


def test_edit_during_query_is_not_cached_as_successful_empty(settings):
    async def run():
        card(settings, 1)
        await reconcile_index(Embedder(), settings)
        class Editing(Embedder):
            async def embed(self, texts):
                card(settings, 1, "updated while querying")
                return [[1., 0.]]
        result = await prefetch_cards("query", Editing(), settings)
        assert not result.cards and result.errors
        result = await prefetch_cards("query", Embedder(), settings)
        assert result.cards and not result.errors
    asyncio.run(run())


def test_dimension_mismatch_is_degraded_not_no_matches(settings):
    async def run():
        card(settings, 1)
        await reconcile_index(Embedder(), settings)
        class ChangedDimension(Embedder):
            async def embed(self, texts):
                return [[1., 0., 0.]]
        result = await prefetch_cards("query", ChangedDimension(), settings)
        assert not result.cards and result.errors
        result = await prefetch_cards("query", ChangedDimension(), settings)
        assert result.cards and not result.errors
    asyncio.run(run())


@pytest.mark.parametrize("indices", [[0, 0], [], [5]])
def test_incomplete_or_invalid_relevance_verdict_is_failure(settings, indices):
    class BadJudge(Embedder):
        async def chat(self, **kwargs):
            return SimpleNamespace(tool_calls=[{"name": "submit_memory_select", "arguments": json.dumps({
                "verdicts": [{"index": i, "relevant": True, "reason": "bad"} for i in indices]
            })}])
    card(settings, 1)
    result = asyncio.run(prefetch_cards("query", BadJudge(), settings))
    assert result.errors and not result.cards


def test_relevance_filters_before_requested_limit(settings):
    class SelectLast(Embedder):
        async def chat(self, **kwargs):
            candidates = json.loads(kwargs["messages"][-1]["content"])["candidates"]
            return SimpleNamespace(tool_calls=[{"name": "submit_memory_select", "arguments": json.dumps({
                "verdicts": [{"index": c["index"], "relevant": c["content"] == "needed", "reason": "checked"}
                             for c in candidates]
            })}])
    for i in range(1, 5):
        card(settings, i, "needed" if i == 4 else f"irrelevant {i}")
    result = asyncio.run(search_cards("query", 1, SelectLast(), settings))
    assert not result.errors and [c["content"] for _, _, c in result.hits] == ["needed"]


def test_card_change_during_relevance_is_not_injected(settings):
    class ChangingJudge(Embedder):
        async def chat(self, **kwargs):
            card(settings, 1, "modified during relevance")
            return await super().chat(**kwargs)
    card(settings, 1)
    result = asyncio.run(prefetch_cards("query", ChangingJudge(), settings))
    assert result.errors and not result.cards


def test_later_card_failure_keeps_earlier_lifecycle_row(settings, monkeypatch):
    import memory.archive as module
    archive = TaskArchive(settings)
    task = archive.create_task("task", "coding", "summary")
    original = module.write_card_file
    def fail_second(value, settings):
        if value["content"] == "two":
            raise OSError("disk full")
        return original(value, settings)
    monkeypatch.setattr(module, "write_card_file", fail_second)
    with pytest.raises(OSError):
        archive.create_cards(task, [{"type": "lesson", "content": "one"},
                                    {"type": "lesson", "content": "two"}], "task", "coding")
    with connect(settings.memory_db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM archive_cards")]
    assert ids == [c["card_id"] for c in archive.get_all_active_cards()]
    assert len(ids) == 1


def test_cancelled_chat_closes_stream(settings):
    from runtime.llm import LLMGateway
    from unittest.mock import AsyncMock
    class HangingStream:
        closed = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            await asyncio.Event().wait()
        async def close(self):
            self.closed = True
    async def run():
        stream = HangingStream()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=stream))))
        gateway = object.__new__(LLMGateway)
        gateway.settings = settings
        gateway._sem = asyncio.Semaphore(1)
        gateway.chat_client = gateway.flash_client = client
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.01):
                await gateway._openai_chat(model=settings.pro_model, messages=[], tools=None,
                                          tool_choice=None, max_tokens=None, on_delta=None)
        assert stream.closed
    asyncio.run(run())


def test_gateway_failure_is_not_reported_as_model_format_error(settings):
    from runtime.errors import ErrorClass
    class Unavailable(Embedder):
        async def chat(self, **kwargs):
            return SimpleNamespace(error_class=ErrorClass.TRANSIENT, tool_calls=[])
    card(settings, 1)
    result = asyncio.run(prefetch_cards("query", Unavailable(), settings))
    assert result.errors == ["LLMCallError: LLM gateway failed: transient"]


def test_grep_does_not_follow_card_symlink_outside_allowed_roots(settings, tmp_path):
    from memory.retrieve import memory_grep
    path = card(settings, 1)
    outside = tmp_path / "private.md"
    outside.write_text("PRIVATE_CANARY", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)
    assert memory_grep("PRIVATE_CANARY", settings) == "(no matches)"


def test_material_excerpt_preserves_requirements_at_end(settings):
    from runtime.lab.ingest import material_context
    path = settings.workspace_dir / "TASK.md"
    path.write_text("课程说明：" + "阅读教学安排。" * 800 + "\n最终要求：实现 TTL 覆盖写入并更新过期时间。", encoding="utf-8")
    text = material_context(settings, settings.workspace_dir / ".labhandler" / "session")
    assert "课程说明" in text and "最终要求" in text and "更新过期时间" in text
    assert len(text) <= 1100


@pytest.mark.parametrize("returned", ["submit_brief", "other_tool", None])
def test_flash_forced_tool_adapter_and_null_stream_chunks(settings, returned):
    from runtime.llm import LLMGateway
    from runtime.errors import ErrorClass
    from unittest.mock import AsyncMock
    class Stream:
        closed = False
        async def close(self):
            self.closed = True
        def __aiter__(self):
            async def chunks():
                yield None
                calls = [SimpleNamespace(index=0, id="call", function=SimpleNamespace(name=returned, arguments="{}"))] if returned else []
                delta = SimpleNamespace(content="" if calls else "text only", tool_calls=calls)
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="tool_calls" if calls else "stop", delta=delta)])
                yield None
            return chunks()
    async def run():
        stream = Stream()
        create = AsyncMock(return_value=stream)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        gateway = object.__new__(LLMGateway)
        gateway.settings = replace(settings, flash_native_forced_tools=False)
        gateway._sem = asyncio.Semaphore(1)
        gateway.chat_client = gateway.flash_client = client
        tools = [{"type": "function", "function": {"name": name}} for name in ["read_file", "submit_brief"]]
        messages = [{"role": "user", "content": "finish"}]
        result = await gateway._openai_chat(model=settings.flash_model, messages=messages, tools=tools,
                  tool_choice={"type": "function", "function": {"name": "submit_brief"}}, max_tokens=None, on_delta=None)
        sent = create.call_args.kwargs
        assert sent["tool_choice"] == "auto"
        assert [t["function"]["name"] for t in sent["tools"]] == ["submit_brief"]
        assert len(tools) == 2 and len(messages) == 1
        assert stream.closed
        assert result.error_class is (ErrorClass.OK if returned == "submit_brief" else ErrorClass.VALIDATION)
    asyncio.run(run())


def test_relevance_retries_transient_and_uses_flash(settings):
    from runtime.errors import ErrorClass
    class Flaky(Embedder):
        attempts = 0
        async def chat(self, **kwargs):
            self.attempts += 1
            assert kwargs["model"] == settings.flash_model
            if self.attempts == 1:
                return SimpleNamespace(error_class=ErrorClass.TRANSIENT, tool_calls=[])
            return await super().chat(**kwargs)
    card(settings, 1)
    llm = Flaky()
    result = asyncio.run(prefetch_cards("query", llm, settings))
    assert result.cards and not result.errors and llm.attempts == 2


def test_relevance_does_not_retry_auth_failure(settings):
    from runtime.errors import ErrorClass
    class Unauthorized(Embedder):
        attempts = 0
        async def chat(self, **kwargs):
            self.attempts += 1
            return SimpleNamespace(error_class=ErrorClass.AUTH, tool_calls=[])
    card(settings, 1)
    llm = Unauthorized()
    result = asyncio.run(prefetch_cards("query", llm, settings))
    assert result.errors and not result.cards and llm.attempts == 1


def test_embedding_retries_transient(settings):
    import httpx
    from openai import APITimeoutError
    class FlakyEmbedding(Embedder):
        attempts = 0
        async def embed(self, texts):
            self.attempts += 1
            if self.attempts == 1:
                raise APITimeoutError(request=httpx.Request("POST", "https://test.invalid"))
            return await super().embed(texts)
    card(settings, 1)
    llm = FlakyEmbedding()
    report = asyncio.run(index_card_ids([1], llm, settings))
    assert report["indexed"] == 1 and not report["errors"] and llm.attempts == 2


def test_material_excerpt_keeps_middle_requirement(settings):
    from runtime.lab.ingest import material_context
    path = settings.workspace_dir / "TASK.md"
    path.write_text("课程日程。\n" * 200 + "必须实现带 TTL 的缓存并更新过期时间。\n"
                    + "提交时间说明。\n" * 200, encoding="utf-8")
    context = material_context(settings, settings.workspace_dir / ".labhandler" / "session")
    assert "必须实现带 TTL" in context and len(context) <= 1100
