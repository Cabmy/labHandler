"""扫 workspace 写目录；PDF/docx/pptx 转成 session 下可读 markdown。"""

import re
from pathlib import Path

from config.runtime import RuntimeSettings
from runtime.lab.persist import CATALOG_FILE, read_text, write_text
from tools.sandbox_tools import sandbox_convert_to_markdown
from tools.workspace_utils import iter_workspace_files

MATERIAL_CONTEXT_FILE = "MATERIAL_CONTEXT.md"

_PARSEABLE = {".pdf", ".docx", ".pptx"}


async def ingest(settings: RuntimeSettings, session_dir: Path) -> str:
    ws = settings.workspace_dir
    lines = [
        "# Catalog",
        "Paths are relative to the workspace root. Use `foo.py`, never `workspace/foo.py`.",
        "",
    ]
    if ws.exists():
        converted_root = session_dir / "converted"
        for p in iter_workspace_files(ws, max_files=40):
            rel = p.relative_to(ws).as_posix()
            size = p.stat().st_size
            if p.suffix.lower() not in _PARSEABLE:
                lines.append(f"- `{rel}`  {size} B")
                continue
            dest = converted_root / p.relative_to(ws)
            dest = dest.with_name(dest.name + ".md")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(await sandbox_convert_to_markdown(str(p)), encoding="utf-8")
                conv = dest.relative_to(ws).as_posix()
                lines.append(f"- `{rel}`  {size} B  converted=`{conv}`")
            except Exception as e:
                lines.append(f"- `{rel}`  {size} B  convert_failed={type(e).__name__}")
    else:
        lines.append("(empty)")
    text = "\n".join(lines) + "\n"
    write_text(session_dir, MATERIAL_CONTEXT_FILE, material_context(settings, session_dir))
    write_text(session_dir, CATALOG_FILE, text)
    return text


def material_context(settings: RuntimeSettings, session_dir: Path) -> str:
    """在 ingest 时快照材料摘录，后续检索不混入本次生成的产物。

    文档优先，保留路径；总计至多 6000 字，每份最多 1000 字；长文件优先保留要求行，同时保留开头和结尾。
    PDF/Office 使用 ingest 已转换的正文，拒绝越界符号链接及门禁文件。
    """
    from tools.policy import get_policy

    extensions = {".md", ".txt", ".rst", ".py", ".java", ".c", ".cpp", ".h", ".sql"}
    roots = [settings.workspace_dir, session_dir / "converted"]
    candidates: list[tuple[str, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in iter_workspace_files(root, extensions=extensions):
            if (not path.resolve().is_relative_to(root.resolve())
                    or "acceptance" in path.relative_to(root).parts
                    or get_policy().is_control_file(path)):
                continue
            candidates.append((str(path.relative_to(root)), path))
    candidates.sort(key=lambda item: (
        item[1].suffix.lower() not in {".md", ".txt", ".rst"}, item[0]))
    remaining = 6000
    chunks: list[str] = []
    for name, path in candidates:
        if remaining <= 0:
            break
        excerpt = material_excerpt(path, min(1000, remaining))
        if path.suffix.lower() in {".md", ".txt", ".rst"}:
            excerpt = "\n".join(dict.fromkeys(excerpt.splitlines()))
        chunk = f"\n材料 {name}:\n{excerpt}"[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
    return "".join(chunks)


def retrieval_query(question: str, session_dir: Path) -> str:
    """限制 embedding 输入长度；用户请求优先，材料使用 ingest 的初始快照。"""
    return question[:2000] + "\n\n" + read_text(session_dir, MATERIAL_CONTEXT_FILE)


def material_excerpt(path: Path, limit: int) -> str:
    """流式提取要求行并保留首尾，避免只截文件两端；内存占用有界。"""
    with path.open(encoding="utf-8", errors="replace") as source:
        head = source.read(limit + 1)
    if len(head) <= limit:
        return head
    separator = "\n…\n"
    requirement = re.compile(r"要求|必须|不得|禁止|接口|交付|\b(?:must|shall|requirements?|deliverables?)\b", re.I)
    important: list[str] = []
    remaining = limit // 2
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in iter(lambda: source.readline(4096), ""):
            line = line.strip()
            if requirement.search(line) and line not in important:
                excerpt = line[:remaining]
                important.append(excerpt)
                remaining -= len(excerpt) + 1
                if remaining <= 0:
                    break
    middle = "\n".join(important)
    separators = separator if not middle else separator + middle + separator
    tail_size = max(0, (limit - len(separators)) // 2)
    if not tail_size:
        return head[:limit]
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - tail_size * 4))
        tail = source.read(tail_size * 4).decode("utf-8", errors="replace")[-tail_size:]
    return head[:limit - len(separators) - tail_size] + separators + tail
