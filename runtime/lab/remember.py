"""本 lab 适用的 /remember 条文。REMEMBER.json 与 SPEC/MEMORY 分开。

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
    chosen: list[str] = []
    for row in payload.get("verdicts") or []:
        if not isinstance(row, dict) or not row.get("applies"):
            continue
        text = str(row.get("rule") or "").strip()
        if text in catalog and text not in chosen:
            chosen.append(text)
    return chosen


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
    if not applied:
        return True
    ok = {
        str(row.get("rule") or "").strip()
        for row in (verdict.get("rule_verdicts") or [])
        if isinstance(row, dict) and row.get("satisfied")
    }
    return all(rule in ok for rule in applied)
