"""skill 工具层：目录、SOP、reference、script。

一次 lab 至多绑定一个 skill（coding / essay / lab_report 互斥），也可以不绑。
load_skill 写入绑定；同名再调只是重读 SOP，换名直接拒绝。
load_skill_reference / use_skill_script 必须命中已绑定的那一份。
"""

from pathlib import Path

from skills.repository import (
    list_skill_documents,
    list_skill_references,
    list_skill_scripts,
    load_skill_document,
    load_skill_reference as _load_skill_reference,
)


LOAD_SKILL = "load_skill"
LOAD_SKILL_REFERENCE = "load_skill_reference"
USE_SKILL_SCRIPT = "use_skill_script"


def list_skill_meta() -> list[dict[str, str]]:
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]


def _available() -> str:
    names = [s["name"] for s in list_skill_documents()]
    return ", ".join(names) if names else "none"


class SkillBind:
    """一次 lab 的 skill 槽：空，或锁死一个名字。"""

    def __init__(self) -> None:
        self.name: str | None = None

    def load(self, skill_name: str) -> str:
        skill_name = (skill_name or "").strip()
        if not skill_name:
            return "[ERROR/Validation] missing skill_name"
        if self.name is not None and self.name != skill_name:
            return (
                f"[ERROR/Validation] skill already bound to {self.name!r}; "
                f"one skill per lab (available: {_available()})"
            )
        try:
            doc = load_skill_document(skill_name)
        except FileNotFoundError:
            return (
                f"[ERROR/FileNotFoundError] skill not found: {skill_name} "
                f"(available: {_available()})"
            )
        self.name = skill_name
        refs = list_skill_references(skill_name)
        scripts = list_skill_scripts(skill_name)
        lines = [f"# {doc['name'] or skill_name}", "", doc["body"]]
        if refs:
            lines.append("")
            lines.append(f"references: {', '.join(refs)}")
        if scripts:
            lines.append(f"scripts: {', '.join(scripts)}")
        return "\n".join(lines)

    def gate(self, skill_name: str) -> str:
        """空串放行；否则是给模型看的错误。"""
        skill_name = (skill_name or "").strip()
        if self.name is None:
            return "[ERROR/Validation] load_skill first (or skip skills this lab)"
        if skill_name != self.name:
            return (
                f"[ERROR/Validation] bound to {self.name!r}, not {skill_name!r}"
            )
        return ""


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
