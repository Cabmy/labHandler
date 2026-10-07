# labHandler

中文大学 Lab 自动化 agent。作业材料放进 `workspace/`，harness 在 Docker 沙箱内完成规划、执行、验收与总结，产物和 `SUMMARY.md` 写回 `workspace/`。

## 工作流程

```
ingest 材料目录 → Remember-Judge 裁定长期规则 → Pro 写 SPEC.md
→ 逐步派发 Flash（1 个可写 / 多个只读并行）→ 沙箱内 pytest 验收
→ Pro Judge（continue / finish / revise_spec / takeover）→ SUMMARY.md
```

缺用户才能给的信息时，Pro 阶段可 `submit_halt` 短路到 SUMMARY；Flash 通过 blocked brief 上报。

## 架构

分层视图：HTTP 入口 → RuntimeTask 控制面 → 阶段流水线 → 每个 Pro/Flash 节点的
一轮 ReAct → LLM 网关与工具（经 MCP 进 Docker 沙箱），最下是持久化/记忆/可观测横切层。
Pro、Flash 是职责角色，不代表具体供应商；可使用不同网关，也可使用名称相同的模型。

```
┌────────────────────────────────────────────────────────────────────┐
│ server · FastAPI 127.0.0.1:8000                                    │
│   Web UI ─► tasks · files · memory · skills                        │
│   one lab per session · thread_id · upload & task control          │
└────────────────────────────────┬───────────────────────────────────┘
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ control plane · runtime/task.py RuntimeTask tree                   │
│   status · step_budget · wall-clock deadline · permission · cancel │
│   LLM sees only the semantic projection, never counters/cancel     │
│                                                                    │
│   LabRunner (runtime/lab/runner.py) ─► flow.run_lab(LabState)      │
└────────────────────────────────┬───────────────────────────────────┘
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ phase pipeline · order in runtime/lab/flow.py, defs in phase.py    │
│                                                                    │
│   ingest catalog                                                   │
│     └─► Remember-Judge (Pro, tool_choice=required) submit_remember │
│     └─► Pro submit_spec ─► SPEC.md (folds in applied /remember)    │
│     └─► step loop (<= _MAX_STEPS):                                 │
│           Pro submit_dispatch ─► write_acceptance(task_id)         │
│             ├─ 1 Flash  = writable                                 │
│             └─ N Flash  = all read-only, parallel                  │
│           Flash submit_brief ─► programmatic gate (pytest)         │
│             pass · fail · test_invalid · no_hard_criteria          │
│           Pro submit_judge ─► continue|finish|revise_spec|takeover │
│     └─► Pro submit_summary ─► SUMMARY.md                           │
│                                                                    │
│   Pro submit_halt ─► short-circuit SUMMARY (need_user)             │
└────────────────────────────────┬───────────────────────────────────┘
                                 ▼  per Pro / Flash node
┌────────────────────────────────────────────────────────────────────┐
│ one ReAct turn · runtime/loop/cycle.py:run_loop                    │
│                                                                    │
│   assemble(context) ─► llm.chat ─► tool_calls ─► task.record       │
│      ▲                                            │                │
│      │  budget / compact                          ▼                │
│      │  (> COMPACT_TRIGGER_RATIO -> compact)   control.decide      │
│      └────────────────────────────  retry|nudge|force_brief|stop   │
│                                                                    │
│   structured output via function calling only; read-only calls     │
│   run parallel, write/submit serialize; one submit_* exit each     │
└────────────────┬────────────────────────┬──────────────────────────┘
                 ▼                        ▼
┌──────────────────────────────┐  ┌──────────────────────────────────┐
│ LLM gateway · runtime/llm.py │  │ tools · tools/ + loop/registry   │
│   Pro / Flash / Embedding    │  │   fs · search · sandbox · skill  │
│   configurable API endpoints │  │   profile · policy SecurityPolicy│
│   streaming · max_retries=0  │  │   path guard + cmd whitelist     │
│   _breaker: consecutive fails│  │   + role write-protect           │
│     -> escalate error FATAL  │  │   heavy ops -> MCP -> Docker     │
└──────────────────────────────┘  └─────────────────┬────────────────┘
                                                    ▼ MCP
                                  ┌──────────────────────────────────┐
                                  │ Sandbox · configurable image     │
                                  │   PYTHONPATH=/workspace pytest   │
                                  │   artifacts + SUMMARY.md         │
                                  │     written back to workspace/   │
                                  └──────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│ cross-cutting                                                      │
│   persist · JOURNAL.jsonl (append-only) + STATE.json + ledger      │
│             idempotent resume by artifact sha256 fingerprint       │
│   memory  · card markdown (source of truth) + SQLite / sqlite-vec  │
│             /dream offline curation                                │
│   observe · configured trace backend + local JSONL                 │
│             run / step / turn / llm / tool / gate                  │
└────────────────────────────────────────────────────────────────────┘
```

