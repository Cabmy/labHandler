"""跨 lab 记忆检索。卡片 markdown 是事实源，向量表是派生索引。

预取：SPEC 时按作业文本取 top-3 卡片正文交给 runtime.context.notes 落盘。本模块只产出
单张卡片的文本，卡片怎么拼、怎么存、怎么被 Pro 丢掉都归 notes 模块。
余弦低于 PREFETCH_MIN_SCORE 视为无关，不注入（弃权）。
冷检索：memory_search 返回指针，memory_read 打开 md。
每次 lab 开始时 reconcile_index 对齐文件与向量表。

读路径只允许 cards_dir 与 workspace_dir。语义检索 k 夹在 1..8；grep 命中上限默认 40；
单文件正文最多返回 80000 字。
"""

from pathlib import Path
import asyncio
import os
import re
from typing import Any

from config.runtime import RuntimeSettings, get_settings
from memory.archive import get_task_archive
from memory.vectors import VectorIndex, content_sha256
from tools.policy import get_policy, is_under_workspace
from tools.workspace_utils import READ_CHAR_CAP, grep_in_roots

PREFETCH_K = 3
PREFETCH_MIN_SCORE = 0.25
_SEARCH_K_CAP = 8
_CARD_NAME = re.compile(r"^(\d+)(?:\.md)?$", re.I)
_RECONCILE_LOCK = asyncio.Lock()


def _cards_dir(settings: RuntimeSettings | None = None) -> Path:
    """返回 settings.cards_dir，目录保证存在。"""
    s = settings or get_settings()
    s.cards_dir.mkdir(parents=True, exist_ok=True)
    return s.cards_dir


def card_path(card_id: int, settings: RuntimeSettings | None = None) -> Path:
    """卡片文件路径：{cards_dir}/{card_id}.md。"""
    return _cards_dir(settings) / f"{card_id}.md"


def write_card_file(card: dict[str, Any], settings: RuntimeSettings | None = None) -> Path:
    """写出 {card_id}.md：YAML frontmatter（含 content_sha256）+ 正文 content。覆盖同 id 已有文件。"""
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
    """读卡片文件全文（含 frontmatter）。"""
    return path.read_text(encoding="utf-8")


