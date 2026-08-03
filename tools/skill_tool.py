"""skill_tool - 为 agent system prompt 组装加载 skills + Coder 按需读 reference/script。

Skills 布局（agentskills.io 兼容 frontmatter）：
  skills/<name>/SKILL.md（精简 SOP）+ skills/<name>/references/*.md（详细材料）
  + skills/<name>/scripts/*（可执行脚本）

  ---
  name: coding
  description: ...
  when_to_use: ...
  ---
  # 标题

  Body SOP（markdown）

渐进式披露：SKILL.md body 拼入 system prompt（第一层）；
SOP 提到 references/ 材料时，Coder 调 load_skill_reference 按需读取
（第二层）；SOP 提到 scripts/ 脚本时，Coder 调 use_skill_script 复制
进 workspace 再在沙箱执行（第三层）。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool

from skills.repository import (
    list_skill_documents,
    list_skill_scripts,
    load_skill_document,
    load_skill_reference as _load_skill_reference,
)


@tool
def load_skill(skill_name: str) -> dict:
    """Load skills/<skill_name>/SKILL.md. Returns {name, description, when_to_use, body}."""
    return load_skill_document(skill_name)


@tool
def load_skill_reference(skill_name: str, ref_name: str) -> str:
    """Read a skill's detailed reference material skills/<skill_name>/references/<ref_name> on demand.

    When the SOP (the skill body in the system prompt) mentions material under references/,
    use this tool to read the full text, e.g. load_skill_reference("coding", "testing.md").
    If ref_name does not exist, returns the available list.
    """
    try:
        return _load_skill_reference(skill_name, ref_name)
    except (FileNotFoundError, PermissionError) as e:
        # 返回错误串而非抛出，让 ReAct 拿到可用列表自纠正
        return f"[ERROR/{type(e).__name__}] {e}"


@tool
def use_skill_script(skill_name: str, script_name: str) -> str:
    """Copy a skill's bundled script skills/<skill_name>/scripts/<script_name> into the workspace
    and return a sandbox-executable path.

    The sandbox only mounts the workspace; the skills/ directory is invisible inside the
    container — when the SOP mentions a scripts/ script, use this tool to get the path, then
    run it with sandbox_execute_bash, e.g.:
      use_skill_script("lab_report", "plot_template.py")
      -> sandbox_execute_bash "python /workspace/.labhandler/scripts/plot_template.py"
    If the script does not exist, returns the available list.
    """
    import shutil

    from config.runtime import get_settings

    src = (
        get_settings().skills_dir / skill_name / "scripts" / Path(script_name).name
    )
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


@tool
def list_skills() -> list[dict]:
    """List all skills (reads frontmatter only, does not return body)."""
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]


# 非工具 helper（用于 agent 启动时组装 system prompt）
def get_skill_body(skill_name: str) -> str | None:
    try:
        doc = load_skill_document(skill_name)
    except FileNotFoundError:
        return None
    return doc["body"]


def list_skill_meta() -> list[dict[str, str]]:
    """返回所有 skills 的 frontmatter（name + description + when_to_use），不含 body。

    供 Intake 节点做 skill 匹配/分类。
    """
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]
