"""知识卡片的文件事实层：格式、原子写入、快照与检索文本。"""

import hashlib
from pathlib import Path
from typing import Any

import yaml

from config.runtime import RuntimeSettings, get_settings
from infra.files import atomic_write_text

VALID_CARD_TYPES = frozenset({"lesson", "strategy", "pattern"})


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cards_dir(settings: RuntimeSettings | None = None) -> Path:
    path = (settings or get_settings()).cards_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def card_path(card_id: int, settings: RuntimeSettings | None = None) -> Path:
    return cards_dir(settings) / f"{int(card_id)}.md"


def _parts(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise ValueError("卡片缺少 YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("卡片 frontmatter 未结束")
    meta = yaml.safe_load(text[4:end])
    if not isinstance(meta, dict):
        raise ValueError("卡片 frontmatter 必须是对象")
    return meta, text[end + 5:].strip()


def read_card(path: Path) -> dict[str, Any]:
    """正文与元数据只从当前文件读取，拒绝空白或格式损坏卡片。"""
    if path.is_symlink() or not path.stem.isdigit():
        raise ValueError("卡片必须是目录内的数字命名普通文件")
    meta, body = _parts(path.read_text(encoding="utf-8"))
    if int(meta["card_id"]) != int(path.stem):
        raise ValueError(f"卡片 id 与文件名不一致: {path.name}")
    if meta.get("card_type") not in VALID_CARD_TYPES or not body:
        raise ValueError(f"卡片类型非法或正文为空: {path.name}")
    return {**meta, "card_id": int(path.stem), "content": body}


def write_card_file(card: dict[str, Any], settings: RuntimeSettings | None = None) -> Path:
    body = str(card.get("content") or "").strip()
    ctype = card.get("card_type") or card.get("type")
    if not body or ctype not in VALID_CARD_TYPES:
        raise ValueError("卡片类型非法或正文为空")
    meta = {
        "card_id": int(card["card_id"]),
        "task_id": int(card["task_id"]),
        "card_type": ctype,
        "task_title": str(card.get("task_title") or ""),
        "task_type": str(card.get("task_type") or ""),
    }
    path = card_path(meta["card_id"], settings)
    atomic_write_text(path, "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
                      + "---\n\n" + body + "\n")
    return path


def embedding_text(card: dict[str, Any]) -> str:
    """嵌入包含适用背景；指纹覆盖实际送入模型的全部文本。"""
    return (f"任务类型: {card.get('task_type', '')}\n"
            f"卡片类型: {card['card_type']}\n"
            f"任务标题: {card.get('task_title', '')}\n"
            f"内容: {card['content']}")


def snapshot(settings: RuntimeSettings) -> tuple[dict[str, dict], list[str]]:
    cards: dict[str, dict] = {}
    errors: list[str] = []
    root = cards_dir(settings).resolve()
    for path in sorted(root.glob("*.md")):
        try:
            cards[str(path)] = read_card(path)
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as e:
            errors.append(f"{path.name}: {type(e).__name__}: {e}")
    return cards, errors
