# hwHandler

> 中文大学作业 / Lab 自动化 AI agent。把作业说明丢进 `workspace/`，agent 在沙箱内完成实现 → 测试 → 校验 → 总结的闭环，最终落地到本地 workspace。

## 项目定位

把 Plan-Execute-Replan agent 的能力具体化到「写代码 + 写实验报告」这个垂直场景，跑通从"题面理解"到"产物 + 教训沉淀"的完整闭环：

- **输入**：`workspace/` 下的作业说明（README.md / 实验指导.md / .pdf / .docx）
- **输出**：`workspace/<产物文件>` + `workspace/SUMMARY.md`（人话提纲，面向用户）
- **跨任务记忆**：每次 `/done` 沉淀到 `task_archive`（SQLite + Chroma 双写），下次新作业时 Planner 自动召回历史经验

## 架构

```
┌───────────────────── workspace/ (host 边界) ─────────────────────┐
│  README.md / lab_*.md / lab_*.pdf  ← 作业说明                     │
│  solution.py / lab_report.md        ← 产物（Coder 写）            │
│  SUMMARY.md                         ← 人话提纲（Summarizer 写）   │
│  .hwhandler/progress_log.jsonl      ← 节点完成日志                │
│  .hwhandler/tool_history.jsonl      ← 所有工具调用                │
└───────────────────────────────────────────────────────────────────┘
                  ▲                                    ▲
                  │ host fs_tools (路径越界 → PermissionError)
                  │                                    │
┌─────────────────┴────────────────────┐  ┌────────────┴───────────┐
│  cli.py REPL                          │  │ AIO Sandbox 容器       │
│   ↓                                   │  │  (33 个 MCP tools:     │
│  orchestrator/graph.py (LangGraph)    │  │   sandbox_execute_code │
│   Intake → Planner → Coder → Verifier │  │   sandbox_*_editor     │
│              ↑                ↓        │  │   browser_*            │
│              └─ Replan ←─ fail        │  │   convert_to_markdown) │
│                              ↓ pass   │  └────────────────────────┘
│              Compile → Summarizer     │
│   ↓                                   │
│  ui/live_panel (rich + graph.stream)  │
└───────────────────────────────────────┘
```

**LLM 后端**：Paratera（DeepSeek-V4-Pro 思考模式 `reasoning_effort=max` + GLM-Embedding-3 dim=2048）
**编排**：LangGraph 主图 + LangGraph prebuilt `create_react_agent`
**RAG**：BM25（jieba 分词）+ Vector（Chroma）+ RRF 融合（k=60）
**沙箱**：字节 AIO Sandbox 自托管 Docker 容器，自带 MCP 接口
**MCP**：单一 `aio_sandbox` server（评估后裁掉了 fetch MCP，因 sandbox 自带的 `convert_to_markdown` + `browser_*` 是其超集）

## 安装与运行

```bash
# 1. 创建 conda 环境
conda create -n hwhandler python=3.11 -y && conda activate hwhandler
pip install -r requirements.txt

# 2. 配置（.env 单一来源）
cp .env.example .env
# 编辑 .env 填入 PARATERA_API_KEY；其他变量保持默认即可

# 3. 把作业说明丢进 workspace/，启动 REPL（容器自动起，首次拉镜像约 2.29GB / 5 分钟）
echo "实现二分查找（O(log n)）" > workspace/README.md
python cli.py
# › 请按 README 实现
# ⏳ intake → planner → coder ✅ → verifier → compile → summarizer
# › /done       # 沉淀到 task_archive
# › /quit
```

> **容器自动管理**：`cli.py` 启动时检测 `AIO_SANDBOX_MCP_URL` 是否通；不通则按 `docker inspect aio-sandbox` 状态自动 `docker start` 或 `docker run`，并轮询 60s 等就绪。设置 `HW_AUTOSTART_SANDBOX=false` 可禁用，回到手动管控（`docker run -d --name aio-sandbox --security-opt seccomp=unconfined --shm-size 2g -p 8080:8080 -v ./workspace:/workspace -e DISABLE_JUPYTER=true -e DISABLE_CODE_SERVER=true <image>`）。
>
> **⚠️ 旧用户一次性迁移**：本版本起 `workspace/` 会通过 `-v` bind-mount 到容器内 `/workspace/`，让 sandbox 工具直接读 PDF/DOCX。**如果你之前已经 `docker run` 过老镜像（无此挂载），启动时会看到迁移提示**，请运行一次：
> ```bash
> docker rm -f aio-sandbox
> ```
> 然后重启 `python cli.py`，cli 会自动用新挂载重建容器。

REPL 命令：`/help` `/done` `/done --clear` `/show` `/show summary` `/skills` `/profile` `/quit`

