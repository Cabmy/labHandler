"""skill 工具：SOP 加载与 reference/script 按需取用（无 langchain）。"""

from __future__ import annotations

from pathlib import Path

from skills.repository import (
    list_skill_documents,
    list_skill_scripts,
    load_skill_document,
    load_skill_reference as _load_skill_reference,
)


def load_skill(skill_name: str) -> dict:
    return load_skill_document(skill_name)


def load_skill_reference(skill_name: str, ref_name: str) -> str:
    try:
        return _load_skill_reference(skill_name, ref_name)
    except (FileNotFoundError, PermissionError) as e:
        return f"[ERROR/{type(e).__name__}] {e}"


def use_skill_script(skill_name: str, script_name: str) -> str:
    import shutil

    from config.runtime import get_settings

    src = get_settings().skills_dir / skill_name / "scripts" / Path(script_name).name
    if not src.is_file():
        available = list_skill_scripts(skill_name)
        return (
            f"[ERROR/FileNotFoundError] script not found: "
            f"{skill_name}/scripts/{script_name} (available: {available or 'none'})"
        )
    dest_dir = get_settings().workspace_dir / ".labhandler" / "scripts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)
    return (
        f"script staged at: /workspace/.labhandler/scripts/{src.name}"
        f" (run it with sandbox_execute_bash, e.g. `python /workspace/.labhandler/scripts/{src.name}`)"
    )


def list_skills() -> list[dict]:
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]


def get_skill_body(skill_name: str) -> str | None:
    try:
        doc = load_skill_document(skill_name)
    except FileNotFoundError:
        return None
    return doc["body"]


def list_skill_meta() -> list[dict[str, str]]:
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]