## 核心设计

- **SPEC 指挥**：Pro 先写 SPEC.md 钉死总目标与接口契约，之后每步只交出下一份 Flash 任务书；Flash 每次新开空对话，一次只做一步能做完的量。
- **阶段强制**：工具表按「角色 × 阶段」收窄，每阶段只暴露自己的 submit 出口；表外调用直接被拒。
- **程序性验收四态**：`pass` / `fail` / `test_invalid` / `no_hard_criteria`（无硬指标不等于通过）。验收代码只由 Pro 写，在沙箱内跑 pytest；产物不可检验时如实标注，交 Judge 语义判断。
- **Runtime Task 控制面**：状态、预算、取消、权限在 Task 树上，LLM 只看见语义投影。
- **上下文管理**：超预算即压缩——近期留原文、早期成纪要、tool 正文卸盘，压完重新装配再发请求。NOTES.md 常驻，FORGET.md 记排除项。
- **幂等续跑**：append-only JOURNAL.jsonl 逐轮落盘，崩在任意一轮可续；副作用账本按产物指纹跳过已完成任务。
- **安全边界**：host 白名单 + 路径守护 + 审计；重量操作走 MCP 进 Docker 沙箱。
- **可观测**：Langfuse + 本地 JSONL，span 覆盖 run / step / turn / llm / tool / 验收。
- **跨 lab 记忆**：卡片 Markdown 是正文与元数据的唯一事实源；sqlite-vec 在 SQLite 内执行精确余弦检索，`/dream` 直接读取当前文件治理。
- **会话隔离**：`done` 归档 → workspace 进 `.trash/` → 重建沙箱 → 新 lab。

记忆检索在 ingest 时保存有长度上限的材料摘录，与用户请求共同构造查询。自动预取和主动检索共用候选召回阈值 `MEMORY_MIN_SCORE`（默认 0.35）；候选先按模型端点、模型名、维度和当前文件指纹过滤，再按正文去重。最多 8 张候选交给 Flash 通过 function calling 逐项判断是否与实际任务相关，最后按相似度取 top-k。向量分数不等于适用性；筛选失败或裁定不完整时明确降级，不注入未经筛选的候选。每次有候选的检索会增加一次 Flash 调用。索引失败逐卡报告，失败预取不缓存为空结果。Embedding 与筛选只对瞬时故障有限重试，完整检索受 `MEMORY_TIMEOUT_S`（默认 90 秒）限制。材料流式提取要求行并保留首尾，摘录仍有长度预算。

记忆数据库仅保存任务归档、卡片 ID/生命周期与向量缓存，不保存卡片正文副本；淘汰时删除卡片文件并记录生命周期。sqlite-vec 使用 SQL 距离函数精确扫描，不引入独立服务或 ANN 索引。该版本不兼容旧数据库 schema：升级时配置新的 `MEMORY_DB_PATH` 与 `CARDS_DIR`，旧数据保留备份；不提供旧表读取或自动迁移路径。

`PRO_NATIVE_FORCED_TOOLS` 与 `FLASH_NATIVE_FORCED_TOOLS` 分别声明端点能力，不按模型名称推断。设为 false 时，强制交卷只暴露指定工具，使用 auto 并严格校验响应，纯文本或越权工具视为失败；设为 true 时使用原生强制工具。流式解析会忽略空心跳并在取消时关闭流。

设计细节与不变量见 [AGENTS.md](AGENTS.md)。

## 安装

