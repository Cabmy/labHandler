import re
from pathlib import Path

SECTIONS = ["实验目的", "实验环境", "实验原理", "实验步骤", "实验数据", "结果分析", "问题与讨论", "结论"]
QUOTE = "本实验在单机 Python 3.11 上完成，不使用外部数据库。"


def test_eight_sections_and_blockquote():
    text = Path("实验报告.md").read_text(encoding="utf-8")
    for title in SECTIONS:
        assert title in text
    quoted = [ln for ln in text.splitlines() if ln.startswith(">") and QUOTE in ln]
    assert quoted
    assert "（此处建议附" in text
    assert not re.search(r"已拍摄|如下图|见图\d", text)
