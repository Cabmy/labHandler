#!/usr/bin/env python3
"""依次跑 T2（ablation=none）和四组 T3。模型由 run_suite 钉住（Pro=grok-4.7）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ABLATIONS = ("none", "no_prefetch", "no_compact", "no_remember", "no_parallel")


def main() -> int:
    k = sys.argv[1] if len(sys.argv) > 1 else "3"
    for name in ABLATIONS:
        print(f"[all] start ablation={name} k={k}", flush=True)
        proc = subprocess.run(
            [sys.executable, "-u", "eval/run_suite.py",
                "--k", k, "--ablation", name],
            cwd=ROOT,
        )
        print(f"[all] ablation={name} exit={proc.returncode}", flush=True)
        if proc.returncode != 0:
            return proc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
