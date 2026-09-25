#!/usr/bin/env python3
"""每个带 oracle/ 的题先用隐藏测试和冒烟测试验收。不过的题不能进 live。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from eval.grade import hidden_passed, smoke_passed

SUITE = Path(__file__).resolve().parent / "suite"


def check_case(case_dir: Path) -> str:
    oracle = case_dir / "oracle"
    hidden = case_dir / "hidden"
    materials = case_dir / "materials"
    if not oracle.is_dir() or not hidden.is_dir():
        return "skip"
    work = Path(tempfile.mkdtemp(prefix=f"oracle-{case_dir.name}-"))
    try:
        ws = work / "workspace"
        shutil.copytree(materials, ws)
        for src in oracle.iterdir():
            dest = ws / src.name
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)
        ok, infra, log = hidden_passed(ws, hidden)
        if not ok or infra:
            return f"hidden fail infra={infra}\n{log}"
        sok, sinfra, slog = smoke_passed(ws)
        if not sok or sinfra:
            return f"smoke fail infra={sinfra}\n{slog}"
        return "ok"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    failed = 0
    for case_dir in sorted(p for p in SUITE.iterdir() if (p / "expect.yaml").is_file()):
        status = check_case(case_dir)
        print(f"{case_dir.name}: {status.splitlines()[0]}")
        if status != "ok" and status != "skip":
            print(status)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
