"""离线知识治理（/dream）。

按 (card_type, task_type) 分组；组内至少 2 张才送 Pro 判定。
合并成功：新卡写入 archive + markdown + 向量索引；源卡 retired_at 置位并删文件。
合并写入失败：对应 source_ids 不淘汰。单组 LLM/解析失败记入 errors，其余组继续。
卡片文件为事实源；向量表为派生索引。
"""

from typing import Any

from config.prompts import DREAM_SYSTEM
from config.runtime import get_settings
from memory.archive import VALID_CARD_TYPES, get_task_archive
from memory.retrieve import delete_card_file, index_card_ids, write_card_file
from runtime.llm import LLMGateway
from runtime.loop.schema import DREAM_SCHEMA, SUBMIT_DREAM
from runtime.loop.parse import oneshot_schema

_MIN_GROUP_SIZE = 2  # 少于此张数的分组不进入治理


def _group_cards(cards: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """按 (card_type, task_type) 归组。组内顺序与输入一致。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in cards:
        key = (str(c.get("card_type", "")), str(c.get("task_type", "")))
        groups.setdefault(key, []).append(c)
    return groups


def _build_group_msg(card_type: str, task_type: str, cards: list[dict[str, Any]]) -> str:
    """编一份给 Pro 的分组正文。约定：card_id 越大越新。"""
    lines = [
        f"## 治理分组：card_type={card_type} / task_type={task_type}（共 {len(cards)} 张）",
        "card_id 越大越新。",
        "",
    ]
    for c in cards:
        lines.append(f"### card_id={c['card_id']}（来自任务：{c.get('task_title', '?')}）")
        lines.append(str(c.get("content", "")).strip())
        lines.append("")
    return "\n".join(lines)


async def _judge_group(
    llm: LLMGateway,
    card_type: str,
    task_type: str,
    cards: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int], str | None]:
    """对本组调用 submit_dream。返回 (merged, retire_ids, error)。

    retire_ids / source_ids 只保留本组已有 card_id。
    merged 要求 content 非空且至少两个 source；类型强制为本组 card_type（非法则 lesson）。
    LLM 失败时 merged/retire 为空，error 为异常摘要。
    """
    group_ids = {c["card_id"] for c in cards}
    try:
        data = await oneshot_schema(
            llm,
            model=get_settings().pro_model,
            system=DREAM_SYSTEM,
            user=_build_group_msg(card_type, task_type, cards),
            name=SUBMIT_DREAM,
            schema=DREAM_SCHEMA,
            description="Submit dream governance decision",
        )
    except Exception as e:
        return [], [], f"{type(e).__name__}: {e}"

    retire_ids = [
        int(i)
        for i in (data.get("retire_ids") or [])
        if isinstance(i, (int, str)) and str(i).isdigit() and int(i) in group_ids
    ]
    merged: list[dict[str, Any]] = []
    for m in data.get("merged") or []:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content", "")).strip()
        source_ids = [
            int(i)
            for i in (m.get("source_ids") or [])
            if isinstance(i, (int, str)) and str(i).isdigit() and int(i) in group_ids
        ]
        if not content or len(source_ids) < 2:
            continue
        merged.append(
            {
                "type": card_type if card_type in VALID_CARD_TYPES else "lesson",
                "content": content,
                "source_ids": source_ids,
            }
        )
        for sid in source_ids:
            if sid not in retire_ids:
                retire_ids.append(sid)
    return merged, retire_ids, None


async def run_dream(llm: LLMGateway | None = None) -> dict[str, Any]:
    """跑完一轮治理，返回统计 dict（judged / merged_created / retired / errors / ...）。

    新卡挂在源卡中 card_id 最大者的 task 下。pattern 且源卡 ≥3 时只记 promotion_suggestions，不写 skill 文件。
    """
    settings = get_settings()
    llm = llm or LLMGateway(settings)
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
        merged, retire_ids, err = await _judge_group(llm, card_type, task_type, group)
        report["judged"] += 1
        if err:
            report["errors"].append(f"[{card_type}/{task_type}] {err}")
            continue
        for m in merged:
            newest = card_by_id[max(m["source_ids"])]
            ids = archive.create_cards(
                newest["task_id"],
                [{"type": m["type"], "content": m["content"]}],
                str(newest.get("task_title", "")),
                task_type,
            )
            if not ids:
                retire_ids = [i for i in retire_ids if i not in m["source_ids"]]
                continue
            created_ids.extend(ids)
            report["merged_created"] += len(ids)
            for cid in ids:
                write_card_file(
                    {
                        "card_id": cid,
                        "task_id": newest["task_id"],
                        "card_type": m["type"],
                        "content": m["content"],
                        "task_title": newest.get("task_title", ""),
                        "task_type": task_type,
                    },
                    settings,
                )
            if m["type"] == "pattern" and len(m["source_ids"]) >= 3:
                report["promotion_suggestions"].append(
                    f"pattern 卡（合并自 {len(m['source_ids'])} 张，task_type={task_type}）"
                    f"可考虑晋升为 skills/{task_type}/references/"
                )
        all_retire.extend(retire_ids)

    if all_retire:
        unique = sorted(set(all_retire))
        report["retired"] = archive.retire_cards(unique)
        for cid in unique:
            delete_card_file(cid, settings)

    if created_ids:
        idx = await index_card_ids(created_ids, llm, settings)
        report["reindex"] = idx
    return report
