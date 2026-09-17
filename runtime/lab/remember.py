"""本 lab 适用的 /remember 条文。REMEMBER.json 与 SPEC/NOTES 分开。

remember_judge 只裁定 applies；未点名的条目默认不适用。
步骤 Judge 的 rule_verdicts 必须盖住适用列表，否则不能 finish。
"""

from pathlib import Path
from typing import Any

from runtime.lab.persist import read_json, write_json

REMEMBER_FILE = "REMEMBER.json"


def catalog_rules(profile: dict[str, Any]) -> list[str]:
    prefs = profile.get("preferences") or {}
    return [str(r).strip() for r in (prefs.get("style_rules") or []) if str(r).strip()]


def applied_from_payload(catalog: list[str], payload: dict[str, Any]) -> list[str]:
    """按 index 取回规则原文。

    以前靠模型把 rule 原文抄回来做精确匹配，抄错（改写、错字）时那条裁定会被
    静默丢掉——applies=true 也等于没说。编号是稳定标识，原文只当标注。
    """
    chosen: list[str] = []
    for row in payload.get("verdicts") or []:
        if not isinstance(row, dict) or not row.get("applies"):
            continue
        text = _resolve(catalog, row)
        if text and text not in chosen:
            chosen.append(text)
    return chosen


def _resolve(catalog: list[str], row: dict[str, Any]) -> str:
    """index 优先。degraded 模式的 schema 不带 index，退回原文精确匹配。"""
    index = row.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        return catalog[index] if 0 <= index < len(catalog) else ""
    text = str(row.get("rule") or "").strip()
    return text if text in catalog else ""


def load_applied(session_dir: Path) -> list[str] | None:
    raw = read_json(session_dir, REMEMBER_FILE)
    if not isinstance(raw, dict):
        return None
    rules = raw.get("applied")
    if not isinstance(rules, list):
        return []
    return [str(r).strip() for r in rules if str(r).strip()]


def save_applied(session_dir: Path, rules: list[str]) -> None:
    write_json(session_dir, REMEMBER_FILE, {"applied": rules})


def rules_satisfied(applied: list[str], verdict: dict[str, Any]) -> bool:
    """步骤 Judge 必须盖住 applied 里的每一条。对齐靠 index，与 applied_from_payload 同一套。"""
    if not applied:
        return True
    ok: set[str] = set()
    for row in verdict.get("rule_verdicts") or []:
        if not isinstance(row, dict) or not row.get("satisfied"):
            continue
        text = _resolve(applied, row)
        if text:
            ok.add(text)
    return all(rule in ok for rule in applied)
