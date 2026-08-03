"""离线知识治理（/dream）。

从 SQLite 取所有活跃卡片，按 (card_type, task_type) 分组，每组一次 LLM 调用
判定合并/去重/淘汰（含被后续任务证伪的 lesson），然后执行：
- 创建合并卡片（挂到最新源卡片的任务）
- 软删除被合并/淘汰的卡片（archive_cards.retired_at，检索与索引 SQL 均过滤）
- rebuild_archive_index() 全量重建 Chroma + BM25

触发入口：CLI /dream 或 Web POST /api/dream。纯离线操作，不碰主图状态。
"""

from __future__ import annotations

from typing import Any

from config.prompts import DREAM_SYSTEM
from llm import get_llm
from llm.invoke import invoke_llm_json
from memory.archive import VALID_CARD_TYPES, get_task_archive

# 卡片数少于这个值的分组不值得治理（单卡无从去重/证伪，保守跳过）
_MIN_GROUP_SIZE = 2


def _group_cards(cards: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in cards:
        key = (str(c.get("card_type", "")), str(c.get("task_type", "")))
        groups.setdefault(key, []).append(c)
    return groups


def _build_group_msg(card_type: str, task_type: str, cards: list[dict[str, Any]]) -> str:
    lines = [
        f"## 治理分组：card_type={card_type} / task_type={task_type}（共 {len(cards)} 张）",
        "card_id 越大越新（后归档的任务经验）。",
        "",
    ]
    for c in cards:
        lines.append(f"### card_id={c['card_id']}（来自任务：{c.get('task_title', '?')}）")
        lines.append(str(c.get("content", "")).strip())
        lines.append("")
    return "\n".join(lines)


def _judge_group(
    card_type: str, task_type: str, cards: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[int], str | None]:
    """每组一次 LLM 调用。返回：(merged_cards, retire_ids, error)。

    输出校验（防 LLM 越权）：retire_ids / source_ids 必须都在
    本组 card_ids 内；merged type 强制为本组 card_type；非法条目丢弃。
    """
    group_ids = {c["card_id"] for c in cards}
    llm = get_llm()
    try:
        data = invoke_llm_json(
            llm, DREAM_SYSTEM, _build_group_msg(card_type, task_type, cards)
        )
    except Exception as e:
        return [], [], f"{type(e).__name__}: {e}"

    retire_ids = [
        int(i) for i in (data.get("retire_ids") or [])
        if isinstance(i, (int, str)) and str(i).isdigit() and int(i) in group_ids
    ]

    merged: list[dict[str, Any]] = []
    for m in data.get("merged") or []:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content", "")).strip()
        source_ids = [
            int(i) for i in (m.get("source_ids") or [])
            if isinstance(i, (int, str)) and str(i).isdigit() and int(i) in group_ids
        ]
        if not content or len(source_ids) < 2:
            continue  # 合并必须吸收至少 2 张原卡，否则视为非法决策
        merged.append({
            "type": card_type if card_type in VALID_CARD_TYPES else "lesson",
            "content": content,
            "source_ids": source_ids,
        })
        # 合并的源卡片必须淘汰（LLM 漏报时的兜底）
        for sid in source_ids:
            if sid not in retire_ids:
                retire_ids.append(sid)

    return merged, retire_ids, None


def run_dream() -> dict[str, Any]:
    """执行一轮离线治理。

    返回：
        {groups: n, judged: n, merged_created: n, retired: n,
         promotion_suggestions: [...], errors: [...]}
    """
    archive = get_task_archive()
    cards = archive.get_all_active_cards()
    groups = _group_cards(cards)

    report: dict[str, Any] = {
        "total_cards": len(cards),
        "groups": len(groups),
        "judged": 0,
        "merged_created": 0,
        "retired": 0,
        "promotion_suggestions": [],
        "errors": [],
    }

    all_retire: list[int] = []
    created_ids: list[int] = []
    card_by_id = {c["card_id"]: c for c in cards}

    for (card_type, task_type), group in sorted(groups.items()):
        if len(group) < _MIN_GROUP_SIZE:
            continue
        merged, retire_ids, err = _judge_group(card_type, task_type, group)
        report["judged"] += 1
        if err:
            report["errors"].append(f"[{card_type}/{task_type}] {err}")
            continue

        # 先写合并卡片（挂到最新源卡片的任务，保留最近上下文）
        for m in merged:
            newest = card_by_id[max(m["source_ids"])]
            ids = archive.create_cards(
                newest["task_id"],
                [{"type": m["type"], "content": m["content"]}],
                str(newest.get("task_title", "")),
                task_type,
            )
            if not ids:
                # 写入被吞（如 UNIQUE 去重）：保留源卡片，避免淘汰旧卡却没有新卡
                retire_ids = [i for i in retire_ids if i not in m["source_ids"]]
                continue
            created_ids.extend(ids)
            report["merged_created"] += len(ids)
            # E3：高频 pattern 卡建议晋升为 skill reference（仅提示，不自动写）
            if m["type"] == "pattern" and len(m["source_ids"]) >= 3:
                report["promotion_suggestions"].append(
                    f"pattern 卡（合并自 {len(m['source_ids'])} 张，task_type={task_type}）"
                    f"可考虑晋升为 skills/{task_type}/references/ 材料："
                    f"{m['content'][:80]}…"
                )

        all_retire.extend(retire_ids)

    if all_retire:
        report["retired"] = archive.retire_cards(sorted(set(all_retire)))

    # 有写操作才值得全量重建索引
    if created_ids or report["retired"]:
        from rag.archive_retriever import rebuild_archive_index

        report["reindex"] = rebuild_archive_index()

    return report
