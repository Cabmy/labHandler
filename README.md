# labHandler

中文大学 Lab 自动化 agent。作业材料放进 `workspace/`，harness 在 Docker 沙箱内完成规划、执行、验收与总结，产物和 `SUMMARY.md` 写回 `workspace/`。

## 工作流程

```
ingest 材料目录 → Remember-Judge 裁定长期规则 → Pro 写 SPEC.md
→ 逐步派发 Flash（1 个可写 / 多个只读并行）→ 沙箱内 pytest 验收
→ Pro Judge（continue / finish / revise_spec / takeover）→ SUMMARY.md
```

缺用户才能给的信息时，任一阶段可 `submit_halt` 短路到 SUMMARY。

## 核心设计

- **SPEC 指挥**：Pro 先写 SPEC.md 钉死总目标与接口契约，之后每步只交出下一份 Flash 任务书；Flash 每次新开空对话，一次只做一步能做完的量。
- **阶段强制**：工具表按「角色 × 阶段」收窄，每阶段只暴露自己的 submit 出口；表外调用直接被拒。
- **程序性验收四态**：`pass` / `fail` / `test_invalid` / `no_hard_criteria`（无硬指标不等于通过）。验收代码只由 Pro 写，在沙箱内跑 pytest；产物不可检验时如实标注，交 Judge 语义判断。
- **Runtime Task 控制面**：状态、预算、取消、权限在 Task 树上，LLM 只看见语义投影。
- **上下文管理**：超预算即压缩——近期留原文、早期成纪要、tool 正文卸盘，压完重新装配再发请求。NOTES.md 常驻，FORGET.md 记排除项。
- **幂等续跑**：append-only JOURNAL.jsonl 逐轮落盘，崩在任意一轮可续；副作用账本按产物指纹跳过已完成任务。
- **安全边界**：host 白名单 + 路径守护 + 审计；重量操作走 MCP 进 Docker 沙箱。
- **可观测**：Langfuse + 本地 JSONL，span 覆盖 run / step / turn / llm / tool / 验收。
- **跨 lab 记忆**：卡片 markdown 为事实源，向量索引为派生；`/dream` 离线治理。
- **会话隔离**：`done` 归档 → workspace 进 `.trash/` → 重建沙箱 → 新 lab。

设计细节与不变量见 [AGENTS.md](AGENTS.md)。

## 安装

要求 Python 3.11、Docker、AgentRouter Key（Chat）与 Paratera Key（Embedding）。

```bash
conda create -n labhandler python=3.11 -y && conda activate labhandler
pip install -r requirements.txt
cp config/.env.example config/.env   # 填 LLM_API_KEY 与 EMBEDDING_API_KEY
```

必填项缺失启动即报错。关键配置：

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

口径见 `eval/` 各脚本 docstring。

## Web 操作

| 动作 | 作用 |
|---|---|
| 上传材料 + 下达任务 | 跑当前 lab |
| 停止 | 取消 in-flight Task，lab 仍在 |
| 归档结束 | 卡片入档 → 清场 → 重建沙箱 → 下一个 lab |
| Remember | 写长期偏好；Remember-Judge 裁定适用性，步骤 Judge 对照后才可结束 |
| 知识治理 | 离线合并/淘汰卡片并重建向量索引 |
| Skill 编辑 | 自然语言改现有 skill，diff 确认后落盘 |
