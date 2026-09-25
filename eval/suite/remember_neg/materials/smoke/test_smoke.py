from pathlib import Path


def test_report_exists():
    text = Path("实验报告.md").read_text(encoding="utf-8")
    assert "实验" in text
