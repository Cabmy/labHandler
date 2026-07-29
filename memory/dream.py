"""/dream 离线知识治理。

从 SQLite 取全部活跃卡片，按 (card_type, task_type) 分组，每组一次 LLM 调用
判定 合并/去重/淘汰（含被后续任务证伪的 lesson），然后执行：
- 新增 merged 卡（挂到最新 source 卡所属 task）
- 软删被合并/淘汰卡（archive_cards.retired_at，检索与索引 SQL 均过滤）
- rebuild_archive_index() 全量重建 Chroma + BM25

触发入口：CLI /dream 或 Web POST /api/dream。纯离线操作，不碰主图 state。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from config.prompts import DREAM_SYSTEM, parse_result_json
from llm import get_llm
from memory.archive import VALID_CARD_TYPES, get_task_archive

# 组内卡片数低于该值不值得治理（单卡无从去重/证伪，保守跳过）
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
    """一组一次 LLM 调用。Returns: (merged_cards, retire_ids, error)。

    输出校验（防 LLM 越权）：retire_ids / source_ids 必须都在本组 card_id 内；
    merged type 强制回本组 card_type；非法条目丢弃。
    """
    group_ids = {c["card_id"] for c in cards}
    llm = get_llm()
    try:
        resp = llm.invoke(
            [
                SystemMessage(content=DREAM_SYSTEM),
                HumanMessage(content=_build_group_msg(card_type, task_type, cards)),
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = parse_result_json(text)
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
            continue  # 合并至少要吸收 2 张原卡，否则视为无效决策
        merged.append({
            "type": card_type if card_type in VALID_CARD_TYPES else "lesson",
            "content": content,
            "source_ids": source_ids,
        })
        # 被合并的原卡必须淘汰（LLM 漏报时兜底补上）
        for sid in source_ids:
            if sid not in retire_ids:
                retire_ids.append(sid)

    return merged, retire_ids, None


def run_dream() -> dict[str, Any]:
    """执行一轮离线治理。

    Returns:
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

        # 先写 merged 卡（挂到最新 source 卡的 task，保留最近上下文归属）
        for m in merged:
            newest = card_by_id[max(m["source_ids"])]
            ids = archive.create_cards(
                newest["task_id"],
                [{"type": m["type"], "content": m["content"]}],
                str(newest.get("task_title", "")),
                task_type,
            )
            if not ids:
                # 写入被吞（如 UNIQUE 去重）：source 卡保留，避免退了旧卡又没有新卡
                retire_ids = [i for i in retire_ids if i not in m["source_ids"]]
                continue
            created_ids.extend(ids)
            report["merged_created"] += len(ids)
            # E3：高频 pattern 卡建议人工晋升为 skill reference（只提示，不自动写入）
            if m["type"] == "pattern" and len(m["source_ids"]) >= 3:
                report["promotion_suggestions"].append(
                    f"pattern 卡（合并自 {len(m['source_ids'])} 张，task_type={task_type}）"
                    f"可考虑晋升为 skills/{task_type}/references/ 材料："
                    f"{m['content'][:80]}…"
                )

        all_retire.extend(retire_ids)

    if all_retire:
        report["retired"] = archive.retire_cards(sorted(set(all_retire)))

    # 有任何写操作才值得全量重建索引
    if created_ids or report["retired"]:
        from rag.archive_retriever import rebuild_archive_index

        report["reindex"] = rebuild_archive_index()

    return report
