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

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from langchain_core.tools import tool

SKILLS_DIR = Path(os.getenv("SKILLS_DIR", "./skills")).resolve()


def _parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """切 frontmatter / body。返回 (meta, body)。"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = text[end + 5 :]
    return meta, body


@tool
def load_skill(skill_name: str) -> dict:
    """加载 skills/<skill_name>.md。返回 {name, description, when_to_use, body}。"""
    path = SKILLS_DIR / f"{skill_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"skill 不存在：{skill_name}（路径 {path}）")
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_skill_md(text)
    return {
        "name": meta.get("name", skill_name),
        "description": meta.get("description", ""),
        "when_to_use": meta.get("when_to_use", ""),
        "body": body.strip(),
    }


@tool
def list_skills() -> list[dict]:
    """列所有 skills（只读 frontmatter，不返回 body）。"""
    out: list[dict] = []
    if not SKILLS_DIR.exists():
        return out
    for p in sorted(SKILLS_DIR.glob("*.md")):
        meta, _ = _parse_skill_md(p.read_text(encoding="utf-8"))
        out.append(
            {
                "name": meta.get("name", p.stem),
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
            }
        )
    return out


# 非 tool 辅助（agent 启动时拼 system prompt 用）
def get_skill_body(skill_name: str) -> Optional[str]:
    path = SKILLS_DIR / f"{skill_name}.md"
    if not path.exists():
        return None
    _, body = _parse_skill_md(path.read_text(encoding="utf-8"))
    return body.strip()


SKILL_TOOLS = [load_skill, list_skills]
