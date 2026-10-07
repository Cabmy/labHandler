"""记忆检索编排：文件对账、嵌入、去重与相关性筛选。失败与空结果明确区分。"""

import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
from typing import Any

from config.runtime import RuntimeSettings, get_settings
from memory.archive import TaskArchive
from memory.cards import card_path, cards_dir, content_sha256, embedding_text, read_card, snapshot
from memory.vectors import VectorIndex
from memory.relevance import select_relevant
from memory.calls import retry_transient
from tools.policy import get_policy, is_under_workspace
from tools.workspace_utils import READ_CHAR_CAP, grep_in_roots

_LOG = logging.getLogger(__name__)
_RECONCILE_LOCK = asyncio.Lock()
_SEARCH_K_CAP = 8
PREFETCH_K = 3


@dataclass
class SearchResult:
    hits: list[tuple[Path, float, dict]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PrefetchResult:
    cards: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def embed_card_file(path: Path, llm, settings: RuntimeSettings) -> None:
    """嵌入文件快照；网络等待期间文件变更或淘汰时不提交过期向量。"""
    text = embedding_text(read_card(path))
    vecs = await retry_transient(lambda: llm.embed([text]), settings)
    if len(vecs) != 1:
        raise ValueError("embedding 返回数量与输入不一致")
    if embedding_text(read_card(path)) != text:
        raise ValueError("嵌入期间卡片发生变化，等待下次对账")
    VectorIndex(settings).upsert(str(path.resolve()), text, vecs[0])


async def index_card_ids(card_ids: list[int], llm, settings: RuntimeSettings | None = None) -> dict[str, Any]:
    """仅索引已经落盘的卡片，绝不从归档库回写正文。"""
    s = settings or get_settings()
    errors: list[str] = []
    indexed = 0
    async with _RECONCILE_LOCK:
        idx = VectorIndex(s)
        for cid in dict.fromkeys(card_ids):
            path = card_path(cid, s)
            idx.delete(str(path.resolve()))
            try:
                await embed_card_file(path, llm, s)
                indexed += 1
            except Exception as e:
                errors.append(f"{path.name}: {type(e).__name__}: {e}")
    return {"indexed": indexed, "failed": len(errors), "errors": errors}


async def reconcile_index(llm, settings: RuntimeSettings | None = None) -> dict[str, Any]:
    """先清除失效向量，再逐卡重建。坏卡不阻塞其他卡片；失败可重试。"""
    s = settings or get_settings()
    async with asyncio.timeout(s.tool_timeout_s), _RECONCILE_LOCK:
        idx = VectorIndex(s)
        cards, errors = snapshot(s)
        rows = idx.all_rows()
        pending: list[Path] = []
        dropped = 0
        rebuilt = 0
        for path, row in rows.items():
            card = cards.get(path)
            if (card is None or row["embedding_space"] != idx.space
                    or row["content_sha256"] != content_sha256(embedding_text(card))):
                idx.delete(path)
                dropped += 1
        for path, card in cards.items():
            row = rows.get(path)
            if (row is None or row["embedding_space"] != idx.space
                    or row["content_sha256"] != content_sha256(embedding_text(card))):
                pending.append(Path(path))
        for path in pending:
            try:
                await embed_card_file(path, llm, s)
                rebuilt += 1
            except Exception as e:
                errors.append(f"{path.name}: {type(e).__name__}: {e}")
        if errors:
            _LOG.warning("记忆索引对账未完成: %s", "; ".join(errors))
        return {"rebuilt": rebuilt, "dropped": dropped, "errors": errors}


async def search_cards(query: str, k: int, llm,
                       settings: RuntimeSettings | None = None) -> SearchResult:
    s = settings or get_settings()
    try:
        async with asyncio.timeout(min(s.tool_timeout_s, s.memory_timeout_s)):
            return await _search_cards(query, k, llm, s)
    except TimeoutError:
        return SearchResult(errors=["记忆检索超时，可重试"])


async def _search_cards(query: str, k: int, llm,
                        settings: RuntimeSettings) -> SearchResult:
    s = settings or get_settings()
    result = SearchResult()
    if not query.strip():
        return result
    try:
        report = await reconcile_index(llm, s)
        result.errors.extend(report["errors"])
        cards, errors = snapshot(s)
        result.errors.extend(errors)
        if not cards:
            return result
        vecs = await retry_transient(lambda: llm.embed([query]), s)
        if len(vecs) != 1:
            raise ValueError("embedding 返回数量与输入不一致")
        # 网络等待后重新取文件快照，删除/修改的卡片不参与排序。
        cards, errors = snapshot(s)
        result.errors.extend(errors)
        fingerprints = {p: content_sha256(embedding_text(c)) for p, c in cards.items()}
        index = VectorIndex(s)
        rows = index.all_rows()
        for path, fingerprint in fingerprints.items():
            row = rows.get(path)
            if (row is None or row["content_sha256"] != fingerprint
                    or row["embedding_space"] != index.space
                    or row["dimension"] != len(vecs[0])):
                result.errors.append(f"{Path(path).name}: 当前卡片尚无匹配的有效索引，可重试")
                if row is not None and row["dimension"] != len(vecs[0]):
                    index.delete(path)
        candidates = index.search(vecs[0], fingerprints, s.memory_min_score)
        seen: set[str] = set()
        shortlisted: list[tuple[Path, float, dict]] = []
        for path, score in candidates:
            card = cards[path]
            key = content_sha256(card["content"])
            if key in seen:
                continue
            seen.add(key)
            shortlisted.append((Path(path), score, card))
            if len(shortlisted) >= _SEARCH_K_CAP:
                break
        if shortlisted:
            selected = await select_relevant(query, [c for _, _, c in shortlisted], llm, s)
            for i in selected:
                path, score, card = shortlisted[i]
                try:
                    current = read_card(path)
                except FileNotFoundError:
                    continue
                if embedding_text(current) != embedding_text(card):
                    result.errors.append(f"{path.name}: 相关性筛选期间卡片变化，可重试")
                    continue
                result.hits.append((path, score, card))
                if len(result.hits) >= max(1, min(k, _SEARCH_K_CAP)):
                    break
    except Exception as e:
        result.errors.append(f"{type(e).__name__}: {e}")
    result.errors = list(dict.fromkeys(result.errors))
    return result


async def memory_search(query: str, k: int, llm, settings: RuntimeSettings | None = None) -> str:
    result = await search_cards(query, k, llm, settings)
    lines = ["## Archived cards"] if result.hits else []
    for path, score, card in result.hits:
        lines.append(f"- {path.name} {card['card_type']} score={score:.3f} {card.get('task_title', '')}")
    if result.errors:
        lines.append("[ERROR/MemoryRetrieval] 检索未完整完成，可重试；不能视为没有相关记忆。"
                     + "\n" + "\n".join(result.errors))
    return "\n".join(lines) or "(no matches)"


async def prefetch_cards(query: str, llm, settings: RuntimeSettings | None = None,
                         k: int = PREFETCH_K) -> PrefetchResult:
    if os.getenv("EVAL_DISABLE_PREFETCH") == "1":
        return PrefetchResult()
    result = await search_cards(query, k, llm, settings)
    return PrefetchResult(
        cards=[f"{p.name} score={score:.3f}\n{card['content']}" for p, score, card in result.hits],
        errors=result.errors,
    )


def retire_cards_matching(needle: str, settings: RuntimeSettings | None = None) -> int:
    s = settings or get_settings()
    needle = needle.strip()
    if not needle:
        return 0
    matched = re.fullmatch(r"(\d+)(?:\.md)?", Path(needle).name)
    if matched:
        ids = [int(matched.group(1))]
    else:
        cards, _ = snapshot(s)
        ids = [c["card_id"] for p, c in cards.items()
               if needle in Path(p).name or needle in c["content"]]
    n = TaskArchive(s).retire_cards(ids)
    idx = VectorIndex(s)
    for cid in ids:
        idx.delete(str(card_path(cid, s).resolve()))
    return n


def memory_grep(
    pattern: str,
    settings: RuntimeSettings | None = None,
    limit: int = 40,
    *,
    extra_roots: list[Path] | None = None,
) -> str:
    """在 cards_dir 以及 extra_roots（缺省为 workspace/.labhandler）下按正则扫文件。

    委托给公共 grep_in_roots。非法正则返回 [ERROR/Validation]；
    命中达 limit 后截断；无命中返回 "(no matches)"。
    """
    s = settings or get_settings()
    roots = [cards_dir(s)]
    if extra_roots is not None:
        roots.extend(extra_roots)
    else:
        ws = s.workspace_dir / ".labhandler"
        if ws.is_dir():
            roots.append(ws)

    def _skip(p: Path) -> bool:
        return (get_policy().is_control_file(p)
                or not any(is_under_workspace(p, root) for root in roots))

    return grep_in_roots(pattern, roots, limit, file_filter=_skip)


def memory_read(
    path: str,
    settings: RuntimeSettings | None = None,
    *,
    extra_roots: list[Path] | None = None,
) -> str:
    """读允许范围内的文件，正文最多 80000 字。

    相对路径依次试 extra_roots、cards_dir、workspace_dir；绝对路径必须落在这些根之下。
    越权 [ERROR/PermissionError]，缺失 [ERROR/FileNotFoundError]。
    """
    s = settings or get_settings()
    candidate = Path(path)
    bases = [*(extra_roots or []), cards_dir(s), s.workspace_dir]
    if not candidate.is_absolute():
        for base in bases:
            p = (base / path).resolve()
            if not is_under_workspace(p, base):
                continue
            if p.is_file():
                if get_policy().is_control_file(p):
                    return f"[ERROR/PermissionError] harness control file is not readable: {path}"
                return p.read_text(encoding="utf-8", errors="replace")[:READ_CHAR_CAP]
        return f"[ERROR/FileNotFoundError] {path}"
    resolved = candidate.resolve()
    allowed = [b.resolve() for b in bases]
    if not any(is_under_workspace(resolved, a) for a in allowed):
        return f"[ERROR/PermissionError] path not allowed: {path}"
    if not resolved.is_file():
        return f"[ERROR/FileNotFoundError] {path}"
    if get_policy().is_control_file(resolved):
        return f"[ERROR/PermissionError] harness control file is not readable: {path}"
    return resolved.read_text(encoding="utf-8", errors="replace")[:READ_CHAR_CAP]
