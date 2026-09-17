# labHandler 未推送改动包（相对 origin/main）

本机不能 push。把这份文件拷到远端仓库根目录，按下面步骤应用，效果等于把当前工作区（去掉 Cursor 适配层）推到 `origin/main`。

- **基准提交**：`dbbcd8b`（`origin/main`）
- **远端若不是这个 commit**：先 `git checkout dbbcd8b`，或自行解决冲突后再 `git apply`
- **不含**：`runtime/cursor_chat.py`、`scripts/cursor_adapter.py`、`runtime/llm.py` 的 Cursor 分支、`requirements.txt` 里的 `cursor-sdk`、`CURSOR_CHANNEL` prompt、`CursorAgentError`
- **含**：记忆/NOTES/卡片槽、remember 按 index 对齐、eval 骨架（含后来修过的聚合、门禁、regate）。不含 `eval/runs/`。文末有本机重算后的数。

## 在远端怎么落地

仓库根目录、干净工作树：

```bash
git rev-parse --short HEAD    # 期望 dbbcd8b
python3 - <<'PY'
from pathlib import Path
text = Path("labHandler_unpushed.md").read_text(encoding="utf-8")
start = text.index("<!-- PATCH_START -->\n") + len("<!-- PATCH_START -->\n")
end = text.index("\n<!-- PATCH_END -->", start)
Path("labhandler_remote_apply.patch").write_text(text[start:end] + "\n", encoding="utf-8")
print("wrote labhandler_remote_apply.patch")
PY
git apply --check labhandler_remote_apply.patch
git apply labhandler_remote_apply.patch
rm labhandler_remote_apply.patch
python3 eval/selftest_report.py
```

`git apply --check` 失败就停。应用后 `runtime/llm.py` 应仍走 `AsyncOpenAI`，不要出现 `cursor_chat`。

## 改了什么

### 记忆 / 上下文 / remember / 其它 runtime

与上一版相同：`sqlite3` import、`reconcile_index`、`PREFETCH_MIN_SCORE=0.25`、检索去重、跨 task 同 hash 不入库、空卡不建 task；`MEMORY.md` → `NOTES.md` 加长笔记和独立 cards 槽，`memory_forget` 会 retired；remember/Judge 用 index 对齐；`AuthenticationError` → AUTH；Decision 带 reason。不含 Cursor 适配。

### eval（相对上一版 push 包新增或改过）

