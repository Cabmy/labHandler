"""扫 workspace 写目录；PDF/docx/pptx 转成 session 下可读 markdown。"""

from pathlib import Path

from config.runtime import RuntimeSettings
from runtime.lab.persist import CATALOG_FILE, write_text
from tools.sandbox_tools import sandbox_convert_to_markdown
from tools.workspace_utils import iter_workspace_files

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
    write_text(session_dir, CATALOG_FILE, text)
    return text
