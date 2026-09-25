"""用户画像：profile/me.yaml 为唯一事实源。

点号路径读写（identity.name、preferences.writing_style.formality）。
写回走 runtime.lab.persist.atomic_write_text（tmp+fsync+rename+dir fsync）：
崩溃后磁盘上始终是一份完整 YAML。
load：文件缺失或 YAML 非法时得到空 dict，不抛。
inject_for_agent 把 identity / preferences / style_rules 拼进名单内 agent 的 system 末尾。
"""

from pathlib import Path
from typing import Any

import yaml

from config.runtime import get_settings


# ─── 内部 helper ─────────────────────────────────────────────


def _profile_path() -> Path:
    """返回 settings.profile_path。"""
    return get_settings().profile_path


def _atomic_write(data: dict[str, Any]) -> None:
    """把 dict 写成 YAML，走仓库统一原子写（tmp+fsync+rename+dir fsync）。

    延迟导入：runtime.lab.persist 的依赖链会回到 memory.profile，顶层 import 成环。
    """
    from runtime.lab.persist import atomic_write_text

    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    atomic_write_text(_profile_path(), text)


# ─── 公开 API ───────────────────────────────────────────────────


def load_profile() -> dict[str, Any]:
    """读 profile YAML 为 dict。文件缺失、非 dict、或解析失败 → 空 dict，不抛。"""
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


def update_field(dotted_path: str, value: Any) -> dict[str, Any]:
    """覆盖点号路径上已有字段，原子写回，返回写后的整份 dict。

    路径不存在或中间节点非 dict 时抛 KeyError。不创建缺失父节点。
    示例：update_field("preferences.writing_style.formality", "high")
    """
    data = load_profile()
    parts = dotted_path.split(".")
    cur = data
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(
                f"Path does not exist: {dotted_path} (broken at {p})")
        cur = cur[p]
    if not isinstance(cur, dict):
        raise KeyError(f"Path is not a dict: {dotted_path}")
    cur[parts[-1]] = value
    _atomic_write(data)
    return data


def add_field(dotted_path: str, value: Any) -> dict[str, Any]:
    """在点号路径写入字段，缺失的父节点建成空 dict，原子写回，返回写后的整份 dict。"""
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


def inject_for_agent(
    agent_name: str,
    system_prompt: str,
    *,
    rules: list[str] | None = None,
    include_rules: bool = True,
) -> str:
    """把 identity / preferences 以及本 lab 适用的 style_rules 拼到 system 末尾。

    仅 pro / flash 会注入。
    include_rules=False 时不写规则段（remember_judge 裁定前）。
    rules 非 None 时只用这份名单，不再读 profile 全量 style_rules。
    优先级：当轮用户指令 > 本 lab 适用规则 > skill SOP。
    """
    agent = agent_name.lower()
    if agent not in {"pro", "flash"}:
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
            th = "requires type hints" if cs.get(
                "type_hints") else "type hints optional"
            ds = cs.get("docstring", "short")
            lines.append(f"- Coding style: {th}; docstring={ds}")

    if include_rules and rules is None:
        rules = prefs.get("style_rules") or []
    chosen = [str(r).strip() for r in (rules or [])
              if str(r).strip()] if include_rules else []
    if include_rules:
        lines.append("")
        lines.append("## User long-term rules applicable to this lab")
        lines.append("")
        lines.append(
            "### Precedence (holds for every skill type: coding / essay / lab_report / other)"
        )
        lines.append(
            "- These long-term rules **outrank the current skill SOP**. When a rule conflicts with any "
            "convention in a loaded skill SOP (placeholder format, file naming, "
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
        if chosen:
            for r in chosen:
                lines.append(f"- {r}")
        else:
            lines.append("- （本 lab 没有适用的 /remember 规则）")

    return system_prompt + "\n".join(lines)


def append_rule(rule: str) -> dict[str, Any]:
    """在 preferences.style_rules 末尾追加一条规则，原子写回，返回写后的整份 dict。

    空字符串抛 ValueError。style_rules 缺失或非 list 时先建成空 list 再追加。
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
