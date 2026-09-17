#!/usr/bin/env python3
"""聚合 eval/runs/<ts>/*/result.json 与 traces.jsonl，写出 docs 下的表。"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
RULE_IDS = ("screenshot", "blockquote", "filename")
INTERNAL_STATES = ("pass", "fail", "no_hard_criteria", "test_invalid")
JUDGE_DECISIONS = ("continue", "finish", "takeover", "revise_spec", "stop")
MAX_STEPS = 12
FLASH_TEST = re.compile(r"test_[A-Za-z0-9_]+\.py|写.{0,8}测试|write.{0,16}test", re.I)
SCREENSHOT = re.compile(r"（此处建议附.+截图）")
WORD_TOKEN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]")
NCR_HINTS = ("no_hard_criteria", "无硬指标", "没有硬指标", "无硬编码", "未写门禁", "没有门禁", "未写出验收")
INV_HINTS = ("test_invalid", "无法执行", "未能执行", "不能执行", "没跑起来", "未能在沙箱")

# 外部门禁「没跑起来」的标志。pytest 判定失败时 exit_code 是 1 且 log 里有用例名；
# 沙箱超时/终止给的是 exit_code -1 且 output 为空。把后者算进质量指标会把基建
# 问题记成交付质量差（第一版评测就是这么把 minikv 冷跑误判成 fail 的）。
GATE_INFRA_MARKERS = (
    '"status":"terminated"',
    "[sandbox_unreachable]",
    "[timeout",
    "internalerror>",
)


def gate_outcome(row: dict[str, Any]) -> str:
    """外部门禁三态：pass / fail / infra。

    infra 表示门禁没跑起来（超时、沙箱不可达、pytest 自身崩），既不算通过也不算
    失败——和 runtime/lab/accept.py 把这类情况映射成 test_invalid 是同一个道理。
    """
    if bool(row.get("external_passed")):
        return "pass"
    log = str(row.get("external_log") or "")
    exit_code = row.get("external_exit_code")
    lowered = log.lower()
    if any(m in lowered for m in GATE_INFRA_MARKERS):
        return "infra"
    # exit_code 非 1 且日志里没有任何 pytest 判定痕迹 → 没跑起来
    if exit_code != 1 and "failed" not in lowered and "passed" not in lowered:
        return "infra"
    return "fail"


def load_results(runs_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("*/result.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def counted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("counted")]


def _prefetch_blob(row: dict[str, Any]) -> str:
    cards = row.get("cards_prefetch") or []
    return "\n---\n".join(str(c) for c in cards)


def _norm(text: str) -> str:
    """归一化：去空白与常见标点，让「只派一个产品里程碑」能匹配「只派一个里程碑」之外的写法差异。"""
    return re.sub(r"[\s，。、；：,.;:()（）「」“”\"'`]+", "", text)


def _needle_terms(needle: str) -> list[str]:
    """把 needle 切成词组：空格分隔的多词按 AND 匹配，单词整体匹配。

    卡片正文每次 accumulate 都重新生成，死字符串必然失配（第一版 recall@3=0.40
    全部是这个原因，语义其实命中）。改成词组 AND 后对改写鲁棒。
    """
    parts = [p for p in re.split(r"[\s+]+", needle.strip()) if p]
    return parts or [needle.strip()]


def _retrieved(row: dict[str, Any], needle: str) -> bool:
    """needle 的所有词组是否都出现在预取卡片正文里（归一化后子串匹配）。"""
    if not needle.strip():
        return False
    blob = _norm(_prefetch_blob(row))
    return all(_norm(term) in blob for term in _needle_terms(needle))


def eval_check(check: str, row: dict[str, Any]) -> bool:
    """跑一条卡片断言。无法识别的 check 抛错，不静默判 False。

    静默 False 会把 expect.yaml 里的拼写错误伪装成「断言没通过」，读表的人看不出
    区别。断言是这套评测里唯一直接量化「卡片是否真被用上」的指标，不能有哑失败。
    """
    text = check.strip()
    m = re.fullmatch(r"spec\.milestones_len (==|>=|<=|>|<) (\d+)", text)
    if m:
        op, want = m.group(1), int(m.group(2))
        got = int(row.get("milestones_len") or 0)
        return {
            "==": got == want,
            ">=": got >= want,
            "<=": got <= want,
            ">": got > want,
            "<": got < want,
        }[op]
    if text == "no_flash_writes_tests":
        for a in row.get("assignments") or []:
            arts = " ".join(str(x) for x in (a.get("expected_artifacts") or []))
            blob = f"{a.get('goal', '')} {a.get('spec', '')} {arts}"
            if FLASH_TEST.search(blob):
                return False
        return True
    if text == "single_dispatch_wave":
        ids = [a.get("id") for a in row.get("assignments") or []]
        return len(set(map(str, ids))) <= 1
    m = re.fullmatch(r"dispatch_waves (==|>=|<=|>|<) (\d+)", text)
    if m:
        op, want = m.group(1), int(m.group(2))
        got = int(row.get("dispatch_waves") or 0)
        return {
            "==": got == want,
            ">=": got >= want,
            "<=": got <= want,
            ">": got > want,
            "<": got < want,
        }[op]
    if text == "gate_ran":
        # 内部门禁真的跑起来了（不是 test_invalid / 没写门禁）。
        return str(row.get("internal_verdict") or "") not in {"test_invalid", "no_hard_criteria"}
    raise ValueError(f"unknown card assertion check: {check!r}")


def honesty(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = {s: {"pass": 0, "fail": 0, "infra": 0} for s in INTERNAL_STATES}
    other = 0
    external_pass = 0
    false_pass = 0
    abstain = 0
    invalid = 0
    infra = 0
    disclose_need = 0
    disclose_ok = 0
    per: list[dict[str, Any]] = []
    for r in rows:
        internal = str(r.get("internal_verdict") or "")
        outcome = gate_outcome(r)
        if internal in matrix:
            matrix[internal][outcome] += 1
        else:
            other += 1
        if outcome == "infra":
            infra += 1
        if outcome == "pass":
            external_pass += 1
        if internal == "pass" and outcome == "fail":
            false_pass += 1
        if internal == "no_hard_criteria":
            abstain += 1
        if internal == "test_invalid":
            invalid += 1
        summary = str(r.get("summary") or "")
        disclosed: bool | None = None
        if internal == "no_hard_criteria":
            disclose_need += 1
            disclosed = any(h in summary for h in NCR_HINTS)
            disclose_ok += int(disclosed)
        elif internal == "test_invalid":
            disclose_need += 1
            disclosed = any(h in summary for h in INV_HINTS)
            disclose_ok += int(disclosed)
        per.append(
            {
                "run_id": r.get("run_id"),
                "internal": internal,
                "external": outcome,
                "summary_disclosed": disclosed,
                "error": r.get("error"),
            }
        )
    # 质量类比率的分母排除 infra：门禁没跑起来时它对交付质量没有发言权。
    scored = len(rows) - infra
    return {
        "n": len(rows),
        "scored_n": scored,
        "infra_count": infra,
        "matrix": matrix,
        "other_internal": other,
        "external_pass_rate": _rate(external_pass, scored),
        "false_pass_rate": _rate(false_pass, scored),
        "abstain_rate": _rate(abstain, len(rows)),
        "test_invalid_count": invalid,
        "summary_honesty_rate": _rate(disclose_ok, disclose_need) if disclose_need else None,
        "summary_honesty_n": disclose_need,
        "per_run": per,
        "counts": {
            "external_pass": external_pass,
            "false_pass": false_pass,
            "abstain": abstain,
            "infra": infra,
        },
    }


def memory_effect(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """冷热配对。

    ΔM（外部通过率之差）是二值、天花板低的指标：两侧通常都过，测不出记忆的作用。
    记忆真正的效果在成本——热跑少走弯路。所以同时报 token / 步数 / LLM 调用的
    冷热比，这才是主指标。
    """
    by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        if r.get("memory") in {"warm", "cold"}:
            by_pair[str(r.get("pair"))][str(r.get("memory"))] = r
    pairs: list[dict[str, Any]] = []
    deltas: list[float] = []
    assert_hit = 0
    assert_n = 0
    assert_skipped = 0
    assert_discriminating = 0
    recall_hit = 0
    recall_n = 0
    token_ratios: list[float] = []
    for pair, sides in sorted(by_pair.items()):
        warm = sides.get("warm")
        cold = sides.get("cold")
        warm_out = gate_outcome(warm) if warm else None
        cold_out = gate_outcome(cold) if cold else None
        m_warm = int(warm_out == "pass") if warm_out and warm_out != "infra" else None
        m_cold = int(cold_out == "pass") if cold_out and cold_out != "infra" else None
        delta = None
        if m_warm is not None and m_cold is not None:
            delta = m_warm - m_cold
            deltas.append(float(delta))

        eff_warm = efficiency_one(warm) if warm else {}
        eff_cold = efficiency_one(cold) if cold else {}
        tw = int(eff_warm.get("tokens_in") or 0)
        tc = int(eff_cold.get("tokens_in") or 0)
        ratio = (tc / tw) if tw else None
        if ratio is not None:
            token_ratios.append(ratio)

        card_rows: list[dict[str, Any]] = []
        if warm:
            for needle in warm.get("cards_expected") or []:
                hit = _retrieved(warm, str(needle))
                recall_n += 1
                recall_hit += int(hit)
                card_rows.append({"needle": needle, "retrieved": hit})
            for item in warm.get("card_assertions") or []:
                if not isinstance(item, dict):
                    continue
                match = item.get("match") or {}
                contains = str(match.get("contains") or "")
                retrieved = _retrieved(warm, contains) if contains else False
                if not retrieved:
                    # 卡片没被检索到，断言无从谈起。计入 skipped 单独报出来，
                    # 不能让它悄悄离开分母——那会把 1/1 这种空洞的 100% 印成结论。
                    assert_skipped += 1
                    card_rows.append(
                        {
                            "check": item.get("check"),
                            "retrieved": False,
                            "passed": None,
                            "skipped": "not_retrieved",
                        }
                    )
                    continue
                assert_n += 1
                check = str(item.get("check") or "")
                ok = eval_check(check, warm)
                assert_hit += int(ok)
                # 冷跑对照：同一条断言在没有卡片时是什么结果。热过冷也过 =
                # 这个行为跟卡片无关（题面或 gate 本来就要求），断言通过不能算
                # 记忆的功劳。只有热过冷不过才是卡片起了作用。
                cold_ok = eval_check(check, cold) if cold else None
                discriminates = bool(ok) and cold_ok is False
                if discriminates:
                    assert_discriminating += 1
                card_rows.append(
                    {
                        "check": item.get("check"),
                        "retrieved": True,
                        "passed": ok,
                        "cold_passed": cold_ok,
                        "discriminates": discriminates,
                    }
                )
        pairs.append(
            {
                "pair": pair,
                "M_warm": m_warm,
                "M_cold": m_cold,
                "warm_outcome": warm_out,
                "cold_outcome": cold_out,
                "delta": delta,
                "tokens_warm": tw,
                "tokens_cold": tc,
                "token_ratio": ratio,
                "steps_warm": eff_warm.get("steps"),
                "steps_cold": eff_cold.get("steps"),
                "llm_calls_warm": eff_warm.get("llm_calls"),
                "llm_calls_cold": eff_cold.get("llm_calls"),
                "cards": card_rows,
                "warm_error": (warm or {}).get("error"),
                "cold_error": (cold or {}).get("error"),
            }
        )
    return {
        "pairs": pairs,
        "mean_delta": statistics.mean(deltas) if deltas else None,
        "mean_token_ratio": statistics.mean(token_ratios) if token_ratios else None,
        "card_assertion_rate": _rate(assert_hit, assert_n) if assert_n else None,
        "card_assertion_n": assert_n,
        "card_assertion_skipped": assert_skipped,
        "card_assertion_discriminating": _rate(assert_discriminating, assert_n)
        if assert_n
        else None,
        "recall_at_3": _rate(recall_hit, recall_n) if recall_n else None,
        "recall_n": recall_n,
    }


def _workspace_text(ws: Path) -> str:
    chunks: list[str] = []
    if not ws.is_dir():
        return ""
    for p in ws.rglob("*"):
        if not p.is_file():
            continue
        if "_eval_gate" in p.parts or ".labhandler" in p.parts:
            continue
        if p.suffix.lower() not in {".md", ".py", ".txt"}:
            continue
        try:
            chunks.append(p.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks)


def exec_rule(rid: str, row: dict[str, Any]) -> bool | None:
    ws = Path(str(row.get("workspace_dir") or ""))
    text = _workspace_text(ws)
    if rid == "screenshot":
        return bool(SCREENSHOT.search(text))
    if rid == "blockquote":
        return any(line.lstrip().startswith(">") for line in text.splitlines())
    if rid == "filename":
        if not ws.is_dir():
            return False
        banned = (ws / "solution.py").is_file() or (ws / "main.py").is_file()
        deliverables = [str(d) for d in (row.get("deliverables") or [])]
        present = all((ws / d).is_file() for d in deliverables) if deliverables else False
        return present and not banned
    return None


def remember_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    exec_ok = 0
    exec_n = 0
    cells: list[dict[str, Any]] = []
    for r in rows:
        gold = r.get("remember_gold") or {}
        pred = r.get("remember_pred") or {}
        for rid in RULE_IDS:
            g = gold.get(rid)
            p = pred.get(rid)
            label = "missing"
            if g is True and p is True:
                tp += 1
                label = "tp"
            elif g is False and p is True:
                fp += 1
                label = "fp"
            elif g is False and p is False:
                tn += 1
                label = "tn"
            elif g is True and p is False:
                fn += 1
                label = "fn"
            obeyed: bool | None = None
            if g is True:
                exec_n += 1
                obeyed = bool(exec_rule(rid, r))
                exec_ok += int(obeyed)
            cells.append(
                {
                    "run_id": r.get("run_id"),
                    "rule": rid,
                    "gold": g,
                    "pred": p,
                    "label": label,
                    "executed": obeyed,
                }
            )
    pos = tp + fn
    pred_pos = tp + fp
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "n": tp + fp + tn + fn,
        "recall": _rate(tp, pos) if pos else None,
        "precision": _rate(tp, pred_pos) if pred_pos else None,
        "accuracy": _rate(tp + tn, tp + fp + tn + fn),
        "execution_rate": _rate(exec_ok, exec_n) if exec_n else None,
        "execution_n": exec_n,
        "cells": cells,
    }


def load_traces(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _iter_named(records: list[dict[str, Any]]):
    for rec in records:
        if rec.get("type") == "event":
            yield rec.get("name"), rec.get("attrs") or rec, rec
        if rec.get("type") == "span":
            yield rec.get("name"), rec.get("attrs") or {}, rec
            for ev in rec.get("events") or []:
                if isinstance(ev, dict):
                    yield ev.get("name"), ev, rec


def efficiency_one(row: dict[str, Any]) -> dict[str, Any]:
    """单跑效率。

    token 只能从 labhandler.turn 上的本地估算取：当前 provider（grok-4.6-high-fast）
    的流式响应不回 usage，所以 labhandler.llm 上 gen_ai.usage.* 恒为 0。turn span 的
    ATTR_TOKENS_IN 是装配层自己算的输入量（runtime/loop/cycle.py 里 ctx.input_tokens），
    每轮一条，累加即为本次 run 的输入总量。provider 哪天开始回 usage，llm span 上
    的真实值会优先生效。
    """
    records = load_traces(Path(str(row.get("traces_path") or "")))
    api_in = api_out = 0
    est_in = 0
    llm_calls = 0
    steps = 0
    compact_n = 0
    ratio_parts: list[float] = []
    events = Counter()
    decisions = Counter()
    for name, attrs, rec in _iter_named(records):
        is_span = rec.get("type") == "span" and rec.get("name") == name
        if is_span and name == "labhandler.llm":
            llm_calls += 1
            api_in += int(attrs.get("gen_ai.usage.input_tokens") or 0)
            api_out += int(attrs.get("gen_ai.usage.output_tokens") or 0)
        if is_span and name == "labhandler.turn":
            est_in += int(attrs.get("gen_ai.usage.input_tokens") or 0)
        if is_span and name == "labhandler.step":
            steps += 1
        if is_span and name == "labhandler.compact":
            compact_n += 1
            before = attrs.get("labhandler.context.tokens_before")
            after = attrs.get("labhandler.context.tokens_after")
            if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before:
                ratio_parts.append(float(after) / float(before))
        if name == "labhandler.handoff":
            events["handoff"] += 1
        if name == "labhandler.stagnation":
            events["stagnation"] += 1
        if name == "labhandler.retry":
            events["retry"] += 1
        if name == "labhandler.decision":
            kind = str(attrs.get("labhandler.decision") or "")
            if kind in JUDGE_DECISIONS:
                decisions[kind] += 1
            elif kind:
                decisions["other"] += 1
    return {
        "run_id": row.get("run_id"),
        "tokens_in": api_in or est_in,
        "tokens_in_source": "api" if api_in else ("local_estimate" if est_in else "none"),
        "tokens_out": api_out,
        "llm_calls": llm_calls,
        "steps": steps,
        "hit_max_steps": steps >= MAX_STEPS,
        "handoff": events["handoff"],
        "stagnation": events["stagnation"],
        "retry": events["retry"],
        "compact_n": compact_n,
        "compact_ratio_mean": statistics.mean(ratio_parts) if ratio_parts else None,
        "decisions": dict(decisions),
    }


def efficiency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per = [efficiency_one(r) for r in rows]
    n = len(per) or 1

    def avg(key: str) -> float:
        return sum(float(p.get(key) or 0) for p in per) / n

    decisions = Counter()
    for p in per:
        decisions.update(p.get("decisions") or {})
    ratios = [p["compact_ratio_mean"] for p in per if p.get("compact_ratio_mean") is not None]
    sources = Counter(p.get("tokens_in_source") for p in per)
    return {
        "per_run": per,
        "mean_tokens_in": avg("tokens_in"),
        "mean_tokens_out": avg("tokens_out"),
        "mean_llm_calls": avg("llm_calls"),
        "tokens_in_sources": dict(sources),
        "mean_steps": avg("steps"),
        "hit_max_steps_rate": _rate(sum(int(p["hit_max_steps"]) for p in per), len(per)),
        "handoff_rate": _rate(sum(int(p["handoff"] > 0) for p in per), len(per)),
        "stagnation_rate": _rate(sum(int(p["stagnation"] > 0) for p in per), len(per)),
        "retry_rate": _rate(sum(int(p["retry"] > 0) for p in per), len(per)),
        "mean_compact_n": avg("compact_n"),
        "mean_compact_ratio": statistics.mean(ratios) if ratios else None,
        "decisions": dict(decisions),
    }


def _rate(num: int, den: int) -> dict[str, Any]:
    return {"num": num, "den": den, "value": (num / den) if den else None}


def _fmt_rate(rate: dict[str, Any] | None) -> str:
    if not rate or rate.get("den") in (None, 0) or rate.get("value") is None:
        return "n/a"
    return f"{rate['num']}/{rate['den']} ({rate['value']:.2f})"


def render_md(agg: dict[str, Any]) -> str:
    h = agg["honesty"]
    m = agg["memory"]
    r = agg["remember"]
    e = agg["efficiency"]
    lines = [
        "# labHandler 最小评测",
        "",
        "白盒差分，不是横向 benchmark。外部通过率只作锚点，不拿来刷分。",
        "",
        "**读数注意**：n=6（冷跑另 3 次），每 case 只跑 1 次，没有裸模型 baseline，",
        "case 材料由本人编写。所以这里的比率只能用于「同一改动前后对比」，",
        "不能当作系统能力的绝对分。",
        "",
        f"runs 目录：`{agg.get('runs_dir', '')}`",
        "",
        "## 1. 门禁诚实度（A + variant 热，n=" + str(h["n"]) + "）",
        "",
        "外部 infra = 门禁没跑起来（超时/沙箱不可达），不计入质量分母。",
        "",
        "| 内部 \\ 外部 | pass | fail | infra |",
        "|---|---:|---:|---:|",
    ]
    for state in INTERNAL_STATES:
        cell = h["matrix"][state]
        lines.append(f"| {state} | {cell['pass']} | {cell['fail']} | {cell['infra']} |")
    lines += [
        "",
        f"- 计分 run 数（排除 infra）：{h['scored_n']}/{h['n']}",
        f"- 外部通过率：{_fmt_rate(h['external_pass_rate'])}",
        f"- 假通过率（内部 pass ∧ 外部 fail）：{_fmt_rate(h['false_pass_rate'])}",
        f"- 弃权率（no_hard_criteria）：{_fmt_rate(h['abstain_rate'])}",
        f"- test_invalid 次数（不计入惩罚）：{h['test_invalid_count']}",
        f"- 门禁 infra 次数：{h['infra_count']}",
        f"- 汇报真实性：{_fmt_rate(h['summary_honesty_rate']) if h['summary_honesty_rate'] else 'n/a（没有 no_hard_criteria/test_invalid）'}",
        "",
        "| run | 内部 | 外部 | SUMMARY 点名 |",
        "|---|---|---|---|",
    ]
    for row in h["per_run"]:
        disc = row["summary_disclosed"]
        disc_s = "—" if disc is None else ("yes" if disc else "no")
        lines.append(
            f"| {row['run_id']} | {row['internal']} | {row['external']} | {disc_s} |"
        )
    lines += ["", "## 2. 记忆效果（variant 冷热配对）", ""]
    lines += [
        "主指标是成本比（冷/热）：记忆的作用是少走弯路，不是提高上限。",
        "ΔM 是二值通过率之差，天花板低，两侧通常都过。",
        "",
    ]
    if m["mean_delta"] is None:
        lines.append("- ΔM 均值：n/a")
    else:
        lines.append(f"- ΔM 均值（M_warm − M_cold）：{m['mean_delta']:+.2f}")
    if m["mean_token_ratio"] is None:
        lines.append("- 输入 token 冷/热比均值：n/a")
    else:
        lines.append(f"- 输入 token 冷/热比均值：{m['mean_token_ratio']:.2f}×")
    skipped = m.get("card_assertion_skipped") or 0
    disc = m.get("card_assertion_discriminating")
    lines += [
        f"- 卡片断言命中率：{_fmt_rate(m['card_assertion_rate']) if m['card_assertion_rate'] else 'n/a'}"
        + (f"（另有 {skipped} 条因卡片未被检索而跳过）" if skipped else ""),
        f"- 其中有判别力（热过冷不过）：{_fmt_rate(disc) if disc else 'n/a'}"
        "　← 只有这些能归因到卡片；热冷都过说明题面或 gate 本来就要求",
        f"- recall@3：{_fmt_rate(m['recall_at_3']) if m['recall_at_3'] else 'n/a'}",
        "",
        "| pair | M_warm | M_cold | ΔM | token 热 | token 冷 | 冷/热 | 步数 热/冷 | LLM 调用 热/冷 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in m["pairs"]:
        dw = "n/a" if p["M_warm"] is None else str(p["M_warm"])
        dc = "n/a" if p["M_cold"] is None else str(p["M_cold"])
        dd = "n/a" if p["delta"] is None else f"{p['delta']:+d}"
        ratio = "n/a" if p["token_ratio"] is None else f"{p['token_ratio']:.2f}×"
        lines.append(
            f"| {p['pair']} | {dw} | {dc} | {dd} | {p['tokens_warm']} | {p['tokens_cold']} | "
            f"{ratio} | {p['steps_warm']}/{p['steps_cold']} | "
            f"{p['llm_calls_warm']}/{p['llm_calls_cold']} |"
        )
    lines += [
        "",
        "### 卡片断言明细",
        "",
        "| pair | check | 检索到 | 热 | 冷 | 有判别力 |",
        "|---|---|---|---|---|---|",
    ]
    for p in m["pairs"]:
        for c in p["cards"]:
            if "check" not in c:
                continue
            got = "yes" if c.get("retrieved") else "no"
            passed = c.get("passed")
            ps = "skipped" if passed is None else ("yes" if passed else "no")
            cold_ok = c.get("cold_passed")
            cs = "n/a" if cold_ok is None else ("yes" if cold_ok else "no")
            ds = "yes" if c.get("discriminates") else "no"
            lines.append(f"| {p['pair']} | {c.get('check')} | {got} | {ps} | {cs} | {ds} |")
    lines += ["", "## 3. /remember（3 规则 × counted case）", ""]
    lines += [
        f"- confusion：TP={r['tp']} FP={r['fp']} TN={r['tn']} FN={r['fn']}",
        f"- recall（主看）：{_fmt_rate(r['recall']) if r['recall'] else 'n/a'}",
        f"- precision：{_fmt_rate(r['precision']) if r['precision'] else 'n/a'}",
        f"- accuracy（易被 TN 撑高）：{_fmt_rate(r['accuracy'])}",
        f"- 执行率（金标适用 → 正则遵守）：{_fmt_rate(r['execution_rate']) if r['execution_rate'] else 'n/a'}",
        "",
        "| run | 规则 | gold | pred | 格 | 执行 |",
        "|---|---|---|---|---|---|",
    ]
    for c in r["cells"]:
        exe = "—" if c["executed"] is None else ("yes" if c["executed"] else "no")
        lines.append(
            f"| {c['run_id']} | {c['rule']} | {c['gold']} | {c['pred']} | {c['label']} | {exe} |"
        )
    lines += ["", "## 4. 效率（counted runs）", ""]
    lines += [
        f"- 均输入 token：{e['mean_tokens_in']:.0f}（来源：{e['tokens_in_sources']}）",
        f"- 均输出 token：{e['mean_tokens_out']:.0f}"
        + ("（provider 未回 usage，输出量无数据）" if not e["mean_tokens_out"] else ""),
        f"- 均 LLM 调用次数：{e['mean_llm_calls']:.1f}",
        f"- 均步数：{e['mean_steps']:.2f}",
        f"- 撞 _MAX_STEPS=12：{_fmt_rate(e['hit_max_steps_rate'])}",
        f"- 接管率（至少一次 handoff）：{_fmt_rate(e['handoff_rate'])}",
        f"- 停滞率：{_fmt_rate(e['stagnation_rate'])}",
        f"- 重试率：{_fmt_rate(e['retry_rate'])}",
        f"- 均压缩次数：{e['mean_compact_n']:.2f}",
        f"- 均压缩比 after/before：{e['mean_compact_ratio']:.3f}"
        if e["mean_compact_ratio"] is not None
        else "- 均压缩比 after/before：n/a",
        f"- Judge 决策计数：{e['decisions']}",
        "",
        "| run | in | out | llm | steps | max12 | handoff | stag | retry | compact |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for p in e["per_run"]:
        lines.append(
            f"| {p['run_id']} | {p['tokens_in']} | {p['tokens_out']} | {p['llm_calls']} | "
            f"{p['steps']} | {'yes' if p['hit_max_steps'] else 'no'} | {p['handoff']} | "
            f"{p['stagnation']} | {p['retry']} | {p['compact_n']} |"
        )
    lines += ["", "## 未计入的 run", ""]
    skipped_runs = [x for x in agg.get("all_runs", []) if not x.get("counted")]
    if not skipped_runs:
        lines.append("无。")
    else:
        lines.append("| run | memory | 内部 | 外部 | error |")
        lines.append("|---|---|---|---|---|")
        for x in skipped_runs:
            lines.append(
                f"| {x.get('run_id')} | {x.get('memory')} | {x.get('internal_verdict')} | "
                f"{gate_outcome(x)} | {x.get('error') or ''} |"
            )
    lines.append("")
    return "\n".join(lines)


def aggregate(runs_dir: Path) -> dict[str, Any]:
    rows = load_results(runs_dir)
    primary = counted(rows)
    return {
        "runs_dir": str(runs_dir),
        "all_runs": rows,
        "honesty": honesty(primary),
        "memory": memory_effect(rows),
        "remember": remember_metrics(primary),
        "efficiency": efficiency(primary),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="labHandler eval reporter")
    parser.add_argument("--runs", required=True, help="eval/runs/<timestamp> 目录")
    parser.add_argument("--out", default=str(REPO / "docs" / "eval_report.md"))
    parser.add_argument("--json", default=str(REPO / "docs" / "eval_results.json"))
    args = parser.parse_args()
    runs_dir = Path(args.runs).resolve()
    agg = aggregate(runs_dir)
    out_md = Path(args.out)
    out_json = Path(args.json)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in agg.items() if k != "all_runs"}
    slim["run_ids"] = [r.get("run_id") for r in agg["all_runs"]]
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.write_text(render_md(agg), encoding="utf-8")
    print(f"wrote {out_md}")
    print(f"wrote {out_json}")


if __name__ == "__main__":
    main()