要求 Python 3.11、Docker，以及兼容 OpenAI Chat Completions / Embeddings 协议的服务。Chat 端点须支持流式输出和 function calling；可以使用第三方网关、自建服务或不同供应商的端点。

```bash
conda create -n labhandler python=3.11 -y && conda activate labhandler
pip install -r requirements.txt
cp config/.env.example config/.env   # 填端点、密钥、模型名及所需请求头
```

必填项缺失启动即报错；模型名和 API 地址必须显式配置，没有固定供应商回退。

| 角色 | 端点 | 密钥 | 模型 | 自定义请求头 |
|---|---|---|---|---|
| Pro | `LLM_BASE_URL` | `LLM_API_KEY` | `PRO_MODEL` | `LLM_DEFAULT_HEADERS` |
| Flash | `FLASH_BASE_URL` | `FLASH_API_KEY` | `FLASH_MODEL` | `FLASH_DEFAULT_HEADERS` |
| Embedding | `EMBEDDING_BASE_URL` | `EMBEDDING_API_KEY` | `EMBEDDING_MODEL` | `EMBEDDING_DEFAULT_HEADERS` |

Flash 与 Pro 使用同一端点时，端点、密钥和请求头可继承；切换到独立端点时须显式配置 Flash 密钥，请求头默认不继承。请求头为 JSON 对象，`{}` 表示不加自定义头。需要白名单头或自定义鉴权头时按所选服务填写；不要求额外请求头的服务无需添加供应商专用字段。网关按角色路由，同名模型也不会串用端点或密钥。无鉴权的本地服务可填写其接受的占位 key。

`AIO_SANDBOX_IMAGE` / `AIO_SANDBOX_MCP_URL` 可指定兼容沙箱镜像与 MCP 服务；遥测可使用配置的 Langfuse 兼容端点或关闭远程上报。

其他关键配置：

- `CONTEXT_BUDGET_TOKENS`：所用模型的真实上下文窗口（默认 200000），达 `COMPACT_TRIGGER_RATIO` 触发压缩。
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`：遥测后端，注意 EU/US 区域，选错会 401；留空只写本地 `workspace/.labhandler/traces.jsonl`。

## 启动

```bash
python -m server   # 默认 127.0.0.1:8000
```

首次启动拉起 AIO Sandbox 容器（镜像约 2.29GB），进程退出时 `docker stop`；`LAB_AUTOSTART_SANDBOX=false` 可禁用自启。

## 测试与评测

```bash
python -m eval.selftest_report              # 聚合逻辑自检，无需 LLM
python eval/run_case.py --case two_sum      # 跑单个 case
PYTHONPATH=. python eval/run_suite.py --k 3 # 跑 suite（需 LLM Key）
```

评测与主流程共用 `config/runtime.py` 加载的 `config/.env`：Pro / Flash / Embedding
模型、网关、密钥和遥测均沿用配置。每次评测隔离工作区与记忆库，使用测试画像；
case 的 `budget` / `reserve` 可覆盖上下文预算，普通 case 使用启动配置，
`no_compact` 使用启动配置的窗口与输出预留覆盖 case 预算。口径见 `eval/` 各脚本 docstring。


检索验证（不启动 Docker）：

```bash
python -m pytest -q eval/test_memory.py eval/test_providers.py
# 实际调用配置中的 Embedding 与 Flash API，仅使用合成卡片和临时数据：
python -m eval.live_memory --output /tmp/labhandler-memory-check
```

真实 API 评测输出 `report.json`，分别报告召回、无关查询弃权、精确选择和错误次数；Embedding 缓存绑定端点、模型和鉴权配置摘要；更换请求头或密钥也会重建派生索引，摘要不会暴露密钥。它验证固定样本，不代表所有未来任务均能正确检索。

## Web 操作

| 动作 | 作用 |
|---|---|
| 上传材料 + 下达任务 | 跑当前 lab |
| 停止 | 取消 in-flight Task，lab 仍在 |
| 归档结束 | 卡片入档 → 清场 → 重建沙箱 → 下一个 lab |
| Remember | 写长期偏好；Remember-Judge 裁定适用性，步骤 Judge 对照后才可结束 |
| 知识治理 | 离线合并/淘汰卡片并重建向量索引 |
| Skill 编辑 | 自然语言改现有 skill，diff 确认后落盘 |
