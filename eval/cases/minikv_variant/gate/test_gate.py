"""外部门禁：TTL MiniKV + 八节报告。agent 跑 lab 时看不见。"""
from __future__ import annotations

import re
import time
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
    for name in ("实验报告.md", "lab_report.md", "REPORT.md", "report.md"):
        p = ROOT / name
        if p.is_file():
            return p.read_text(encoding="utf-8")
    md = [
        p
        for p in ROOT.glob("*.md")
        if p.name not in {"SUMMARY.md", "CATALOG.md", "TASK.md", "GUIDE.md"}
        and "_eval_gate" not in p.parts
    ]
    assert md, "missing lab report markdown"
    return md[0].read_text(encoding="utf-8")


def test_report_has_eight_sections():
    text = _report_text()
    missing = [s for s in SECTIONS if s not in text]
    assert not missing, f"missing sections: {missing}"


def test_screenshot_placeholder():
    assert PLACEHOLDER.search(_report_text())


def test_blockquote_quote():
    text = _report_text()
    assert any(line.lstrip().startswith(">") for line in text.splitlines())


def test_expire_hides_key(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT))
    from minikv import MiniKV

    kv = MiniKV()
    kv.set("k", "v")
    assert kv.expire("k", 0.15) is True
    assert kv.expire("missing", 1.0) is False
    time.sleep(0.25)
    assert kv.get("k") is None
    assert "k" not in kv.keys()
    path = tmp_path / "snap.json"
    kv.save(str(path))
    other = MiniKV()
    other.load(str(path))
    assert other.get("k") is None
