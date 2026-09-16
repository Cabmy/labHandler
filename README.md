# labHandler

中文大学 Lab 自动化 AI agent。把作业材料丢进 `workspace/`，自建 harness 在沙箱内完成规划、执行、程序性验收与总结，产物和 `SUMMARY.md` 写回 `workspace/`。

## 核心能力

- **Runtime Task 控制面**：执行状态、预算、取消、权限在 Task 树上；LLM 只看见语义投影
- **Pro / Flash**：`glm-5.3` 规划与判定，`deepseek-v4-flash` 执行；一波 1 个 Flash 可写，多个 Flash 全只读
- **function calling 结构化出口**：`submit_plan` / `submit_brief` / `submit_judge` / `submit_summary`
- **程序性验收四态**：`pass` / `fail` / `test_invalid` / `no_hard_criteria`（无硬指标不等于通过）
- **上下文压缩**：超阈值时 Pro 摘要旧对话，并把 tool 正文卸到文件系统
- **两层安全边界**：host 白名单 + 路径守护 + 审计；重量操作走 MCP Docker 沙箱
- **跨 lab 记忆**：卡片文件 + 向量索引最终一致；`/dream` 离线治理
- **会话隔离**：`done` 归档 → 清场 → 重建沙箱 → 新 lab

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
| 追加规则 | 写入 profile `style_rules` |
