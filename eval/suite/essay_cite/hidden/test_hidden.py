import re
from pathlib import Path


def test_direct_and_indirect():
    text = Path("essay.md").read_text(encoding="utf-8")
    assert re.search(r'"单机内存不能代替提交记录。"\s*—\s*王川\(2024\)《实验笔记》', text)
    assert re.search(r"\(王川,\s*2024\)", text)
    # 间接句不能只是把原句换个说法再贴一次而不标注。直接引用那句除外。
    body = text.replace('"单机内存不能代替提交记录。"', "")
    assert "单机内存不能代替提交记录" not in body
