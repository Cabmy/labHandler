"""会话内的两份主动记忆，都落在 session 目录。

MEMORY.md —— Pro 判定「必须长久记住」的不变量。每轮由 assemble 从文件重新注入，
             不经 compact 摘要。Judge 可追加、改写或删除；整份超 _MAX_CHARS 时
             淘汰最早的条目。
FORGET.md —— Pro 判定「无关杂乱」的描述。compact 时作为排除指令交给
             summarizer；压缩完成后立即清空——被描述的内容已不在 history 里，
             再留着只会成为新噪声。

条目按整行匹配，同一条不会写两遍。空则删文件。原子落盘。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.lab.persist import read_text, write_text

MEMORY_FILE = "MEMORY.md"
FORGET_FILE = "FORGET.md"

_MEMORY_HEADER = "# 必须记住的不变量\n"
_FORGET_HEADER = "# 压缩时忽略的杂乱上下文\n"

# 单条上限：MEMORY.md 每轮整份进上下文。超出截成一行。
MEMORY_ENTRY_MAX = 80
# 单份文件硬上限；超出时丢掉最早的条目。
_MAX_CHARS = 2000


@dataclass(frozen=True)
class SessionNotes:
    memory: str
    forget: str

    @property
    def has_memory(self) -> bool:
        return bool(self.memory.strip())

    @property
    def has_forget(self) -> bool:
        return bool(self.forget.strip())


def load_notes(sdir: Path) -> SessionNotes:
    return SessionNotes(
        memory=_body(read_text(sdir, MEMORY_FILE), _MEMORY_HEADER),
        forget=_body(read_text(sdir, FORGET_FILE), _FORGET_HEADER),
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


def append_memory(sdir: Path, text: str) -> bool:
    """追加一条必须长期保留的不变量。超出 MEMORY_ENTRY_MAX 的尾部丢掉；重复或空串不写。"""
    return _append(sdir, MEMORY_FILE, _MEMORY_HEADER, text, limit=MEMORY_ENTRY_MAX)


def append_forget(sdir: Path, text: str) -> bool:
    """追加一条「总结时请忽略」的噪声描述。只对下一次 compact 有效；压缩完成后文件被清空。"""
    return _append(sdir, FORGET_FILE, _FORGET_HEADER, text)


def remove_memory(sdir: Path, needles: list[str]) -> int:
    """删掉正文等于或包含 needle 的条目。返回删除条数；无有效 needle 时为 0。"""
    keys = [n for n in (_one_line(n, 200) for n in needles) if n]
    if not keys:
        return 0
    lines = _read_lines(sdir, MEMORY_FILE, _MEMORY_HEADER)
    kept = [ln for ln in lines if not any(_matches(ln, k) for k in keys)]
    dropped = len(lines) - len(kept)
    if dropped:
        _write_lines(sdir, MEMORY_FILE, _MEMORY_HEADER, kept)
    return dropped


def replace_memory(sdir: Path, old: str, new: str) -> int:
    """把匹配 old 的条目改成 new（同样 ≤ MEMORY_ENTRY_MAX）。new 为空则删除该条。返回改动条数。"""
    key = _one_line(old, 200)
    replacement = _one_line(new, MEMORY_ENTRY_MAX)
    if not key:
        return 0
    lines = _read_lines(sdir, MEMORY_FILE, _MEMORY_HEADER)
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
        _write_lines(sdir, MEMORY_FILE, _MEMORY_HEADER, out)
    return n


def apply_memory(
    sdir: Path,
    *,
    replace: Any = None,
    remove: Any = None,
    append: str = "",
) -> None:
    """先改、再删、最后追加。Judge 一次裁决里对 MEMORY.md 的全部变更，顺序固定。"""
    for item in replace or []:
        if not isinstance(item, dict):
            continue
        replace_memory(sdir, str(item.get("old") or ""), str(item.get("new") or ""))
    needles = remove if isinstance(remove, list) else ([remove] if remove else [])
    remove_memory(sdir, [str(x) for x in needles])
    append_memory(sdir, append)


def clear_forget(sdir: Path) -> int:
    """压缩完成后清空 FORGET.md，返回清掉的条数。文件不存在视为 0。

    被排除的内容此时已不在 history 里；文件必须删掉，否则下一轮又变成噪声。
    """
    body = _body(read_text(sdir, FORGET_FILE), _FORGET_HEADER)
    count = len([ln for ln in body.splitlines() if ln.strip()])
    (sdir / FORGET_FILE).unlink(missing_ok=True)
    return count


def memory_block(notes: SessionNotes) -> str:
    """交给 assemble 的记忆块。空则返回空串，调用方不加 memory 槽位。"""
    if not notes.has_memory:
        return ""
    return "## 必须记住的不变量\n" + notes.memory


def forget_directive(notes: SessionNotes) -> str:
    """交给 summarizer 的排除指令。无条目时返回空串，不拼进 summarizer 的 system。"""
    if not notes.has_forget:
        return ""
    return (
        "以下内容已被判定为与任务无关的杂乱上下文，"
        "总结时必须刻意忽略，不要出现在摘要里：\n" + notes.forget
    )
