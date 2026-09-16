"""Progressive disclosure：SKILL.md 进 system，references 按需。"""

from __future__ import annotations

from tools.skill_tool import get_skill_body, list_skill_meta


def skill_system_block() -> str:
    metas = list_skill_meta()
    if not metas:
        return ""
    lines = ["## Skills (SOP inlined; load references/scripts on demand)", ""]
    for m in metas:
        body = get_skill_body(m["name"]) or ""
        lines.append(f"### Skill: {m['name']}")
        if m.get("description"):
            lines.append(m["description"].strip())
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines)
