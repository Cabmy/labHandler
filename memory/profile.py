"""Profile 模块 - 读写 profile/me.yaml + 注入 agent system prompt。

设计要点：
1. 单一 me.yaml 文件为唯一事实源；不用 DB（个人 profile 不需历史）
2. update_field / add_field 用原子写：先 .tmp 再 rename（崩溃安全）
3. inject_for_agent：将关键 profile 字段拼入 agent system prompt
4. 点号路径："identity.name" / "preferences.writing_style.formality"
5. 不用 pydantic - YAML -> dict 即可（保持轻量）
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from config.runtime import get_settings


# ─── 内部 helper ─────────────────────────────────────────────


def _profile_path() -> Path:
    """从 settings 解析 profile YAML 路径。"""
    return get_settings().profile_path


def _atomic_write(data: dict[str, Any]) -> None:
    """原子将 dict 写入 YAML：先 .tmp 再 POSIX rename。"""
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    # POSIX 原子 rename；Windows 回退用 shutil.move
    try:
        os.replace(tmp, path)
    except Exception:
        shutil.move(str(tmp), str(path))


# ─── 公开 API ───────────────────────────────────────────────────


def load_profile() -> dict[str, Any]:
    """读 profile YAML；文件缺失或解析出错时返回空 dict。"""
    path = _profile_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def get_profile() -> dict[str, Any]:
    """返回当前 profile dict（load_profile 的便捷别名）。"""
    return load_profile()


def update_field(dotted_path: str, value: Any) -> dict[str, Any]:
    """更新点号路径处的字段（path 必须已存在）。

    示例：update_field("preferences.writing_style.formality", "high")
    """
    data = load_profile()
    parts = dotted_path.split(".")
    cur = data
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(f"Path does not exist: {dotted_path} (broken at {p})")
        cur = cur[p]
    if not isinstance(cur, dict):
        raise KeyError(f"Path is not a dict: {dotted_path}")
    cur[parts[-1]] = value
    _atomic_write(data)
    return data


def add_field(dotted_path: str, value: Any) -> dict[str, Any]:
    """在点号路径处新增字段（缺失的父节点自动创建为 dict）。"""
    data = load_profile()
    parts = dotted_path.split(".")
    cur = data
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
    _atomic_write(data)
    return data


def inject_for_agent(agent_name: str, system_prompt: str) -> str:
    """将关键 profile 字段拼到 system prompt 末尾，供所有产出类 agent 使用。

    注入对象：coder / planner / verifier / summarizer。
    Intake 跳过（只解析题面，不产出）。

    style_rules 段附带优先级声明：长期规则 > skill SOP（全 skill 类型通用），
    补上 config/prompts.py:412（profile < 题面）与本文件 rules < 当轮用户指令
    之外缺失的那条轴。verifier 的 stage-2 判官同样经本函数注入
    （agents/verifier.py:467），故写侧与判侧口径一致。
    """
    agent = agent_name.lower()
    if agent not in {"coder", "planner", "verifier", "summarizer", "pro", "flash"}:
        return system_prompt

    data = load_profile()
    if not data:
        return system_prompt

    identity = data.get("identity") or {}
    prefs = data.get("preferences") or {}

    lines = ["", "## User preferences (profile)"]
    if identity:
        name = identity.get("name", "")
        sid = identity.get("student_id", "")
        if name or sid:
            lines.append(f"- User: {name} (student ID {sid})")
    if prefs:
        lang = prefs.get("language")
        if lang:
            lines.append(f"- Language: {lang}")
        ws = prefs.get("writing_style") or {}
        if ws:
            formality = ws.get("formality", "medium")
            avg_len = ws.get("avg_sentence_len", 25)
            lines.append(
                f"- Writing style: formality={formality}, avg sentence len≈{avg_len}"
            )
        cs = prefs.get("coding_style") or {}
        if cs:
            th = "requires type hints" if cs.get("type_hints") else "type hints optional"
            ds = cs.get("docstring", "short")
            lines.append(f"- Coding style: {th}; docstring={ds}")

    # 自由文本规则（经 /remember 累积）
    rules = prefs.get("style_rules") or []
    rules = [str(r).strip() for r in rules if str(r).strip()]
    if rules:
        lines.append("")
        lines.append("## User long-term rules (accumulated via /remember)")
        lines.append("")
        lines.append(
            "### Precedence (holds for every skill type: coding / essay / lab_report / other)"
        )
        lines.append(
            "- These long-term rules **outrank the current skill SOP**. When a rule conflicts with any "
            "convention stated in the `## Current skill SOP` section (placeholder format, file naming, "
            "section wording, writing style, ...), the rule is authoritative and the SOP's conflicting "
            "wording is void — this holds even when the SOP demonstrates its own version through concrete "
            "examples in a loaded reference file."
        )
        lines.append(
            "- Not affected by the above: current-turn user instructions still take precedence over these "
            "rules; the problem statement's own explicit requirements and the academic-integrity block are "
            "separate sources and are not overridden here."
        )
        lines.append("")
        for r in rules:
            lines.append(f"- {r}")

    return system_prompt + "\n".join(lines)


def append_rule(rule: str) -> dict[str, Any]:
    """向 preferences.style_rules（list）追加一条自由文本规则。

    /remember 命令的后端；复用 _atomic_write 避免覆盖式的 add_field。
    """
    text = (rule or "").strip()
    if not text:
        raise ValueError("rule is empty")
    data = load_profile()
    prefs = data.setdefault("preferences", {})
    if not isinstance(prefs, dict):
        prefs = {}
        data["preferences"] = prefs
    rules = prefs.get("style_rules")
    if not isinstance(rules, list):
        rules = []
    rules.append(text)
    prefs["style_rules"] = rules
    _atomic_write(data)
    return data
