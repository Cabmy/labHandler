"""跨 lab 记忆检索：向量 + 文件系统。卡片 markdown 是事实源。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from config.runtime import RuntimeSettings, get_settings
from memory.archive import get_task_archive
from memory.vectors import VectorIndex, content_sha256


def _cards_dir(settings: RuntimeSettings | None = None) -> Path:
    s = settings or get_settings()
    s.cards_dir.mkdir(parents=True, exist_ok=True)
    return s.cards_dir


def card_path(card_id: int, settings: RuntimeSettings | None = None) -> Path:
    return _cards_dir(settings) / f"{card_id}.md"


def write_card_file(card: dict[str, Any], settings: RuntimeSettings | None = None) -> Path:
    cid = int(card["card_id"])
    body = str(card.get("content") or "")
    meta = [
        "---",
        f"card_id: {cid}",
        f"task_id: {card.get('task_id', '')}",
        f"card_type: {card.get('card_type') or card.get('type', '')}",
        f"task_title: {card.get('task_title', '')}",
        f"task_type: {card.get('task_type', '')}",
        f"content_sha256: {content_sha256(body)}",
        "---",
        "",
        body,
        "",
    ]
    path = card_path(cid, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(meta), encoding="utf-8")
    return path


def read_card_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_card_body(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        return text
    return text[end + 5 :].strip()


async def embed_card_file(path: Path, llm, settings: RuntimeSettings | None = None) -> None:
    s = settings or get_settings()
    text = read_card_file(path)
    body = parse_card_body(text)
    vecs = await llm.embed([body or text[:2000]])
    VectorIndex(settings=s).upsert(str(path), body, vecs[0])


async def index_card_ids(card_ids: list[int], llm, settings: RuntimeSettings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    archive = get_task_archive()
    cards = archive.get_cards_by_ids(card_ids)
    indexed = 0
    errors: list[str] = []
    for card in cards:
        try:
            path = write_card_file(card, s)
            await embed_card_file(path, llm, s)
            indexed += 1
        except Exception as e:
            errors.append(f"{card.get('card_id')}: {type(e).__name__}: {e}")
    return {"indexed": indexed, "failed": len(errors), "errors": errors}


def delete_card_file(card_id: int, settings: RuntimeSettings | None = None) -> None:
    s = settings or get_settings()
    path = card_path(card_id, s)
    VectorIndex(settings=s).delete(str(path))
    path.unlink(missing_ok=True)


async def reconcile_index(llm, settings: RuntimeSettings | None = None) -> dict[str, int]:
    """启动对账：文件为事实源，不一致则重建对应索引行。"""
    s = settings or get_settings()
    idx = VectorIndex(settings=s)
    files = {str(p): p for p in _cards_dir(s).glob("*.md")}
    rows = {r["path"]: r for r in idx.all_rows()}
    rebuilt = 0
    dropped = 0
    for path_str, p in files.items():
        body = parse_card_body(read_card_file(p))
        sha = content_sha256(body)
        row = rows.get(path_str)
        if (
            row is None
            or row.get("content_sha256") != sha
            or row.get("embedding_model") != s.embedding_model
        ):
            await embed_card_file(p, llm, s)
            rebuilt += 1
    for path_str in rows:
        if path_str not in files:
            idx.delete(path_str)
            dropped += 1
    return {"rebuilt": rebuilt, "dropped": dropped}


async def memory_search(query: str, k: int, llm, settings: RuntimeSettings | None = None) -> str:
    s = settings or get_settings()
    vecs = await llm.embed([query])
    hits = VectorIndex(settings=s).search(vecs[0], k=max(1, min(k, 8)))
    if not hits:
        return "(no matches)"
    lines: list[str] = []
    for path, score in hits:
        p = Path(path)
        snippet = parse_card_body(read_card_file(p))[:400] if p.is_file() else ""
        lines.append(f"{p.name} score={score:.3f}\n{snippet}")
    return "\n---\n".join(lines)


def memory_grep(pattern: str, settings: RuntimeSettings | None = None, limit: int = 40) -> str:
    s = settings or get_settings()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[ERROR/Validation] invalid regex: {e}"
    roots = [_cards_dir(s)]
    ws = s.workspace_dir / ".labhandler"
    if ws.is_dir():
        roots.append(ws)
    hits: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p}:{i}:{line[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n[truncated limit={limit}]"
    return "\n".join(hits) if hits else "(no matches)"


def memory_read(path: str, settings: RuntimeSettings | None = None) -> str:
    s = settings or get_settings()
    candidate = Path(path)
    if not candidate.is_absolute():
        for base in (_cards_dir(s), s.workspace_dir):
            p = (base / path).resolve()
            try:
                p.relative_to(base.resolve())
            except ValueError:
                continue
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")[:80_000]
        return f"[ERROR/FileNotFoundError] {path}"
    resolved = candidate.resolve()
    allowed = [_cards_dir(s).resolve(), s.workspace_dir.resolve()]
    if not any(_is_under(resolved, a) for a in allowed):
        return f"[ERROR/PermissionError] path not allowed: {path}"
    if not resolved.is_file():
        return f"[ERROR/FileNotFoundError] {path}"
    return resolved.read_text(encoding="utf-8", errors="replace")[:80_000]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
