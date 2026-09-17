#!/usr/bin/env python3
"""跑一个 eval case，或 --all 串行跑 9 次 lab。产物写入 --out 下的 result.json。

不调用 LabSession.done()（会把 workspace 推进 .trash）。A 的卡片由本脚本
create_cards + await index_card_ids 写入 pair 卡池。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CASES_DIR = Path(__file__).resolve().parent / "cases"
PROFILE_PATH = Path(__file__).resolve().parent / "profile.yaml"
RULE_IDS = ("screenshot", "blockquote", "filename")
PAIRS: list[tuple[str, str]] = [
    ("two_sum", "two_sum_variant"),
    ("minikv_lab05", "minikv_variant"),
    ("essay_a", "essay_variant"),
]


def load_expect(case: str) -> dict[str, Any]:
    path = CASES_DIR / case / "expect.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid expect.yaml: {path}")
    return data


def reset_singletons() -> None:
    from config.runtime import get_settings

    get_settings.cache_clear()
    import memory.archive as archive_mod
    import tools.policy as policy_mod

    archive_mod._default_archive = None
    policy_mod._policy = None
    policy_mod._auditor = None


def apply_env(*, workspace: Path, cards_dir: Path, memory_db: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    cards_dir.mkdir(parents=True, exist_ok=True)
    memory_db.parent.mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_DIR"] = str(workspace.resolve())
    os.environ["CARDS_DIR"] = str(cards_dir.resolve())
    os.environ["MEMORY_DB_PATH"] = str(memory_db.resolve())
    os.environ["PROFILE_PATH"] = str(PROFILE_PATH.resolve())
    reset_singletons()


def seed_workspace(case: str, workspace: Path) -> None:
    if workspace.exists():
        for child in workspace.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    workspace.mkdir(parents=True, exist_ok=True)
    materials = CASES_DIR / case / "materials"
    for src in materials.iterdir():
        dest = workspace / src.name
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)


def _payload(brief: Any) -> dict[str, Any]:
    if not isinstance(brief, dict):
        return {}
    payload = brief.get("payload")
    if isinstance(payload, dict):
        return payload
    return brief


def extract_state_facts(state: dict[str, Any]) -> dict[str, Any]:
    milestones: list[str] = []
    assignments: list[dict[str, Any]] = []
    remember_verdicts: list[dict[str, Any]] = []
    node_kinds: dict[str, int] = {}
    for node in (state.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        kind = node.get("kind")
        if kind:
            node_kinds[str(kind)] = node_kinds.get(str(kind), 0) + 1
        brief = node.get("brief") or {}
        payload = _payload(brief)
        if kind == "spec" and payload.get("milestones") is not None:
            milestones = [str(x) for x in (payload.get("milestones") or [])]
        if kind == "dispatch":
            for raw in payload.get("assignments") or []:
                if isinstance(raw, dict):
                    assignments.append(
                        {
                            "id": raw.get("id"),
                            "goal": str(raw.get("goal") or ""),
                            "spec": str(raw.get("spec") or ""),
                            "expected_artifacts": list(raw.get("expected_artifacts") or []),
                        }
                    )
        if kind == "worker":
            spec = node.get("node_spec") or {}
            if isinstance(spec, dict) and spec.get("goal"):
                assignments.append(
                    {
                        "id": spec.get("id"),
                        "goal": str(spec.get("goal") or ""),
                        "spec": str(spec.get("spec") or ""),
                        "expected_artifacts": list(spec.get("expected_artifacts") or []),
                    }
                )
        if kind == "remember_judge":
            remember_verdicts = [
                row for row in (payload.get("verdicts") or []) if isinstance(row, dict)
            ]
    pred: dict[str, bool | None] = {rid: None for rid in RULE_IDS}
    for row in remember_verdicts:
        idx = row.get("index")
        if isinstance(idx, int) and 0 <= idx < len(RULE_IDS):
            pred[RULE_IDS[idx]] = bool(row.get("applies"))
    return {
        "milestones": milestones,
        "milestones_len": len(milestones),
        "assignments": assignments,
        "remember_verdicts": remember_verdicts,
        "remember_pred": pred,
        "node_kinds": node_kinds,
        # 派发轮数 = dispatch 节点数。同一个里程碑反复重派时这个数会涨，
        # milestones_len 不会——后者冷热完全一致，没有判别力。
        "dispatch_waves": node_kinds.get("dispatch", 0),
        "takeovers": node_kinds.get("takeover", 0),
    }


def remember_gold(expect: dict[str, Any]) -> dict[str, bool]:
    raw = expect.get("remember") or {}
    return {rid: bool(raw.get(rid)) for rid in RULE_IDS}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def run_external_gate(workspace: Path, case: str, timeout: float) -> dict[str, Any]:
    from tools.sandbox_tools import sandbox_run

    dest = workspace / "_eval_gate"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CASES_DIR / case / "gate", dest)
    code, log = await sandbox_run(
        "cd /workspace && PYTHONPATH=/workspace python -m pytest -q _eval_gate",
        timeout=timeout,
    )
    return {
        "exit_code": int(code),
        "passed": code == 0,
        "log": (log or "")[-4000:],
    }


async def archive_knowledge(session: Any, result: dict[str, Any]) -> dict[str, Any]:
    from config.runtime import get_settings
    from memory.archive import TaskArchive
    from memory.retrieve import index_card_ids
    import memory.archive as archive_mod

    cards = result.get("knowledge_cards") or []
    if not cards:
        return {"skipped": "no_cards", "card_ids": []}
    settings = get_settings()
    archive = TaskArchive(str(settings.memory_db_path))
    archive_mod._default_archive = archive
    if not archive.has_new_cards(cards):
        return {"skipped": "no_new_cards", "card_ids": []}
    title = str(result.get("question") or "eval")
    summary = str(result.get("summary") or "")
    task_id = archive.create_task(title, "other", summary[:4000])
    card_ids = archive.create_cards(task_id, cards, title, "other")
    indexed: dict[str, Any] = {"indexed": 0, "failed": 0, "errors": []}
    if card_ids:
        indexed = await index_card_ids(card_ids, session.llm, settings)
    return {"task_id": task_id, "card_ids": card_ids, **indexed}


def snapshot_session(session_dir: Path) -> dict[str, Any]:
    from runtime.context.notes import load_cards
    from runtime.lab.persist import STATE_FILE
    from runtime.lab.remember import load_applied

    state: dict[str, Any] = {}
    state_path = session_dir / STATE_FILE
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    facts = extract_state_facts(state) if state else {
        "milestones": [],
        "milestones_len": 0,
        "assignments": [],
        "remember_verdicts": [],
        "remember_pred": {rid: None for rid in RULE_IDS},
        "node_kinds": {},
        "dispatch_waves": 0,
        "takeovers": 0,
    }
    cards = load_cards(session_dir)
    applied = load_applied(session_dir)
    return {
        "session_dir": str(session_dir),
        "cards_prefetch": cards if cards is not None else [],
        "cards_prefetched": cards is not None,
        "applied_rules": applied if applied is not None else [],
        **facts,
    }


async def run_one(
    *,
    case: str,
    memory: str,
    out_root: Path,
    pair: str,
    archive: bool,
) -> dict[str, Any]:
    expect = load_expect(case)
    run_id = f"{case}_{memory}"
    run_dir = out_root / run_id
    workspace = run_dir / "workspace"
    if memory == "cold":
        pool = out_root / "pools" / f"{pair}_cold"
    else:
        pool = out_root / "pools" / pair
    cards_dir = pool / "cards"
    memory_db = pool / "memory.db"
    apply_env(workspace=workspace, cards_dir=cards_dir, memory_db=memory_db)

    from config.runtime import get_settings
    from infra.sandbox_boot import recreate_sandbox
    from runtime.session import LabSession

    settings = get_settings()
    recreate_sandbox(log=print)
    seed_workspace(case, workspace)

    session = LabSession(settings=settings)
    question = str(expect.get("question") or "")
    lab_result: dict[str, Any] = {}
    error: str | None = None
    try:
        async def on_event(ev: dict[str, Any]) -> None:
            kind = ev.get("kind") or ""
            node = ev.get("node") or ""
            print(f"[eval {run_id}] {kind} {node}", flush=True)

        lab_result = await session.run(question, on_event=on_event)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        lab_result = {
            "verdict": "error",
            "summary": "",
            "knowledge_cards": [],
            "question": question,
        }

    snap = snapshot_session(session.session_path) if session.session_path.exists() else {
        "session_dir": str(session.session_path),
        "cards_prefetch": [],
        "cards_prefetched": False,
        "applied_rules": [],
        "milestones": [],
        "milestones_len": 0,
        "assignments": [],
        "remember_verdicts": [],
        "remember_pred": {rid: None for rid in RULE_IDS},
        "node_kinds": {},
        "dispatch_waves": 0,
        "takeovers": 0,
    }

    gate: dict[str, Any]
    try:
        gate = await run_external_gate(workspace, case, timeout=settings.tool_timeout_s)
    except Exception as exc:
        gate = {
            "exit_code": -1,
            "passed": False,
            "log": f"{type(exc).__name__}: {exc}",
        }

    archive_result: dict[str, Any] = {"skipped": "not_accumulate"}
    if archive and not error:
        try:
            archive_result = await archive_knowledge(session, lab_result)
        except Exception as exc:
            archive_result = {"error": f"{type(exc).__name__}: {exc}", "card_ids": []}

    traces_src = settings.traces_path
    traces_dst = run_dir / "traces.jsonl"
    if traces_src.is_file():
        shutil.copy2(traces_src, traces_dst)

    summary_path = workspace / "SUMMARY.md"
    summary = str(lab_result.get("summary") or "")
    if not summary and summary_path.is_file():
        summary = summary_path.read_text(encoding="utf-8")

    counted = memory != "cold"
    result = {
        "run_id": run_id,
        "case": case,
        "pair": pair,
        "role": expect.get("role"),
        "memory": memory,
        "counted": counted,
        "question": question,
        "internal_verdict": lab_result.get("verdict"),
        "external_passed": bool(gate.get("passed")),
        "external_exit_code": gate.get("exit_code"),
        "external_log": gate.get("log"),
        "summary": summary,
        "knowledge_cards": lab_result.get("knowledge_cards") or [],
        "archive": archive_result,
        "remember_gold": remember_gold(expect),
        "remember_pred": snap.get("remember_pred"),
        "applied_rules": snap.get("applied_rules"),
        "cards_prefetch": snap.get("cards_prefetch"),
        "cards_expected": list(expect.get("cards_expected") or []),
        "card_assertions": list(expect.get("card_assertions") or []),
        "milestones": snap.get("milestones"),
        "milestones_len": snap.get("milestones_len"),
        "assignments": snap.get("assignments"),
        "node_kinds": snap.get("node_kinds"),
        "dispatch_waves": snap.get("dispatch_waves"),
        "takeovers": snap.get("takeovers"),
        "deliverables": list(expect.get("deliverables") or []),
        "workspace_dir": str(workspace),
        "cards_dir": str(cards_dir),
        "memory_db": str(memory_db),
        "traces_path": str(traces_dst if traces_dst.is_file() else traces_src),
        "session_dir": snap.get("session_dir"),
        "thread_id": session.thread_id,
        "error": error,
    }
    write_json(run_dir / "result.json", result)
    print(
        f"[eval {run_id}] verdict={result['internal_verdict']} "
        f"external={result['external_passed']} archive={archive_result}",
        flush=True,
    )
    return result


async def run_all(out_root: Path) -> list[dict[str, Any]]:
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for accumulate, variant in PAIRS:
        expect = load_expect(accumulate)
        pair = str(expect.get("pair") or accumulate)
        print(f"\n===== pair {pair}: {accumulate} accumulate =====", flush=True)
        results.append(
            await run_one(
                case=accumulate,
                memory="accumulate",
                out_root=out_root,
                pair=pair,
                archive=True,
            )
        )
        print(f"===== pair {pair}: {variant} warm =====", flush=True)
        results.append(
            await run_one(
                case=variant,
                memory="warm",
                out_root=out_root,
                pair=pair,
                archive=False,
            )
        )
        print(f"===== pair {pair}: {variant} cold =====", flush=True)
        results.append(
            await run_one(
                case=variant,
                memory="cold",
                out_root=out_root,
                pair=pair,
                archive=False,
            )
        )
    write_json(out_root / "manifest.json", {"runs": [r["run_id"] for r in results]})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="labHandler eval runner")
    parser.add_argument("--all", action="store_true", help="跑 3 对 × (A + warm + cold)")
    parser.add_argument("--case", help="单个 case 目录名")
    parser.add_argument(
        "--memory",
        choices=("accumulate", "warm", "cold"),
        default="accumulate",
    )
    parser.add_argument("--pair", help="卡池名，默认取 expect.yaml 的 pair")
    parser.add_argument("--out", help="输出根目录，默认 eval/runs/<timestamp>")
    parser.add_argument(
        "--archive",
        action="store_true",
        help="单 case 时把 knowledge_cards 写入当前卡池",
    )
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "runs" / time.strftime(
        "%Y%m%d_%H%M%S"
    )
    out = out.resolve()
    if args.all:
        asyncio.run(run_all(out))
        return
    if not args.case:
        parser.error("需要 --all 或 --case")
    expect = load_expect(args.case)
    pair = args.pair or str(expect.get("pair") or args.case)
    archive = bool(args.archive or args.memory == "accumulate")
    asyncio.run(
        run_one(
            case=args.case,
            memory=args.memory,
            out_root=out,
            pair=pair,
            archive=archive,
        )
    )


if __name__ == "__main__":
    main()
