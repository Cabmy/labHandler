"""skills 读写仓储：统一 frontmatter 解析、列表/正文读取与编辑落盘。

布局（progressive disclosure 三层）：
skills/<name>/SKILL.md      — frontmatter + 精简 SOP（拼入 agent system prompt）
skills/<name>/references/   — 详细材料，由 Coder 通过 load_skill_reference 按需读取
skills/<name>/scripts/      — 可执行脚本，由 Coder 通过 use_skill_script 复制进 workspace 后在沙箱执行

写入只有一个入口 apply_skill_operations（/edit_skill 编辑判官的落盘层），
apply 前整批校验，一条非法整批拒绝。
"""

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
    return get_settings().skills_dir / skill_name / "SKILL.md"


def _references_dir(skill_name: str) -> Path:
    return get_settings().skills_dir / skill_name / "references"


def _scripts_dir(skill_name: str) -> Path:
    return get_settings().skills_dir / skill_name / "scripts"


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
    for p in sorted(skills_dir.glob("*/SKILL.md")):
        meta, body = _parse_skill_md(p.read_text(encoding="utf-8"))
        out.append(
            {
                "name": str(meta.get("name", p.parent.name)),
                "description": str(meta.get("description", "")),
                "when_to_use": str(meta.get("when_to_use", "")),
                "body": body.strip(),
                "file_name": str(p.relative_to(skills_dir)),
            }
        )
    return out


def list_skill_references(skill_name: str) -> list[str]:
    """列出 skill 的 references/ 材料文件名（无则空列表）。"""
    ref_dir = _references_dir(skill_name)
    if not ref_dir.is_dir():
        return []
    return sorted(p.name for p in ref_dir.glob("*.md"))


def list_skill_scripts(skill_name: str) -> list[str]:
    """列出 skill 的 scripts/ 可执行脚本文件名（不限后缀；无则空列表）。"""
    sc_dir = _scripts_dir(skill_name)
    if not sc_dir.is_dir():
        return []
    return sorted(p.name for p in sc_dir.glob("*") if p.is_file())


def load_skill_reference(skill_name: str, ref_name: str) -> str:
    """读取 skills/<name>/references/<ref_name> 全文（progressive disclosure 第二层）。

    ref_name 做 basename 化 + resolve 边界校验，防 ../ 越界读 skills 外文件。
    """
    ref_dir = _references_dir(skill_name)
    candidate = (ref_dir / Path(ref_name).name).resolve()
    try:
        candidate.relative_to(ref_dir.resolve())
    except ValueError as e:
        raise PermissionError(f"非法 reference 路径：{ref_name!r}") from e
    if not candidate.is_file():
        available = list_skill_references(skill_name)
        raise FileNotFoundError(
            f"reference 不存在：{skill_name}/references/{ref_name}"
            f"（可用：{available or '无'}）"
        )
    return candidate.read_text(encoding="utf-8")


# ─── /edit_skill 编辑读写层 ────────────────────────────────────────


def read_skill_files(skill_name: str) -> dict[str, str]:
    """读取 skill 全部文件，返回 {相对路径: 全文}（编辑判官拼上下文与算 diff 用）。

    覆盖 SKILL.md + references/*.md + scripts/*；skill 不存在抛 FileNotFoundError。
    """
    skill_dir = get_settings().skills_dir / skill_name
    if not _skill_file(skill_name).exists():
        raise FileNotFoundError(f"skill 不存在：{skill_name}（路径 {skill_dir}）")
    files: dict[str, str] = {
        "SKILL.md": _skill_file(skill_name).read_text(encoding="utf-8"),
    }
    for ref in list_skill_references(skill_name):
        files[f"references/{ref}"] = (_references_dir(skill_name) / ref).read_text(encoding="utf-8")
    for sc in list_skill_scripts(skill_name):
        files[f"scripts/{sc}"] = (_scripts_dir(skill_name) / sc).read_text(encoding="utf-8")
    return files


def _validate_operation(skill_name: str, op: dict[str, Any]) -> Path:
    """校验单条编辑操作，返回解析后的目标绝对路径；非法抛 PermissionError/ValueError。

    file 只允许三种形态：SKILL.md / references/<basename>.md / scripts/<basename>。
    白名单锁死在本 skill 目录内，结构上不可能创建新 skill 或越界写。
    """
    action = str(op.get("action", ""))
    file = str(op.get("file", ""))
    content = op.get("content", "")
    if action not in {"write", "delete"}:
        raise ValueError(f"非法 action：{action!r}（只允许 write/delete）")

    skill_dir = (get_settings().skills_dir / skill_name).resolve()
    parts = Path(file).parts
    if file == "SKILL.md":
        target = skill_dir / "SKILL.md"
    elif len(parts) == 2 and parts[0] == "references" and parts[1].endswith(".md"):
        target = skill_dir / "references" / Path(parts[1]).name
    elif len(parts) == 2 and parts[0] == "scripts":
        target = skill_dir / "scripts" / Path(parts[1]).name
    else:
        raise PermissionError(
            f"非法 file 路径：{file!r}"
            "（只允许 SKILL.md / references/<name>.md / scripts/<name>）"
        )
    # resolve 边界兜底（basename 化后理论上不可能越界，防御性保留）
    try:
        target.resolve().relative_to(skill_dir)
    except ValueError as e:
        raise PermissionError(f"越界 file 路径：{file!r}") from e

    if action == "delete" and file == "SKILL.md":
        raise PermissionError("禁止删除 SKILL.md（skill 主文件必须存在）")
    if action == "write":
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"write 操作 content 为空：{file}")
        if file == "SKILL.md":
            meta, _ = _parse_skill_md(content)
            if not content.startswith("---\n") or not meta.get("name"):
                raise ValueError(
                    "SKILL.md 内容必须以 '---' frontmatter 开头且含 name 字段"
                    "（丢失元数据会破坏 Intake 的 skill 匹配）"
                )
    return target


def apply_skill_operations(skill_name: str, operations: list[dict[str, Any]]) -> list[str]:
    """校验并落盘编辑操作。apply 前整批校验，一条非法整批拒绝（不做半应用）。

    Returns: 已应用的 file 相对路径清单。
    """
    validated: list[tuple[dict[str, Any], Path]] = [
        (op, _validate_operation(skill_name, op)) for op in operations
    ]
    applied: list[str] = []
    for op, target in validated:
        if op["action"] == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(op["content"]), encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        applied.append(str(op["file"]))
    return applied
