#!/usr/bin/env python3
"""把 suite 的 result.json 收成 docs/eval.md 里的分数向量。不合成总分。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

_SUITE = Path(__file__).resolve().parent / "suite"


def _rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.glob("*/result.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def _rate(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return num / den


def _infra(row: dict[str, Any]) -> bool:
    """沙箱超时、pytest 自身崩溃才是 infra。没交出可导入的代码算失败，不从分母里拿掉。"""
    if row.get("error"):
        return True
    if not row.get("hidden_infra"):
        return False
    log = str(row.get("hidden_log") or "").lower()
    return (
        "no module named pytest" in log
        or "internalerror" in log
        or "[timeout" in log
    )


def _measured(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if not _infra(r)]


def _gold_needles(case: str) -> list[str]:
    """金标卡正文的首行。预取落盘带完整正文，用子串判断有没有检到这张卡。"""
    path = _SUITE / case / "expect.yaml"
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    needles: list[str] = []
    for card in data.get("cards") or []:
        content = str(card.get("content") or "")
        line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
        if line:
            needles.append(line)
    return needles


def _prefetch_precision(rows: list[dict[str, Any]]) -> float | None:
    """top-3 里金标卡占比。冲突题两张都要在，只检到旧卡分子记 0。空预取算一次失误。"""
    num = den = 0
    for row in rows:
        case = str(row.get("case") or "")
        if case not in {"mem_transfer", "mem_conflict"} or row.get("memory") == "empty":
            continue
        needles = _gold_needles(case)
        if not needles:
            continue
        got = [str(x) for x in (row.get("prefetch") or [])]
        blob = "\n".join(got)
        hits = sum(1 for needle in needles if needle in blob)
        den += max(len(got), 1)
        if hits == len(needles):
            num += hits
    return _rate(num, den)


def _f1(rows: list[dict[str, Any]]) -> float | None:
    tp = fp = fn = 0
    for row in rows:
        if row.get("ablation") == "no_remember":
            continue
        gold = {int(i) for i in (row.get("gold_applies") or [])}
        pred = set()
        for item in row.get("remember") or []:
            if item.get("applies") and isinstance(item.get("index"), int):
                pred.add(int(item["index"]))
        for i in pred & gold:
            tp += 1
        fp += len(pred - gold)
        fn += len(gold - pred)
    if tp + fp + fn == 0:
        return None
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    if prec + rec == 0:
        return 0.0
    return 2 * prec * rec / (prec + rec)


def vector(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counted = [r for r in rows if r.get("memory") != "empty"]
    measured = _measured(counted)
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in measured:
        by_case.setdefault(str(row.get("case")), []).append(row)
    pass3 = 0
    pass3_den = 0
    for case_rows in by_case.values():
        if len(case_rows) < 3:
            continue
        pass3_den += 1
        if all(r.get("hidden_passed") for r in case_rows[:3]):
            pass3 += 1
    hidden_hits = sum(1 for r in measured if r.get("hidden_passed"))
    smoke_rows = [r for r in measured if not r.get("smoke_infra")]
    smoke_hits = sum(1 for r in smoke_rows if r.get("smoke_passed"))
    hidden_rate = _rate(hidden_hits, len(measured))
    smoke_rate = _rate(smoke_hits, len(smoke_rows))
    hack_gap = None
    if hidden_rate is not None and smoke_rate is not None:
        hack_gap = smoke_rate - hidden_rate

    def _hidden_rate(case: str, memory: str) -> float | None:
        subset = _measured(
            [r for r in rows if r.get("case") == case and r.get("memory") == memory]
        )
        return _rate(sum(1 for r in subset if r.get("hidden_passed")), len(subset))

    gold = _hidden_rate("mem_transfer", "gold")
    empty = _hidden_rate("mem_transfer", "empty")
    lift = None if gold is None or empty is None else gold - empty

    abstain_num = abstain_den = 0
    for row in measured:
        if not row.get("abstain"):
            continue
        abstain_den += 1
        text = "\n".join(str(x) for x in (row.get("prefetch") or []))
        if not text.strip():
            abstain_num += 1
    compact_rows = [r for r in measured if r.get("compact_required") == "required"]
    compact_ok = sum(
        1
        for r in compact_rows
        if r.get("signature_ok") and r.get("frozen_ok") and int(r.get("compact_n") or 0) > 0
    )
    false_finish = sum(
        1
        for r in measured
        if r.get("judge_finished")
        and (
            not r.get("hidden_passed")
            or not r.get("rules_ok")
            or r.get("verdict") == "no_hard_criteria"
        )
    )
    token_rows = [r for r in measured if r.get("hidden_passed")]
    tokens = [
        int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in token_rows
    ]
    return {
        "runs": len(rows),
        "measured": len(measured),
        "hidden_pass@1": hidden_rate,
        "hidden_pass^3": _rate(pass3, pass3_den),
        "hack_gap": hack_gap,
        "memory_lift": lift,
        "prefetch_precision": _prefetch_precision(measured),
        "abstain_rate": _rate(abstain_num, abstain_den),
        "compact_obey": _rate(compact_ok, len(compact_rows)),
        "remember_f1": _f1(measured),
        "false_finish": _rate(false_finish, len(measured)),
        "phase_violation": sum(int(r.get("phase_violation") or 0) for r in rows),
        "tokens_per_pass": (sum(tokens) / len(tokens)) if tokens else None,
    }


def write_vector(root: Path) -> dict[str, Any]:
    data = vector(_rows(root))
    path = root / "vector.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    write_vector(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
