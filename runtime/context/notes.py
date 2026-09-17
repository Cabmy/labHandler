"""会话内 Pro 能主动写的三份文件，都落在 session 目录，每轮由 assemble 重新注入。

NOTES.md  —— Pro 运行时写的不变量，进 notes 槽。短句 ≤80 字，或指向 notes/*.md 的指针。
CARDS.md  —— SPEC 预取的跨 lab 知识卡片正文，进 cards 槽（仅 Pro），与 NOTES 无关。
             本模块是这份文件格式的唯一出处：memory.retrieve 只给单张卡片的文本，
             卡片之间用 --- 分隔由这里拼、这里拆。文件存在（哪怕是空的）即表示已预取。
FORGET.md —— 无关杂乱的描述，只作用于下一次 compact：交给 summarizer 排除，压完即清空。

遗忘一张卡片是一次动作两个后果（forget_card）：立刻从 CARDS.md 去掉，所以下一拍的
cards 槽里就没有它；同时记进 FORGET.md，所以下一次摘要也不会把它写回来。
跨 lab 淘汰（retired_at + 删文件）由 memory.retrieve.retire_cards_matching 负责，
本模块不碰归档库。

条目按整行匹配，同一条不会写两遍。原子落盘。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.lab.persist import read_text, write_text

NOTES_FILE = "NOTES.md"
CARDS_FILE = "CARDS.md"
FORGET_FILE = "FORGET.md"
LONG_NOTES_DIR = "notes"

_NOTES_HEADER = "# Notes\n"
_FORGET_HEADER = "# 压缩时忽略的杂乱上下文\n"
_CARD_SEP = "\n---\n"

# 单条上限：NOTES.md 每轮整份进上下文。超出截成一行。
NOTES_ENTRY_MAX = 80
# 单份文件硬上限；超出时丢掉最早的条目。
_MAX_CHARS = 2000


@dataclass(frozen=True)
class SessionNotes:
    """一拍要注入的全部会话笔记。block 属性就是 assemble 对应槽位的正文。"""

    notes: str
    forget: str
    cards: list[str] = field(default_factory=list)

    @property
    def has_notes(self) -> bool:
        return bool(self.notes.strip())

    @property
    def has_forget(self) -> bool:
        return bool(self.forget.strip())

    @property
    def notes_block(self) -> str:
        return "## Notes\n" + self.notes if self.has_notes else ""

    @property
    def cards_block(self) -> str:
        if not self.cards:
            return ""
        return "## Archived knowledge\n" + _CARD_SEP.join(self.cards)


def load_notes(sdir: Path) -> SessionNotes:
    return SessionNotes(
        notes=_body(read_text(sdir, NOTES_FILE), _NOTES_HEADER),
        forget=_body(read_text(sdir, FORGET_FILE), _FORGET_HEADER),
        cards=load_cards(sdir) or [],
    )


def _body(raw: str, header: str) -> str:
    text = raw.strip()
    if text.startswith(header.strip()):
        text = text[len(header.strip()):]
    return text.strip()


def _plain(line: str) -> str:
    s = line.strip()
    return s[2:].strip() if s.startswith("- ") else s


def _one_line(text: str, limit: int) -> str:
    entry = " ".join(_plain(text).split())
    if len(entry) <= limit:
        return entry
    cut = entry[:limit].rsplit(" ", 1)[0]
    return cut or entry[:limit]


def _read_lines(sdir: Path, filename: str, header: str) -> list[str]:
    body = _body(read_text(sdir, filename), header)
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _write_lines(sdir: Path, filename: str, header: str, lines: list[str]) -> None:
    if len("\n".join(lines)) > _MAX_CHARS:
        while lines and len("\n".join(lines)) > _MAX_CHARS:
            lines.pop(0)
    if not lines:
        (sdir / filename).unlink(missing_ok=True)
        return
    write_text(sdir, filename, header + "\n" + "\n".join(lines) + "\n")


def _matches(line: str, needle: str) -> bool:
    body = _plain(line)
    key = " ".join(needle.strip().split())
    if key.startswith("- "):
        key = key[2:].strip()
    if not key:
        return False
    return body == key or key in body


def _append(sdir: Path, filename: str, header: str, text: str, *, limit: int | None = None) -> bool:
    entry = _one_line(text, limit) if limit else text.strip()
    if not entry:
        return False
    lines = _read_lines(sdir, filename, header)
    bullet = entry if entry.startswith("- ") else f"- {entry}"
    if bullet in lines:
        return False
    lines.append(bullet)
    _write_lines(sdir, filename, header, lines)
    return True


def append_notes(sdir: Path, text: str) -> bool:
    """追加一条必须长期保留的不变量。超出 NOTES_ENTRY_MAX 的尾部丢掉；重复或空串不写。"""
    return _append(sdir, NOTES_FILE, _NOTES_HEADER, text, limit=NOTES_ENTRY_MAX)


def append_forget(sdir: Path, text: str) -> bool:
    """追加一条「总结时请忽略」的噪声描述。只对下一次 compact 有效；压缩完成后文件被清空。"""
    return _append(sdir, FORGET_FILE, _FORGET_HEADER, text)


def remove_notes(sdir: Path, needles: list[str]) -> int:
    """删掉正文等于或包含 needle 的条目。返回删除条数；无有效 needle 时为 0。"""
    keys = [n for n in (_one_line(n, 200) for n in needles) if n]
    if not keys:
        return 0
    lines = _read_lines(sdir, NOTES_FILE, _NOTES_HEADER)
    kept = [ln for ln in lines if not any(_matches(ln, k) for k in keys)]
    dropped = len(lines) - len(kept)
    if dropped:
        _write_lines(sdir, NOTES_FILE, _NOTES_HEADER, kept)
    return dropped


def replace_notes(sdir: Path, old: str, new: str) -> int:
    """把匹配 old 的条目改成 new（同样 ≤ NOTES_ENTRY_MAX）。new 为空则删除该条。返回改动条数。"""
    key = _one_line(old, 200)
    replacement = _one_line(new, NOTES_ENTRY_MAX)
    if not key:
        return 0
    lines = _read_lines(sdir, NOTES_FILE, _NOTES_HEADER)
    out: list[str] = []
    n = 0
    for ln in lines:
        if not _matches(ln, key):
            out.append(ln)
            continue
        n += 1
        if replacement:
            bullet = f"- {replacement}"
            if bullet not in out:
                out.append(bullet)
    if n:
        _write_lines(sdir, NOTES_FILE, _NOTES_HEADER, out)
    return n


def apply_notes(
    sdir: Path,
    *,
    replace: Any = None,
    remove: Any = None,
    append: str = "",
) -> None:
    """先改、再删、最后追加。Judge 一次裁决里对 NOTES.md 的全部变更，顺序固定。"""
    for item in replace or []:
        if not isinstance(item, dict):
            continue
        replace_notes(sdir, str(item.get("old") or ""), str(item.get("new") or ""))
    needles = remove if isinstance(remove, list) else ([remove] if remove else [])
    remove_notes(sdir, [str(x) for x in needles])
    append_notes(sdir, append)


def load_cards(sdir: Path) -> list[str] | None:
    """读 CARDS.md。返回卡片列表；None 表示本 lab 还没预取过（续跑据此不重复检索）。"""
    if not (sdir / CARDS_FILE).is_file():
        return None
    return [c.strip() for c in read_text(sdir, CARDS_FILE).split(_CARD_SEP) if c.strip()]


def write_cards(sdir: Path, cards: list[str]) -> None:
    """写出预取卡片。空表也落盘（空文件 = 已预取，续跑不再搜）。"""
    write_text(sdir, CARDS_FILE, _CARD_SEP.join(cards) + "\n" if cards else "")


def forget_card(sdir: Path, needle: str) -> int:
    """Pro 主动遗忘一张卡片：立刻从 CARDS.md 去掉，并记进 FORGET.md。返回丢掉的张数。

    needle 是文件名或正文里的唯一子串。命中 0 张仍然记 FORGET——卡片可能已经被摘要
    吸收进纪要，那份副本要靠下一次 compact 才能去掉。
    """
    cards = load_cards(sdir) or []
    kept = [c for c in cards if needle not in c]
    if len(kept) != len(cards):
        write_cards(sdir, kept)
    append_forget(sdir, needle)
    return len(cards) - len(kept)


def write_long_note(sdir: Path, spec: Any) -> bool:
    """把较长正文写入 notes/{name}.md，NOTES.md 只留文件名指针。"""
    if not isinstance(spec, dict):
        return False
    raw = Path(str(spec.get("name") or "")).name
    stem = raw[:-3] if raw.endswith(".md") else raw
    raw = stem + ".md"
    if not stem or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in stem):
        return False
    if stem.startswith("."):
        return False
    content = str(spec.get("content") or "").strip()
    if not content:
        return False
    dest_dir = sdir / LONG_NOTES_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    write_text(dest_dir, raw, content + "\n")
    return append_notes(sdir, raw)


def read_long_note(sdir: Path, path: str) -> str:
    """读 notes/ 下由 notes_write 落盘的长文。只认文件名，取 .name 即杜绝越权。"""
    name = Path(path).name
    target = sdir / LONG_NOTES_DIR / name
    if name in {"", ".", ".."} or not target.is_file():
        return f"[ERROR/FileNotFoundError] {path}"
    return target.read_text(encoding="utf-8", errors="replace")[:80_000]


def clear_forget(sdir: Path) -> int:
    """压缩完成后清空 FORGET.md，返回清掉的条数。文件不存在视为 0。

    被排除的内容此时已不在 history 里；文件必须删掉，否则下一轮又变成噪声。
    """
    body = _body(read_text(sdir, FORGET_FILE), _FORGET_HEADER)
    count = len([ln for ln in body.splitlines() if ln.strip()])
    (sdir / FORGET_FILE).unlink(missing_ok=True)
    return count


def forget_directive(notes: SessionNotes) -> str:
    """交给 summarizer 的排除指令。无条目时返回空串，不拼进 summarizer 的 system。"""
    if not notes.has_forget:
        return ""
    return (
        "以下内容已被判定为与任务无关的杂乱上下文，"
        "总结时必须刻意忽略，不要出现在摘要里：\n" + notes.forget
    )