## 简历亮点（架构决策）

### 1. Plan-Execute-Replan 主图（`orchestrator/graph.py` + `replan.py`）
6 业务节点 + Verifier 后 conditional_edges 路由：
- `pass` → Compile → Summarizer
- `fail` 且 iter < `MAX_REPLAN_ITER=2` → 回 Planner（重拆 DAG）
- `fail` 且 iter ≥ MAX → Compile（标 partial 走 Summarizer 出"部分完成"）

**实测触发**：Phase 5 e2e 实测 Verifier 第一轮 fail → Replan 自动回 Planner → 第二轮 pass，verifier_runs=`[fail, pass]`，iteration=2，最终全部 pytest 通过。

### 2. Verifier 两阶段（硬指标 + LLM 语义覆盖）
- **阶段 1**：硬指标按 `intake.type` 分支：coding 类查交付物存在 + `pytest -q` 通过；lab_report 类查必备章节齐
- **阶段 2**：LLM 语义覆盖判官，输入 `intake.constraints + user_constraints + artifacts 内容`，输出 `{covered:[...], missing:[...], suggested_fix:""}` 结构化判定
- verdict 二态：`pass / fail`；任何阶段 1 硬指标失败或阶段 2 语义缺失都直接 fail，触发 Replan

设计要点：约束来源**双源**：题面（Intake 抽）+ 用户对话（HwState.user_constraints 累加），Verifier 同时比对。

### 3. AIO Sandbox 沙箱选型 + MCP 单 server 极简化
**为什么没用 E2B / 自实现 Docker SDK**：
| 维度 | 自实现 Docker | E2B (Firecracker) | **AIO Sandbox（已选）** |
|---|---|---|---|
| 工作量 | ~150 行 + Dockerfile | ~30 行 SDK | **~30 行 MCP 客户端** |
| MCP 原生 | ❌ | ✅ | ✅ **容器内置 MCP** |
| 国内可用 | ✅ | ❌ 需国际卡 | ✅ 字节自研 |
| 综合能力 | 自己实现 | 仅代码执行 | **一体化** Browser/VSCode/Jupyter |

**为什么从 2 个 MCP server 裁到 1 个**：原计划接 `aio_sandbox + @modelcontextprotocol/server-fetch` 两个；Phase 4 评估后发现 sandbox 的 `sandbox_convert_to_markdown`（http(s) URI → markdown）+ 23 个 `browser_*` tools（带 JS 渲染）是 fetch MCP 的超集，留两个反而增加部署复杂度 → 裁到 1 个。

### 4. 跨会话经验沉淀（SQLite + Chroma 双写）
- `memory/archive.py` 每次 `/done` 写一行：title / type / summary / lessons / workspace_snapshot（= SUMMARY.md 全文）
- SQLite 是 source of truth（结构化查询）；Chroma 当语义索引（用 title+summary+lessons 拼 embedding 输入）
- Planner 接到新作业时先调 `archive_search` 召回 Top-3 相似历史 `lessons` 拼 prompt
- **跨进程一致**：两者都是文件存储，subprocess 启新 python 立即可用

**实测**：Phase 8 e2e 中 P8.1 实现二分查找 → /done 沉淀 → P8.2 微调 → /done --clear → **subprocess 真重启 python 跑 archive_search**，召回 2 条历史经验。

### 5. DeepSeek-V4-Pro 思考模式契约
- `reasoning_effort: "max"` + `extra_body={"thinking": {"type": "enabled"}}` 顶层传给 ChatOpenAI（model_kwargs 会触发 langchain-openai 1.2 警告）
- Phase 0 反向验证发现 Paratera **不强制必传 reasoning_content**（与文档不符）→ 简化为"建议保留 + 仅 warn 不 raise"，避免主流程因序列化丢字段而报错

### 6. 主 agent 双层权限边界
- **宿主侧**：`tools/fs_tools.py` 的 `_safe_path` helper：`Path(p).resolve()` 后必须 `is_relative_to(WORKSPACE_DIR)`，否则 `PermissionError`；`host_bash` 限 `cwd=WORKSPACE_DIR` + regex 黑名单（拒 `..` / 绝对路径前缀 / `~` 展开）
- **沙箱侧**：所有重操作走 AIO Sandbox 容器，享受 Docker fs 隔离 + 用户权限限制（容器内非 root；`/etc/passwd` / `/host_escape` 写入会被拒）
- **越权 4 用例**实测全部抛 `PermissionError`：`/etc/passwd`、`../../etc`、`host_bash 'cat /etc/passwd'`、`host_bash 'cd .. && ls'`

