from pathlib import Path


def test_placeholder_not_fake_shot():
    text = Path("实验报告.md").read_text(encoding="utf-8")
    assert "（此处建议附" in text
    assert "已拍摄" not in text
