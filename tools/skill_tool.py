"""skill_tool - 加载 skills 给 agent 拼 system prompt + Coder 按需读 references / 用 scripts

skills 布局（agentskills.io 兼容 frontmatter）：
  skills/<name>/SKILL.md（精简 SOP）+ skills/<name>/references/*.md（详细材料）
  + skills/<name>/scripts/*（可执行脚本）

  ---
  name: coding
  description: ...
  when_to_use: ...
  ---
  # 标题

  正文 SOP（markdown）

progressive disclosure：SKILL.md body 拼入 system prompt（第一层），
SOP 提到 references/ 材料时由 Coder 调 load_skill_reference 按需读取（第二层）；
SOP 提到 scripts/ 脚本时由 Coder 调 use_skill_script 复制进 workspace 后在沙箱执行（第三层）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from skills.repository import (
    list_skill_documents,
    list_skill_scripts,
    load_skill_document,
    load_skill_reference as _load_skill_reference,
)


@tool
def load_skill(skill_name: str) -> dict:
    """加载 skills/<skill_name>/SKILL.md。返回 {name, description, when_to_use, body}。"""
    return load_skill_document(skill_name)


@tool
def load_skill_reference(skill_name: str, ref_name: str) -> str:
    """按需读取 skill 的详细参考材料 skills/<skill_name>/references/<ref_name>。

    SOP（system prompt 里的 skill 正文）提到 references/ 下的材料时用本工具读全文，
    如 load_skill_reference("coding", "testing.md")。ref_name 不存在时会返回可用清单。
    """
    try:
        return _load_skill_reference(skill_name, ref_name)
    except (FileNotFoundError, PermissionError) as e:
        # 返回错误字符串而非抛异常，让 ReAct 拿到可用清单自纠正
        return f"[ERROR/{type(e).__name__}] {e}"


@tool
def use_skill_script(skill_name: str, script_name: str) -> str:
    """把 skill 附带的脚本 skills/<skill_name>/scripts/<script_name> 复制进 workspace，
    返回沙箱内可执行路径。

    沙箱只挂载 workspace，skills/ 目录容器不可见——SOP 提到 scripts/ 脚本时用本工具
    取得路径，再用 sandbox_execute_bash 执行，如：
      use_skill_script("lab_report", "plot_template.py")
      → sandbox_execute_bash "python /workspace/.labhandler/scripts/plot_template.py"
    脚本不存在时返回可用清单。
    """
    import shutil

    from config.runtime import get_settings

    src = (
        get_settings().skills_dir / skill_name / "scripts" / Path(script_name).name
    )
    if not src.is_file():
        available = list_skill_scripts(skill_name)
        return (
            f"[ERROR/FileNotFoundError] 脚本不存在："
            f"{skill_name}/scripts/{script_name}（可用：{available or '无'}）"
        )
    dest_dir = get_settings().workspace_dir / ".labhandler" / "scripts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)
    return (
        f"脚本已就位：/workspace/.labhandler/scripts/{src.name}"
        f"（用 sandbox_execute_bash 执行，如 `python /workspace/.labhandler/scripts/{src.name}`）"
    )


@tool
def list_skills() -> list[dict]:
    """列所有 skills（只读 frontmatter，不返回 body）。"""
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]


# 非 tool 辅助（agent 启动时拼 system prompt 用）
def get_skill_body(skill_name: str) -> Optional[str]:
    try:
        doc = load_skill_document(skill_name)
    except FileNotFoundError:
        return None
    return doc["body"]


def list_skill_meta() -> list[dict[str, str]]:
    """返回所有 skill 的 frontmatter（name + description + when_to_use），不含 body。

    供 Intake 节点做 skill 匹配分类用。
    """
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]