- `eval/run_case.py`：`result.json` 增加 `dispatch_waves` / `takeovers`（派发轮数才有冷热判别力，`milestones_len` 冷热常一样）。
- `eval/report.py`：外部门禁三态 pass/fail/**infra**（超时、沙箱不可达不算质量失败）；needle 按词组 AND + 归一化，不再死字符串；未知 `check` 抛错；卡片断言单独报 skipped，并对比冷跑算「有判别力」；token 优先 llm span 的 usage，没有则用 turn 上的本地估算；记忆主指标改成冷/热成本比，ΔM 只作辅。
- `eval/regate.py`：只重跑外部门禁，不重跑 lab。一次一个 run。会在沙箱里补装 pytest，并重试 MCP 未就绪。
- `eval/selftest_report.py`：聚合层自检。落地后先跑它。
- `eval/cases/two_sum_variant/gate/test_gate.py`：`test_negatives` 期望从错误的 `[2, 4]` 改为 `[1, 4]`（-4+5）。
- 三个 variant 的 `expect.yaml`：needle 改成词组；断言改成 `dispatch_waves <= N` / `gate_ran` / `no_flash_writes_tests`。

```bash
python eval/run_case.py --all --out eval/runs/<ts>
# 门禁写错或超时，不必重跑 lab：
python eval/regate.py --runs eval/runs/<ts> --only <run_id>
python eval/report.py --runs eval/runs/<ts> --out docs/eval_report.md --json docs/eval_results.json
python eval/selftest_report.py
```

## 本机已经跑出来的数（2026-09-17，regate + 重聚合后）

白盒差分。n=6 且每 case 一次，只能做改动前后对比，不当绝对分。

门禁诚实度（A + variant 热）：外部 6/6，假通过 0，弃权 0，infra 0。先前那 1 次假通过是门禁期望值写错，已改正并 regate。

记忆（variant 冷热）：ΔM 全 0（两侧都过）。输入 token 冷/热比均值 **3.96×**（essay 7.70×，two_sum 3.18×，minikv 1.00×）。卡片断言 5/5，其中有判别力 3/5（三条 `dispatch_waves`）；recall@3 6/6。

/remember：TP=6 FP=0 TN=12 FN=0，执行率 6/6。

效率（counted，token 来自 turn 本地估算）：均输入 372471，均 LLM 调用 27.2，接管率 3/6，停滞率 2/6。

## 补丁正文

`<!-- PATCH_START -->` 与 `<!-- PATCH_END -->` 之间是 unified diff，给 `git apply` 用。

<!-- PATCH_START -->
diff --git a/config/prompts.py b/config/prompts.py
--- a/config/prompts.py
+++ b/config/prompts.py
@@ -28,7 +28,7 @@
   → you write SUMMARY.md
 
 Who does what:
-- You: read materials, write SPEC, load_skill, write_acceptance, dispatch, judge, MEMORY, summary.
+- You: read materials, write SPEC, load_skill, write_acceptance, dispatch, judge, NOTES, summary.
   Product code only in takeover. SPEC / dispatch / judge cannot create `two_sum.py` — Flash
   creates it after you spec and dispatch. A FileNotFound on a path not in the catalog means
   that file does not exist yet. It is a deliverable, not a blocker. Do not keep re-reading
@@ -108,8 +108,17 @@
 the rule was listed). It also does NOT block SPEC: omit that rule and specify the homework
 in MATERIALS.md / the user request.
 Do not browse `.labhandler`. load_skill at most one SOP, by primary deliverable
-(coding vs essay vs lab_report). MEMORY.md survives compaction; FORGET.md is a one-shot compact
+(coding vs essay vs lab_report). NOTES.md survives compaction; FORGET.md is a one-shot compact
 hint, then discarded.
+Archived knowledge: at SPEC the harness prefetches the top-3 matching cards and injects their
+bodies in their own slot after the transcript ("## Archived knowledge"), separate from NOTES.md.
+They are prior-lab lessons; where they conflict with the materials, the materials win.
+memory_search / memory_grep return more pointers; memory_read opens the md.
+If a prefetched card is false, outdated, or contradicted by the materials, memory_forget it —
+gone from your next turn, and retired from the archive so later labs will not retrieve it.
+Do not memory_forget a still-true card just because this homework is about something else.
+notes_append a ≤80-char invariant, or notes_write {name, content} for a longer must-remember
+(NOTES.md keeps only the filename; notes_read it later).
 
 """
 
@@ -223,7 +232,7 @@
 - User-facing files use the user's language.
 """
 
-# Pro-Judge：对本步 briefs + harness gate 给出 continue/finish/revise_spec/takeover；并可改 MEMORY.md。
+# Pro-Judge：对本步 briefs + harness gate 给出 continue/finish/revise_spec/takeover；并可改 NOTES.md。
 JUDGE_SYSTEM = _JOB + _HARNESS + """## Role
 You are Pro-Judge. The harness already ran your gate after Flash submitted. Read the worker
 briefs plus Gate (pass/fail/test_invalid/no_hard_criteria). Call submit_judge.
@@ -257,30 +266,36 @@
   write_acceptance.
 - Transient failures in briefs are not specification failures.
 - A worker only did one small step. Judge that step, not the whole task.
-- Applicable /remember rules are in the user message. Fill rule_verdicts for each.
+- Applicable /remember rules are in the user message, numbered. Fill rule_verdicts for each,
+  identified by that index — do not retype the rule text as the identifier.
   finish only when every applicable rule is satisfied. Do not enforce inapplicable ones.
 
-## Managing MEMORY.md (you are the only one who can)
-It is injected every later turn and survives compaction. Keep it a short list of current
-invariants. Empty edits are the default. When a fact is superseded, change or delete the old
-line — do not only append.
+## Managing NOTES.md (you are the only one who can)
+It is injected every later turn and survives compaction. Keep it a short list. Empty edits are
+the default. When a fact is superseded, change or delete the old line — do not only append.
 
-- memory_append: at most one new line, ≤80 characters. Label plus the invariant.
-  Good: "TTL: lazy delete on get/scan/delete; ttl_s=None is permanent."
+- notes_append: at most one new line, ≤80 characters. A short invariant, or a pointer to a
+  longer note file (`ttl.md`). Good: "TTL: lazy delete on get/scan/delete; ttl_s=None is permanent."
   Bad: algorithms, field lists, fsync order, or anything already in SPEC.md.
-- memory_replace: [{old, new}] rewrite matching bullets (old may be a unique substring). new=""
+- notes_write: {name, content} for a longer must-remember. Writes notes/{name}.md and appends
+  the filename as a pointer in NOTES.md. Later notes_read that filename.
+- notes_replace: [{old, new}] rewrite matching bullets (old may be a unique substring). new=""
   deletes.
-- memory_remove: [substring, ...] drop matching bullets that are stale or wrong.
-- forget_append: describe context that turned out to be noise — abandoned approaches, dead-end
-  probes, superseded guesses. Describe the topic to drop, not the conclusion.
-  This is a one-shot instruction to the next compaction: once history is compacted the described
-  content is gone and the note is discarded with it. Do not re-add the same line later.
+- notes_remove: [substring, ...] drop matching bullets that are stale or wrong.
+- forget_append: noise in the transcript — abandoned approaches or dead-end probes. Describe the
+  topic, not the conclusion. Next compact: Flash omits it from the digest, then the hint is gone.
+
+## Dropping a prefetched card
+memory_forget {card}: a filename ("4.md") or a unique substring of the card body. The card
+leaves your context on the very next turn, is kept out of the next digest, and is retired
+from the archive (later labs will not retrieve it). Use it when the card is false, outdated,
+or contradicted by this homework. Do not retire a still-true card just because it is off-topic.
 """
 
-# remember_judge：只裁定 /remember 是否适用于本份作业，不写 SPEC/MEMORY。
+# remember_judge：只裁定 /remember 是否适用于本份作业，不写 SPEC/NOTES。
 REMEMBER_JUDGE_SYSTEM = """## Role
 You are Remember-Judge. You only decide applies=true/false for listed /remember rules.
-Do not write SPEC.md, MEMORY.md, code, 实验报告, screenshot placeholders, or any homework
+Do not write SPEC.md, NOTES.md, code, 实验报告, screenshot placeholders, or any homework
 artifact into this transcript. Do not follow the rules — judge whether THIS homework will
 produce the artifact a rule names. Do not pick a skill.
 
diff --git a/runtime/errors.py b/runtime/errors.py
--- a/runtime/errors.py
+++ b/runtime/errors.py
@@ -51,6 +51,8 @@
     if exc is not None:
         name = type(exc).__name__
         qual = f"{type(exc).__module__}.{name}"
+        if name in {"AuthenticationError"}:
+            return ErrorClass.AUTH
         if name in _TRANSIENT_TYPES or qual.endswith("TimeoutError"):
             return ErrorClass.TRANSIENT
         if name in {"PermissionError"}:
diff --git a/.gitignore b/.gitignore
index 73141b1..3582b78 100644
--- a/.gitignore
+++ b/.gitignore
@@ -25,6 +25,7 @@ tests/
 docs/
 tmp/
 benchmark/
+eval/runs/
 
 # IDE
 .vscode/
diff --git a/README.md b/README.md
index e024711..9e2dac7 100644
--- a/README.md
+++ b/README.md
@@ -22,8 +22,10 @@
   阶段指令追加在对话末尾；Flash 每份任务书新开上下文
 - **上下文管理**：最近数轮保留原文，更早的由 Flash 摘要一次，tool 正文卸到磁盘；
   压缩后立即重新装配再发请求
-- **双 notes**：`MEMORY.md` 常驻上下文、压缩吃不掉，Judge 可追加/改写/删除条目；
-  `FORGET.md` 让摘要刻意跳过噪声，压缩完成即清空
+- **NOTES 与卡片**：`NOTES.md` 常驻 notes 槽、压缩吃不掉，Judge 可追加/改写/删除；长笔记
+  `notes_write` 落盘，NOTES.md 只留指针，`notes_read` 再打开。SPEC 预取的归档卡片进独立
+  cards 槽（排在历史之后，本次作业的要求先入场）；Pro 调 `memory_forget` 下一拍即消失，
+  并记进 `FORGET.md` 让下次摘要也不回流。`forget_append` 只标历史噪声，压完即清空。
 - **幂等续跑**：副作用账本记录产物指纹，产物完好的任务直接跳过
 - **两层安全边界**：host 白名单 + 路径守护 + 审计；重量操作走 MCP Docker 沙箱
 - **可观测**：Langfuse + 本地 JSONL，span 树覆盖 run / spec / dispatch / step / task / turn /
diff --git a/memory/archive.py b/memory/archive.py
index a889b34..a91a482 100644
--- a/memory/archive.py
+++ b/memory/archive.py
@@ -70,24 +70,61 @@ class TaskArchive:
             conn.commit()
             return cursor.lastrowid or 0
 
+    def _parse_card(self, card: dict) -> tuple[str, str, str] | None:
+        """合法卡片 → (card_type, content, content_hash)；否则 None。"""
+        card_type = str(card.get("type", "")).strip()
+        content = str(card.get("content", "")).strip()
+        if card_type not in VALID_CARD_TYPES or not content:
+            return None
+        return card_type, content, hashlib.sha256(content.encode()).hexdigest()[:16]
+
+    def _active_duplicate(self, conn: sqlite3.Connection, card_type: str, content_hash: str) -> bool:
+        row = conn.execute(
+            """
+            SELECT 1 FROM archive_cards
+            WHERE card_type = ? AND content_hash = ? AND retired_at IS NULL
+            LIMIT 1
+            """,
+            (card_type, content_hash),
+        ).fetchone()
+        return row is not None
+
+    def has_new_cards(self, knowledge_cards: list[dict]) -> bool:
+        """是否存在尚未入库的活跃卡（跨 task 同 hash 视为已有）。"""
+        seen: set[tuple[str, str]] = set()
+        with connect(self.db_path) as conn:
+            for card in knowledge_cards:
+                parsed = self._parse_card(card)
+                if parsed is None:
+                    continue
+                card_type, _content, content_hash = parsed
+                key = (card_type, content_hash)
+                if key in seen:
+                    continue
+                seen.add(key)
+                if not self._active_duplicate(conn, card_type, content_hash):
+                    return True
+        return False
+
     def create_cards(
         self, task_id: int, knowledge_cards: list[dict], task_title: str, task_type: str
     ) -> list[int]:
         """写入 archive_cards，返回实际插入的 card_id。
 
         不入库：card_type 不在白名单、content 为空、
-        同 task 内 (card_type, content_hash) 已存在（UNIQUE，吞 IntegrityError）。
+        任意活跃卡已有同一 (card_type, content_hash)（跨 task 也不再收）、
+        同 task 内 UNIQUE 冲突（吞 IntegrityError）。
         search_text 由 task_type / card_type / task_title / content 拼成。
         """
         inserted_ids: list[int] = []
         with connect(self.db_path) as conn:
             for card in knowledge_cards:
-                card_type = str(card.get("type", "")).strip()
-                content = str(card.get("content", "")).strip()
-                if card_type not in VALID_CARD_TYPES or not content:
+                parsed = self._parse_card(card)
+                if parsed is None:
+                    continue
+                card_type, content, content_hash = parsed
+                if self._active_duplicate(conn, card_type, content_hash):
                     continue
-
-                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                 search_text = (
                     f"任务类型: {task_type}\n"
                     f"卡片类型: {card_type}\n"
diff --git a/memory/retrieve.py b/memory/retrieve.py
index e8d679e..a1ebb7a 100644
--- a/memory/retrieve.py
+++ b/memory/retrieve.py
@@ -1,10 +1,17 @@
 """跨 lab 记忆检索。卡片 markdown 是事实源，向量表是派生索引。
 
+预取：SPEC 时按作业文本取 top-3 卡片正文交给 runtime.context.notes 落盘。本模块只产出
+单张卡片的文本，卡片怎么拼、怎么存、怎么被 Pro 丢掉都归 notes 模块。
+余弦低于 PREFETCH_MIN_SCORE 视为无关，不注入（弃权）。
+冷检索：memory_search 返回指针，memory_read 打开 md。
+每次 lab 开始时 reconcile_index 对齐文件与向量表。
+
 读路径只允许 cards_dir 与 workspace_dir。语义检索 k 夹在 1..8；grep 命中上限默认 40；
 单文件正文最多返回 80000 字。
 """
 
 from pathlib import Path
+import asyncio
 import re
 from typing import Any
 
@@ -13,6 +20,12 @@ from memory.archive import get_task_archive
 from memory.vectors import VectorIndex, content_sha256
 from tools.policy import get_policy
 
+PREFETCH_K = 3
+PREFETCH_MIN_SCORE = 0.25
+_SEARCH_K_CAP = 8
+_CARD_NAME = re.compile(r"^(\d+)(?:\.md)?$", re.I)
+_RECONCILE_LOCK = asyncio.Lock()
+
 
 def _cards_dir(settings: RuntimeSettings | None = None) -> Path:
     """返回 settings.cards_dir，目录保证存在。"""
@@ -54,6 +67,22 @@ def read_card_file(path: Path) -> str:
     return path.read_text(encoding="utf-8")
 
 
+def parse_card_meta(text: str) -> dict[str, str]:
+    """读首段 YAML frontmatter 为扁平 dict。无合法分隔则空 dict。"""
+    if not text.startswith("---"):
+        return {}
+    end = text.find("\n---\n", 3)
+    if end < 0:
+        return {}
+    meta: dict[str, str] = {}
+    for line in text[4:end].splitlines():
+        if ":" not in line:
+            continue
+        key, value = line.split(":", 1)
+        meta[key.strip()] = value.strip()
+    return meta
+
+
 def parse_card_body(text: str) -> str:
     """去掉首段 --- frontmatter，返回正文。无合法分隔则整份原文。"""
     if not text.startswith("---"):
@@ -98,52 +127,151 @@ def delete_card_file(card_id: int, settings: RuntimeSettings | None = None) -> N
     path.unlink(missing_ok=True)
 
 
+def resolve_card_ids(needle: str, settings: RuntimeSettings | None = None) -> list[int]:
+    """把 memory_forget 的针（文件名或正文子串）解析成 card_id。"""
+    needle = needle.strip()
+    if not needle:
+        return []
+    name = Path(needle).name
+    matched = _CARD_NAME.match(name)
+    if matched:
+        return [int(matched.group(1))]
+    embedded = re.search(r"(?:^|\b)(\d+)\.md\b", needle)
+    if embedded:
+        return [int(embedded.group(1))]
+    s = settings or get_settings()
+    hits: list[int] = []
+    for p in _cards_dir(s).glob("*.md"):
+        if not p.stem.isdigit():
+            continue
+        try:
+            raw = read_card_file(p)
+        except Exception:
+            continue
+        if needle in p.name or needle in raw:
+            hits.append(int(p.stem))
+    return hits
+
+
+def retire_cards_matching(needle: str, settings: RuntimeSettings | None = None) -> int:
+    """按针淘汰归档卡：写 retired_at，删 markdown 与向量行。返回实际标记数。"""
+    ids = resolve_card_ids(needle, settings)
+    if not ids:
+        return 0
+    n = get_task_archive().retire_cards(ids)
+    for cid in ids:
+        delete_card_file(cid, settings)
+    return n
+
+
 async def reconcile_index(llm, settings: RuntimeSettings | None = None) -> dict[str, int]:
     """以 cards_dir 下 *.md 为事实源对齐向量表。
 
     缺行、content_sha256 不一致、或 embedding_model 与当前设置不同 → 重建该行。
-    没有对应文件的索引行删除。
+    没有对应文件的索引行删除。同进程并发调用串行化，避免重复嵌入。
     """
     s = settings or get_settings()
-    idx = VectorIndex(settings=s)
-    files = {str(p): p for p in _cards_dir(s).glob("*.md")}
-    rows = {r["path"]: r for r in idx.all_rows()}
-    rebuilt = 0
-    dropped = 0
-    for path_str, p in files.items():
-        body = parse_card_body(read_card_file(p))
-        sha = content_sha256(body)
-        row = rows.get(path_str)
-        if (
-            row is None
-            or row.get("content_sha256") != sha
-            or row.get("embedding_model") != s.embedding_model
-        ):
-            await embed_card_file(p, llm, s)
-            rebuilt += 1
-    for path_str in rows:
-        if path_str not in files:
-            idx.delete(path_str)
-            dropped += 1
-    return {"rebuilt": rebuilt, "dropped": dropped}
+    async with _RECONCILE_LOCK:
+        idx = VectorIndex(settings=s)
+        files = {str(p): p for p in _cards_dir(s).glob("*.md")}
+        rows = {r["path"]: r for r in idx.all_rows()}
+        rebuilt = 0
+        dropped = 0
+        for path_str, p in files.items():
+            body = parse_card_body(read_card_file(p))
+            sha = content_sha256(body)
+            row = rows.get(path_str)
+            if (
+                row is None
+                or row.get("content_sha256") != sha
+                or row.get("embedding_model") != s.embedding_model
+            ):
+                await embed_card_file(p, llm, s)
+                rebuilt += 1
+        for path_str in rows:
+            if path_str not in files:
+                idx.delete(path_str)
+                dropped += 1
+        return {"rebuilt": rebuilt, "dropped": dropped}
+
+
+async def search_cards(
+    query: str,
+    k: int,
+    llm,
+    settings: RuntimeSettings | None = None,
+) -> list[tuple[Path, float, str]]:
+    """嵌入 query，返回至多 k 条 (path, score, body)。空表或无命中为 []。"""
+    s = settings or get_settings()
+    vecs = await llm.embed([query])
+    hits = VectorIndex(settings=s).search(vecs[0], k=max(1, k))
+    out: list[tuple[Path, float, str]] = []
+    for path, score in hits:
+        p = Path(path)
+        if not p.is_file():
+            continue
+        out.append((p, score, parse_card_body(read_card_file(p))))
+    return out
+
+
+def render_pointers(hits: list[tuple[Path, float, str]], *, scores: bool = False) -> str:
+    """检索工具用的卡片指针：文件名 + card_type，不含正文。scores 时附带余弦分。"""
+    lines = ["## Archived cards"]
+    for path, score, _body in hits:
+        raw = read_card_file(path) if path.is_file() else ""
+        ctype = parse_card_meta(raw).get("card_type", "")
+        extra = f" score={score:.3f}" if scores else ""
+        lines.append(f"- {path.name} {ctype}{extra}".rstrip())
+    return "\n".join(lines)
+
+
+def _unique(hits: list[tuple[Path, float, str]], limit: int) -> list[tuple[Path, float, str]]:
+    """按正文去重，保留分数更高的先到者。"""
+    seen: set[str] = set()
+    picked: list[tuple[Path, float, str]] = []
+    for item in hits:
+        key = content_sha256(item[2]) if item[2] else str(item[0])
+        if key in seen:
+            continue
+        seen.add(key)
+        picked.append(item)
+        if len(picked) >= limit:
+            break
+    return picked
 
 
 async def memory_search(query: str, k: int, llm, settings: RuntimeSettings | None = None) -> str:
-    """语义检索：嵌入 query，返回至多 min(k, 8) 条（至少 1）卡片正文前 400 字。
+    """语义检索：嵌入 query，返回至多 min(k, 8) 条去重指针。正文用 memory_read。
 
-    无命中 "(no matches)"。文件已删的命中 snippet 为空。
+    无命中 "(no matches)"。
     """
-    s = settings or get_settings()
-    vecs = await llm.embed([query])
-    hits = VectorIndex(settings=s).search(vecs[0], k=max(1, min(k, 8)))
+    hits = await search_cards(query, _SEARCH_K_CAP, llm, settings)
+    hits = _unique(hits, max(1, min(k, _SEARCH_K_CAP)))
     if not hits:
         return "(no matches)"
-    lines: list[str] = []
-    for path, score in hits:
-        p = Path(path)
-        snippet = parse_card_body(read_card_file(p))[:400] if p.is_file() else ""
-        lines.append(f"{p.name} score={score:.3f}\n{snippet}")
-    return "\n---\n".join(lines)
+    return render_pointers(hits, scores=True)
+
+
+async def prefetch_cards(
+    query: str,
+    llm,
+    settings: RuntimeSettings | None = None,
+    k: int = PREFETCH_K,
+) -> list[str]:
+    """SPEC 预取：按作业文本取 top-k 去重卡片，每张一段「文件名 + 完整正文」。
+
+    分数低于 PREFETCH_MIN_SCORE 的卡片丢掉（全部低于则返回 []）。嵌入失败也不打断 lab。
+    """
+    if not query.strip():
+        return []
+    try:
+        hits = await search_cards(query, _SEARCH_K_CAP, llm, settings)
+    except Exception:
+        return []
+    hits = [(p, score, body) for p, score, body in hits if score >= PREFETCH_MIN_SCORE]
+    if not hits:
+        return []
+    return [f"{p.name} score={score:.3f}\n{body}" for p, score, body in _unique(hits, k)]
 
 
 def memory_grep(
@@ -189,16 +317,22 @@ def memory_grep(
     return "\n".join(hits) if hits else "(no matches)"
 
 
-def memory_read(path: str, settings: RuntimeSettings | None = None) -> str:
+def memory_read(
+    path: str,
+    settings: RuntimeSettings | None = None,
+    *,
+    extra_roots: list[Path] | None = None,
+) -> str:
     """读允许范围内的文件，正文最多 80000 字。
 
-    相对路径依次试 cards_dir、workspace_dir；绝对路径必须落在这两者之下。
+    相对路径依次试 extra_roots、cards_dir、workspace_dir；绝对路径必须落在这些根之下。
     越权 [ERROR/PermissionError]，缺失 [ERROR/FileNotFoundError]。
     """
     s = settings or get_settings()
     candidate = Path(path)
+    bases = [*(extra_roots or []), _cards_dir(s), s.workspace_dir]
     if not candidate.is_absolute():
-        for base in (_cards_dir(s), s.workspace_dir):
+        for base in bases:
             p = (base / path).resolve()
             try:
                 p.relative_to(base.resolve())
@@ -210,7 +344,7 @@ def memory_read(path: str, settings: RuntimeSettings | None = None) -> str:
                 return p.read_text(encoding="utf-8", errors="replace")[:80_000]
         return f"[ERROR/FileNotFoundError] {path}"
     resolved = candidate.resolve()
-    allowed = [_cards_dir(s).resolve(), s.workspace_dir.resolve()]
+    allowed = [b.resolve() for b in bases]
     if not any(_is_under(resolved, a) for a in allowed):
         return f"[ERROR/PermissionError] path not allowed: {path}"
     if not resolved.is_file():
diff --git a/memory/vectors.py b/memory/vectors.py
index 7456aa0..6128d7c 100644
--- a/memory/vectors.py
+++ b/memory/vectors.py
@@ -7,6 +7,7 @@ content_sha256 + embedding_model 供对账：与当前正文或当前模型不
 
 import hashlib
 import os
+import sqlite3
 import struct
 from pathlib import Path
 
diff --git a/runtime/context/assemble.py b/runtime/context/assemble.py
index 3b4b17e..e994c08 100644
--- a/runtime/context/assemble.py
+++ b/runtime/context/assemble.py
@@ -1,7 +1,16 @@
 """上下文槽位装配。纯函数：同样的输入永远得到同样的 messages。
 
-槽位顺序按「越稳定越靠前」排，利于上游 prompt cache：
-system → memory → user → spec → history → retrieved → events
+槽位顺序按「一拍之内还会不会变」排，不变的靠前，利于上游 prompt cache：
+system → notes → user → spec → history → cards → retrieved → events
+
+notes：本 lab NOTES.md（短句或长文指针）。只在阶段交卷时被改写，而那一刻 system
+       本来就换了新的阶段 prompt，缓存反正要断，所以放前面不额外付费。
+cards：SPEC 预取的跨 lab 卡片正文，仅 Pro。Pro 随时可以 memory_forget 掉一张，
+       一拍之内就会变，所以必须排在 history 之后，否则每忘一张就作废整条历史的缓存。
+       forget 同时把卡从归档里淘汰，下一 lab 检索不到。
+       排在 SPEC 与历史之后还有第二个好处：本次作业的要求先入场，旧 lab 的经验后到，
+       两者冲突时模型更容易按前者走。
+retrieved：本 loop 里 memory_search/grep/read、notes_read、load_skill 的追加结果，不进 history。
 
 user 槽只承载「本轮之前没有任何对话」的那一段开场指令；为空时整条消息不出现。
 跨阶段连续对话的指令由调用方直接写进 history，这样它才排在既有往来之后，
@@ -44,7 +53,8 @@ def assemble(
     project_spec: str,
     history: list[dict[str, Any]],
     retrieved: str,
-    memory: str,
+    notes: str,
+    cards: str = "",
     events: list[dict[str, str]],
     working: str,
     tool_schemas: list[dict[str, Any]] | None = None,
@@ -52,8 +62,8 @@ def assemble(
     messages: list[dict[str, Any]] = [
         {"role": "system", "content": system},
     ]
-    if memory:
-        messages.append({"role": "user", "content": memory})
+    if notes:
+        messages.append({"role": "user", "content": notes})
     if user_input:
         messages.append({"role": "user", "content": user_input})
     if project_spec:
@@ -61,6 +71,8 @@ def assemble(
 
     messages.extend(strip_private(m) for m in history)
 
+    if cards:
+        messages.append({"role": "user", "content": cards})
     if retrieved:
         messages.append({"role": "user", "content": f"## Retrieved knowledge\n{retrieved}"})
 
diff --git a/runtime/context/compact.py b/runtime/context/compact.py
index dfc060d..cf471bb 100644
--- a/runtime/context/compact.py
+++ b/runtime/context/compact.py
@@ -1,11 +1,12 @@
-"""上下文压缩：最近若干轮保留原文，更早的由 Flash 收成一段纪要。
+"""上下文压缩：最近若干轮保留原文，更早的由 Flash 收成一份分节纪要。
 
 切分单位是回合：带 tool_calls 的 assistant 与其全部 tool 响应同属一个不可再分
 的单位。拆开会产出协议非法的孤儿 tool 消息。
 
 卸盘按 DumpScope（agent × task_id）分目录，Pro 与各 Flash 互不混放。
-FORGET.md 只在 Pro 压缩时作为排除指令并清空；Flash 压缩不碰它。
-MEMORY.md 不在 history 里、不经摘要——assemble 每轮从文件重新注入。
+notes 槽与 cards 槽不在 history 里、不经摘要——assemble 每轮从文件重新注入。
+FORGET.md 只在 Pro 压缩时作为排除指令并清空；Flash 压缩不碰它。被遗忘的卡片此前已经
+从 cards 槽消失，这里只负责让它不要从旧对话经摘要回流。
 是否该压、阈值、usage 校准归 TokenBudget；本模块只切回合、落盘 tool 正文、写纪要。
 """
 
@@ -25,19 +26,42 @@ DUMP_DIR = "tool_results"
 OFFLOAD_MARKER = "[offloaded "
 PREVIEW_CHARS = 2000
 LIVE_OFFLOAD_CHARS = 12_000
-_SUMMARY_MAX_TOKENS = 900
+# 分小节的纪要比一段话长；给足额度，避免刚好在小节中间被 finish_reason=length 截断。
+_SUMMARY_MAX_TOKENS = 1400
 _BLOB_CHAR_CAP = 24000
 _INLINE_TOOL_CAP = 600
 
-_SUMMARY_SYSTEM = """你在压缩一个 agent 的历史对话，供它后续回合继续使用。
+_SUMMARY_SYSTEM = """你在压缩一个 agent 的历史对话。你的纪要会顶替这段原文，成为它后续回合
+唯一看得见的版本：原文不再回来，你漏掉的事实等于被删掉。
 
-保留：任务约束、已确认的结论、文件名与路径、接口签名、测试结果、
-失败原因、仍未解决的问题。
-丢弃：寒暄、重复的试探、已被推翻的中间猜想。
-不要编造任何未在原文出现的事实。工具的完整输出已另存到磁盘，
-需要时可以 grep {dump_hint}。
+输入是拍平的对话。[user] 是 harness 或用户下达的指令，[assistant] 是它的回答，
+[调用] 是它发出的工具调用与参数，[xxx 结果] 是工具返回（过长的只留了头部，
+全文已落盘到 {dump_hint}）。越靠后的回合越重要，写得越细。
 
-输出一段紧凑的中文纪要，不要分点堆砌套话。"""
+先求全、再求简：宁可多留一条事实，也不要为了句子通顺把事实揉掉。
+标识符一律照抄——文件路径、函数与接口签名、字段名、命令、报错原文、数字指标，
+一个字符都不要改写或翻译。不要编造原文没有的东西，没被验证过的结论写明「未验证」。
+SPEC.md 与 NOTES.md 由 harness 另行注入，不要复述它们。
+
+按下列小节输出，没有内容的小节整节省略，不要写「无」：
+
+### 硬约束
+指令里明确要求或禁止的事情：固定的名字、格式、交付物、不许碰的东西。原话照抄。
+
+### 已完成
+已落地的产物：路径 + 里面是什么 + 验收或测试的结论。
+
+### 失败与修复
+踩过的错、报错原文、怎么修好的；以及被否决的做法和否决理由。
+这一节防的是重复踩坑，不要省。
+
+### 未决
+没做完、没验证、或仍然存疑的问题。
+
+### 当前进度
+紧挨着这次压缩之前正在做的事。
+
+整份不超过 800 字。只写事实，不要转述它的心理活动，不要客套和总结陈词。"""
 
 
 @dataclass
@@ -250,7 +274,7 @@ async def _summarize(
     dump_hint: str,
     apply_forget: bool,
 ) -> tuple[str, str | None]:
-    """用 Flash 把旧回合收成一段纪要。成功返回 (正文, None)；异常或空响应返回 ("", 错误串)。"""
+    """用 Flash 把旧回合收成一份分节纪要。成功返回 (正文, None)；异常或空响应返回 ("", 错误串)。"""
     system = _SUMMARY_SYSTEM.format(dump_hint=dump_hint)
     if apply_forget:
         directive = forget_directive(notes)
@@ -258,11 +282,12 @@ async def _summarize(
             system = system + "\n\n" + directive
 
     try:
+        blob = _render_for_summary(turns)
         result = await llm.chat(
             model=settings.flash_model,
             messages=[
                 {"role": "system", "content": system},
-                {"role": "user", "content": _render_for_summary(turns)},
+                {"role": "user", "content": blob},
             ],
             max_tokens=_SUMMARY_MAX_TOKENS,
         )
diff --git a/runtime/context/notes.py b/runtime/context/notes.py
index 0765821..08f8c10 100644
--- a/runtime/context/notes.py
+++ b/runtime/context/notes.py
@@ -1,51 +1,72 @@
-"""会话内的两份主动记忆，都落在 session 目录。
+"""会话内 Pro 能主动写的三份文件，都落在 session 目录，每轮由 assemble 重新注入。
 
-MEMORY.md —— Pro 判定「必须长久记住」的不变量。每轮由 assemble 从文件重新注入，
-             不经 compact 摘要。Judge 可追加、改写或删除；整份超 _MAX_CHARS 时
-             淘汰最早的条目。
-FORGET.md —— Pro 判定「无关杂乱」的描述。compact 时作为排除指令交给
-             summarizer；压缩完成后立即清空——被描述的内容已不在 history 里，
-             再留着只会成为新噪声。
+NOTES.md  —— Pro 运行时写的不变量，进 notes 槽。短句 ≤80 字，或指向 notes/*.md 的指针。
+CARDS.md  —— SPEC 预取的跨 lab 知识卡片正文，进 cards 槽（仅 Pro），与 NOTES 无关。
+             本模块是这份文件格式的唯一出处：memory.retrieve 只给单张卡片的文本，
+             卡片之间用 --- 分隔由这里拼、这里拆。文件存在（哪怕是空的）即表示已预取。
+FORGET.md —— 无关杂乱的描述，只作用于下一次 compact：交给 summarizer 排除，压完即清空。
 
-条目按整行匹配，同一条不会写两遍。空则删文件。原子落盘。
+遗忘一张卡片是一次动作两个后果（forget_card）：立刻从 CARDS.md 去掉，所以下一拍的
+cards 槽里就没有它；同时记进 FORGET.md，所以下一次摘要也不会把它写回来。
+跨 lab 淘汰（retired_at + 删文件）由 memory.retrieve.retire_cards_matching 负责，
+本模块不碰归档库。
+
+条目按整行匹配，同一条不会写两遍。原子落盘。
 """
 
-from dataclasses import dataclass
+from dataclasses import dataclass, field
 from pathlib import Path
 from typing import Any
 
 from runtime.lab.persist import read_text, write_text
 
-MEMORY_FILE = "MEMORY.md"
+NOTES_FILE = "NOTES.md"
+CARDS_FILE = "CARDS.md"
 FORGET_FILE = "FORGET.md"
+LONG_NOTES_DIR = "notes"
 
-_MEMORY_HEADER = "# 必须记住的不变量\n"
+_NOTES_HEADER = "# Notes\n"
 _FORGET_HEADER = "# 压缩时忽略的杂乱上下文\n"
+_CARD_SEP = "\n---\n"
 
-# 单条上限：MEMORY.md 每轮整份进上下文。超出截成一行。
-MEMORY_ENTRY_MAX = 80
+# 单条上限：NOTES.md 每轮整份进上下文。超出截成一行。
+NOTES_ENTRY_MAX = 80
 # 单份文件硬上限；超出时丢掉最早的条目。
 _MAX_CHARS = 2000
 
 
 @dataclass(frozen=True)
 class SessionNotes:
-    memory: str
+    """一拍要注入的全部会话笔记。block 属性就是 assemble 对应槽位的正文。"""
+
+    notes: str
     forget: str
+    cards: list[str] = field(default_factory=list)
 
     @property
-    def has_memory(self) -> bool:
-        return bool(self.memory.strip())
+    def has_notes(self) -> bool:
+        return bool(self.notes.strip())
 
     @property
     def has_forget(self) -> bool:
         return bool(self.forget.strip())
 
+    @property
+    def notes_block(self) -> str:
+        return "## Notes\n" + self.notes if self.has_notes else ""
+
+    @property
+    def cards_block(self) -> str:
+        if not self.cards:
+            return ""
+        return "## Archived knowledge\n" + _CARD_SEP.join(self.cards)
+
 
 def load_notes(sdir: Path) -> SessionNotes:
     return SessionNotes(
-        memory=_body(read_text(sdir, MEMORY_FILE), _MEMORY_HEADER),
+        notes=_body(read_text(sdir, NOTES_FILE), _NOTES_HEADER),
         forget=_body(read_text(sdir, FORGET_FILE), _FORGET_HEADER),
+        cards=load_cards(sdir) or [],
     )
 
 
@@ -107,9 +128,9 @@ def _append(sdir: Path, filename: str, header: str, text: str, *, limit: int | N
     return True
 
 
-def append_memory(sdir: Path, text: str) -> bool:
-    """追加一条必须长期保留的不变量。超出 MEMORY_ENTRY_MAX 的尾部丢掉；重复或空串不写。"""
-    return _append(sdir, MEMORY_FILE, _MEMORY_HEADER, text, limit=MEMORY_ENTRY_MAX)
+def append_notes(sdir: Path, text: str) -> bool:
+    """追加一条必须长期保留的不变量。超出 NOTES_ENTRY_MAX 的尾部丢掉；重复或空串不写。"""
+    return _append(sdir, NOTES_FILE, _NOTES_HEADER, text, limit=NOTES_ENTRY_MAX)
 
 
 def append_forget(sdir: Path, text: str) -> bool:
@@ -117,26 +138,26 @@ def append_forget(sdir: Path, text: str) -> bool:
     return _append(sdir, FORGET_FILE, _FORGET_HEADER, text)
 
 
-def remove_memory(sdir: Path, needles: list[str]) -> int:
+def remove_notes(sdir: Path, needles: list[str]) -> int:
     """删掉正文等于或包含 needle 的条目。返回删除条数；无有效 needle 时为 0。"""
     keys = [n for n in (_one_line(n, 200) for n in needles) if n]
     if not keys:
         return 0
-    lines = _read_lines(sdir, MEMORY_FILE, _MEMORY_HEADER)
+    lines = _read_lines(sdir, NOTES_FILE, _NOTES_HEADER)
     kept = [ln for ln in lines if not any(_matches(ln, k) for k in keys)]
     dropped = len(lines) - len(kept)
     if dropped:
-        _write_lines(sdir, MEMORY_FILE, _MEMORY_HEADER, kept)
+        _write_lines(sdir, NOTES_FILE, _NOTES_HEADER, kept)
     return dropped
 
 
-def replace_memory(sdir: Path, old: str, new: str) -> int:
-    """把匹配 old 的条目改成 new（同样 ≤ MEMORY_ENTRY_MAX）。new 为空则删除该条。返回改动条数。"""
+def replace_notes(sdir: Path, old: str, new: str) -> int:
+    """把匹配 old 的条目改成 new（同样 ≤ NOTES_ENTRY_MAX）。new 为空则删除该条。返回改动条数。"""
     key = _one_line(old, 200)
-    replacement = _one_line(new, MEMORY_ENTRY_MAX)
+    replacement = _one_line(new, NOTES_ENTRY_MAX)
     if not key:
         return 0
-    lines = _read_lines(sdir, MEMORY_FILE, _MEMORY_HEADER)
+    lines = _read_lines(sdir, NOTES_FILE, _NOTES_HEADER)
     out: list[str] = []
     n = 0
     for ln in lines:
@@ -149,25 +170,80 @@ def replace_memory(sdir: Path, old: str, new: str) -> int:
             if bullet not in out:
                 out.append(bullet)
     if n:
-        _write_lines(sdir, MEMORY_FILE, _MEMORY_HEADER, out)
+        _write_lines(sdir, NOTES_FILE, _NOTES_HEADER, out)
     return n
 
 
-def apply_memory(
+def apply_notes(
     sdir: Path,
     *,
     replace: Any = None,
     remove: Any = None,
     append: str = "",
 ) -> None:
-    """先改、再删、最后追加。Judge 一次裁决里对 MEMORY.md 的全部变更，顺序固定。"""
+    """先改、再删、最后追加。Judge 一次裁决里对 NOTES.md 的全部变更，顺序固定。"""
     for item in replace or []:
         if not isinstance(item, dict):
             continue
-        replace_memory(sdir, str(item.get("old") or ""), str(item.get("new") or ""))
+        replace_notes(sdir, str(item.get("old") or ""), str(item.get("new") or ""))
     needles = remove if isinstance(remove, list) else ([remove] if remove else [])
-    remove_memory(sdir, [str(x) for x in needles])
-    append_memory(sdir, append)
+    remove_notes(sdir, [str(x) for x in needles])
+    append_notes(sdir, append)
+
+
+def load_cards(sdir: Path) -> list[str] | None:
+    """读 CARDS.md。返回卡片列表；None 表示本 lab 还没预取过（续跑据此不重复检索）。"""
+    if not (sdir / CARDS_FILE).is_file():
+        return None
+    return [c.strip() for c in read_text(sdir, CARDS_FILE).split(_CARD_SEP) if c.strip()]
+
+
+def write_cards(sdir: Path, cards: list[str]) -> None:
+    """写出预取卡片。空表也落盘（空文件 = 已预取，续跑不再搜）。"""
+    write_text(sdir, CARDS_FILE, _CARD_SEP.join(cards) + "\n" if cards else "")
+
+
+def forget_card(sdir: Path, needle: str) -> int:
+    """Pro 主动遗忘一张卡片：立刻从 CARDS.md 去掉，并记进 FORGET.md。返回丢掉的张数。
+
+    needle 是文件名或正文里的唯一子串。命中 0 张仍然记 FORGET——卡片可能已经被摘要
+    吸收进纪要，那份副本要靠下一次 compact 才能去掉。
+    """
+    cards = load_cards(sdir) or []
+    kept = [c for c in cards if needle not in c]
+    if len(kept) != len(cards):
+        write_cards(sdir, kept)
+    append_forget(sdir, needle)
+    return len(cards) - len(kept)
+
+
+def write_long_note(sdir: Path, spec: Any) -> bool:
+    """把较长正文写入 notes/{name}.md，NOTES.md 只留文件名指针。"""
+    if not isinstance(spec, dict):
+        return False
+    raw = Path(str(spec.get("name") or "")).name
+    stem = raw[:-3] if raw.endswith(".md") else raw
+    raw = stem + ".md"
+    if not stem or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in stem):
+        return False
+    if stem.startswith("."):
+        return False
+    content = str(spec.get("content") or "").strip()
+    if not content:
+        return False
+    dest_dir = sdir / LONG_NOTES_DIR
+    dest_dir.mkdir(parents=True, exist_ok=True)
+    write_text(dest_dir, raw, content + "\n")
+    return append_notes(sdir, raw)
+
+
+def read_long_note(sdir: Path, path: str) -> str:
+    """读 notes/ 下由 notes_write 落盘的长文。只认文件名，取 .name 即杜绝越权。"""
+    name = Path(path).name
+    target = sdir / LONG_NOTES_DIR / name
+    if name in {"", ".", ".."} or not target.is_file():
+        return f"[ERROR/FileNotFoundError] {path}"
+    return target.read_text(encoding="utf-8", errors="replace")[:80_000]
 
 
 def clear_forget(sdir: Path) -> int:
@@ -181,13 +257,6 @@ def clear_forget(sdir: Path) -> int:
     return count
 
 
-def memory_block(notes: SessionNotes) -> str:
-    """交给 assemble 的记忆块。空则返回空串，调用方不加 memory 槽位。"""
-    if not notes.has_memory:
-        return ""
-    return "## 必须记住的不变量\n" + notes.memory
-
-
 def forget_directive(notes: SessionNotes) -> str:
     """交给 summarizer 的排除指令。无条目时返回空串，不拼进 summarizer 的 system。"""
     if not notes.has_forget:
diff --git a/runtime/lab/flow.py b/runtime/lab/flow.py
index 76a977c..60c456b 100644
--- a/runtime/lab/flow.py
+++ b/runtime/lab/flow.py
@@ -11,7 +11,14 @@ from pathlib import Path
 from typing import Any
 
 from memory.profile import load_profile
-from runtime.context.notes import append_forget, apply_memory
+from memory.retrieve import prefetch_cards, reconcile_index
+from runtime.context.notes import (
+    append_forget,
+    apply_notes,
+    load_cards,
+    write_cards,
+    write_long_note,
+)
 from runtime.lab.accept import AcceptResult, FAIL, NO_HARD_CRITERIA, TEST_INVALID, missing_gate
 from runtime.lab.helpers import (
     halt_of,
@@ -123,7 +130,7 @@ class LabState:
         rules = self.runner._applied_rules or []
         if not rules:
             return "（本 lab 没有适用的 /remember 规则）"
-        return "\n".join(f"- {r}" for r in rules)
+        return "\n".join(f"{i}. {r}" for i, r in enumerate(rules))
 
     async def drive_pro(self, kind: TaskKind, user: str, label: str) -> dict[str, Any]:
         """跑一拍 Pro：并入同一条 transcript，submit 写回 Task 树。"""
@@ -181,6 +188,16 @@ class LabState:
             kept = result.history if phase.carry_prose else _strip_prose(result.history)
             self.pro_history[:] = drop_dangling_tool_calls(kept)
 
+        payload = _submit_payload(result.submit)
+        append_forget(self.session_dir, str(payload.get("forget_append") or ""))
+        apply_notes(
+            self.session_dir,
+            replace=payload.get("notes_replace"),
+            remove=payload.get("notes_remove"),
+            append=str(payload.get("notes_append") or ""),
+        )
+        write_long_note(self.session_dir, payload.get("notes_write"))
+
         if result.submit:
             child.transit(TaskStatus.COMPLETED)
             child.brief = result.submit
@@ -202,6 +219,14 @@ def _strip_prose(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
     ]
 
 
+def _submit_payload(submit: dict[str, Any] | None) -> dict[str, Any]:
+    """submit_brief 是裸 payload；其余是 {name, payload}。"""
+    if not submit:
+        return {}
+    inner = submit.get("payload") if submit.get("name") else submit
+    return inner if isinstance(inner, dict) else {}
+
+
 async def run_lab(
     runner: Any,
     question: str,
@@ -238,6 +263,10 @@ async def _begin(
     save_tree(session_dir, tree)
 
     reset_sandbox_failure_counter()
+    try:
+        await reconcile_index(runner.llm, runner.settings)
+    except Exception:
+        pass
     ledger = EffectLedger.load(session_dir, runner.settings.workspace_dir)
     catalog = read_text(session_dir, CATALOG_FILE) or await ingest(runner.settings, session_dir)
     return LabState(
@@ -320,6 +349,12 @@ async def write_spec(st: LabState) -> dict[str, Any] | None:
     if st.halt:
         return None
 
+    cards = load_cards(st.session_dir)
+    if cards is None:
+        cards = await prefetch_cards(st.question, st.runner.llm, settings=st.runner.settings)
+        write_cards(st.session_dir, cards)
+    await st.emit({"kind": "cards", "n": len(cards)})
+
     project = resume_spec(st.tree) if st.resume else None
     if project is not None:
         await st.emit({"kind": "node_done", "node": "spec", "log": [{"resumed": True}]})
@@ -508,9 +543,9 @@ async def _one_step(
 
     briefs: list[dict[str, Any]] = []
     spec_invalid = False
-    last_gate = AcceptResult(state=NO_HARD_CRITERIA)
+    step_gates: list[AcceptResult] = []
     if assignments:
-        briefs, spec_invalid, last_gate, step_halt = await run_step(
+        briefs, spec_invalid, wave_gate, step_halt = await run_step(
             st.runner,
             assignments,
             question=st.question,
@@ -525,6 +560,7 @@ async def _one_step(
         if step_halt:
             st.halt = step_halt
             return "break", gate_nudge, revisions
+        step_gates.append(wave_gate)
         for a, brief in zip(assignments, briefs):
             state = str((brief.get("tests") or {}).get("state") or "")
             if state:
@@ -532,7 +568,8 @@ async def _one_step(
     if replay:
         extra, extra_gate = await _replay_gates(st, replay)
         briefs = briefs + extra
-        last_gate = worst_gate(last_gate, extra_gate)
+        step_gates.append(extra_gate)
+    last_gate = worst_gate(step_gates)
     st.run_gate = last_gate
 
     decision = await _judge(st, dispatch, briefs, last_gate)
@@ -554,11 +591,11 @@ async def _replay_gates(
     st: LabState, replay: list
 ) -> tuple[list[dict[str, Any]], AcceptResult]:
     """同一产品本 run 已派过 Flash，且门禁是 test_invalid：只重跑 pytest。"""
-    last = AcceptResult(state=NO_HARD_CRITERIA)
+    gates: list[AcceptResult] = []
     briefs: list[dict[str, Any]] = []
     for a in replay:
         gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
-        last = worst_gate(last, gate)
+        gates.append(gate)
         st.record_gate(a.gate_id, gate.state)
         brief = {
             "assignment_id": a.id,
@@ -582,17 +619,23 @@ async def _replay_gates(
                 "gate": gate.state,
             }
         )
-    return briefs, last
+    return briefs, worst_gate(gates)
 
 
 def _summary_gate(st: LabState) -> AcceptResult:
     """整次 lab 的门禁：各 assignment 最差态。没有记录则用最近一步。"""
     if not st.gates:
         return st.run_gate
-    acc = AcceptResult(state=NO_HARD_CRITERIA)
-    for state in st.gates.values():
-        acc = worst_gate(acc, AcceptResult(state=state))
-    return acc
+    return worst_gate(AcceptResult(state=state) for state in st.gates.values())
+
+
+def _gate_line(st: LabState) -> str:
+    """整体门禁 + 逐个 assignment 的明细，让收尾那一拍能自己对账。"""
+    overall = _summary_gate(st).state
+    if not st.gates:
+        return overall
+    detail = ", ".join(f"{gid}={state}" for gid, state in sorted(st.gates.items()))
+    return f"{overall}（{detail}）"
 
 
 async def _judge(
@@ -607,7 +650,8 @@ async def _judge(
         f"Briefs:\n{json.dumps(briefs, ensure_ascii=False)[:_BRIEFS_CAP]}\n"
         f"## Applicable /remember rules\n{st.remember_block()}\n"
         "If the gate is no_hard_criteria you MUST say so in evidence and give a semantic rationale. "
-        "Fill rule_verdicts for every applicable /remember rule. finish only when all are satisfied."
+        "Fill rule_verdicts for every applicable /remember rule, identified by the index above. "
+        "Do not retype the rule text as the identifier. finish only when all are satisfied."
     )
     if last_gate.state == TEST_INVALID and st.gate_unrunnable():
         judge_user += (
@@ -637,12 +681,12 @@ async def _judge(
     verdict = payload_of(judge_submit, SUBMIT_JUDGE)
     decision = str(verdict.get("decision") or "continue")
     if last_gate.state == TEST_INVALID and dispatch.assignments and not st.gate_unrunnable():
-        refreshed = AcceptResult(state=NO_HARD_CRITERIA)
+        refreshed: list[AcceptResult] = []
         for a in dispatch.assignments:
             gate = await evaluate_gate(st.runner, st.session_dir, a.gate_id)
             st.record_gate(a.gate_id, gate.state)
-            refreshed = worst_gate(refreshed, gate)
-        last_gate = refreshed
+            refreshed.append(gate)
+        last_gate = worst_gate(refreshed)
         st.run_gate = last_gate
     if decision == "finish" and not rules_satisfied(st.runner._applied_rules or [], verdict):
         decision = "continue"
@@ -654,14 +698,6 @@ async def _judge(
     if decision == "takeover" and last_gate.state == TEST_INVALID:
         decision = "continue"
 
-    apply_memory(
-        st.session_dir,
-        replace=verdict.get("memory_replace"),
-        remove=verdict.get("memory_remove"),
-        append=str(verdict.get("memory_append") or ""),
-    )
-    append_forget(st.session_dir, str(verdict.get("forget_append") or ""))
-
     st.runner._trace_event(
         S.EV_DECISION,
         **{
@@ -752,8 +788,10 @@ async def summarize(st: LabState) -> dict[str, Any]:
             f"and the artifacts below.\n\n"
             f"User request: {st.question}\n\n## SPEC.md\n{read_text(st.session_dir, SPEC_FILE)}\n\n"
             f"## 完成情况\n{render_progress(st.progress)}\n\n"
-            f"Gate: {_summary_gate(st).state}\n"
-            "Call submit_summary. If the gate was no_hard_criteria, say so in user_summary."
+            f"Gate: {_gate_line(st)}\n"
+            "Call submit_summary. Report that gate state as-is; it is the harness result, "
+            "and the per-assignment breakdown above is what it was computed from. "
+            "If it says no_hard_criteria, say so in user_summary."
         )
     await st.emit({"kind": "node_start", "node": "summary"})
     payload = payload_of(
diff --git a/runtime/lab/helpers.py b/runtime/lab/helpers.py
index 463d9f8..529ac2c 100644
--- a/runtime/lab/helpers.py
+++ b/runtime/lab/helpers.py
@@ -1,5 +1,6 @@
 """LabRunner 用的纯函数：payload 抽取、续跑切分、门禁取最差。"""
 
+from collections.abc import Iterable
 from dataclasses import replace
 from typing import Any
 
@@ -79,21 +80,29 @@ def render_progress(progress: list[tuple[str, str]]) -> str:
     return "\n".join(lines)[-_PROGRESS_CAP:] or "（还没有完成任何步骤）"
 
 
-def worst_gate(a: AcceptResult, b: AcceptResult) -> AcceptResult:
-    order = {PASS: 1, NO_HARD_CRITERIA: 2, TEST_INVALID: 3, FAIL: 4}
-    return b if order.get(b.state, 0) > order.get(a.state, 0) else a
+# 四态由好到坏。no_hard_criteria 排在 pass 之后：没有硬指标不等于通过。
+_GATE_RANK = {PASS: 1, NO_HARD_CRITERIA: 2, TEST_INVALID: 3, FAIL: 4}
+
+
+def worst_gate(results: Iterable[AcceptResult]) -> AcceptResult:
+    """一组门禁里最差的那一个，原样返回（保住它的 passed/failed/log）。
+
+    只有空输入才是 no_hard_criteria——那是「这里没有门禁」。它不能当折叠初值：
+    它比 pass 差，pass 永远替换不掉它，整组全过也会被报成无硬指标。
+    """
+    worst: AcceptResult | None = None
+    for result in results:
+        if worst is None or _GATE_RANK.get(result.state, 0) > _GATE_RANK.get(worst.state, 0):
+            worst = result
+    return worst if worst is not None else AcceptResult(state=NO_HARD_CRITERIA)
 
 
 def step_gate(results: list[tuple[RuntimeTask, dict[str, Any]]]) -> AcceptResult:
-    """整步的门禁结论：有 fail 取 fail，全无硬指标取 no_hard_criteria。"""
-    states = [
-        str((brief.get("tests") or {}).get("state") or NO_HARD_CRITERIA)
+    """整步的门禁结论。与 run 级同一套序，避免两处各自定义「整体门禁」。"""
+    return worst_gate(
+        AcceptResult(state=str((brief.get("tests") or {}).get("state") or NO_HARD_CRITERIA))
         for _, brief in results
-    ]
-    for priority in (FAIL, TEST_INVALID, PASS):
-        if priority in states:
-            return AcceptResult(state=priority)
-    return AcceptResult(state=NO_HARD_CRITERIA)
+    )
 
 
 def resume_spec(tree: TaskTree) -> ProjectSpec | None:
diff --git a/runtime/lab/remember.py b/runtime/lab/remember.py
index 22f7506..c2a3187 100644
--- a/runtime/lab/remember.py
+++ b/runtime/lab/remember.py
@@ -1,4 +1,4 @@
-"""本 lab 适用的 /remember 条文。REMEMBER.json 与 SPEC/MEMORY 分开。
+"""本 lab 适用的 /remember 条文。REMEMBER.json 与 SPEC/NOTES 分开。
 
 remember_judge 只裁定 applies；未点名的条目默认不适用。
 步骤 Judge 的 rule_verdicts 必须盖住适用列表，否则不能 finish。
@@ -57,11 +57,14 @@ def save_applied(session_dir: Path, rules: list[str]) -> None:
 
 
 def rules_satisfied(applied: list[str], verdict: dict[str, Any]) -> bool:
+    """步骤 Judge 必须盖住 applied 里的每一条。对齐靠 index，与 applied_from_payload 同一套。"""
     if not applied:
         return True
-    ok = {
-        str(row.get("rule") or "").strip()
-        for row in (verdict.get("rule_verdicts") or [])
-        if isinstance(row, dict) and row.get("satisfied")
-    }
+    ok: set[str] = set()
+    for row in verdict.get("rule_verdicts") or []:
+        if not isinstance(row, dict) or not row.get("satisfied"):
+            continue
+        text = _resolve(applied, row)
+        if text:
+            ok.add(text)
     return all(rule in ok for rule in applied)
diff --git a/runtime/loop/control.py b/runtime/loop/control.py
index 78f7137..08347dd 100644
--- a/runtime/loop/control.py
+++ b/runtime/loop/control.py
@@ -14,6 +14,7 @@ class Decision:
     kind: str  # continue | retry_tool | nudge | force_brief | stop
     delay: float = 0.0
     text: str = ""
+    # 短枚举，供 EV_DECISION 归因。continue 也必须有：ok / logic / transient / ...
     reason: str = ""
 
     @property
@@ -46,19 +47,24 @@ def decide(
         return Decision(kind="force_brief", reason="step_budget")
 
     if stagnation is StagnationSignal.REPEAT:
-        return Decision(kind="nudge", text=NUDGE_TEXT)
+        return Decision(kind="nudge", text=NUDGE_TEXT, reason="repeat")
 
     if error_class is ErrorClass.TRANSIENT:
         if snap.transient_count >= settings.transient_retry_max:
             return Decision(
                 kind="nudge",
                 text="Transient retries exhausted. Switch tools or change approach. Do not mark spec_invalid.",
+                reason="transient_exhausted",
             )
-        return Decision(kind="retry_tool", delay=delay_for(snap.transient_count))
+        return Decision(
+            kind="retry_tool",
+            delay=delay_for(snap.transient_count),
+            reason="transient",
+        )
 
     if error_class is ErrorClass.LOGIC:
         if snap.consecutive_logic >= settings.consecutive_logic_failure_max:
             return Decision(kind="stop", reason="logic_exhausted")
-        return Decision(kind="continue")
+        return Decision(kind="continue", reason="logic")
 
-    return Decision(kind="continue")
+    return Decision(kind="continue", reason="ok")
diff --git a/runtime/loop/cycle.py b/runtime/loop/cycle.py
index c0e191d..43b573e 100644
--- a/runtime/loop/cycle.py
+++ b/runtime/loop/cycle.py
@@ -18,7 +18,7 @@ from config.runtime import RuntimeSettings
 from runtime.context.assemble import AgentContext, assemble, validate_message_sequence
 from runtime.context.budget import TokenBudget
 from runtime.context.compact import DumpScope, compact_history
-from runtime.context.notes import load_notes, memory_block
+from runtime.context.notes import load_notes
 from runtime.loop.control import decide
 from runtime.errors import ErrorClass
 from runtime.llm import LLMGateway
@@ -38,6 +38,7 @@ _RETRIEVAL_TOOLS = {
     "memory_search",
     "memory_grep",
     "memory_read",
+    "notes_read",
     LOAD_SKILL,
     LOAD_SKILL_REFERENCE,
 }
@@ -191,7 +192,8 @@ async def run_loop(
                 project_spec=project_spec,
                 history=history,
                 retrieved=retrieved,
-                memory=memory_block(notes),
+                notes=notes.notes_block,
+                cards=notes.cards_block if spec.name == PRO else "",
                 events=task.events,
                 working=working,
                 tool_schemas=openai_tools,
diff --git a/runtime/loop/handlers.py b/runtime/loop/handlers.py
index ccca31c..c82f0c7 100644
--- a/runtime/loop/handlers.py
+++ b/runtime/loop/handlers.py
@@ -9,6 +9,7 @@ from runtime.loop.registry import Handler, ToolContext, ToolRegistry, ToolSpec,
 
 def build_base_specs() -> list[ToolSpec]:
     from memory.retrieve import memory_grep, memory_read, memory_search
+    from runtime.context.notes import forget_card, read_long_note
     from runtime.lab.accept import write_acceptance_file
     from runtime.loop.schema import (
         ACCEPT_FILE_SCHEMA,
@@ -117,6 +118,22 @@ def build_base_specs() -> list[ToolSpec]:
     async def h_mem_read(args: dict[str, Any], ctx: ToolContext) -> str:
         return await asyncio.to_thread(memory_read, args["path"], ctx.settings)
 
+    async def h_notes_read(args: dict[str, Any], ctx: ToolContext) -> str:
+        return await asyncio.to_thread(read_long_note, ctx.session_dir, args["path"])
+
+    async def h_mem_forget(args: dict[str, Any], ctx: ToolContext) -> str:
+        from memory.retrieve import retire_cards_matching
+
+        card = str(args.get("card") or "").strip()
+        if not card:
+            return "[ERROR/Validation] need a card filename or a topic substring"
+        n = await asyncio.to_thread(forget_card, ctx.session_dir, card)
+        retired = await asyncio.to_thread(retire_cards_matching, card, ctx.settings)
+        msg = f"dropped {n} card(s) from the next turn; recorded in FORGET.md"
+        if retired:
+            msg += f"; retired {retired} from the archive"
+        return msg
+
     async def h_profile(args: dict[str, Any], ctx: ToolContext) -> str:
         return json.dumps(await asyncio.to_thread(read_profile), ensure_ascii=False)
 
@@ -185,7 +202,7 @@ def build_base_specs() -> list[ToolSpec]:
         ),
         ToolSpec(
             "memory_search",
-            "Vector-search archived knowledge cards.",
+            "Vector-search archived cards; returns filename+type pointers, not bodies. memory_read to open.",
             _fields("query", query="string", k="integer"),
             readonly,
             h_mem_search,
@@ -199,11 +216,28 @@ def build_base_specs() -> list[ToolSpec]:
         ),
         ToolSpec(
             "memory_read",
-            "Read one card, session note, or tool_results dump.",
+            "Read one archived card (4.md) or a tool_results dump.",
             _fields("path", path="string"),
             readonly,
             h_mem_read,
         ),
+        ToolSpec(
+            "notes_read",
+            "Read a long note written by notes_write (filename in NOTES.md, e.g. ttl.md).",
+            _fields("path", path="string"),
+            pro,
+            h_notes_read,
+        ),
+        ToolSpec(
+            "memory_forget",
+            "Retire a card that is false, outdated, or contradicted by this homework: gone from "
+            "the next turn, dropped from the archive so later labs will not retrieve it. "
+            "card = its filename or a unique substring of its body. Do not use this merely "
+            "because a still-true card is off-topic for this assignment.",
+            _fields("card", card="string"),
+            pro,
+            h_mem_forget,
+        ),
         ToolSpec(
             LOAD_SKILL,
             "Pull one skill SOP this lab. Skills are mutually exclusive; skip if none fits.",
diff --git a/runtime/loop/schema.py b/runtime/loop/schema.py
index d7ac63a..e914e24 100644
--- a/runtime/loop/schema.py
+++ b/runtime/loop/schema.py
@@ -2,9 +2,22 @@
 
 from typing import Any
 
-from runtime.context.notes import MEMORY_ENTRY_MAX
+from runtime.context.notes import NOTES_ENTRY_MAX
 from runtime.lab.spec import MAX_ASSIGNMENTS
 
+# 短句用 notes_append；长文用 notes_write，NOTES.md 只留文件名指针。
+NOTES_WRITE = {
+    "type": "object",
+    "properties": {
+        "name": {"type": "string", "description": "文件名，如 ttl.md。NOTES.md 只留这一条指针"},
+        "content": {"type": "string", "description": "较长的必须记住的正文。短句请用 notes_append"},
+    },
+}
+FORGET_APPEND = {
+    "type": "string",
+    "description": "无关噪声（已放弃的路径、死胡同探测）。下次 compact 时 Flash 会从摘要里去掉。热卡片请用 memory_forget。",
+}
+
 # Flash brief 只含本步实际改动与碰到的错误，长度上限 BRIEF_MAX。
 BRIEF_MAX = 600
 
@@ -74,6 +87,13 @@ SPEC_SCHEMA: dict[str, Any] = {
             "items": {"type": "string"},
             "description": "少量有意义的 Flash 产品块（一题/一文件/一函数），不是阅读/设计/测试/打磨清单。一道题一条即可",
         },
+        "forget_append": FORGET_APPEND,
+        "notes_append": {
+            "type": "string",
+            "maxLength": NOTES_ENTRY_MAX,
+            "description": "新增一条不超过 80 字符的不变量。空则不写。",
+        },
+        "notes_write": NOTES_WRITE,
     },
     "additionalProperties": True,
 }
@@ -139,6 +159,13 @@ DISPATCH_SCHEMA: dict[str, Any] = {
             "items": ASSIGNMENT_SCHEMA,
             "description": f"本步派出的 Flash，最多 {MAX_ASSIGNMENTS} 个。Flash 侧全部完成时给空数组",
         },
+        "forget_append": FORGET_APPEND,
+        "notes_append": {
+            "type": "string",
+            "maxLength": NOTES_ENTRY_MAX,
+            "description": "新增一条不超过 80 字符的不变量。空则不写。",
+        },
+        "notes_write": NOTES_WRITE,
     },
     "additionalProperties": True,
 }
@@ -193,17 +220,17 @@ JUDGE_SCHEMA: dict[str, Any] = {
             "enum": ["continue", "revise_spec", "takeover", "finish"],
         },
         "evidence": {"type": "string", "minLength": 4},
-        "memory_append": {
+        "notes_append": {
             "type": "string",
-            "maxLength": MEMORY_ENTRY_MAX,
+            "maxLength": NOTES_ENTRY_MAX,
             "description": "新增一条不超过 80 字符的不变量。空则不写。禁止贴实现细节或复述 SPEC.md。",
         },
-        "memory_remove": {
+        "notes_remove": {
             "type": "array",
             "items": {"type": "string"},
-            "description": "删除 MEMORY.md 中正文等于或包含该字符串的条目。过时了就删，不要只追加。",
+            "description": "删除 NOTES.md 中正文等于或包含该字符串的条目。过时了就删，不要只追加。",
         },
-        "memory_replace": {
+        "notes_replace": {
             "type": "array",
             "items": {
                 "type": "object",
@@ -212,27 +239,32 @@ JUDGE_SCHEMA: dict[str, Any] = {
                     "old": {"type": "string", "description": "要改的那条：全文或能唯一定位的子串"},
                     "new": {
                         "type": "string",
-                        "maxLength": MEMORY_ENTRY_MAX,
+                        "maxLength": NOTES_ENTRY_MAX,
                         "description": "改写后的短不变量；空字符串表示删除",
                     },
                 },
             },
             "description": "改已有条目。事实变了就改，不要另起一行让旧事实继续常驻。",
         },
-        "forget_append": {
-            "type": "string",
-            "description": "已确认与任务无关的杂乱上下文描述。压缩总结时会被刻意忽略。",
-        },
+        "notes_write": NOTES_WRITE,
+        "forget_append": FORGET_APPEND,
         "rule_verdicts": {
             "type": "array",
-            "description": "对本 lab 已裁定适用的每条 /remember 规则给出对照结论。finish 时必须全部 satisfied。",
+            "description": "对本 lab 已裁定适用的每条 /remember 规则给出对照结论。按用户消息里的编号对齐。finish 时必须全部 satisfied。",
             "items": {
                 "type": "object",
-                "required": ["rule", "satisfied", "note"],
+                "required": ["index", "satisfied", "note"],
                 "properties": {
-                    "rule": {"type": "string"},
+                    "index": {
+                        "type": "integer",
+                        "description": "用户消息里印在该规则前面的编号。身份靠它对齐，不要靠抄规则原文",
+                    },
                     "satisfied": {"type": "boolean"},
                     "note": {"type": "string"},
+                    "rule": {
+                        "type": "string",
+                        "description": "可选，仅作可读标注；抄错不影响对齐",
+                    },
                 },
             },
         },
diff --git a/runtime/phase.py b/runtime/phase.py
index 825ffa9..4e172d5 100644
--- a/runtime/phase.py
+++ b/runtime/phase.py
@@ -41,6 +41,7 @@ FLASH = "flash"
 # 只读 Pro 阶段默认看不见 pro 权限工具；skill 读取经 extra 点名放行。
 # Flash 不加载 skill；submit_halt 经 extra 点名放行。
 _SKILL_READ = frozenset({LOAD_SKILL, LOAD_SKILL_REFERENCE})
+_PRO_SESSION = frozenset({"memory_forget", "notes_read"})
 # 除 SUMMARY 外均可短路：规划时可能看不出来，做到一半才发现缺用户才能给的信息。
 _HALT = frozenset({SUBMIT_HALT})
 
@@ -95,7 +96,7 @@ PHASES: dict[TaskKind, PhaseSpec] = {
         submit_tool=SUBMIT_SPEC,
         permission=Permission.PRO,
         visible=Permission.READONLY,
-        extra_tools=_SKILL_READ | _HALT,
+        extra_tools=_SKILL_READ | _PRO_SESSION | _HALT,
         shares_thread=True,
     ),
     TaskKind.REMEMBER_JUDGE: PhaseSpec(
@@ -117,7 +118,7 @@ PHASES: dict[TaskKind, PhaseSpec] = {
         submit_tool=SUBMIT_DISPATCH,
         permission=Permission.PRO,
         visible=Permission.READONLY,
-        extra_tools=_SKILL_READ | {WRITE_ACCEPTANCE} | _HALT,
+        extra_tools=_SKILL_READ | _PRO_SESSION | {WRITE_ACCEPTANCE} | _HALT,
         shares_thread=True,
     ),
     TaskKind.JUDGE: PhaseSpec(
@@ -126,7 +127,7 @@ PHASES: dict[TaskKind, PhaseSpec] = {
         submit_tool=SUBMIT_JUDGE,
         permission=Permission.PRO,
         visible=Permission.READONLY,
-        extra_tools=_SKILL_READ | {WRITE_ACCEPTANCE} | _HALT,
+        extra_tools=_SKILL_READ | _PRO_SESSION | {WRITE_ACCEPTANCE} | _HALT,
         shares_thread=True,
     ),
     TaskKind.TAKEOVER: PhaseSpec(
@@ -143,7 +144,7 @@ PHASES: dict[TaskKind, PhaseSpec] = {
         submit_tool=SUBMIT_SUMMARY,
         permission=Permission.PRO,
         visible=Permission.READONLY,
-        extra_tools=_SKILL_READ,
+        extra_tools=_SKILL_READ | _PRO_SESSION,
         shares_thread=True,
     ),
     TaskKind.WORKER: PhaseSpec(
diff --git a/runtime/session.py b/runtime/session.py
index ff3aeb9..9b4dae6 100644
--- a/runtime/session.py
+++ b/runtime/session.py
@@ -121,8 +121,26 @@ class LabSession:
         title = self.last_result.get("question") or "未命名任务"
         summary = self.last_result.get("summary") or ""
         ttype = "other"
+        if not cards:
+            return {
+                "task_id": None,
+                "card_ids": [],
+                "indexed": 0,
+                "failed": 0,
+                "errors": [],
+                "skipped": "no_cards",
+            }
         try:
             archive = get_task_archive()
+            if not archive.has_new_cards(cards):
+                return {
+                    "task_id": None,
+                    "card_ids": [],
+                    "indexed": 0,
+                    "failed": 0,
+                    "errors": [],
+                    "skipped": "no_cards",
+                }
             task_id = archive.create_task(title, ttype, summary[:4000])
             card_ids = archive.create_cards(task_id, cards, title, ttype)
             result: dict[str, Any] = {
diff --git a/runtime/task.py b/runtime/task.py
index e9e78c4..84a5b8f 100644
--- a/runtime/task.py
+++ b/runtime/task.py
@@ -23,7 +23,7 @@ class TaskKind(str, Enum):
     WORKER = "worker"      # Flash 执行一份任务书
     TAKEOVER = "takeover"  # Flash 做不动时 Pro 接手本步实现
     JUDGE = "judge"
-    REMEMBER_JUDGE = "remember_judge"  # 裁定 /remember 是否适用于本 lab，不写 SPEC/MEMORY
+    REMEMBER_JUDGE = "remember_judge"  # 裁定 /remember 是否适用于本 lab，不写 SPEC/NOTES
     SUMMARY = "summary"
 
 
diff --git a/server/app.py b/server/app.py
index 2f32c02..14f83f1 100644
--- a/server/app.py
+++ b/server/app.py
@@ -51,6 +51,21 @@ def _is_running() -> bool:
     return _current_task is not None and not _current_task.done()
 
 
+@app.on_event("startup")
+async def _warm_memory_index() -> None:
+    """进程起来时对齐卡片文件与向量表。换 EMBEDDING_MODEL 后必须走这里才会重建。失败不挡服务。"""
+
+    async def _run() -> None:
+        try:
+            from memory.retrieve import reconcile_index
+
+            await reconcile_index(_session.llm, _session.settings)
+        except Exception:
+            pass
+
+    asyncio.create_task(_run())
+
+
 @app.on_event("shutdown")
 async def _shutdown() -> None:
     """进程退出前把 span 刷出去，否则最后一段 trace 会丢在缓冲里。"""
@@ -380,13 +395,15 @@ async def done() -> dict[str, Any]:
         title = (_session.last_result or {}).get("question") or "未命名任务"
         summary = (_session.last_result or {}).get("summary") or ""
         archive = get_task_archive()
-        task_id = archive.create_task(title, "other", summary[:4000])
-        card_ids = archive.create_cards(task_id, cards, title, "other")
-        if card_ids:
-            archive_extra = await index_card_ids(card_ids, _session.llm, _session.settings)
-            archive_extra["task_id"] = task_id
-            archive_extra["card_ids"] = card_ids
-            _session.last_result["knowledge_cards"] = []  # 已入档，清空以免 _session.done 二次归档
+        if archive.has_new_cards(cards):
+            task_id = archive.create_task(title, "other", summary[:4000])
+            card_ids = archive.create_cards(task_id, cards, title, "other")
+            if card_ids:
+                archive_extra = await index_card_ids(card_ids, _session.llm, _session.settings)
+                archive_extra["task_id"] = task_id
+                archive_extra["card_ids"] = card_ids
+        if _session.last_result:
+            _session.last_result["knowledge_cards"] = []  # 已处理，清空以免 _session.done 再建空 task
     result = await asyncio.to_thread(_session.done, lambda m: None)
     if archive_extra:
         result["archive"] = {**(result.get("archive") or {}), **archive_extra}
diff --git a/server/static/index.html b/server/static/index.html
index b36e8bb..f8ec2e8 100644
--- a/server/static/index.html
+++ b/server/static/index.html
@@ -572,6 +572,10 @@
         logTool(d.name, (d.args || '').slice(0, 80));
         if (d.result) logLine('toolret', '└ ' + d.result.slice(0, 140));
       });
+      es.addEventListener('cards', (e) => {
+        const d = JSON.parse(e.data);
+        logLine('toolret', 'prefetched cards ' + (d.n || 0));
+      });
       es.addEventListener('node_done', (e) => {
         const d = JSON.parse(e.data);
         const bits = (d.log || []).map(en =>
diff --git a/tools/policy.py b/tools/policy.py
index 35f2c9f..b00143b 100644
--- a/tools/policy.py
+++ b/tools/policy.py
@@ -74,7 +74,7 @@ class SecurityPolicy:
         re.compile(_BOUNDARY + r"~/"),                   # ~/path 家目录展开
         re.compile(_BOUNDARY + r"~($|[\s'\")])"),        # 独立 ~ token
     ]
-    # LLM 不可见的控制面文件。SPEC.md / MEMORY.md / 卸盘正文不在此列。
+    # LLM 不可见的控制面文件。SPEC.md / NOTES.md / 卸盘正文不在此列。
     CONTROL_FILES = frozenset({
         "STATE.json",
         "EFFECTS.json",
diff --git a/eval/profile.yaml b/eval/profile.yaml
new file mode 100644
--- /dev/null
+++ b/eval/profile.yaml
@@ -0,0 +1,18 @@
+# 评测专用画像。不写入 profile/me.yaml。
+# style_rules 顺序即 remember_judge 的 index：0 screenshot / 1 blockquote / 2 filename
+identity:
+  name: 张鑫
+  student_id: 202112342
+
+preferences:
+  language: zh-CN
+  writing_style:
+    formality: medium
+    avg_sentence_len: 25
+  coding_style:
+    type_hints: true
+    docstring: short
+  style_rules:
+    - 实验报告里的截图统一写成「（此处建议附 XX 截图）」占位，不要编造已经拍摄的截图。
+    - 引用实验指导书原文时用 markdown 引用块（行首 >），不要改写成自己的话却不标注。
+    - 编程题源文件按题目命名（如 two_sum.py），禁止 solution.py / main.py。
diff --git a/eval/run_case.py b/eval/run_case.py
new file mode 100644
--- /dev/null
+++ b/eval/run_case.py
@@ -0,0 +1,456 @@
+#!/usr/bin/env python3
+"""跑一个 eval case，或 --all 串行跑 9 次 lab。产物写入 --out 下的 result.json。
+
+不调用 LabSession.done()（会把 workspace 推进 .trash）。A 的卡片由本脚本
+create_cards + await index_card_ids 写入 pair 卡池。
+"""
+from __future__ import annotations
+
+import argparse
+import asyncio
+import json
+import os
+import shutil
+import sys
+import time
+import traceback
+from pathlib import Path
+from typing import Any
+
+import yaml
+
+REPO = Path(__file__).resolve().parent.parent
+if str(REPO) not in sys.path:
+    sys.path.insert(0, str(REPO))
+
+CASES_DIR = Path(__file__).resolve().parent / "cases"
+PROFILE_PATH = Path(__file__).resolve().parent / "profile.yaml"
+RULE_IDS = ("screenshot", "blockquote", "filename")
+PAIRS: list[tuple[str, str]] = [
+    ("two_sum", "two_sum_variant"),
+    ("minikv_lab05", "minikv_variant"),
+    ("essay_a", "essay_variant"),
+]
+
+
+def load_expect(case: str) -> dict[str, Any]:
+    path = CASES_DIR / case / "expect.yaml"
+    data = yaml.safe_load(path.read_text(encoding="utf-8"))
+    if not isinstance(data, dict):
+        raise ValueError(f"invalid expect.yaml: {path}")
+    return data
+
+
+def reset_singletons() -> None:
+    from config.runtime import get_settings
+
+    get_settings.cache_clear()
+    import memory.archive as archive_mod
+    import tools.policy as policy_mod
+
+    archive_mod._default_archive = None
+    policy_mod._policy = None
+    policy_mod._auditor = None
+
+
+def apply_env(*, workspace: Path, cards_dir: Path, memory_db: Path) -> None:
+    workspace.mkdir(parents=True, exist_ok=True)
+    cards_dir.mkdir(parents=True, exist_ok=True)
+    memory_db.parent.mkdir(parents=True, exist_ok=True)
+    os.environ["WORKSPACE_DIR"] = str(workspace.resolve())
+    os.environ["CARDS_DIR"] = str(cards_dir.resolve())
+    os.environ["MEMORY_DB_PATH"] = str(memory_db.resolve())
+    os.environ["PROFILE_PATH"] = str(PROFILE_PATH.resolve())
+    reset_singletons()
+
+
+def seed_workspace(case: str, workspace: Path) -> None:
+    if workspace.exists():
+        for child in workspace.iterdir():
+            if child.is_dir():
+                shutil.rmtree(child)
+            else:
+                child.unlink()
+    workspace.mkdir(parents=True, exist_ok=True)
+    materials = CASES_DIR / case / "materials"
+    for src in materials.iterdir():
+        dest = workspace / src.name
+        if src.is_dir():
+            shutil.copytree(src, dest)
+        else:
+            shutil.copy2(src, dest)
+
+
+def _payload(brief: Any) -> dict[str, Any]:
+    if not isinstance(brief, dict):
+        return {}
+    payload = brief.get("payload")
+    if isinstance(payload, dict):
+        return payload
+    return brief
+
+
+def extract_state_facts(state: dict[str, Any]) -> dict[str, Any]:
+    milestones: list[str] = []
+    assignments: list[dict[str, Any]] = []
+    remember_verdicts: list[dict[str, Any]] = []
+    node_kinds: dict[str, int] = {}
+    for node in (state.get("nodes") or {}).values():
+        if not isinstance(node, dict):
+            continue
+        kind = node.get("kind")
+        if kind:
+            node_kinds[str(kind)] = node_kinds.get(str(kind), 0) + 1
+        brief = node.get("brief") or {}
+        payload = _payload(brief)
+        if kind == "spec" and payload.get("milestones") is not None:
+            milestones = [str(x) for x in (payload.get("milestones") or [])]
+        if kind == "dispatch":
+            for raw in payload.get("assignments") or []:
+                if isinstance(raw, dict):
+                    assignments.append(
+                        {
+                            "id": raw.get("id"),
+                            "goal": str(raw.get("goal") or ""),
+                            "spec": str(raw.get("spec") or ""),
+                            "expected_artifacts": list(raw.get("expected_artifacts") or []),
+                        }
+                    )
+        if kind == "worker":
+            spec = node.get("node_spec") or {}
+            if isinstance(spec, dict) and spec.get("goal"):
+                assignments.append(
+                    {
+                        "id": spec.get("id"),
+                        "goal": str(spec.get("goal") or ""),
+                        "spec": str(spec.get("spec") or ""),
+                        "expected_artifacts": list(spec.get("expected_artifacts") or []),
+                    }
+                )
+        if kind == "remember_judge":
+            remember_verdicts = [
+                row for row in (payload.get("verdicts") or []) if isinstance(row, dict)
+            ]
+    pred: dict[str, bool | None] = {rid: None for rid in RULE_IDS}
+    for row in remember_verdicts:
+        idx = row.get("index")
+        if isinstance(idx, int) and 0 <= idx < len(RULE_IDS):
+            pred[RULE_IDS[idx]] = bool(row.get("applies"))
+    return {
+        "milestones": milestones,
+        "milestones_len": len(milestones),
+        "assignments": assignments,
+        "remember_verdicts": remember_verdicts,
+        "remember_pred": pred,
+        "node_kinds": node_kinds,
+        # 派发轮数 = dispatch 节点数。同一个里程碑反复重派时这个数会涨，
+        # milestones_len 不会——后者冷热完全一致，没有判别力。
+        "dispatch_waves": node_kinds.get("dispatch", 0),
+        "takeovers": node_kinds.get("takeover", 0),
+    }
+
+
+def remember_gold(expect: dict[str, Any]) -> dict[str, bool]:
+    raw = expect.get("remember") or {}
+    return {rid: bool(raw.get(rid)) for rid in RULE_IDS}
+
+
+def write_json(path: Path, data: dict[str, Any]) -> None:
+    path.parent.mkdir(parents=True, exist_ok=True)
+    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
+
+
+async def run_external_gate(workspace: Path, case: str, timeout: float) -> dict[str, Any]:
+    from tools.sandbox_tools import sandbox_run
+
+    dest = workspace / "_eval_gate"
+    if dest.exists():
+        shutil.rmtree(dest)
+    shutil.copytree(CASES_DIR / case / "gate", dest)
+    code, log = await sandbox_run(
+        "cd /workspace && PYTHONPATH=/workspace python -m pytest -q _eval_gate",
+        timeout=timeout,
+    )
+    return {
+        "exit_code": int(code),
+        "passed": code == 0,
+        "log": (log or "")[-4000:],
+    }
+
+
+async def archive_knowledge(session: Any, result: dict[str, Any]) -> dict[str, Any]:
+    from config.runtime import get_settings
+    from memory.archive import TaskArchive
+    from memory.retrieve import index_card_ids
+    import memory.archive as archive_mod
+
+    cards = result.get("knowledge_cards") or []
+    if not cards:
+        return {"skipped": "no_cards", "card_ids": []}
+    settings = get_settings()
+    archive = TaskArchive(str(settings.memory_db_path))
+    archive_mod._default_archive = archive
+    if not archive.has_new_cards(cards):
+        return {"skipped": "no_new_cards", "card_ids": []}
+    title = str(result.get("question") or "eval")
+    summary = str(result.get("summary") or "")
+    task_id = archive.create_task(title, "other", summary[:4000])
+    card_ids = archive.create_cards(task_id, cards, title, "other")
+    indexed: dict[str, Any] = {"indexed": 0, "failed": 0, "errors": []}
+    if card_ids:
+        indexed = await index_card_ids(card_ids, session.llm, settings)
+    return {"task_id": task_id, "card_ids": card_ids, **indexed}
+
+
+def snapshot_session(session_dir: Path) -> dict[str, Any]:
+    from runtime.context.notes import load_cards
+    from runtime.lab.persist import STATE_FILE
+    from runtime.lab.remember import load_applied
+
+    state: dict[str, Any] = {}
+    state_path = session_dir / STATE_FILE
+    if state_path.is_file():
+        try:
+            state = json.loads(state_path.read_text(encoding="utf-8"))
+        except json.JSONDecodeError:
+            state = {}
+    facts = extract_state_facts(state) if state else {
+        "milestones": [],
+        "milestones_len": 0,
+        "assignments": [],
+        "remember_verdicts": [],
+        "remember_pred": {rid: None for rid in RULE_IDS},
+        "node_kinds": {},
+        "dispatch_waves": 0,
+        "takeovers": 0,
+    }
+    cards = load_cards(session_dir)
+    applied = load_applied(session_dir)
+    return {
+        "session_dir": str(session_dir),
+        "cards_prefetch": cards if cards is not None else [],
+        "cards_prefetched": cards is not None,
+        "applied_rules": applied if applied is not None else [],
+        **facts,
+    }
+
+
+async def run_one(
+    *,
+    case: str,
+    memory: str,
+    out_root: Path,
+    pair: str,
+    archive: bool,
+) -> dict[str, Any]:
+    expect = load_expect(case)
+    run_id = f"{case}_{memory}"
+    run_dir = out_root / run_id
+    workspace = run_dir / "workspace"
+    if memory == "cold":
+        pool = out_root / "pools" / f"{pair}_cold"
+    else:
+        pool = out_root / "pools" / pair
+    cards_dir = pool / "cards"
+    memory_db = pool / "memory.db"
+    apply_env(workspace=workspace, cards_dir=cards_dir, memory_db=memory_db)
+
+    from config.runtime import get_settings
+    from infra.sandbox_boot import recreate_sandbox
+    from runtime.session import LabSession
+
+    settings = get_settings()
+    recreate_sandbox(log=print)
+    seed_workspace(case, workspace)
+
+    session = LabSession(settings=settings)
+    question = str(expect.get("question") or "")
+    lab_result: dict[str, Any] = {}
+    error: str | None = None
+    try:
+        async def on_event(ev: dict[str, Any]) -> None:
+            kind = ev.get("kind") or ""
+            node = ev.get("node") or ""
+            print(f"[eval {run_id}] {kind} {node}", flush=True)
+
+        lab_result = await session.run(question, on_event=on_event)
+    except Exception as exc:
+        error = f"{type(exc).__name__}: {exc}"
+        traceback.print_exc()
+        lab_result = {
+            "verdict": "error",
+            "summary": "",
+            "knowledge_cards": [],
+            "question": question,
+        }
+
+    snap = snapshot_session(session.session_path) if session.session_path.exists() else {
+        "session_dir": str(session.session_path),
+        "cards_prefetch": [],
+        "cards_prefetched": False,
+        "applied_rules": [],
+        "milestones": [],
+        "milestones_len": 0,
+        "assignments": [],
+        "remember_verdicts": [],
+        "remember_pred": {rid: None for rid in RULE_IDS},
+        "node_kinds": {},
+        "dispatch_waves": 0,
+        "takeovers": 0,
+    }
+
+    gate: dict[str, Any]
+    try:
+        gate = await run_external_gate(workspace, case, timeout=settings.tool_timeout_s)
+    except Exception as exc:
+        gate = {
+            "exit_code": -1,
+            "passed": False,
+            "log": f"{type(exc).__name__}: {exc}",
+        }
+
+    archive_result: dict[str, Any] = {"skipped": "not_accumulate"}
+    if archive and not error:
+        try:
+            archive_result = await archive_knowledge(session, lab_result)
+        except Exception as exc:
+            archive_result = {"error": f"{type(exc).__name__}: {exc}", "card_ids": []}
+
+    traces_src = settings.traces_path
+    traces_dst = run_dir / "traces.jsonl"
+    if traces_src.is_file():
+        shutil.copy2(traces_src, traces_dst)
+
+    summary_path = workspace / "SUMMARY.md"
+    summary = str(lab_result.get("summary") or "")
+    if not summary and summary_path.is_file():
+        summary = summary_path.read_text(encoding="utf-8")
+
+    counted = memory != "cold"
+    result = {
+        "run_id": run_id,
+        "case": case,
+        "pair": pair,
+        "role": expect.get("role"),
+        "memory": memory,
+        "counted": counted,
+        "question": question,
+        "internal_verdict": lab_result.get("verdict"),
+        "external_passed": bool(gate.get("passed")),
+        "external_exit_code": gate.get("exit_code"),
+        "external_log": gate.get("log"),
+        "summary": summary,
+        "knowledge_cards": lab_result.get("knowledge_cards") or [],
+        "archive": archive_result,
+        "remember_gold": remember_gold(expect),
+        "remember_pred": snap.get("remember_pred"),
+        "applied_rules": snap.get("applied_rules"),
+        "cards_prefetch": snap.get("cards_prefetch"),
+        "cards_expected": list(expect.get("cards_expected") or []),
+        "card_assertions": list(expect.get("card_assertions") or []),
+        "milestones": snap.get("milestones"),
+        "milestones_len": snap.get("milestones_len"),
+        "assignments": snap.get("assignments"),
+        "node_kinds": snap.get("node_kinds"),
+        "dispatch_waves": snap.get("dispatch_waves"),
+        "takeovers": snap.get("takeovers"),
+        "deliverables": list(expect.get("deliverables") or []),
+        "workspace_dir": str(workspace),
+        "cards_dir": str(cards_dir),
+        "memory_db": str(memory_db),
+        "traces_path": str(traces_dst if traces_dst.is_file() else traces_src),
+        "session_dir": snap.get("session_dir"),
+        "thread_id": session.thread_id,
+        "error": error,
+    }
+    write_json(run_dir / "result.json", result)
+    print(
+        f"[eval {run_id}] verdict={result['internal_verdict']} "
+        f"external={result['external_passed']} archive={archive_result}",
+        flush=True,
+    )
+    return result
+
+
+async def run_all(out_root: Path) -> list[dict[str, Any]]:
+    out_root.mkdir(parents=True, exist_ok=True)
+    results: list[dict[str, Any]] = []
+    for accumulate, variant in PAIRS:
+        expect = load_expect(accumulate)
+        pair = str(expect.get("pair") or accumulate)
+        print(f"\n===== pair {pair}: {accumulate} accumulate =====", flush=True)
+        results.append(
+            await run_one(
+                case=accumulate,
+                memory="accumulate",
+                out_root=out_root,
+                pair=pair,
+                archive=True,
+            )
+        )
+        print(f"===== pair {pair}: {variant} warm =====", flush=True)
+        results.append(
+            await run_one(
+                case=variant,
+                memory="warm",
+                out_root=out_root,
+                pair=pair,
+                archive=False,
+            )
+        )
+        print(f"===== pair {pair}: {variant} cold =====", flush=True)
+        results.append(
+            await run_one(
+                case=variant,
+                memory="cold",
+                out_root=out_root,
+                pair=pair,
+                archive=False,
+            )
+        )
+    write_json(out_root / "manifest.json", {"runs": [r["run_id"] for r in results]})
+    return results
+
+
+def main() -> None:
+    parser = argparse.ArgumentParser(description="labHandler eval runner")
+    parser.add_argument("--all", action="store_true", help="跑 3 对 × (A + warm + cold)")
+    parser.add_argument("--case", help="单个 case 目录名")
+    parser.add_argument(
+        "--memory",
+        choices=("accumulate", "warm", "cold"),
+        default="accumulate",
+    )
+    parser.add_argument("--pair", help="卡池名，默认取 expect.yaml 的 pair")
+    parser.add_argument("--out", help="输出根目录，默认 eval/runs/<timestamp>")
+    parser.add_argument(
+        "--archive",
+        action="store_true",
+        help="单 case 时把 knowledge_cards 写入当前卡池",
+    )
+    args = parser.parse_args()
+    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "runs" / time.strftime(
+        "%Y%m%d_%H%M%S"
+    )
+    out = out.resolve()
+    if args.all:
+        asyncio.run(run_all(out))
+        return
+    if not args.case:
+        parser.error("需要 --all 或 --case")
+    expect = load_expect(args.case)
+    pair = args.pair or str(expect.get("pair") or args.case)
+    archive = bool(args.archive or args.memory == "accumulate")
+    asyncio.run(
+        run_one(
+            case=args.case,
+            memory=args.memory,
+            out_root=out,
+            pair=pair,
+            archive=archive,
+        )
+    )
+
+
+if __name__ == "__main__":
+    main()
diff --git a/eval/report.py b/eval/report.py
new file mode 100644
--- /dev/null
+++ b/eval/report.py
@@ -0,0 +1,740 @@
+#!/usr/bin/env python3
+"""聚合 eval/runs/<ts>/*/result.json 与 traces.jsonl，写出 docs 下的表。"""
+from __future__ import annotations
+
+import argparse
+import json
+import re
+import statistics
+from collections import Counter, defaultdict
+from pathlib import Path
+from typing import Any
+
+REPO = Path(__file__).resolve().parent.parent
+RULE_IDS = ("screenshot", "blockquote", "filename")
+INTERNAL_STATES = ("pass", "fail", "no_hard_criteria", "test_invalid")
+JUDGE_DECISIONS = ("continue", "finish", "takeover", "revise_spec", "stop")
+MAX_STEPS = 12
+FLASH_TEST = re.compile(r"test_[A-Za-z0-9_]+\.py|写.{0,8}测试|write.{0,16}test", re.I)
+SCREENSHOT = re.compile(r"（此处建议附.+截图）")
+WORD_TOKEN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]")
+NCR_HINTS = ("no_hard_criteria", "无硬指标", "没有硬指标", "无硬编码", "未写门禁", "没有门禁", "未写出验收")
+INV_HINTS = ("test_invalid", "无法执行", "未能执行", "不能执行", "没跑起来", "未能在沙箱")
+
+# 外部门禁「没跑起来」的标志。pytest 判定失败时 exit_code 是 1 且 log 里有用例名；
+# 沙箱超时/终止给的是 exit_code -1 且 output 为空。把后者算进质量指标会把基建
+# 问题记成交付质量差（第一版评测就是这么把 minikv 冷跑误判成 fail 的）。
+GATE_INFRA_MARKERS = (
+    '"status":"terminated"',
+    "[sandbox_unreachable]",
+    "[timeout",
+    "internalerror>",
+)
+
+
+def gate_outcome(row: dict[str, Any]) -> str:
+    """外部门禁三态：pass / fail / infra。
+
+    infra 表示门禁没跑起来（超时、沙箱不可达、pytest 自身崩），既不算通过也不算
+    失败——和 runtime/lab/accept.py 把这类情况映射成 test_invalid 是同一个道理。
+    """
+    if bool(row.get("external_passed")):
+        return "pass"
+    log = str(row.get("external_log") or "")
+    exit_code = row.get("external_exit_code")
+    lowered = log.lower()
+    if any(m in lowered for m in GATE_INFRA_MARKERS):
+        return "infra"
+    # exit_code 非 1 且日志里没有任何 pytest 判定痕迹 → 没跑起来
+    if exit_code != 1 and "failed" not in lowered and "passed" not in lowered:
+        return "infra"
+    return "fail"
+
+
+def load_results(runs_dir: Path) -> list[dict[str, Any]]:
+    rows: list[dict[str, Any]] = []
+    for path in sorted(runs_dir.glob("*/result.json")):
+        rows.append(json.loads(path.read_text(encoding="utf-8")))
+    return rows
+
+
+def counted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
+    return [r for r in rows if r.get("counted")]
+
+
+def _prefetch_blob(row: dict[str, Any]) -> str:
+    cards = row.get("cards_prefetch") or []
+    return "\n---\n".join(str(c) for c in cards)
+
+
+def _norm(text: str) -> str:
+    """归一化：去空白与常见标点，让「只派一个产品里程碑」能匹配「只派一个里程碑」之外的写法差异。"""
+    return re.sub(r"[\s，。、；：,.;:()（）「」“”\"'`]+", "", text)
+
+
+def _needle_terms(needle: str) -> list[str]:
+    """把 needle 切成词组：空格分隔的多词按 AND 匹配，单词整体匹配。
+
+    卡片正文每次 accumulate 都重新生成，死字符串必然失配（第一版 recall@3=0.40
+    全部是这个原因，语义其实命中）。改成词组 AND 后对改写鲁棒。
+    """
+    parts = [p for p in re.split(r"[\s+]+", needle.strip()) if p]
+    return parts or [needle.strip()]
+
+
+def _retrieved(row: dict[str, Any], needle: str) -> bool:
+    """needle 的所有词组是否都出现在预取卡片正文里（归一化后子串匹配）。"""
+    if not needle.strip():
+        return False
+    blob = _norm(_prefetch_blob(row))
+    return all(_norm(term) in blob for term in _needle_terms(needle))
+
+
+def eval_check(check: str, row: dict[str, Any]) -> bool:
+    """跑一条卡片断言。无法识别的 check 抛错，不静默判 False。
+
+    静默 False 会把 expect.yaml 里的拼写错误伪装成「断言没通过」，读表的人看不出
+    区别。断言是这套评测里唯一直接量化「卡片是否真被用上」的指标，不能有哑失败。
+    """
+    text = check.strip()
+    m = re.fullmatch(r"spec\.milestones_len (==|>=|<=|>|<) (\d+)", text)
+    if m:
+        op, want = m.group(1), int(m.group(2))
+        got = int(row.get("milestones_len") or 0)
+        return {
+            "==": got == want,
+            ">=": got >= want,
+            "<=": got <= want,
+            ">": got > want,
+            "<": got < want,
+        }[op]
+    if text == "no_flash_writes_tests":
+        for a in row.get("assignments") or []:
+            arts = " ".join(str(x) for x in (a.get("expected_artifacts") or []))
+            blob = f"{a.get('goal', '')} {a.get('spec', '')} {arts}"
+            if FLASH_TEST.search(blob):
+                return False
+        return True
+    if text == "single_dispatch_wave":
+        ids = [a.get("id") for a in row.get("assignments") or []]
+        return len(set(map(str, ids))) <= 1
+    m = re.fullmatch(r"dispatch_waves (==|>=|<=|>|<) (\d+)", text)
+    if m:
+        op, want = m.group(1), int(m.group(2))
+        got = int(row.get("dispatch_waves") or 0)
+        return {
+            "==": got == want,
+            ">=": got >= want,
+            "<=": got <= want,
+            ">": got > want,
+            "<": got < want,
+        }[op]
+    if text == "gate_ran":
+        # 内部门禁真的跑起来了（不是 test_invalid / 没写门禁）。
+        return str(row.get("internal_verdict") or "") not in {"test_invalid", "no_hard_criteria"}
+    raise ValueError(f"unknown card assertion check: {check!r}")
+
+
+def honesty(rows: list[dict[str, Any]]) -> dict[str, Any]:
+    matrix = {s: {"pass": 0, "fail": 0, "infra": 0} for s in INTERNAL_STATES}
+    other = 0
+    external_pass = 0
+    false_pass = 0
+    abstain = 0
+    invalid = 0
+    infra = 0
+    disclose_need = 0
+    disclose_ok = 0
+    per: list[dict[str, Any]] = []
+    for r in rows:
+        internal = str(r.get("internal_verdict") or "")
+        outcome = gate_outcome(r)
+        if internal in matrix:
+            matrix[internal][outcome] += 1
+        else:
+            other += 1
+        if outcome == "infra":
+            infra += 1
+        if outcome == "pass":
+            external_pass += 1
+        if internal == "pass" and outcome == "fail":
+            false_pass += 1
+        if internal == "no_hard_criteria":
+            abstain += 1
+        if internal == "test_invalid":
+            invalid += 1
+        summary = str(r.get("summary") or "")
+        disclosed: bool | None = None
+        if internal == "no_hard_criteria":
+            disclose_need += 1
+            disclosed = any(h in summary for h in NCR_HINTS)
+            disclose_ok += int(disclosed)
+        elif internal == "test_invalid":
+            disclose_need += 1
+            disclosed = any(h in summary for h in INV_HINTS)
+            disclose_ok += int(disclosed)
+        per.append(
+            {
+                "run_id": r.get("run_id"),
+                "internal": internal,
+                "external": outcome,
+                "summary_disclosed": disclosed,
+                "error": r.get("error"),
+            }
+        )
+    # 质量类比率的分母排除 infra：门禁没跑起来时它对交付质量没有发言权。
+    scored = len(rows) - infra
+    return {
+        "n": len(rows),
+        "scored_n": scored,
+        "infra_count": infra,
+        "matrix": matrix,
+        "other_internal": other,
+        "external_pass_rate": _rate(external_pass, scored),
+        "false_pass_rate": _rate(false_pass, scored),
+        "abstain_rate": _rate(abstain, len(rows)),
+        "test_invalid_count": invalid,
+        "summary_honesty_rate": _rate(disclose_ok, disclose_need) if disclose_need else None,
+        "summary_honesty_n": disclose_need,
+        "per_run": per,
+        "counts": {
+            "external_pass": external_pass,
+            "false_pass": false_pass,
+            "abstain": abstain,
+            "infra": infra,
+        },
+    }
+
+
+def memory_effect(rows: list[dict[str, Any]]) -> dict[str, Any]:
+    """冷热配对。
+
+    ΔM（外部通过率之差）是二值、天花板低的指标：两侧通常都过，测不出记忆的作用。
+    记忆真正的效果在成本——热跑少走弯路。所以同时报 token / 步数 / LLM 调用的
+    冷热比，这才是主指标。
+    """
+    by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
+    for r in rows:
+        if r.get("memory") in {"warm", "cold"}:
+            by_pair[str(r.get("pair"))][str(r.get("memory"))] = r
+    pairs: list[dict[str, Any]] = []
+    deltas: list[float] = []
+    assert_hit = 0
+    assert_n = 0
+    assert_skipped = 0
+    assert_discriminating = 0
+    recall_hit = 0
+    recall_n = 0
+    token_ratios: list[float] = []
+    for pair, sides in sorted(by_pair.items()):
+        warm = sides.get("warm")
+        cold = sides.get("cold")
+        warm_out = gate_outcome(warm) if warm else None
+        cold_out = gate_outcome(cold) if cold else None
+        m_warm = int(warm_out == "pass") if warm_out and warm_out != "infra" else None
+        m_cold = int(cold_out == "pass") if cold_out and cold_out != "infra" else None
+        delta = None
+        if m_warm is not None and m_cold is not None:
+            delta = m_warm - m_cold
+            deltas.append(float(delta))
+
+        eff_warm = efficiency_one(warm) if warm else {}
+        eff_cold = efficiency_one(cold) if cold else {}
+        tw = int(eff_warm.get("tokens_in") or 0)
+        tc = int(eff_cold.get("tokens_in") or 0)
+        ratio = (tc / tw) if tw else None
+        if ratio is not None:
+            token_ratios.append(ratio)
+
+        card_rows: list[dict[str, Any]] = []
+        if warm:
+            for needle in warm.get("cards_expected") or []:
+                hit = _retrieved(warm, str(needle))
+                recall_n += 1
+                recall_hit += int(hit)
+                card_rows.append({"needle": needle, "retrieved": hit})
+            for item in warm.get("card_assertions") or []:
+                if not isinstance(item, dict):
+                    continue
+                match = item.get("match") or {}
+                contains = str(match.get("contains") or "")
+                retrieved = _retrieved(warm, contains) if contains else False
+                if not retrieved:
+                    # 卡片没被检索到，断言无从谈起。计入 skipped 单独报出来，
+                    # 不能让它悄悄离开分母——那会把 1/1 这种空洞的 100% 印成结论。
+                    assert_skipped += 1
+                    card_rows.append(
+                        {
+                            "check": item.get("check"),
+                            "retrieved": False,
+                            "passed": None,
+                            "skipped": "not_retrieved",
+                        }
+                    )
+                    continue
+                assert_n += 1
+                check = str(item.get("check") or "")
+                ok = eval_check(check, warm)
+                assert_hit += int(ok)
+                # 冷跑对照：同一条断言在没有卡片时是什么结果。热过冷也过 =
+                # 这个行为跟卡片无关（题面或 gate 本来就要求），断言通过不能算
+                # 记忆的功劳。只有热过冷不过才是卡片起了作用。
+                cold_ok = eval_check(check, cold) if cold else None
+                discriminates = bool(ok) and cold_ok is False
+                if discriminates:
+                    assert_discriminating += 1
+                card_rows.append(
+                    {
+                        "check": item.get("check"),
+                        "retrieved": True,
+                        "passed": ok,
+                        "cold_passed": cold_ok,
+                        "discriminates": discriminates,
+                    }
+                )
+        pairs.append(
+            {
+                "pair": pair,
+                "M_warm": m_warm,
+                "M_cold": m_cold,
+                "warm_outcome": warm_out,
+                "cold_outcome": cold_out,
+                "delta": delta,
+                "tokens_warm": tw,
+                "tokens_cold": tc,
+                "token_ratio": ratio,
+                "steps_warm": eff_warm.get("steps"),
+                "steps_cold": eff_cold.get("steps"),
+                "llm_calls_warm": eff_warm.get("llm_calls"),
+                "llm_calls_cold": eff_cold.get("llm_calls"),
+                "cards": card_rows,
+                "warm_error": (warm or {}).get("error"),
+                "cold_error": (cold or {}).get("error"),
+            }
+        )
+    return {
+        "pairs": pairs,
+        "mean_delta": statistics.mean(deltas) if deltas else None,
+        "mean_token_ratio": statistics.mean(token_ratios) if token_ratios else None,
+        "card_assertion_rate": _rate(assert_hit, assert_n) if assert_n else None,
+        "card_assertion_n": assert_n,
+        "card_assertion_skipped": assert_skipped,
+        "card_assertion_discriminating": _rate(assert_discriminating, assert_n)
+        if assert_n
+        else None,
+        "recall_at_3": _rate(recall_hit, recall_n) if recall_n else None,
+        "recall_n": recall_n,
+    }
+
+
+def _workspace_text(ws: Path) -> str:
+    chunks: list[str] = []
+    if not ws.is_dir():
+        return ""
+    for p in ws.rglob("*"):
+        if not p.is_file():
+            continue
+        if "_eval_gate" in p.parts or ".labhandler" in p.parts:
+            continue
+        if p.suffix.lower() not in {".md", ".py", ".txt"}:
+            continue
+        try:
+            chunks.append(p.read_text(encoding="utf-8"))
+        except OSError:
+            continue
+    return "\n".join(chunks)
+
+
+def exec_rule(rid: str, row: dict[str, Any]) -> bool | None:
+    ws = Path(str(row.get("workspace_dir") or ""))
+    text = _workspace_text(ws)
+    if rid == "screenshot":
+        return bool(SCREENSHOT.search(text))
+    if rid == "blockquote":
+        return any(line.lstrip().startswith(">") for line in text.splitlines())
+    if rid == "filename":
+        if not ws.is_dir():
+            return False
+        banned = (ws / "solution.py").is_file() or (ws / "main.py").is_file()
+        deliverables = [str(d) for d in (row.get("deliverables") or [])]
+        present = all((ws / d).is_file() for d in deliverables) if deliverables else False
+        return present and not banned
+    return None
+
+
+def remember_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
+    tp = fp = tn = fn = 0
+    exec_ok = 0
+    exec_n = 0
+    cells: list[dict[str, Any]] = []
+    for r in rows:
+        gold = r.get("remember_gold") or {}
+        pred = r.get("remember_pred") or {}
+        for rid in RULE_IDS:
+            g = gold.get(rid)
+            p = pred.get(rid)
+            label = "missing"
+            if g is True and p is True:
+                tp += 1
+                label = "tp"
+            elif g is False and p is True:
+                fp += 1
+                label = "fp"
+            elif g is False and p is False:
+                tn += 1
+                label = "tn"
+            elif g is True and p is False:
+                fn += 1
+                label = "fn"
+            obeyed: bool | None = None
+            if g is True:
+                exec_n += 1
+                obeyed = bool(exec_rule(rid, r))
+                exec_ok += int(obeyed)
+            cells.append(
+                {
+                    "run_id": r.get("run_id"),
+                    "rule": rid,
+                    "gold": g,
+                    "pred": p,
+                    "label": label,
+                    "executed": obeyed,
+                }
+            )
+    pos = tp + fn
+    pred_pos = tp + fp
+    return {
+        "tp": tp,
+        "fp": fp,
+        "tn": tn,
+        "fn": fn,
+        "n": tp + fp + tn + fn,
+        "recall": _rate(tp, pos) if pos else None,
+        "precision": _rate(tp, pred_pos) if pred_pos else None,
+        "accuracy": _rate(tp + tn, tp + fp + tn + fn),
+        "execution_rate": _rate(exec_ok, exec_n) if exec_n else None,
+        "execution_n": exec_n,
+        "cells": cells,
+    }
+
+
+def load_traces(path: Path) -> list[dict[str, Any]]:
+    if not path.is_file():
+        return []
+    rows: list[dict[str, Any]] = []
+    for line in path.read_text(encoding="utf-8").splitlines():
+        line = line.strip()
+        if not line:
+            continue
+        try:
+            rows.append(json.loads(line))
+        except json.JSONDecodeError:
+            continue
+    return rows
+
+
+def _iter_named(records: list[dict[str, Any]]):
+    for rec in records:
+        if rec.get("type") == "event":
+            yield rec.get("name"), rec.get("attrs") or rec, rec
+        if rec.get("type") == "span":
+            yield rec.get("name"), rec.get("attrs") or {}, rec
+            for ev in rec.get("events") or []:
+                if isinstance(ev, dict):
+                    yield ev.get("name"), ev, rec
+
+
+def efficiency_one(row: dict[str, Any]) -> dict[str, Any]:
+    """单跑效率。
+
+    token 只能从 labhandler.turn 上的本地估算取：当前 provider（grok-4.6-high-fast）
+    的流式响应不回 usage，所以 labhandler.llm 上 gen_ai.usage.* 恒为 0。turn span 的
+    ATTR_TOKENS_IN 是装配层自己算的输入量（runtime/loop/cycle.py 里 ctx.input_tokens），
+    每轮一条，累加即为本次 run 的输入总量。provider 哪天开始回 usage，llm span 上
+    的真实值会优先生效。
+    """
+    records = load_traces(Path(str(row.get("traces_path") or "")))
+    api_in = api_out = 0
+    est_in = 0
+    llm_calls = 0
+    steps = 0
+    compact_n = 0
+    ratio_parts: list[float] = []
+    events = Counter()
+    decisions = Counter()
+    for name, attrs, rec in _iter_named(records):
+        is_span = rec.get("type") == "span" and rec.get("name") == name
+        if is_span and name == "labhandler.llm":
+            llm_calls += 1
+            api_in += int(attrs.get("gen_ai.usage.input_tokens") or 0)
+            api_out += int(attrs.get("gen_ai.usage.output_tokens") or 0)
+        if is_span and name == "labhandler.turn":
+            est_in += int(attrs.get("gen_ai.usage.input_tokens") or 0)
+        if is_span and name == "labhandler.step":
+            steps += 1
+        if is_span and name == "labhandler.compact":
+            compact_n += 1
+            before = attrs.get("labhandler.context.tokens_before")
+            after = attrs.get("labhandler.context.tokens_after")
+            if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before:
+                ratio_parts.append(float(after) / float(before))
+        if name == "labhandler.handoff":
+            events["handoff"] += 1
+        if name == "labhandler.stagnation":
+            events["stagnation"] += 1
+        if name == "labhandler.retry":
+            events["retry"] += 1
+        if name == "labhandler.decision":
+            kind = str(attrs.get("labhandler.decision") or "")
+            if kind in JUDGE_DECISIONS:
+                decisions[kind] += 1
+            elif kind:
+                decisions["other"] += 1
+    return {
+        "run_id": row.get("run_id"),
+        "tokens_in": api_in or est_in,
+        "tokens_in_source": "api" if api_in else ("local_estimate" if est_in else "none"),
+        "tokens_out": api_out,
+        "llm_calls": llm_calls,
+        "steps": steps,
+        "hit_max_steps": steps >= MAX_STEPS,
+        "handoff": events["handoff"],
+        "stagnation": events["stagnation"],
+        "retry": events["retry"],
+        "compact_n": compact_n,
+        "compact_ratio_mean": statistics.mean(ratio_parts) if ratio_parts else None,
+        "decisions": dict(decisions),
+    }
+
+
+def efficiency(rows: list[dict[str, Any]]) -> dict[str, Any]:
+    per = [efficiency_one(r) for r in rows]
+    n = len(per) or 1
+
+    def avg(key: str) -> float:
+        return sum(float(p.get(key) or 0) for p in per) / n
+
+    decisions = Counter()
+    for p in per:
+        decisions.update(p.get("decisions") or {})
+    ratios = [p["compact_ratio_mean"] for p in per if p.get("compact_ratio_mean") is not None]
+    sources = Counter(p.get("tokens_in_source") for p in per)
+    return {
+        "per_run": per,
+        "mean_tokens_in": avg("tokens_in"),
+        "mean_tokens_out": avg("tokens_out"),
+        "mean_llm_calls": avg("llm_calls"),
+        "tokens_in_sources": dict(sources),
+        "mean_steps": avg("steps"),
+        "hit_max_steps_rate": _rate(sum(int(p["hit_max_steps"]) for p in per), len(per)),
+        "handoff_rate": _rate(sum(int(p["handoff"] > 0) for p in per), len(per)),
+        "stagnation_rate": _rate(sum(int(p["stagnation"] > 0) for p in per), len(per)),
+        "retry_rate": _rate(sum(int(p["retry"] > 0) for p in per), len(per)),
+        "mean_compact_n": avg("compact_n"),
+        "mean_compact_ratio": statistics.mean(ratios) if ratios else None,
+        "decisions": dict(decisions),
+    }
+
+
+def _rate(num: int, den: int) -> dict[str, Any]:
+    return {"num": num, "den": den, "value": (num / den) if den else None}
+
+
+def _fmt_rate(rate: dict[str, Any] | None) -> str:
+    if not rate or rate.get("den") in (None, 0) or rate.get("value") is None:
+        return "n/a"
+    return f"{rate['num']}/{rate['den']} ({rate['value']:.2f})"
+
+
+def render_md(agg: dict[str, Any]) -> str:
+    h = agg["honesty"]
+    m = agg["memory"]
+    r = agg["remember"]
+    e = agg["efficiency"]
+    lines = [
+        "# labHandler 最小评测",
+        "",
+        "白盒差分，不是横向 benchmark。外部通过率只作锚点，不拿来刷分。",
+        "",
+        "**读数注意**：n=6（冷跑另 3 次），每 case 只跑 1 次，没有裸模型 baseline，",
+        "case 材料由本人编写。所以这里的比率只能用于「同一改动前后对比」，",
+        "不能当作系统能力的绝对分。",
+        "",
+        f"runs 目录：`{agg.get('runs_dir', '')}`",
+        "",
+        "## 1. 门禁诚实度（A + variant 热，n=" + str(h["n"]) + "）",
+        "",
+        "外部 infra = 门禁没跑起来（超时/沙箱不可达），不计入质量分母。",
+        "",
+        "| 内部 \\ 外部 | pass | fail | infra |",
+        "|---|---:|---:|---:|",
+    ]
+    for state in INTERNAL_STATES:
+        cell = h["matrix"][state]
+        lines.append(f"| {state} | {cell['pass']} | {cell['fail']} | {cell['infra']} |")
+    lines += [
+        "",
+        f"- 计分 run 数（排除 infra）：{h['scored_n']}/{h['n']}",
+        f"- 外部通过率：{_fmt_rate(h['external_pass_rate'])}",
+        f"- 假通过率（内部 pass ∧ 外部 fail）：{_fmt_rate(h['false_pass_rate'])}",
+        f"- 弃权率（no_hard_criteria）：{_fmt_rate(h['abstain_rate'])}",
+        f"- test_invalid 次数（不计入惩罚）：{h['test_invalid_count']}",
+        f"- 门禁 infra 次数：{h['infra_count']}",
+        f"- 汇报真实性：{_fmt_rate(h['summary_honesty_rate']) if h['summary_honesty_rate'] else 'n/a（没有 no_hard_criteria/test_invalid）'}",
+        "",
+        "| run | 内部 | 外部 | SUMMARY 点名 |",
+        "|---|---|---|---|",
+    ]
+    for row in h["per_run"]:
+        disc = row["summary_disclosed"]
+        disc_s = "—" if disc is None else ("yes" if disc else "no")
+        lines.append(
+            f"| {row['run_id']} | {row['internal']} | {row['external']} | {disc_s} |"
+        )
+    lines += ["", "## 2. 记忆效果（variant 冷热配对）", ""]
+    lines += [
+        "主指标是成本比（冷/热）：记忆的作用是少走弯路，不是提高上限。",
+        "ΔM 是二值通过率之差，天花板低，两侧通常都过。",
+        "",
+    ]
+    if m["mean_delta"] is None:
+        lines.append("- ΔM 均值：n/a")
+    else:
+        lines.append(f"- ΔM 均值（M_warm − M_cold）：{m['mean_delta']:+.2f}")
+    if m["mean_token_ratio"] is None:
+        lines.append("- 输入 token 冷/热比均值：n/a")
+    else:
+        lines.append(f"- 输入 token 冷/热比均值：{m['mean_token_ratio']:.2f}×")
+    skipped = m.get("card_assertion_skipped") or 0
+    disc = m.get("card_assertion_discriminating")
+    lines += [
+        f"- 卡片断言命中率：{_fmt_rate(m['card_assertion_rate']) if m['card_assertion_rate'] else 'n/a'}"
+        + (f"（另有 {skipped} 条因卡片未被检索而跳过）" if skipped else ""),
+        f"- 其中有判别力（热过冷不过）：{_fmt_rate(disc) if disc else 'n/a'}"
+        "　← 只有这些能归因到卡片；热冷都过说明题面或 gate 本来就要求",
+        f"- recall@3：{_fmt_rate(m['recall_at_3']) if m['recall_at_3'] else 'n/a'}",
+        "",
+        "| pair | M_warm | M_cold | ΔM | token 热 | token 冷 | 冷/热 | 步数 热/冷 | LLM 调用 热/冷 |",
+        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
+    ]
+    for p in m["pairs"]:
+        dw = "n/a" if p["M_warm"] is None else str(p["M_warm"])
+        dc = "n/a" if p["M_cold"] is None else str(p["M_cold"])
+        dd = "n/a" if p["delta"] is None else f"{p['delta']:+d}"
+        ratio = "n/a" if p["token_ratio"] is None else f"{p['token_ratio']:.2f}×"
+        lines.append(
+            f"| {p['pair']} | {dw} | {dc} | {dd} | {p['tokens_warm']} | {p['tokens_cold']} | "
+            f"{ratio} | {p['steps_warm']}/{p['steps_cold']} | "
+            f"{p['llm_calls_warm']}/{p['llm_calls_cold']} |"
+        )
+    lines += [
+        "",
+        "### 卡片断言明细",
+        "",
+        "| pair | check | 检索到 | 热 | 冷 | 有判别力 |",
+        "|---|---|---|---|---|---|",
+    ]
+    for p in m["pairs"]:
+        for c in p["cards"]:
+            if "check" not in c:
+                continue
+            got = "yes" if c.get("retrieved") else "no"
+            passed = c.get("passed")
+            ps = "skipped" if passed is None else ("yes" if passed else "no")
+            cold_ok = c.get("cold_passed")
+            cs = "n/a" if cold_ok is None else ("yes" if cold_ok else "no")
+            ds = "yes" if c.get("discriminates") else "no"
+            lines.append(f"| {p['pair']} | {c.get('check')} | {got} | {ps} | {cs} | {ds} |")
+    lines += ["", "## 3. /remember（3 规则 × counted case）", ""]
+    lines += [
+        f"- confusion：TP={r['tp']} FP={r['fp']} TN={r['tn']} FN={r['fn']}",
+        f"- recall（主看）：{_fmt_rate(r['recall']) if r['recall'] else 'n/a'}",
+        f"- precision：{_fmt_rate(r['precision']) if r['precision'] else 'n/a'}",
+        f"- accuracy（易被 TN 撑高）：{_fmt_rate(r['accuracy'])}",
+        f"- 执行率（金标适用 → 正则遵守）：{_fmt_rate(r['execution_rate']) if r['execution_rate'] else 'n/a'}",
+        "",
+        "| run | 规则 | gold | pred | 格 | 执行 |",
+        "|---|---|---|---|---|---|",
+    ]
+    for c in r["cells"]:
+        exe = "—" if c["executed"] is None else ("yes" if c["executed"] else "no")
+        lines.append(
+            f"| {c['run_id']} | {c['rule']} | {c['gold']} | {c['pred']} | {c['label']} | {exe} |"
+        )
+    lines += ["", "## 4. 效率（counted runs）", ""]
+    lines += [
+        f"- 均输入 token：{e['mean_tokens_in']:.0f}（来源：{e['tokens_in_sources']}）",
+        f"- 均输出 token：{e['mean_tokens_out']:.0f}"
+        + ("（provider 未回 usage，输出量无数据）" if not e["mean_tokens_out"] else ""),
+        f"- 均 LLM 调用次数：{e['mean_llm_calls']:.1f}",
+        f"- 均步数：{e['mean_steps']:.2f}",
+        f"- 撞 _MAX_STEPS=12：{_fmt_rate(e['hit_max_steps_rate'])}",
+        f"- 接管率（至少一次 handoff）：{_fmt_rate(e['handoff_rate'])}",
+        f"- 停滞率：{_fmt_rate(e['stagnation_rate'])}",
+        f"- 重试率：{_fmt_rate(e['retry_rate'])}",
+        f"- 均压缩次数：{e['mean_compact_n']:.2f}",
+        f"- 均压缩比 after/before：{e['mean_compact_ratio']:.3f}"
+        if e["mean_compact_ratio"] is not None
+        else "- 均压缩比 after/before：n/a",
+        f"- Judge 决策计数：{e['decisions']}",
+        "",
+        "| run | in | out | llm | steps | max12 | handoff | stag | retry | compact |",
+        "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|",
+    ]
+    for p in e["per_run"]:
+        lines.append(
+            f"| {p['run_id']} | {p['tokens_in']} | {p['tokens_out']} | {p['llm_calls']} | "
+            f"{p['steps']} | {'yes' if p['hit_max_steps'] else 'no'} | {p['handoff']} | "
+            f"{p['stagnation']} | {p['retry']} | {p['compact_n']} |"
+        )
+    lines += ["", "## 未计入的 run", ""]
+    skipped_runs = [x for x in agg.get("all_runs", []) if not x.get("counted")]
+    if not skipped_runs:
+        lines.append("无。")
+    else:
+        lines.append("| run | memory | 内部 | 外部 | error |")
+        lines.append("|---|---|---|---|---|")
+        for x in skipped_runs:
+            lines.append(
+                f"| {x.get('run_id')} | {x.get('memory')} | {x.get('internal_verdict')} | "
+                f"{gate_outcome(x)} | {x.get('error') or ''} |"
+            )
+    lines.append("")
+    return "\n".join(lines)
+
+
+def aggregate(runs_dir: Path) -> dict[str, Any]:
+    rows = load_results(runs_dir)
+    primary = counted(rows)
+    return {
+        "runs_dir": str(runs_dir),
+        "all_runs": rows,
+        "honesty": honesty(primary),
+        "memory": memory_effect(rows),
+        "remember": remember_metrics(primary),
+        "efficiency": efficiency(primary),
+    }
+
+
+def main() -> None:
+    parser = argparse.ArgumentParser(description="labHandler eval reporter")
+    parser.add_argument("--runs", required=True, help="eval/runs/<timestamp> 目录")
+    parser.add_argument("--out", default=str(REPO / "docs" / "eval_report.md"))
+    parser.add_argument("--json", default=str(REPO / "docs" / "eval_results.json"))
+    args = parser.parse_args()
+    runs_dir = Path(args.runs).resolve()
+    agg = aggregate(runs_dir)
+    out_md = Path(args.out)
+    out_json = Path(args.json)
+    out_md.parent.mkdir(parents=True, exist_ok=True)
+    out_json.parent.mkdir(parents=True, exist_ok=True)
+    slim = {k: v for k, v in agg.items() if k != "all_runs"}
+    slim["run_ids"] = [r.get("run_id") for r in agg["all_runs"]]
+    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
+    out_md.write_text(render_md(agg), encoding="utf-8")
+    print(f"wrote {out_md}")
+    print(f"wrote {out_json}")
+
+
+if __name__ == "__main__":
+    main()
diff --git a/eval/regate.py b/eval/regate.py
new file mode 100644
--- /dev/null
+++ b/eval/regate.py
@@ -0,0 +1,166 @@
+#!/usr/bin/env python3
+"""只重跑外部门禁，不重跑 lab。
+
+用途：门禁本身写错（第一版 two_sum_variant 的 test_negatives 期望值算错）或门禁
+超时被记成质量失败（minikv_variant 冷跑 exit_code=-1）时，交付物是好的，没必要
+再烧一遍 lab。本脚本按 result.json 里的 workspace 重跑 gate，就地更新
+external_passed / external_exit_code / external_log，并留一条 regate 记录。
+"""
+from __future__ import annotations
+
+import argparse
+import asyncio
+import json
+import shutil
+import sys
+import time
+from pathlib import Path
+from typing import Any
+
+REPO = Path(__file__).resolve().parent.parent
+if str(REPO) not in sys.path:
+    sys.path.insert(0, str(REPO))
+
+CASES_DIR = Path(__file__).resolve().parent / "cases"
+
+
+async def _pytest_in_sandbox(target: str, timeout: float) -> tuple[int, str]:
+    """在沙箱里跑一次 gate。
+
+    两件事要处理：
+    1. recreate_sandbox 只轮询端口，端口通了 MCP 的 streamable_http 端点还可能没起来，
+       首次 initialize 会 ReadError。重试并在每次之间重置客户端缓存。
+    2. 重建后的容器没装 pytest。runtime/lab/accept.py 对内部门禁做了同样的兜底安装，
+       外部门禁不装就会把"没跑起来"记成 fail。
+    """
+    from mcp_client import reset_mcp_client
+    from tools.sandbox_tools import sandbox_run
+
+    command = f"cd /workspace && PYTHONPATH=/workspace python -m pytest -q {target}"
+    last = (-1, "")
+    for attempt in range(5):
+        try:
+            code, log = await sandbox_run(command, timeout=timeout)
+            lowered = (log or "").lower()
+            if "no module named pytest" in lowered:
+                install, ilog = await sandbox_run(
+                    "python -m pip install -q pytest", timeout=timeout
+                )
+                if install != 0:
+                    return code, f"{log}\n[regate] pip install pytest 失败：{ilog}"
+                code, log = await sandbox_run(command, timeout=timeout)
+                lowered = (log or "").lower()
+            if "[sandbox_unreachable]" not in lowered:
+                return code, log
+            last = (code, log or "")
+        except Exception as exc:  # noqa: BLE001 - 首轮 MCP 未就绪
+            last = (-1, f"{type(exc).__name__}: {exc}")
+        reset_mcp_client()
+        await asyncio.sleep(3 * (attempt + 1))
+    return last
+
+
+async def rerun_one(result_path: Path, timeout: float) -> dict[str, Any]:
+    from tools.sandbox_tools import sandbox_workspace_path
+
+    row = json.loads(result_path.read_text(encoding="utf-8"))
+    case = str(row.get("case") or "")
+    workspace = Path(str(row.get("workspace_dir") or ""))
+    if not workspace.is_dir():
+        return {"run_id": row.get("run_id"), "skipped": "missing_workspace"}
+
+    dest = workspace / "_eval_gate"
+    if dest.exists():
+        shutil.rmtree(dest)
+    shutil.copytree(CASES_DIR / case / "gate", dest)
+
+    target = sandbox_workspace_path(dest)
+    code, log = await _pytest_in_sandbox(target, timeout)
+    before = {
+        "external_passed": row.get("external_passed"),
+        "external_exit_code": row.get("external_exit_code"),
+    }
+    row["external_passed"] = code == 0
+    row["external_exit_code"] = int(code)
+    row["external_log"] = (log or "")[-4000:]
+    history = row.get("regate") or []
+    history.append(
+        {
+            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
+            "before": before,
+            "after": {"external_passed": row["external_passed"], "external_exit_code": int(code)},
+        }
+    )
+    row["regate"] = history
+    result_path.write_text(
+        json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
+    )
+    return {
+        "run_id": row.get("run_id"),
+        "before": before["external_passed"],
+        "after": row["external_passed"],
+        "exit_code": int(code),
+    }
+
+
+async def main_async(runs_dir: Path, only: list[str], recreate: bool) -> None:
+    """一个进程只处理一个 run。
+
+    沙箱挂载的是 WORKSPACE_DIR，每个 run 的 workspace 不同，所以必须换环境变量再
+    重建容器。但 mcp_client 缓存的 session 绑在旧容器上，同进程内反复重建会让
+    streamable_http 的 cancel scope 跨 task 退出而炸。所以循环交给外层 shell，
+    本函数只做一个。
+
+    --no-recreate 用于容器已经挂在目标 workspace 上的情况：跳过重建，直接跑。
+    重建后 MCP 端点要几秒才真正可用，第一次 initialize 常常 ReadError。
+    """
+    import os
+
+    from config.runtime import get_settings
+    from infra.sandbox_boot import ensure_sandbox, recreate_sandbox
+
+    paths = sorted(runs_dir.glob("*/result.json"))
+    targets = [p for p in paths if not only or p.parent.name in only]
+    if not targets:
+        print("[regate] no matching run", flush=True)
+        return
+    if len(targets) > 1:
+        names = " ".join(p.parent.name for p in targets)
+        print(f"[regate] 一次只能跑一个 run，请分别执行：{names}", flush=True)
+        return
+
+    path = targets[0]
+    ws = json.loads(path.read_text(encoding="utf-8")).get("workspace_dir")
+    os.environ["WORKSPACE_DIR"] = str(Path(str(ws)).resolve())
+    get_settings.cache_clear()
+    import tools.policy as policy_mod
+
+    policy_mod._policy = None
+    policy_mod._auditor = None
+    if recreate:
+        recreate_sandbox(log=lambda m: None)
+        # 端口通了不等于 MCP 端点可用，给它一点时间。
+        await asyncio.sleep(5)
+    else:
+        ensure_sandbox(log=lambda m: None)
+    out = await rerun_one(path, timeout=get_settings().tool_timeout_s)
+    print(f"[regate] {json.dumps(out, ensure_ascii=False)}", flush=True)
+
+
+def main() -> None:
+    parser = argparse.ArgumentParser(description="只重跑 eval 外部门禁")
+    parser.add_argument("--runs", required=True)
+    parser.add_argument("--only", nargs="*", default=[], help="限定 run_id（一次一个）")
+    parser.add_argument(
+        "--no-recreate",
+        action="store_true",
+        help="容器已挂在目标 workspace 上时跳过重建（避免 MCP session 失效）",
+    )
+    args = parser.parse_args()
+    asyncio.run(
+        main_async(Path(args.runs).resolve(), args.only, recreate=not args.no_recreate)
+    )
+
+
+if __name__ == "__main__":
+    main()
diff --git a/eval/selftest_report.py b/eval/selftest_report.py
new file mode 100644
--- /dev/null
+++ b/eval/selftest_report.py
@@ -0,0 +1,143 @@
+#!/usr/bin/env python3
+"""eval/report.py 的自检。
+
+第一版评测的四个错数字全部出在这一层（门禁 infra 被当质量失败、needle 死字符串、
+断言静默跳过、token 读错 span），所以聚合逻辑本身要有断言兜着。
+
+跑法：python3 eval/selftest_report.py
+"""
+from __future__ import annotations
+
+import sys
+from pathlib import Path
+
+sys.path.insert(0, str(Path(__file__).resolve().parent))
+
+import report  # noqa: E402
+
+
+def check(label: str, got: object, want: object) -> bool:
+    ok = got == want
+    print(f"{'ok  ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
+    return ok
+
+
+def main() -> int:
+    fails = 0
+
+    # --- gate_outcome：超时/终止不能算 fail ---------------------------------
+    fails += not check(
+        "gate_outcome 超时 → infra",
+        report.gate_outcome(
+            {"external_passed": False, "external_exit_code": -1, "external_log": '{"status":"terminated","output":"","exit_code":-1}'}
+        ),
+        "infra",
+    )
+    fails += not check(
+        "gate_outcome 真判定失败 → fail",
+        report.gate_outcome(
+            {
+                "external_passed": False,
+                "external_exit_code": 1,
+                "external_log": "1 failed, 4 passed in 0.03s",
+            }
+        ),
+        "fail",
+    )
+    fails += not check(
+        "gate_outcome 通过 → pass",
+        report.gate_outcome({"external_passed": True, "external_exit_code": 0, "external_log": "5 passed"}),
+        "pass",
+    )
+    fails += not check(
+        "gate_outcome 沙箱不可达 → infra",
+        report.gate_outcome(
+            {"external_passed": False, "external_exit_code": -1, "external_log": "[SANDBOX_UNREACHABLE] ..."}
+        ),
+        "infra",
+    )
+
+    # --- needle：对卡片改写鲁棒 --------------------------------------------
+    row = {"cards_prefetch": ["2.md score=0.5\n单函数作业只派一个里程碑、一次 dispatch。"]}
+    fails += not check("needle 词组 AND 命中改写后的卡片", report._retrieved(row, "只派 一个里程碑"), True)
+    fails += not check("needle 不该命中无关内容", report._retrieved(row, "八节 实验报告"), False)
+    fails += not check("空 needle 不算命中", report._retrieved(row, ""), False)
+
+    # --- eval_check：未知 check 必须抛，不能静默 False ----------------------
+    try:
+        report.eval_check("no_such_check", {})
+        print("FAIL 未知 check 应该抛 ValueError，却静默返回了")
+        fails += 1
+    except ValueError:
+        print("ok   未知 check 抛 ValueError")
+
+    fails += not check(
+        "dispatch_waves <= 2 在 waves=1 时通过",
+        report.eval_check("dispatch_waves <= 2", {"dispatch_waves": 1}),
+        True,
+    )
+    fails += not check(
+        "dispatch_waves <= 2 在 waves=3 时不通过",
+        report.eval_check("dispatch_waves <= 2", {"dispatch_waves": 3}),
+        False,
+    )
+    fails += not check(
+        "gate_ran 在 no_hard_criteria 时为假",
+        report.eval_check("gate_ran", {"internal_verdict": "no_hard_criteria"}),
+        False,
+    )
+
+    # --- honesty：infra 不进质量分母 ---------------------------------------
+    h = report.honesty(
+        [
+            {"run_id": "a", "internal_verdict": "pass", "external_passed": True, "external_exit_code": 0, "external_log": "1 passed", "summary": ""},
+            {"run_id": "b", "internal_verdict": "pass", "external_passed": False, "external_exit_code": -1, "external_log": '{"status":"terminated"}', "summary": ""},
+        ]
+    )
+    fails += not check("infra 计数", h["infra_count"], 1)
+    fails += not check("计分分母排除 infra", h["external_pass_rate"]["den"], 1)
+    fails += not check("infra 不算假通过", h["false_pass_rate"]["num"], 0)
+
+    # --- memory_effect：断言跳过不能悄悄缩小分母 ---------------------------
+    warm = {
+        "pair": "p",
+        "memory": "warm",
+        "external_passed": True,
+        "external_exit_code": 0,
+        "external_log": "1 passed",
+        "cards_prefetch": ["1.md\n完全无关的正文"],
+        "cards_expected": ["某个不存在的处方"],
+        "card_assertions": [{"match": {"contains": "某个不存在的处方"}, "check": "dispatch_waves <= 2"}],
+        "dispatch_waves": 1,
+        "traces_path": "",
+    }
+    cold = dict(warm, memory="cold", dispatch_waves=5)
+    m = report.memory_effect([warm, cold])
+    fails += not check("卡片没检索到 → 断言分母为 0", m["card_assertion_n"], 0)
+    fails += not check("卡片没检索到 → 记一条 skipped", m["card_assertion_skipped"], 1)
+    fails += not check("卡片没检索到 → 命中率 n/a 而非 100%", m["card_assertion_rate"], None)
+    fails += not check("recall 记 0 命中", m["recall_at_3"]["num"], 0)
+
+    # --- memory_effect：热过冷不过才算有判别力 -----------------------------
+    warm2 = dict(warm, cards_prefetch=["1.md\n单函数作业只派一个里程碑"], cards_expected=["只派 一个里程碑"],
+                 card_assertions=[{"match": {"contains": "只派 一个里程碑"}, "check": "dispatch_waves <= 2"}])
+    cold2 = dict(warm2, memory="cold", dispatch_waves=5)
+    m2 = report.memory_effect([warm2, cold2])
+    fails += not check("断言进入分母", m2["card_assertion_n"], 1)
+    fails += not check("热过冷不过 → 有判别力", m2["card_assertion_discriminating"]["num"], 1)
+
+    warm3 = dict(warm2)
+    cold3 = dict(warm2, memory="cold")  # 冷跑同样通过
+    m3 = report.memory_effect([warm3, cold3])
+    fails += not check("热冷都过 → 无判别力", m3["card_assertion_discriminating"]["num"], 0)
+
+    print()
+    if fails:
+        print(f"{fails} 项失败")
+        return 1
+    print("全部通过")
+    return 0
+
+
+if __name__ == "__main__":
+    raise SystemExit(main())
diff --git a/eval/cases/two_sum/expect.yaml b/eval/cases/two_sum/expect.yaml
new file mode 100644
--- /dev/null
+++ b/eval/cases/two_sum/expect.yaml
@@ -0,0 +1,11 @@
+pair: two_sum
+role: accumulate
+question: 完成工作区中的两数之和编程作业。只交指定的 Python 源文件。
+deliverables:
+  - two_sum.py
+remember:
+  screenshot: false
+  blockquote: false
+  filename: true
+cards_expected: []
+card_assertions: []
diff --git a/eval/cases/two_sum/materials/TASK.md b/eval/cases/two_sum/materials/TASK.md
new file mode 100644
--- /dev/null
+++ b/eval/cases/two_sum/materials/TASK.md
@@ -0,0 +1,24 @@
+# 作业：两数之和
+
+只交一个 Python 源文件，文件名必须是 `two_sum.py`。不要交 `solution.py` 或 `main.py`。
+这不是实验课，不需要实验报告，不需要截图。
+
+## 函数
+
+```python
+def two_sum(nums: list[int], target: int) -> list[int]:
+    """返回两个下标 i、j，满足 nums[i] + nums[j] == target 且 i != j。"""
+```
+
+## 约定
+
+- `nums` 无序。
+- 下标从 0 开始。返回的两个下标顺序不限。
+- 保证至多一对解。若无解，返回空列表 `[]`。
+- 允许出现重复值，例如 `[3, 3], target=6` → `[0, 1]`。
+- 禁止使用暴力双重循环（O(n²)）。请使用一次扫描的哈希表。
+- 不要在作业目录里提交 `test_*.py`；如需自测，由你本地完成，不作为提交物。
+
+## 样例
+
+输入 `nums = [2, 7, 11, 15], target = 9`，返回 `[0, 1]`（或 `[1, 0]`）。
diff --git a/eval/cases/two_sum/gate/test_gate.py b/eval/cases/two_sum/gate/test_gate.py
new file mode 100644
--- /dev/null
+++ b/eval/cases/two_sum/gate/test_gate.py
@@ -0,0 +1,24 @@
+"""外部门禁：agent 跑 lab 时看不见本文件。"""
+from __future__ import annotations
+
+from two_sum import two_sum
+
+
+def test_example():
+    assert sorted(two_sum([2, 7, 11, 15], 9)) == [0, 1]
+
+
+def test_duplicates():
+    assert sorted(two_sum([3, 3], 6)) == [0, 1]
+
+
+def test_later_pair():
+    assert sorted(two_sum([3, 2, 4], 6)) == [1, 2]
+
+
+def test_no_solution():
+    assert two_sum([1, 2, 3], 100) == []
+
+
+def test_negatives():
+    assert sorted(two_sum([-1, -2, -3, -4, -5], -8)) == [2, 4]
diff --git a/eval/cases/two_sum_variant/expect.yaml b/eval/cases/two_sum_variant/expect.yaml
new file mode 100644
--- /dev/null
+++ b/eval/cases/two_sum_variant/expect.yaml
@@ -0,0 +1,23 @@
+pair: two_sum
+role: measure
+question: 完成工作区中的有序两数之和编程作业。只交指定的 Python 源文件。
+deliverables:
+  - two_sum_sorted.py
+remember:
+  screenshot: false
+  blockquote: false
+  filename: true
+# needle 按词组 AND 匹配，对卡片改写鲁棒（卡片每次 accumulate 重新生成，
+# 死字符串必然失配）。
+cards_expected:
+  - 只派 一个里程碑
+  - 不要把测试派给 Flash
+card_assertions:
+  # 冷热都只写 1 个里程碑，所以 milestones_len 没有判别力，不用它。
+  # 有判别力的是「派了几次才做完」：热跑 2 次派发，冷跑 5 次。
+  - match:
+      contains: 只派 一个里程碑
+    check: dispatch_waves <= 2
+  - match:
+      contains: 不要把测试派给 Flash
+    check: no_flash_writes_tests
diff --git a/eval/cases/two_sum_variant/materials/TASK.md b/eval/cases/two_sum_variant/materials/TASK.md
new file mode 100644
--- /dev/null
+++ b/eval/cases/two_sum_variant/materials/TASK.md
@@ -0,0 +1,24 @@
+# 作业：有序数组的两数之和
+
+只交一个 Python 源文件，文件名必须是 `two_sum_sorted.py`。不要交 `solution.py` 或 `main.py`。
+这不是实验课，不需要实验报告，不需要截图。
+
+## 函数
+
+```python
+def two_sum_sorted(numbers: list[int], target: int) -> list[int]:
+    """numbers 已按非降序排好。返回两个下标，使对应元素之和为 target。"""
+```
+
+## 约定
+
+- 下标从 **1** 开始（不是 0）。
+- 恰好存在一对解；`i != j`。若实现上遇到无解，返回 `[]`。
+- 返回的两个下标按升序排列，例如 `[1, 2]`。
+- 数组已排序，请使用双指针，不要再套一遍哈希表作业的写法充数。
+- 不要提交 `test_*.py`。
+
+## 样例
+
+输入 `numbers = [2, 7, 11, 15], target = 9`，返回 `[1, 2]`。
+输入 `numbers = [2, 3, 4], target = 6`，返回 `[1, 3]`。
diff --git a/eval/cases/two_sum_variant/gate/test_gate.py b/eval/cases/two_sum_variant/gate/test_gate.py
new file mode 100644
--- /dev/null
+++ b/eval/cases/two_sum_variant/gate/test_gate.py
@@ -0,0 +1,25 @@
+"""外部门禁：agent 跑 lab 时看不见本文件。"""
+from __future__ import annotations
+
+from two_sum_sorted import two_sum_sorted
+
+
+def test_example():
+    assert two_sum_sorted([2, 7, 11, 15], 9) == [1, 2]
+
+
+def test_ends():
+    assert two_sum_sorted([2, 3, 4], 6) == [1, 3]
+
+
+def test_duplicates():
+    assert two_sum_sorted([1, 1, 3], 2) == [1, 2]
+
+
+def test_no_solution():
+    assert two_sum_sorted([1, 2, 3], 100) == []
+
+
+def test_negatives():
+    # [-4, -1, 0, 5] 里和为 1 的唯一一对是 -4 + 5，1-based 下标 [1, 4]。
+    assert two_sum_sorted([-4, -1, 0, 5], 1) == [1, 4]
diff --git a/eval/cases/minikv_lab05/expect.yaml b/eval/cases/minikv_lab05/expect.yaml
new file mode 100644
--- /dev/null
+++ b/eval/cases/minikv_lab05/expect.yaml
@@ -0,0 +1,12 @@
+pair: minikv
+role: accumulate
+question: 按工作区实验指导书完成 MiniKV 实验：实现指定模块并撰写八节实验报告。
+deliverables:
+  - minikv.py
+  - 实验报告.md
+remember:
+  screenshot: true
+  blockquote: true
+  filename: false
+cards_expected: []
+card_assertions: []
diff --git a/eval/cases/minikv_lab05/materials/GUIDE.md b/eval/cases/minikv_lab05/materials/GUIDE.md
new file mode 100644
--- /dev/null
+++ b/eval/cases/minikv_lab05/materials/GUIDE.md
@@ -0,0 +1,62 @@
+# 实验五 MiniKV 指导书
+
+本实验是操作系统课程的实验课作业。提交物包括可运行代码和一份实验报告。
+
+## 实验目的
+
+实现一个进程内的迷你键值存储 MiniKV，理解「内存表 + 快照落盘」的基本结构。
+
+## 实验环境
+
+Linux，Python 3.11。不需要 Docker 集群。
+
+## 任务
+
+实现文件 `minikv.py`，类名与方法签名如下（不得改名）：
+
+```python
+class MiniKV:
+    def __init__(self) -> None: ...
+    def set(self, key: str, value: str) -> None: ...
+    def get(self, key: str) -> str | None: ...
+    def delete(self, key: str) -> bool: ...
+    def keys(self) -> list[str]: ...
+    def save(self, path: str) -> None: ...
+    def load(self, path: str) -> None: ...
+```
+
+约定：
+
+- `get` 缺失键返回 `None`。
+- `delete` 删除成功返回 `True`，键不存在返回 `False`。
+- `keys` 返回当前所有键，顺序不限。
+- `save` / `load` 使用 JSON 文件，值为字符串。
+
+## 实验步骤
+
+1. 实现内存表。
+2. 用若干 `set/get/delete` 验证正确性，记录命令与输出。
+3. `save` 后再 `load` 到新实例，核对键值一致。
+4. 把过程写入实验报告。
+
+## 实验结果要求
+
+报告必须包含以下八节，标题需出现对应关键词：
+
+1. 实验目的
+2. 实验原理
+3. 实验环境
+4. 实验步骤
+5. 实验结果
+6. 结果分析与讨论
+7. 结论
+8. 思考题
+
+思考题：若进程在 `save` 中途崩溃，可能出现什么不一致？你的实现如何面对？
+
+## 提交
+
+- 代码：`minikv.py`
+- 报告：`实验报告.md`
+- 无法截图时，必须使用占位句「（此处建议附 XX 截图）」，禁止假装已经贴了真实截图。
+- 引用本指导书原文时使用 markdown 引用块（行首 `>`）。
diff --git a/eval/cases/minikv_lab05/gate/test_gate.py b/eval/cases/minikv_lab05/gate/test_gate.py
new file mode 100644
--- /dev/null
+++ b/eval/cases/minikv_lab05/gate/test_gate.py
@@ -0,0 +1,82 @@
+"""外部门禁：报告结构 + MiniKV 冒烟。agent 跑 lab 时看不见。"""
+from __future__ import annotations
+
+import json
+import os
+import re
+from pathlib import Path
+
+ROOT = Path(__file__).resolve().parents[1]
+SECTIONS = [
+    "实验目的",
+    "实验原理",
+    "实验环境",
+    "实验步骤",
+    "实验结果",
+    "结果分析",
+    "结论",
+    "思考题",
+]
+PLACEHOLDER = re.compile(r"（此处建议附.+截图）")
+
+
+def _report_text() -> str:
+    candidates = [
+        ROOT / "实验报告.md",
+        ROOT / "lab_report.md",
+        ROOT / "REPORT.md",
+        ROOT / "report.md",
+    ]
+    for p in candidates:
+        if p.is_file():
+            return p.read_text(encoding="utf-8")
+    md = [
+        p
+        for p in ROOT.glob("*.md")
+        if p.name not in {"SUMMARY.md", "CATALOG.md", "TASK.md", "GUIDE.md"}
+        and not str(p).startswith(str(ROOT / "_eval_gate"))
+    ]
+    assert md, "missing lab report markdown"
+    return md[0].read_text(encoding="utf-8")
+
+
+def test_report_has_eight_sections():
+    text = _report_text()
+    missing = [s for s in SECTIONS if s not in text]
+    assert not missing, f"missing sections: {missing}"
+
+
+def test_screenshot_placeholder():
+    text = _report_text()
+    assert PLACEHOLDER.search(text), "expected screenshot placeholder （此处建议附 XX 截图）"
+
+
+def test_blockquote_quote():
+    text = _report_text()
+    assert any(line.lstrip().startswith(">") for line in text.splitlines()), (
+        "expected a markdown blockquote quoting the lab guide"
+    )
+
+
+def test_minikv_roundtrip(tmp_path):
+    import sys
+
+    sys.path.insert(0, str(ROOT))
+    from minikv import MiniKV
+
+    kv = MiniKV()
+    kv.set("a", "1")
+    kv.set("b", "2")
+    assert kv.get("a") == "1"
+    assert kv.delete("a") is True
+    assert kv.get("a") is None
+    assert kv.delete("missing") is False
+    path = tmp_path / "snap.json"
+    kv.save(str(path))
+    other = MiniKV()
+    other.load(str(path))
+    assert other.get("b") == "2"
+    assert "b" in other.keys()
+    data = json.loads(path.read_text(encoding="utf-8"))
+    assert isinstance(data, dict)
+    os.unlink(path)
diff --git a/eval/cases/minikv_variant/expect.yaml b/eval/cases/minikv_variant/expect.yaml
new file mode 100644
--- /dev/null
+++ b/eval/cases/minikv_variant/expect.yaml
@@ -0,0 +1,25 @@
+pair: minikv
+role: measure
+question: 按工作区实验指导书完成 MiniKV-TTL 实验：实现带过期时间的模块并撰写八节实验报告。
+deliverables:
+  - minikv.py
+  - 实验报告.md
+remember:
+  screenshot: true
+  blockquote: true
+  filename: false
+cards_expected:
+  - 八节 实验目的
+  - 截图 （此处建议附
+card_assertions:
+  # 原来这里是 spec.milestones_len >= 1，恒真——任何非空 SPEC 都过，等于没测。
+  # 报告的八节/截图/引用块由 /remember 规则和 gate 保证，冷热完全一致，也没有
+  # 判别力。真正的差别在派发轮数：热跑 3 轮，冷跑 4 轮，所以阈值取 3。
+  - match:
+      contains: 八节 实验目的
+    check: dispatch_waves <= 3
+  # 4.md 讲的是「门禁里的 import 要放进测试函数内，否则收集期 ModuleNotFoundError
+  # 会被记成 test_invalid」。这条卡直接对应内部门禁能否跑起来。
+  - match:
+      contains: 收集期 ModuleNotFoundError
+    check: gate_ran
diff --git a/eval/cases/minikv_variant/materials/GUIDE.md b/eval/cases/minikv_variant/materials/GUIDE.md
new file mode 100644
--- /dev/null
+++ b/eval/cases/minikv_variant/materials/GUIDE.md
@@ -0,0 +1,53 @@
+# 实验六 MiniKV 带 TTL 指导书
+
+本实验是操作系统课程的实验课作业，在实验五内存表基础上增加按键过期。提交物包括代码和实验报告。
+
+## 实验目的
+
+为 MiniKV 增加 `expire` / `ttl`，观察过期键在读取与落盘时的行为。
+
+## 实验环境
+
+Linux，Python 3.11。
+
+## 任务
+
+实现文件 `minikv.py`：
+
+```python
+class MiniKV:
+    def __init__(self) -> None: ...
+    def set(self, key: str, value: str) -> None: ...
+    def get(self, key: str) -> str | None: ...
+    def delete(self, key: str) -> bool: ...
+    def keys(self) -> list[str]: ...
+    def expire(self, key: str, ttl_s: float) -> bool: ...
+    def ttl(self, key: str) -> float | None: ...
+    def save(self, path: str) -> None: ...
+    def load(self, path: str) -> None: ...
+```
+
+约定：
+
+- `expire`：键存在则设置从现在起 `ttl_s` 秒后过期，返回 `True`；键不存在返回 `False`。
+- `ttl`：未设置过期返回 `None`；已过期或不存在返回 `None`；否则返回剩余秒数（允许浮点误差 0.05）。
+- `get` / `keys` 不得返回已过期的键。过期后 `get` 为 `None`。
+- `save` 不要把已过期的键写进 JSON。
+
+## 实验步骤
+
+1. 实现 TTL。
+2. `set` 后 `expire(key, 0.2)`，睡眠 0.3 秒，确认 `get` 为 `None`。
+3. 记录命令、输出，写入八节实验报告。
+
+## 报告
+
+必须含：实验目的、实验原理、实验环境、实验步骤、实验结果、结果分析与讨论、结论、思考题。
+
+思考题：过期检查放在 `get` 时惰性删除，与后台线程主动删除各有什么代价？
+
+## 提交
+
+- `minikv.py` 与 `实验报告.md`
+- 截图占位必须写成「（此处建议附 XX 截图）」
+- 引用本指导书用 markdown 引用块（行首 `>`）
diff --git a/eval/cases/minikv_variant/gate/test_gate.py b/eval/cases/minikv_variant/gate/test_gate.py
new file mode 100644
--- /dev/null
+++ b/eval/cases/minikv_variant/gate/test_gate.py
@@ -0,0 +1,69 @@
+"""外部门禁：TTL MiniKV + 八节报告。agent 跑 lab 时看不见。"""
+from __future__ import annotations
+
+import re
+import time
+from pathlib import Path
+
+ROOT = Path(__file__).resolve().parents[1]
+SECTIONS = [
+    "实验目的",
+    "实验原理",
+    "实验环境",
+    "实验步骤",
+    "实验结果",
+    "结果分析",
+    "结论",
+    "思考题",
+]
+PLACEHOLDER = re.compile(r"（此处建议附.+截图）")
+
+
+def _report_text() -> str:
+    for name in ("实验报告.md", "lab_report.md", "REPORT.md", "report.md"):
+        p = ROOT / name
+        if p.is_file():
+            return p.read_text(encoding="utf-8")
+    md = [
+        p
+        for p in ROOT.glob("*.md")
+        if p.name not in {"SUMMARY.md", "CATALOG.md", "TASK.md", "GUIDE.md"}
+        and "_eval_gate" not in p.parts
+    ]
+    assert md, "missing lab report markdown"
+    return md[0].read_text(encoding="utf-8")
+
+
+def test_report_has_eight_sections():
+    text = _report_text()
+    missing = [s for s in SECTIONS if s not in text]
+    assert not missing, f"missing sections: {missing}"
+
+
+def test_screenshot_placeholder():
+    assert PLACEHOLDER.search(_report_text())
+
+
+def test_blockquote_quote():
+    text = _report_text()
+    assert any(line.lstrip().startswith(">") for line in text.splitlines())
+
+
+def test_expire_hides_key(tmp_path):
+    import sys
+
+    sys.path.insert(0, str(ROOT))
+    from minikv import MiniKV
+
+    kv = MiniKV()
+    kv.set("k", "v")
+    assert kv.expire("k", 0.15) is True
+    assert kv.expire("missing", 1.0) is False
+    time.sleep(0.25)
+    assert kv.get("k") is None
+    assert "k" not in kv.keys()
+    path = tmp_path / "snap.json"
+    kv.save(str(path))
+    other = MiniKV()
+    other.load(str(path))
+    assert other.get("k") is None
diff --git a/eval/cases/essay_a/expect.yaml b/eval/cases/essay_a/expect.yaml
new file mode 100644
--- /dev/null
+++ b/eval/cases/essay_a/expect.yaml
@@ -0,0 +1,11 @@
+pair: essay
+role: accumulate
+question: 按工作区题目写一篇议论文，只交指定的 markdown 作文。
+deliverables:
+  - essay.md
+remember:
+  screenshot: false
+  blockquote: false
+  filename: false
+cards_expected: []
+card_assertions: []
diff --git a/eval/cases/essay_a/materials/PROMPT.md b/eval/cases/essay_a/materials/PROMPT.md
new file mode 100644
--- /dev/null
+++ b/eval/cases/essay_a/materials/PROMPT.md
@@ -0,0 +1,14 @@
+# 写作作业
+
+写一篇议论文，题目：**为什么大学编程作业应该自带验收测试**。
+
+这不是实验报告，不要写实验目的/步骤/结果，不要放截图占位。不要提交 Python 源文件。
+
+## 要求
+
+- 文件名必须是 `essay.md`。
+- 正文字数 800–1200 字（按汉字与字母数字计数，可用中文）。
+- 必须出现以下四个小节标题（可用 `##`）：引言、论点、反方、结论。
+- 立场：支持「作业应附带机器可跑的验收测试」。
+- 不要编造不存在的论文或 DOI。没有可引用文献时在文末写「无外部文献」。
+- 文末加一段不超过 80 字的工具使用说明。
diff --git a/eval/cases/essay_a/gate/test_gate.py b/eval/cases/essay_a/gate/test_gate.py
new file mode 100644
--- /dev/null
+++ b/eval/cases/essay_a/gate/test_gate.py
@@ -0,0 +1,31 @@
+"""外部门禁：字数与小节。不判文采。agent 跑 lab 时看不见。"""
+from __future__ import annotations
+
+import re
+from pathlib import Path
+
+ROOT = Path(__file__).resolve().parents[1]
+SECTIONS = ["引言", "论点", "反方", "结论"]
+WORD_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]")
+
+
+def _essay() -> str:
+    p = ROOT / "essay.md"
+    assert p.is_file(), "missing essay.md"
+    return p.read_text(encoding="utf-8")
+
+
+def test_no_banned_filenames():
+    for name in ("solution.py", "main.py"):
+        assert not (ROOT / name).exists(), f"unexpected {name}"
+
+
+def test_required_sections():
+    text = _essay()
+    missing = [s for s in SECTIONS if s not in text]
+    assert not missing, f"missing sections: {missing}"
+
+
+def test_word_count():
+    n = len(WORD_RE.findall(_essay()))
+    assert 800 <= n <= 1200, f"word count {n} not in [800, 1200]"
diff --git a/eval/cases/essay_variant/expect.yaml b/eval/cases/essay_variant/expect.yaml
new file mode 100644
--- /dev/null
+++ b/eval/cases/essay_variant/expect.yaml
@@ -0,0 +1,19 @@
+pair: essay
+role: measure
+question: 按工作区题目写一篇议论文，只交指定的 markdown 作文。
+deliverables:
+  - essay.md
+remember:
+  screenshot: false
+  blockquote: false
+  filename: false
+cards_expected:
+  - 议论作文 essay.md
+  - 全文模板 重派
+card_assertions:
+  # essay 的小节/字数/「无外部文献」都写在题面里、由 gate 校验，冷热都会做到，
+  # 不能拿来证明卡片起了作用。2.md 的处方是「Flash 连续 logic_exhausted 时按同一
+  # id 重派并给出全文模板」——它的效果就是少派几轮：热跑 2 次，冷跑 6 次。
+  - match:
+      contains: 全文模板 重派
+    check: dispatch_waves <= 2
diff --git a/eval/cases/essay_variant/materials/PROMPT.md b/eval/cases/essay_variant/materials/PROMPT.md
new file mode 100644
--- /dev/null
+++ b/eval/cases/essay_variant/materials/PROMPT.md
@@ -0,0 +1,14 @@
+# 写作作业
+
+写一篇议论文，题目：**AI 代写实验报告的边界在哪里**。
+
+这不是实验课提交的实验报告本身，不要按八节实验报告来写，不要放截图占位。不要提交 Python 源文件。
+
+## 要求
+
+- 文件名必须是 `essay.md`。
+- 正文字数 800–1200 字（按汉字与字母数字计数）。
+- 必须出现以下四个小节标题（可用 `##`）：引言、论点、反方、结论。
+- 立场：承认工具可以起草，但过程数据与截图必须来自真实实验。
+- 不要编造不存在的论文或 DOI。没有可引用文献时在文末写「无外部文献」。
+- 文末加一段不超过 80 字的工具使用说明。
diff --git a/eval/cases/essay_variant/gate/test_gate.py b/eval/cases/essay_variant/gate/test_gate.py
new file mode 100644
--- /dev/null
+++ b/eval/cases/essay_variant/gate/test_gate.py
@@ -0,0 +1,31 @@
+"""外部门禁：字数与小节。不判文采。agent 跑 lab 时看不见。"""
+from __future__ import annotations
+
+import re
+from pathlib import Path
+
+ROOT = Path(__file__).resolve().parents[1]
+SECTIONS = ["引言", "论点", "反方", "结论"]
+WORD_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]")
+
+
+def _essay() -> str:
+    p = ROOT / "essay.md"
+    assert p.is_file(), "missing essay.md"
+    return p.read_text(encoding="utf-8")
+
+
+def test_no_banned_filenames():
+    for name in ("solution.py", "main.py"):
+        assert not (ROOT / name).exists(), f"unexpected {name}"
+
+
+def test_required_sections():
+    text = _essay()
+    missing = [s for s in SECTIONS if s not in text]
+    assert not missing, f"missing sections: {missing}"
+
+
+def test_word_count():
+    n = len(WORD_RE.findall(_essay()))
+    assert 800 <= n <= 1200, f"word count {n} not in [800, 1200]"

<!-- PATCH_END -->
