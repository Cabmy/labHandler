"""外部门禁：字数与小节。不判文采。agent 跑 lab 时看不见。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ["引言", "论点", "反方", "结论"]
WORD_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]")


def _essay() -> str:
    p = ROOT / "essay.md"
    assert p.is_file(), "missing essay.md"
    return p.read_text(encoding="utf-8")


def test_no_banned_filenames():
    for name in ("solution.py", "main.py"):
        assert not (ROOT / name).exists(), f"unexpected {name}"


def test_required_sections():
    text = _essay()
    missing = [s for s in SECTIONS if s not in text]
    assert not missing, f"missing sections: {missing}"


def test_word_count():
    n = len(WORD_RE.findall(_essay()))
    assert 800 <= n <= 1200, f"word count {n} not in [800, 1200]"
