#!/usr/bin/env python3
"""按 docs/eval.md 跑 suite。Pro 固定 grok-4.7，Flash 沿用 .env 配置。不改 config/.env。

  PYTHONPATH=. python eval/run_suite.py --k 3
  PYTHONPATH=. python eval/run_suite.py --k 3 --ablation no_prefetch
消融：none | no_prefetch | no_compact | no_remember | no_parallel
"""

from __future__ import annotations
import yaml

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# 必须在 import config.runtime 之前钉住 Pro 模型。load_dotenv 不覆盖已有环境变量。
os.environ["PRO_MODEL"] = "grok-4.7"
os.environ.pop("FLASH_MODEL", None)  # Flash 沿用 .env（当前 deepseek-v4-flash）
# 评测进程不把 span 打到 Langfuse。留空会盖过 .env，本地 traces.jsonl 仍在。
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SUITE = Path(__file__).resolve().parent / "suite"
PROFILE = SUITE / "profile.yaml"


def _cases() -> list[Path]:
    return sorted(p for p in SUITE.iterdir() if (p / "expect.yaml").is_file())


def _load(case_dir: Path) -> dict[str, Any]:
    data = yaml.safe_load(
        (case_dir / "expect.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"bad expect: {case_dir}")
    return data


def _reset() -> None:
    from config.runtime import get_settings
    import memory.archive as archive_mod
    import tools.policy as policy_mod

    get_settings.cache_clear()
    archive_mod._default_archive = None
    policy_mod._policy = None
    policy_mod._auditor = None


def _apply_env(workspace: Path, cards: Path, db: Path, expect: dict[str, Any], ablation: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    cards.mkdir(parents=True, exist_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_DIR"] = str(workspace.resolve())
    os.environ["CARDS_DIR"] = str(cards.resolve())
    os.environ["MEMORY_DB_PATH"] = str(db.resolve())
    os.environ["PROFILE_PATH"] = str(PROFILE.resolve())
    os.environ["PRO_MODEL"] = "grok-4.7"
    os.environ.pop("FLASH_MODEL", None)
    budget = expect.get("budget")
    reserve = expect.get("reserve")
    if ablation == "no_compact":
        budget, reserve = 200000, 8192
    if budget:
        os.environ["CONTEXT_BUDGET_TOKENS"] = str(int(budget))
    else:
        os.environ.pop("CONTEXT_BUDGET_TOKENS", None)
    if reserve:
        os.environ["OUTPUT_RESERVE_TOKENS"] = str(int(reserve))
    else:
        os.environ.pop("OUTPUT_RESERVE_TOKENS", None)
    if ablation == "no_prefetch":
        os.environ["EVAL_DISABLE_PREFETCH"] = "1"
    else:
        os.environ.pop("EVAL_DISABLE_PREFETCH", None)
    if ablation == "no_remember":
        os.environ["EVAL_DISABLE_REMEMBER"] = "1"
    else:
        os.environ.pop("EVAL_DISABLE_REMEMBER", None)
    if ablation == "no_parallel":
        os.environ["EVAL_MAX_ASSIGNMENTS"] = "1"
    else:
        os.environ.pop("EVAL_MAX_ASSIGNMENTS", None)
    _reset()


def _seed_materials(case_dir: Path, workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(case_dir / "materials", workspace)


async def _seed_cards(expect: dict[str, Any], session: Any) -> list[int]:
    cards = expect.get("cards") or []
    if not cards:
        return []
    from config.runtime import get_settings
    from memory.archive import TaskArchive
    from memory.retrieve import index_card_ids, write_card_file
    import memory.archive as archive_mod

    settings = get_settings()
    archive = TaskArchive(str(settings.memory_db_path))
    archive_mod._default_archive = archive
    task_type = str(cards[0].get("task_type") or "coding")
    task_id = archive.create_task(
        str(expect.get("id")), task_type, "suite seed")
    ids: list[int] = []
    for card in cards:
        content = str(card.get("content") or "").strip()
        made = archive.create_cards(
            task_id,
            [{"type": card.get("type") or "lesson", "content": content}],
            str(card.get("title") or expect.get("id")),
            task_type,
        )
        for cid in made:
            write_card_file(
                {
                    "card_id": cid,
                    "task_id": task_id,
                    "card_type": card.get("type") or "lesson",
                    "content": content,
                    "task_title": card.get("title") or "",
                    "task_type": task_type,
                },
                settings,
            )
            ids.append(cid)
    if ids:
        await index_card_ids(ids, session.llm, settings)
    return ids


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _state(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "STATE.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _payload(node: dict[str, Any]) -> dict[str, Any]:
    brief = node.get("brief") or {}
    if isinstance(brief, dict) and isinstance(brief.get("payload"), dict):
        return brief["payload"]
    return brief if isinstance(brief, dict) else {}


def _judge_view(state: dict[str, Any]) -> dict[str, Any]:
    finished = False
    verdict: dict[str, Any] = {}
    remember: list[dict[str, Any]] = []
    for node in (state.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        kind = node.get("kind")
        payload = _payload(node)
        if kind == "judge" and payload.get("decision") == "finish":
            finished = True
            verdict = payload
        if kind == "remember_judge":
            remember = [row for row in (payload.get(
                "verdicts") or []) if isinstance(row, dict)]
    return {"finished": finished, "verdict": verdict, "remember": remember}


async def run_case(
    case_dir: Path,
    *,
    out_root: Path,
    k: int,
    ablation: str,
    memory: str,
) -> dict[str, Any]:
    from config.runtime import get_settings
    from eval.grade import hidden_passed, smoke_passed
    from eval.report import efficiency_one
    from infra.sandbox_boot import recreate_sandbox
    from memory.retrieve import prefetch_cards
    from runtime.context.notes import load_cards
    from runtime.lab.remember import load_applied, rules_satisfied
    from runtime.session import LabSession

    expect = _load(case_dir)
    case_id = str(expect["id"])
    run_id = f"{ablation}_{case_id}_k{k}_{memory}"
    run_dir = out_root / run_id
    workspace = run_dir / "workspace"
    pool = run_dir / "pool"
    _apply_env(workspace, pool / "cards", pool / "memory.db", expect, ablation)
    if memory == "empty":
        expect = {**expect, "cards": []}
    _seed_materials(case_dir, workspace)
    settings = get_settings()
    try:
        recreate_sandbox(log=print)
    except Exception as exc:
        print(f"[suite] sandbox boot: {type(exc).__name__}: {exc}", flush=True)

    session = LabSession(settings=settings)
    error = ""
    lab: dict[str, Any] = {}
    try:
        card_ids = await _seed_cards(expect, session)
    except Exception as exc:
        card_ids = []
        error = f"seed:{type(exc).__name__}: {exc}"
    question = str(expect.get("question") or "")
    if not error:
        try:
            lab = await session.run(question)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()

    hidden_ok, hidden_infra, hidden_log = False, True, "not graded"
    smoke_ok, smoke_infra, smoke_log = False, True, "not graded"
    try:
        hidden_ok, hidden_infra, hidden_log = hidden_passed(
            workspace, case_dir / "hidden")
        smoke_ok, smoke_infra, smoke_log = smoke_passed(workspace)
    except Exception as exc:
        hidden_infra = True
        hidden_log = f"{type(exc).__name__}: {exc}"

    session_dir = session.session_path
    state = _state(session_dir) if session_dir.exists() else {}
    view = _judge_view(state)
    applied = load_applied(session_dir) if session_dir.exists() else []
    rules_ok = rules_satisfied(applied or [], view["verdict"])
    cards_now = load_cards(session_dir) if session_dir.exists() else None
    prefetch = cards_now if cards_now is not None else []
    summary = str(lab.get("summary") or "")
    summary_path = workspace / "SUMMARY.md"
    if not summary and summary_path.is_file():
        summary = summary_path.read_text(encoding="utf-8")
    forget = ""
    forget_path = session_dir / "FORGET.md" if session_dir.exists() else None
    if forget_path and forget_path.is_file():
        forget = forget_path.read_text(encoding="utf-8")

    frozen_ok = None
    frozen_name = expect.get("frozen")
    if frozen_name:
        src = case_dir / "materials" / str(frozen_name)
        dst = workspace / str(frozen_name)
        frozen_ok = src.is_file() and dst.is_file() and _sha256(src) == _sha256(dst)
    signature_ok = None
    sig = expect.get("signature")
    if sig:
        blob = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in workspace.rglob("*.py")
            if "smoke" not in p.parts and not p.name.startswith("test_")
        )
        signature_ok = str(sig) in blob

    poison = str(expect.get("poison") or "")
    poison_prefetch = None
    if poison:
        try:
            again = await prefetch_cards(question, session.llm, settings)
        except Exception:
            again = []
        poison_prefetch = any(poison in item for item in again)

    traces_src = settings.traces_path
    traces_dst = run_dir / "traces.jsonl"
    if traces_src.is_file():
        shutil.copy2(traces_src, traces_dst)
    eff = efficiency_one({"traces_path": str(traces_dst), "run_id": run_id})
    phase_violation = 0
    if traces_dst.is_file():
        phase_violation = traces_dst.read_text(encoding="utf-8", errors="replace").count(
            "unavailable during"
        )

    row = {
        "run_id": run_id,
        "case": case_id,
        "group": expect.get("group"),
        "ablation": ablation,
        "k": k,
        "memory": memory,
        "models": {"pro": settings.pro_model, "flash": settings.flash_model},
        "context_budget_tokens": settings.context_budget_tokens,
        "prefetch_min_score": 0.25,
        "error": error,
        "hidden_passed": hidden_ok,
        "hidden_infra": hidden_infra,
        "hidden_log": hidden_log,
        "smoke_passed": smoke_ok,
        "smoke_infra": smoke_infra,
        "judge_finished": view["finished"],
        "rules_ok": rules_ok,
        "applied_rules": applied or [],
        "remember": view["remember"],
        "gold_applies": list(expect.get("gold_applies") or []),
        "forbid_applied": list(expect.get("forbid_applied") or []),
        "prefetch": prefetch,
        "abstain": bool(expect.get("abstain")),
        "poison": poison,
        "poison_in_forget": bool(poison) and poison in forget,
        "poison_in_summary": bool(poison) and poison in summary,
        "poison_prefetch": poison_prefetch,
        "frozen_ok": frozen_ok,
        "signature_ok": signature_ok,
        "compact_n": eff.get("compact_n"),
        "compact_required": expect.get("compact"),
        "tokens_in": eff.get("tokens_in") or 0,
        "tokens_out": eff.get("tokens_out") or 0,
        "phase_violation": phase_violation,
        "card_ids": card_ids,
        "verdict": lab.get("verdict"),
    }
    (run_dir / "result.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"[suite] {run_id} hidden={hidden_ok} infra={hidden_infra} smoke={smoke_ok} err={error[:120]}",
        flush=True,
    )
    close = getattr(session.llm, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception as exc:
            print(
                f"[suite] llm close: {type(exc).__name__}: {exc}", flush=True)
    return row


def _plans(cases: list[Path], k: int, ablation: str, only: set[str]) -> list[tuple[Path, int, str]]:
    plans: list[tuple[Path, int, str]] = []
    for case_dir in cases:
        expect = _load(case_dir)
        if only and expect["id"] not in only:
            continue
        memories = ["gold"]
        if expect["id"] == "mem_transfer":
            memories.append("empty")
        for i in range(1, k + 1):
            for memory in memories:
                plans.append((case_dir, i, memory))
    return plans


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument(
        "--ablation",
        default="none",
        choices=("none", "no_prefetch", "no_compact",
                 "no_remember", "no_parallel"),
    )
    parser.add_argument("--ids", default="", help="逗号分隔，只跑这些 case")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else REPO / "eval" / \
        "runs" / f"suite-{args.ablation}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    only = {x.strip() for x in args.ids.split(",") if x.strip()}
    plans = _plans(_cases(), args.k, args.ablation, only)
    print(f"[suite] {len(plans)} runs -> {out}", flush=True)
    for case_dir, k, memory in plans:
        await run_case(case_dir, out_root=out, k=k, ablation=args.ablation, memory=memory)
    from eval.score_suite import write_vector

    write_vector(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
