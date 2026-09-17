"""外部门禁：报告结构 + MiniKV 冒烟。agent 跑 lab 时看不见。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = [
    "实验目的",
    "实验原理",
    "实验环境",
    "实验步骤",
    "实验结果",
    "结果分析",
    "结论",
    "思考题",
]
PLACEHOLDER = re.compile(r"（此处建议附.+截图）")


def _report_text() -> str:
    candidates = [
        ROOT / "实验报告.md",
        ROOT / "lab_report.md",
        ROOT / "REPORT.md",
        ROOT / "report.md",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    md = [
        p
        for p in ROOT.glob("*.md")
        if p.name not in {"SUMMARY.md", "CATALOG.md", "TASK.md", "GUIDE.md"}
        and not str(p).startswith(str(ROOT / "_eval_gate"))
    ]
    assert md, "missing lab report markdown"
    return md[0].read_text(encoding="utf-8")


def test_report_has_eight_sections():
    text = _report_text()
    missing = [s for s in SECTIONS if s not in text]
    assert not missing, f"missing sections: {missing}"


def test_screenshot_placeholder():
    text = _report_text()
    assert PLACEHOLDER.search(text), "expected screenshot placeholder （此处建议附 XX 截图）"


def test_blockquote_quote():
    text = _report_text()
    assert any(line.lstrip().startswith(">") for line in text.splitlines()), (
        "expected a markdown blockquote quoting the lab guide"
    )


def test_minikv_roundtrip(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT))
    from minikv import MiniKV

    kv = MiniKV()
    kv.set("a", "1")
    kv.set("b", "2")
    assert kv.get("a") == "1"
    assert kv.delete("a") is True
    assert kv.get("a") is None
    assert kv.delete("missing") is False
    path = tmp_path / "snap.json"
    kv.save(str(path))
    other = MiniKV()
    other.load(str(path))
    assert other.get("b") == "2"
    assert "b" in other.keys()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    os.unlink(path)
