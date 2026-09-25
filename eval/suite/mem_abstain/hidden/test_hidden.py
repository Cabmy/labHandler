from pathlib import Path


def test_essay_stays_on_topic():
    text = Path("essay.md").read_text(encoding="utf-8")
    assert "文件" in text
    assert "MiniKV" not in text
    assert "TTL" not in text
