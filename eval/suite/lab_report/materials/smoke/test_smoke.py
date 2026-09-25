from pathlib import Path


def test_has_purpose_section():
    text = Path("实验报告.md").read_text(encoding="utf-8")
    assert "实验目的" in text