### 7. RAG 混合检索 + RRF 融合
- BM25（jieba 中文分词）+ Vector（GLM-Embedding-3 dim=2048）双路召回
- RRF 融合（`k_const=60` 来自 Cormack et al. 2009）：`score(d) = Σ 1/(k+rank_i(d))`
- 不做加权求和（BM25 分数和余弦相似度尺度不齐，要先归一化才能加；RRF 只用排名信息规避了这个问题）

### 8. Skills 1M context 全塞策略
- `skills/coding.md` / `essay.md` / `lab_report.md` 启动时全部拼进 system prompt（DeepSeek-V4-Pro 1M context 够用）
- **未来可改按需加载**（context 紧张时）；当前选择全塞是因为 Planner 选 skill 时不知道用户后续会不会换题型，全在比加载-卸载省心

### 9. Profile 自修改（identity 双工 + preferences 直写）
- REPL 层一次轻量 LLM 调用判用户输入是否在改 profile（"以后我..." / "学号写错了..."）
- `identity.*` 变更：cli `Prompt.ask` y/N 二次确认（敏感字段防误改）
- `preferences.*` 变更：直接 `update_profile` 写回 yaml
- 实测意图判别准：`"以后代码用 type hints"` / `"学号写错了"` / 普通任务请求 三类全准

## 端到端实测（Phase 8）

| 用例 | 结果 |
|---|---|
| **P8.1 冒烟（二分查找）** | 252.9s / verdict=pass / **pytest 10/10** / SUMMARY 7 章节齐 |
| **P8.2 多轮微调（"加详细注释"）** | 455.5s / 双轮 pass / 注释行 13→101 (+88) / pytest 仍 10 过 |
| **P8.3 跨会话（subprocess 真重启）** | 召回 2 条历史经验 |
| **P8.4 沙箱越权（5 用例）** | Docker fs 隔离生效（看不到宿主 /home）；容器用户权限拒 `/etc/passwd` 写；网络默认开放（注：见 Future Work） |

## Future Work

**架构演进**：
- 沙箱升级到 KVM MicroVM（CubeSandbox / E2B Firecracker）以获得内核级隔离 + 网络策略
- 单容器常驻 → 容器池（多任务并行）

**Verifier 增强**：
- 阶段 2 加 ruff / mypy / type_hints / writing_style 漂移检测（当前只做语义覆盖）

**用户体验**：
- CLI 增量总结面板 diff 渲染（follow-up 时只渲染本轮改动）
- Profile 自修改 audit log（当前直接覆盖 yaml；未来加 git-style 历史 + 回滚）

**集成扩展**：
- MCP server 暴露自家能力（archive_search / load_skill / verify_artifact）
- Watchdog 实时监听 workspace 变化（当前一次进程一个任务）

**题型扩展**：
- 数学证明类作业（LaTeX）
- 数据分析类（Jupyter notebook 沉淀）

## 项目结构

```
hwHandler/
├── llm/                # Paratera (DeepSeek-V4-Pro) + Ollama 兜底
├── rag/                # BM25 + Vector + RRF（jieba 中文分词）
├── memory/             # task_archive (SQLite+Chroma) + Profile 读写
├── profile/me.yaml     # 用户画像（identity + preferences）
├── mcp_client/         # MCP 客户端（仅 AIO Sandbox 单 server）
├── skills/             # 3 类作业 SOP（coding / essay / lab_report）
├── tools/              # 14 个本地工具 + 33 个 sandbox MCP（动态加载）
├── orchestrator/       # LangGraph 主图 + Replan + Compile + State
├── agents/             # 5 个核心 agent
├── ui/live_panel.py    # rich + graph.stream 实时渲染
├── cli.py              # REPL 入口
├── workspace/          # 用户工作区（fs_tools 路径守护）
├── PLAN.md             # 设计文档（约 1500 行）
├── STEPS.md            # 实施步骤（Phase 0-8 完成记录）
└── verify/REPORT.md    # Phase 0 7 条踩坑记录
```

## 简历叙事（3-5 行版）

> hwHandler — 基于 LangGraph + AIO Sandbox MCP 的中文大学作业 / Lab 自动化 agent。设计并落地 Plan-Execute-Replan 主图（Verifier 两阶段：硬指标 + LLM 语义覆盖）、双层权限边界（宿主 fs_tools `_safe_path` + 沙箱 Docker 隔离）、跨会话 RAG 经验沉淀（SQLite + Chroma 双写 + RRF 融合）；针对 DeepSeek-V4-Pro 思考模式做了完整契约验证（Phase 0 7 条踩坑记录到 verify/REPORT.md）；评估后把 MCP 多 server 裁到单 server（sandbox_convert_to_markdown + browser_* 是 fetch MCP 超集）。端到端实测：实现二分查找 252s pass；多轮"加详细注释"注释行 +88、pytest 全过；跨进程 subprocess 召回历史经验。
