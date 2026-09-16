# labHandler

中文大学 Lab 自动化 AI agent。把作业材料丢进 `workspace/`，自建 harness 在沙箱内完成规划、执行、程序性验收与总结，产物和 `SUMMARY.md` 写回 `workspace/`。

## 核心能力

- **SPEC.md 指挥**：Pro 先写 SPEC.md 作为总纲（总目标、全局接口契约、交给 Flash 的产品里程碑），
  之后每一步只决定「接下来派给 Flash 什么」，给每个 Flash 单独写一份任务书。
  Flash 每次 assignment 新开空对话。
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
| Remember | 写入 profile 长期偏好；Remember-Judge 裁定是否适用，步骤 Judge 对照后才能结束 |
| 知识治理 | 离线合并/淘汰卡片并重建向量索引 |
| Skill 编辑 | 自然语言改现有 skill，diff 确认后落盘 |
