from pathlib import Path


def test_file_exists():
    text = Path("essay.md").read_text(encoding="utf-8")
    assert "单机内存不能代替提交记录" in text
