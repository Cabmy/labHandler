# labHandler 改动复现包（等同 push）

相对 `f049de6 重构底层harness` 工作区的全部 harness 改动。把本文件里每个「文件」块按路径覆盖/新建，即可在干净 checkout 上复现同一仓库状态。

生成日期：2026-09-16。

## 刻意不包含

- `runtime/cursor_chat.py` 以及 `LLMGateway` 里对 Cursor SDK 的分支。Chat 仍是最初的 **AsyncOpenAI + `chat.completions` 流式**（AgentRouter / 其它 OpenAI 兼容网关）。
- `cursor-sdk` 依赖、`crsr_` / `api.cursor.com` 配置、`CursorAgentError` 分类。
- `.cursor/skills/langfuse/**`（编辑器 skill，不是 labHandler 运行时）。
- `config/.env`（密钥）。用 `.env.example` 复制后自填。
- `workspace/` 跑迹。

本地曾把 Pro/Flash 都改成 `grok-4.6-high-fast` 是为了走 Cursor。本包的 `.env.example` 与 `config/runtime.py` 默认模型已改回 AgentRouter：`glm-5.3` / `deepseek-v4-flash`。

## 怎么落地

把本文件放到仓库根目录（相对 `f049de6`）。提取器只认正文里的 PACKFILE 注释；脚本自身把标记拆开写，不会误匹配。

```bash
git checkout f049de6
# 将 changeset-harness.md 放在仓库根后：
python3 - <<'PY'
from pathlib import Path
text = Path("changeset-harness.md").read_text(encoding="utf-8")
start = text.find("\n## 文件正文\n")
if start < 0:
    raise SystemExit("missing 文件正文")
text = text[start + 1:]
marker = "<" + "!-- PACKFILE:"
i = 0
n = 0
while True:
    a = text.find(marker, i)
    if a < 0:
        break
    b = text.find("-->", a)
    path = text[a + len(marker):b].strip()
    rest = text[b + 3:].lstrip("
")
    line0, _, after = rest.partition("
")
    ticks = "".join(ch for ch in line0 if ch == "`")
    if not ticks.startswith("```"):
        raise SystemExit(f"bad fence for {path}: {line0!r}")
    close = "
" + ticks + "
"
    end = after.find(close)
    if end < 0:
        if after.endswith("
" + ticks):
            body = after[: -(len(ticks) + 1)]
        elif after.endswith(ticks):
            body = after[: -len(ticks)]
        else:
            raise SystemExit(f"unclosed fence for {path}")
    else:
        body = after[:end]
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body.rstrip() + "
", encoding="utf-8")
    print("wrote", path, dest.stat().st_size)
    n += 1
    i = b + 3
print("files", n)
PY
cp config/.env.example config/.env   # 填密钥
pip install -r requirements.txt
```

不要创建 `runtime/cursor_chat.py`。

## 改动在做什么

1. **阶段强制**：`runtime/phase.py` 是「谁执行、看见哪些工具、从哪个 submit 交卷」的唯一出处。规划/判决阶段节点仍是 pro，工具表只给只读 + 点名的 skill 读。
2. **SPEC 指挥**：Pro 先写 SPEC.md；每步只决定下一步，给 Flash 自包含任务书。多 Flash 全部只读。
3. **Skill 渐进加载**：system 只放目录；Pro `load_skill` 绑一个 SOP，全程互斥；Flash 不加载。
4. **上下文**：最近 N 轮原文，更早摘要；tool 卸盘按 `DumpScope(agent, task_id)` 隔离。`MEMORY.md` 常驻，`FORGET.md` 仅 Pro 压缩用。
5. **Judge / Takeover**：每次 Judge 新 `run_loop`；handoff 是 step_goal + briefs，不并 Flash history。Remember-Judge 不并 Pro 主线。
6. **读文件续读**：`read_file` 1-based offset + limit，超 80k 字符截断并给 next offset。
7. **控制面**：LLM `max_retries=0`；瞬态/校验/停滞由 `control.decide` 按 Task 预算处理。一轮 assistant 里多个 `tool_calls` 会按顺序全部执行。
8. **可观测**：Langfuse + `traces.jsonl`。
9. **全库去掉** `from __future__ import annotations`（前向引用改 `Self` / 引号）。
10. **dotenv** 在 `config/runtime.py` 模块加载，避免 `__main__` 先 `ensure_sandbox` 时读不到 key。

## 文件清单
- `README.md` — **覆盖** — 按现行 harness 重写能力说明：SPEC 指挥、阶段强制、Pro 单线程、双 notes、Langfuse。Chat 仍写 AgentRouter，不提 Cursor SDK。
- `requirements.txt` — **覆盖** — 去掉 cursor-sdk。保留 openai / jsonschema / langfuse>=3 / pytest。LLM 仍走 OpenAI 兼容 SDK。
- `config/.env.example` — **覆盖** — 接入格式回到 AgentRouter（v1 + sk- + 白名单头）。补上本次新增旋钮：OUTPUT_RESERVE_TOKENS、压缩参数、Langfuse。不含 crsr_ / api.cursor.com。
- `config/runtime.py` — **覆盖** — 模块加载 config/.env（修 uv 启动时 LLM_API_KEY 未读）。RuntimeSettings 增加 Langfuse / 压缩 / 输出预留。缺项抛 ConfigError，无静默回落。
- `config/prompts.py` — **覆盖** — 阶段 system prompt 与 SPEC/Dispatch/Judge/Takeover/Summary/Remember 对齐；skill 只给目录，由 Pro load_skill。
- `runtime/llm.py` — **覆盖** — 保持最初接入：仅 AsyncOpenAI chat.completions 流式。本层 max_retries=0，瞬态交给 control.decide。增加 ChatResult.truncated 与 aclose。不含 CursorChatBackend。
- `runtime/errors.py` — **覆盖** — 错误分类枚举。去掉 CursorAgentError 特判，分类仍覆盖 OpenAI 超时/429/鉴权。
- `runtime/phase.py` — **新建** — 阶段定义表：谁执行、工具可见范围、唯一 submit 出口、是否并入 Pro transcript。Flash / Remember-Judge 不并线。
- `runtime/spec.py` — **新建** — ProjectSpec + Assignment。一次最多 3 个 Flash；多于 1 个全部只读；domain 互不重叠。
- `runtime/loop.py` — **覆盖** — ReAct 循环：一轮可执行多条 tool_calls（顺序 await）。DumpScope 按 agent/task 隔离卸盘。Flash compact 不碰 FORGET.md。
- `runtime/orchestrator.py` — **覆盖** — 主流程：SPEC → Remember-Judge → Dispatch → Worker → 程序性验收 → Judge；takeover 仅 Judge 裁定。Pro catalog + SkillBind；Flash 不加载 skill。
- `runtime/tools.py` — **覆盖** — 工具注册与权限门。load_skill 仅 Pro。read_file 走 offset/limit。memory_grep 按 DumpScope.extra_roots。
- `runtime/schema_call.py` — **覆盖** — 各阶段结构化出口 schema：submit_spec / dispatch / brief / judge / summary / remember。
- `runtime/accept.py` — **覆盖** — 程序性验收四态 pass/fail/test_invalid/no_hard_criteria，沙箱内跑。
- `runtime/control.py` — **覆盖** — 控制面 decide：瞬态重试、校验、停滞、逻辑耗尽。与 LLM 层解耦。
- `runtime/retry.py` — **覆盖** — 退避从 LLM 层拿掉后，只给仍需要的调用点用。
- `runtime/scheduler.py` — **覆盖** — 并行只读 Flash 调度，上限 MAX_PARALLEL_READONLY_WORKERS。
- `runtime/session.py` — **覆盖** — LabSession 持有 LLMGateway / Tracer / 设置；done 清场重建沙箱。
- `runtime/persist.py` — **覆盖** — 幂等续跑账本：产物指纹完好则跳过。
- `runtime/task.py` — **覆盖** — Task 树、Permission、TaskKind。classmethod 返回 Self（去掉 future annotations）。
- `runtime/stagnation.py` — **覆盖** — 重复 (tool,args,result) 检测；grace 后再 LOOP_CONFIRMED。
- `runtime/remember.py` — **新建** — Remember-Judge：崭新 run_loop，不并入 Pro 主线。
- `runtime/context/__init__.py` — **覆盖** — 导出 assemble / compact / disclosure / budget / notes。
- `runtime/context/assemble.py` — **覆盖** — 装配 system + skill 目录 + notes + 检索 + 历史。阶段指令放 turn 末尾，稳住前缀缓存。
- `runtime/context/compact.py` — **覆盖** — 最近 N 轮原文，更早摘要；tool 正文卸盘。DumpScope：Pro/Flash 路径隔离。仅 Pro 写 FORGET.md。
- `runtime/context/disclosure.py` — **覆盖** — skill_catalog_block 只放 name/description/when_to_use，不内联 SOP。
- `runtime/context/budget.py` — **新建** — 本地字符估计 + 用上一轮 usage.input_tokens 投影。触发压缩阈值。
- `runtime/context/notes.py` — **新建** — MEMORY.md 常驻；FORGET.md 压缩时跳过噪声，压完清空。
- `runtime/observe/spans.py` — **覆盖** — span 名字与属性常量：run/spec/dispatch/step/task/turn/llm/tool/验收。
- `runtime/observe/tracer.py` — **覆盖** — Langfuse + 本地 JSONL 双 sink。
- `runtime/observe/sinks.py` — **新建** — Langfuse / JSONL sink 实现。公钥私钥留空则只写 traces.jsonl。
- `memory/db.py` — **新建** — SQLite 连接与 schema 初始化。归档表与向量表同库。
- `memory/__init__.py` — **覆盖** — 导出 archive / retrieve / vectors / dream / profile。
- `memory/archive.py` — **覆盖** — 卡片入档。
- `memory/retrieve.py` — **覆盖** — memory_grep 支持 extra_roots（DumpScope）。
- `memory/vectors.py` — **覆盖** — 向量索引与 embedding_model 对账重建。
- `memory/dream.py` — **覆盖** — /dream 离线合并淘汰卡片并重建索引。
- `memory/profile.py` — **覆盖** — profile 读写。
- `tools/fs_tools.py` — **覆盖** — read_file：1-based offset、limit 行、80k 字符封顶，截断时给出 next offset。
- `tools/skill_tool.py` — **覆盖** — SkillBind：全程只能绑一个 skill；同名可重读，换名拒绝。gate 约束 reference/script。
- `tools/policy.py` — **覆盖** — host 白名单与路径守护。
- `tools/sandbox_tools.py` — **覆盖** — MCP 沙箱工具。
- `tools/search_tool.py` — **覆盖** — DDG 检索。
- `tools/profile_tool.py` — **覆盖** — 追加规则写入 profile。
- `tools/workspace_utils.py` — **覆盖** — 去掉 from __future__ import annotations。
- `skills/repository.py` — **覆盖** — skill 仓库扫描。去掉 future annotations。
- `skills/editor.py` — **覆盖** — /edit_skill 自然语言改 SOP，diff 确认后落盘。
- `skills/coding/SKILL.md` — **覆盖** — 精简给 Pro 的 coding SOP。
- `skills/coding/references/pitfalls.md` — **覆盖** — 随 SOP 收紧。
- `skills/coding/references/testing.md` — **覆盖** — 随 SOP 收紧。
- `skills/essay/SKILL.md` — **覆盖** — 精简 essay SOP。
- `skills/essay/references/citation.md` — **覆盖** — 随 SOP 收紧。
- `skills/lab_report/SKILL.md` — **覆盖** — 精简 lab_report SOP。
- `mcp_client/client.py` — **覆盖** — 去掉 future annotations。
- `infra/net_probe.py` — **覆盖** — 去掉 future annotations。
- `infra/sandbox_boot.py` — **覆盖** — 去掉 future annotations。
- `server/app.py` — **覆盖** — 去掉重复 load_dotenv（改由 config.runtime 加载）。shutdown 调 llm.aclose。
- `server/__main__.py` — **覆盖** — 去掉 future annotations。
- `server/static/index.html` — **覆盖** — Web 操作面小改（知识治理等）。

---

## 文件正文

### `README.md`

**覆盖。** 按现行 harness 重写能力说明：SPEC 指挥、阶段强制、Pro 单线程、双 notes、Langfuse。Chat 仍写 AgentRouter，不提 Cursor SDK。

<!-- PACKFILE: README.md -->
````markdown
# labHandler

中文大学 Lab 自动化 AI agent。把作业材料丢进 `workspace/`，自建 harness 在沙箱内完成规划、执行、程序性验收与总结，产物和 `SUMMARY.md` 写回 `workspace/`。

## 核心能力

- **SPEC.md 指挥**：Pro 先写 SPEC.md 作为总纲（总目标、全局接口契约、里程碑），
  之后每一步只决定「接下来做什么」，给每个 Flash 单独写一份任务书
- **自顶向下小步推进**：Flash 一次做不完整个项目，每份任务书都是它一步能做完的量；
  做完一步看结果再定下一步
- **派一个还是派多个**：派一个 Flash 时它拿写权限；派多个（最多 3 个）时全部只读，
  各管一个互不重叠的领域，适合并行调研
- **接口契约**：SPEC.md 钉死函数/类/文件的名字与签名，验收代码按这些名字预先写好
- **按需硬编码测试**：有可量化指标且产物可检验时才写测试；没有就如实标注，交 Judge 语义判断
- **程序性验收四态**：`pass` / `fail` / `test_invalid` / `no_hard_criteria`
  （无硬指标不等于通过），在沙箱内执行
- **Runtime Task 控制面**：执行状态、预算、取消、权限在 Task 树上；LLM 只看见语义投影
- **阶段强制**：工具表按「角色 × 阶段」收窄——每个阶段只暴露自己的那一个 submit 工具，
  规划/判决阶段只给只读工具；收窄后的表同时是执行边界，调表外的名字直接被拒
- **Pro 单线程**：SPEC / dispatch / judge / 接管 / 收尾共用一份 Pro transcript，
  阶段指令追加在对话末尾；Flash 每份任务书新开上下文
- **上下文管理**：最近数轮保留原文，更早的由 Flash 摘要一次，tool 正文卸到磁盘；
  压缩后立即重新装配再发请求
- **双 notes**：`MEMORY.md` 常驻上下文、压缩吃不掉，Judge 可追加/改写/删除条目；
  `FORGET.md` 让摘要刻意跳过噪声，压缩完成即清空
- **幂等续跑**：副作用账本记录产物指纹，产物完好的任务直接跳过
- **两层安全边界**：host 白名单 + 路径守护 + 审计；重量操作走 MCP Docker 沙箱
- **可观测**：Langfuse + 本地 JSONL，span 树覆盖 run / spec / dispatch / step / task / turn /
  llm / tool / 验收，含耗时、token、重试、停滞、handoff、压缩、Judge 决策
- **跨 lab 记忆**：卡片文件 + 向量索引最终一致；`/dream` 离线治理
- **会话隔离**：`done` 归档 → 清场 → 重建沙箱 → 新 lab

设计细节与不变量见 [docs/architecture.md](docs/architecture.md)。

## 环境要求

- Python 3.11
- Docker（沙箱容器）
- AgentRouter API Key（Chat）+ Paratera API Key（Embedding）

## 安装

```bash
conda create -n labhandler python=3.11 -y && conda activate labhandler
pip install -r requirements.txt
cp config/.env.example config/.env   # 填 LLM_API_KEY 与 EMBEDDING_API_KEY
```

必填项缺失会在启动时直接报错，不会跑到一半才失败。

`CONTEXT_BUDGET_TOKENS` 请填所用模型的真实上下文窗口（默认 200000）。输入侧可用量 =
该值减去 `OUTPUT_RESERVE_TOKENS`，达到其 `COMPACT_TRIGGER_RATIO` 时触发压缩。
发请求前用本地字符估计；若上一轮接口返回了 `usage.input_tokens`，下一轮用
「估计 × (真实/估计)」对照同一阈值。

接 Langfuse 填 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`，以及
`LANGFUSE_BASE_URL`（EU：`https://cloud.langfuse.com`，US：`https://us.cloud.langfuse.com`）。
区域选错会 401，界面里什么都没有。留空则只写本地 `workspace/.labhandler/traces.jsonl`。

## 测试

```bash
pytest tests/
```

## 启动

```bash
python -m server
```

默认 127.0.0.1:8000。首次启动会拉起 AIO Sandbox 容器（镜像约 2.29GB）。`LAB_AUTOSTART_SANDBOX=false` 可禁用。

## Web 操作

| 动作 | 作用 |
|---|---|
| 上传材料 + 下达任务 | 跑当前 lab |
| 停止 | 取消 in-flight Task，lab 仍在 |
| 归档结束 | 卡片入档 → workspace 进 `.trash/` → 重建沙箱 → 下一个 lab |
| 知识治理 | 离线合并/淘汰卡片并重建向量索引 |
| Skill 编辑 | 自然语言改现有 skill，diff 确认后落盘 |
| 追加规则 | 写入 profile；Remember-Judge 裁定是否适用，步骤 Judge 对照后才能结束 |
````

### `requirements.txt`

**覆盖。** 去掉 cursor-sdk。保留 openai / jsonschema / langfuse>=3 / pytest。LLM 仍走 OpenAI 兼容 SDK。

<!-- PACKFILE: requirements.txt -->
```text
# LLM & Agent
openai>=1.40.0
jsonschema

# MCP
mcp

# Web Search
ddgs
httpx[socks]
beautifulsoup4

# Config / 数据
python-dotenv
PyYAML
numpy

# Web
fastapi
uvicorn
python-multipart
sse-starlette

# Observability
langfuse>=3.0

# 验收测试在沙箱内跑 pytest；本地开发也需要
pytest>=8.0
```

### `config/.env.example`

**覆盖。** 接入格式回到 AgentRouter（v1 + sk- + 白名单头）。补上本次新增旋钮：OUTPUT_RESERVE_TOKENS、压缩参数、Langfuse。不含 crsr_ / api.cursor.com。

<!-- PACKFILE: config/.env.example -->
```dotenv
# labHandler 集中配置模板
# 复制为 config/.env 后填入真实密钥；运行时只读 config/.env。

# ─── Chat（AgentRouter / OpenAI 兼容网关）────────────────
# 最初接入格式：AsyncOpenAI + chat.completions（流式）。
# AgentRouter 示例：https://agentrouter.org/v1 + sk-... + glm-5.3 / deepseek-v4-flash
# 缺白名单头会 401 unauthorized client detected。
LLM_BASE_URL=https://agentrouter.org/v1
LLM_API_KEY=sk-xxx_replace_me
PRO_MODEL=glm-5.3
FLASH_MODEL=deepseek-v4-flash
LLM_DEFAULT_HEADERS={"User-Agent":"claude-cli/1.0.108 (external, cli)","x-app":"cli","anthropic-version":"2023-06-01"}

# ─── Embedding（Paratera）──────────────────────────────────────
# 卡片向量用此端点。embedding_model 写入索引行，与当前值不一致时对账重建该行。
EMBEDDING_BASE_URL=https://llmapi.paratera.com/v1/
EMBEDDING_API_KEY=sk-xxx_replace_me
EMBEDDING_MODEL=GLM-Embedding-3

# ─── AIO Sandbox 容器 ─────────────────────────────────────────
AIO_SANDBOX_IMAGE=enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
AIO_SANDBOX_PORT=8080
AIO_SANDBOX_MCP_URL=http://127.0.0.1:8080/mcp
LAB_AUTOSTART_SANDBOX=true

# ─── 存储路径 ─────────────────────────────────────────────────
# MEMORY_DB_PATH 同时承载归档表与向量表；CARDS_DIR 是卡片 markdown 事实源。
MEMORY_DB_PATH=./.labhandler_data/memory.db
CARDS_DIR=./.labhandler_data/cards
PROFILE_PATH=./profile/me.yaml
SKILLS_DIR=./skills
WORKSPACE_DIR=./workspace

# ─── Harness 预算 ─────────────────────────────────────────────
# PRO/FLASH_STEP_BUDGET：该角色单次 run 的 LLM 步数上限；耗尽则 force_brief。
PRO_STEP_BUDGET=20
FLASH_STEP_BUDGET=30
# 单次 run 墙钟上限（秒）；到期停跑 reason=deadline。
TASK_WALL_TIME_S=900
# 单次工具调用超时（秒）。
TOOL_TIMEOUT_S=120
# control.decide 对瞬态失败的 retry_tool 次数上限；耗尽后 nudge。
TRANSIENT_RETRY_MAX=3
# 同一出口工具连续 payload 校验失败次数；达到后以 degraded 模式再校。
VALIDATION_RETRY_MAX=3
# 连续相同 (tool, args, result) 次数，达到则 REPEAT/nudge。
STAGNATION_REPEAT_THRESHOLD=3
# nudge 后再给的步数；仍重复则 LOOP_CONFIRMED 停跑。
STAGNATION_GRACE_STEPS=2
# 连续逻辑失败上限；达到则停跑 reason=logic_exhausted。
CONSECUTIVE_LOGIC_FAILURE_MAX=3
# 只读并行 Flash 的 semaphore 上限。
MAX_PARALLEL_READONLY_WORKERS=3
# 进程内同时进行的 LLM 请求上限。
LLM_MAX_CONCURRENCY=4

# ─── 上下文窗口 ───────────────────────────────────────────────
# CONTEXT_BUDGET_TOKENS = 所用模型的上下文窗口。
# 输入侧可用量 = CONTEXT_BUDGET_TOKENS - OUTPUT_RESERVE_TOKENS。
# 占用达到可用量 × COMPACT_TRIGGER_RATIO 时压缩；压完立即重新装配再发请求。
# 发前用本地字符估计；上一轮若有 usage.input_tokens，下一轮按真实/估计比例投影后再比阈值。
# OUTPUT_RESERVE_TOKENS 必须小于 CONTEXT_BUDGET_TOKENS（get_settings 时校验）。
CONTEXT_BUDGET_TOKENS=200000
OUTPUT_RESERVE_TOKENS=8192
COMPACT_TRIGGER_RATIO=0.7
# 最近这么多回合保留原文，更早的压成一段纪要。
COMPACT_KEEP_RECENT_TURNS=3

# ─── 可观测（Langfuse）────────────────────────────────────────
# 密钥在项目 Settings → API Keys。LANGFUSE_BASE_URL 优先于 LANGFUSE_HOST。
# EU: https://cloud.langfuse.com   US: https://us.cloud.langfuse.com
# 区域选错会 401，UI 里什么都看不到。公钥/私钥留空则只写本地 traces.jsonl。
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
LANGFUSE_HOST=https://us.cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
TRACE_JSONL_ENABLED=true

# ─── 网络代理（可选，DDG 检索用） ─────────────────────────────
# PROXY 空则直连。SEARCH_MAX_RETRIES / SEARCH_RETRY_DELAY：检索失败重试次数与间隔秒。
PROXY=
SEARCH_MAX_RETRIES=2
SEARCH_RETRY_DELAY=3
```

### `config/runtime.py`

**覆盖。** 模块加载 config/.env（修 uv 启动时 LLM_API_KEY 未读）。RuntimeSettings 增加 Langfuse / 压缩 / 输出预留。缺项抛 ConfigError，无静默回落。

<!-- PACKFILE: config/runtime.py -->
```python
"""运行时配置中心：config/.env 读成一份不可变的 RuntimeSettings。

进程内单例。缺项或非法值在 get_settings() 时抛 ConfigError，无静默回落。
硬约束：LLM_API_KEY 非空；PRO_MODEL / FLASH_MODEL 非空；
OUTPUT_RESERVE_TOKENS < CONTEXT_BUDGET_TOKENS。
"""

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class ConfigError(RuntimeError):
    """配置缺失或不合法。只在 get_settings() 时抛出。"""


# LLM_DEFAULT_HEADERS 为空时使用。AgentRouter 缺这组头会 401 unauthorized client detected。
_DEFAULT_LLM_HEADERS = {
    "User-Agent": "claude-cli/1.0.108 (external, cli)",
    "x-app": "cli",
    "anthropic-version": "2023-06-01",
}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """读整数环境变量。缺省用 default；非整数或低于 minimum 抛 ConfigError。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as e:
            raise ConfigError(f"{name} 必须是整数，实际是 {raw!r}") from e
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 必须 >= {minimum}，实际是 {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """读浮点环境变量。缺省用 default；非数字或越界抛 ConfigError。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as e:
            raise ConfigError(f"{name} 必须是浮点数，实际是 {raw!r}") from e
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 必须 >= {minimum}，实际是 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 必须 <= {maximum}，实际是 {value}")
    return value


def _env_str(name: str, default: str = "") -> str:
    """读字符串环境变量并 strip。未设置时用 default。"""
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    """读布尔环境变量。false/0/no/off（大小写不敏感）为假，其余非空为真。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def _env_headers() -> dict[str, str]:
    """LLM_DEFAULT_HEADERS：合法 JSON 对象 → str→str；空则 _DEFAULT_LLM_HEADERS；非法 JSON 抛 ConfigError。"""
    raw = _env_str("LLM_DEFAULT_HEADERS")
    if not raw:
        return dict(_DEFAULT_LLM_HEADERS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"LLM_DEFAULT_HEADERS 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ConfigError("LLM_DEFAULT_HEADERS 必须是 JSON 对象")
    return {str(k): str(v) for k, v in data.items()}


@dataclass(frozen=True)
class RuntimeSettings:
    """一次启动的全部旋钮。frozen：读完后进程内不再变。"""

    # 工作区 / 技能 / 画像 / 记忆库路径
    workspace_dir: Path
    skills_dir: Path
    profile_path: Path
    memory_db_path: Path
    cards_dir: Path
    # Chat
    llm_base_url: str
    llm_api_key: str
    pro_model: str
    flash_model: str
    llm_default_headers: dict[str, str]
    # Embedding
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    # 预算与控制
    pro_step_budget: int
    flash_step_budget: int
    task_wall_time_s: float
    tool_timeout_s: float
    transient_retry_max: int
    validation_retry_max: int
    stagnation_repeat_threshold: int
    stagnation_grace_steps: int
    consecutive_logic_failure_max: int
    max_parallel_readonly_workers: int
    llm_max_concurrency: int
    # 上下文
    context_budget_tokens: int
    compact_trigger_ratio: float
    compact_keep_recent_turns: int
    output_reserve_tokens: int
    # 可观测
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str
    langfuse_environment: str
    trace_jsonl_enabled: bool
    # 沙箱
    aio_sandbox_mcp_url: str
    aio_sandbox_image: str
    aio_sandbox_port: int
    lab_autostart_sandbox: bool
    # 检索
    proxy: str | None
    search_max_retries: int
    search_retry_delay: int

    @property
    def traces_path(self) -> Path:
        """本地 JSONL 轨迹：{workspace_dir}/.labhandler/traces.jsonl。"""
        return self.workspace_dir / ".labhandler" / "traces.jsonl"


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    """返回进程内唯一 RuntimeSettings。首次调用读环境；非法值抛 ConfigError。"""
    llm_key = _env_str("LLM_API_KEY")
    if not llm_key:
        raise ConfigError("LLM_API_KEY 未配置。复制 config/.env.example 到 config/.env 并填入密钥。")

    pro_model = _env_str("PRO_MODEL", "glm-5.3")
    flash_model = _env_str("FLASH_MODEL", "deepseek-v4-flash")
    if not pro_model or not flash_model:
        raise ConfigError("PRO_MODEL 与 FLASH_MODEL 不能为空")

    context_budget = _env_int("CONTEXT_BUDGET_TOKENS", 200000, minimum=4096)
    output_reserve = _env_int("OUTPUT_RESERVE_TOKENS", 8192, minimum=256)
    if output_reserve >= context_budget:
        raise ConfigError(
            f"OUTPUT_RESERVE_TOKENS({output_reserve}) 必须小于 CONTEXT_BUDGET_TOKENS({context_budget})"
        )

    return RuntimeSettings(
        workspace_dir=Path(_env_str("WORKSPACE_DIR", "./workspace")).resolve(),
        skills_dir=Path(_env_str("SKILLS_DIR", "./skills")).resolve(),
        profile_path=Path(_env_str("PROFILE_PATH", "./profile/me.yaml")).resolve(),
        memory_db_path=Path(_env_str("MEMORY_DB_PATH", "./.labhandler_data/memory.db")).resolve(),
        cards_dir=Path(_env_str("CARDS_DIR", "./.labhandler_data/cards")).resolve(),
        llm_base_url=_env_str("LLM_BASE_URL", "https://agentrouter.org/v1"),
        llm_api_key=llm_key,
        pro_model=pro_model,
        flash_model=flash_model,
        llm_default_headers=_env_headers(),
        embedding_base_url=_env_str("EMBEDDING_BASE_URL", "https://llmapi.paratera.com/v1/"),
        embedding_api_key=_env_str("EMBEDDING_API_KEY"),
        embedding_model=_env_str("EMBEDDING_MODEL", "GLM-Embedding-3"),
        pro_step_budget=_env_int("PRO_STEP_BUDGET", 20, minimum=1),
        flash_step_budget=_env_int("FLASH_STEP_BUDGET", 30, minimum=1),
        task_wall_time_s=_env_float("TASK_WALL_TIME_S", 900.0, minimum=10.0),
        tool_timeout_s=_env_float("TOOL_TIMEOUT_S", 120.0, minimum=1.0),
        transient_retry_max=_env_int("TRANSIENT_RETRY_MAX", 3, minimum=1),
        validation_retry_max=_env_int("VALIDATION_RETRY_MAX", 3, minimum=1),
        stagnation_repeat_threshold=_env_int("STAGNATION_REPEAT_THRESHOLD", 3, minimum=2),
        stagnation_grace_steps=_env_int("STAGNATION_GRACE_STEPS", 2, minimum=0),
        consecutive_logic_failure_max=_env_int("CONSECUTIVE_LOGIC_FAILURE_MAX", 3, minimum=1),
        max_parallel_readonly_workers=_env_int("MAX_PARALLEL_READONLY_WORKERS", 3, minimum=1),
        llm_max_concurrency=_env_int("LLM_MAX_CONCURRENCY", 4, minimum=1),
        context_budget_tokens=context_budget,
        compact_trigger_ratio=_env_float("COMPACT_TRIGGER_RATIO", 0.7, minimum=0.1, maximum=0.95),
        compact_keep_recent_turns=_env_int("COMPACT_KEEP_RECENT_TURNS", 3, minimum=1),
        output_reserve_tokens=output_reserve,
        langfuse_public_key=_env_str("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_env_str("LANGFUSE_SECRET_KEY"),
        langfuse_host=_env_str("LANGFUSE_BASE_URL")
        or _env_str("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        langfuse_environment=_env_str("LANGFUSE_TRACING_ENVIRONMENT", "development"),
        trace_jsonl_enabled=_env_bool("TRACE_JSONL_ENABLED", True),
        aio_sandbox_mcp_url=_env_str("AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp"),
        aio_sandbox_image=_env_str(
            "AIO_SANDBOX_IMAGE",
            "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest",
        ),
        aio_sandbox_port=_env_int("AIO_SANDBOX_PORT", 8080, minimum=1),
        lab_autostart_sandbox=_env_bool("LAB_AUTOSTART_SANDBOX", True),
        proxy=_env_str("PROXY") or None,
        search_max_retries=_env_int("SEARCH_MAX_RETRIES", 2, minimum=0),
        search_retry_delay=_env_int("SEARCH_RETRY_DELAY", 3, minimum=0),
    )
```

### `config/prompts.py`

**覆盖。** 阶段 system prompt 与 SPEC/Dispatch/Judge/Takeover/Summary/Remember 对齐；skill 只给目录，由 Pro load_skill。

<!-- PACKFILE: config/prompts.py -->
```python
"""labHandler 全部 LLM 角色的 system prompt。结构化出口经 function calling schema 落地；本模块是唯一出处。"""

# Pro：写出 SPEC.md（目标、整任务固定的接口名、一步可完成的里程碑），经 submit_spec 落地。
SPEC_SYSTEM = """## Role
You are Pro. Read MATERIALS.md and the user request, then write SPEC.md — the single specification that
governs the whole task. Call submit_spec.

## What SPEC.md is
A top-down specification, not a task list and not a dependency graph:
- goal: what the finished work must be
- overview: how you intend to get there
- interfaces: names that are fixed for the entire task (function / class / cli / file / http) with
  signatures. Once written here they never change. Acceptance code will import them verbatim.
- deliverables, constraints, acceptance_strategy
- milestones: the order you will advance in, decomposed top-down

## Milestones are steps, not the project
Each milestone must be something ONE worker can finish in ONE assignment. Workers are fast but weak —
they cannot build the whole thing at once, and they will produce garbage if you ask them to.
If a milestone still reads like a project, split it until it doesn't.
You are not committing to a schedule here; you will decide each concrete assignment later, one at a time,
with the results of the previous step in hand.

## Rules
- Do not invent requirements absent from MATERIALS.md or the user request.
- Academic integrity: never copy a user's sample report verbatim into deliverables.
- User-facing artifacts use the user's language (Chinese if the user wrote Chinese).
"""

# Pro：只派发「这一步」的 assignments；空数组表示 SPEC.md 已满足。
DISPATCH_SYSTEM = """## Role
You are Pro, deciding the single next step. Look at SPEC.md, what is already done, and the latest briefs,
then call submit_dispatch with the assignments for THIS step only.

## One step at a time
Never try to get the whole task done in one dispatch. Advance one milestone, see the result, then decide
again. A worker that is handed too much will half-do it and report success.
Each assignment must be sized so one worker finishes it within its own step budget.

## One worker or several
- One worker: it gets WRITE permission. Use this whenever files must be created or modified.
  This is the normal case.
- Several workers (max 3): they all get READONLY permission and run in parallel, so use this only for
  investigation, comparison, or review. Each must own a distinct, non-overlapping `domain`, and you write
  a separate assignment for each — its own goal, its own spec text, its own artifacts.
  Never split one piece of writing work across parallel workers.

## Writing an assignment
Give it: id, domain, goal, spec (the detailed brief for that worker), and testable.
Add expected_artifacts, constraints, done_when when they apply.
The `spec` field is what that worker reads, so write it for someone who cannot see SPEC.md's rationale
or the other workers — self-contained, concrete, bounded.

## Interface contract
If an assignment is testable you MUST declare `interfaces` — the exact names the worker has to produce.
Reuse the names already fixed in SPEC.md; only add new ones for things SPEC.md did not pin down.

## When to write tests
Set testable=true and call write_acceptance ONLY when both hold:
  1. there is a quantifiable target (an exact return value, an exit code, a measurable threshold), and
  2. the artifact can be checked by code.
Otherwise set testable=false and write no test. A missing gate is honest; a fake gate is not.
Tests go through write_acceptance(task_id=assignment id, filename, content), one file per call,
written against the interfaces you declared. Never put test source in submit_dispatch.

## Finishing
When SPEC.md is fully satisfied, call submit_dispatch with an empty assignments array.
"""

# Flash：只完成手头这一份 assignment，经 submit_brief 交卷。
FLASH_SYSTEM = """## Role
You are Flash, a worker. You receive one assignment and execute exactly that. Finish ONLY by calling
submit_brief.

## Rules
- The assignment's interface contract is binding. Use those exact names, signatures, and paths.
  Pre-written acceptance code imports them verbatim; renaming anything fails the gate.
- Stay inside your assignment. It is one small step; do not finish the whole project, and do not touch
  another worker's domain. Pro decides what comes next — do not list work you skipped or future milestones.
- Transient API/sandbox blips are NOT spec_invalid. Keep going or switch tools.
  spec_invalid is only for a broken assumption in the assignment itself (files it claimed exist but don't,
  an interface that contradicts what is already in the workspace).
- brief: short. What you changed, and any error that actually happened (tool name + one-line cause).
  Never just "done" / "已完成". Never "this step did not do X".
- Empty search is an observation. Repeating an identical tool+args+result will be stopped as a loop.
- You cannot write acceptance/ or session files. Under readonly you cannot write files or execute code at all.
- Prefer sandbox_execute_bash for synchronous command results.
- User-facing files use the user's language.
"""

# Pro-Judge：对本步 briefs + harness gate 给出 continue/finish/revise_spec/takeover；并可改 MEMORY.md。
JUDGE_SYSTEM = """## Role
You are Pro-Judge. Read the worker briefs for the step that just finished, plus the harness gate
(pass/fail/test_invalid/no_hard_criteria). Call submit_judge.

## What each decision means
- continue: this step's work is good. Pro will decide the next step.
  Use this for every healthy step — it does NOT end the lab.
- finish: SPEC.md is fully satisfied; skip straight to the summary.
- revise_spec: SPEC.md itself is wrong — wrong goal, wrong interfaces, impossible approach.
  Only for problems in the specification, not for a worker doing a poor job.
- takeover: you will implement the remaining work of this step yourself.

## Rules
- continue only when the gate is pass AND a spot-check of the implementation looks right.
- test_invalid means the tests themselves are broken — rewrite the tests, do not punish Flash.
- no_hard_criteria is NOT a pass. evidence MUST explicitly say there is no programmatic gate
  and give a semantic rationale.
- Transient failures in briefs are not specification failures.
- A worker only did one small step. Judge that step, not the whole task.
- Applicable /remember rules are in the user message. Fill rule_verdicts for each.
  finish only when every applicable rule is satisfied. Do not enforce inapplicable ones.

## Managing MEMORY.md (you are the only one who can)
It is injected every later turn and survives compaction. Keep it a short list of current invariants.
Empty edits are the default. When a fact is superseded, change or delete the old line — do not only append.

- memory_append: at most one new line, ≤80 characters. Label plus the invariant.
  Good: "TTL: lazy delete on get/scan/delete; ttl_s=None is permanent."
  Bad: algorithms, field lists, fsync order, or anything already in SPEC.md.
- memory_replace: [{old, new}] rewrite matching bullets (old may be a unique substring). new="" deletes.
- memory_remove: [substring, ...] drop matching bullets that are stale or wrong.
- forget_append: describe context that turned out to be noise — abandoned approaches, dead-end
  probes, superseded guesses. Describe the topic to drop, not the conclusion.
  This is a one-shot instruction to the next compaction: once history is compacted the described
  content is gone and the note is discarded with it. Do not re-add the same line later.
"""

# remember_judge：只裁定 /remember 是否适用于本 lab，不写 SPEC/MEMORY。
REMEMBER_JUDGE_SYSTEM = """## Role
You are Remember-Judge. Do not write SPEC.md, MEMORY.md, or code.
For each listed /remember rule, set applies=true only if THIS lab actually produces
the artifact the rule talks about. When unsure, applies=false. Call submit_remember
with the exact rule text.
"""

# Pro 接手本步剩余实现，经 submit_brief 交卷；后续步骤仍正常派发。
TAKEOVER_SYSTEM = """## Role
You are Pro taking over a step the workers could not finish. Edit the workspace yourself, then call
submit_brief with the outcome. Same brief bar as Flash: short, what you changed, errors you hit, no skipped-work list. You may write files and run the sandbox.
Do only this step's work — the remaining steps are still dispatched normally afterwards.
"""

# Pro 收尾：同一条 Pro 对话写 SUMMARY.md 与可选 knowledge_cards（80–800 字）。
SUMMARY_SYSTEM = """## Role
You are Pro wrapping up this lab. You already ran SPEC, dispatch, and judge in this conversation.
Call submit_summary with user_summary (markdown for SUMMARY.md) and optional knowledge_cards
(type lesson|strategy|pattern, content). Cards must be reusable, concrete, 80-800 characters.
If the gate was no_hard_criteria, say so in user_summary. Write SUMMARY in the user's language.
"""

# /dream：对同一 (card_type, task_type) 组合并/淘汰卡片；不确定则保留。
DREAM_SYSTEM = """## Role
You organize archived knowledge cards of one (card_type, task_type) group.
Call submit_dream.

Rules:
- Merge semantic duplicates into one richer card; all sources go to retire_ids.
- Retire empty-talk / one-off noise / falsified older cards (larger card_id is newer).
- When unsure, keep the card (omit it).
- merged.content 80-800 chars; type must match the group card_type.
"""

# 最小改动修订已有 skill 包；operations=[] 表示无需改文件。
EDIT_SKILL_SYSTEM = """## Role
You revise an existing skill package. Call submit_skill_edit.

Rules:
- Minimal change; keep SKILL.md frontmatter (name/description/when_to_use); body ≤150 lines SOP.
- Persistent user rules must land in files, not verbal promises.
- Style samples: distill features, never copy sample text into the skill.
- file only: SKILL.md | references/<name>.md | scripts/<name>
- write is a full overwrite; deleting SKILL.md is forbidden; SKILL.md must start with --- and contain name.
- If no change is needed, operations=[] and explain in summary.
"""
```

### `runtime/llm.py`

**覆盖。** 保持最初接入：仅 AsyncOpenAI chat.completions 流式。本层 max_retries=0，瞬态交给 control.decide。增加 ChatResult.truncated 与 aclose。不含 CursorChatBackend。

<!-- PACKFILE: runtime/llm.py -->
```python
"""LLM 入口：Chat / Embedding 走 OpenAI 兼容网关。

瞬时失败分类后原样返回 ChatResult.error_class；退避与放弃由 control.decide
按 Task 剩余步数和墙钟决定。本层 max_retries=0。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer


@dataclass
class ChatResult:
    content: str
    reasoning: str
    tool_calls: list[dict[str, str]]
    finish_reason: str | None
    usage: dict[str, int]
    error_class: ErrorClass = ErrorClass.OK

    @property
    def truncated(self) -> bool:
        """输出把剩余窗口写满了。内容和 tool_call 都可能是半截的。"""
        return self.finish_reason == "length"


class LLMGateway:
    """可注入的 LLM 入口。生命周期由调用方持有，与 Tracer / Settings 同寿。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        chat_client: AsyncOpenAI | None = None,
        embed_client: AsyncOpenAI | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self.tracer = tracer
        self.chat_client = chat_client or AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            default_headers=settings.llm_default_headers,
            max_retries=0,
            timeout=120.0,
        )
        self.embed_client = embed_client or AsyncOpenAI(
            api_key=settings.embedding_api_key or "empty",
            base_url=settings.embedding_base_url,
            max_retries=0,
            timeout=60.0,
        )

    async def aclose(self) -> None:
        close = getattr(self.chat_client, "close", None)
        if callable(close):
            await close()

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str, bool], Awaitable[None]] | None = None,
        span_name: str = S.LLM,
    ) -> ChatResult:
        tracer = self.tracer
        span_cm = (
            tracer.span(
                span_name,
                kind=S.KIND_GENERATION,
                **{S.ATTR_MODEL: model, "messages": len(messages)},
            )
            if tracer is not None
            else None
        )
        span = span_cm.__enter__() if span_cm is not None else None
        try:
            try:
                result = await self._openai_chat(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    on_delta=on_delta,
                )
            except Exception as e:
                result = ChatResult(
                    content="",
                    reasoning="",
                    tool_calls=[],
                    finish_reason="error",
                    usage={},
                    error_class=classify(e),
                )
            if span is not None:
                span.set(
                    **{
                        S.ATTR_TOKENS_IN: result.usage.get("input_tokens", 0),
                        S.ATTR_TOKENS_OUT: result.usage.get("output_tokens", 0),
                        S.ATTR_TOKENS_REASONING: result.usage.get("reasoning_tokens", 0),
                        S.ATTR_FINISH_REASON: result.finish_reason,
                        S.ATTR_ERROR_CLASS: result.error_class.value,
                    }
                )
                span.output(
                    {
                        "tool_calls": [tc.get("name") for tc in result.tool_calls],
                        "content_chars": len(result.content),
                        "truncated": result.truncated,
                    }
                )
            return result
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    async def _openai_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        max_tokens: int | None,
        on_delta: Callable[[str, bool], Awaitable[None]] | None,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        content = ""
        reasoning = ""
        acc: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}
        finish_reason: str | None = None

        async with self._sem:
            stream = await self.chat_client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    details = getattr(u, "completion_tokens_details", None)
                    usage = {
                        "input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                        "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                        "reasoning_tokens": int(getattr(details, "reasoning_tokens", 0) or 0),
                    }
                if not chunk.choices:
                    continue
                ch = chunk.choices[0]
                finish_reason = ch.finish_reason or finish_reason
                delta = ch.delta
                if delta is None:
                    continue
                if delta.content:
                    content += delta.content
                    if on_delta:
                        await on_delta(delta.content, False)
                extra = getattr(delta, "reasoning_content", None)
                if not extra:
                    extra = (getattr(delta, "model_extra", None) or {}).get("reasoning_content")
                if extra:
                    reasoning += extra
                    if on_delta:
                        await on_delta(extra, True)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        slot = acc.setdefault(tc.index or 0, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        fn = tc.function
                        if fn is not None:
                            if fn.name:
                                slot["name"] += fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments

        return ChatResult(
            content=content,
            reasoning=reasoning,
            tool_calls=[acc[i] for i in sorted(acc) if acc[i].get("name")],
            finish_reason=finish_reason,
            usage=usage,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._sem:
            resp = await self.embed_client.embeddings.create(
                model=self.settings.embedding_model,
                input=texts,
            )
        return [list(item.embedding) for item in resp.data]
```

### `runtime/errors.py`

**覆盖。** 错误分类枚举。去掉 CursorAgentError 特判，分类仍覆盖 OpenAI 超时/429/鉴权。

<!-- PACKFILE: runtime/errors.py -->
```python
"""Error taxonomy：纯枚举 + 无状态 classify。"""

from enum import Enum


class ErrorClass(str, Enum):
    OK = "ok"
    TRANSIENT = "transient"
    VALIDATION = "validation"
    PERMISSION = "permission"
    ENVIRONMENT = "environment"
    LOGIC = "logic"
    ACCEPTABLE = "acceptable"
    AUTH = "auth"
    FATAL = "fatal"
    LOOP = "loop"


_AUTH_MARKERS = (
    "unauthorized client detected",
    "invalid api key",
    "incorrect api key",
    "authentication",
)

_TRANSIENT_TYPES = {
    "TimeoutError",
    "asyncio.TimeoutError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "APIConnectionError",
    "ConnectError",
    "ReadTimeout",
    "WriteTimeout",
}


def classify(exc: BaseException | None, *, text: str = "") -> ErrorClass:
    """把异常或工具正文映射到 ErrorClass。不含状态、不做处置。"""
    blob = f"{type(exc).__name__ if exc else ''} {exc or ''} {text}".lower()
    if any(m in blob for m in _AUTH_MARKERS) or "401" in blob or "403" in blob:
        if "permission" in blob and "401" not in blob:
            pass
        else:
            if any(m in blob for m in _AUTH_MARKERS) or "401" in blob:
                return ErrorClass.AUTH
            if "403" in blob and "forbidden" in blob:
                return ErrorClass.AUTH

    if exc is not None:
        name = type(exc).__name__
        qual = f"{type(exc).__module__}.{name}"
        if name in _TRANSIENT_TYPES or qual.endswith("TimeoutError"):
            return ErrorClass.TRANSIENT
        if name in {"PermissionError"}:
            return ErrorClass.PERMISSION
        if name in {"FileNotFoundError", "NotADirectoryError", "IsADirectoryError"}:
            return ErrorClass.ENVIRONMENT
        if name in {"json.JSONDecodeError", "JSONDecodeError", "ValidationError"}:
            return ErrorClass.VALIDATION
        if "rate limit" in blob or "429" in blob or "5xx" in blob or "status 5" in blob:
            return ErrorClass.TRANSIENT
        if "sandbox_unreachable" in blob:
            return ErrorClass.FATAL

    if "[error/permissionerror]" in blob:
        return ErrorClass.PERMISSION
    if "[sandbox_unreachable]" in blob:
        return ErrorClass.FATAL
    if "timeout" in blob or "429" in blob or "rate limit" in blob:
        return ErrorClass.TRANSIENT
    if "malformed" in blob or "invalid json" in blob or "schema" in blob and "fail" in blob:
        return ErrorClass.VALIDATION
    if "not found" in blob or "no such file" in blob:
        return ErrorClass.ENVIRONMENT
    return ErrorClass.LOGIC if exc is not None else ErrorClass.OK
```

### `runtime/phase.py`

**新建。** 阶段定义表：谁执行、工具可见范围、唯一 submit 出口、是否并入 Pro transcript。Flash / Remember-Judge 不并线。

<!-- PACKFILE: runtime/phase.py -->
```python
"""阶段定义表：每一拍「谁执行、看得见什么、从哪交卷」的唯一出处。

TaskKind 是阶段的唯一身份。system prompt、结构化出口、工具可见范围、节点权限、
是否并入 Pro 那条长对话，全部从这里派生；调用方只传 kind。

两个权限概念不要混：
- permission 是节点权限，进 Task 树与审计，决定文件写检查放不放行。
- visible 只影响这一拍的工具表里出现哪些名字，取值必须不宽于 permission。
  规划与判决阶段的节点仍是 pro（要调 pro 门槛的 submit_*），但工具表只给只读，
  因此「写 SPEC 时顺手改了代码」在工具表层面就不可能发生。

本模块只描述阶段，不决定阶段顺序——顺序在 orchestrator 的主流程里。
"""

from dataclasses import dataclass

from config.prompts import (
    DISPATCH_SYSTEM,
    FLASH_SYSTEM,
    JUDGE_SYSTEM,
    REMEMBER_JUDGE_SYSTEM,
    SPEC_SYSTEM,
    SUMMARY_SYSTEM,
    TAKEOVER_SYSTEM,
)
from runtime.schema_call import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
    WRITE_ACCEPTANCE,
)
from runtime.task import Permission, TaskKind
from tools.skill_tool import LOAD_SKILL, LOAD_SKILL_REFERENCE

PRO = "pro"
FLASH = "flash"
# 只读 Pro 阶段默认看不见 pro 权限工具；skill 读取经 extra 点名放行。
# Flash / remember_judge 不在 extra 里，因此调不到。
_SKILL_READ = frozenset({LOAD_SKILL, LOAD_SKILL_REFERENCE})


@dataclass(frozen=True)
class PhaseSpec:
    agent: str
    """pro | flash。决定用哪个模型，以及 profile 按哪个角色注入。"""

    system: str
    submit_tool: str
    """本阶段唯一的结构化出口。调别的 submit_* 会被 loop 当校验错误退回。"""

    permission: Permission | None = None
    """节点权限。留空表示由调用方给——只有 WORKER 如此，它按本波人数决定。"""

    visible: Permission | None = None
    """工具表收窄到这个角色。留空表示跟随节点权限。"""

    extra_tools: frozenset[str] = frozenset()
    """visible 之外额外开放的具体工具名。"""

    shares_thread: bool = False
    """是否并入 Pro 那条贯穿全程的 transcript。"""

    def node_permission(self, fallback: Permission | None = None) -> Permission:
        chosen = self.permission or fallback
        if chosen is None:
            raise ValueError(f"phase {self.submit_tool} 需要调用方提供 permission")
        return chosen

    def visible_role(self, node_permission: Permission) -> str:
        return (self.visible or node_permission).value


PHASES: dict[TaskKind, PhaseSpec] = {
    TaskKind.SPEC: PhaseSpec(
        agent=PRO,
        system=SPEC_SYSTEM,
        submit_tool=SUBMIT_SPEC,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        extra_tools=_SKILL_READ,
        shares_thread=True,
    ),
    TaskKind.REMEMBER_JUDGE: PhaseSpec(
        agent=PRO,
        system=REMEMBER_JUDGE_SYSTEM,
        submit_tool=SUBMIT_REMEMBER,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        # 只裁定规则适用性，与主线无关，不占 Pro 的 transcript。
        shares_thread=False,
    ),
    TaskKind.DISPATCH: PhaseSpec(
        agent=PRO,
        system=DISPATCH_SYSTEM,
        submit_tool=SUBMIT_DISPATCH,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        # 验收测试在派活时预先写好，所以这一拍要开 write_acceptance。
        extra_tools=_SKILL_READ | {WRITE_ACCEPTANCE},
        shares_thread=True,
    ),
    TaskKind.JUDGE: PhaseSpec(
        agent=PRO,
        system=JUDGE_SYSTEM,
        submit_tool=SUBMIT_JUDGE,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        # 门禁已在沙箱跑过，判决只读代码与 briefs；test_invalid 时可重写测试。
        extra_tools=_SKILL_READ | {WRITE_ACCEPTANCE},
        shares_thread=True,
    ),
    TaskKind.TAKEOVER: PhaseSpec(
        agent=PRO,
        system=TAKEOVER_SYSTEM,
        submit_tool=SUBMIT_BRIEF,
        permission=Permission.PRO,
        # visible 留空：接管要真正改文件跑沙箱，拿节点权限的全集。
        shares_thread=True,
    ),
    TaskKind.SUMMARY: PhaseSpec(
        agent=PRO,
        system=SUMMARY_SYSTEM,
        submit_tool=SUBMIT_SUMMARY,
        permission=Permission.PRO,
        visible=Permission.READONLY,
        extra_tools=_SKILL_READ,
        shares_thread=True,
    ),
    TaskKind.WORKER: PhaseSpec(
        agent=FLASH,
        system=FLASH_SYSTEM,
        submit_tool=SUBMIT_BRIEF,
        # permission 由 permission_for_wave 给：独个 Flash 拿写权限，并行则全部只读。
        shares_thread=False,
    ),
}


def phase_of(kind: TaskKind) -> PhaseSpec:
    try:
        return PHASES[kind]
    except KeyError:
        raise ValueError(f"{kind} 不是一个 agent 阶段") from None
```

### `runtime/spec.py`

**新建。** ProjectSpec + Assignment。一次最多 3 个 Flash；多于 1 个全部只读；domain 互不重叠。

<!-- PACKFILE: runtime/spec.py -->
```python
"""SPEC 模型：ProjectSpec（总纲）与 Assignment（单份 Flash 任务书）。

SPEC.md 由 Pro 维护，是后续每一步派发的共同依据。一份 Assignment 自包含，
且 domain 在同一次派发内互不重叠。一次派发最多 MAX_ASSIGNMENTS 个 Flash；
多于 1 个时全部降只读。
"""

from dataclasses import dataclass, field
from typing import Any

# 一次派发最多几个 Flash。n>1 时 permission 全部为 readonly。
MAX_ASSIGNMENTS = 3


@dataclass(frozen=True)
class InterfaceItem:
    """一个 Flash 必须精确实现的可被调用/被检查的名字。"""

    name: str
    kind: str = "function"  # function | class | cli | file | http
    signature: str = ""
    description: str = ""

    @classmethod
    def from_payload(cls, raw: Any) -> "InterfaceItem | None":
        if isinstance(raw, str):
            return cls(name=raw.strip()) if raw.strip() else None
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            kind=str(raw.get("kind") or "function").strip() or "function",
            signature=str(raw.get("signature") or "").strip(),
            description=str(raw.get("description") or "").strip(),
        )

    def render(self) -> str:
        head = f"- `{self.signature or self.name}`（{self.kind}）"
        return f"{head}：{self.description}" if self.description else head

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "signature": self.signature,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProjectSpec:
    """SPEC.md 的结构化形式：整个任务的总纲，由 Pro 维护。"""

    goal: str
    overview: str = ""
    interfaces: list[InterfaceItem] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_strategy: str = ""
    milestones: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "ProjectSpec":
        return cls(
            goal=str(raw.get("goal") or "").strip(),
            overview=str(raw.get("overview") or "").strip(),
            interfaces=_parse_interfaces(raw.get("interfaces")),
            deliverables=_str_list(raw.get("deliverables")),
            constraints=_str_list(raw.get("constraints")),
            acceptance_strategy=str(raw.get("acceptance_strategy") or "").strip(),
            milestones=_str_list(raw.get("milestones")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "overview": self.overview,
            "interfaces": [i.to_dict() for i in self.interfaces],
            "deliverables": list(self.deliverables),
            "constraints": list(self.constraints),
            "acceptance_strategy": self.acceptance_strategy,
            "milestones": list(self.milestones),
        }

    def render(self) -> str:
        """渲染成 SPEC.md。这份文件是后续每一步派发的共同依据。"""
        parts = ["# SPEC", "", "## 总目标", self.goal or "(未填写)"]
        if self.overview:
            parts += ["", "## 方案概述", self.overview]
        if self.interfaces:
            parts += [
                "",
                "## 全局接口契约",
                "以下名字在整个任务范围内固定，任何一步都不得改名或改签名。",
                *[i.render() for i in self.interfaces],
            ]
        parts += _section("交付物", self.deliverables)
        parts += _section("全局约束", self.constraints)
        parts += _section("里程碑（自顶向下的推进顺序）", self.milestones)
        if self.acceptance_strategy:
            parts += ["", "## 验收策略", self.acceptance_strategy]
        return "\n".join(parts).strip() + "\n"


@dataclass(frozen=True)
class Assignment:
    """一次派发中交给某一个 Flash 的任务书。"""

    id: str
    domain: str
    goal: str
    # 验收文件落盘时用的 id。Pro 写 write_acceptance 时用的是它原本给的 id，
    # 之后 id 可能因为重名被改写，门禁必须仍按原名去找测试。
    acceptance_id: str = ""
    spec: str = ""
    expected_artifacts: list[str] = field(default_factory=list)
    interfaces: list[InterfaceItem] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    testable: bool = False
    acceptance_intent: str = ""
    acceptance_files: list[str] = field(default_factory=list)

    @property
    def gate_id(self) -> str:
        """去 acceptance/ 下找测试时用的目录名。"""
        return self.acceptance_id or self.id

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "Assignment":
        return cls(
            id=str(raw.get("id") or "").strip(),
            domain=str(raw.get("domain") or "").strip(),
            goal=str(raw.get("goal") or "").strip(),
            acceptance_id=str(raw.get("acceptance_id") or "").strip(),
            spec=str(raw.get("spec") or "").strip(),
            expected_artifacts=_str_list(raw.get("expected_artifacts")),
            interfaces=_parse_interfaces(raw.get("interfaces")),
            constraints=_str_list(raw.get("constraints")),
            done_when=_str_list(raw.get("done_when")),
            testable=bool(raw.get("testable")),
            acceptance_intent=str(raw.get("acceptance_intent") or "").strip(),
            acceptance_files=_str_list(raw.get("acceptance_files")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "goal": self.goal,
            "acceptance_id": self.gate_id,
            "spec": self.spec,
            "expected_artifacts": list(self.expected_artifacts),
            "interfaces": [i.to_dict() for i in self.interfaces],
            "constraints": list(self.constraints),
            "done_when": list(self.done_when),
            "testable": self.testable,
            "acceptance_intent": self.acceptance_intent,
            "acceptance_files": list(self.acceptance_files),
        }


@dataclass(frozen=True)
class Dispatch:
    """Pro 对"下一步做什么"的一次决定。assignments 为空表示无事可做。"""

    step_goal: str
    assignments: list[Assignment] = field(default_factory=list)

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "Dispatch":
        return cls(
            step_goal=str(raw.get("step_goal") or "").strip(),
            assignments=[
                Assignment.from_payload(a)
                for a in (raw.get("assignments") or [])
                if isinstance(a, dict)
            ],
        )

    @property
    def is_empty(self) -> bool:
        return not self.assignments


def render_assignment(
    assignment: Assignment,
    *,
    user_request: str,
    project_spec: str,
    step_goal: str,
    done_so_far: list[tuple[str, str]] | None = None,
    parallel_domains: list[str] | None = None,
) -> str:
    """渲染成这个 Flash 本轮唯一的任务输入。"""
    parts: list[str] = [
        f"# 任务书 {assignment.id}",
        "",
        f"**负责领域**：{assignment.domain or assignment.goal}",
        "",
        "## 本步目标",
        assignment.goal,
    ]
    if assignment.spec:
        parts += ["", "## 详细说明", assignment.spec]

    parts += [
        "",
        "## 这一步在整体中的位置",
        f"当前步骤要推进的是：{step_goal}" if step_goal else "（未说明）",
        "",
        "本任务书只覆盖上面这一小段，不要越界去做整个项目。"
        "后续步骤由 Pro 调度；brief 只写你做了什么和碰到的错误，不要写本步没做的事。",
    ]

    if done_so_far:
        lines = "\n".join(f"- **{aid}**：{summary}" for aid, summary in done_so_far if summary)
        if lines:
            parts += ["", "## 已经完成的部分", lines]

    if parallel_domains:
        others = "、".join(parallel_domains)
        parts += [
            "",
            "## 同时进行的其它领域",
            f"本轮还有其它 worker 分别负责：{others}。不要碰它们的领域。",
        ]

    if assignment.interfaces:
        parts += [
            "",
            "## 接口契约（名字必须完全一致）",
            "验收代码会直接按这些名字导入和调用。改名、换签名、换文件位置都会导致验收失败。",
            *[i.render() for i in assignment.interfaces],
        ]

    parts += _section("预期产物", assignment.expected_artifacts)
    parts += _section("约束", assignment.constraints)
    parts += _section("完成判据", assignment.done_when)

    if assignment.testable:
        files = "、".join(assignment.acceptance_files) or "已就绪的验收文件"
        parts += [
            "",
            "## 验收方式",
            f"本步有可量化指标，测试已预先写好（{files}）。{assignment.acceptance_intent}",
            "你提交 brief 后 harness 会自动在沙箱里跑这些测试，结果不由你填写。",
        ]
    else:
        parts += [
            "",
            "## 验收方式",
            "本步没有可量化的程序性指标，不会跑自动测试。"
            "请在 brief 里给出可核查的事实（改了哪些文件、依据是什么），供 Judge 语义判断。",
        ]

    if project_spec:
        parts += ["", "## 总纲 SPEC.md（供对齐，不要重复实现）", project_spec]

    parts += [
        "",
        "## 原始用户诉求",
        user_request.strip(),
        "",
        "完成后调用 `submit_brief` 结束。瞬时错误不算 spec_invalid。",
    ]
    return "\n".join(parts).strip() + "\n"


def _section(title: str, items: list[str]) -> list[str]:
    body = [f"- {x}" for x in items if str(x).strip()]
    return ["", f"## {title}", *body] if body else []


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _parse_interfaces(raw: Any) -> list[InterfaceItem]:
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in (InterfaceItem.from_payload(x) for x in raw)
        if item is not None
    ]
```

### `runtime/loop.py`

**覆盖。** ReAct 循环：一轮可执行多条 tool_calls（顺序 await）。DumpScope 按 agent/task 隔离卸盘。Flash compact 不碰 FORGET.md。

<!-- PACKFILE: runtime/loop.py -->
```python
"""一轮 Agent Loop：发出去的上下文、模型响应、工具结果与控制面决策。

硬不变量：
1. history 是工具消息的唯一载体。装配层只有这一个入口，同一批结果因此只会
   出现一次，且始终紧跟在它的 assistant 之后。
2. 压缩一旦发生，发给模型的永远是重新装配后的那一份；否则触发压缩的那一轮
   仍会把超长上下文发出去。
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from runtime.context.assemble import AgentContext, assemble, validate_message_sequence
from runtime.context.budget import TokenBudget
from runtime.context.compact import DumpScope, compact_history
from runtime.context.notes import load_notes, memory_block
from runtime.control import decide
from runtime.errors import ErrorClass
from runtime.llm import LLMGateway
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.phase import PRO
from runtime.retry import sleep_delay
from runtime.schema_call import (
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_DISPATCH,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
    VALIDATION_TOOL_RESULT,
    coerce_brief,
    parse_args,
    synthetic_brief,
    tool_choice_required,
    validate_payload,
)
from runtime.stagnation import NUDGE_TEXT, StagnationSignal, StagnationTracker
from runtime.task import Permission, RuntimeTask, TaskStatus
from runtime.tools import ToolContext, ToolRegistry
from tools.skill_tool import LOAD_SKILL, LOAD_SKILL_REFERENCE

_SUBMIT_TOOLS = {
    SUBMIT_SPEC,
    SUBMIT_DISPATCH,
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SUMMARY,
}
_RETRIEVAL_TOOLS = {
    "memory_search",
    "memory_grep",
    "memory_read",
    LOAD_SKILL,
    LOAD_SKILL_REFERENCE,
}
_RETRIEVED_CAP = 6000

# 一轮里多个工具调用时，取最严重的那个错误类别记账。
# 只留最后一个会让「先失败后成功」的一轮被记成全绿，熔断器永远攒不够计数。
_ERROR_SEVERITY = {
    ErrorClass.OK: 0,
    ErrorClass.ACCEPTABLE: 1,
    ErrorClass.VALIDATION: 2,
    ErrorClass.TRANSIENT: 3,
    ErrorClass.ENVIRONMENT: 4,
    ErrorClass.PERMISSION: 4,
    ErrorClass.LOGIC: 5,
    ErrorClass.LOOP: 6,
    ErrorClass.AUTH: 7,
    ErrorClass.FATAL: 8,
}
_STAGNATION_SEVERITY = {
    StagnationSignal.NONE: 0,
    StagnationSignal.REPEAT: 1,
    StagnationSignal.LOOP_CONFIRMED: 2,
}


def _worse_error(a: ErrorClass, b: ErrorClass) -> ErrorClass:
    return b if _ERROR_SEVERITY.get(b, 0) > _ERROR_SEVERITY.get(a, 0) else a


def _worse_stagnation(a: StagnationSignal, b: StagnationSignal) -> StagnationSignal:
    return b if _STAGNATION_SEVERITY[b] > _STAGNATION_SEVERITY[a] else a


@dataclass
class AgentSpec:
    """一拍 agent 的全部参数，由 runtime.phase 解析好后传进来。

    permission 是节点权限（ctx.role，决定写检查放不放行）；visible_role 与
    extra_tools 只管工具表里出现哪些名字，两者由阶段定义给出。
    tool_extras 原样进 ToolContext（例如全程共用的 SkillBind）。
    """

    name: str
    model: str
    permission: Permission
    system: str
    submit_tool: str
    visible_role: str
    extra_tools: frozenset[str] = frozenset()
    tool_extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    brief: dict[str, Any] | None
    submit: dict[str, Any] | None
    reason: str
    history: list[dict[str, Any]] = field(default_factory=list)


def drop_dangling_tool_calls(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉末尾没有 tool 响应的 assistant。

    上一轮如果在工具执行途中被墙钟或取消打断，history 会以一个悬空的
    tool_calls 结尾，下一次请求必然被网关拒。
    """
    if not history:
        return history
    last_owner = -1
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_owner = i
    if last_owner < 0:
        return history
    answered = {
        str(m.get("tool_call_id") or "")
        for m in history[last_owner + 1 :]
        if m.get("role") == "tool"
    }
    expected = {str(tc.get("id") or "") for tc in history[last_owner]["tool_calls"]}
    if expected <= answered:
        return history
    return history[:last_owner]


async def run_loop(
    task: RuntimeTask,
    spec: AgentSpec,
    *,
    settings: RuntimeSettings,
    llm: LLMGateway,
    registry: ToolRegistry,
    session_dir: Path,
    user_input: str,
    project_spec: str,
    history: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    tracer: Tracer | None = None,
    clock: Callable[[], float] = time.time,
) -> LoopResult:
    role = spec.permission.value
    tools_reg = registry.for_role(
        spec.visible_role, submit_tool=spec.submit_tool, extra=spec.extra_tools
    )
    openai_tools = tools_reg.openai_tools()
    tracker = StagnationTracker(
        threshold=settings.stagnation_repeat_threshold,
        grace_steps=settings.stagnation_grace_steps,
    )
    history = list(history or [])
    retrieved = ""
    force_brief = False
    last_error = ErrorClass.OK
    last_stag = StagnationSignal.NONE
    validation_streak: dict[str, int] = {}
    tokens = TokenBudget.from_settings(settings)
    dump = DumpScope(session_dir=session_dir, agent=spec.name, task_id=task.task_id)

    def done(
        brief: dict[str, Any] | None,
        submit: dict[str, Any] | None,
        reason: str,
    ) -> LoopResult:
        return LoopResult(brief, submit, reason, history=list(history))

    async def emit(kind: str, **payload: Any) -> None:
        if on_event:
            await on_event({"kind": kind, **payload})

    def trace_event(name: str, **attrs: Any) -> None:
        if tracer is not None:
            tracer.event(name, **attrs)

    async def build_context() -> AgentContext:
        """返回本轮实际发给模型的上下文。压缩一旦发生，history 已换成压缩后的版本。"""
        nonlocal history
        history = drop_dangling_tool_calls(history)
        notes = load_notes(session_dir)
        working = (
            f"changed_files={task.execution_state.get('changed_files', [])}"
        )

        def build() -> AgentContext:
            return assemble(
                system=spec.system,
                user_input=user_input,
                project_spec=project_spec,
                history=history,
                retrieved=retrieved,
                memory=memory_block(notes),
                events=task.events,
                working=working,
                tool_schemas=openai_tools,
            )

        ctx = build()
        result = await compact_history(
            history=history,
            dump=dump,
            settings=settings,
            llm=llm,
            notes=notes,
            budget=tokens,
            estimate=ctx.input_tokens,
            apply_forget=spec.name == PRO,
            tracer=tracer,
        )
        if result.compacted:
            history = result.history
            ctx = build()
            await emit(
                "compact",
                node=spec.name,
                before=result.tokens_before,
                after=tokens.projected(ctx.input_tokens),
                summarized=result.turns_summarized,
                forget_cleared=result.forget_cleared,
                scale=round(tokens.scale, 3),
            )
        return ctx

    async def one_turn() -> LoopResult | None:
        nonlocal force_brief, last_error, last_stag, history, retrieved

        span_cm = (
            tracer.span(
                S.TURN,
                kind=S.KIND_CHAIN,
                **{
                    S.ATTR_TASK_ID: task.task_id,
                    S.ATTR_AGENT: spec.name,
                    S.ATTR_STEP: task.step_count,
                    S.ATTR_STEP_BUDGET: task.step_budget,
                },
            )
            if tracer is not None
            else None
        )
        turn_span = span_cm.__enter__() if span_cm is not None else None
        try:
            ctx = await build_context()
            if turn_span is not None:
                turn_span.set(**{S.ATTR_TOKENS_IN: ctx.input_tokens})

            problems = validate_message_sequence(ctx.messages)
            if problems:
                trace_event(S.EV_DECISION, reason="protocol_violation", detail="; ".join(problems))

            choice: Any = tool_choice_required(spec.submit_tool) if force_brief else "auto"

            async def on_delta(text: str, reasoning: bool) -> None:
                await emit("content", node=spec.name, text=text, reasoning=reasoning)

            result = await llm.chat(
                model=spec.model,
                messages=ctx.messages,
                tools=openai_tools,
                tool_choice=choice,
                on_delta=on_delta,
            )
            last_error = result.error_class
            tokens.observe(ctx.input_tokens, result.usage)

            if result.error_class is ErrorClass.AUTH:
                task.record(result.error_class, StagnationSignal.NONE, result.usage)
                return done(None, None, "auth")
            if result.error_class is ErrorClass.TRANSIENT:
                task.record(result.error_class, StagnationSignal.NONE, result.usage)
                return None
            if result.truncated:
                # finish_reason=length：content / tool_calls 可能是半截。events 注入截断提示。
                trace_event(S.EV_DECISION, reason="output_truncated")
                task.events.append(
                    {"text": "Your previous reply was cut off by the output limit. Be more concise."}
                )

            if not result.tool_calls:
                task.record(ErrorClass.OK, StagnationSignal.NONE, result.usage)
                if result.content:
                    history.append({"role": "assistant", "content": result.content})
                if force_brief:
                    return done(
                        synthetic_brief(
                            outcome="failed", brief="model did not call the required submit tool"
                        ),
                        None,
                        "force_brief_failed",
                    )
                return None

            tctx = ToolContext(
                role=role,
                settings=settings,
                session_dir=session_dir,
                llm=llm,
                on_event=on_event,
                extras={**spec.tool_extras, "dump": dump},
            )
            submit_name: str | None = None
            submit_payload: dict[str, Any] | None = None
            tool_messages: list[dict[str, Any]] = []

            calls = [
                {**tc, "id": tc["id"] or f"call_{i}"} for i, tc in enumerate(result.tool_calls)
            ]
            history.append(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in calls
                    ],
                }
            )

            def reply(call_id: str, content: str) -> None:
                tool_messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": content}
                )

            turn_error = ErrorClass.OK
            turn_stag = StagnationSignal.NONE

            for tc in calls:
                name = tc["name"]
                call_id = tc["id"]
                await emit("tool", node=spec.name, name=name, args=tc["arguments"][:200], result="")

                parsed, perr = parse_args(tc["arguments"])
                if parsed is None:
                    validation_streak[name] = validation_streak.get(name, 0) + 1
                    turn_error = _worse_error(turn_error, ErrorClass.VALIDATION)
                    reply(call_id, VALIDATION_TOOL_RESULT.format(err=perr))
                    continue

                if name in _SUBMIT_TOOLS:
                    if name != spec.submit_tool:
                        # 本阶段出口固定为 spec.submit_tool。错调的 payload 不交给上层，只回校验错误。
                        turn_error = _worse_error(turn_error, ErrorClass.VALIDATION)
                        reply(
                            call_id,
                            VALIDATION_TOOL_RESULT.format(
                                err=f"{name} is not the exit for this stage; call {spec.submit_tool}"
                            ),
                        )
                        continue
                    degraded = validation_streak.get(name, 0) >= settings.validation_retry_max
                    err = validate_payload(name, parsed, degraded=degraded)
                    if err:
                        validation_streak[name] = validation_streak.get(name, 0) + 1
                        turn_error = _worse_error(turn_error, ErrorClass.VALIDATION)
                        reply(call_id, VALIDATION_TOOL_RESULT.format(err=err))
                        continue
                    validation_streak[name] = 0
                    submit_name, submit_payload = name, parsed
                    reply(call_id, "ok")
                    continue

                tctx.extras["tool_call_id"] = call_id
                outcome = await tools_reg.execute(
                    name, parsed, tctx, timeout=settings.tool_timeout_s, tracer=tracer
                )
                turn_error = _worse_error(turn_error, outcome.error_class)
                signal = tracker.observe(name, parsed, outcome.text)
                turn_stag = _worse_stagnation(turn_stag, signal)
                if signal is not StagnationSignal.NONE:
                    trace_event(
                        S.EV_STAGNATION,
                        **{S.ATTR_SIGNAL: signal.value, S.ATTR_TOOL: name},
                    )
                if name in _RETRIEVAL_TOOLS:
                    retrieved = (retrieved + "\n" + outcome.text)[-_RETRIEVED_CAP:]
                reply(call_id, outcome.text)
                await emit(
                    "tool",
                    node=spec.name,
                    name=name,
                    args=str(parsed)[:200],
                    result=outcome.text[:200],
                    error_class=outcome.error_class.value,
                )

            history.extend(tool_messages)
            last_error, last_stag = turn_error, turn_stag
            task.record(last_error, last_stag, result.usage)

            if submit_payload is not None:
                if turn_span is not None:
                    turn_span.set(**{S.ATTR_OUTCOME: submit_name})
                if submit_name == SUBMIT_BRIEF:
                    return done(coerce_brief(submit_payload), submit_payload, "submit_brief")
                return done(None, {"name": submit_name, "payload": submit_payload}, "submit")
            return None
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    try:
        while True:
            remaining = max(1.0, task.deadline - clock())
            if task.status is TaskStatus.PENDING:
                task.transit(TaskStatus.RUNNING)

            finished = await asyncio.wait_for(one_turn(), timeout=remaining)
            if finished is not None:
                return finished

            decision = decide(
                task.snapshot(),
                error_class=last_error,
                stagnation=last_stag,
                settings=settings,
                now=clock(),
            )
            trace_event(
                S.EV_DECISION,
                **{S.ATTR_DECISION: decision.kind, S.ATTR_REASON: decision.reason},
            )

            if decision.kind == "retry_tool":
                trace_event(
                    S.EV_RETRY,
                    **{
                        S.ATTR_ATTEMPT: task.transient_count,
                        S.ATTR_DELAY_S: decision.delay,
                        S.ATTR_ERROR_CLASS: last_error.value,
                    },
                )
                await sleep_delay(decision.delay)
                continue

            if decision.kind == "nudge":
                history.append(
                    {"role": "user", "content": decision.text or NUDGE_TEXT, "pinned": True}
                )
                last_stag = StagnationSignal.NONE
                continue

            if decision.kind == "force_brief":
                if force_brief:
                    return done(
                        synthetic_brief(
                            outcome="failed",
                            brief="forced submit failed schema or was not called",
                        ),
                        None,
                        "force_brief_failed",
                    )
                force_brief = True
                continue

            if decision.kind == "stop":
                return done(
                    synthetic_brief(outcome="failed", brief=f"stopped: {decision.reason}"),
                    None,
                    decision.reason,
                )
    except asyncio.TimeoutError:
        return done(
            synthetic_brief(outcome="failed", brief="wall-clock deadline reached"),
            None,
            "deadline",
        )
```

### `runtime/orchestrator.py`

**覆盖。** 主流程：SPEC → Remember-Judge → Dispatch → Worker → 程序性验收 → Judge；takeover 仅 Judge 裁定。Pro catalog + SkillBind；Flash 不加载 skill。

<!-- PACKFILE: runtime/orchestrator.py -->
````python
"""Task 树解释器。一次 lab = 一份 SPEC.md + 至多 _MAX_STEPS 步派发，每步 1~3 个 Flash。

所有权与数据流：
- Pro 全链路共用一份 history（SPEC / dispatch / judge / 接管 / 改 SPEC / summary）。
  Flash 每次 assignment 新开 loop。
- 每步验收结论进 last_gate；整个 run 的 verdict 取各步最差的那个。
- 派发粒度固定在「一个 Flash 一步能做完」。空 assignments 或 judge=finish 结束推进。
"""

import asyncio
import json
import time
from dataclasses import replace
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from memory.profile import inject_for_agent, load_profile
from runtime.accept import AcceptResult, NO_HARD_CRITERIA, PASS, sanity_and_run
from runtime.context.disclosure import skill_catalog_block
from runtime.context.notes import append_forget, apply_memory
from runtime.llm import LLMGateway
from runtime.loop import AgentSpec, drop_dangling_tool_calls, run_loop
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.phase import PRO, phase_of
from runtime.persist import (
    EffectLedger,
    MATERIALS_FILE,
    SPEC_FILE,
    audit_offset,
    changed_files_from_audit,
    read_text,
    save_tree,
    write_text,
)
from runtime.remember import (
    applied_from_payload,
    catalog_rules,
    load_applied,
    rules_satisfied,
    save_applied,
)
from runtime.schema_call import (
    SUBMIT_BRIEF,
    SUBMIT_DISPATCH,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SPEC,
    SUBMIT_SUMMARY,
    synthetic_brief,
)
from runtime.scheduler import permission_for_wave, run_wave
from runtime.spec import Assignment, Dispatch, ProjectSpec, render_assignment
from runtime.task import Permission, RuntimeTask, TaskKind, TaskStatus, TaskTree
from runtime.tools import ToolRegistry, build_registry
from tools.sandbox_tools import reset_sandbox_failure_counter, sandbox_convert_to_markdown
from tools.skill_tool import SkillBind
from tools.workspace_utils import iter_workspace_files

EventSink = Callable[[dict[str, Any]], Awaitable[None]]

_PARSEABLE = {".pdf", ".docx", ".pptx"}
_TEXT_EXT = {".md", ".txt", ".py", ".json", ".yml", ".yaml", ".toml", ".csv", ".rst"}
_MATERIALS_CAP = 16000
_BRIEFS_CAP = 12000
_PROGRESS_CAP = 8000
# 一次 lab 最多推进多少步。超过此上限无论 judge 如何都进入 summary。
_MAX_STEPS = 12
# SPEC.md 最多重写几次。用尽后不再 revise_spec，进入 summary。
_MAX_SPEC_REVISIONS = 2


class LabRunner:
    def __init__(
        self,
        settings: RuntimeSettings,
        llm: LLMGateway,
        *,
        registry: ToolRegistry | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry or build_registry()
        self.tracer = tracer
        self.clock = clock
        self._cancel = asyncio.Event()
        self._applied_rules: list[str] | None = None
        self._skill = SkillBind()

    def request_stop(self) -> None:
        self._cancel.set()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _arm(self) -> None:
        """每次 run 开始时解除上一次的停止信号，否则停一次之后永远停着。"""
        self._cancel.clear()

    async def _emit(self, sink: EventSink | None, payload: dict[str, Any]) -> None:
        if sink:
            await sink(payload)

    def _trace_event(self, name: str, **attrs: Any) -> None:
        if self.tracer is not None:
            self.tracer.event(name, **attrs)

    # ── 材料摄取 ────────────────────────────────────────

    async def ingest(self, session_dir: Path) -> str:
        ws = self.settings.workspace_dir
        if not ws.exists():
            write_text(session_dir, MATERIALS_FILE, "# Materials\n\n(empty workspace)\n")
            return read_text(session_dir, MATERIALS_FILE)

        lines = ["# Materials", ""]
        for p in iter_workspace_files(ws, max_files=40):
            rel = p.relative_to(ws)
            suffix = p.suffix.lower()
            lines.append(f"## {rel}")
            if suffix in _PARSEABLE:
                try:
                    lines.append((await sandbox_convert_to_markdown(str(p)))[:12000])
                except Exception as e:
                    lines.append(f"(parse failed: {type(e).__name__}: {e})")
            elif suffix in _TEXT_EXT:
                lines.append("```")
                lines.append(p.read_text(encoding="utf-8", errors="replace")[:8000])
                lines.append("```")
            else:
                lines.append(f"(binary, {p.stat().st_size} bytes)")
            lines.append("")

        text = "\n".join(lines)
        write_text(session_dir, MATERIALS_FILE, text)
        return text

    def _agent_spec(self, kind: TaskKind, permission: Permission) -> AgentSpec:
        """把阶段定义解析成这一拍的 AgentSpec：模型、注入后的 system、工具可见范围。"""
        phase = phase_of(kind)
        model = self.settings.pro_model if phase.agent == PRO else self.settings.flash_model
        include_rules = self._applied_rules is not None
        system = inject_for_agent(
            phase.agent,
            phase.system,
            rules=self._applied_rules or [],
            include_rules=include_rules,
        )
        if phase.agent == PRO and phase.shares_thread:
            catalog = skill_catalog_block()
            if catalog:
                system = f"{system}\n\n{catalog}"
        return AgentSpec(
            name=phase.agent,
            model=model,
            permission=permission,
            system=system,
            submit_tool=phase.submit_tool,
            visible_role=phase.visible_role(permission),
            extra_tools=phase.extra_tools,
            tool_extras={"skill": self._skill} if phase.agent == PRO else {},
        )

    # ── 主流程 ──────────────────────────────────────────

    async def run(
        self,
        question: str,
        session_dir: Path,
        tree: TaskTree,
        *,
        on_event: EventSink | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        tracer = self.tracer
        span_cm = (
            tracer.span(
                S.RUN,
                kind=S.KIND_AGENT,
                inputs=question,
                **{S.ATTR_THREAD_ID: session_dir.name, S.ATTR_TASK_ID: tree.root_id},
            )
            if tracer is not None
            else None
        )
        run_span = span_cm.__enter__() if span_cm is not None else None
        self._arm()
        try:
            result = await self._run_inner(
                question, session_dir, tree, on_event=on_event, resume=resume
            )
            if run_span is not None:
                run_span.set(**{S.ATTR_OUTCOME: result.get("verdict")})
                run_span.output(result.get("summary", "")[:2000])
            return result
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)
            if tracer is not None:
                tracer.flush()

    async def _run_inner(
        self,
        question: str,
        session_dir: Path,
        tree: TaskTree,
        *,
        on_event: EventSink | None,
        resume: bool,
    ) -> dict[str, Any]:
        self._applied_rules = None
        self._skill = SkillBind()
        root = tree.get(tree.root_id)
        if root.status is TaskStatus.PENDING:
            root.transit(TaskStatus.RUNNING)
        save_tree(session_dir, tree)

        reset_sandbox_failure_counter()
        ledger = EffectLedger.load(session_dir, self.settings.workspace_dir)
        materials = read_text(session_dir, MATERIALS_FILE) or await self.ingest(session_dir)

        last_gate = AcceptResult(state=NO_HARD_CRITERIA)
        # 整个 run 的结论取各步里最差的那个：中间失败的一步不能被后面一步洗白
        run_gate = AcceptResult(state=NO_HARD_CRITERIA)
        # 已完成的步骤：assignment id -> brief 摘要。进入后续 dispatch / summary 的 user 上下文。
        progress: list[tuple[str, str]] = []
        # 本次 run 内真正跑过的 id，用于避免验收目录串号
        executed_ids: set[str] = set()
        # 续跑时产物仍完好的 id：Pro 再派到它就直接跳过
        resumable: set[str] = set()
        # Pro 全链路共用；Flash 不走这份。remember_judge 单独开，不写入。
        pro_history: list[dict[str, Any]] = []

        async def run_pro(kind: TaskKind, user: str, label: str) -> dict[str, Any]:
            phase = phase_of(kind)
            permission = phase.node_permission()
            # 并线阶段的指令直接进 transcript，排在既有往来之后；user 槽留空。
            carry = phase.shares_thread
            incoming = pro_history + [{"role": "user", "content": user}] if carry else []
            child = tree.add_child(
                root.task_id,
                kind=kind,
                permission=permission,
                step_budget=self.settings.pro_step_budget,
                deadline=self.clock() + self.settings.task_wall_time_s,
            )
            child.transit(TaskStatus.RUNNING)

            span_cm = (
                self.tracer.span(
                    S.TASK,
                    kind=S.KIND_AGENT,
                    **{
                        S.ATTR_TASK_ID: child.task_id,
                        S.ATTR_TASK_KIND: kind.value,
                        S.ATTR_AGENT: f"pro:{label}",
                    },
                )
                if self.tracer is not None
                else None
            )
            if span_cm is not None:
                span_cm.__enter__()
            try:
                result = await run_loop(
                    child,
                    self._agent_spec(kind, permission),
                    settings=self.settings,
                    llm=self.llm,
                    registry=self.registry,
                    session_dir=session_dir,
                    user_input="" if carry else user,
                    project_spec=read_text(session_dir, SPEC_FILE),
                    history=incoming,
                    on_event=on_event,
                    tracer=self.tracer,
                    clock=self.clock,
                )
            finally:
                if span_cm is not None:
                    span_cm.__exit__(None, None, None)

            if carry:
                # 存回去的 transcript 必须配平：末尾若留着没有 tool 响应的
                # assistant，下一阶段追加的指令会被 drop_dangling_tool_calls 一并截掉。
                pro_history[:] = drop_dangling_tool_calls(result.history)

            if result.submit:
                child.transit(TaskStatus.COMPLETED)
                child.brief = result.submit
            else:
                try:
                    child.transit(TaskStatus.FAILED)
                except ValueError:
                    pass
            save_tree(session_dir, tree)
            return result.submit or {}

        def payload_of(submit: dict[str, Any], name: str) -> dict[str, Any]:
            """名字对得上才取出 payload；对不上返回空 dict，上层不会读到错阶段字段。"""
            if not submit:
                return {}
            if "name" not in submit:
                return submit
            return submit.get("payload", {}) if submit["name"] == name else {}

        # ── SPEC.md ─────────────────────────────────────
        spec_user = (
            f"User request:\n{question}\n\n## {MATERIALS_FILE}\n{materials[:_MATERIALS_CAP]}\n\n"
            "Write SPEC.md for this task and call submit_spec. Decompose the work top-down into "
            "milestones that a single weak worker can each finish in one assignment."
        )

        project = self._resume_spec(tree) if resume else None
        if project is not None:
            await self._emit(on_event, {"kind": "node_done", "node": "spec", "log": [{"resumed": True}]})
        else:
            await self._emit(on_event, {"kind": "node_start", "node": "spec"})
            submit = await run_pro(TaskKind.SPEC, spec_user, "spec")
            project = ProjectSpec.from_payload(payload_of(submit, SUBMIT_SPEC))
            if not project.goal:
                # 没有总纲就没有派发依据，继续往下只会让 worker 对着空规格干活
                await self._emit(
                    on_event, {"kind": "error", "detail": "Pro 未能产出 SPEC.md，任务终止"}
                )
                try:
                    root.transit(TaskStatus.FAILED)
                except ValueError:
                    pass
                save_tree(session_dir, tree)
                return {
                    "verdict": "spec_failed",
                    "summary": "",
                    "knowledge_cards": [],
                    "question": question,
                }
            await self._emit(
                on_event,
                {"kind": "node_done", "node": "spec", "log": [{"milestones": len(project.milestones)}]},
            )
        write_text(session_dir, SPEC_FILE, project.render())
        await self._scope_remember(
            question, session_dir, run_pro, payload_of, on_event, resume=resume
        )

        if resume:
            resumable = {aid for aid in ledger.entries if ledger.satisfied(aid)}
            for aid in sorted(resumable):
                progress.append((aid, "（续跑：产物已存在且未被改动，跳过重跑）"))
            if resumable:
                await self._emit(
                    on_event, {"kind": "resume_skip", "assignments": sorted(resumable)}
                )

        # ── 逐步推进 ────────────────────────────────────
        step = 0
        revisions = 0
        while step < _MAX_STEPS:
            if self._cancelled():
                root.transit(TaskStatus.CANCELLED)
                save_tree(session_dir, tree)
                return {"verdict": "cancelled", "summary": "", "question": question}

            step += 1
            dispatch_user = (
                f"User request: {question}\n\n"
                f"## SPEC.md\n{read_text(session_dir, SPEC_FILE)}\n\n"
                f"## 已完成的步骤\n{self._render_progress(progress)}\n\n"
                f"这是第 {step} 步（最多 {_MAX_STEPS} 步）。决定接下来这一步做什么，调用 submit_dispatch。"
                "只派这一步的活；全部做完时给空的 assignments。"
            )
            await self._emit(on_event, {"kind": "node_start", "node": f"dispatch:{step}"})
            submit = await run_pro(TaskKind.DISPATCH, dispatch_user, f"dispatch{step}")
            dispatch = Dispatch.from_payload(payload_of(submit, SUBMIT_DISPATCH))

            if dispatch.is_empty:
                await self._emit(
                    on_event,
                    {"kind": "node_done", "node": f"dispatch:{step}", "log": [{"assignments": 0}]},
                )
                break

            assignments, skipped = self._partition(dispatch.assignments, resumable, executed_ids, step)
            if skipped:
                await self._emit(on_event, {"kind": "resume_skip", "assignments": skipped})
            await self._emit(
                on_event,
                {
                    "kind": "node_done",
                    "node": f"dispatch:{step}",
                    "log": [
                        {
                            "step_goal": dispatch.step_goal,
                            "assignments": len(assignments),
                            "skipped": len(skipped),
                        }
                    ],
                },
            )
            if not assignments:
                # 本步 assignments 全部命中 resumable，progress 已有摘要，不跑 worker
                continue

            briefs, spec_invalid, last_gate = await self._run_step(
                assignments,
                question=question,
                step_goal=dispatch.step_goal,
                session_dir=session_dir,
                tree=tree,
                root=root,
                ledger=ledger,
                progress=progress,
                on_event=on_event,
            )
            run_gate = self._worst_gate(run_gate, last_gate)

            # ── Judge ───────────────────────────────────
            judge_user = (
                f"User request: {question}\nStep goal: {dispatch.step_goal}\n"
                f"Gate: {last_gate.state}\n"
                f"Briefs:\n{json.dumps(briefs, ensure_ascii=False)[:_BRIEFS_CAP]}\n"
                f"## Applicable /remember rules\n{self._remember_block()}\n"
                "If the gate is no_hard_criteria you MUST say so in evidence and give a semantic rationale. "
                "Fill rule_verdicts for every applicable /remember rule. finish only when all are satisfied."
            )
            await self._emit(on_event, {"kind": "node_start", "node": "judge"})
            verdict = payload_of(
                await run_pro(TaskKind.JUDGE, judge_user, "judge"),
                SUBMIT_JUDGE,
            )
            decision = str(verdict.get("decision") or "continue")
            if decision == "finish" and not rules_satisfied(self._applied_rules or [], verdict):
                decision = "continue"

            apply_memory(
                session_dir,
                replace=verdict.get("memory_replace"),
                remove=verdict.get("memory_remove"),
                append=str(verdict.get("memory_append") or ""),
            )
            append_forget(session_dir, str(verdict.get("forget_append") or ""))

            self._trace_event(
                S.EV_DECISION,
                **{
                    S.ATTR_DECISION: decision,
                    S.ATTR_GATE_STATE: last_gate.state,
                    S.ATTR_REASON: str(verdict.get("evidence") or "")[:500],
                },
            )
            await self._emit(
                on_event, {"kind": "node_done", "node": "judge", "log": [{"decision": decision}]}
            )

            if decision == "finish":
                break

            if decision == "takeover":
                self._trace_event(S.EV_HANDOFF, **{S.ATTR_FROM: "flash", S.ATTR_TO: "pro"})
                await self._emit(on_event, {"kind": "pro_takeover", "reason": "judge"})
                take = await run_pro(
                    TaskKind.TAKEOVER,
                    f"Take over this step yourself.\nUser: {question}\n"
                    f"Step goal: {dispatch.step_goal}\n"
                    f"Briefs: {json.dumps(briefs, ensure_ascii=False)[:8000]}",
                    "takeover",
                )
                payload = payload_of(take, SUBMIT_BRIEF)
                if payload.get("brief"):
                    progress.append((f"step{step}-pro", str(payload["brief"])))
                    # 接管也写了文件，门禁必须重跑，不能沿用 worker 那次的结论
                    for a in assignments:
                        last_gate = await self._gate(session_dir, a.gate_id)
                        if not last_gate.is_pass:
                            break
                else:
                    # 接管未完成（预算耗尽/被判停）：progress 记成待办，
                    # 否则下一步 dispatch 会把它当成已完成。
                    progress.append(
                        (f"step{step}-pro", "Pro 接管未完成本步，该工作仍然待办")
                    )
                continue

            if decision == "revise_spec" or spec_invalid:
                if revisions >= _MAX_SPEC_REVISIONS:
                    self._trace_event(
                        S.EV_DECISION,
                        **{S.ATTR_DECISION: "stop", S.ATTR_REASON: "spec_revision_budget"},
                    )
                    await self._emit(
                        on_event, {"kind": "spec_revision_exhausted", "limit": _MAX_SPEC_REVISIONS}
                    )
                    break
                revisions += 1
                redo = await run_pro(
                    TaskKind.SPEC,
                    spec_user
                    + "\n\nThe current SPEC.md was rejected. Revise it given these briefs:\n"
                    + json.dumps(briefs, ensure_ascii=False)[:8000],
                    "revise_spec",
                )
                project = ProjectSpec.from_payload(payload_of(redo, SUBMIT_SPEC))
                write_text(session_dir, SPEC_FILE, project.render())
                continue

        # ── Summary ─────────────────────────────────────
        summary_user = (
            f"The lab is complete. You are still Pro — write the wrap-up from this conversation "
            f"and the artifacts below.\n\n"
            f"User request: {question}\n\n## SPEC.md\n{read_text(session_dir, SPEC_FILE)}\n\n"
            f"## 完成情况\n{self._render_progress(progress)}\n\n"
            f"Gate: {run_gate.state}\n"
            "Call submit_summary. If the gate was no_hard_criteria, say so in user_summary."
        )
        await self._emit(on_event, {"kind": "node_start", "node": "summary"})
        payload = payload_of(
            await run_pro(TaskKind.SUMMARY, summary_user, "summary"),
            SUBMIT_SUMMARY,
        )
        summary_text = str(payload.get("user_summary") or "")
        if summary_text:
            self.settings.workspace_dir.mkdir(parents=True, exist_ok=True)
            (self.settings.workspace_dir / "SUMMARY.md").write_text(summary_text, encoding="utf-8")
        await self._emit(on_event, {"kind": "node_done", "node": "summary", "log": []})

        try:
            root.transit(TaskStatus.COMPLETED)
        except ValueError:
            pass
        save_tree(session_dir, tree)
        return {
            "verdict": run_gate.state,
            "summary": summary_text,
            "knowledge_cards": list(payload.get("knowledge_cards") or []),
            "question": question,
        }

    # ── 一步内的派发 ────────────────────────────────────

    async def _run_step(
        self,
        assignments: list[Assignment],
        *,
        question: str,
        step_goal: str,
        session_dir: Path,
        tree: TaskTree,
        root: RuntimeTask,
        ledger: EffectLedger,
        progress: list[tuple[str, str]],
        on_event: EventSink | None,
    ) -> tuple[list[dict[str, Any]], bool, AcceptResult]:
        permission = permission_for_wave(len(assignments))
        workers = [
            tree.add_child(
                root.task_id,
                kind=TaskKind.WORKER,
                permission=permission,
                step_budget=self.settings.flash_step_budget,
                deadline=self.clock() + self.settings.task_wall_time_s,
                node_spec=a.to_dict(),
            )
            for a in assignments
        ]
        save_tree(session_dir, tree)

        domains = [a.domain or a.goal for a in assignments]
        done_so_far = list(progress)

        span_cm = (
            self.tracer.span(
                S.STEP,
                kind=S.KIND_CHAIN,
                **{
                    S.ATTR_WAVE_SIZE: len(workers),
                    S.ATTR_PERMISSION: permission.value,
                    S.ATTR_STEP_GOAL: step_goal,
                },
            )
            if self.tracer is not None
            else None
        )
        if span_cm is not None:
            span_cm.__enter__()
        try:
            results = await run_wave(
                workers,
                lambda w: self._run_worker(
                    w,
                    question=question,
                    step_goal=step_goal,
                    session_dir=session_dir,
                    tree=tree,
                    ledger=ledger,
                    done_so_far=done_so_far,
                    domains=domains,
                    on_event=on_event,
                ),
                self.settings,
                tracer=self.tracer,
            )
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

        briefs = []
        spec_invalid = False
        for worker, brief in results:
            briefs.append(brief)
            aid = str((worker.node_spec or {}).get("id") or "")
            if not aid:
                continue
            if brief.get("outcome") == "spec_invalid":
                spec_invalid = True
            progress.append((aid, str(brief.get("brief") or "")))

        return briefs, spec_invalid, self._step_gate(results)

    async def _run_worker(
        self,
        worker: RuntimeTask,
        *,
        question: str,
        step_goal: str,
        session_dir: Path,
        tree: TaskTree,
        ledger: EffectLedger,
        done_so_far: list[tuple[str, str]],
        domains: list[str],
        on_event: EventSink | None,
    ) -> dict[str, Any]:
        if worker.status is TaskStatus.PENDING:
            worker.transit(TaskStatus.RUNNING)
        # 只统计这个 worker 自己写过的文件，不要把整个会话的写入都算给它
        audit_mark = audit_offset(self.settings.workspace_dir)

        assignment = Assignment.from_payload(worker.node_spec or {})
        others = [d for d in domains if d and d != (assignment.domain or assignment.goal)]
        prompt = render_assignment(
            assignment,
            user_request=question,
            project_spec=read_text(session_dir, SPEC_FILE),
            step_goal=step_goal,
            done_so_far=done_so_far,
            parallel_domains=others if len(domains) > 1 else None,
        )

        span_cm = (
            self.tracer.span(
                S.TASK,
                kind=S.KIND_AGENT,
                inputs=prompt,
                **{
                    S.ATTR_TASK_ID: worker.task_id,
                    S.ATTR_TASK_KIND: TaskKind.WORKER.value,
                    S.ATTR_ASSIGNMENT_ID: assignment.id,
                    S.ATTR_DOMAIN: assignment.domain,
                    S.ATTR_AGENT: f"flash:{assignment.id}",
                    S.ATTR_PERMISSION: worker.permission.value,
                },
            )
            if self.tracer is not None
            else None
        )
        task_span = span_cm.__enter__() if span_cm is not None else None
        try:
            await self._emit(on_event, {"kind": "node_start", "node": f"flash:{assignment.id}"})
            try:
                result = await asyncio.wait_for(
                    run_loop(
                        worker,
                        self._agent_spec(TaskKind.WORKER, worker.permission),
                        settings=self.settings,
                        llm=self.llm,
                        registry=self.registry,
                        session_dir=session_dir,
                        user_input=prompt,
                        project_spec="",
                        on_event=on_event,
                        tracer=self.tracer,
                        clock=self.clock,
                    ),
                    timeout=max(1.0, worker.deadline - self.clock()),
                )
                brief = result.brief or synthetic_brief(
                    outcome="failed", brief=result.reason or "no brief"
                )
            except asyncio.TimeoutError:
                brief = synthetic_brief(
                    outcome="failed",
                    brief="Your assignment was stopped because the execution budget was exhausted.",
                    changed_files=changed_files_from_audit(
                        self.settings.workspace_dir, since=audit_mark
                    ),
                )
                try:
                    worker.transit(TaskStatus.CANCELLED)
                except ValueError:
                    pass
                worker.brief = brief
                # 超时留下的是半成品：撤掉账本条目，否则续跑会把它当成已完成
                ledger.drop(assignment.id)
                save_tree(session_dir, tree)
                return brief

            brief["changed_files"] = brief.get("changed_files") or changed_files_from_audit(
                self.settings.workspace_dir, since=audit_mark
            )
            gate = await self._gate(session_dir, assignment.gate_id)
            brief["tests"] = gate.as_tests()
            worker.brief = brief

            succeeded = brief.get("outcome") == "done" and gate.state in {PASS, NO_HARD_CRITERIA}
            if succeeded:
                ledger.record(assignment.id, brief["changed_files"])
            else:
                ledger.drop(assignment.id)

            try:
                if succeeded:
                    worker.transit(TaskStatus.COMPLETED)
                elif worker.status is TaskStatus.RUNNING:
                    worker.transit(TaskStatus.FAILED)
            except ValueError:
                pass

            if task_span is not None:
                task_span.set(
                    **{S.ATTR_OUTCOME: brief.get("outcome"), S.ATTR_GATE_STATE: gate.state}
                )
                task_span.output(str(brief.get("brief") or "")[:2000])

            await self._emit(
                on_event,
                {
                    "kind": "worker_brief",
                    "assignment_id": assignment.id,
                    "domain": assignment.domain,
                    "outcome": brief.get("outcome"),
                    "brief": brief.get("brief"),
                    "tests": brief.get("tests"),
                    "gate": gate.state,
                },
            )
            save_tree(session_dir, tree)
            return brief
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    def _remember_block(self) -> str:
        rules = self._applied_rules or []
        if not rules:
            return "（本 lab 没有适用的 /remember 规则）"
        return "\n".join(f"- {r}" for r in rules)

    async def _scope_remember(
        self,
        question: str,
        session_dir: Path,
        run_pro: Callable[..., Awaitable[dict[str, Any]]],
        payload_of: Callable[[dict[str, Any], str], dict[str, Any]],
        on_event: EventSink | None,
        *,
        resume: bool,
    ) -> None:
        catalog = catalog_rules(load_profile())
        if resume:
            existing = load_applied(session_dir)
            if existing is not None:
                self._applied_rules = existing
                return
        if not catalog:
            self._applied_rules = []
            save_applied(session_dir, [])
            return
        await self._emit(on_event, {"kind": "node_start", "node": "remember_judge"})
        listed = "\n".join(f"- {rule}" for rule in catalog)
        submit = await run_pro(
            TaskKind.REMEMBER_JUDGE,
            (
                f"User request:\n{question}\n\n## SPEC.md\n{read_text(session_dir, SPEC_FILE)}\n\n"
                f"## /remember rules\n{listed}\n\nCall submit_remember."
            ),
            "remember",
        )
        self._applied_rules = applied_from_payload(catalog, payload_of(submit, SUBMIT_REMEMBER))
        save_applied(session_dir, self._applied_rules)
        await self._emit(
            on_event,
            {
                "kind": "node_done",
                "node": "remember_judge",
                "log": [{"applicable": len(self._applied_rules)}],
            },
        )

    async def _gate(self, session_dir: Path, assignment_id: str) -> AcceptResult:
        span_cm = (
            self.tracer.span(
                S.ACCEPT, kind=S.KIND_EVALUATOR, **{S.ATTR_ASSIGNMENT_ID: assignment_id}
            )
            if self.tracer is not None
            else None
        )
        span = span_cm.__enter__() if span_cm is not None else None
        try:
            gate = await sanity_and_run(session_dir, assignment_id, settings=self.settings)
            if span is not None:
                span.set(
                    **{
                        S.ATTR_GATE_STATE: gate.state,
                        S.ATTR_GATE_PASSED: gate.passed,
                        S.ATTR_GATE_FAILED: gate.failed,
                        S.ATTR_GATE_EXIT: gate.exit_code,
                    }
                )
                span.output(gate.log[-1000:])
            return gate
        finally:
            if span_cm is not None:
                span_cm.__exit__(None, None, None)

    # ── 辅助 ────────────────────────────────────────────

    @staticmethod
    def _partition(
        assignments: list[Assignment],
        resumable: set[str],
        executed: set[str],
        step: int,
    ) -> tuple[list[Assignment], list[str]]:
        """把 assignments 分成「本步要跑」和「续跑可跳过」；跑的那组 id 在本次 run 内唯一。

        resumable 命中：产物指纹仍完好，跳过重跑，已落地的文件不被覆盖。
        去重只改 executed 里出现过的 id：账本以 id 为键，给 resumable 候选改名会让
        satisfied() 永远对不上。
        """
        run: list[Assignment] = []
        skipped: list[str] = []
        for i, a in enumerate(assignments):
            original = a.id or f"s{step}_{i}"
            if original in resumable:
                resumable.discard(original)
                skipped.append(original)
                continue

            aid = original
            suffix = 0
            while aid in executed:
                suffix += 1
                aid = f"{original}_s{step}" if suffix == 1 else f"{original}_s{step}_{suffix}"
            executed.add(aid)

            # 改名只影响账本与 span 的键；验收目录仍按 Pro 写测试时用的原名查找
            run.append(replace(a, id=aid, acceptance_id=a.gate_id or original))
        return run, skipped

    @staticmethod
    def _render_progress(progress: list[tuple[str, str]]) -> str:
        if not progress:
            return "（还没有完成任何步骤）"
        lines = [f"- **{aid}**：{summary}" for aid, summary in progress if summary]
        return "\n".join(lines)[-_PROGRESS_CAP:] or "（还没有完成任何步骤）"

    @staticmethod
    def _worst_gate(a: AcceptResult, b: AcceptResult) -> AcceptResult:
        order = {PASS: 1, NO_HARD_CRITERIA: 2, "test_invalid": 3, "fail": 4}
        return b if order.get(b.state, 0) > order.get(a.state, 0) else a

    @staticmethod
    def _step_gate(results: list[tuple[RuntimeTask, dict[str, Any]]]) -> AcceptResult:
        """整步的门禁结论：有 fail 取 fail，全无硬指标取 no_hard_criteria。"""
        states = [
            str((brief.get("tests") or {}).get("state") or NO_HARD_CRITERIA)
            for _, brief in results
        ]
        for priority in ("fail", "test_invalid", PASS):
            if priority in states:
                return AcceptResult(state=priority)
        return AcceptResult(state=NO_HARD_CRITERIA)

    @staticmethod
    def _resume_spec(tree: TaskTree) -> ProjectSpec | None:
        """从已 COMPLETED 的 SPEC 节点取出 payload。没有可用 goal 时返回 None，走新起草路径。"""
        for task in tree.nodes.values():
            if task.kind is not TaskKind.SPEC or task.status is not TaskStatus.COMPLETED:
                continue
            brief = task.brief or {}
            payload = brief.get("payload") if brief.get("name") == SUBMIT_SPEC else brief
            if payload and payload.get("goal"):
                return ProjectSpec.from_payload(payload)
        return None
````

### `runtime/tools.py`

**覆盖。** 工具注册与权限门。load_skill 仅 Pro。read_file 走 offset/limit。memory_grep 按 DumpScope.extra_roots。

<!-- PACKFILE: runtime/tools.py -->
```python
"""Tool registry：可见工具表、执行、以及归一化后的 ToolOutcome。

for_role 产出的收窄 registry 同时是可见性边界和执行边界：不在表里的名字调不动，
模型从 history 里翻出别处的工具名照抄也会被挡下。收窄依据由 runtime.phase 给，
本模块不认识阶段。普通工具的 schema 只用于广告字段和缺参提示，额外字段放行；
submit_* 的结构化约束在 loop 里由 validate_payload 执行。error_class 从正文前缀
或异常类型得出。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass, classify
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.schema_call import SCHEMA_TOOLS, openai_tool
from tools.policy import get_auditor

Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: frozenset[str]
    handler: Handler


@dataclass
class ToolContext:
    role: str
    settings: RuntimeSettings
    session_dir: Any
    llm: Any
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    text: str
    error_class: ErrorClass = ErrorClass.OK


def _fields(*required: str, extra: bool = False, **props: str) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {k: {"type": t} for k, t in props.items()},
    }
    if required:
        schema["required"] = list(required)
    if extra:
        schema["additionalProperties"] = True
    return schema


def _advertise(schema: dict[str, Any]) -> dict[str, Any]:
    advertised = {**schema, "additionalProperties": True}
    advertised.pop("required", None)
    return advertised


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_args(schema: dict[str, Any], args: dict[str, Any]) -> str:
    """按 schema.required / integer 属性给出缺参提示，不拦截额外字段。"""
    miss = [k for k in (schema.get("required") or []) if _blank(args.get(k))]
    if miss:
        return f"[ERROR/Validation] missing {', '.join(miss)}"
    for key, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "integer":
            continue
        raw = args.get(key)
        if raw in (None, ""):
            continue
        try:
            args[key] = int(raw)
        except (TypeError, ValueError):
            return f"[ERROR/Validation] {key} must be an integer"
    return ""


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {s.name: s for s in specs}

    def for_role(
        self, role: str, submit_tool: str, extra: frozenset[str] = frozenset()
    ) -> "ToolRegistry":
        """本拍实际可见的工具：role 允许的，加 extra 点名的，加唯一出口 submit_tool。

        role / extra 由 runtime.phase 决定，本模块不认识阶段。收窄后的这份
        registry 同时用于 execute，所以不在表里的名字调不动。
        """
        specs = [
            s
            for s in self._specs.values()
            if (role in s.permissions or s.name in extra or s.name == submit_tool)
            and (s.name not in SCHEMA_TOOLS or s.name == submit_tool)
        ]
        return ToolRegistry(specs)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            openai_tool(
                s.name,
                s.description,
                s.input_schema if s.name in SCHEMA_TOOLS else _advertise(s.input_schema),
            )
            for s in self._specs.values()
        ]

    def names(self) -> set[str]:
        return set(self._specs)

    async def execute(
        self,
        name: str,
        raw_args: str | dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float,
        tracer: Tracer | None = None,
    ) -> ToolOutcome:
        if tracer is None:
            return await self._execute(name, raw_args, ctx, timeout=timeout, tracer=None)
        with tracer.span(
            S.TOOL,
            kind=S.KIND_TOOL,
            inputs=raw_args if isinstance(raw_args, dict) else str(raw_args)[:2000],
            **{S.ATTR_TOOL: name, S.ATTR_PERMISSION: ctx.role},
        ) as span:
            outcome = await self._execute(name, raw_args, ctx, timeout=timeout, tracer=tracer)
            span.set(**{S.ATTR_ERROR_CLASS: outcome.error_class.value})
            span.output(outcome.text[:2000])
            return outcome

    async def _execute(
        self,
        name: str,
        raw_args: str | dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float,
        tracer: Tracer | None,
    ) -> ToolOutcome:
        spec = self._specs.get(name)
        if spec is None:
            # 本阶段工具表里没有。列出可用名字，避免模型照着 history 里的旧工具反复重试。
            return ToolOutcome(
                f"[ERROR/Validation] tool {name} is not available at this stage; "
                f"available: {', '.join(sorted(self._specs))}",
                ErrorClass.VALIDATION,
            )
        if ctx.role not in spec.permissions and name not in SCHEMA_TOOLS:
            get_auditor().record(name, {}, f"denied:role={ctx.role}")
            if tracer is not None:
                tracer.event(S.GUARDRAIL, **{S.ATTR_TOOL: name, S.ATTR_TOOL_ALLOWED: False})
            return ToolOutcome(
                f"[ERROR/PermissionError] tool {name} is not allowed for role={ctx.role}",
                ErrorClass.PERMISSION,
            )
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as e:
                return ToolOutcome(f"[ERROR/Validation] invalid json: {e}", ErrorClass.VALIDATION)
        else:
            args = raw_args
        if not isinstance(args, dict):
            return ToolOutcome("[ERROR/Validation] arguments must be an object", ErrorClass.VALIDATION)
        if name not in SCHEMA_TOOLS:
            hint = _check_args(spec.input_schema, args)
            if hint:
                return ToolOutcome(hint, ErrorClass.VALIDATION)

        ctx.extras["submit_name"] = name
        try:
            text = await asyncio.wait_for(spec.handler(args, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            get_auditor().record(name, args, "error:timeout")
            return ToolOutcome(f"[TIMEOUT after {timeout}s] tool {name}", ErrorClass.TRANSIENT)
        except PermissionError as e:
            get_auditor().record(name, args, f"denied:{e}")
            return ToolOutcome(f"[ERROR/PermissionError] {e}", ErrorClass.PERMISSION)
        except Exception as e:
            get_auditor().record(name, args, f"error:{type(e).__name__}")
            return ToolOutcome(f"[ERROR/{type(e).__name__}] {e}", classify(e))

        if "[SANDBOX_UNREACHABLE]" in text:
            return ToolOutcome(text, ErrorClass.FATAL)
        if text.startswith("[ERROR/PermissionError]"):
            return ToolOutcome(text, ErrorClass.PERMISSION)
        if text.startswith("[ERROR/Validation]"):
            return ToolOutcome(text, ErrorClass.VALIDATION)
        if text.startswith("[TIMEOUT"):
            return ToolOutcome(text, ErrorClass.TRANSIENT)
        if text.startswith("[ERROR/"):
            cls = classify(None, text=text)
            return ToolOutcome(text, ErrorClass.LOGIC if cls is ErrorClass.OK else cls)
        get_auditor().record(name, args, "ok")
        from runtime.context.compact import DumpScope, LIVE_OFFLOAD_CHARS, dump_tool_result

        dump = ctx.extras.get("dump")
        if (
            isinstance(dump, DumpScope)
            and len(text) > LIVE_OFFLOAD_CHARS
        ):
            text = dump_tool_result(
                dump,
                tool=name,
                call_id=str(ctx.extras.get("tool_call_id") or ""),
                body=text,
            )
        return ToolOutcome(text, ErrorClass.OK)


def build_base_specs() -> list[ToolSpec]:
    from memory.retrieve import memory_grep, memory_read, memory_search
    from runtime.accept import write_acceptance_file
    from runtime.schema_call import (
        ACCEPT_FILE_SCHEMA,
        BRIEF_SCHEMA,
        DISPATCH_SCHEMA,
        JUDGE_SCHEMA,
        REMEMBER_SCHEMA,
        SPEC_SCHEMA,
        SUMMARY_SCHEMA,
        SUBMIT_BRIEF,
        SUBMIT_DISPATCH,
        SUBMIT_JUDGE,
        SUBMIT_REMEMBER,
        SUBMIT_SPEC,
        SUBMIT_SUMMARY,
        WRITE_ACCEPTANCE,
    )
    from tools.fs_tools import glob_files, grep_files, host_bash, list_dir, patch_file, read_file, write_file
    from tools.profile_tool import read_profile
    from tools.sandbox_tools import call_sandbox, sandbox_convert_to_markdown
    from tools.search_tool import web_search
    from tools.skill_tool import (
        LOAD_SKILL,
        LOAD_SKILL_REFERENCE,
        USE_SKILL_SCRIPT,
        SkillBind,
        load_skill_reference,
        use_skill_script,
    )

    async def h_read(args: dict[str, Any], ctx: ToolContext) -> str:
        limit = args.get("limit")
        return read_file(
            args["path"],
            int(args.get("offset") or 1),
            None if limit in (None, "") else int(limit),
        )

    async def h_write(args: dict[str, Any], ctx: ToolContext) -> str:
        return write_file(args["path"], args["content"], role=ctx.role)

    async def h_list(args: dict[str, Any], ctx: ToolContext) -> str:
        r = list_dir(args.get("path") or ".")
        return r if isinstance(r, str) else "\n".join(r)

    async def h_patch(args: dict[str, Any], ctx: ToolContext) -> str:
        return patch_file(args["path"], args["old"], args["new"], role=ctx.role)

    async def h_glob(args: dict[str, Any], ctx: ToolContext) -> str:
        return "\n".join(glob_files(args["pattern"], int(args.get("limit") or 80)))

    async def h_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        return grep_files(args["pattern"], args.get("glob") or "**/*", int(args.get("limit") or 40))

    async def h_bash(args: dict[str, Any], ctx: ToolContext) -> str:
        return host_bash(args["cmd"], int(args.get("timeout") or 30))

    async def h_search(args: dict[str, Any], ctx: ToolContext) -> str:
        hits = await web_search(args["query"], int(args.get("max_results") or 5))
        return json.dumps(hits, ensure_ascii=False) if hits else "(no matches)"

    def _bind(ctx: ToolContext) -> SkillBind:
        slot = ctx.extras.get("skill")
        if not isinstance(slot, SkillBind):
            slot = SkillBind()
            ctx.extras["skill"] = slot
        return slot

    async def h_load_skill(args: dict[str, Any], ctx: ToolContext) -> str:
        return _bind(ctx).load(str(args.get("skill_name") or ""))

    async def h_skill_ref(args: dict[str, Any], ctx: ToolContext) -> str:
        err = _bind(ctx).gate(str(args.get("skill_name") or ""))
        if err:
            return err
        return load_skill_reference(args["skill_name"], args["ref_name"])

    async def h_skill_script(args: dict[str, Any], ctx: ToolContext) -> str:
        err = _bind(ctx).gate(str(args.get("skill_name") or ""))
        if err:
            return err
        return use_skill_script(args["skill_name"], args["script_name"])

    async def h_mem_search(args: dict[str, Any], ctx: ToolContext) -> str:
        return await memory_search(args["query"], int(args.get("k") or 5), ctx.llm, ctx.settings)

    async def h_mem_grep(args: dict[str, Any], ctx: ToolContext) -> str:
        from runtime.context.compact import DumpScope

        dump = ctx.extras.get("dump")
        extra = (
            dump.grep_roots(ctx.settings.workspace_dir / ".labhandler")
            if isinstance(dump, DumpScope)
            else None
        )
        return memory_grep(args["pattern"], ctx.settings, extra_roots=extra)

    async def h_mem_read(args: dict[str, Any], ctx: ToolContext) -> str:
        return memory_read(args["path"], ctx.settings)

    async def h_profile(args: dict[str, Any], ctx: ToolContext) -> str:
        return json.dumps(read_profile(), ensure_ascii=False)

    async def h_accept(args: dict[str, Any], ctx: ToolContext) -> str:
        path = write_acceptance_file(
            ctx.session_dir, args["task_id"], args["filename"], args["content"], role=ctx.role
        )
        return f"wrote acceptance file {path.name} for task {args['task_id']}"

    async def h_submit(args: dict[str, Any], ctx: ToolContext) -> str:
        ctx.extras["submit"] = {"name": ctx.extras.get("submit_name"), "payload": args}
        return "submitted"

    async def h_convert(args: dict[str, Any], ctx: ToolContext) -> str:
        return await sandbox_convert_to_markdown(args["file_path"])

    def h_sandbox(tool: str) -> Handler:
        async def run(args: dict[str, Any], ctx: ToolContext) -> str:
            return await call_sandbox(tool, **args)

        return run

    readonly = frozenset({"readonly", "write", "pro"})
    write = frozenset({"write", "pro"})
    pro = frozenset({"pro"})

    specs = [
        ToolSpec(
            "read_file",
            "Read a workspace text file. path is relative to workspace, including offloaded tool_results. "
            "offset is a 1-based line number; limit is max lines. If truncated, call again with next offset.",
            _fields("path", path="string", offset="integer", limit="integer"),
            readonly,
            h_read,
        ),
        ToolSpec("list_dir", "List a workspace directory (non-recursive).", _fields(path="string"), readonly, h_list),
        ToolSpec(
            "glob_files",
            "Glob workspace files. Always supply a tight pattern; results are capped.",
            _fields("pattern", pattern="string", limit="integer"),
            readonly,
            h_glob,
        ),
        ToolSpec(
            "grep_files",
            "Regex search workspace files. Empty result is an observation, not a failure.",
            _fields("pattern", pattern="string", glob="string", limit="integer"),
            readonly,
            h_grep,
        ),
        ToolSpec(
            "web_search",
            "DuckDuckGo search. Empty hits are an observation.",
            _fields("query", query="string", max_results="integer"),
            readonly,
            h_search,
        ),
        ToolSpec(
            "memory_search",
            "Vector-search archived knowledge cards.",
            _fields("query", query="string", k="integer"),
            readonly,
            h_mem_search,
        ),
        ToolSpec(
            "memory_grep",
            "Regex search knowledge cards and allowed dumps (Flash: this assignment only; Pro: whole .labhandler).",
            _fields("pattern", pattern="string"),
            readonly,
            h_mem_grep,
        ),
        ToolSpec(
            "memory_read",
            "Read one card, session note, or tool_results dump.",
            _fields("path", path="string"),
            readonly,
            h_mem_read,
        ),
        ToolSpec(
            LOAD_SKILL,
            "Pull one skill SOP this lab. Skills are mutually exclusive; skip if none fits.",
            _fields("skill_name", skill_name="string"),
            pro,
            h_load_skill,
        ),
        ToolSpec(
            LOAD_SKILL_REFERENCE,
            "Read skills/<name>/references/<ref_name> for the skill already bound by load_skill.",
            _fields("skill_name", "ref_name", skill_name="string", ref_name="string"),
            pro,
            h_skill_ref,
        ),
        ToolSpec("read_profile", "Read profile/me.yaml.", _fields(), readonly, h_profile),
        ToolSpec(
            "write_file",
            "Overwrite a workspace text file.",
            _fields("path", "content", path="string", content="string"),
            write,
            h_write,
        ),
        ToolSpec(
            "patch_file",
            "Exact unique string replacement in a workspace file.",
            _fields("path", "old", "new", path="string", old="string", new="string"),
            write,
            h_patch,
        ),
        ToolSpec(
            "host_bash",
            "Run a whitelisted bash command in the host workspace.",
            _fields("cmd", cmd="string", timeout="integer"),
            write,
            h_bash,
        ),
        ToolSpec(
            USE_SKILL_SCRIPT,
            "Copy a script of the bound skill into workspace/.labhandler/scripts for sandbox execution.",
            _fields("skill_name", "script_name", skill_name="string", script_name="string"),
            pro,
            h_skill_script,
        ),
        ToolSpec(
            "sandbox_convert_to_markdown",
            "Parse PDF/DOCX/PPT inside the sandbox into markdown.",
            _fields("file_path", file_path="string"),
            readonly,
            h_convert,
        ),
        ToolSpec(
            "sandbox_execute_bash",
            "Run a bash command inside the sandbox (/workspace).",
            _fields("command", command="string"),
            write,
            h_sandbox("sandbox_execute_bash"),
        ),
        ToolSpec(
            "sandbox_execute_code",
            "Run code inside the sandbox Jupyter kernel. Prefer bash for sync results.",
            _fields("code", code="string"),
            write,
            h_sandbox("sandbox_execute_code"),
        ),
        ToolSpec(
            "sandbox_file_operations",
            "Sandbox file operations (path translated to /workspace).",
            _fields(path="string", extra=True),
            write,
            h_sandbox("sandbox_file_operations"),
        ),
        ToolSpec(WRITE_ACCEPTANCE, "Write one acceptance test file (Pro only).", ACCEPT_FILE_SCHEMA, pro, h_accept),
        ToolSpec(SUBMIT_SPEC, "Submit SPEC.md: the top-down specification governing the whole task.", SPEC_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_DISPATCH, "Submit the assignments for the next single step. Empty assignments means the work is done.", DISPATCH_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_JUDGE, "Submit the judge decision.", JUDGE_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_REMEMBER, "Submit which /remember rules apply to this lab.", REMEMBER_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_SUMMARY, "Submit SUMMARY.md plus knowledge cards.", SUMMARY_SCHEMA, pro, h_submit),
        ToolSpec(SUBMIT_BRIEF, "Submit the worker brief. This is the only Flash→Pro exit.", BRIEF_SCHEMA, write | readonly | pro, h_submit),
    ]
    return specs


def build_registry() -> ToolRegistry:
    return ToolRegistry(build_base_specs())
```

### `runtime/schema_call.py`

**覆盖。** 各阶段结构化出口 schema：submit_spec / dispatch / brief / judge / summary / remember。

<!-- PACKFILE: runtime/schema_call.py -->
```python
"""submit_* / write_acceptance 的 JSON schema 与校验。"""

import json
from typing import Any

import jsonschema

from runtime.context.notes import MEMORY_ENTRY_MAX
from runtime.spec import MAX_ASSIGNMENTS

# Flash brief 只含本步实际改动与碰到的错误，长度上限 BRIEF_MAX。
BRIEF_MAX = 600

SUBMIT_SPEC = "submit_spec"
SUBMIT_DISPATCH = "submit_dispatch"
WRITE_ACCEPTANCE = "write_acceptance"
SUBMIT_BRIEF = "submit_brief"
SUBMIT_JUDGE = "submit_judge"
SUBMIT_REMEMBER = "submit_remember"
SUBMIT_SUMMARY = "submit_summary"
SUBMIT_DREAM = "submit_dream"
SUBMIT_SKILL_EDIT = "submit_skill_edit"

# 结构化出口：无副作用，能否调用由 for_role 的工具表决定。
# write_acceptance 写文件，不在此集合，必须走 check_write。
SCHEMA_TOOLS = {
    SUBMIT_SPEC,
    SUBMIT_DISPATCH,
    SUBMIT_BRIEF,
    SUBMIT_JUDGE,
    SUBMIT_REMEMBER,
    SUBMIT_SUMMARY,
    SUBMIT_DREAM,
    SUBMIT_SKILL_EDIT,
}

INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "验收代码会按这个名字导入/调用，必须精确"},
        "kind": {"type": "string", "enum": ["function", "class", "cli", "file", "http"]},
        "signature": {"type": "string", "description": "如 solve(nums: list[int]) -> int"},
        "description": {"type": "string"},
    },
}

SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "milestones"],
    "properties": {
        "goal": {"type": "string", "minLength": 8, "description": "整个任务的总目标"},
        "overview": {"type": "string", "description": "打算怎么做，方案层面的概述"},
        "interfaces": {
            "type": "array",
            "items": INTERFACE_SCHEMA,
            "description": "全局固定的名字，任何一步都不得改动",
        },
        "deliverables": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance_strategy": {"type": "string", "description": "整体打算怎么验收"},
        "milestones": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": "自顶向下的推进顺序，每条是一小步，不是整个项目",
        },
    },
    "additionalProperties": True,
}

MIN_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string"},
        "milestones": {"type": "array", "items": {"type": "string"}},
    },
}

ASSIGNMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "domain", "goal", "testable"],
    "properties": {
        "id": {"type": "string", "description": "本次派发内唯一"},
        "domain": {
            "type": "string",
            "description": "这个 worker 负责的领域。同一次派发中各不相同、互不重叠",
        },
        "goal": {"type": "string", "minLength": 8, "description": "一个 worker 一步能做完的量"},
        "spec": {"type": "string", "description": "写给这个 worker 的详细任务书正文"},
        "expected_artifacts": {"type": "array", "items": {"type": "string"}},
        "interfaces": {
            "type": "array",
            "items": INTERFACE_SCHEMA,
            "description": "该 worker 必须实现的确切名字。testable 为 true 时必填",
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "done_when": {"type": "array", "items": {"type": "string"}},
        "testable": {
            "type": "boolean",
            "description": "有可量化指标且产物可被程序检验时为 true",
        },
        "acceptance_intent": {"type": "string"},
        "acceptance_files": {"type": "array", "items": {"type": "string"}},
    },
}

DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["step_goal", "assignments"],
    "properties": {
        "step_goal": {"type": "string", "description": "这一步要推进什么"},
        "assignments": {
            "type": "array",
            "maxItems": MAX_ASSIGNMENTS,
            "items": ASSIGNMENT_SCHEMA,
            "description": f"本步派出的 worker，最多 {MAX_ASSIGNMENTS} 个。全部完成时给空数组",
        },
    },
    "additionalProperties": True,
}

MIN_DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["assignments"],
    "properties": {
        "step_goal": {"type": "string"},
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "goal"],
                "properties": {"id": {"type": "string"}, "goal": {"type": "string"}},
            },
        },
    },
}

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "brief"],
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "blocked", "spec_invalid", "failed"]},
        "brief": {
            "type": "string",
            "minLength": 8,
            "maxLength": BRIEF_MAX,
            "description": "本步实际做了什么、改了哪些文件、碰到什么错误。短。不要写没做的后续步骤。",
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

MIN_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "brief"],
    "properties": {
        "outcome": {"type": "string"},
        "brief": {"type": "string"},
    },
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "evidence"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["continue", "revise_spec", "takeover", "finish"],
        },
        "evidence": {"type": "string", "minLength": 4},
        "memory_append": {
            "type": "string",
            "maxLength": MEMORY_ENTRY_MAX,
            "description": "新增一条不超过 80 字符的不变量。空则不写。禁止贴实现细节或复述 SPEC.md。",
        },
        "memory_remove": {
            "type": "array",
            "items": {"type": "string"},
            "description": "删除 MEMORY.md 中正文等于或包含该字符串的条目。过时了就删，不要只追加。",
        },
        "memory_replace": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["old", "new"],
                "properties": {
                    "old": {"type": "string", "description": "要改的那条：全文或能唯一定位的子串"},
                    "new": {
                        "type": "string",
                        "maxLength": MEMORY_ENTRY_MAX,
                        "description": "改写后的短不变量；空字符串表示删除",
                    },
                },
            },
            "description": "改已有条目。事实变了就改，不要另起一行让旧事实继续常驻。",
        },
        "forget_append": {
            "type": "string",
            "description": "已确认与任务无关的杂乱上下文描述。压缩总结时会被刻意忽略。",
        },
        "rule_verdicts": {
            "type": "array",
            "description": "对本 lab 已裁定适用的每条 /remember 规则给出对照结论。finish 时必须全部 satisfied。",
            "items": {
                "type": "object",
                "required": ["rule", "satisfied", "note"],
                "properties": {
                    "rule": {"type": "string"},
                    "satisfied": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
    },
    "additionalProperties": True,
}

REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["rule", "applies"],
                "properties": {
                    "rule": {"type": "string"},
                    "applies": {"type": "boolean"},
                },
            },
        },
    },
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["user_summary"],
    "properties": {
        "user_summary": {"type": "string", "minLength": 8},
        "knowledge_cards": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "content"],
                "properties": {
                    "type": {"type": "string", "enum": ["lesson", "strategy", "pattern"]},
                    "content": {"type": "string"},
                },
            },
        },
    },
}

ACCEPT_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "filename", "content"],
    "properties": {
        "task_id": {"type": "string"},
        "filename": {"type": "string"},
        "content": {"type": "string"},
    },
}

DREAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merged": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["content", "source_ids"],
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "source_ids": {
                        "type": "array",
                        "minItems": 2,
                        "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                    },
                },
            },
        },
        "retire_ids": {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        },
    },
}

SKILL_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "operations"],
    "properties": {
        "summary": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "file"],
                "properties": {
                    "action": {"type": "string", "enum": ["write", "delete"]},
                    "file": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
            },
        },
    },
}

_FULL = {
    SUBMIT_SPEC: SPEC_SCHEMA,
    SUBMIT_DISPATCH: DISPATCH_SCHEMA,
    WRITE_ACCEPTANCE: ACCEPT_FILE_SCHEMA,
    SUBMIT_BRIEF: BRIEF_SCHEMA,
    SUBMIT_JUDGE: JUDGE_SCHEMA,
    SUBMIT_REMEMBER: REMEMBER_SCHEMA,
    SUBMIT_SUMMARY: SUMMARY_SCHEMA,
    SUBMIT_DREAM: DREAM_SCHEMA,
    SUBMIT_SKILL_EDIT: SKILL_EDIT_SCHEMA,
}

_MIN = {
    SUBMIT_SPEC: MIN_SPEC_SCHEMA,
    SUBMIT_DISPATCH: MIN_DISPATCH_SCHEMA,
    SUBMIT_BRIEF: MIN_BRIEF_SCHEMA,
    SUBMIT_JUDGE: {
        "type": "object",
        "required": ["decision", "evidence"],
        "properties": {
            "decision": {"type": "string"},
            "evidence": {"type": "string"},
        },
    },
    SUBMIT_SUMMARY: {
        "type": "object",
        "required": ["user_summary"],
        "properties": {"user_summary": {"type": "string"}},
    },
    SUBMIT_REMEMBER: {
        "type": "object",
        "required": ["verdicts"],
        "properties": {"verdicts": {"type": "array"}},
    },
}


def openai_tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def tool_choice_required(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def parse_args(raw: str) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return None, f"invalid json: {e}"
    if not isinstance(data, dict):
        return None, "arguments must be a JSON object"
    return data, ""


def validate_payload(name: str, payload: dict[str, Any], *, degraded: bool = False) -> str:
    schema = (_MIN if degraded else _FULL).get(name)
    if schema is None:
        return f"unknown schema tool: {name}"
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as e:
        return e.message
    if name == SUBMIT_JUDGE:
        evidence = str(payload.get("evidence") or "")
        if "no_hard_criteria" in evidence.lower() and len(evidence) < 24:
            return "no_hard_criteria requires an explicit semantic rationale in evidence"
    if name == SUBMIT_BRIEF:
        brief = str(payload.get("brief") or "").strip()
        if brief in {"已完成", "done", "ok", "做不了"}:
            return "brief is too generic; say what you changed and any errors you hit"
    if name == SUBMIT_DISPATCH and not degraded:
        return _validate_dispatch(payload)
    return ""


def _validate_dispatch(payload: dict[str, Any]) -> str:
    """派发自洽性：id 唯一、领域不重叠、可测的必须给接口契约。"""
    assignments = payload.get("assignments") or []
    if len(assignments) > MAX_ASSIGNMENTS:
        return f"at most {MAX_ASSIGNMENTS} assignments per dispatch; split into more steps"

    ids = [str(a.get("id") or "") for a in assignments]
    if len(set(ids)) != len(ids):
        return "assignment id must be unique within a dispatch"

    domains = [str(a.get("domain") or "").strip().lower() for a in assignments]
    if len(assignments) > 1 and len(set(domains)) != len(domains):
        return (
            "parallel assignments must own disjoint domains; "
            "give each worker a distinct domain or dispatch them in separate steps"
        )

    for assignment in assignments:
        aid = str(assignment.get("id") or "")
        if assignment.get("testable") and not (
            assignment.get("interfaces") or assignment.get("acceptance_files")
        ):
            return (
                f"assignment {aid!r} is testable but declares no interfaces; "
                "acceptance code needs exact names to import and call"
            )
    return ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] or text[:limit]


def coerce_brief(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome": str(payload.get("outcome") or "failed"),
        "brief": _clip(str(payload.get("brief") or ""), BRIEF_MAX),
        "changed_files": list(payload.get("changed_files") or []),
    }


def synthetic_brief(*, outcome: str, brief: str, changed_files: list[str] | None = None) -> dict[str, Any]:
    return coerce_brief({"outcome": outcome, "brief": brief, "changed_files": changed_files or []})


VALIDATION_TOOL_RESULT = (
    "[ERROR/Validation] arguments failed schema check: {err}. "
    "Resubmit this tool with valid JSON matching the schema."
)


async def oneshot_schema(
    llm,
    *,
    model: str,
    system: str,
    user: str,
    name: str,
    schema: dict[str, Any],
    description: str,
) -> dict[str, Any]:
    from runtime.llm import LLMGateway

    assert isinstance(llm, LLMGateway)
    result = await llm.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=[openai_tool(name, description, schema)],
        tool_choice=tool_choice_required(name),
    )
    if not result.tool_calls:
        raise ValueError("model did not call the required function")
    payload, err = parse_args(result.tool_calls[0]["arguments"])
    if payload is None:
        raise ValueError(err)
    msg = validate_payload(name, payload)
    if msg:
        raise ValueError(msg)
    return payload
```

### `runtime/accept.py`

**覆盖。** 程序性验收四态 pass/fail/test_invalid/no_hard_criteria，沙箱内跑。

<!-- PACKFILE: runtime/accept.py -->
```python
"""程序性验收：acceptance/<task_id>/*.py 在沙箱内跑 pytest，给出四态结论。

测试只在沙箱容器里执行。沙箱不可达或 pytest 自身崩溃 → test_invalid
（Judge 语义：门禁没跑起来，不据此惩罚 Flash）。没有 .py 文件 →
no_hard_criteria。collect-only 失败 → test_invalid。跑通且 exit 0 → pass，
否则 fail。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from config.runtime import RuntimeSettings
from tools.policy import get_policy
from tools.sandbox_tools import sandbox_workspace_path, sandbox_run

PASS = "pass"
FAIL = "fail"
TEST_INVALID = "test_invalid"
NO_HARD_CRITERIA = "no_hard_criteria"

_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors)")
_LOG_CAP = 4000


@dataclass
class AcceptResult:
    state: str
    passed: int = 0
    failed: int = 0
    exit_code: int = 0
    log: str = ""

    @property
    def is_pass(self) -> bool:
        return self.state == PASS

    def as_tests(self) -> dict:
        return {
            "state": self.state,
            "passed": self.passed,
            "failed": self.failed,
            "exit_code": self.exit_code,
            "log": self.log[-_LOG_CAP:],
        }


def acceptance_dir(session_dir: Path, task_id: str) -> Path:
    return session_dir / "acceptance" / task_id


def write_acceptance_file(
    session_dir: Path, task_id: str, filename: str, content: str, *, role: str
) -> Path:
    """写一个验收测试文件。role 必须是调用方的真实角色。

    acceptance/ 下的内容决定门禁结论，只有 Pro 能写；用字面量 "pro" 调用
    check_write 等于让这道检查自我批准。
    """
    name = Path(filename).name
    if not name.endswith(".py"):
        raise ValueError(f"acceptance file must be a .py test file, got {filename!r}")
    dest = acceptance_dir(session_dir, task_id)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    policy = get_policy()
    policy.check_write(str(path.relative_to(policy.workspace_dir)), role)
    path.write_text(content, encoding="utf-8")
    return path


def parse_pytest_counts(text: str) -> tuple[int, int]:
    """从 pytest 摘要行解析通过/失败数。error 也计入失败。"""
    passed = failed = 0
    for count, kind in _COUNT_RE.findall(text):
        n = int(count)
        if kind == "passed":
            passed = n
        else:
            failed += n
    return passed, failed


async def _pytest(target: str, extra: list[str], timeout: float) -> tuple[int, str]:
    """在沙箱里跑 pytest。返回 (exit_code, 合并日志)。"""
    args = " ".join(extra)
    command = f"cd /workspace && python -m pytest -q --tb=short {args} {target}".strip()
    return await sandbox_run(command, timeout=timeout)


async def sanity_and_run(
    session_dir: Path, task_id: str, *, settings: RuntimeSettings
) -> AcceptResult:
    """四态：no_hard_criteria / test_invalid / pass / fail。"""
    root = acceptance_dir(session_dir, task_id)
    if not root.is_dir() or not any(root.glob("*.py")):
        return AcceptResult(
            state=NO_HARD_CRITERIA,
            log="acceptance/ 下没有 pytest 文件，本节点没有程序性门禁",
        )

    try:
        target = sandbox_workspace_path(root)
    except ValueError as e:
        return AcceptResult(state=TEST_INVALID, exit_code=-1, log=f"验收目录不在 workspace 内：{e}")

    timeout = settings.tool_timeout_s

    code, log = await _pytest(target, ["--collect-only"], timeout)
    if code != 0:
        return AcceptResult(state=TEST_INVALID, exit_code=code, log=log)

    code, log = await _pytest(target, [], timeout)
    if code != 0 and _is_infra_failure(log):
        return AcceptResult(state=TEST_INVALID, exit_code=code, log=log)

    passed, failed = parse_pytest_counts(log)
    if code == 0:
        return AcceptResult(state=PASS, passed=passed, failed=0, exit_code=0, log=log)
    return AcceptResult(
        state=FAIL, passed=passed, failed=max(failed, 1), exit_code=code, log=log
    )


_INFRA_MARKERS = (
    "[sandbox_unreachable]",
    "[timeout",
    "internalerror>",      # pytest 自身崩溃，不是被测代码的问题
    "no such file or directory",
)


def _is_infra_failure(log: str) -> bool:
    """区分「测试判定失败」和「门禁根本没跑起来」。"""
    lowered = log.lower()
    return any(marker in lowered for marker in _INFRA_MARKERS)
```

### `runtime/control.py`

**覆盖。** 控制面 decide：瞬态重试、校验、停滞、逻辑耗尽。与 LLM 层解耦。

<!-- PACKFILE: runtime/control.py -->
```python
"""决策器：读 Task 快照 + 信号，输出唯一 Decision。无状态。"""

from dataclasses import dataclass

from config.runtime import RuntimeSettings
from runtime.errors import ErrorClass
from runtime.stagnation import StagnationSignal, NUDGE_TEXT
from runtime.task import TaskSnapshot, TaskStatus
from runtime.retry import delay_for


@dataclass(frozen=True)
class Decision:
    kind: str  # continue | retry_tool | nudge | force_brief | stop
    delay: float = 0.0
    text: str = ""
    reason: str = ""

    @property
    def is_stop(self) -> bool:
        return self.kind == "stop"


def decide(
    snap: TaskSnapshot,
    *,
    error_class: ErrorClass,
    stagnation: StagnationSignal,
    settings: RuntimeSettings,
    now: float,
) -> Decision:
    if snap.status in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
        return Decision(kind="stop", reason=snap.status.value.lower())

    if error_class is ErrorClass.AUTH:
        return Decision(kind="stop", reason="auth")
    if error_class is ErrorClass.FATAL:
        return Decision(kind="stop", reason="fatal")
    if error_class is ErrorClass.LOOP or stagnation is StagnationSignal.LOOP_CONFIRMED:
        return Decision(kind="stop", reason="LOOP_DETECTED")

    if now >= snap.deadline:
        return Decision(kind="stop", reason="deadline")

    if snap.step_count >= snap.step_budget:
        return Decision(kind="force_brief", reason="step_budget")

    if stagnation is StagnationSignal.REPEAT:
        return Decision(kind="nudge", text=NUDGE_TEXT)

    if error_class is ErrorClass.TRANSIENT:
        if snap.transient_count >= settings.transient_retry_max:
            return Decision(
                kind="nudge",
                text="Transient retries exhausted. Switch tools or change approach. Do not mark spec_invalid.",
            )
        return Decision(kind="retry_tool", delay=delay_for(snap.transient_count))

    if error_class is ErrorClass.LOGIC:
        if snap.consecutive_logic >= settings.consecutive_logic_failure_max:
            return Decision(kind="stop", reason="logic_exhausted")
        return Decision(kind="continue")

    return Decision(kind="continue")
```

### `runtime/retry.py`

**覆盖。** 退避从 LLM 层拿掉后，只给仍需要的调用点用。

<!-- PACKFILE: runtime/retry.py -->
```python
"""退避延迟：delay_for(attempt) 的秒数，指数增长、封顶 MAX_WAIT。

重试决策只由 control.decide 产出（它能看见 Task 的步数、墙钟和瞬时失败计数）。
LLMGateway / SDK 的 max_retries=0，避免多层退避把 wall-clock 预算耗在等待上。
"""

import asyncio

MIN_WAIT = 0.5
MAX_WAIT = 8.0


def delay_for(attempt: int, *, min_wait: float = MIN_WAIT, max_wait: float = MAX_WAIT) -> float:
    """第 attempt 次瞬时失败后应等待的秒数（指数退避，封顶 max_wait）。"""
    return min(max_wait, min_wait * (2 ** max(0, attempt - 1)))


async def sleep_delay(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)
```

### `runtime/scheduler.py`

**覆盖。** 并行只读 Flash 调度，上限 MAX_PARALLEL_READONLY_WORKERS。

<!-- PACKFILE: runtime/scheduler.py -->
```python
"""执行一步里的 Flash worker：1 个串行可写，多个只读并行。

任一 worker 的 outcome=spec_invalid 时取消尚未完成的兄弟，并给它们合成
failed brief。并发上限为 settings.max_parallel_readonly_workers。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from config.runtime import RuntimeSettings
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer
from runtime.schema_call import synthetic_brief
from runtime.task import Permission, RuntimeTask, TaskStatus


def permission_for_wave(n: int) -> Permission:
    """单个 worker 可写；多个并行一律只读，避免并发写冲突。"""
    return Permission.WRITE if n == 1 else Permission.READONLY


async def run_wave(
    workers: list[RuntimeTask],
    runner: Callable[[RuntimeTask], Awaitable[dict[str, Any]]],
    settings: RuntimeSettings,
    *,
    tracer: Tracer | None = None,
) -> list[tuple[RuntimeTask, dict[str, Any]]]:
    if not workers:
        return []
    if len(workers) == 1:
        w = workers[0]
        return [(w, await runner(w))]

    sem = asyncio.Semaphore(settings.max_parallel_readonly_workers)
    results: dict[str, dict[str, Any]] = {}

    async def one(w: RuntimeTask) -> None:
        async with sem:
            try:
                results[w.task_id] = await runner(w)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if tracer is not None:
                    tracer.event(
                        S.EV_STEP_ERROR,
                        **{S.ATTR_TASK_ID: w.task_id, S.ATTR_REASON: f"{type(e).__name__}: {e}"},
                    )
                results[w.task_id] = synthetic_brief(
                    outcome="failed", brief=f"{type(e).__name__}: {e}"
                )

    tasks = {w.task_id: asyncio.ensure_future(one(w)) for w in workers}
    pending = set(tasks.values())
    try:
        while pending:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(
                (results.get(w.task_id) or {}).get("outcome") == "spec_invalid" for w in workers
            ):
                for t in pending:
                    t.cancel()
                break
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()
        outcomes = await asyncio.gather(*tasks.values(), return_exceptions=True)
        # one() 已把普通异常写成 failed brief。此处剩余的是未捕获异常，记进 span。
        for (task_id, _), outcome in zip(tasks.items(), outcomes):
            if isinstance(outcome, Exception) and tracer is not None:
                tracer.event(
                    S.EV_STEP_ERROR,
                    **{
                        S.ATTR_TASK_ID: task_id,
                        S.ATTR_REASON: f"unhandled {type(outcome).__name__}: {outcome}",
                    },
                )

    out: list[tuple[RuntimeTask, dict[str, Any]]] = []
    for w in workers:
        brief = results.get(w.task_id)
        if brief is None:
            brief = synthetic_brief(
                outcome="failed",
                brief="Sibling workers were cancelled after spec_invalid.",
            )
            if w.status is TaskStatus.RUNNING:
                try:
                    w.transit(TaskStatus.CANCELLED)
                except ValueError:
                    pass
        out.append((w, brief))
    return out
```

### `runtime/session.py`

**覆盖。** LabSession 持有 LLMGateway / Tracer / 设置；done 清场重建沙箱。

<!-- PACKFILE: runtime/session.py -->
```python
"""lab 生命周期：一次会话对应一个 thread_id、一棵 Task 树、一个会话目录。

新任务永远新开会话。续跑只通过 attach_resume 显式挂上未完成的树。
done() 归档知识卡、把 workspace 挪进 .trash、重建沙箱，并换一棵空 runner。
"""

import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings, get_settings
from runtime.llm import LLMGateway
from runtime.observe.sinks import build_sinks
from runtime.observe.tracer import Tracer
from runtime.orchestrator import LabRunner
from runtime.persist import latest_incomplete, session_dir as make_session_dir
from runtime.task import TaskTree, new_session_task
from runtime.tools import build_registry


def _new_thread_id() -> str:
    return f"lab_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class LabSession:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        llm: LLMGateway | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or get_settings()
        tracer = Tracer(
            build_sinks(
                jsonl_path=self.settings.traces_path if self.settings.trace_jsonl_enabled else None,
                langfuse_public_key=self.settings.langfuse_public_key,
                langfuse_secret_key=self.settings.langfuse_secret_key,
                langfuse_host=self.settings.langfuse_host,
                environment=self.settings.langfuse_environment,
                log=print,
            )
        )
        self.llm = llm or LLMGateway(self.settings, tracer=tracer)
        self.tracer = tracer
        self.clock = clock
        self.runner = LabRunner(
            self.settings,
            self.llm,
            registry=build_registry(),
            tracer=tracer,
            clock=clock,
        )
        self.thread_id = _new_thread_id()
        self.tree: TaskTree | None = None
        self.last_result: dict[str, Any] = {}
        self._resuming = False

    @property
    def session_path(self) -> Path:
        return make_session_dir(self.settings.workspace_dir, self.thread_id)

    def peek_resume(self) -> dict[str, Any] | None:
        found = latest_incomplete(self.settings.workspace_dir)
        if not found:
            return None
        tid, tree = found
        root = tree.get(tree.root_id)
        return {"thread_id": tid, "status": root.status.value}

    def attach_resume(self, thread_id: str, tree: TaskTree) -> None:
        """挂上一个未完成的会话。续跑是显式动作，只在这里发生。"""
        self.thread_id = thread_id
        self.tree = tree
        self._resuming = True

    def decline_resume(self) -> dict[str, Any]:
        found = latest_incomplete(self.settings.workspace_dir)
        if found:
            self.thread_id, self.tree = found[0], found[1]
        return self.done(log=lambda m: None)

    def request_stop(self) -> None:
        self.runner.request_stop()

    async def run(self, question: str, on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> dict[str, Any]:
        resume = self._resuming
        if not resume:
            # 每个新任务都是一条独立会话：新的 thread_id、新的 Task 树、新的目录。
            # 复用上一轮的树会让新任务读到上一轮的 SPEC.md 和材料。
            self.thread_id = _new_thread_id()
            self.tree = TaskTree(
                new_session_task(
                    step_budget=self.settings.pro_step_budget + self.settings.flash_step_budget,
                    wall_time_s=self.settings.task_wall_time_s * 4,
                    now=self.clock(),
                )
            )
        assert self.tree is not None
        self._resuming = False
        self.tracer.new_trace()
        self.session_path.mkdir(parents=True, exist_ok=True)
        result = await self.runner.run(
            question,
            self.session_path,
            self.tree,
            on_event=on_event,
            resume=resume,
        )
        self.last_result = result
        return result

    def archive(self) -> dict[str, Any]:
        from memory.archive import get_task_archive
        from memory.retrieve import index_card_ids
        import asyncio

        cards = self.last_result.get("knowledge_cards") or []
        title = self.last_result.get("question") or "未命名任务"
        summary = self.last_result.get("summary") or ""
        ttype = "other"
        try:
            archive = get_task_archive()
            task_id = archive.create_task(title, ttype, summary[:4000])
            card_ids = archive.create_cards(task_id, cards, title, ttype)
            result: dict[str, Any] = {
                "task_id": task_id,
                "card_ids": card_ids,
                "indexed": 0,
                "failed": 0,
                "errors": [],
            }
            if card_ids:

                async def _idx() -> dict[str, Any]:
                    return await index_card_ids(card_ids, self.llm, self.settings)

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    result["index_pending"] = True
                else:
                    idx = asyncio.run(_idx())
                    result.update(idx)
            return result
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def clear_workspace(self) -> tuple[Path, list[str]]:
        ws = self.settings.workspace_dir
        trash_dir = ws.parent / ".trash"
        ts = time.strftime("%Y%m%d_%H%M%S")
        bucket = trash_dir / ts
        bucket.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        if ws.exists():
            for p in ws.iterdir():
                shutil.move(str(p), str(bucket / p.name))
                moved.append(p.name)
        ws.mkdir(parents=True, exist_ok=True)
        return bucket, moved

    def done(self, log=print) -> dict[str, Any]:
        self.request_stop()
        archive_result = self.archive()
        bucket, moved = self.clear_workspace()
        try:
            from infra.sandbox_boot import recreate_sandbox

            sandbox = "ok" if recreate_sandbox(log=log) else "not_ready"
        except Exception as e:
            sandbox = f"failed: {type(e).__name__}: {e}"
        self.tree = None
        self.last_result = {}
        self.thread_id = _new_thread_id()
        self.runner = LabRunner(
            self.settings,
            self.llm,
            registry=build_registry(),
            tracer=self.tracer,
            clock=self.clock,
        )
        return {
            "archive": archive_result,
            "trashed_to": str(bucket),
            "moved": moved,
            "sandbox": sandbox,
        }
```

### `runtime/persist.py`

**覆盖。** 幂等续跑账本：产物指纹完好则跳过。

<!-- PACKFILE: runtime/persist.py -->
```python
"""会话目录的持久化原语：原子写、Task 树快照、副作用账本、续跑扫描。

所有写入走 atomic_write_text（tmp + fsync + rename），崩在半路不会留下
截断的文件。副作用账本让恢复具备幂等性：已经产出且内容未变的节点不重跑。
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.task import TaskTree, TaskStatus

STATE_FILE = "STATE.json"
LEDGER_FILE = "EFFECTS.json"
SPEC_FILE = "SPEC.md"
MATERIALS_FILE = "MATERIALS.md"


def session_root(workspace: Path) -> Path:
    return workspace / ".labhandler" / "sessions"


def session_dir(workspace: Path, thread_id: str) -> Path:
    return session_root(workspace) / thread_id


# ── 原子写 ──────────────────────────────────────────────

def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_text(sdir: Path, name: str, content: str) -> None:
    atomic_write_text(sdir / name, content)


def read_text(sdir: Path, name: str) -> str:
    path = sdir / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def write_json(sdir: Path, name: str, payload: Any) -> None:
    atomic_write_text(sdir / name, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(sdir: Path, name: str) -> Any | None:
    path = sdir / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ── Task 树快照 ─────────────────────────────────────────

def save_tree(sdir: Path, tree: TaskTree) -> None:
    write_json(sdir, STATE_FILE, tree.to_dict())


def load_tree(sdir: Path) -> TaskTree | None:
    """损坏或缺 root_id 的 STATE.json 返回 None。调用方据此判定不可续跑。"""
    data = read_json(sdir, STATE_FILE)
    if not isinstance(data, dict) or "root_id" not in data:
        return None
    try:
        return TaskTree.from_dict(data)
    except (KeyError, ValueError, TypeError):
        return None


# ── 副作用账本（恢复幂等）────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class EffectLedger:
    """node_id -> {相对路径: 内容 sha256}。

    worker 完成后记录它实际产出的文件指纹；续跑时若文件仍在且指纹一致，
    说明这个节点的副作用已经落地，不必重跑。
    """

    sdir: Path
    workspace: Path
    entries: dict[str, dict[str, str]]

    @classmethod
    def load(cls, sdir: Path, workspace: Path) -> "EffectLedger":
        raw = read_json(sdir, LEDGER_FILE)
        entries = raw if isinstance(raw, dict) else {}
        return cls(sdir=sdir, workspace=workspace, entries=entries)

    def _resolve(self, rel: str) -> Path | None:
        candidate = (self.workspace / rel).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def record(self, node_id: str, changed_files: list[str]) -> None:
        fingerprints: dict[str, str] = {}
        for rel in changed_files:
            path = self._resolve(rel)
            if path is None:
                continue
            try:
                fingerprints[rel] = sha256_file(path)
            except OSError:
                continue
        self.entries[node_id] = fingerprints
        write_json(self.sdir, LEDGER_FILE, self.entries)

    def satisfied(self, node_id: str) -> bool:
        """节点产出是否仍然完好。无记录或指纹对不上都返回 False。"""
        fingerprints = self.entries.get(node_id)
        if not fingerprints:
            return False
        for rel, digest in fingerprints.items():
            path = self._resolve(rel)
            if path is None:
                return False
            try:
                if sha256_file(path) != digest:
                    return False
            except OSError:
                return False
        return True

    def drop(self, node_id: str) -> None:
        if self.entries.pop(node_id, None) is not None:
            write_json(self.sdir, LEDGER_FILE, self.entries)


# ── 续跑扫描 ────────────────────────────────────────────

def latest_incomplete(workspace: Path) -> tuple[str, TaskTree] | None:
    root = session_root(workspace)
    if not root.is_dir():
        return None
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for d in dirs:
        tree = load_tree(d)
        if tree is None:
            continue
        root_task = tree.get(tree.root_id)
        if root_task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return d.name, tree
        if any(n.status is TaskStatus.RUNNING for n in tree.nodes.values()):
            return d.name, tree
    return None


# ── 审计回溯 ────────────────────────────────────────────

_WRITE_TOOLS = {
    "write_file",
    "patch_file",
    "sandbox_file_operations",
    "sandbox_str_replace_editor",
    "write_acceptance",
    "use_skill_script",
}


def audit_offset(workspace: Path) -> int:
    """当前审计日志的行数。worker 启动时取一次，用于界定它自己的写入。"""
    path = workspace / ".labhandler" / "audit.jsonl"
    if not path.is_file():
        return 0
    try:
        return sum(1 for _ in path.open(encoding="utf-8"))
    except OSError:
        return 0


def changed_files_from_audit(workspace: Path, *, since: int = 0) -> list[str]:
    """回捞 since 行之后写过的文件。brief 没给 changed_files 时兜底。

    不带 since 会把整个会话的写入都算到某一个 worker 头上，账本按 id 记的
    指纹就会混进别人的产物，续跑判断随之失真。
    """
    path = workspace / ".labhandler" / "audit.jsonl"
    if not path.is_file():
        return []
    seen: set[str] = set()
    out: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[since:]
    except OSError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("tool") not in _WRITE_TOOLS:
            continue
        if not str(rec.get("outcome", "")).startswith("ok"):
            continue
        args = rec.get("args") or {}
        for key in ("path", "file_path", "filename"):
            value = args.get(key)
            if value and str(value) not in seen:
                seen.add(str(value))
                out.append(str(value))
    return out
```

### `runtime/task.py`

**覆盖。** Task 树、Permission、TaskKind。classmethod 返回 Self（去掉 future annotations）。

<!-- PACKFILE: runtime/task.py -->
```python
"""RuntimeTask 控制面：树、状态、预算计数、投影事件。"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Self

from runtime.errors import ErrorClass
from runtime.stagnation import StagnationSignal


class TaskKind(str, Enum):
    """节点种类，同时是阶段的唯一身份。

    除 SESSION 外每一项都在 runtime.phase.PHASES 里有一条定义，那里说明本阶段
    由谁执行、看得见哪些工具、从哪个 submit_* 交卷。
    """

    SESSION = "session"
    SPEC = "spec"          # Pro 起草/修订 SPEC.md
    DISPATCH = "dispatch"  # Pro 决定下一步派谁做什么
    WORKER = "worker"      # Flash 执行一份任务书
    TAKEOVER = "takeover"  # Flash 做不动时 Pro 接手本步实现
    JUDGE = "judge"
    REMEMBER_JUDGE = "remember_judge"  # 裁定 /remember 是否适用于本 lab，不写 SPEC/MEMORY
    SUMMARY = "summary"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Permission(str, Enum):
    READONLY = "readonly"
    WRITE = "write"
    PRO = "pro"


_LEGAL = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


@dataclass
class TaskSnapshot:
    task_id: str
    kind: TaskKind
    status: TaskStatus
    permission: Permission
    deadline: float
    step_count: int
    step_budget: int
    tool_failures: int
    transient_count: int
    validation_count: int
    consecutive_logic: int
    last_error_class: str | None
    events: list[dict[str, str]]
    execution_state: dict[str, Any]


@dataclass
class RuntimeTask:
    task_id: str
    parent_id: str | None
    kind: TaskKind
    permission: Permission
    step_budget: int
    deadline: float
    status: TaskStatus = TaskStatus.PENDING
    children: list[str] = field(default_factory=list)
    step_count: int = 0
    tool_failures: int = 0
    transient_count: int = 0
    validation_count: int = 0
    consecutive_logic: int = 0
    execution_state: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, str]] = field(default_factory=list)
    brief: dict[str, Any] | None = None
    node_spec: dict[str, Any] = field(default_factory=dict)

    def transit(self, new_status: TaskStatus) -> None:
        allowed = _LEGAL[self.status]
        if new_status not in allowed and new_status != self.status:
            raise ValueError(f"illegal transition {self.status} -> {new_status}")
        self.status = new_status

    def record(
        self,
        error_class: ErrorClass | None,
        stagnation_signal: StagnationSignal | None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """计数字段的唯一写入口。"""
        self.step_count += 1
        if usage:
            self.execution_state["last_usage"] = usage
        if error_class is not None:
            self.execution_state["last_error_class"] = error_class.value
            if error_class is ErrorClass.TRANSIENT:
                self.transient_count += 1
                self.tool_failures += 1
            elif error_class is ErrorClass.VALIDATION:
                self.validation_count += 1
            elif error_class is ErrorClass.LOGIC:
                self.consecutive_logic += 1
            elif error_class in {ErrorClass.OK, ErrorClass.ACCEPTABLE}:
                self.consecutive_logic = 0
            elif error_class is ErrorClass.LOOP:
                self.events.append(
                    {
                        "text": "You have repeated the same action and the execution loop was stopped."
                    }
                )
            elif error_class is ErrorClass.FATAL:
                self.events.append({"text": "A fatal environment error stopped the task."})
            elif error_class is ErrorClass.AUTH:
                self.events.append(
                    {
                        "text": "Authentication or client configuration failed. Check LLM_API_KEY and whitelist headers."
                    }
                )

        if stagnation_signal is StagnationSignal.REPEAT:
            self.events.append(
                {
                    "text": "You have repeated the same action 3 times without changing the state."
                }
            )
        elif stagnation_signal is StagnationSignal.LOOP_CONFIRMED:
            self.events.append(
                {
                    "text": "Loop detected after the stagnation grace window. The worker will stop."
                }
            )

        if self.step_count >= self.step_budget:
            self.events.append(
                {
                    "text": "Your task was stopped because the execution budget was exhausted."
                }
            )

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=self.task_id,
            kind=self.kind,
            status=self.status,
            permission=self.permission,
            deadline=self.deadline,
            step_count=self.step_count,
            step_budget=self.step_budget,
            tool_failures=self.tool_failures,
            transient_count=self.transient_count,
            validation_count=self.validation_count,
            consecutive_logic=self.consecutive_logic,
            last_error_class=self.execution_state.get("last_error_class"),
            events=list(self.events),
            execution_state=dict(self.execution_state),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_id": self.parent_id,
            "kind": self.kind.value,
            "permission": self.permission.value,
            "step_budget": self.step_budget,
            "deadline": self.deadline,
            "status": self.status.value,
            "children": list(self.children),
            "step_count": self.step_count,
            "tool_failures": self.tool_failures,
            "transient_count": self.transient_count,
            "validation_count": self.validation_count,
            "consecutive_logic": self.consecutive_logic,
            "execution_state": self.execution_state,
            "events": self.events,
            "brief": self.brief,
            "node_spec": self.node_spec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            task_id=data["task_id"],
            parent_id=data.get("parent_id"),
            kind=TaskKind(data["kind"]),
            permission=Permission(data["permission"]),
            step_budget=int(data["step_budget"]),
            deadline=float(data["deadline"]),
            status=TaskStatus(data["status"]),
            children=list(data.get("children") or []),
            step_count=int(data.get("step_count") or 0),
            tool_failures=int(data.get("tool_failures") or 0),
            transient_count=int(data.get("transient_count") or 0),
            validation_count=int(data.get("validation_count") or 0),
            consecutive_logic=int(data.get("consecutive_logic") or 0),
            execution_state=dict(data.get("execution_state") or {}),
            events=list(data.get("events") or []),
            brief=data.get("brief"),
            node_spec=dict(data.get("node_spec") or {}),
        )


class TaskTree:
    """一棵 RuntimeTask 树。"""

    def __init__(self, root: RuntimeTask) -> None:
        self.root_id = root.task_id
        self.nodes: dict[str, RuntimeTask] = {root.task_id: root}

    def get(self, task_id: str) -> RuntimeTask:
        return self.nodes[task_id]

    def add_child(
        self,
        parent_id: str,
        *,
        kind: TaskKind,
        permission: Permission,
        step_budget: int,
        deadline: float,
        node_spec: dict[str, Any] | None = None,
    ) -> RuntimeTask:
        parent = self.nodes[parent_id]
        child = RuntimeTask(
            task_id=f"{kind.value}_{uuid.uuid4().hex[:8]}",
            parent_id=parent_id,
            kind=kind,
            permission=permission,
            step_budget=step_budget,
            deadline=deadline,
            node_spec=node_spec or {},
        )
        self.nodes[child.task_id] = child
        parent.children.append(child.task_id)
        return child

    def children_of(self, task_id: str) -> list[RuntimeTask]:
        node = self.nodes[task_id]
        return [self.nodes[cid] for cid in node.children]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "nodes": {tid: t.to_dict() for tid, t in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        nodes = {
            tid: RuntimeTask.from_dict(payload)
            for tid, payload in (data.get("nodes") or {}).items()
        }
        root_id = data["root_id"]
        tree = cls.__new__(cls)
        tree.root_id = root_id
        tree.nodes = nodes
        return tree


def new_session_task(*, step_budget: int, wall_time_s: float, now: float | None = None) -> RuntimeTask:
    t0 = now if now is not None else time.time()
    return RuntimeTask(
        task_id=f"session_{uuid.uuid4().hex[:10]}",
        parent_id=None,
        kind=TaskKind.SESSION,
        permission=Permission.PRO,
        step_budget=step_budget,
        deadline=t0 + wall_time_s,
    )
```

### `runtime/stagnation.py`

**覆盖。** 重复 (tool,args,result) 检测；grace 后再 LOOP_CONFIRMED。

<!-- PACKFILE: runtime/stagnation.py -->
```python
"""死循环探测：连续相同 (tool, args_hash, result_hash)。只返回信号。"""

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# 只用于事后诊断，不参与判重；长任务下必须有界
_HISTORY_MAX = 50


class StagnationSignal(str, Enum):
    NONE = "none"
    REPEAT = "repeat"
    LOOP_CONFIRMED = "loop_confirmed"


def _normalize_args(args: dict[str, Any] | None) -> str:
    payload = args or {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class StagnationTracker:
    threshold: int = 3
    grace_steps: int = 2
    _last_key: tuple[str, str, str] | None = None
    _streak: int = 0
    _nudged: bool = False
    _grace_left: int = 0
    history: deque[tuple[str, str, str]] = field(
        default_factory=lambda: deque(maxlen=_HISTORY_MAX)
    )

    def observe(self, tool_name: str, args: dict[str, Any] | None, result_text: str) -> StagnationSignal:
        key = (tool_name, _hash_text(_normalize_args(args)), _hash_text(result_text or ""))
        self.history.append(key)
        if self._last_key == key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
            self._nudged = False
            self._grace_left = 0

        if self._nudged:
            if self._streak >= self.threshold and self._grace_left <= 0:
                return StagnationSignal.LOOP_CONFIRMED
            self._grace_left = max(0, self._grace_left - 1)
            if self._streak >= self.threshold:
                return StagnationSignal.REPEAT
            return StagnationSignal.NONE

        if self._streak >= self.threshold:
            self._nudged = True
            self._grace_left = self.grace_steps
            return StagnationSignal.REPEAT
        return StagnationSignal.NONE


NUDGE_TEXT = (
    "You have repeated the same action 3 times without changing the state. "
    "Do not repeat it. Re-plan using a different approach."
)
```

### `runtime/remember.py`

**新建。** Remember-Judge：崭新 run_loop，不并入 Pro 主线。

<!-- PACKFILE: runtime/remember.py -->
```python
"""本 lab 适用的 /remember 条文。REMEMBER.json 与 SPEC/MEMORY 分开。

remember_judge 只裁定 applies；未点名的条目默认不适用。
步骤 Judge 的 rule_verdicts 必须盖住适用列表，否则不能 finish。
"""

from pathlib import Path
from typing import Any

from runtime.persist import read_json, write_json

REMEMBER_FILE = "REMEMBER.json"


def catalog_rules(profile: dict[str, Any]) -> list[str]:
    prefs = profile.get("preferences") or {}
    return [str(r).strip() for r in (prefs.get("style_rules") or []) if str(r).strip()]


def applied_from_payload(catalog: list[str], payload: dict[str, Any]) -> list[str]:
    chosen: list[str] = []
    for row in payload.get("verdicts") or []:
        if not isinstance(row, dict) or not row.get("applies"):
            continue
        text = str(row.get("rule") or "").strip()
        if text in catalog and text not in chosen:
            chosen.append(text)
    return chosen


def load_applied(session_dir: Path) -> list[str] | None:
    raw = read_json(session_dir, REMEMBER_FILE)
    if not isinstance(raw, dict):
        return None
    rules = raw.get("applied")
    if not isinstance(rules, list):
        return []
    return [str(r).strip() for r in rules if str(r).strip()]


def save_applied(session_dir: Path, rules: list[str]) -> None:
    write_json(session_dir, REMEMBER_FILE, {"applied": rules})


def rules_satisfied(applied: list[str], verdict: dict[str, Any]) -> bool:
    if not applied:
        return True
    ok = {
        str(row.get("rule") or "").strip()
        for row in (verdict.get("rule_verdicts") or [])
        if isinstance(row, dict) and row.get("satisfied")
    }
    return all(rule in ok for rule in applied)
```

### `runtime/context/__init__.py`

**覆盖。** 导出 assemble / compact / disclosure / budget / notes。

<!-- PACKFILE: runtime/context/__init__.py -->
```python
"""上下文子系统：token 预算、槽位装配、回合压缩、会话笔记、技能披露。"""
```

### `runtime/context/assemble.py`

**覆盖。** 装配 system + skill 目录 + notes + 检索 + 历史。阶段指令放 turn 末尾，稳住前缀缓存。

<!-- PACKFILE: runtime/context/assemble.py -->
```python
"""上下文槽位装配。纯函数：同样的输入永远得到同样的 messages。

槽位顺序按「越稳定越靠前」排，利于上游 prompt cache：
system → memory → user → spec → history → retrieved → events

user 槽只承载「本轮之前没有任何对话」的那一段开场指令；为空时整条消息不出现。
跨阶段连续对话的指令由调用方直接写进 history，这样它才排在既有往来之后，
而不是被 user 槽顶到全部历史之前。

history 是工具消息的唯一载体，其中已经包含成对的 assistant(tool_calls) +
tool 响应。装配层没有第二个 tool 槽位：任何旁路注入都会让同一批结果出现
两次，其中一份还会落在 user 消息之后变成协议非法的孤儿消息。

input_tokens = 消息估计 + 本轮 tool schema 估计，与 TokenBudget 用同一套计数。
发出的消息不含 harness 私有字段（如 pinned）。
"""

from dataclasses import dataclass
from typing import Any

from runtime.context.budget import count_messages, count_tool_schemas

# harness 内部标记，不属于 OpenAI 消息结构；发出去会被严格网关拒绝
_PRIVATE_KEYS = frozenset({"pinned"})


@dataclass(frozen=True)
class AgentContext:
    messages: list[dict[str, Any]]
    input_tokens: int


def strip_private(message: dict[str, Any]) -> dict[str, Any]:
    """剥掉 harness 私有字段。无此类字段时返回原对象；否则返回不含这些键的新 dict。"""
    if _PRIVATE_KEYS.isdisjoint(message):
        return message
    return {k: v for k, v in message.items() if k not in _PRIVATE_KEYS}


def assemble(
    *,
    system: str,
    user_input: str,
    project_spec: str,
    history: list[dict[str, Any]],
    retrieved: str,
    memory: str,
    events: list[dict[str, str]],
    working: str,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> AgentContext:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
    ]
    if memory:
        messages.append({"role": "user", "content": memory})
    if user_input:
        messages.append({"role": "user", "content": user_input})
    if project_spec:
        messages.append({"role": "user", "content": f"## SPEC.md\n{project_spec}"})

    messages.extend(strip_private(m) for m in history)

    if retrieved:
        messages.append({"role": "user", "content": f"## Retrieved knowledge\n{retrieved}"})

    event_lines = [e.get("text", "") for e in events if e.get("text")]
    if working:
        event_lines.append(working)
    if event_lines:
        messages.append(
            {"role": "user", "content": "## Execution events\n" + "\n".join(event_lines)}
        )

    tokens = count_messages(messages) + count_tool_schemas(tool_schemas)
    return AgentContext(messages=messages, input_tokens=tokens)


def validate_message_sequence(messages: list[dict[str, Any]]) -> list[str]:
    """检查 OpenAI 兼容协议的消息顺序约束。返回问题列表，空列表表示合法。

    规则：
    1. role=tool 必须处在「带 tool_calls 的 assistant」之后的连续 tool 块里，否则是孤儿。
    2. 带 tool_calls 的 assistant 之后，每个 tool_call id 都要有对应的响应。
    3. tool_call_id 在整份 messages 里不得重复。
    """
    problems: list[str] = []
    pending: dict[str, int] = {}
    owner_index: int | None = None
    seen_ids: set[str] = set()

    def close_block() -> None:
        nonlocal owner_index, pending
        if pending:
            missing = ", ".join(sorted(pending))
            problems.append(
                f"消息 {owner_index} 的 tool_call 没有响应：{missing}"
            )
        pending = {}
        owner_index = None

    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if owner_index is None:
                problems.append(
                    f"消息 {i} 是孤儿 tool：前面没有带 tool_calls 的 assistant"
                )
                continue
            if call_id in seen_ids:
                problems.append(f"消息 {i} 的 tool_call_id 重复：{call_id}")
            seen_ids.add(call_id)
            if call_id not in pending:
                problems.append(
                    f"消息 {i} 的 tool_call_id 未在上游 assistant 中声明：{call_id}"
                )
            else:
                pending.pop(call_id, None)
            continue

        close_block()

        if role == "assistant" and msg.get("tool_calls"):
            owner_index = i
            for tc in msg["tool_calls"]:
                tc_id = str(tc.get("id") or "")
                pending[tc_id] = i

    close_block()
    return problems
```

### `runtime/context/compact.py`

**覆盖。** 最近 N 轮原文，更早摘要；tool 正文卸盘。DumpScope：Pro/Flash 路径隔离。仅 Pro 写 FORGET.md。

<!-- PACKFILE: runtime/context/compact.py -->
```python
"""上下文压缩：最近若干轮保留原文，更早的由 Flash 收成一段纪要。

切分单位是回合：带 tool_calls 的 assistant 与其全部 tool 响应同属一个不可再分
的单位。拆开会产出协议非法的孤儿 tool 消息。

卸盘按 DumpScope（agent × task_id）分目录，Pro 与各 Flash 互不混放。
FORGET.md 只在 Pro 压缩时作为排除指令并清空；Flash 压缩不碰它。
MEMORY.md 不在 history 里、不经摘要——assemble 每轮从文件重新注入。
是否该压、阈值、usage 校准归 TokenBudget；本模块只切回合、落盘 tool 正文、写纪要。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.runtime import RuntimeSettings
from runtime.context.budget import TokenBudget, count_messages
from runtime.context.notes import SessionNotes, clear_forget, forget_directive
from runtime.observe import spans as S
from runtime.observe.tracer import Tracer

DUMP_DIR = "tool_results"
OFFLOAD_MARKER = "[offloaded "
PREVIEW_CHARS = 2000
LIVE_OFFLOAD_CHARS = 12_000
_SUMMARY_MAX_TOKENS = 900
_BLOB_CHAR_CAP = 24000
_INLINE_TOOL_CAP = 600

_SUMMARY_SYSTEM = """你在压缩一个 agent 的历史对话，供它后续回合继续使用。

保留：任务约束、已确认的结论、文件名与路径、接口签名、测试结果、
失败原因、仍未解决的问题。
丢弃：寒暄、重复的试探、已被推翻的中间猜想。
不要编造任何未在原文出现的事实。工具的完整输出已另存到磁盘，
需要时可以 grep {dump_hint}。

输出一段紧凑的中文纪要，不要分点堆砌套话。"""


@dataclass
class CompactResult:
    history: list[dict[str, Any]]
    compacted: bool = False
    tokens_before: int = 0
    tokens_after: int = 0
    turns_summarized: int = 0
    turns_kept: int = 0
    forget_cleared: int = 0
    error: str | None = None


@dataclass
class Turn:
    """一个不可再分的对话回合。带 tool_calls 的 assistant 与其全部 tool 响应同属一个 Turn；pinned 的不参与摘要。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    pinned: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.messages


def split_turns(history: list[dict[str, Any]]) -> list[Turn]:
    """把扁平 history 切成回合。能配对的 tool 并入当前 assistant；无法配对的单独成回合。"""
    turns: list[Turn] = []
    current: Turn | None = None

    for msg in history:
        role = msg.get("role")
        if role == "tool" and current is not None and current.messages:
            head = current.messages[0]
            if head.get("role") == "assistant" and head.get("tool_calls"):
                current.messages.append(msg)
                continue
        if current is not None:
            turns.append(current)
        current = Turn(messages=[msg], pinned=bool(msg.get("pinned")))

    if current is not None:
        turns.append(current)
    return [t for t in turns if not t.is_empty]


def _tool_name_map(turn: Turn) -> dict[str, str]:
    head = turn.messages[0]
    out: dict[str, str] = {}
    for tc in head.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict):
            out[str(tc.get("id") or "")] = str(fn.get("name") or "tool")
    return out


def _safe_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:60] or "tool"


@dataclass(frozen=True)
class DumpScope:
    """一次 loop 的卸盘槽。路径：tool_results/<agent>/<task_id>/。"""

    session_dir: Path
    agent: str
    task_id: str

    @property
    def dump_dir(self) -> Path:
        return self.session_dir / DUMP_DIR / _safe_name(self.agent) / _safe_name(self.task_id)

    @property
    def rel_dir(self) -> str:
        return (
            Path(".labhandler")
            / "sessions"
            / self.session_dir.name
            / DUMP_DIR
            / _safe_name(self.agent)
            / _safe_name(self.task_id)
        ).as_posix()

    def grep_roots(self, labhandler: Path) -> list[Path]:
        """Pro 可扫整个 .labhandler；Flash 只扫自己的卸盘目录。"""
        if self.agent == "pro":
            return [labhandler]
        return [self.dump_dir]


def is_offloaded(content: str) -> bool:
    return content.startswith(OFFLOAD_MARKER)


def dump_rel_path(scope: DumpScope, filename: str) -> str:
    return f"{scope.rel_dir}/{filename}" if filename else scope.rel_dir


def format_stub(rel_path: str, body: str, preview_chars: int = PREVIEW_CHARS) -> str:
    preview = body[:preview_chars]
    lines = [
        f"{OFFLOAD_MARKER}{len(body)} chars → {rel_path}]",
        "Full result: read_file or memory_read that path; search with memory_grep.",
        preview,
    ]
    if len(body) > preview_chars:
        lines.append("…")
    return "\n".join(lines)


def _next_dump_seq(dump_dir: Path) -> int:
    seq = 0
    if not dump_dir.is_dir():
        return seq
    for p in dump_dir.iterdir():
        m = re.match(r"^(\d{4})-", p.name)
        if m:
            seq = max(seq, int(m.group(1)) + 1)
    return seq


def write_tool_dump(scope: DumpScope, *, tool: str, call_id: str, body: str) -> str | None:
    """把全文写入本 loop 的 tool_results 槽，返回文件名。空正文、已是桩、或写盘失败时返回 None。"""
    if not body or is_offloaded(body):
        return None
    dump_dir = scope.dump_dir
    filename = (
        f"{_next_dump_seq(dump_dir):04d}-"
        f"{_safe_name(tool)}-{_safe_name(call_id)[:12]}.txt"
    )
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / filename).write_text(body, encoding="utf-8")
    except OSError:
        return None
    return filename


def dump_tool_result(
    scope: DumpScope,
    *,
    tool: str,
    call_id: str,
    body: str,
    preview_chars: int = PREVIEW_CHARS,
) -> str:
    """把全文写入本 loop 的卸盘槽，返回带相对路径 + preview 的桩。写盘失败则退回原文。"""
    filename = write_tool_dump(scope, tool=tool, call_id=call_id, body=body)
    if filename is None:
        return body
    return format_stub(dump_rel_path(scope, filename), body, preview_chars)


def offload_tool_bodies(turns: list[Turn], scope: DumpScope) -> list[str]:
    """把将被摘要的回合里的 tool 正文落盘，返回已写入的文件名。只写磁盘，不改消息内容。"""
    written: list[str] = []
    for turn in turns:
        names = _tool_name_map(turn)
        for msg in turn.messages:
            if msg.get("role") != "tool":
                continue
            call_id = str(msg.get("tool_call_id") or "")
            filename = write_tool_dump(
                scope,
                tool=names.get(call_id, "tool"),
                call_id=call_id,
                body=str(msg.get("content") or ""),
            )
            if filename:
                written.append(filename)
    return written


def _render_for_summary(turns: list[Turn]) -> str:
    """把待摘要的回合渲染成纯文本。tool 正文只留头部（全文已在磁盘）；整段再截到 _BLOB_CHAR_CAP。"""
    parts: list[str] = []
    for turn in turns:
        names = _tool_name_map(turn)
        for msg in turn.messages:
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "")
            if role == "assistant" and msg.get("tool_calls"):
                calls = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") if isinstance(tc, dict) else {}
                    if isinstance(fn, dict):
                        calls.append(f"{fn.get('name')}({str(fn.get('arguments') or '')[:200]})")
                parts.append(f"[assistant] {content}\n[调用] " + "; ".join(calls))
            elif role == "tool":
                tool = names.get(str(msg.get("tool_call_id") or ""), "tool")
                clipped = content[:_INLINE_TOOL_CAP]
                suffix = " …(全文已落盘)" if len(content) > _INLINE_TOOL_CAP else ""
                parts.append(f"[{tool} 结果] {clipped}{suffix}")
            else:
                parts.append(f"[{role}] {content}")
    return "\n".join(parts)[-_BLOB_CHAR_CAP:]


async def _summarize(
    turns: list[Turn],
    *,
    settings: RuntimeSettings,
    llm: Any,
    notes: SessionNotes,
    dump_hint: str,
    apply_forget: bool,
) -> tuple[str, str | None]:
    """用 Flash 把旧回合收成一段纪要。成功返回 (正文, None)；异常或空响应返回 ("", 错误串)。"""
    system = _SUMMARY_SYSTEM.format(dump_hint=dump_hint)
    if apply_forget:
        directive = forget_directive(notes)
        if directive:
            system = system + "\n\n" + directive

    try:
        result = await llm.chat(
            model=settings.flash_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _render_for_summary(turns)},
            ],
            max_tokens=_SUMMARY_MAX_TOKENS,
        )
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"

    text = (result.content or "").strip()
    if not text:
        return "", f"summarizer 未返回内容 (error_class={result.error_class.value})"
    return text, None


async def compact_history(
    *,
    history: list[dict[str, Any]],
    dump: DumpScope,
    settings: RuntimeSettings,
    llm: Any,
    notes: SessionNotes,
    budget: TokenBudget,
    estimate: int,
    apply_forget: bool = False,
    tracer: Tracer | None = None,
) -> CompactResult:
    """超过 TokenBudget 触发阈值才压缩。未触发、或可移动回合不超过保留数时，原样返回且无副作用。

    触发后：旧回合 tool 正文落入 dump 槽 → Flash 总结 → 连续旧片段就地换成一条纪要（pinned 留原位）。
    仅 Pro（apply_forget）读取并清空 FORGET.md。
    摘要失败仍替换 history，纪要位置写失败说明，compacted=True。
    """
    projected = budget.projected(estimate)
    if not budget.should_compact(estimate):
        return CompactResult(history=history, tokens_before=projected, tokens_after=projected)

    turns = split_turns(history)
    movable = [t for t in turns if not t.pinned]
    keep_n = settings.compact_keep_recent_turns

    if len(movable) <= keep_n:
        return CompactResult(history=history, tokens_before=projected, tokens_after=projected)

    old = movable[:-keep_n]
    recent = movable[-keep_n:]
    summarized = {id(t) for t in old}

    span_cm = (
        tracer.span(
            S.COMPACT,
            kind=S.KIND_CHAIN,
            **{
                S.ATTR_TOKENS_BEFORE: projected,
                S.ATTR_TURNS_SUMMARIZED: len(old),
                S.ATTR_TURNS_KEPT: len(recent),
            },
        )
        if tracer
        else None
    )
    span = span_cm.__enter__() if span_cm is not None else None
    try:
        dumped = offload_tool_bodies(old, dump)
        dump_rel = dump.rel_dir
        dump_hint = f"`{dump_rel}/`" + (f"（{len(dumped)} 个文件）" if dumped else "")
        summary, error = await _summarize(
            old,
            settings=settings,
            llm=llm,
            notes=notes,
            dump_hint=dump_hint,
            apply_forget=apply_forget,
        )

        note = summary if summary else f"(摘要生成失败：{error}。)"
        digest = {
            "role": "user",
            "content": (
                f"## 早前对话纪要\n{note}\n\n"
                f"早前工具的完整输出在 {dump_hint}。"
                "需要原文时用 memory_grep 搜索正文，或用 memory_read / read_file 按路径读取。"
            ),
        }

        # 按原顺序重建：连续被摘要的回合只发出一条纪要，pinned 回合留在原位。
        new_history: list[dict[str, Any]] = []
        emitted = False
        for turn in turns:
            if id(turn) in summarized:
                if not emitted:
                    new_history.append(digest)
                    emitted = True
                continue
            new_history.extend(turn.messages)
        if not emitted:
            new_history.insert(0, digest)

        cleared = clear_forget(dump.session_dir) if apply_forget else 0

        after = budget.projected(count_messages(new_history))
        result = CompactResult(
            history=new_history,
            compacted=True,
            tokens_before=projected,
            tokens_after=after,
            turns_summarized=len(old),
            turns_kept=len(recent),
            forget_cleared=cleared,
            error=error,
        )
        if span is not None:
            span.set(
                **{
                    S.ATTR_TOKENS_AFTER: after,
                    S.ATTR_COMPACTED: True,
                    S.ATTR_FORGET_CLEARED: cleared,
                }
            )
            if error:
                span.set(**{S.ATTR_REASON: error})
        return result
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)
```

### `runtime/context/disclosure.py`

**覆盖。** skill_catalog_block 只放 name/description/when_to_use，不内联 SOP。

<!-- PACKFILE: runtime/context/disclosure.py -->
```python
"""技能披露：system 只放目录（name / description / when_to_use）。

SOP 由 Pro 经 load_skill 拉取，一次 lab 至多一份；references / scripts 再按需。
无 skill 时返回空串，system 不加 Skills 段。
"""

from tools.skill_tool import list_skill_meta


def skill_catalog_block() -> str:
    metas = list_skill_meta()
    if not metas:
        return ""
    lines = [
        "## Skills (optional; mutually exclusive)",
        "Call load_skill at most once this lab to pull one SOP, or skip.",
        "After that, load_skill_reference / use_skill_script only work for the bound skill.",
        "",
    ]
    for m in metas:
        lines.append(f"### {m['name']}")
        if m.get("description"):
            lines.append(m["description"].strip())
        if m.get("when_to_use"):
            lines.append(m["when_to_use"].strip())
        lines.append("")
    return "\n".join(lines)
```

### `runtime/context/budget.py`

**新建。** 本地字符估计 + 用上一轮 usage.input_tokens 投影。触发压缩阈值。

<!-- PACKFILE: runtime/context/budget.py -->
```python
"""Token 计量与压缩预算。

本地估计：CJK 1 字 1 token，ASCII 约 4 字符 1 token，其余约 2 字符 1 token。
中文必须按字计，否则窗口会被低估约 2 倍，压缩阈值偏小。一条消息的估计必须
覆盖 content、tool_calls.arguments，以及每轮随请求发出的 tool schema。

估计全程本地完成。发请求前只有这份数字；发后若 usage 带回 input_tokens，
TokenBudget 记下 actual/estimate 比例，下一轮投影到真实窗口再决定压不压。
窗口、输出预留、触发比、校准比例都只活在这一个对象里。
"""

import json
from dataclasses import dataclass
from typing import Any, Self

from config.runtime import RuntimeSettings

# 每条消息的角色/分隔符固定开销
_MESSAGE_OVERHEAD = 4
# 每个 tool_call 的包装开销（id、type、function 外壳）
_TOOL_CALL_OVERHEAD = 8


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角
        or 0x3040 <= code <= 0x30FF   # 假名
        or 0xAC00 <= code <= 0xD7AF   # 谚文
    )


def count_text(text: str) -> int:
    """单段文本的 token 估计。CJK 1 字 1 token，ASCII 4 字符 1 token，其余 2 字符 1 token。"""
    if not text:
        return 0
    cjk = 0
    ascii_like = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ch.isascii():
            ascii_like += 1
        else:
            other += 1
    return cjk + (ascii_like + 3) // 4 + (other + 1) // 2


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def count_message(message: dict[str, Any]) -> int:
    """一条 message 的 token 估计：角色开销 + content + 每个 tool_call 的外壳/名/arguments；
    tool 响应另加 tool_call_id 开销。"""
    total = _MESSAGE_OVERHEAD + count_text(_content_text(message.get("content")))
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        total += _TOOL_CALL_OVERHEAD
        total += count_text(str(fn.get("name") or ""))
        total += count_text(str(fn.get("arguments") or ""))
    if message.get("tool_call_id"):
        total += 4
    return total


def count_messages(messages: list[dict[str, Any]]) -> int:
    return sum(count_message(m) for m in messages)


def count_tool_schemas(tools: list[dict[str, Any]] | None) -> int:
    """工具定义每轮都随请求发送，必须计入输入预算，否则压缩阈值会偏小。"""
    if not tools:
        return 0
    return count_text(json.dumps(tools, ensure_ascii=False))


# 单次 usage 可能被缓存计数、网关少报等带偏，比例钳在这个区间。
_SCALE_MIN = 0.25
_SCALE_MAX = 4.0


@dataclass
class TokenBudget:
    """一次 agent loop 的输入窗口账本。

    input_limit = window - output_reserve（至少 1024）。trigger = input_limit × trigger_ratio。
    projected = 本地估计 × scale。scale 默认 1.0；observe 仅在 usage.input_tokens 与估计
    都为正时更新，并钳在 [_SCALE_MIN, _SCALE_MAX]。should_compact 在 projected ≥ trigger 时为真。
    """

    window: int
    output_reserve: int
    trigger_ratio: float
    scale: float = 1.0

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> Self:
        return cls(
            window=settings.context_budget_tokens,
            output_reserve=settings.output_reserve_tokens,
            trigger_ratio=settings.compact_trigger_ratio,
        )

    @property
    def input_limit(self) -> int:
        return max(1024, self.window - self.output_reserve)

    @property
    def trigger(self) -> int:
        return int(self.input_limit * self.trigger_ratio)

    def projected(self, estimate: int) -> int:
        return max(0, int(estimate * self.scale))

    def should_compact(self, estimate: int) -> bool:
        return self.projected(estimate) >= self.trigger

    def observe(self, estimate: int, usage: dict[str, Any] | None) -> None:
        actual = int((usage or {}).get("input_tokens") or 0)
        if estimate <= 0 or actual <= 0:
            return
        self.scale = min(_SCALE_MAX, max(_SCALE_MIN, actual / estimate))
```

### `runtime/context/notes.py`

**新建。** MEMORY.md 常驻；FORGET.md 压缩时跳过噪声，压完清空。

<!-- PACKFILE: runtime/context/notes.py -->
```python
"""会话内的两份主动记忆，都落在 session 目录。

MEMORY.md —— Pro 判定「必须长久记住」的不变量。每轮由 assemble 从文件重新注入，
             不经 compact 摘要。Judge 可追加、改写或删除；整份超 _MAX_CHARS 时
             淘汰最早的条目。
FORGET.md —— Pro 判定「无关杂乱」的描述。compact 时作为排除指令交给
             summarizer；压缩完成后立即清空——被描述的内容已不在 history 里，
             再留着只会成为新噪声。

条目按整行匹配，同一条不会写两遍。空则删文件。原子落盘。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.persist import read_text, write_text

MEMORY_FILE = "MEMORY.md"
FORGET_FILE = "FORGET.md"

_MEMORY_HEADER = "# 必须记住的不变量\n"
_FORGET_HEADER = "# 压缩时忽略的杂乱上下文\n"

# 单条上限：MEMORY.md 每轮整份进上下文。超出截成一行。
MEMORY_ENTRY_MAX = 80
# 单份文件硬上限；超出时丢掉最早的条目。
_MAX_CHARS = 2000


@dataclass(frozen=True)
class SessionNotes:
    memory: str
    forget: str

    @property
    def has_memory(self) -> bool:
        return bool(self.memory.strip())

    @property
    def has_forget(self) -> bool:
        return bool(self.forget.strip())


def load_notes(sdir: Path) -> SessionNotes:
    return SessionNotes(
        memory=_body(read_text(sdir, MEMORY_FILE), _MEMORY_HEADER),
        forget=_body(read_text(sdir, FORGET_FILE), _FORGET_HEADER),
    )


def _body(raw: str, header: str) -> str:
    text = raw.strip()
    if text.startswith(header.strip()):
        text = text[len(header.strip()):]
    return text.strip()


def _plain(line: str) -> str:
    s = line.strip()
    return s[2:].strip() if s.startswith("- ") else s


def _one_line(text: str, limit: int) -> str:
    entry = " ".join(_plain(text).split())
    if len(entry) <= limit:
        return entry
    cut = entry[:limit].rsplit(" ", 1)[0]
    return cut or entry[:limit]


def _read_lines(sdir: Path, filename: str, header: str) -> list[str]:
    body = _body(read_text(sdir, filename), header)
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _write_lines(sdir: Path, filename: str, header: str, lines: list[str]) -> None:
    if len("\n".join(lines)) > _MAX_CHARS:
        while lines and len("\n".join(lines)) > _MAX_CHARS:
            lines.pop(0)
    if not lines:
        (sdir / filename).unlink(missing_ok=True)
        return
    write_text(sdir, filename, header + "\n" + "\n".join(lines) + "\n")


def _matches(line: str, needle: str) -> bool:
    body = _plain(line)
    key = " ".join(needle.strip().split())
    if key.startswith("- "):
        key = key[2:].strip()
    if not key:
        return False
    return body == key or key in body


def _append(sdir: Path, filename: str, header: str, text: str, *, limit: int | None = None) -> bool:
    entry = _one_line(text, limit) if limit else text.strip()
    if not entry:
        return False
    lines = _read_lines(sdir, filename, header)
    bullet = entry if entry.startswith("- ") else f"- {entry}"
    if bullet in lines:
        return False
    lines.append(bullet)
    _write_lines(sdir, filename, header, lines)
    return True


def append_memory(sdir: Path, text: str) -> bool:
    """追加一条必须长期保留的不变量。超出 MEMORY_ENTRY_MAX 的尾部丢掉；重复或空串不写。"""
    return _append(sdir, MEMORY_FILE, _MEMORY_HEADER, text, limit=MEMORY_ENTRY_MAX)


def append_forget(sdir: Path, text: str) -> bool:
    """追加一条「总结时请忽略」的噪声描述。只对下一次 compact 有效；压缩完成后文件被清空。"""
    return _append(sdir, FORGET_FILE, _FORGET_HEADER, text)


def remove_memory(sdir: Path, needles: list[str]) -> int:
    """删掉正文等于或包含 needle 的条目。返回删除条数；无有效 needle 时为 0。"""
    keys = [n for n in (_one_line(n, 200) for n in needles) if n]
    if not keys:
        return 0
    lines = _read_lines(sdir, MEMORY_FILE, _MEMORY_HEADER)
    kept = [ln for ln in lines if not any(_matches(ln, k) for k in keys)]
    dropped = len(lines) - len(kept)
    if dropped:
        _write_lines(sdir, MEMORY_FILE, _MEMORY_HEADER, kept)
    return dropped


def replace_memory(sdir: Path, old: str, new: str) -> int:
    """把匹配 old 的条目改成 new（同样 ≤ MEMORY_ENTRY_MAX）。new 为空则删除该条。返回改动条数。"""
    key = _one_line(old, 200)
    replacement = _one_line(new, MEMORY_ENTRY_MAX)
    if not key:
        return 0
    lines = _read_lines(sdir, MEMORY_FILE, _MEMORY_HEADER)
    out: list[str] = []
    n = 0
    for ln in lines:
        if not _matches(ln, key):
            out.append(ln)
            continue
        n += 1
        if replacement:
            bullet = f"- {replacement}"
            if bullet not in out:
                out.append(bullet)
    if n:
        _write_lines(sdir, MEMORY_FILE, _MEMORY_HEADER, out)
    return n


def apply_memory(
    sdir: Path,
    *,
    replace: Any = None,
    remove: Any = None,
    append: str = "",
) -> None:
    """先改、再删、最后追加。Judge 一次裁决里对 MEMORY.md 的全部变更，顺序固定。"""
    for item in replace or []:
        if not isinstance(item, dict):
            continue
        replace_memory(sdir, str(item.get("old") or ""), str(item.get("new") or ""))
    needles = remove if isinstance(remove, list) else ([remove] if remove else [])
    remove_memory(sdir, [str(x) for x in needles])
    append_memory(sdir, append)


def clear_forget(sdir: Path) -> int:
    """压缩完成后清空 FORGET.md，返回清掉的条数。文件不存在视为 0。

    被排除的内容此时已不在 history 里；文件必须删掉，否则下一轮又变成噪声。
    """
    body = _body(read_text(sdir, FORGET_FILE), _FORGET_HEADER)
    count = len([ln for ln in body.splitlines() if ln.strip()])
    (sdir / FORGET_FILE).unlink(missing_ok=True)
    return count


def memory_block(notes: SessionNotes) -> str:
    """交给 assemble 的记忆块。空则返回空串，调用方不加 memory 槽位。"""
    if not notes.has_memory:
        return ""
    return "## 必须记住的不变量\n" + notes.memory


def forget_directive(notes: SessionNotes) -> str:
    """交给 summarizer 的排除指令。无条目时返回空串，不拼进 summarizer 的 system。"""
    if not notes.has_forget:
        return ""
    return (
        "以下内容已被判定为与任务无关的杂乱上下文，"
        "总结时必须刻意忽略，不要出现在摘要里：\n" + notes.forget
    )
```

### `runtime/observe/spans.py`

**覆盖。** span 名字与属性常量：run/spec/dispatch/step/task/turn/llm/tool/验收。

<!-- PACKFILE: runtime/observe/spans.py -->
```python
"""Span 名、observation 类型与属性 key。其它模块只引用本文件常量。"""

# ── span 名 ────────────────────────────────────────────
RUN = "labhandler.run"
TASK = "labhandler.task"
AGENT = "labhandler.agent"
TURN = "labhandler.turn"
CONTEXT_BUILD = "labhandler.context_build"
COMPACT = "labhandler.compact"
LLM = "labhandler.llm"
TOOL = "labhandler.tool"
GUARDRAIL = "labhandler.guardrail"
STEP = "labhandler.step"
JUDGE = "labhandler.judge"
ACCEPT = "labhandler.accept"

# ── 点事件名 ───────────────────────────────────────────
EV_RETRY = "labhandler.retry"
EV_STAGNATION = "labhandler.stagnation"
EV_HANDOFF = "labhandler.handoff"
EV_DECISION = "labhandler.decision"
EV_STEP_ERROR = "labhandler.step_error"

# ── observation 类型（Langfuse as_type）────────────────
KIND_SPAN = "span"
KIND_AGENT = "agent"
KIND_CHAIN = "chain"
KIND_TOOL = "tool"
KIND_GENERATION = "generation"
KIND_GUARDRAIL = "guardrail"
KIND_EVALUATOR = "evaluator"

# ── 属性 key ───────────────────────────────────────────
ATTR_RUN_ID = "labhandler.run_id"
ATTR_THREAD_ID = "labhandler.thread_id"
ATTR_TASK_ID = "labhandler.task_id"
ATTR_TASK_KIND = "labhandler.task_kind"
ATTR_PARENT_TASK_ID = "labhandler.parent_task_id"
ATTR_ASSIGNMENT_ID = "labhandler.assignment_id"
ATTR_DOMAIN = "labhandler.domain"
ATTR_STEP_GOAL = "labhandler.step_goal"
ATTR_AGENT = "labhandler.agent_name"
ATTR_PERMISSION = "labhandler.permission"
ATTR_STEP = "labhandler.step"
ATTR_STEP_BUDGET = "labhandler.step_budget"

ATTR_MODEL = "gen_ai.request.model"
ATTR_TOKENS_IN = "gen_ai.usage.input_tokens"
ATTR_TOKENS_OUT = "gen_ai.usage.output_tokens"
ATTR_TOKENS_REASONING = "labhandler.usage.reasoning_tokens"
ATTR_FINISH_REASON = "gen_ai.response.finish_reason"

ATTR_TOOL = "labhandler.tool_name"
ATTR_TOOL_ALLOWED = "labhandler.tool_allowed"
ATTR_ERROR_CLASS = "labhandler.error_class"
ATTR_OUTCOME = "labhandler.outcome"

ATTR_DECISION = "labhandler.decision"
ATTR_REASON = "labhandler.reason"
ATTR_ATTEMPT = "labhandler.attempt"
ATTR_DELAY_S = "labhandler.delay_s"
ATTR_SIGNAL = "labhandler.signal"
ATTR_FROM = "labhandler.from"
ATTR_TO = "labhandler.to"

ATTR_TOKENS_BEFORE = "labhandler.context.tokens_before"
ATTR_TOKENS_AFTER = "labhandler.context.tokens_after"
ATTR_TURNS_KEPT = "labhandler.context.turns_kept"
ATTR_TURNS_SUMMARIZED = "labhandler.context.turns_summarized"
ATTR_COMPACTED = "labhandler.context.compacted"
ATTR_FORGET_CLEARED = "labhandler.context.forget_cleared"

ATTR_GATE_STATE = "labhandler.gate.state"
ATTR_GATE_PASSED = "labhandler.gate.passed"
ATTR_GATE_FAILED = "labhandler.gate.failed"
ATTR_GATE_EXIT = "labhandler.gate.exit_code"

ATTR_WAVE_SIZE = "labhandler.step.workers"
```

### `runtime/observe/tracer.py`

**覆盖。** Langfuse + 本地 JSONL 双 sink。

<!-- PACKFILE: runtime/observe/tracer.py -->
```python
"""Tracer：真实计时 + 父子嵌套 + 贯穿一次 run 的 trace_id。

span 必须包住真正的操作体：事后 `with tracer.span(...): pass` 会使
elapsed_ms 恒为 0 且丢掉因果。父子关系用 contextvars 维护；
asyncio.create_task 会拷贝当前 context，并行 worker 挂在所在 step 的 span 下。
Sink 自身异常被吞掉，观测失败不改变主流程。
"""

import contextvars
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from runtime.observe import spans as S
from runtime.observe.sinks import Sink, SpanRecord

_CURRENT: contextvars.ContextVar["SpanHandle | None"] = contextvars.ContextVar(
    "labhandler_current_span", default=None
)


class SpanHandle:
    """一个活跃 span。用 set/output/event 往上挂数据，结束由 Tracer 负责。"""

    __slots__ = ("record", "_handles", "_tracer")

    def __init__(self, record: SpanRecord, handles: list[Any], tracer: "Tracer") -> None:
        self.record = record
        self._handles = handles
        self._tracer = tracer

    @property
    def trace_id(self) -> str:
        return self.record.trace_id

    @property
    def span_id(self) -> str:
        return self.record.span_id

    def set(self, **attrs: Any) -> "SpanHandle":
        for k, v in attrs.items():
            if v is not None:
                self.record.attrs[k] = v
        return self

    def output(self, value: Any) -> "SpanHandle":
        self.record.output = value
        return self

    def event(self, name: str, **attrs: Any) -> None:
        payload = {k: v for k, v in attrs.items() if v is not None}
        self.record.events.append({"name": name, **payload})
        for sink, handle in zip(self._tracer.sinks, self._handles):
            try:
                sink.event(self.record, handle, name, payload)
            except Exception:
                pass


class _NullSpan(SpanHandle):
    """未启用观测时的占位，保持调用点无分支。"""

    def __init__(self) -> None:  # noqa: D107 - 不调用父类
        self.record = SpanRecord(
            name="", kind=S.KIND_SPAN, span_id="", trace_id="", parent_id=None, started_at=0.0
        )
        self._handles = []
        self._tracer = None  # type: ignore[assignment]

    def set(self, **attrs: Any) -> "SpanHandle":
        return self

    def output(self, value: Any) -> "SpanHandle":
        return self

    def event(self, name: str, **attrs: Any) -> None:
        return None


_NULL = _NullSpan()


class Tracer:
    def __init__(self, sinks: list[Sink] | None = None, *, trace_id: str | None = None) -> None:
        self.sinks = sinks or []
        self.trace_id = trace_id or uuid.uuid4().hex

    @property
    def enabled(self) -> bool:
        return bool(self.sinks)

    def new_trace(self, trace_id: str | None = None) -> str:
        """开一条新 trace（一次 lab = 一条）。"""
        self.trace_id = trace_id or uuid.uuid4().hex
        return self.trace_id

    def current(self) -> SpanHandle | None:
        return _CURRENT.get()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = S.KIND_SPAN,
        inputs: Any = None,
        **attrs: Any,
    ) -> Iterator[SpanHandle]:
        """包住操作体。elapsed_ms 是真实耗时，异常会记进 span 并继续上抛。"""
        if not self.sinks:
            yield _NULL
            return

        parent = _CURRENT.get()
        record = SpanRecord(
            name=name,
            kind=kind,
            span_id=uuid.uuid4().hex[:16],
            trace_id=self.trace_id,
            parent_id=parent.span_id if parent else None,
            started_at=time.perf_counter(),
            attrs={S.ATTR_RUN_ID: self.trace_id, **{k: v for k, v in attrs.items() if v is not None}},
            inputs=inputs,
        )
        handles = []
        for sink in self.sinks:
            try:
                handles.append(sink.open(record))
            except Exception:
                handles.append(None)

        handle = SpanHandle(record, handles, self)
        token = _CURRENT.set(handle)
        try:
            yield handle
        except BaseException as e:
            record.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            _CURRENT.reset(token)
            record.elapsed_ms = int((time.perf_counter() - record.started_at) * 1000)
            for sink, h in zip(self.sinks, handles):
                try:
                    sink.close(record, h)
                except Exception:
                    pass

    def event(self, name: str, **attrs: Any) -> None:
        """挂在当前 span 上的点事件：重试、停滞、handoff、决策。"""
        if not self.sinks:
            return
        current = _CURRENT.get()
        if current is not None:
            current.event(name, **attrs)
            return
        payload = {k: v for k, v in attrs.items() if v is not None}
        for sink in self.sinks:
            try:
                sink.event(None, None, name, payload)
            except Exception:
                pass

    def flush(self) -> None:
        for sink in self.sinks:
            try:
                sink.flush()
            except Exception:
                pass

    def shutdown(self) -> None:
        for sink in self.sinks:
            try:
                sink.shutdown()
            except Exception:
                pass


__all__ = ["Tracer", "SpanHandle", "S"]
```

### `runtime/observe/sinks.py`

**新建。** Langfuse / JSONL sink 实现。公钥私钥留空则只写 traces.jsonl。

<!-- PACKFILE: runtime/observe/sinks.py -->
```python
"""Span sink 接口与两个实现：JSONL 与 Langfuse。

Tracer 只依赖 Sink 协议。JSONL 是本地兜底；Langfuse 构造失败时由
build_sinks 跳过，主流程不受影响。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class SpanRecord:
    """一个已结束（或刚开始）的 span 的完整描述。"""

    name: str
    kind: str
    span_id: str
    trace_id: str
    parent_id: str | None
    started_at: float
    attrs: dict[str, Any] = field(default_factory=dict)
    inputs: Any = None
    output: Any = None
    elapsed_ms: int = 0
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class Sink(Protocol):
    """span 导出后端。所有方法必须吞掉自身异常，观测不能拖垮主流程。"""

    def open(self, record: SpanRecord) -> Any: ...

    def close(self, record: SpanRecord, handle: Any) -> None: ...

    def event(
        self, record: SpanRecord | None, handle: Any, name: str, attrs: dict[str, Any]
    ) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class JsonlSink:
    """把每个结束的 span 追加成一行 JSON。无外部依赖。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def open(self, record: SpanRecord) -> Any:
        return None

    def close(self, record: SpanRecord, handle: Any) -> None:
        self._write(
            {
                "type": "span",
                "name": record.name,
                "kind": record.kind,
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                "parent_id": record.parent_id,
                "elapsed_ms": record.elapsed_ms,
                "attrs": record.attrs,
                "error": record.error,
                "events": record.events,
            }
        )

    def event(
        self, record: SpanRecord | None, handle: Any, name: str, attrs: dict[str, Any]
    ) -> None:
        if record is not None:
            # 已挂在 span 上，随 close 一起落盘。这里再写一行会让统计重复计数。
            return
        self._write({"type": "event", "name": name, "trace_id": None, "span_id": None, "attrs": attrs})

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


# Langfuse as_type 只接受固定枚举，越界会抛。
_LANGFUSE_KINDS = {
    "span",
    "agent",
    "chain",
    "tool",
    "generation",
    "embedding",
    "retriever",
    "evaluator",
    "guardrail",
}

# 这些 attr 直接映射到 Langfuse 的一等字段，而非 metadata。
_ATTR_MODEL = "gen_ai.request.model"
_ATTR_TOKENS_IN = "gen_ai.usage.input_tokens"
_ATTR_TOKENS_OUT = "gen_ai.usage.output_tokens"
_ATTR_THREAD_ID = "labhandler.thread_id"
_META_MAX = 200
_IO_CAP = 4000


def _cap(value: Any, limit: int = _IO_CAP) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 3] + "..."
    return value


def sanitize_metadata(attrs: dict[str, Any]) -> dict[str, str]:
    """Langfuse v4 要求 metadata 为 dict[str,str] 且值 ≤200 字；密钥类字段丢掉。"""
    out: dict[str, str] = {}
    for key, raw in attrs.items():
        if raw is None:
            continue
        name = str(key)
        low = name.lower()
        if any(tok in low for tok in ("api_key", "secret", "password", "token", "authorization")):
            continue
        text = str(raw)
        if len(text) > _META_MAX:
            text = text[: _META_MAX - 3] + "..."
        out[name[:_META_MAX]] = text
    return out


class LangfuseSink:
    """Langfuse 后端。构造失败或未配置密钥时由 build_sinks 跳过。

    启动时不做 auth_check；密钥由 .env 原样使用，导出失败只记日志，主流程不中断。
    """

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str,
        release: str | None = None,
        environment: str = "development",
        **_: object,
    ) -> None:
        from langfuse import Langfuse

        self.base_url = (host or "").strip().rstrip("/") or "https://cloud.langfuse.com"
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self.base_url,
            host=self.base_url,
            release=release,
            environment=environment,
            tracing_enabled=True,
        )

    def open(self, record: SpanRecord) -> Any:
        try:
            from langfuse import propagate_attributes

            kind = record.kind if record.kind in _LANGFUSE_KINDS else "span"
            meta = sanitize_metadata(record.attrs)
            kwargs: dict[str, Any] = {
                "name": record.name,
                "as_type": kind,
                "metadata": meta,
            }
            if record.inputs is not None:
                kwargs["input"] = _cap(record.inputs)
            model = record.attrs.get(_ATTR_MODEL)
            if kind == "generation" and model:
                kwargs["model"] = str(model)
            cm = self._client.start_as_current_observation(**kwargs)
            span = cm.__enter__()
            session_id = str(record.attrs.get(_ATTR_THREAD_ID) or "")[:200]
            prop_kw: dict[str, Any] = {"metadata": meta, "tags": ["labhandler"]}
            if session_id:
                prop_kw["session_id"] = session_id
            if record.parent_id is None:
                prop_kw["trace_name"] = record.name
            prop = propagate_attributes(**prop_kw)
            prop.__enter__()
            return (cm, prop, span)
        except Exception:
            return None

    def close(self, record: SpanRecord, handle: Any) -> None:
        if handle is None:
            return
        cm, prop, span = handle
        try:
            update: dict[str, Any] = {"metadata": sanitize_metadata(record.attrs)}
            if record.output is not None:
                update["output"] = _cap(record.output)
            if record.error:
                update["level"] = "ERROR"
                update["status_message"] = str(record.error)[:500]
            usage = {
                k: int(record.attrs[v])
                for k, v in (("input", _ATTR_TOKENS_IN), ("output", _ATTR_TOKENS_OUT))
                if isinstance(record.attrs.get(v), int)
            }
            if usage:
                update["usage_details"] = usage
            span.update(**update)
        except Exception:
            pass
        try:
            prop.__exit__(None, None, None)
        except Exception:
            pass
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass

    def event(
        self, record: SpanRecord | None, handle: Any, name: str, attrs: dict[str, Any]
    ) -> None:
        try:
            if handle is not None:
                _, _, span = handle
                span.create_event(name=name, metadata=sanitize_metadata(attrs))
                return
            self._client.start_observation(
                name=name, as_type="span", metadata=sanitize_metadata(attrs)
            ).end()
        except Exception:
            pass

    def trace_id(self) -> str | None:
        try:
            return self._client.get_current_trace_id()
        except Exception:
            return None

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.flush()


def build_sinks(
    *,
    jsonl_path: Path | None,
    langfuse_public_key: str,
    langfuse_secret_key: str,
    langfuse_host: str,
    release: str | None = None,
    environment: str = "development",
    log=None,
) -> list[Sink]:
    """按配置组装 sink 列表。Langfuse 不可用时只降级，不抛。"""
    sinks: list[Sink] = []
    if jsonl_path is not None:
        sinks.append(JsonlSink(jsonl_path))
    if langfuse_public_key and langfuse_secret_key:
        try:
            sinks.append(
                LangfuseSink(
                    public_key=langfuse_public_key,
                    secret_key=langfuse_secret_key,
                    host=langfuse_host,
                    release=release,
                    environment=environment,
                    log=log,
                )
            )
        except Exception as e:
            if log:
                log(f"[observe] Langfuse 初始化失败，已降级为本地 JSONL：{type(e).__name__}: {e}")
    return sinks
```

### `memory/db.py`

**新建。** SQLite 连接与 schema 初始化。归档表与向量表同库。

<!-- PACKFILE: memory/db.py -->
```python
"""SQLite 连接入口。archive 与 vectors 共用同一库文件，必须走本函数，连接参数才一致。"""

import sqlite3
from pathlib import Path

_BUSY_TIMEOUT_MS = 10_000


def connect(db_path: Path) -> sqlite3.Connection:
    """打开一条 WAL 连接：busy_timeout=10s，synchronous=NORMAL。

    /dream 与 memory_search 会并发打同一库；锁等待最多 10s，
    超时抛 OperationalError: database is locked。
    """
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
```

### `memory/__init__.py`

**覆盖。** 导出 archive / retrieve / vectors / dream / profile。

<!-- PACKFILE: memory/__init__.py -->
```python
"""跨 lab 记忆：任务归档、知识卡片检索、用户画像。"""

from .archive import TaskArchive, get_task_archive
from .profile import (
    add_field,
    append_rule,
    get_profile,
    inject_for_agent,
    load_profile,
    update_field,
)

__all__ = [
    "TaskArchive",
    "get_task_archive",
    "get_profile",
    "load_profile",
    "inject_for_agent",
    "update_field",
    "add_field",
    "append_rule",
]
```

### `memory/archive.py`

**覆盖。** 卡片入档。

<!-- PACKFILE: memory/archive.py -->
```python
"""任务归档的 SQLite 事实层。

task_archive 一行一次 lab；archive_cards 挂在其上。
卡片 markdown 与向量索引由 memory.retrieve / memory.vectors 派生；本模块不写文件。
card_type 仅 lesson / strategy / pattern。retired_at 非空视为淘汰，读接口一律排除。
"""

import hashlib
import os
import sqlite3
from typing import Any

from config.runtime import get_settings
from memory.db import connect

# card_type 仅允许这三项；其余在 create_cards 中丢弃。
VALID_CARD_TYPES = frozenset({"lesson", "strategy", "pattern"})


class TaskArchive:
    """绑定一个 memory.db 的归档读写。父目录不存在则创建。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path: str = db_path or str(get_settings().memory_db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """保证 task_archive 与 archive_cards 两表存在（IF NOT EXISTS）。"""
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_title TEXT NOT NULL,
                    task_type TEXT,
                    user_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS archive_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    card_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    vector_error TEXT,
                    content_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    retired_at TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES task_archive(id),
                    UNIQUE(task_id, card_type, content_hash)
                )
                """
            )
            conn.commit()

    # --- 写接口 -------------------------------------------------------

    def create_task(self, task_title: str, task_type: str, user_summary: str) -> int:
        """插入一条 task_archive，返回新 task_id。"""
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO task_archive (task_title, task_type, user_summary) VALUES (?, ?, ?)",
                (task_title, task_type, user_summary),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def create_cards(
        self, task_id: int, knowledge_cards: list[dict], task_title: str, task_type: str
    ) -> list[int]:
        """写入 archive_cards，返回实际插入的 card_id。

        不入库：card_type 不在白名单、content 为空、
        同 task 内 (card_type, content_hash) 已存在（UNIQUE，吞 IntegrityError）。
        search_text 由 task_type / card_type / task_title / content 拼成。
        """
        inserted_ids: list[int] = []
        with connect(self.db_path) as conn:
            for card in knowledge_cards:
                card_type = str(card.get("type", "")).strip()
                content = str(card.get("content", "")).strip()
                if card_type not in VALID_CARD_TYPES or not content:
                    continue

                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                search_text = (
                    f"任务类型: {task_type}\n"
                    f"卡片类型: {card_type}\n"
                    f"任务标题: {task_title}\n"
                    f"内容: {content}"
                )

                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO archive_cards
                            (task_id, card_type, content, search_text, content_hash)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (task_id, card_type, content, search_text, content_hash),
                    )
                    conn.commit()
                    inserted_ids.append(cursor.lastrowid or 0)
                except sqlite3.IntegrityError:
                    pass

        return inserted_ids

    def mark_card_vector_error(self, card_id: int, error: str) -> None:
        """把该卡向量索引失败原因写入 vector_error（截断 500 字）。"""
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE archive_cards SET vector_error = ? WHERE id = ?",
                (error[:500], card_id),
            )
            conn.commit()

    def clear_card_vector_error(self, card_id: int) -> None:
        """将该卡 vector_error 置空。"""
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE archive_cards SET vector_error = NULL WHERE id = ?",
                (card_id,),
            )
            conn.commit()

    def retire_cards(self, card_ids: list[int]) -> int:
        """把仍活跃的卡片 retired_at 置为当前时间，返回实际标记数。已淘汰的行不动。"""
        if not card_ids:
            return 0
        placeholders = ",".join("?" * len(card_ids))
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                f"""
                UPDATE archive_cards SET retired_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders}) AND retired_at IS NULL
                """,
                card_ids,
            )
            conn.commit()
            return cursor.rowcount

    # --- 读接口 -------------------------------------------------------

    def get_cards_for_indexing(self) -> list[dict[str, Any]]:
        """全部未淘汰卡片 + 父任务字段，按 card_id 升序。供建索引。"""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT c.id as card_id, c.card_type, c.content, c.search_text, c.vector_error,
                       t.id as task_id, t.task_title, t.task_type
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.retired_at IS NULL
                ORDER BY c.id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_cards_by_ids(self, card_ids: list[int]) -> list[dict[str, Any]]:
        """按 card_id 取未淘汰卡片及父任务。返回顺序与入参中仍存在的 id 一致。"""
        if not card_ids:
            return []
        placeholders = ",".join("?" * len(card_ids))
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT c.id as card_id, c.card_type, c.content, c.search_text,
                       t.id as task_id, t.task_title, t.task_type
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.id IN ({placeholders}) AND c.retired_at IS NULL
                """,
                card_ids,
            )
            rows_by_id = {row["card_id"]: dict(row) for row in cursor.fetchall()}
            return [rows_by_id[rid] for rid in card_ids if rid in rows_by_id]

    def get_all_active_cards(self) -> list[dict[str, Any]]:
        """全部未淘汰卡片（card_id / 分组键 / content），按 card_id 升序。/dream 输入。"""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT c.id as card_id, c.card_type, c.content, c.task_id,
                       t.task_title, t.task_type
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.retired_at IS NULL
                ORDER BY c.id
                """
            )
            return [dict(row) for row in cursor.fetchall()]


# 进程内唯一 TaskArchive，绑定 settings.memory_db_path。

_default_archive: TaskArchive | None = None


def get_task_archive() -> TaskArchive:
    """返回进程内唯一 TaskArchive。首次调用时按 settings.memory_db_path 创建。"""
    global _default_archive
    if _default_archive is None:
        _default_archive = TaskArchive()
    return _default_archive
```

### `memory/retrieve.py`

**覆盖。** memory_grep 支持 extra_roots（DumpScope）。

<!-- PACKFILE: memory/retrieve.py -->
```python
"""跨 lab 记忆检索。卡片 markdown 是事实源，向量表是派生索引。

读路径只允许 cards_dir 与 workspace_dir。语义检索 k 夹在 1..8；grep 命中上限默认 40；
单文件正文最多返回 80000 字。
"""

from pathlib import Path
import re
from typing import Any

from config.runtime import RuntimeSettings, get_settings
from memory.archive import get_task_archive
from memory.vectors import VectorIndex, content_sha256


def _cards_dir(settings: RuntimeSettings | None = None) -> Path:
    """返回 settings.cards_dir，目录保证存在。"""
    s = settings or get_settings()
    s.cards_dir.mkdir(parents=True, exist_ok=True)
    return s.cards_dir


def card_path(card_id: int, settings: RuntimeSettings | None = None) -> Path:
    """卡片文件路径：{cards_dir}/{card_id}.md。"""
    return _cards_dir(settings) / f"{card_id}.md"


def write_card_file(card: dict[str, Any], settings: RuntimeSettings | None = None) -> Path:
    """写出 {card_id}.md：YAML frontmatter（含 content_sha256）+ 正文 content。覆盖同 id 已有文件。"""
    cid = int(card["card_id"])
    body = str(card.get("content") or "")
    meta = [
        "---",
        f"card_id: {cid}",
        f"task_id: {card.get('task_id', '')}",
        f"card_type: {card.get('card_type') or card.get('type', '')}",
        f"task_title: {card.get('task_title', '')}",
        f"task_type: {card.get('task_type', '')}",
        f"content_sha256: {content_sha256(body)}",
        "---",
        "",
        body,
        "",
    ]
    path = card_path(cid, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(meta), encoding="utf-8")
    return path


def read_card_file(path: Path) -> str:
    """读卡片文件全文（含 frontmatter）。"""
    return path.read_text(encoding="utf-8")


def parse_card_body(text: str) -> str:
    """去掉首段 --- frontmatter，返回正文。无合法分隔则整份原文。"""
    if not text.startswith("---"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        return text
    return text[end + 5 :].strip()


async def embed_card_file(path: Path, llm, settings: RuntimeSettings | None = None) -> None:
    """用正文（无 frontmatter）嵌入并 upsert 到向量表。正文为空时退回全文前 2000 字。"""
    s = settings or get_settings()
    text = read_card_file(path)
    body = parse_card_body(text)
    vecs = await llm.embed([body or text[:2000]])
    VectorIndex(settings=s).upsert(str(path), body, vecs[0])


async def index_card_ids(card_ids: list[int], llm, settings: RuntimeSettings | None = None) -> dict[str, Any]:
    """按 archive 行写文件并建索引。单卡失败记入 errors，其余继续。"""
    s = settings or get_settings()
    archive = get_task_archive()
    cards = archive.get_cards_by_ids(card_ids)
    indexed = 0
    errors: list[str] = []
    for card in cards:
        try:
            path = write_card_file(card, s)
            await embed_card_file(path, llm, s)
            indexed += 1
        except Exception as e:
            errors.append(f"{card.get('card_id')}: {type(e).__name__}: {e}")
    return {"indexed": indexed, "failed": len(errors), "errors": errors}


def delete_card_file(card_id: int, settings: RuntimeSettings | None = None) -> None:
    """同时删除该卡的向量行与 markdown。文件不存在不报错。"""
    s = settings or get_settings()
    path = card_path(card_id, s)
    VectorIndex(settings=s).delete(str(path))
    path.unlink(missing_ok=True)


async def reconcile_index(llm, settings: RuntimeSettings | None = None) -> dict[str, int]:
    """以 cards_dir 下 *.md 为事实源对齐向量表。

    缺行、content_sha256 不一致、或 embedding_model 与当前设置不同 → 重建该行。
    没有对应文件的索引行删除。
    """
    s = settings or get_settings()
    idx = VectorIndex(settings=s)
    files = {str(p): p for p in _cards_dir(s).glob("*.md")}
    rows = {r["path"]: r for r in idx.all_rows()}
    rebuilt = 0
    dropped = 0
    for path_str, p in files.items():
        body = parse_card_body(read_card_file(p))
        sha = content_sha256(body)
        row = rows.get(path_str)
        if (
            row is None
            or row.get("content_sha256") != sha
            or row.get("embedding_model") != s.embedding_model
        ):
            await embed_card_file(p, llm, s)
            rebuilt += 1
    for path_str in rows:
        if path_str not in files:
            idx.delete(path_str)
            dropped += 1
    return {"rebuilt": rebuilt, "dropped": dropped}


async def memory_search(query: str, k: int, llm, settings: RuntimeSettings | None = None) -> str:
    """语义检索：嵌入 query，返回至多 min(k, 8) 条（至少 1）卡片正文前 400 字。

    无命中 "(no matches)"。文件已删的命中 snippet 为空。
    """
    s = settings or get_settings()
    vecs = await llm.embed([query])
    hits = VectorIndex(settings=s).search(vecs[0], k=max(1, min(k, 8)))
    if not hits:
        return "(no matches)"
    lines: list[str] = []
    for path, score in hits:
        p = Path(path)
        snippet = parse_card_body(read_card_file(p))[:400] if p.is_file() else ""
        lines.append(f"{p.name} score={score:.3f}\n{snippet}")
    return "\n---\n".join(lines)


def memory_grep(
    pattern: str,
    settings: RuntimeSettings | None = None,
    limit: int = 40,
    *,
    extra_roots: list[Path] | None = None,
) -> str:
    """在 cards_dir 以及 extra_roots（缺省为 workspace/.labhandler）下按正则扫文件。

    非法正则：[ERROR/Validation]。命中达 limit 后截断并标注。
    无命中 "(no matches)"。读失败的文件跳过。
    """
    s = settings or get_settings()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[ERROR/Validation] invalid regex: {e}"
    roots = [_cards_dir(s)]
    if extra_roots is not None:
        roots.extend(extra_roots)
    else:
        ws = s.workspace_dir / ".labhandler"
        if ws.is_dir():
            roots.append(ws)
    hits: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p}:{i}:{line[:200]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + f"\n[truncated limit={limit}]"
    return "\n".join(hits) if hits else "(no matches)"


def memory_read(path: str, settings: RuntimeSettings | None = None) -> str:
    """读允许范围内的文件，正文最多 80000 字。

    相对路径依次试 cards_dir、workspace_dir；绝对路径必须落在这两者之下。
    越权 [ERROR/PermissionError]，缺失 [ERROR/FileNotFoundError]。
    """
    s = settings or get_settings()
    candidate = Path(path)
    if not candidate.is_absolute():
        for base in (_cards_dir(s), s.workspace_dir):
            p = (base / path).resolve()
            try:
                p.relative_to(base.resolve())
            except ValueError:
                continue
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")[:80_000]
        return f"[ERROR/FileNotFoundError] {path}"
    resolved = candidate.resolve()
    allowed = [_cards_dir(s).resolve(), s.workspace_dir.resolve()]
    if not any(_is_under(resolved, a) for a in allowed):
        return f"[ERROR/PermissionError] path not allowed: {path}"
    if not resolved.is_file():
        return f"[ERROR/FileNotFoundError] {path}"
    return resolved.read_text(encoding="utf-8", errors="replace")[:80_000]


def _is_under(path: Path, root: Path) -> bool:
    """path 是否位于 root 之下（含 root 自身）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
```

### `memory/vectors.py`

**覆盖。** 向量索引与 embedding_model 对账重建。

<!-- PACKFILE: memory/vectors.py -->
```python
"""卡片向量索引（SQLite card_vectors 表）。

卡片 markdown 为事实源；本表按 path 存 embedding。
content_sha256 + embedding_model 供对账：与当前正文或当前模型不一致则重建。
向量为 little-endian float32 blob。
"""

import hashlib
import os
import struct
from pathlib import Path

import numpy as np

from config.runtime import RuntimeSettings, get_settings
from memory.db import connect


def _pack(vec: list[float]) -> bytes:
    """float 列表 → little-endian float32 blob。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> np.ndarray:
    """little-endian float32 blob → 一维 ndarray。长度必须是 4 的倍数。"""
    n = len(blob) // 4
    return np.frombuffer(blob, dtype="<f4", count=n)



class VectorIndex:
    """一张 memory.db 上的派生索引。父目录不存在则创建。"""

    def __init__(
        self,
        db_path: Path | None = None,
        settings: RuntimeSettings | None = None,
        embed_fn=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = Path(db_path or self.settings.memory_db_path)
        self.embed_fn = embed_fn
        os.makedirs(self.db_path.parent, exist_ok=True)
        self._init()

    def _init(self) -> None:
        """保证 card_vectors 表存在（IF NOT EXISTS）。"""
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS card_vectors (
                    path TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    vector BLOB NOT NULL
                )
                """
            )
            conn.commit()

    def upsert(self, path: str, content: str, vector: list[float]) -> None:
        """按 path 覆盖写入向量。content_sha256 取自当前正文，模型名取 settings.embedding_model。"""
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO card_vectors(path, content_sha256, embedding_model, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_sha256=excluded.content_sha256,
                    embedding_model=excluded.embedding_model,
                    vector=excluded.vector
                """,
                (path, sha, self.settings.embedding_model, _pack(vector)),
            )
            conn.commit()

    def delete(self, path: str) -> None:
        """删除该 path 的索引行。行不存在则无操作。"""
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM card_vectors WHERE path = ?", (path,))
            conn.commit()

    def get(self, path: str) -> dict | None:
        """读该 path 的元数据（不含向量 blob）。无此行返回 None。"""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT path, content_sha256, embedding_model FROM card_vectors WHERE path = ?",
                (path,),
            ).fetchone()
            return dict(row) if row else None

    def search(self, query_vec: list[float], k: int = 5) -> list[tuple[str, float]]:
        """余弦相似度 top-k：(path, score) 按分数降序。空表或无可比行返回 []。

        向量维度与 query_vec 不一致的行不参与本次排序。
        """
        q = np.asarray(query_vec, dtype=np.float32)
        qn = float(np.linalg.norm(q)) or 1.0
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT path, vector FROM card_vectors").fetchall()
        if not rows:
            return []

        dim = q.shape[0]
        paths: list[str] = []
        vectors: list[np.ndarray] = []
        for path, blob in rows:
            vec = _unpack(blob)
            if vec.shape[0] != dim:
                # 维度与查询向量不一致的行不进入本次排序
                continue
            paths.append(path)
            vectors.append(vec)
        if not paths:
            return []

        matrix = np.vstack(vectors)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ q) / (norms * qn)
        top = np.argsort(-scores)[:k]
        return [(paths[i], float(scores[i])) for i in top]

    def all_rows(self) -> list[dict]:
        """全部索引行的元数据（path / content_sha256 / embedding_model），不含向量 blob。"""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT path, content_sha256, embedding_model FROM card_vectors"
                )
            ]


def content_sha256(text: str) -> str:
    """UTF-8 SHA-256 hex。卡片正文与索引对账用同一函数。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

### `memory/dream.py`

**覆盖。** /dream 离线合并淘汰卡片并重建索引。

<!-- PACKFILE: memory/dream.py -->
```python
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
from runtime.schema_call import DREAM_SCHEMA, SUBMIT_DREAM, oneshot_schema

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
```

### `memory/profile.py`

**覆盖。** profile 读写。

<!-- PACKFILE: memory/profile.py -->
```python
"""用户画像：profile/me.yaml 为唯一事实源。

点号路径读写（identity.name、preferences.writing_style.formality）。
写回先落同目录 .tmp，再 POSIX rename：崩溃后磁盘上始终是一份完整 YAML。
load：文件缺失或 YAML 非法时得到空 dict，不抛。
inject_for_agent 把 identity / preferences / style_rules 拼进名单内 agent 的 system 末尾。
"""

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from config.runtime import get_settings


# ─── 内部 helper ─────────────────────────────────────────────


def _profile_path() -> Path:
    """返回 settings.profile_path。"""
    return get_settings().profile_path


def _atomic_write(data: dict[str, Any]) -> None:
    """把 dict 写成 YAML。先写 .tmp 再 os.replace；失败则 shutil.move。崩溃后磁盘上始终是完整文件。"""
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    # os.replace 在 POSIX 上原子替换；失败则 shutil.move
    try:
        os.replace(tmp, path)
    except Exception:
        shutil.move(str(tmp), str(path))


# ─── 公开 API ───────────────────────────────────────────────────


def load_profile() -> dict[str, Any]:
    """读 profile YAML 为 dict。文件缺失、非 dict、或解析失败 → 空 dict，不抛。"""
    path = _profile_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def get_profile() -> dict[str, Any]:
    """当前 profile dict，语义同 load_profile。"""
    return load_profile()


def update_field(dotted_path: str, value: Any) -> dict[str, Any]:
    """覆盖点号路径上已有字段，原子写回，返回写后的整份 dict。

    路径不存在或中间节点非 dict 时抛 KeyError。不创建缺失父节点。
    示例：update_field("preferences.writing_style.formality", "high")
    """
    data = load_profile()
    parts = dotted_path.split(".")
    cur = data
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(f"Path does not exist: {dotted_path} (broken at {p})")
        cur = cur[p]
    if not isinstance(cur, dict):
        raise KeyError(f"Path is not a dict: {dotted_path}")
    cur[parts[-1]] = value
    _atomic_write(data)
    return data


def add_field(dotted_path: str, value: Any) -> dict[str, Any]:
    """在点号路径写入字段，缺失的父节点建成空 dict，原子写回，返回写后的整份 dict。"""
    data = load_profile()
    parts = dotted_path.split(".")
    cur = data
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
    _atomic_write(data)
    return data


def inject_for_agent(
    agent_name: str,
    system_prompt: str,
    *,
    rules: list[str] | None = None,
    include_rules: bool = True,
) -> str:
    """把 identity / preferences 以及本 lab 适用的 style_rules 拼到 system 末尾。

    仅 coder / planner / verifier / summarizer / pro / flash 会注入。
    include_rules=False 时不写规则段（起草 SPEC、remember_judge 裁定前）。
    rules 非 None 时只用这份名单，不再读 profile 全量 style_rules。
    优先级：当轮用户指令 > 本 lab 适用规则 > skill SOP。
    """
    agent = agent_name.lower()
    if agent not in {"coder", "planner", "verifier", "summarizer", "pro", "flash"}:
        return system_prompt

    data = load_profile()
    if not data:
        return system_prompt

    identity = data.get("identity") or {}
    prefs = data.get("preferences") or {}

    lines = ["", "## User preferences (profile)"]
    if identity:
        name = identity.get("name", "")
        sid = identity.get("student_id", "")
        if name or sid:
            lines.append(f"- User: {name} (student ID {sid})")
    if prefs:
        lang = prefs.get("language")
        if lang:
            lines.append(f"- Language: {lang}")
        ws = prefs.get("writing_style") or {}
        if ws:
            formality = ws.get("formality", "medium")
            avg_len = ws.get("avg_sentence_len", 25)
            lines.append(
                f"- Writing style: formality={formality}, avg sentence len≈{avg_len}"
            )
        cs = prefs.get("coding_style") or {}
        if cs:
            th = "requires type hints" if cs.get("type_hints") else "type hints optional"
            ds = cs.get("docstring", "short")
            lines.append(f"- Coding style: {th}; docstring={ds}")

    if include_rules and rules is None:
        rules = prefs.get("style_rules") or []
    chosen = [str(r).strip() for r in (rules or []) if str(r).strip()] if include_rules else []
    if include_rules:
        lines.append("")
        lines.append("## User long-term rules applicable to this lab")
        lines.append("")
        lines.append(
            "### Precedence (holds for every skill type: coding / essay / lab_report / other)"
        )
        lines.append(
            "- These long-term rules **outrank the current skill SOP**. When a rule conflicts with any "
            "convention in a loaded skill SOP (placeholder format, file naming, "
            "section wording, writing style, ...), the rule is authoritative and the SOP's conflicting "
            "wording is void — this holds even when the SOP demonstrates its own version through concrete "
            "examples in a loaded reference file."
        )
        lines.append(
            "- Not affected by the above: current-turn user instructions still take precedence over these "
            "rules; the problem statement's own explicit requirements and the academic-integrity block are "
            "separate sources and are not overridden here."
        )
        lines.append("")
        if chosen:
            for r in chosen:
                lines.append(f"- {r}")
        else:
            lines.append("- （本 lab 没有适用的 /remember 规则）")

    return system_prompt + "\n".join(lines)


def append_rule(rule: str) -> dict[str, Any]:
    """在 preferences.style_rules 末尾追加一条规则，原子写回，返回写后的整份 dict。

    空字符串抛 ValueError。style_rules 缺失或非 list 时先建成空 list 再追加。
    """
    text = (rule or "").strip()
    if not text:
        raise ValueError("rule is empty")
    data = load_profile()
    prefs = data.setdefault("preferences", {})
    if not isinstance(prefs, dict):
        prefs = {}
        data["preferences"] = prefs
    rules = prefs.get("style_rules")
    if not isinstance(rules, list):
        rules = []
    rules.append(text)
    prefs["style_rules"] = rules
    _atomic_write(data)
    return data
```

### `tools/fs_tools.py`

**覆盖。** read_file：1-based offset、limit 行、80k 字符封顶，截断时给出 next offset。

<!-- PACKFILE: tools/fs_tools.py -->
```python
"""host workspace 文件操作。路径与命令一律经 SecurityPolicy，调用记入 ToolAuditor。

不变量：读写只落在 WORKSPACE_DIR 内。越界 / 非白名单命令以
[ERROR/PermissionError] 字符串返回（含 PERM_HINT），到不了 OS。
read 默认从第 1 行起，可传 1-based offset / limit（行数）；单次最多 80_000 字符，
截断时标明下一行 offset。list_dir / glob 上限 80 条；grep 上限 40 条。
patch_file 要求 old 在文件中恰好出现一次。host_bash cwd=WORKSPACE_DIR，
默认超时 30s。FileNotFound / IsADirectory / 非法 regex 走错误通道并审计。
"""

import subprocess
from pathlib import Path

from config.runtime import get_settings
from tools.policy import PERM_HINT, get_auditor, get_policy
from tools.workspace_utils import is_excluded_path

WORKSPACE_DIR: Path = get_settings().workspace_dir
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

_GREP_LIMIT = 40
_GLOB_LIMIT = 80
_READ_CHAR_CAP = 80_000


def _perm_msg(e: PermissionError) -> str:
    return f"[ERROR/PermissionError] {e}\nHint: {PERM_HINT}"


def _denied(tool_name: str, args: dict, e: PermissionError) -> str:
    get_auditor().record(tool_name, args, f"denied:{e}")
    return _perm_msg(e)


def read_file(path: str, offset: int = 1, limit: int | None = None) -> str:
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("read_file", {"path": path}, e)
    if not p.exists():
        get_auditor().record("read_file", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        get_auditor().record("read_file", {"path": path}, "error:IsADirectoryError")
        raise IsADirectoryError(f"Not a file: {path}")
    if offset < 1:
        return "[ERROR/Validation] offset must be a 1-based line number"
    if limit is not None and limit < 1:
        return "[ERROR/Validation] limit must be a positive line count"

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    n = len(lines)
    start = offset - 1
    if start >= n:
        get_auditor().record("read_file", {"path": path, "offset": offset}, "ok")
        return f"[ERROR/Validation] offset {offset} past end ({n} lines)"
    end = n if limit is None else min(n, start + limit)
    chunk = "".join(lines[start:end])
    next_line = end + 1
    leftover_lines = n - end
    if len(chunk) > _READ_CHAR_CAP:
        cut = chunk[:_READ_CHAR_CAP]
        nl = cut.rfind("\n")
        if nl >= 0:
            cut = cut[: nl + 1]
        next_line = offset + cut.count("\n")
        leftover_lines = n - (next_line - 1)
        chunk = cut.rstrip("\n") + (
            f"\n[truncated at char cap {_READ_CHAR_CAP}; next offset={next_line}, {leftover_lines} lines remain]"
        )
    elif leftover_lines > 0:
        chunk += f"\n[truncated {leftover_lines} lines; next offset={next_line}]"
    get_auditor().record(
        "read_file",
        {"path": path, "offset": offset, "limit": limit, "n_chars": len(chunk)},
        "ok",
    )
    return chunk


def write_file(path: str, content: str, role: str = "write") -> str:
    try:
        p = get_policy().check_write(path, role)
    except PermissionError as e:
        return _denied("write_file", {"path": path}, e)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    get_auditor().record("write_file", {"path": path, "n_chars": len(content)}, "ok")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


def list_dir(path: str = ".") -> list[str] | str:
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("list_dir", {"path": path}, e)
    if not p.exists():
        get_auditor().record("list_dir", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"Directory not found: {path}")
    if not p.is_dir():
        get_auditor().record("list_dir", {"path": path}, "error:NotADirectoryError")
        raise NotADirectoryError(f"Not a directory: {path}")
    get_auditor().record("list_dir", {"path": path}, "ok")
    names = sorted(x.name for x in p.iterdir())
    if len(names) > _GLOB_LIMIT:
        return names[:_GLOB_LIMIT] + [f"[truncated {len(names) - _GLOB_LIMIT} entries]"]
    return names


def patch_file(path: str, old: str, new: str, role: str = "write") -> str:
    try:
        p = get_policy().check_write(path, role)
    except PermissionError as e:
        return _denied("patch_file", {"path": path}, e)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        get_auditor().record("patch_file", {"path": path}, "error:old_not_found")
        raise ValueError(f"patch_file: old string not found in {path}")
    if count > 1:
        get_auditor().record("patch_file", {"path": path}, f"error:old_x{count}")
        raise ValueError(
            f"patch_file: old string appears {count} times in {path} (must be unique)"
        )
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    get_auditor().record("patch_file", {"path": path}, "ok")
    return f"patched {path} (1 occurrence)"


def glob_files(pattern: str, limit: int = _GLOB_LIMIT) -> list[str]:
    root = WORKSPACE_DIR
    get_policy().check_glob(pattern)
    matches: list[str] = []
    for p in root.glob(pattern):
        if not p.is_file() or not get_policy().contains(p):
            continue
        rel = p.relative_to(root)
        if is_excluded_path(rel):
            continue
        matches.append(str(rel))
        if len(matches) >= limit:
            matches.append(f"[truncated limit={limit}]")
            break
    get_auditor().record("glob_files", {"pattern": pattern}, "ok")
    return matches


def grep_files(pattern: str, glob: str = "**/*", limit: int = _GREP_LIMIT) -> str:
    import re

    root = WORKSPACE_DIR
    get_policy().check_glob(glob)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[ERROR/Validation] invalid regex: {e}"
    hits: list[str] = []
    truncated = False
    for p in root.glob(glob):
        if not p.is_file() or not get_policy().contains(p):
            continue
        rel = p.relative_to(root)
        if is_excluded_path(rel) and ".labhandler" not in rel.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}:{line[:200]}")
                if len(hits) >= limit:
                    truncated = True
                    break
        if truncated:
            break
    get_auditor().record("grep_files", {"pattern": pattern}, "ok")
    if not hits:
        return "(no matches)"
    body = "\n".join(hits)
    if truncated:
        body += f"\n[truncated limit={limit}]"
    return body


def host_bash(cmd: str, timeout: int = 30) -> str:
    try:
        get_policy().check_command(cmd)
    except PermissionError as e:
        return _denied("host_bash", {"cmd": cmd}, e)
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        get_auditor().record("host_bash", {"cmd": cmd}, f"error:timeout_{timeout}s")
        return f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}"
    get_auditor().record("host_bash", {"cmd": cmd}, f"ok:exit={result.returncode}")
    out = result.stdout or ""
    err = result.stderr or ""
    tail = f"\n[exit={result.returncode}]"
    if err:
        return f"{out}\n--- stderr ---\n{err}{tail}"
    return out + tail
```

### `tools/skill_tool.py`

**覆盖。** SkillBind：全程只能绑一个 skill；同名可重读，换名拒绝。gate 约束 reference/script。

<!-- PACKFILE: tools/skill_tool.py -->
```python
"""skill 工具层：目录、SOP、reference、script。

一次 lab 至多绑定一个 skill（coding / essay / lab_report 互斥），也可以不绑。
load_skill 写入绑定；同名再调只是重读 SOP，换名直接拒绝。
load_skill_reference / use_skill_script 必须命中已绑定的那一份。
"""

from pathlib import Path

from skills.repository import (
    list_skill_documents,
    list_skill_references,
    list_skill_scripts,
    load_skill_document,
    load_skill_reference as _load_skill_reference,
)


LOAD_SKILL = "load_skill"
LOAD_SKILL_REFERENCE = "load_skill_reference"
USE_SKILL_SCRIPT = "use_skill_script"


def list_skill_meta() -> list[dict[str, str]]:
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "when_to_use": s["when_to_use"],
        }
        for s in list_skill_documents()
    ]


def _available() -> str:
    names = [s["name"] for s in list_skill_documents()]
    return ", ".join(names) if names else "none"


class SkillBind:
    """一次 lab 的 skill 槽：空，或锁死一个名字。"""

    def __init__(self) -> None:
        self.name: str | None = None

    def load(self, skill_name: str) -> str:
        skill_name = (skill_name or "").strip()
        if not skill_name:
            return "[ERROR/Validation] missing skill_name"
        if self.name is not None and self.name != skill_name:
            return (
                f"[ERROR/Validation] skill already bound to {self.name!r}; "
                f"one skill per lab (available: {_available()})"
            )
        try:
            doc = load_skill_document(skill_name)
        except FileNotFoundError:
            return (
                f"[ERROR/FileNotFoundError] skill not found: {skill_name} "
                f"(available: {_available()})"
            )
        self.name = skill_name
        refs = list_skill_references(skill_name)
        scripts = list_skill_scripts(skill_name)
        lines = [f"# {doc['name'] or skill_name}", "", doc["body"]]
        if refs:
            lines.append("")
            lines.append(f"references: {', '.join(refs)}")
        if scripts:
            lines.append(f"scripts: {', '.join(scripts)}")
        return "\n".join(lines)

    def gate(self, skill_name: str) -> str:
        """空串放行；否则是给模型看的错误。"""
        skill_name = (skill_name or "").strip()
        if self.name is None:
            return "[ERROR/Validation] load_skill first (or skip skills this lab)"
        if skill_name != self.name:
            return (
                f"[ERROR/Validation] bound to {self.name!r}, not {skill_name!r}"
            )
        return ""


def load_skill_reference(skill_name: str, ref_name: str) -> str:
    try:
        return _load_skill_reference(skill_name, ref_name)
    except (FileNotFoundError, PermissionError) as e:
        return f"[ERROR/{type(e).__name__}] {e}"


def use_skill_script(skill_name: str, script_name: str) -> str:
    import shutil

    from config.runtime import get_settings

    src = get_settings().skills_dir / skill_name / "scripts" / Path(script_name).name
    if not src.is_file():
        available = list_skill_scripts(skill_name)
        return (
            f"[ERROR/FileNotFoundError] script not found: "
            f"{skill_name}/scripts/{script_name} (available: {available or 'none'})"
        )
    dest_dir = get_settings().workspace_dir / ".labhandler" / "scripts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)
    return (
        f"script staged at: /workspace/.labhandler/scripts/{src.name}"
        f" (run it with sandbox_execute_bash, e.g. `python /workspace/.labhandler/scripts/{src.name}`)"
    )
```

### `tools/policy.py`

**覆盖。** host 白名单与路径守护。

<!-- PACKFILE: tools/policy.py -->
```python
"""host 端工具的 PreToolUse 策略与实时审计。

SecurityPolicy 是 host 路径边界、host_bash 命令白名单、路径逃逸正则
的唯一实现。检查失败抛 PermissionError；fs_tools 译成
[ERROR/PermissionError] 观测供 ReAct 自纠正。危险命令到不了 subprocess.run。

ToolAuditor 把每次调用追加一行 JSONL 到
workspace/.labhandler/audit.jsonl。审计失败静默，不阻断工具。

不变量：/etc/passwd、../../etc、cd .. && ls、~ 展开一律拒绝。
回归见 AGENTS.md 安全边界节。
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from config.runtime import get_settings

# PermissionError 后给 LLM 的提示（fs_tools 用于 ReAct 自纠正）
PERM_HINT = (
    "host fs tools may only operate inside WORKSPACE_DIR; host_bash allows only whitelisted commands "
    "(pytest, python, git, etc.) and forbids .., absolute-path prefixes, and ~ expansion. "
    "Use relative paths instead (e.g. 'solution.py' rather than '/workspace/solution.py', "
    "`pytest -q test_x.py` rather than `cd /workspace && pytest`), "
    "or use sandbox_run_python / sandbox_file_operations to access /workspace/* inside the container."
)


def _extract_cmd_names(cmd: str) -> list[str]:
    """从命令字符串提取所有基础命令名（处理 | && || ; 链）。"""
    names: list[str] = []
    for segment in re.split(r'\s*&&\s*|\s*\|\|\s*|\s*\|\s*|\s*;\s*', cmd):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split()
        if not parts:
            continue
        name = parts[0].lstrip('./')
        if name:
            names.append(name)
    return names


class SecurityPolicy:
    """host 端工具安全策略：路径边界 + 命令白名单 + 逃逸正则。"""

    # host_bash 白名单：只放行下列基础命令；不在集合内的命令名直接拒绝。
    # 路径逃逸正则是第二道；cwd=WORKSPACE_DIR 是第三道。
    #
    # bash / sh / docker 不在白名单：载荷是字符串，逃逸正则挡不住
    # `bash -c` 里编码过的路径；docker 还能把宿主机挂进容器。
    # 复杂 shell 走 sandbox_execute_bash（容器隔离）。
    ALLOWED_COMMANDS = frozenset({
        'pytest', 'python', 'python3', 'ls', 'mkdir', 'rm', 'cp', 'mv',
        'cat', 'echo', 'git', 'pip', 'pip3', 'chmod', 'touch',
        'head', 'tail', 'wc', 'sort', 'uniq', 'diff', 'which',
        'ln', 'find', 'grep', 'sed', 'awk', 'tree',
    })

    # 路径逃逸防护（不论是否在白名单都检查）
    # 边界字符含引号/括号/等号，以便拦截藏在字符串里的绝对路径
    # （如 python -c "open('/etc/passwd')"）：只认空白前缀的话，
    # 带引号的绝对路径和 .. 穿越都能绕过。
    _BOUNDARY = r"(^|[\s'\"=(])"
    ESCAPE_PATTERNS = [
        re.compile(_BOUNDARY + r"\.\.([/\\\s'\")]|$)"),  # 独立 .. token / 路径穿越（含引号、括号内）
        re.compile(_BOUNDARY + r"/[a-zA-Z]"),            # / 前缀绝对路径（cat /etc/x、open('/etc/x')）
        re.compile(r"(?<![\w.])/(?:etc|usr|var|root|proc|sys|bin|sbin|boot|dev|lib|opt|home)(?:/|\b)"),  # 系统绝对路径（任意前缀如 +/etc；foo/etc 这类相对子目录不受影响）
        re.compile(_BOUNDARY + r"~/"),                   # ~/path 家目录展开
        re.compile(_BOUNDARY + r"~($|[\s'\")])"),        # 独立 ~ token
    ]

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def safe_path(self, p: str) -> Path:
        """解析输入路径并验证其在 workspace_dir 内；越界抛 PermissionError。"""
        if not isinstance(p, str) or not p:
            raise PermissionError(f"Illegal path: {p!r}")
        candidate = (self.workspace_dir / p) if not Path(p).is_absolute() else Path(p)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError as e:
            raise PermissionError(
                f"Out-of-bounds path: {p!r} resolves to {resolved}, not under {self.workspace_dir}"
            ) from e
        return resolved

    def check_write(self, p: str, role: str) -> Path:
        """写路径检查：workspace 边界 + acceptance/ 仅 Pro + session 文件仅 Pro。"""
        resolved = self.safe_path(p)
        rel = resolved.relative_to(self.workspace_dir)
        parts = rel.parts
        if role != "pro" and "acceptance" in parts:
            raise PermissionError(
                f"acceptance/ is Pro-only write; role={role!r} cannot write {p!r}"
            )
        if role != "pro" and ".labhandler" in parts and "scripts" not in parts:
            raise PermissionError(
                f"session files under .labhandler/ are Pro/harness-only; role={role!r} cannot write {p!r}"
            )
        return resolved

    def check_glob(self, pattern: str) -> str:
        """glob/grep 的 pattern 校验：必须相对 workspace，禁止 /、~、..。

        glob/grep 不走 safe_path；pathlib 会展开 `../`，`root.glob('../**/*')`
        能列出 workspace 外文件。非法 pattern 抛 PermissionError。
        """
        if not isinstance(pattern, str) or not pattern.strip():
            raise PermissionError(f"Illegal glob pattern: {pattern!r}")
        if pattern.startswith("/") or pattern.startswith("~"):
            raise PermissionError(
                f"glob pattern must be relative to workspace: {pattern!r}"
            )
        if ".." in Path(pattern).parts:
            raise PermissionError(
                f"glob pattern may not traverse outside workspace: {pattern!r}"
            )
        return pattern

    def contains(self, path: Path) -> bool:
        """路径是否落在 workspace 内。用于过滤 glob 结果，不抛异常。"""
        try:
            path.resolve().relative_to(self.workspace_dir)
        except (ValueError, OSError):
            return False
        return True

    def check_command(self, cmd: str) -> None:
        """host_bash cmd 字符串白名单预检 + 路径逃逸巡查；抛 PermissionError。"""
        if not isinstance(cmd, str) or not cmd.strip():
            raise PermissionError(f"Illegal command: {cmd!r}")

        for name in _extract_cmd_names(cmd):
            if name not in self.ALLOWED_COMMANDS:
                raise PermissionError(
                    f"host_bash command not in whitelist: {name!r} "
                    f"(allowed commands: {sorted(self.ALLOWED_COMMANDS)})"
                )

        for pat in self.ESCAPE_PATTERNS:
            if pat.search(cmd):
                raise PermissionError(
                    f"host_bash command contains path escape: {cmd!r} (pattern={pat.pattern})"
                )


class ToolAuditor:
    """实时工具调用审计器：向 workspace/.labhandler/audit.jsonl 追加 JSONL。"""

    _MAX_ARG_CHARS = 300

    def __init__(self, audit_path: Path) -> None:
        self.audit_path = audit_path

    def record(self, tool: str, args: dict[str, Any], outcome: str) -> None:
        """记录一条审计（outcome 取 ok | denied:<reason> | error:<exception>）。失败静默。"""
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool,
                "args": {
                    k: (v if len(s := str(v)) <= self._MAX_ARG_CHARS
                        else s[: self._MAX_ARG_CHARS] + "…")
                    for k, v in args.items()
                },
                "outcome": outcome[:500],
            }
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # 审计不能拖垮工具调用


# ─── 模块级单例 ─────────────────────────────────────────

_policy: SecurityPolicy | None = None
_auditor: ToolAuditor | None = None


def get_policy() -> SecurityPolicy:
    global _policy
    if _policy is None:
        _policy = SecurityPolicy(get_settings().workspace_dir)
    return _policy


def get_auditor() -> ToolAuditor:
    global _auditor
    if _auditor is None:
        _auditor = ToolAuditor(
            get_settings().workspace_dir / ".labhandler" / "audit.jsonl"
        )
    return _auditor
```

### `tools/sandbox_tools.py`

**覆盖。** MCP 沙箱工具。

<!-- PACKFILE: tools/sandbox_tools.py -->
```python
"""AIO Sandbox MCP 工具封装。

宿主机路径经 sandbox_workspace_path 映射到容器 /workspace；越界抛 ValueError。
call_sandbox 把 MCP 返回值收成文本：同一工具连续失败满 3 次记
[SANDBOX_UNREACHABLE]，其余记 [tool_error]。sandbox_execute_code 若只收到
ack（stdout/stderr/exit_code 皆 null）会附加改走 sandbox_execute_bash 的提示。
list_sandbox_tool_names 在 MCP 不可达时返回空列表。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from config.runtime import get_settings
from mcp_client import call_mcp_tool, list_mcp_tools

_SANDBOX_WORKSPACE = "/workspace"
_SANDBOX_MAX_FAILURES = 3
_sandbox_failures: dict[str, int] = {}
_PATH_KW = {"path", "file_path"}


def sandbox_workspace_path(path: Path | str) -> str:
    """宿主机路径 → 容器内 /workspace 路径。越界抛 ValueError。"""
    host_ws = get_settings().workspace_dir
    target = Path(path).resolve()
    rel = target.relative_to(host_ws)
    return f"{_SANDBOX_WORKSPACE}/{rel}".replace("\\", "/") if str(rel) != "." else _SANDBOX_WORKSPACE


def _translate_path(p: str) -> str:
    if not p or not isinstance(p, str):
        return p
    if p.startswith(_SANDBOX_WORKSPACE) or not p.startswith("/"):
        return p
    try:
        return sandbox_workspace_path(p)
    except (ValueError, OSError):
        return p


def _translate_kwargs(kwargs: dict[str, Any]) -> None:
    for k in list(kwargs.keys()):
        if k in _PATH_KW and isinstance(kwargs[k], str):
            kwargs[k] = _translate_path(kwargs[k])


_ACK_ONLY_HINT = (
    "\n\n[labhandler] ack-only: the Jupyter kernel dispatched an async ack"
    " (stdout/stderr/exit_code are all null); the code may not have finished executing yet."
    " To get results synchronously, use sandbox_execute_bash to run the command instead."
)


def reset_sandbox_failure_counter() -> None:
    _sandbox_failures.clear()


def _content_to_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(item.text or "")
            elif isinstance(item, dict):
                parts.append(str(item.get("text", item)))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    else:
        text = str(content)
    return text


def _annotate_ack_only(text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("stdout") is None
        and payload.get("stderr") is None
        and payload.get("exit_code") is None
    ):
        return text + _ACK_ONLY_HINT
    return text


async def call_sandbox(tool_name: str, **kwargs: Any) -> str:
    _translate_kwargs(kwargs)
    try:
        result = await call_mcp_tool(tool_name, kwargs)
    except Exception as e:
        _sandbox_failures[tool_name] = _sandbox_failures.get(tool_name, 0) + 1
        count = _sandbox_failures[tool_name]
        if count >= _SANDBOX_MAX_FAILURES:
            return (
                f"[SANDBOX_UNREACHABLE] sandbox tool {tool_name} failed {count} times in a row"
                f" ({type(e).__name__}: {e}); the sandbox may be unavailable"
            )
        return f"[tool_error] {type(e).__name__}: {e}"
    _sandbox_failures[tool_name] = 0
    text = _content_to_text(result)
    if tool_name == "sandbox_execute_code":
        text = _annotate_ack_only(text)
    return text


async def sandbox_convert_to_markdown(file_path: str) -> str:
    container_path = _translate_path(file_path)
    if not container_path.startswith(("file://", "http://", "https://", "data:")):
        container_path = f"file://{container_path}"
    return await call_sandbox("sandbox_convert_to_markdown", uri=container_path)


async def list_sandbox_tool_names() -> list[str]:
    try:
        tools = await list_mcp_tools()
    except Exception:
        return []
    return [getattr(t, "name", "") for t in tools if getattr(t, "name", "")]


async def sandbox_run(command: str, *, timeout: float) -> tuple[int, str]:
    """在沙箱里跑一条 bash 命令，返回 (exit_code, 合并日志)。

    超时、[SANDBOX_UNREACHABLE]、[tool_error]、缺 exit_code：exit_code=-1，
    日志带标记。非 JSON / 非 dict 载荷按 exit_code=0 原样返回。
    调用方据此区分环境失败与被测命令失败。
    """
    try:
        raw = await asyncio.wait_for(
            call_sandbox("sandbox_execute_bash", command=command), timeout=timeout
        )
    except asyncio.TimeoutError:
        return -1, f"[TIMEOUT after {timeout}s] {command}"

    if "[SANDBOX_UNREACHABLE]" in raw:
        return -1, raw
    if raw.startswith("[tool_error]"):
        return -1, raw

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0, raw
    if not isinstance(payload, dict):
        return 0, raw

    exit_code = payload.get("exit_code")
    log = "\n".join(
        str(payload.get(k) or "") for k in ("stdout", "stderr") if payload.get(k)
    )
    if exit_code is None:
        return -1, (log or raw) + "\n[labhandler] 沙箱未返回 exit_code"
    return int(exit_code), log or raw
```

### `tools/search_tool.py`

**覆盖。** DDG 检索。

<!-- PACKFILE: tools/search_tool.py -->
```python
"""DuckDuckGo 网页搜索。

web_search 在线程里调 DDGS；max_results 夹到 [1, 8]。空结果会按
search_max_retries 重试；耗尽后若有异常返回一条 [search failed: ...] snippet，
无异常则空列表。代理取 settings.proxy。
"""

import asyncio
import warnings

from config.runtime import get_settings

warnings.filterwarnings("ignore", category=DeprecationWarning, module="duckduckgo_search")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="ddgs")


def _search_once(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    settings = get_settings()
    results = []
    with DDGS(proxy=settings.proxy) as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
            )
    return results


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    settings = get_settings()
    last_err: Exception | None = None
    cap = min(max(1, max_results), 8)
    for attempt in range(settings.search_max_retries + 1):
        try:
            results = await asyncio.to_thread(_search_once, query, cap)
            if results:
                return results
        except Exception as e:
            last_err = e
            if attempt < settings.search_max_retries:
                await asyncio.sleep(settings.search_retry_delay)
    if last_err:
        return [{"title": "", "url": "", "snippet": f"[search failed: {type(last_err).__name__}]"}]
    return []
```

### `tools/profile_tool.py`

**覆盖。** 追加规则写入 profile。

<!-- PACKFILE: tools/profile_tool.py -->
```python
"""用户长期偏好 profile 的工具层入口。

read_profile / update_profile / add_profile_field 委托 memory.profile；
本模块只暴露工具签名，不持有存储。
"""

from typing import Any


def read_profile() -> dict:
    from memory import load_profile

    return load_profile()


def update_profile(path: str, value: Any) -> dict:
    from memory import update_field

    return update_field(path, value)


def add_profile_field(path: str, value: Any) -> dict:
    from memory import add_field

    return add_field(path, value)
```

### `tools/workspace_utils.py`

**覆盖。** 去掉 from __future__ import annotations。

<!-- PACKFILE: tools/workspace_utils.py -->
```python
"""共享的 workspace 文件扫描工具。"""

from pathlib import Path
from typing import Iterator


def is_excluded_path(rel_path: Path) -> bool:
    """检查某个相对路径是否应从 workspace 扫描中排除。

    排除 parts 中含隐藏段（以 '.' 开头）或 '__pycache__' 的任何路径。
    """
    return any(part.startswith(".") or part == "__pycache__" for part in rel_path.parts)


def iter_workspace_files(
    root: Path,
    extensions: set[str] | None = None,
    max_files: int = 0,
) -> Iterator[Path]:
    """产出 workspace 文件，排除隐藏目录与 __pycache__。

    参数：
        root：要扫描的 workspace 根目录。
        extensions：若设置，只产出带这些后缀的文件（如 {'.py', '.md'}）。
                    后缀比较不区分大小写。
        max_files：产出这么多文件后停止。0 表示不限。
    """
    count = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_excluded_path(rel):
            continue
        if extensions is not None and p.suffix.lower() not in extensions:
            continue
        yield p
        count += 1
        if max_files > 0 and count >= max_files:
            return
```

### `skills/repository.py`

**覆盖。** skill 仓库扫描。去掉 future annotations。

<!-- PACKFILE: skills/repository.py -->
```python
"""Skills 读写仓储：统一 frontmatter 解析、列表/body 读取与编辑落盘。

布局（progressive disclosure 三层）：
skills/<name>/SKILL.md      -- frontmatter 进目录；body 由 Pro 经 load_skill 拉取
skills/<name>/references/   -- 详细材料，绑定后经 load_skill_reference 按需读取
skills/<name>/scripts/      -- 可执行脚本，绑定后经 use_skill_script 复制进 workspace

唯一写入入口：apply_skill_operations（/edit_skill 编辑判官的落盘层）；
落盘前整批校验，一条非法整批拒绝。
"""

from pathlib import Path
from typing import Any

import yaml

from config.runtime import get_settings


def _parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """拆分 frontmatter / body。返回 (meta, body)。"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = yaml.safe_load(text[4:end]) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = text[end + 5 :]
    return meta, body


def _skill_file(skill_name: str) -> Path:
    return get_settings().skills_dir / skill_name / "SKILL.md"


def _references_dir(skill_name: str) -> Path:
    return get_settings().skills_dir / skill_name / "references"


def _scripts_dir(skill_name: str) -> Path:
    return get_settings().skills_dir / skill_name / "scripts"


def load_skill_document(skill_name: str) -> dict[str, str]:
    path = _skill_file(skill_name)
    if not path.exists():
        raise FileNotFoundError(f"skill not found: {skill_name} (path {path})")
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_skill_md(text)
    return {
        "name": str(meta.get("name", skill_name)),
        "description": str(meta.get("description", "")),
        "when_to_use": str(meta.get("when_to_use", "")),
        "body": body.strip(),
    }


def list_skill_documents() -> list[dict[str, str]]:
    skills_dir = get_settings().skills_dir
    if not skills_dir.exists():
        return []
    out: list[dict[str, str]] = []
    for p in sorted(skills_dir.glob("*/SKILL.md")):
        meta, body = _parse_skill_md(p.read_text(encoding="utf-8"))
        out.append(
            {
                "name": str(meta.get("name", p.parent.name)),
                "description": str(meta.get("description", "")),
                "when_to_use": str(meta.get("when_to_use", "")),
                "body": body.strip(),
                "file_name": str(p.relative_to(skills_dir)),
            }
        )
    return out


def list_skill_references(skill_name: str) -> list[str]:
    """列出 skill 的 references/ 材料文件名（无则空列表）。"""
    ref_dir = _references_dir(skill_name)
    if not ref_dir.is_dir():
        return []
    return sorted(p.name for p in ref_dir.glob("*.md"))


def list_skill_scripts(skill_name: str) -> list[str]:
    """列出 skill 的 scripts/ 可执行脚本文件名（任意后缀；无则空列表）。"""
    sc_dir = _scripts_dir(skill_name)
    if not sc_dir.is_dir():
        return []
    return sorted(p.name for p in sc_dir.glob("*") if p.is_file())


def load_skill_reference(skill_name: str, ref_name: str) -> str:
    """读取 skills/<name>/references/<ref_name> 全文。

    ref_name 取 basename + resolve 边界检查，防止 ../ 越界读 skills 外文件。
    """
    ref_dir = _references_dir(skill_name)
    candidate = (ref_dir / Path(ref_name).name).resolve()
    try:
        candidate.relative_to(ref_dir.resolve())
    except ValueError as e:
        raise PermissionError(f"Illegal reference path: {ref_name!r}") from e
    if not candidate.is_file():
        available = list_skill_references(skill_name)
        raise FileNotFoundError(
            f"reference not found: {skill_name}/references/{ref_name}"
            f" (available: {available or 'none'})"
        )
    return candidate.read_text(encoding="utf-8")


# ─── /edit_skill 编辑读写层 ───────────────────────────────────────


def read_skill_files(skill_name: str) -> dict[str, str]:
    """读取 skill 全部文件，返回 {相对路径: 全文}（供编辑判官组装上下文与计算 diff）。

    覆盖 SKILL.md + references/*.md + scripts/*；skill 不存在抛 FileNotFoundError。
    """
    skill_dir = get_settings().skills_dir / skill_name
    if not _skill_file(skill_name).exists():
        raise FileNotFoundError(f"skill 不存在：{skill_name}（路径 {skill_dir}）")
    files: dict[str, str] = {
        "SKILL.md": _skill_file(skill_name).read_text(encoding="utf-8"),
    }
    for ref in list_skill_references(skill_name):
        files[f"references/{ref}"] = (_references_dir(skill_name) / ref).read_text(encoding="utf-8")
    for sc in list_skill_scripts(skill_name):
        files[f"scripts/{sc}"] = (_scripts_dir(skill_name) / sc).read_text(encoding="utf-8")
    return files


def validate_operation(skill_name: str, op: dict[str, Any]) -> Path:
    """校验单个编辑操作，返回解析后的目标绝对路径；非法抛 PermissionError/ValueError。

    file 仅允许三种形式：SKILL.md / references/<basename>.md / scripts/<basename>。
    白名单锁死在本 skill 目录内；结构上不可能新建 skill 或越界写。
    """
    action = str(op.get("action", ""))
    file = str(op.get("file", ""))
    content = op.get("content", "")
    if action not in {"write", "delete"}:
        raise ValueError(f"非法 action：{action!r}（只允许 write/delete）")

    skill_dir = (get_settings().skills_dir / skill_name).resolve()
    parts = Path(file).parts
    if file == "SKILL.md":
        target = skill_dir / "SKILL.md"
    elif len(parts) == 2 and parts[0] == "references" and parts[1].endswith(".md"):
        target = skill_dir / "references" / Path(parts[1]).name
    elif len(parts) == 2 and parts[0] == "scripts":
        target = skill_dir / "scripts" / Path(parts[1]).name
    else:
        raise PermissionError(
            f"非法 file 路径：{file!r}"
            "（只允许 SKILL.md / references/<name>.md / scripts/<name>）"
        )
    # resolve 后必须仍落在本 skill 目录；越界抛 PermissionError
    try:
        target.resolve().relative_to(skill_dir)
    except ValueError as e:
        raise PermissionError(f"越界 file 路径：{file!r}") from e

    if action == "delete" and file == "SKILL.md":
        raise PermissionError("禁止删除 SKILL.md（skill 主文件必须存在）")
    if action == "write":
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"write 操作 content 为空：{file}")
        if file == "SKILL.md":
            meta, _ = _parse_skill_md(content)
            if not content.startswith("---\n") or not meta.get("name"):
                raise ValueError(
                    "SKILL.md 内容必须以 '---' frontmatter 开头且含 name 字段"
                    "（丢失元数据会破坏 Intake 的 skill 匹配）"
                )
    return target


def apply_skill_operations(skill_name: str, operations: list[dict[str, Any]]) -> list[str]:
    """校验并落盘编辑操作。落盘前整批校验，一条非法整批拒绝（不半落盘）。

    返回：已落盘文件相对路径列表。
    """
    validated: list[tuple[dict[str, Any], Path]] = [
        (op, validate_operation(skill_name, op)) for op in operations
    ]
    applied: list[str] = []
    for op, target in validated:
        if op["action"] == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(op["content"]), encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
        applied.append(str(op["file"]))
    return applied
```

### `skills/editor.py`

**覆盖。** /edit_skill 自然语言改 SOP，diff 确认后落盘。

<!-- PACKFILE: skills/editor.py -->
````python
"""/edit_skill 编辑判官：一次 LLM 调用产出多文件操作提案，确认后才落盘。

数据流：读 skill 全部文件 → 指令里若点到 workspace 文件则读作文风样本
→ oneshot_schema 产出 operations → 预校验 + unified diff → 调用方展示
→ apply_edit 经 apply_skill_operations 整批落盘（一条非法整批拒绝）。

不变量：只能改现有 skill（coding/essay/lab_report），编辑入口不能新建 skill。
入口：CLI /edit_skill 或 Web POST /api/edit_skill。不碰主图 / LabSession 状态。
文风样本上限 _MAX_SAMPLE_CHARS；解析失败进 sample_failures，不进 samples。
"""

import difflib
from typing import Any

from config.prompts import EDIT_SKILL_SYSTEM
from config.runtime import get_settings
from runtime.llm import LLMGateway
from runtime.schema_call import SKILL_EDIT_SCHEMA, SUBMIT_SKILL_EDIT, oneshot_schema
from skills.repository import (
    apply_skill_operations,
    list_skill_documents,
    read_skill_files,
    validate_operation,
)

# 单个文风样本的截断上限（字符数）
_MAX_SAMPLE_CHARS = 8000

# host 侧可直接读的文本后缀；PDF/DOCX/PPTX 走 sandbox_convert_to_markdown
_TEXT_EXTS = {".md", ".txt", ".rst", ".markdown"}
_PARSEABLE_EXTS = {".pdf", ".docx", ".pptx"}


def existing_skill_names() -> list[str]:
    """现有 skill 名列表（编辑目标白名单，当前为 coding/essay/lab_report）。"""
    return sorted(s["name"] for s in list_skill_documents())


async def _collect_style_samples(instruction: str) -> tuple[dict[str, str], list[str]]:
    """从指令文本中匹配 workspace 顶层文件名，读作文风样本。

    指令里出现的顶层文件名即作文风样本（Web 侧经上传接口进 workspace）。
    长文件名优先匹配，命中片段从指令中掩蔽，避免「a.md」作为「data.md」
    子串被误拉入。返回：({文件名: 样本文本}, [解析失败描述])。
    """
    ws = get_settings().workspace_dir
    if not ws.is_dir():
        return {}, []
    candidates = [
        p for p in ws.iterdir() if p.is_file() and not p.name.startswith(".")
    ]
    samples: dict[str, str] = {}
    failures: list[str] = []
    masked = instruction
    for p in sorted(candidates, key=lambda p: len(p.name), reverse=True):
        if p.name not in masked:
            continue
        masked = masked.replace(p.name, "\x00")
        suffix = p.suffix.lower()
        if suffix in _TEXT_EXTS:
            text = p.read_text(encoding="utf-8", errors="replace")
        elif suffix in _PARSEABLE_EXTS:
            try:
                from tools.sandbox_tools import sandbox_convert_to_markdown
                text = await sandbox_convert_to_markdown(str(p))
                if not isinstance(text, str):
                    text = str(text)
            except Exception as e:
                # 解析失败的样本不进 samples，原因进 sample_failures；
                # 占位符既喂不了风格，也会让调用方以为已经学到文风。
                failures.append(f"{p.name}：沙箱解析失败（{type(e).__name__}: {e}）")
                continue
        else:
            continue  # 其他后缀（代码/图片等）不作为文风样本
        samples[p.name] = text[:_MAX_SAMPLE_CHARS]
    return samples, failures


def _build_user_msg(
    skill_name: str,
    files: dict[str, str],
    instruction: str,
    samples: dict[str, str],
) -> str:
    lines = [f"## 待编辑 skill：{skill_name}（以下为全部现有文件）", ""]
    for rel, text in files.items():
        lines.append(f"### 文件：{rel}")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")
    lines.append("## 用户编辑指令")
    lines.append(instruction.strip())
    if samples:
        lines.append("")
        lines.append("## 用户文风样本（提炼风格特征用，禁止原文抄入 skill）")
        for name, text in samples.items():
            lines.append(f"### 样本：{name}（已截断至 {_MAX_SAMPLE_CHARS} 字符）")
            lines.append(text)
            lines.append("")
    return "\n".join(lines)


def _make_diffs(
    files: dict[str, str], operations: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """按操作集逐文件生成 unified diff（新建文件旧文本为空；删除新文本为空）。"""
    diffs: list[dict[str, str]] = []
    for op in operations:
        file = str(op["file"])
        old = files.get(file, "")
        new = str(op.get("content", "")) if op["action"] == "write" else ""
        diff = "\n".join(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{file}", tofile=f"b/{file}", lineterm="",
        ))
        diffs.append({"file": file, "diff": diff or "(无内容变化)"})
    return diffs


async def propose_edit(skill_name: str, instruction: str) -> dict[str, Any]:
    """生成编辑提案（不落盘）。"""
    available = existing_skill_names()
    if skill_name not in available:
        raise FileNotFoundError(
            f"skill 不存在：{skill_name!r}（仅支持编辑现有 skill：{available}，不支持新增）"
        )
    files = read_skill_files(skill_name)
    samples, sample_failures = await _collect_style_samples(instruction)

    settings = get_settings()
    llm = LLMGateway(settings)
    data = await oneshot_schema(
        llm,
        model=settings.pro_model,
        system=EDIT_SKILL_SYSTEM,
        user=_build_user_msg(skill_name, files, instruction, samples),
        name=SUBMIT_SKILL_EDIT,
        schema=SKILL_EDIT_SCHEMA,
        description="Submit skill file operations",
    )

    operations_raw = data.get("operations") or []
    if not isinstance(operations_raw, list):
        raise ValueError(f"operations 必须是数组，实际为 {type(operations_raw).__name__}")
    operations: list[dict[str, Any]] = []
    for op in operations_raw:
        if not isinstance(op, dict):
            raise ValueError(f"非法 operation：{op!r}")
        norm = {
            "action": str(op.get("action", "")),
            "file": str(op.get("file", "")),
            "content": op.get("content", "") or "",
        }
        validate_operation(skill_name, norm)
        operations.append(norm)

    return {
        "skill_name": skill_name,
        "summary": str(data.get("summary", "")).strip(),
        "operations": operations,
        "diffs": _make_diffs(files, operations),
        "style_samples": list(samples),
        "sample_failures": sample_failures,
    }


def apply_edit(skill_name: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """确认后落盘（整批校验整批落盘）。返回：{applied: [...]}"""
    return {"applied": apply_skill_operations(skill_name, operations)}
````

### `skills/coding/SKILL.md`

**覆盖。** 精简给 Pro 的 coding SOP。

<!-- PACKFILE: skills/coding/SKILL.md -->
```markdown
---
name: coding
description: |
  Runnable code plus tests. Artifacts: source files and test_*.py.
when_to_use: |
  The work needs a program (实现 / 写代码 / 编程, algorithms, scripts).
  Not this: experiment + lab report → lab_report; pure argumentation → essay.
---

# Coding SOP

For Pro: plan, dispatch, and judge coding work. Flash never sees this file — put what it needs in the assignment.

## SPEC

- Pin interfaces (function / class / CLI / file names and signatures). Acceptance imports them verbatim.
- Name files by topic or problem id (`binary_search.py`, `hw4_q1.py`). Never `solution.py` / `main.py`.
- Do not invent constraints absent from MATERIALS.md or the user request.

## Dispatch

Slice so one Flash finishes one assignment: read constraints → implement one unit → run tests.
A whole homework in one assignment will be half-done and marked success.

Each assignment must be self-contained: exact names, paths, forbidden libraries, boundary behavior.
If testable, declare `interfaces` and write acceptance via `write_acceptance`.

## Tests

Write a gate only when the target is quantifiable and the artifact can be checked by code.
Otherwise `testable=false` — a missing gate is honest.

Test file shares the topic (`test_zuc.py` with `zuc.py`). Cover normal / boundary / exception.
Details: `load_skill_reference("coding", "testing.md")`.
Stuck on pytest or sandbox: `load_skill_reference("coding", "pitfalls.md")`.

## Style and integrity

- Honor `profile.coding_style` (type hints, docstring).
- Internet is for API docs, not answers. Note any copied snippet URL in SUMMARY.
- No other people's names, student IDs, or GitHub handles in the artifact.

## Judge

Pass means interfaces exist and the gate is green. "Looks done" is not enough.
If the worker renamed an interface, fail or `test_invalid` — do not patch the contract silently.
```

### `skills/coding/references/pitfalls.md`

**覆盖。** 随 SOP 收紧。

<!-- PACKFILE: skills/coding/references/pitfalls.md -->
```markdown
# Exception Handling (coding)

| Failure | Response |
|---|---|
| Constraints not in the assignment | `read_file` the workspace README / guidance; do not guess |
| Sandbox unreachable | Gate maps this to `test_invalid`. Do not pretend the tests passed on the host |
| pytest same error ≥3 times | Stop retuning. Next dispatch should shrink the assignment or rewrite tests (`test_invalid` if the gate itself is wrong) |
| Conflicting constraints | Do not force both. Note the conflict in SPEC / SUMMARY and pick the problem statement over the skill |
| `host_bash` PermissionError | Stay inside workspace relative paths, or run in `sandbox_execute_bash` |
| Assignment too large | Split. One Flash, one unit of work |
```

### `skills/coding/references/testing.md`

**覆盖。** 随 SOP 收紧。

<!-- PACKFILE: skills/coding/references/testing.md -->
````markdown
# Test Case Design (coding)

Cover at least one of each:

1. **Normal**: typical input; target in the middle / head / tail
2. **Boundary**: empty, single element, duplicates, extreme values, target absent
3. **Exception**: wrong type / unsorted — follow the problem statement (return sentinel or raise). If silent, pick a sentinel and document it

## Organization

- One behavior per test: `test_<behavior>`
- Assertion message: `assert res == 2, f"expect 2, got {res}"`
- Batch boundaries with `@pytest.mark.parametrize`

```python
import pytest
from binary_search import binary_search

@pytest.mark.parametrize("arr,target,expect", [
    ([1, 2, 3, 4, 5], 3, 2),
    ([1, 2, 3], 1, 0),
    ([1, 2, 3], 3, 2),
    ([], 1, -1),
    ([1, 2, 3], 9, -1),
])
def test_search(arr, target, expect):
    assert binary_search(arr, target) == expect
```

Run with `sandbox_execute_bash`: `pytest test_<name>.py -v`.
Write the gate through `write_acceptance` against SPEC interfaces. Partial pass is fail, not done.
````

### `skills/essay/SKILL.md`

**覆盖。** 精简 essay SOP。

<!-- PACKFILE: skills/essay/SKILL.md -->
```markdown
---
name: essay
description: |
  Written argumentation only: essay, reading notes, term paper, review.
  No runnable code, no experiment process. Artifacts: .md / .docx / .pdf.
when_to_use: |
  The work is 写一篇 / 论述 / 不少于 N 字 / 谈谈看法, deliverable is a document.
  Not this: code + tests → coding; experiment process + report → lab_report.
---

# Essay SOP

For Pro: plan, dispatch, and judge written work. Flash never sees this file — put what it needs in the assignment.

## SPEC

Extract word-count range, required sections, stance (neutral vs take a side), citation style
(APA / GB/T 7714 / none), and theme keywords. Do not invent sources or data.

## Dispatch

Outline before prose. Typical sequence (one Flash each, not parallel writers):
1. outline (intro / arguments / optional rebuttal / conclusion / references)
2. draft the body
3. citations + self-check

Do not split one essay across parallel workers. The assignment's `spec` must include
word-count, stance, section list, and citation rules — Flash cannot see SPEC.md's rationale.

## Writing

Honor `profile.preferences.writing_style` (formality, sentence length).
No filler to hit the word count: one idea per paragraph.
Citation details: `load_skill_reference("essay", "citation.md")`.
If there are no citable sources, say so in SUMMARY; do not fabricate.

End the artifact with a short "tool usage note" (AI assistance).

## Judge

Check: word count in range, required sections present, each argument has a traceable source,
stance matches SPEC, citation format holds, no fabricated references.
Missing evidence is fail, not "close enough".
```

### `skills/essay/references/citation.md`

**覆盖。** 随 SOP 收紧。

<!-- PACKFILE: skills/essay/references/citation.md -->
```markdown
# Citation (essay)

## Direct

`"original text" — author(year)《title》` or `[1] 作者. 标题. 期刊, 年份.`
One quote ≤ 30 characters; longer → paraphrase. Numbers `[1][2]` must match the reference list.

## Indirect

Mark `(author, year)` at sentence end. Own wording, not synonym swap.

## Formats

- GB/T 7714 (default domestic): `[1] 作者. 标题[J]. 期刊名, 年份, 卷(期): 页码.`
- APA: `Author, A. (Year). Title. Journal, Vol(Issue), pages.`
- Web: title + URL + access date
- PDF materials: convert with `sandbox_convert_to_markdown`, cite page (`p.42`)

## Forbidden

Fabricated references / DOIs. Wikipedia is background only, not an academic source.
```

### `skills/lab_report/SKILL.md`

**覆盖。** 精简 lab_report SOP。

<!-- PACKFILE: skills/lab_report/SKILL.md -->
````markdown
---
name: lab_report
description: |
  Lab report with experiment process: objective / principle / steps / results / analysis / conclusion.
  Artifacts: report document plus optional code or data.
when_to_use: |
  写实验报告 / Lab N / 实验指导, or a lab that must submit process + evidence.
  Not this: implement + tests only → coding; argumentation with no experiment → essay.
---

# Lab Report SOP

For Pro: plan, dispatch, and judge lab-report work. Flash never sees this file — put what it needs in the assignment.
**Never fabricate** runs, screenshots, or observations that did not happen.

## SPEC

Extract: one-sentence objective, environment (OS / container / topology / tool versions),
Task list, bonus, submission rules, rubric. Main deliverable is the report, not the helper code.

Required sections (all must appear):

```
1. 实验目的     5. 实验结果（数据表 + 原始记录）
2. 实验原理     6. 结果分析与讨论（与预期对比 + 意外现象）
3. 实验环境     7. 结论
4. 实验步骤     8. 思考题（指导书有则写）
```

## Dispatch

Typical sequence, one Flash at a time:
1. reproduce / collect real output (commands, numbers, errors)
2. write process sections (thinking → command → phenomenon per Task)
3. results table + analysis + conclusion

Writing style and table templates: `load_skill_reference("lab_report", "writing_guide.md")`.

Agent cannot capture screenshots — use `（此处建议附 XX 截图）` placeholders for the user.
Paste code inline at the step it belongs to, with one sentence of purpose before the block.
If a run did not succeed: write 实测未跑通（现象 + 已试方法 + 卡点）, do not invent a passing table.

## Integrity

Lab guidance may be quoted as the object of analysis. Other sources: quote + citation, ≤30 chars.
Do not expose others' student IDs, real IPs, or credentials.
End with a "tool usage note" (which sections AI drafted; screenshots pending).

## Judge

Fail if any of the five keywords is missing (实验目的 / 实验原理 / 实验步骤 / 实验结果 / 结论),
if a Task has no process→phenomenon→conclusion chain, or if results look invented.
Unexpected measurements belong in 分析, not a silent rewrite of the data.
````

### `mcp_client/client.py`

**覆盖。** 去掉 future annotations。

<!-- PACKFILE: mcp_client/client.py -->
```python
"""AIO Sandbox 的 MCP 客户端（官方 SDK streamable HTTP）。

模块级单例 session + tools 列表缓存。连上前 probe_port；探活失败抛 RuntimeError。
call_mcp_tool 失败则 reset 后重连再调一次。reset_mcp_client 清空 session/缓存，
并在已有 event loop 里异步关闭旧连接；无 running loop 时只丢引用。
"""

from typing import Any

from config.runtime import get_settings
from infra.net_probe import probe_port

_session: Any = None
_cm: Any = None
_tools_cache: list[Any] | None = None


async def _open_session() -> Any:
    global _session, _cm
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mcp_url = get_settings().aio_sandbox_mcp_url
    if not probe_port(mcp_url):
        raise RuntimeError(
            f"MCP 探活失败：{mcp_url}。请确认 AIO Sandbox 容器在跑且 health=healthy。"
        )
    _cm = streamablehttp_client(
        mcp_url,
        headers={"Accept": "application/json, text/event-stream"},
    )
    read, write, _sid = await _cm.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    _session = session
    return session


async def get_session() -> Any:
    global _session
    if _session is None:
        return await _open_session()
    return _session


async def list_mcp_tools() -> list[Any]:
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache
    session = await get_session()
    listed = await session.list_tools()
    _tools_cache = list(listed.tools)
    return _tools_cache


async def call_mcp_tool(name: str, arguments: dict[str, Any]) -> Any:
    session = await get_session()
    try:
        return await session.call_tool(name, arguments)
    except Exception:
        reset_mcp_client()
        session = await get_session()
        return await session.call_tool(name, arguments)


def reset_mcp_client() -> None:
    global _session, _cm, _tools_cache
    _tools_cache = None
    sess, cm = _session, _cm
    _session = None
    _cm = None

    async def _close() -> None:
        if sess is not None:
            try:
                await sess.__aexit__(None, None, None)
            except Exception:
                pass
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    try:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(_close())
    except RuntimeError:
        pass
```

### `infra/net_probe.py`

**覆盖。** 去掉 future annotations。

<!-- PACKFILE: infra/net_probe.py -->
```python
"""net_probe - 轻量 TCP 端口可达性探测（sandbox_boot 与 mcp_client 共用）。

单一职责：解析 URL 的 host/port，用 socket.create_connection 检查
端口是否可达。不依赖 docker/subprocess 等重依赖，可安全跨模块复用。
"""

import socket
from urllib.parse import urlparse


def probe_port(url: str, timeout: float = 1.0) -> bool:
    """检查目标 URL 的端口是否可经 TCP 连接到达。"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
```

### `infra/sandbox_boot.py`

**覆盖。** 去掉 future annotations。

<!-- PACKFILE: infra/sandbox_boot.py -->
```python
"""sandbox_boot - 启动时自动拉起 AIO Sandbox 容器。

设计要点：
1. 端口探活成功 -> 直接返回（容器已在跑）。
2. 探活失败 -> `docker inspect`：容器存在但已停 -> `docker start`；
   不存在 -> `docker run`。
3. 轮询端口最多 60s。
4. 未装 docker / docker 失败 -> 打印友好错误，不抛异常
   （后续由 mcp_client 给出一致报错）。
5. 可关闭：LAB_AUTOSTART_SANDBOX=false 跳过整个流程（保留手动控制）。
6. 容器参数从 .env 读取：AIO_SANDBOX_IMAGE / AIO_SANDBOX_PORT / AIO_SANDBOX_MCP_URL。
7. **workspace 绑定挂载**：host WORKSPACE_DIR -> 容器 /workspace（使
   sandbox_convert_to_markdown 等能直接读 PDF/DOCX）；无此挂载的旧容器
   会收到一次性迁移提示。
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from config.runtime import get_settings
from infra.net_probe import probe_port

CONTAINER_NAME = "aio-sandbox"
SANDBOX_WORKSPACE_MOUNT = "/workspace"  # 容器侧统一工作区目录


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _container_state() -> str | None:
    """返回容器状态（running / exited / ...）；不存在返回 None。"""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def _image_exists_locally(image: str) -> bool:
    """检查镜像是否已在本地；用于区分“首次拉镜像”与“仅创建容器”的日志文案。"""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _container_has_workspace_mount(host_workspace: Path) -> bool:
    """检查现有容器是否已将 host WORKSPACE_DIR 挂载到 SANDBOX_WORKSPACE_MOUNT。

    存在但缺挂载 -> 返回 False（由调用方决定是否打迁移提示）。
    容器不存在 / docker 出错 -> 返回 True（不影响后续逻辑）。
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Mounts}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return True  # 容器不存在或 inspect 失败，不打扰
        mounts = json.loads((r.stdout or "[]").strip() or "[]")
        host_resolved = str(host_workspace)
        for m in mounts:
            src = str(m.get("Source", ""))
            dst = str(m.get("Destination", ""))
            # Source 与 host_workspace 须匹配（容忍尾部斜杠 / 软链解析差异）
            if dst == SANDBOX_WORKSPACE_MOUNT and (
                src == host_resolved or Path(src).resolve() == host_workspace
            ):
                return True
        return False
    except Exception:
        return True


def _docker_start(log=print) -> bool:
    try:
        r = subprocess.run(
            ["docker", "start", CONTAINER_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log(f"[sandbox] docker start 失败，stderr：{(r.stderr or '').strip()}")
        return r.returncode == 0
    except Exception as e:
        log(f"[sandbox] docker start 异常：{type(e).__name__}: {e}")
        return False


def _docker_run(image: str, port: int, host_workspace: Path, log=print) -> bool:
    """首次创建容器；本地无镜像时自动拉取（可能耗时几分钟）。

    将 host workspace 挂载到 /workspace（使 sandbox 工具能读 PDF/DOCX）。
    失败时打印 docker stderr 供排查（常见：WSL2 docker-credential-desktop.exe /
    网络拉镜像失败 / 端口冲突）。
    """
    try:
        r = subprocess.run(
            [
                "docker", "run", "-d", "--name", CONTAINER_NAME,
                "--security-opt", "seccomp=unconfined", "--shm-size", "2g",
                "-p", f"{port}:8080",
                "-v", f"{host_workspace}:{SANDBOX_WORKSPACE_MOUNT}",
                "-e", "DISABLE_JUPYTER=true", "-e", "DISABLE_CODE_SERVER=true",
                image,
            ],
            capture_output=True, text=True, timeout=600,  # 预留 10 分钟拉镜像
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            log(f"[sandbox] docker run 失败（exit={r.returncode}）：\n  {stderr}")
            # 友好提示：检测 WSL2 凭据助手问题
            if "docker-credential-desktop.exe" in stderr or "exec format error" in stderr:
                log(
                    "[sandbox] 检测到 WSL2 + Docker Desktop 凭据助手错误。\n"
                    "  解决：\n"
                    "    cp ~/.docker/config.json ~/.docker/config.json.bak\n"
                    "    echo '{}' > ~/.docker/config.json\n"
                    "  原因：~/.docker/config.json 指向 Windows .exe 凭据助手，\n"
                    "       从 WSL2 Linux 侧 exec 失败。该镜像是公共仓库，无需登录。"
                )
        return r.returncode == 0
    except Exception as e:
        log(f"[sandbox] docker run 异常：{type(e).__name__}: {e}")
        return False


def ensure_sandbox(log=print) -> bool:
    """按需检测并启动沙箱容器；返回端口最终是否可达。"""
    if os.getenv("LAB_AUTOSTART_SANDBOX", "true").lower() in {"false", "0", "no"}:
        return True  # 用户禁用自启；交由 mcp_client 探活时报错

    url = os.getenv("AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp")
    image = os.getenv(
        "AIO_SANDBOX_IMAGE",
        "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest",
    )
    port = int(os.getenv("AIO_SANDBOX_PORT", "8080"))
    host_workspace = get_settings().workspace_dir
    host_workspace.mkdir(parents=True, exist_ok=True)

    # 一次性迁移提示：无 workspace 挂载的旧容器意味着 agent 永远无法在沙箱读 PDF
    if _docker_available() and not _container_has_workspace_mount(host_workspace):
        log(
            "[sandbox] ⚠️ 检测到旧容器没有 workspace bind-mount。"
            f"sandbox 工具将无法读 {host_workspace} 下的 PDF/DOCX。\n"
            "  请运行：  docker rm -f aio-sandbox\n"
            "  然后重启 python -m server（会自动用新挂载重建容器）。"
        )
        # 继续：旧容器仍可跑，只是读不了文件；由用户决定是否重建。

    if probe_port(url):
        return True

    if not _docker_available():
        log("[sandbox] 未检测到 docker；请先装 docker 或手动起容器。")
        return False

    state = _container_state()
    if state == "running":
        # 容器 running 但端口未通；等待（健康检查可能尚未通过）
        log("[sandbox] 容器 running 但端口未通，等待健康检查...")
    elif state in {"exited", "created", "paused", "dead"}:
        log(f"[sandbox] 容器存在（{state}），尝试 docker start...")
        if not _docker_start(log=log):
            log("[sandbox] docker start 失败；请检查 `docker logs aio-sandbox`。")
            return False
    else:
        if _image_exists_locally(image):
            log("[sandbox] 容器不存在，docker run 创建（本地已有镜像，几秒就绪）...")
        else:
            log(f"[sandbox] 容器不存在，docker run 创建（首次拉镜像约 2.29GB，可能几分钟）...")
        if not _docker_run(image, port, host_workspace, log=log):
            log("[sandbox] docker run 失败；可手动跑：docker run -d --name aio-sandbox ...")
            return False

    # 轮询端口最多 60s
    for _ in range(60):
        if probe_port(url):
            log(f"[sandbox] 就绪（{url}）")
            return True
        time.sleep(1)

    log(f"[sandbox] 等待 60s 仍未就绪（{url}）；可 `docker logs aio-sandbox` 排查。")
    return False


def _docker_rm(log=print) -> bool:
    """docker rm -f aio-sandbox；容器不存在也视为成功。"""
    try:
        r = subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True
        # 容器已不存在 -> 视为成功
        if "No such container" in (r.stderr or ""):
            return True
        log(f"[sandbox] docker rm 失败，stderr：{(r.stderr or '').strip()}")
        return False
    except Exception as e:
        log(f"[sandbox] docker rm 异常：{type(e).__name__}: {e}")
        return False


def recreate_sandbox(log=print) -> bool:
    """删除现有容器 + 重置 MCP/sandbox_tools 缓存 + 重启。

    目的：`/done --clear` 不仅清 host workspace，还清容器内的
    pip 全局包 / /tmp / 长驻进程残留，使下一个任务从干净容器起步。
    """
    if os.getenv("LAB_AUTOSTART_SANDBOX", "true").lower() in {"false", "0", "no"}:
        log("[sandbox] LAB_AUTOSTART_SANDBOX=false，跳过重建（请手动 docker rm 后重启 cli）")
        return True
    if not _docker_available():
        log("[sandbox] 未检测到 docker，跳过重建")
        return False

    if _container_state() is not None:
        if _docker_rm(log=log):
            log(f"[sandbox] {CONTAINER_NAME} 已删除")
        else:
            log(f"[sandbox] docker rm {CONTAINER_NAME} 失败；继续尝试重建")

    # 重置 MCP / sandbox 缓存（容器已变）
    try:
        from mcp_client import reset_mcp_client
        reset_mcp_client()
    except Exception as e:
        log(f"[sandbox] reset_mcp_client 失败（继续）：{type(e).__name__}: {e}")
    try:
        from tools.sandbox_tools import reset_sandbox_failure_counter
        reset_sandbox_failure_counter()
    except Exception as e:
        log(f"[sandbox] reset sandbox failures 失败（继续）：{type(e).__name__}: {e}")

    # 重启（等待端口就绪最多 60s）
    return ensure_sandbox(log=log)
```

### `server/app.py`

**覆盖。** 去掉重复 load_dotenv（改由 config.runtime 加载）。shutdown 调 llm.aclose。

<!-- PACKFILE: server/app.py -->
```python
"""labHandler Web 服务器（FastAPI + SSE）。入口：python -m server。"""

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config.runtime import get_settings
from runtime.persist import latest_incomplete
from runtime.session import LabSession
from tools.workspace_utils import iter_workspace_files

WORKSPACE_DIR: Path = get_settings().workspace_dir
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="labHandler", docs_url=None, redoc_url=None)


@app.on_event("shutdown")
async def _shutdown_llm() -> None:
    close = getattr(_session.llm, "aclose", None)
    if close is not None:
        await close()


@app.middleware("http")
async def _no_cache_static(request, call_next):
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html") or request.url.path.endswith(
        (".html", ".js", ".css")
    ):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


_session = LabSession()
_task_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None
_event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()


def _is_running() -> bool:
    return _current_task is not None and not _current_task.done()


@app.on_event("shutdown")
async def _shutdown() -> None:
    """进程退出前把 span 刷出去，否则最后一段 trace 会丢在缓冲里。"""
    _session.request_stop()
    _session.tracer.shutdown()
    await _session.llm.aclose()


def _safe_workspace_path(name: str) -> Path:
    rel = Path(name)
    if not name or rel.is_absolute() or any(
        not part or part.startswith(".") for part in rel.parts
    ):
        raise HTTPException(status_code=400, detail=f"非法文件名：{name!r}")
    resolved = (WORKSPACE_DIR / rel).resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"越界路径：{name!r}")
    return resolved


def _list_workspace_files() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not WORKSPACE_DIR.exists():
        return out
    for p in iter_workspace_files(WORKSPACE_DIR):
        out.append({"name": str(p.relative_to(WORKSPACE_DIR)), "size": p.stat().st_size})
    return sorted(out, key=lambda x: x["name"])


@app.get("/api/files")
async def list_files() -> list[dict[str, Any]]:
    return _list_workspace_files()


@app.post("/api/files")
async def upload_files(files: list[UploadFile]) -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改 workspace")
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        target = _safe_workspace_path(f.filename or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await f.read())
        saved.append(target.name)
    return {"saved": saved}


@app.get("/api/files/{name:path}")
async def download_file(name: str) -> FileResponse:
    resolved = _safe_workspace_path(name)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(resolved, filename=resolved.name)


@app.delete("/api/files/{name:path}")
async def delete_file(name: str) -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，暂不能修改 workspace")
    resolved = _safe_workspace_path(name)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    bucket = WORKSPACE_DIR.parent / ".trash" / time.strftime("%Y%m%d_%H%M%S")
    bucket.mkdir(parents=True, exist_ok=True)
    shutil.move(str(resolved), str(bucket / resolved.name))
    return {"deleted": resolved.name, "trashed_to": str(bucket)}


class TaskRequest(BaseModel):
    question: str


async def _run_lab(question: str) -> None:
    """把一次 LabSession.run 写成 final/error 事件并关闭 SSE。

    续跑 vs 新开由 session 是否已 attach tree 决定。
    """

    async def sink(ev: dict[str, Any]) -> None:
        await _event_queue.put(ev)

    started = time.time()
    try:
        result = await _session.run(question, on_event=sink)
        await _event_queue.put(
            {
                "kind": "final",
                "verdict": result.get("verdict", "unknown"),
                "elapsed": round(time.time() - started, 1),
                "artifacts": await asyncio.to_thread(_list_workspace_files),
            }
        )
    except Exception as e:
        await _event_queue.put({"kind": "error", "detail": f"{type(e).__name__}: {e}"})
    finally:
        _session.tracer.flush()
        await _event_queue.put(None)


@app.post("/api/task")
async def submit_task(req: TaskRequest) -> dict[str, Any]:
    global _current_task
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 为空")
    async with _task_lock:
        if _is_running():
            raise HTTPException(status_code=409, detail="已有任务在运行（单任务约束）")
        while not _event_queue.empty():
            _event_queue.get_nowait()
        _current_task = asyncio.create_task(_run_lab(question))
    return {"accepted": True, "question": question, "thread_id": _session.thread_id}


class ResumeRequest(BaseModel):
    continue_lab: bool
    question: str = "继续上次未完成的 lab"


@app.get("/api/resume")
async def resume_info() -> dict[str, Any]:
    peek = _session.peek_resume()
    return {"resumable": peek}


@app.post("/api/resume")
async def resume_lab(req: ResumeRequest) -> dict[str, Any]:
    global _current_task
    async with _task_lock:
        if _is_running():
            raise HTTPException(status_code=409, detail="已有任务在运行")
        peek = await asyncio.to_thread(latest_incomplete, WORKSPACE_DIR)
        if not peek:
            raise HTTPException(status_code=404, detail="没有未完成的 lab")
        tid, tree = peek
        if not req.continue_lab:
            return await asyncio.to_thread(_session.decline_resume)
        _session.attach_resume(tid, tree)
        while not _event_queue.empty():
            _event_queue.get_nowait()
        _current_task = asyncio.create_task(_run_lab(req.question))
    return {"accepted": True, "thread_id": tid, "resumed": True}


@app.get("/api/task/stream")
async def task_stream() -> EventSourceResponse:
    async def _gen():
        while True:
            try:
                item = await asyncio.wait_for(_event_queue.get(), timeout=300)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
                continue
            if item is None:
                break
            yield {
                "event": item.get("kind", "message"),
                "data": json.dumps(item, ensure_ascii=False, default=str),
            }

    return EventSourceResponse(_gen())


@app.post("/api/stop")
async def stop_task() -> dict[str, Any]:
    if not _is_running():
        return {"stopped": False}
    _session.request_stop()
    return {"stopped": True}


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    peek = _session.peek_resume()
    return {
        "running": _is_running(),
        "thread_id": _session.thread_id,
        "resumable": peek,
        "verdict": (_session.last_result or {}).get("verdict"),
    }


@app.get("/api/summary")
async def get_summary() -> JSONResponse:
    sp = WORKSPACE_DIR / "SUMMARY.md"
    text = sp.read_text(encoding="utf-8") if sp.exists() else ""
    return JSONResponse({"summary": text})


@app.post("/api/dream")
async def dream() -> dict[str, Any]:
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，先等它结束")
    from memory.dream import run_dream

    try:
        return await run_dream(_session.llm)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


class RememberRequest(BaseModel):
    rule: str


@app.get("/api/profile")
async def get_profile_api() -> dict[str, Any]:
    from memory.profile import load_profile

    return load_profile() or {}


@app.post("/api/remember")
async def remember(req: RememberRequest) -> dict[str, Any]:
    from memory.profile import append_rule

    try:
        prof = append_rule(req.rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rules = (prof.get("preferences") or {}).get("style_rules") or []
    return {"style_rules": rules}


_PENDING_EDIT: dict[str, Any] | None = None
_EDIT_PENDING_TTL = 600


class EditSkillRequest(BaseModel):
    skill_name: str
    instruction: str


class EditApplyRequest(BaseModel):
    edit_id: str
    confirm: bool = True


@app.get("/api/skills")
async def list_skills_api() -> list[dict[str, str]]:
    from tools.skill_tool import list_skill_meta

    return list_skill_meta()


@app.post("/api/edit_skill")
async def edit_skill(req: EditSkillRequest) -> dict[str, Any]:
    global _PENDING_EDIT
    if _is_running():
        raise HTTPException(status_code=409, detail="任务运行中，先等它结束")
    from skills.editor import existing_skill_names, propose_edit

    available = existing_skill_names()
    skill_name = (req.skill_name or "").strip()
    instruction = (req.instruction or "").strip()
    if skill_name not in available:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在：{skill_name!r}（仅支持编辑现有 skill：{available}，不支持新增）",
        )
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction 为空")
    try:
        proposal = await propose_edit(skill_name, instruction)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    edit_id = f"edit_{int(time.time() * 1000)}"
    _PENDING_EDIT = {"edit_id": edit_id, "created_at": time.time(), **proposal}
    return {
        "edit_id": edit_id,
        "skill_name": skill_name,
        "summary": proposal["summary"],
        "diffs": proposal["diffs"],
        "style_samples": proposal["style_samples"],
        "sample_failures": proposal["sample_failures"],
        "n_operations": len(proposal["operations"]),
    }


@app.post("/api/edit_skill/apply")
async def edit_skill_apply(req: EditApplyRequest) -> dict[str, Any]:
    global _PENDING_EDIT
    if _PENDING_EDIT is None:
        raise HTTPException(status_code=404, detail="没有待确认的编辑提案")
    if req.edit_id != _PENDING_EDIT["edit_id"]:
        raise HTTPException(status_code=409, detail="edit_id 不匹配（提案已被新提案覆盖）")
    if time.time() - _PENDING_EDIT["created_at"] > _EDIT_PENDING_TTL:
        _PENDING_EDIT = None
        raise HTTPException(status_code=409, detail="提案已超时（10 分钟），请重新发起")

    pending = _PENDING_EDIT
    _PENDING_EDIT = None
    if not req.confirm:
        return {"status": "cancelled"}
    from skills.editor import apply_edit

    try:
        report = await asyncio.to_thread(
            apply_edit, pending["skill_name"], pending["operations"]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return {"status": "applied", **report}


@app.post("/api/done")
async def done() -> dict[str, Any]:
    if _is_running():
        _session.request_stop()
        if _current_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(_current_task), timeout=15)
            except Exception:
                pass
    from memory.retrieve import index_card_ids

    cards = (_session.last_result or {}).get("knowledge_cards") or []
    # 卡片先入档并向量索引，再 _session.done 复位；反序会丢掉未索引卡片。
    archive_extra: dict[str, Any] = {}
    if cards:
        from memory.archive import get_task_archive

        title = (_session.last_result or {}).get("question") or "未命名任务"
        summary = (_session.last_result or {}).get("summary") or ""
        archive = get_task_archive()
        task_id = archive.create_task(title, "other", summary[:4000])
        card_ids = archive.create_cards(task_id, cards, title, "other")
        if card_ids:
            archive_extra = await index_card_ids(card_ids, _session.llm, _session.settings)
            archive_extra["task_id"] = task_id
            archive_extra["card_ids"] = card_ids
            _session.last_result["knowledge_cards"] = []  # 已入档，清空以免 _session.done 二次归档
    result = await asyncio.to_thread(_session.done, lambda m: None)
    if archive_extra:
        result["archive"] = {**(result.get("archive") or {}), **archive_extra}
    return result


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
```

### `server/__main__.py`

**覆盖。** 去掉 future annotations。

<!-- PACKFILE: server/__main__.py -->
```python
"""python -m server 启动入口（uvicorn，默认仅本机 127.0.0.1:8000）。"""

import os

import uvicorn


def main() -> None:
    host = os.getenv("LAB_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("LAB_WEB_PORT", "8000"))
    # 启动时自检沙箱（与 CLI 一致；LAB_AUTOSTART_SANDBOX=false 可禁）
    try:
        from infra.sandbox_boot import ensure_sandbox
        ensure_sandbox(log=print)
    except Exception as e:
        print(f"[server] sandbox 自动启动检查失败（已跳过）：{e}")
    uvicorn.run("server.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
```

### `server/static/index.html`

**覆盖。** Web 操作面小改（知识治理等）。

<!-- PACKFILE: server/static/index.html -->
```html
<!DOCTYPE html>
<html lang="zh-CN">

<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>labHandler</title>
  <style>
    :root {
      --bg: #0f1115;
      --panel: #171a21;
      --border: #2a2f3a;
      --text: #d7dae0;
      --dim: #8b93a1;
      --accent: #4da3ff;
      --green: #4caf7d;
      --yellow: #d9a33c;
      --red: #e05c5c;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
      max-width: 960px;
      margin: 0 auto;
      padding: 24px 16px 60px;
    }

    /* ─── 头部 + tab ─────────────────────────────────── */

    header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }

    h1 {
      font-size: 20px;
    }

    h1 small {
      color: var(--dim);
      font-weight: normal;
      font-size: 12px;
      margin-left: 8px;
    }

    nav {
      display: flex;
      gap: 4px;
    }

    nav button {
      background: transparent;
      border: 0;
      border-radius: 6px;
      color: var(--dim);
      font-size: 13px;
      padding: 6px 14px;
      cursor: pointer;
    }

    nav button.active {
      background: var(--panel);
      border: 1px solid var(--border);
      color: var(--text);
    }

    nav button:disabled {
      opacity: .45;
      cursor: not-allowed;
    }

    /* ─── 卡片 ───────────────────────────────────────── */

    section {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      margin-top: 16px;
    }

    section h2 {
      font-size: 13px;
      color: var(--dim);
      font-weight: normal;
      margin-bottom: 10px;
    }

    .hidden {
      display: none;
    }

    /* ─── 上传区 ─────────────────────────────────────── */

    #drop {
      border: 2px dashed var(--border);
      border-radius: 8px;
      padding: 22px;
      text-align: center;
      color: var(--dim);
      cursor: pointer;
      transition: border-color .15s;
    }

    #drop.hover {
      border-color: var(--accent);
      color: var(--accent);
    }

    #drop.disabled {
      opacity: .5;
      cursor: not-allowed;
    }

    ul.files {
      margin-top: 10px;
      font-size: 13px;
    }

    ul.files li {
      list-style: none;
      padding: 4px 0;
      display: flex;
      justify-content: space-between;
      border-bottom: 1px dashed var(--border);
    }

    ul.files a {
      color: var(--accent);
      text-decoration: none;
    }

    .del-btn {
      background: transparent;
      border: 0;
      color: var(--dim);
      font-size: 13px;
      padding: 0 2px;
      margin-left: 10px;
      cursor: pointer;
    }

    .del-btn:hover {
      color: var(--red);
    }

    /* ─── 输入行 / 按钮 ──────────────────────────────── */

    .row {
      display: flex;
      gap: 8px;
      align-items: center;
    }

    input[type=text] {
      flex: 1;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 14px;
    }

    button {
      background: var(--accent);
      color: #fff;
      border: 0;
      border-radius: 6px;
      padding: 10px 18px;
      font-size: 14px;
      cursor: pointer;
    }

    button:disabled {
      background: var(--border);
      color: var(--dim);
      cursor: not-allowed;
    }

    button.ghost {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--dim);
    }

    #status {
      font-size: 12px;
      color: var(--dim);
    }

    /* ─── 转录区：节点 / 工具行 / dim 结果 ───────────── */

    #log {
      font-family: "SF Mono", Consolas, monospace;
      font-size: 12.5px;
      max-height: 420px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
      line-height: 1.6;
    }

    #log .node {
      color: var(--text);
      font-weight: bold;
      margin-top: 8px;
      display: block;
    }

    #log .tool {
      color: var(--text);
      display: block;
      padding-left: 14px;
    }

    #log .tool b {
      font-weight: bold;
    }

    #log .tool i {
      color: var(--dim);
      font-style: normal;
    }

    #log .toolret {
      color: var(--dim);
      display: block;
      padding-left: 28px;
    }

    #log .done {
      color: var(--green);
      display: block;
      padding-left: 14px;
      opacity: .8;
    }

    #log .err {
      color: var(--red);
      display: block;
    }

    #log .stream {
      color: var(--text);
      padding-left: 14px;
    }

    #log .reason {
      color: var(--dim);
      font-style: italic;
      padding-left: 14px;
    }

    /* ─── 结果区 ─────────────────────────────────────── */

    #verdict {
      font-size: 15px;
      font-weight: bold;
      margin-bottom: 8px;
    }

    #verdict.pass {
      color: var(--green);
    }

    #verdict.fail {
      color: var(--yellow);
    }

    #summary {
      white-space: pre-wrap;
      font-size: 13px;
      line-height: 1.6;
      max-height: 360px;
      overflow-y: auto;
      margin-top: 10px;
    }

    /* ─── Skill 编辑 ─────────────────────────────────── */

    select,
    textarea {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 14px;
    }

    textarea {
      width: 100%;
      margin-top: 8px;
      resize: vertical;
      min-height: 60px;
      font-family: inherit;
    }

    #skillNotice {
      color: var(--yellow);
      font-size: 13px;
      margin-top: 16px;
    }

    #skillDiffs {
      font-family: "SF Mono", Consolas, monospace;
      font-size: 12.5px;
      max-height: 360px;
      overflow-y: auto;
      margin-top: 10px;
    }

    #skillDiffs .diff-file {
      color: var(--accent);
      font-weight: bold;
      margin: 8px 0 2px;
    }

    #skillDiffs pre {
      white-space: pre-wrap;
      word-break: break-all;
      line-height: 1.5;
    }

    #skillDiffs .add {
      color: var(--green);
    }

    #skillDiffs .del {
      color: var(--red);
    }

    #skillMsg {
      font-size: 12px;
      color: var(--dim);
      margin-top: 8px;
    }

  </style>
</head>

<body>

  <header>
    <h1>labHandler <small>把作业材料拖进来，剩下的交给 agent</small></h1>
    <nav>
      <button id="tabTask" class="active">任务</button>
      <button id="tabSkill">Skill</button>
    </nav>
  </header>

  <!-- ═══ 任务视图 ═══ -->
  <div id="viewTask">

    <section id="secUpload">
      <h2>作业材料（等效放入 workspace）</h2>
      <div id="drop">拖拽文件到这里，或点击选择（README / 实验指导 .md .pdf .docx 等）</div>
      <input type="file" id="fileinput" multiple class="hidden">
      <ul id="filelist" class="files"></ul>
    </section>

    <section id="secTask">
      <h2>下达任务（首轮解析题面；后续输入作为补充/修订指令）</h2>
      <div class="row">
        <input type="text" id="question" placeholder="例：请按 README 完成作业" value="请按 README 完成作业">
        <button id="run">开始</button>
        <button id="stopbtn" class="ghost" disabled>停止</button>
        <button id="dreambtn" class="ghost" title="离线治理归档卡片">知识治理</button>
        <span id="status"></span>
      </div>
    </section>

    <section id="secProgress" class="hidden">
      <h2>执行进度</h2>
      <div id="log"></div>
    </section>

    <section id="secResult" class="hidden">
      <h2>结果</h2>
      <div id="verdict"></div>
      <ul id="filelist2" class="files"></ul>
      <div id="summary"></div>
      <div class="row" style="margin-top:12px">
        <button id="donebtn" class="ghost">归档结束（清场 workspace）</button>
      </div>
    </section>

  </div>

  <!-- ═══ Skill / 偏好 ═══ -->
  <div id="viewSkill" class="hidden">

    <div id="skillNotice" class="hidden">任务运行中，暂不能编辑；等本次任务结束后再来。</div>

    <section id="secSkill">
      <h2>编辑现有 skill（自然语言描述改法；文风样本先在「任务」页上传并在指令里写文件名）</h2>
      <div class="row">
        <select id="skillSel"></select>
        <button id="skillPropose">生成修改提案</button>
        <span id="skillStatus" style="font-size:12px;color:var(--dim)"></span>
      </div>
      <textarea id="skillInstr" rows="3" placeholder="例：删掉实验报告模板中的『实验心得』章节，以后都不要该章节；或：学习 我的报告.docx 的文风"></textarea>
      <div id="skillMsg"></div>
      <div id="skillDiffs"></div>
      <div class="row hidden" id="skillActions" style="margin-top:10px">
        <button id="skillApply">应用修改</button>
        <button id="skillCancel" class="ghost">取消</button>
      </div>
    </section>

    <section id="secProfile">
      <h2>长期偏好（/remember）</h2>
      <p style="font-size:12px;color:var(--dim);margin-bottom:10px">
        写入 profile。SPEC 后由 Remember-Judge 裁定是否适用于本 lab；步骤 Judge 对照适用条目，未满足不能结束。
      </p>
      <ul id="ruleList" class="files"></ul>
      <div class="row">
        <input id="ruleInput" type="text" placeholder="例：实验报告结论必须含「误差分析」" style="flex:1">
        <button id="ruleAdd">追加规则</button>
      </div>
    </section>

  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let es = null;          // EventSource
    let streamBuf = null;   // 当前流式文字 span
    let running = false;    // 任务运行态（提交成功 → true；SSE final/error → false）

    // ─── 运行态统一切换（tab 置灰 / 上传禁用 / 开始按钮禁用） ──
    function setRunning(v) {
      running = v;
      $('run').disabled = v;
      $('stopbtn').disabled = !v;
      $('dreambtn').disabled = v;
      $('status').textContent = v ? '运行中…' : '';
      $('drop').classList.toggle('disabled', v);
      $('fileinput').disabled = v;
      $('tabSkill').disabled = v;
      $('skillNotice').classList.toggle('hidden', !v);
      $('secSkill').classList.toggle('hidden', v);
      if (v && !$('viewSkill').classList.contains('hidden')) switchTab('task');
    }

    // ─── tab 切换 ───────────────────────────────────────────────
    function switchTab(name) {
      const task = name === 'task';
      $('viewTask').classList.toggle('hidden', !task);
      $('viewSkill').classList.toggle('hidden', task);
      $('tabTask').classList.toggle('active', task);
      $('tabSkill').classList.toggle('active', !task);
    }
    $('tabTask').onclick = () => switchTab('task');
    $('tabSkill').onclick = () => { if (!running) switchTab('skill'); };

    // ─── 文件上传 / 删除 ────────────────────────────────────────
    async function refreshFiles(listEl) {
      const files = await (await fetch('/api/files')).json();
      listEl.innerHTML = files.map(f =>
        `<li><a href="/api/files/${encodeURIComponent(f.name)}" download>${f.name}</a>` +
        `<span style="color:var(--dim)">${f.size} B` +
        `<button class="del-btn" data-name="${encodeURIComponent(f.name)}" title="删除（移入 .trash/）">✕</button></span></li>`).join('');
      listEl.querySelectorAll('.del-btn').forEach(btn => {
        btn.onclick = () => deleteFile(decodeURIComponent(btn.dataset.name));
      });
    }
    async function deleteFile(name) {
      if (!confirm(`删除 ${name}？（会移入 .trash/，不真删）`)) return;
      const resp = await fetch('/api/files/' + encodeURIComponent(name), { method: 'DELETE' });
      if (!resp.ok) { alert('删除失败：' + (await resp.json()).detail); return; }
      await refreshFiles($('filelist'));
      if (!$('secResult').classList.contains('hidden')) await refreshFiles($('filelist2'));
    }
    async function uploadFiles(files) {
      if (!files.length || running) return;
      const fd = new FormData();
      for (const f of files) fd.append('files', f);
      const resp = await fetch('/api/files', { method: 'POST', body: fd });
      if (!resp.ok) alert('上传失败：' + (await resp.json()).detail);
      await refreshFiles($('filelist'));
    }
    $('drop').onclick = () => { if (!running) $('fileinput').click(); };
    $('fileinput').onchange = (e) => uploadFiles([...e.target.files]);
    $('drop').ondragover = (e) => { e.preventDefault(); if (!running) $('drop').classList.add('hover'); };
    $('drop').ondragleave = () => $('drop').classList.remove('hover');
    $('drop').ondrop = (e) => {
      e.preventDefault(); $('drop').classList.remove('hover');
      uploadFiles([...e.dataTransfer.files]);
    };

    // ─── 转录渲染：节点 bullet / 工具行 / dim 结果 ─────────────
    function logLine(cls, text) {
      streamBuf = null;
      const s = document.createElement('span');
      s.className = cls; s.textContent = text;
      $('log').appendChild(s);
      $('log').scrollTop = $('log').scrollHeight;
    }
    function logTool(name, args) {
      streamBuf = null;
      const s = document.createElement('span');
      s.className = 'tool';
      const b = document.createElement('b');
      b.textContent = '• ' + name;
      s.appendChild(b);
      if (args) {
        const i = document.createElement('i');
        i.textContent = '(' + args + ')';
        s.appendChild(i);
      }
      $('log').appendChild(s);
      $('log').scrollTop = $('log').scrollHeight;
    }
    function logStream(text, reasoning) {
      if (!streamBuf) {
        streamBuf = document.createElement('span');
        streamBuf.className = reasoning ? 'reason' : 'stream';
        $('log').appendChild(streamBuf);
      }
      streamBuf.textContent += text;
      // 流式文字只保留末尾 2000 字符，防 DOM 膨胀
      if (streamBuf.textContent.length > 2000)
        streamBuf.textContent = streamBuf.textContent.slice(-2000);
      $('log').scrollTop = $('log').scrollHeight;
    }

    // ─── SSE 订阅（提交成功后 / 刷新页面时 running 恢复共用） ──
    function attachStream() {
      $('secProgress').classList.remove('hidden');
      $('secResult').classList.add('hidden');
      es = new EventSource('/api/task/stream');
      es.addEventListener('node_start', (e) => {
        const d = JSON.parse(e.data);
        logLine('node', '• ' + d.node);
      });
      es.addEventListener('content', (e) => {
        const d = JSON.parse(e.data);
        logStream(d.text, d.reasoning);
      });
      es.addEventListener('tool', (e) => {
        const d = JSON.parse(e.data);
        logTool(d.name, (d.args || '').slice(0, 80));
        if (d.result) logLine('toolret', '└ ' + d.result.slice(0, 140));
      });
      es.addEventListener('node_done', (e) => {
        const d = JSON.parse(e.data);
        const bits = (d.log || []).map(en =>
          Object.entries(en).filter(([k]) => k !== 'node')
            .map(([k, v]) => `${k}=${JSON.stringify(v).slice(0, 60)}`).join(' · '));
        logLine('done', '✓ ' + (bits.join(' | ') || 'done'));
      });
      es.addEventListener('error_class', (e) => {
        const d = JSON.parse(e.data);
        logLine('err', 'error_class=' + (d.error_class || d.detail || ''));
      });
      es.addEventListener('recovery', (e) => {
        const d = JSON.parse(e.data);
        logLine('done', 'recovery ' + JSON.stringify(d).slice(0, 160));
      });
      es.addEventListener('worker_brief', (e) => {
        const d = JSON.parse(e.data);
        logLine('done', 'brief ' + (d.task_id || '') + ' ' + (d.outcome || '') + ' gate=' + (d.gate || ''));
        if (d.brief) logLine('toolret', '└ ' + String(d.brief).slice(0, 200));
      });
      es.addEventListener('pro_takeover', (e) => {
        logLine('node', '• pro takeover');
      });
      es.addEventListener('trace', (e) => {
        const d = JSON.parse(e.data);
        if (d.trace_id) logLine('toolret', 'trace ' + d.trace_id);
      });
      es.addEventListener('error', (e) => {
        // 只处理服务端推的 kind=error。浏览器自带的 EventSource error 没有 data，
        // 断线重连时也会触发；当成任务失败会把进度停死在最后一个 node。
        if (!e.data) return;
        try {
          const d = JSON.parse(e.data);
          logLine('err', '✗ ' + (d.detail || e.data));
        } catch {
          logLine('err', '✗ ' + e.data);
        }
        finish(null);
      });
      es.addEventListener('final', async (e) => {
        const d = JSON.parse(e.data);
        finish(d);
      });
    }

    // ─── 任务提交 ───────────────────────────────────────────────
    $('run').onclick = async () => {
      const q = $('question').value.trim();
      if (!q) return;
      const resp = await fetch('/api/task', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      if (!resp.ok) { alert((await resp.json()).detail); return; }
      setRunning(true);
      $('log').innerHTML = '';
      attachStream();
    };

    async function finish(final) {
      if (es) { es.close(); es = null; }
      setRunning(false);
      if (!final) return;
      $('secResult').classList.remove('hidden');
      const v = $('verdict');
      v.textContent = (final.verdict === 'pass' ? '✓ 通过' : '⚠ ' + (final.verdict || 'done')) +
        ` · ${final.elapsed}s`;
      v.className = final.verdict === 'pass' ? 'pass' : 'fail';
      await refreshFiles($('filelist2'));
      await refreshFiles($('filelist'));
      const s = await (await fetch('/api/summary')).json();
      $('summary').textContent = s.summary || '（无 SUMMARY.md）';
    }

    $('stopbtn').onclick = async () => {
      await fetch('/api/stop', { method: 'POST' });
    };

    // ─── 归档结束 ───────────────────────────────────────────────
    $('donebtn').onclick = async () => {
      if (!confirm('归档当前任务并清场 workspace？（文件移入 .trash/，沙箱容器将重建，约几十秒）')) return;
      const resp = await fetch('/api/done', { method: 'POST' });
      const d = await resp.json();
      if (!resp.ok) { alert(d.detail); return; }
      alert('已归档：' + JSON.stringify(d.archive));
      $('secResult').classList.add('hidden');
      $('secProgress').classList.add('hidden');
      await refreshFiles($('filelist'));
    };

    // ─── 知识治理（/dream） ─────────────────────────────────────
    $('dreambtn').onclick = async () => {
      if (!confirm('对归档知识卡片做一轮 LLM 合并/去重/淘汰？（可能需要几分钟）')) return;
      $('dreambtn').disabled = true;
      try {
        const resp = await fetch('/api/dream', { method: 'POST' });
        const d = await resp.json();
        if (!resp.ok) { alert(d.detail); return; }
        alert(`治理完成：共 ${d.total_cards} 张卡 / 判定 ${d.judged} 组 / 新建合并卡 ${d.merged_created} / 淘汰 ${d.retired}`);
      } finally { $('dreambtn').disabled = false; }
    };

    // ─── Skill 编辑（提案 → diff → 确认/取消） ──────────────────
    let editId = null;   // 当前 pending 提案 id

    async function loadSkills() {
      try {
        const skills = await (await fetch('/api/skills')).json();
        $('skillSel').innerHTML = skills.map(s =>
          `<option value="${s.name}">${s.name}</option>`).join('');
      } catch (e) { /* 服务未就绪时静默 */ }
    }
    function renderDiffs(diffs) {
      $('skillDiffs').innerHTML = '';
      for (const d of diffs) {
        const title = document.createElement('div');
        title.className = 'diff-file'; title.textContent = '📄 ' + d.file;
        const pre = document.createElement('pre');
        for (const ln of d.diff.split('\n')) {
          const span = document.createElement('span');
          if (ln.startsWith('+') && !ln.startsWith('+++')) span.className = 'add';
          else if (ln.startsWith('-') && !ln.startsWith('---')) span.className = 'del';
          span.textContent = ln + '\n';
          pre.appendChild(span);
        }
        $('skillDiffs').append(title, pre);
      }
    }
    $('skillPropose').onclick = async () => {
      const instr = $('skillInstr').value.trim();
      if (!instr) { alert('请填写编辑指令'); return; }
      $('skillPropose').disabled = true;
      $('skillStatus').textContent = 'LLM 分析中…';
      $('skillMsg').textContent = ''; $('skillDiffs').innerHTML = '';
      $('skillActions').classList.add('hidden'); editId = null;
      try {
        const resp = await fetch('/api/edit_skill', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ skill_name: $('skillSel').value, instruction: instr }),
        });
        const d = await resp.json();
        if (!resp.ok) { alert(d.detail); return; }
        let msg = d.summary || '';
        if (d.style_samples && d.style_samples.length)
          msg += `　（已读取文风样本：${d.style_samples.join('、')}）`;
        if (d.sample_failures && d.sample_failures.length)
          msg += `　⚠️ 文风样本未用上：${d.sample_failures.join('；')}`;
        $('skillMsg').textContent = msg;
        if (!d.n_operations) {
          $('skillMsg').textContent += '　—— 判官认为无需改动';
          return;
        }
        renderDiffs(d.diffs);
        editId = d.edit_id;
        $('skillActions').classList.remove('hidden');
      } finally {
        $('skillPropose').disabled = false;
        $('skillStatus').textContent = '';
      }
    };
    async function applyEdit(confirm) {
      if (!editId) return;
      const resp = await fetch('/api/edit_skill/apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edit_id: editId, confirm }),
      });
      const d = await resp.json();
      if (!resp.ok) { alert(d.detail); return; }
      $('skillMsg').textContent = confirm ? ('✓ 已应用：' + (d.applied || []).join('、')) : '已取消，未落盘';
      $('skillDiffs').innerHTML = '';
      $('skillActions').classList.add('hidden');
      editId = null;
    }
    $('skillApply').onclick = () => applyEdit(true);
    $('skillCancel').onclick = () => applyEdit(false);

    // ─── 长期偏好规则（/api/profile + /api/remember） ──────────
    async function loadRules() {
      try {
        const prof = await (await fetch('/api/profile')).json();
        const rules = (prof.preferences || {}).style_rules || [];
        const ul = $('ruleList');
        ul.innerHTML = '';
        if (!rules.length) {
          const li = document.createElement('li');
          li.style.color = 'var(--dim)'; li.textContent = '（暂无规则；在下方输入追加）';
          ul.appendChild(li); return;
        }
        for (const r of rules) {
          const li = document.createElement('li'); li.textContent = r; ul.appendChild(li);
        }
      } catch (e) { /* 服务未就绪时静默 */ }
    }
    $('ruleAdd').onclick = async () => {
      const rule = $('ruleInput').value.trim();
      if (!rule) return;
      const resp = await fetch('/api/remember', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rule }),
      });
      const d = await resp.json();
      if (!resp.ok) { alert(d.detail); return; }
      $('ruleInput').value = '';
      await loadRules();
    };

    // ─── 初始化：文件列表 + skills + 规则；running 则重接 SSE，resumable 则询问续跑
    async function init() {
      await refreshFiles($('filelist'));
      await loadSkills();
      await loadRules();
      try {
        const st = await (await fetch('/api/state')).json();
        if (st.running) {
          setRunning(true);
          $('log').innerHTML = '';
          attachStream();
        } else if (st.resumable) {
          if (confirm('上次 lab 未完成，是否继续？取消则归档并新开 lab。')) {
            const resp = await fetch('/api/resume', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ continue_lab: true, question: '继续上次未完成的 lab' }),
            });
            if (resp.ok) { setRunning(true); $('log').innerHTML = ''; attachStream(); }
          } else {
            await fetch('/api/resume', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ continue_lab: false }),
            });
          }
        }
      } catch (e) { /* 状态接口失败不阻塞页面 */ }
    }
    init();
  </script>
</body>

</html>
```

## 落地脚本说明

上面「怎么落地」里的提取器从 `## 文件正文` 起扫描 `PACKFILE` 注释，读取随后代码围栏的正文写盘。围栏长度按文件内容自动加长，避免 SKILL.md 里的三级反引号打断。
