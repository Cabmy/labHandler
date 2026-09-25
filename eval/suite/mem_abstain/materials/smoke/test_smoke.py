from pathlib import Path


def test_essay_exists():
    text = Path("essay.md").read_text(encoding="utf-8")
    assert len(text.strip()) > 40
