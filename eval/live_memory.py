"""真实 Embedding + Flash 相关性筛选评测。只在显式运行时调用 API，数据全在指定临时目录。

PYTHONPATH=. python -m eval.live_memory --output /tmp/labhandler-memory-check
"""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time

from config.runtime import get_settings
from memory.cards import write_card_file
from memory.retrieve import search_cards
from runtime.llm import LLMGateway

CARDS = [
    ('TTL覆盖', 'coding', 'put_with_ttl 覆盖已有键时必须改用本次调用的 ttl_ms。键过期后再次写入，get 立刻返回新值，不能套用旧的过期时间。'),
    ('WAL恢复', 'coding', 'MiniKV 的 WAL 在崩溃恢复时重放 put 和 delete。已经过期的键不要重启后复活；写入日志后再更新内存。'),
    ('引用规范', 'essay', '中文学术论文引用采用 GB/T 7714 顺序编码制。正文引用序号与文末参考文献一致，不得编造作者、年份和文献来源。'),
    ('实验报告', 'lab_report', '物理实验报告应记录原始测量数据、仪器精度、误差传播和不确定度，保留有效数字；不要虚构实验结果。'),
    ('并发队列', 'coding', '多生产者多消费者队列使用条件变量和互斥锁；等待条件放在 while 循环中，避免虚假唤醒导致空队列出队。'),
    ('数据库事务', 'coding', '银行转账需要在同一数据库事务中扣款和入账，任何一步失败都回滚，避免部分提交破坏资金守恒。'),
    ('图最短路', 'coding', 'Dijkstra 只适用于非负边权。有负边时使用 Bellman-Ford 并检测负环，不要把已弹出节点的距离直接固定。'),
    ('图像分类', 'coding', '训练图像分类器时先划分训练集和测试集，归一化参数只从训练集估计，防止数据泄漏；类别不平衡时报告宏平均 F1。'),
]
QUERIES = [
    ('ttl', '实现一个支持 TTL 的 KV 存储，重复写同一个键时如何更新到期时间？', [1]),
    ('wal', '键值数据库进程崩溃后，如何通过日志恢复已执行的写入与删除？', [2]),
    ('citation', '写中文学术论文，按 GB/T 7714 给出文内编号和文末参考文献。', [3]),
    ('physics', '整理单摆测量重力加速度的实验报告，需要计算误差与不确定度。', [4]),
    ('queue', '实现多线程阻塞队列，如何正确处理条件变量的虚假唤醒？', [5]),
    ('transfer', '账户 A 向 B 转账，扣款成功但入账失败时应该怎么处理？', [6]),
    ('graph', '带负权边的有向图求单源最短路径，还要判断负环。', [7]),
    ('image', '用不平衡的图片数据训练分类器，如何划分数据并选择评估指标？', [8]),
    ('unrelated_cooking', '请给出西红柿炒鸡蛋的做法，说明盐和糖的用量。', []),
    ('unrelated_poem', '写一首赞美秋天的七言绝句，不需要任何参考文献。', []),
    ('unrelated_sort', '使用冒泡排序对十个整数排序，输出升序结果。', []),
    ('unrelated_translation', '把这句英文翻译成中文：The cat is sleeping on the sofa.', []),
    ('negative_essay_kv', '写一篇关于唐诗意象的鉴赏文章。不是编程题，不涉及键值存储、TTL 或 WAL。', []),
    ('negative_no_citation', '写一篇小学春游记叙文。无需引用学术论文，也不要列参考文献。', []),
    ('generic_with_material', '完成附件实验。\n材料 TASK.md:\n实现具有生存时间的键值存储。覆盖写入时要更新该键的到期时间，过期后允许重新创建。', [1]),
    ('paraphrase_transfer', '两个账户余额此消彼长，中途断电后不能只减一边的钱。', [6]),
]


HOLDOUT = [
    ("holdout_atomic", "设计钱包支付系统，一笔扣款和给商家的入账必须同时成功或同时撤销。", [6]),
    ("holdout_ttl", "缓存中的旧项目超时失效后，再次设置同名项目应当能立即读取到新的内容和期限。", [1]),
    ("holdout_citation", "论文导师要求引用标注采用国标顺序编码，如何避免正文编号与文后条目错位？", [3]),
    ("holdout_queue", "消费者线程被唤醒时队列仍然为空，应该用循环重新检查条件还是直接取数据？", [5]),
    ("holdout_exclusion", "只实现内存 KV 的 get 和 put。明确不需要 TTL、日志、WAL、持久化或崩溃恢复。", []),
    ("holdout_poem", "请围绕银行转账写一首打油诗。不要设计程序或讨论数据库事务。", []),
    ("holdout_graph", "统计有向图中的顶点数和边数，不求最短路，也不需要判断负环。", []),
    ("holdout_generic", "帮我完成这次作业。", []),
]


