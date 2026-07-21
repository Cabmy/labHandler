"""skills 读取仓储：统一 frontmatter 解析与列表/正文读取。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config.runtime import get_settings


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


def _skill_file(skill_name: str) -> Path:
    return get_settings().skills_dir / f"{skill_name}.md"


def load_skill_document(skill_name: str) -> dict[str, str]:
    path = _skill_file(skill_name)
    if not path.exists():
        raise FileNotFoundError(f"skill 不存在：{skill_name}（路径 {path}）")
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_skill_md(text)
    return {
        "name": str(meta.get("name", skill_name)),
        "description": str(meta.get("description", "")),
        "when_to_use": str(meta.get("when_to_use", "")),
        "body": body.strip(),
    }


def list_skill_documents() -> list[dict[str, str]]:
    skills_dir = get_settings().skills_dir
    if not skills_dir.exists():
        return []
    out: list[dict[str, str]] = []
    for p in sorted(skills_dir.glob("*.md")):
        meta, body = _parse_skill_md(p.read_text(encoding="utf-8"))
        out.append(
            {
                "name": str(meta.get("name", p.stem)),
                "description": str(meta.get("description", "")),
                "when_to_use": str(meta.get("when_to_use", "")),
                "body": body.strip(),
                "file_name": p.name,
            }
        )
    return out