def parse_card_meta(text: str) -> dict[str, str]:
    """读首段 YAML frontmatter 为扁平 dict。无合法分隔则空 dict。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---\n", 3)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta


def parse_card_body(text: str) -> str:
    """去掉首段 --- frontmatter，返回正文。无合法分隔则整份原文。"""
    if not text.startswith("---"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        return text
    return text[end + 5:].strip()


async def embed_card_file(path: Path, llm, settings: RuntimeSettings | None = None) -> None:
    """用正文（无 frontmatter）嵌入并 upsert 到向量表。正文为空时退回全文前 2000 字。"""
    s = settings or get_settings()
    text = read_card_file(path)
    body = parse_card_body(text)
    vecs = await llm.embed([body or text[:2000]])
    VectorIndex(settings=s).upsert(str(path), body, vecs[0])


async def index_card_ids(card_ids: list[int], llm, settings: RuntimeSettings | None = None) -> dict[str, Any]:
    """按 archive 行写文件并建索引。单卡失败记入 errors，其余继续。"""
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
    """同时删除该卡的向量行与 markdown。文件不存在不报错。"""
    s = settings or get_settings()
    path = card_path(card_id, s)
    VectorIndex(settings=s).delete(str(path))
    path.unlink(missing_ok=True)


def resolve_card_ids(needle: str, settings: RuntimeSettings | None = None) -> list[int]:
    """把 memory_forget 的针（文件名或正文子串）解析成 card_id。"""
    needle = needle.strip()
    if not needle:
        return []
    name = Path(needle).name
    matched = _CARD_NAME.match(name)
    if matched:
        return [int(matched.group(1))]
    embedded = re.search(r"(?:^|\b)(\d+)\.md\b", needle)
    if embedded:
        return [int(embedded.group(1))]
    s = settings or get_settings()
    hits: list[int] = []
    for p in _cards_dir(s).glob("*.md"):
        if not p.stem.isdigit():
            continue
        try:
            raw = read_card_file(p)
        except Exception:
            continue
        if needle in p.name or needle in raw:
            hits.append(int(p.stem))
    return hits


def retire_cards_matching(needle: str, settings: RuntimeSettings | None = None) -> int:
    """按针淘汰归档卡：写 retired_at，删 markdown 与向量行。返回实际标记数。"""
    ids = resolve_card_ids(needle, settings)
    if not ids:
        return 0
    n = get_task_archive().retire_cards(ids)
    for cid in ids:
        delete_card_file(cid, settings)
    return n


async def reconcile_index(llm, settings: RuntimeSettings | None = None) -> dict[str, int]:
    """以 cards_dir 下 *.md 为事实源对齐向量表。

    缺行、content_sha256 不一致、或 embedding_model 与当前设置不同 → 重建该行。
    没有对应文件的索引行删除。同进程并发调用串行化，避免重复嵌入。
    """
    s = settings or get_settings()
    async with _RECONCILE_LOCK:
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


async def search_cards(
    query: str,
    k: int,
    llm,
    settings: RuntimeSettings | None = None,
) -> list[tuple[Path, float, str]]:
    """嵌入 query，返回至多 k 条 (path, score, body)。空表或无命中为 []。"""
    s = settings or get_settings()
    vecs = await llm.embed([query])
    hits = VectorIndex(settings=s).search(vecs[0], k=max(1, k))
    out: list[tuple[Path, float, str]] = []
    for path, score in hits:
        p = Path(path)
        if not p.is_file():
            continue
        out.append((p, score, parse_card_body(read_card_file(p))))
    return out


def render_pointers(hits: list[tuple[Path, float, str]], *, scores: bool = False) -> str:
    """检索工具用的卡片指针：文件名 + card_type，不含正文。scores 时附带余弦分。"""
    lines = ["## Archived cards"]
    for path, score, _body in hits:
        raw = read_card_file(path) if path.is_file() else ""
        ctype = parse_card_meta(raw).get("card_type", "")
        extra = f" score={score:.3f}" if scores else ""
        lines.append(f"- {path.name} {ctype}{extra}".rstrip())
    return "\n".join(lines)


def _unique(hits: list[tuple[Path, float, str]], limit: int) -> list[tuple[Path, float, str]]:
    """按正文去重，保留分数更高的先到者。"""
    seen: set[str] = set()
    picked: list[tuple[Path, float, str]] = []
    for item in hits:
        key = content_sha256(item[2]) if item[2] else str(item[0])
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


async def memory_search(query: str, k: int, llm, settings: RuntimeSettings | None = None) -> str:
    """语义检索：嵌入 query，返回至多 min(k, 8) 条去重指针。正文用 memory_read。

    无命中 "(no matches)"。
    """
    hits = await search_cards(query, _SEARCH_K_CAP, llm, settings)
    hits = _unique(hits, max(1, min(k, _SEARCH_K_CAP)))
    if not hits:
        return "(no matches)"
    return render_pointers(hits, scores=True)


async def prefetch_cards(
    query: str,
    llm,
    settings: RuntimeSettings | None = None,
    k: int = PREFETCH_K,
) -> list[str]:
    """SPEC 预取：按作业文本取 top-k 去重卡片，每张一段「文件名 + 完整正文」。

    分数低于 PREFETCH_MIN_SCORE 的卡片丢掉（全部低于则返回 []）。嵌入失败也不打断 lab。
    """
    if os.getenv("EVAL_DISABLE_PREFETCH") == "1":
        return []
    if not query.strip():
        return []
    try:
        hits = await search_cards(query, _SEARCH_K_CAP, llm, settings)
    except Exception:
        return []
    hits = [(p, score, body)
            for p, score, body in hits if score >= PREFETCH_MIN_SCORE]
    if not hits:
        return []
    return [f"{p.name} score={score:.3f}\n{body}" for p, score, body in _unique(hits, k)]


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
    roots = [_cards_dir(s)]
    if extra_roots is not None:
        roots.extend(extra_roots)
    else:
        ws = s.workspace_dir / ".labhandler"
        if ws.is_dir():
            roots.append(ws)

    def _skip(p: Path) -> bool:
        return get_policy().is_control_file(p)

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
    bases = [*(extra_roots or []), _cards_dir(s), s.workspace_dir]
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
