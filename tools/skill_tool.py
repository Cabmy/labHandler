"""skill_tool - 加载 skills/*.md 给 agent 拼 system prompt

skills/*.md 格式（agentskills.io 兼容）：
  ---
  name: coding
  description: ...
  when_to_use: ...
  ---
  # 标题

  正文 SOP（markdown）
"""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from skills.repository import list_skill_documents, load_skill_document


@tool
def load_skill(skill_name: str) -> dict:
    """加载 skills/<skill_name>.md。返回 {name, description, when_to_use, body}。"""
    return load_skill_document(skill_name)


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