class RecordedGateway:
    def __init__(self, gateway, cache_path):
        self.gateway = gateway
        self.cache_path = cache_path
        s = gateway.settings
        self.space = [s.embedding_base_url.rstrip("/"), s.embedding_model]
        data = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.vectors = data.get("vectors", {}) if data.get("space") == self.space else {}
        self.embedding_calls = 0
        self.chat_calls = 0

    async def embed(self, texts):
        missing = list(dict.fromkeys(t for t in texts if t not in self.vectors))
        if missing:
            self.embedding_calls += 1
            vectors = await self.gateway.embed(missing)
            self.vectors.update(zip(missing, vectors))
            self.cache_path.write_text(json.dumps({"space": self.space, "vectors": self.vectors}, ensure_ascii=False))
        return [self.vectors[t] for t in texts]

    async def chat(self, **kwargs):
        self.chat_calls += 1
        return await self.gateway.chat(**kwargs)


def metrics(rows):
    positive = [r for r in rows if r["gold"]]
    negative = [r for r in rows if not r["gold"]]
    return {
        "queries": len(rows), "errors": sum(bool(r["errors"]) for r in rows),
        "positive_recall_at_3": [sum(bool(set(r["gold"]) & set(r["selected"])) and not r["errors"] for r in positive), len(positive)],
        "negative_abstention": [sum(not r["selected"] and not r["errors"] for r in negative), len(negative)],
        "exact_selection": [sum(set(r["selected"]) == set(r["gold"]) and not r["errors"] for r in rows), len(rows)],
    }


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
    s = replace(get_settings(), memory_db_path=output / "memory.db", cards_dir=output / "cards",
                workspace_dir=output / "workspace")
    s.workspace_dir.mkdir(exist_ok=True)
    gateway = LLMGateway(s)
    llm = RecordedGateway(gateway, output / "embeddings.json")
    start = time.monotonic()
    rows = []
    limit = asyncio.Semaphore(2)
    try:
        for cid, (title, ttype, body) in enumerate(CARDS, 1):
            write_card_file(dict(card_id=cid, task_id=1, card_type="lesson", task_title=title,
                                 task_type=ttype, content=body), s)
        # 先对账，避免并发测量把冷启动时间重复计入每个查询。
        from memory.retrieve import reconcile_index
        indexing = await reconcile_index(llm, s)
        async def query_one(case, group):
            label, query, gold = case
            async with limit:
                begin = time.monotonic()
                result = await search_cards(query, 3, llm, s)
                row = dict(label=label, group=group, query=query, gold=gold,
                           selected=[int(p.stem) for p, _, _ in result.hits], errors=result.errors,
                           scores=[score for _, score, _ in result.hits], seconds=time.monotonic()-begin)
                rows.append(row)
                (output / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
                print(json.dumps(row, ensure_ascii=False), flush=True)
        from runtime.lab.ingest import ingest, retrieval_query
        material_cases = []
        for label, requirement, gold in [
            ("material_middle_ttl", "必须实现带 TTL 的内存键值存储；覆盖写入时更新过期时间，过期后允许重新创建。", [1]),
            ("material_middle_reverse", "必须实现字符串反转函数，输出逆序字符串。", []),
        ]:
            source = s.workspace_dir / "TASK.md"
            source.write_text("实验指导\n" + "课程日程与提交时间。\n" * 180 + requirement + "\n"
                              + "请阅读课程安排。\n" * 180, encoding="utf-8")
            session = s.workspace_dir / ".labhandler" / label
            await ingest(s, session)
            material_cases.append((label, retrieval_query("完成附件实验。", session), gold))
        await asyncio.gather(*(query_one(c, group) for group, cases in [
            ("initial", QUERIES), ("holdout", HOLDOUT), ("materials", material_cases)] for c in cases))
        report = dict(model=s.embedding_model, selector=s.flash_model, threshold=s.memory_min_score,
                      elapsed=time.monotonic()-start, embedding_calls=llm.embedding_calls,
                      chat_calls=llm.chat_calls, indexing=indexing, metrics=metrics(rows),
                      holdout_metrics=metrics([r for r in rows if r["group"] == "holdout"]),
                      material_metrics=metrics([r for r in rows if r["group"] == "materials"]), rows=rows)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"report": str(output / "report.json"), "metrics": report["metrics"]}, ensure_ascii=False))
        return report
    finally:
        await gateway.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.output))
    passed, total = result["metrics"]["exact_selection"]
    raise SystemExit(0 if passed == total else 1)
