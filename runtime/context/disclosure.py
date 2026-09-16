"""技能披露：system 只放目录（name / description / when_to_use）。

SOP 由 Pro 经 load_skill 拉取，一次 lab 至多一份；references / scripts 再按需。
无 skill 时返回空串，system 不加 Skills 段。
"""

from tools.skill_tool import list_skill_meta


def skill_catalog_block() -> str:
    metas = list_skill_meta()
    if not metas:
        return ""
    lines = [
        "## Skills (optional; mutually exclusive)",
        "Call load_skill at most once this lab to pull one SOP, or skip.",
        "After that, load_skill_reference / use_skill_script only work for the bound skill.",
        "",
    ]
    for m in metas:
        lines.append(f"### {m['name']}")
        if m.get("description"):
            lines.append(m["description"].strip())
        if m.get("when_to_use"):
            lines.append(m["when_to_use"].strip())
        lines.append("")
    return "\n".join(lines)
