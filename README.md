# labHandler

> 中文大学 Lab 自动化 AI agent。把作业材料丢进 `workspace/`，agent 在沙箱内完成「规划 → 编码 → 验证 → 总结」闭环，产物和 `SUMMARY.md` 写回 `workspace/`。

## 核心能力

- **Plan-and-Execute 主图**（LangGraph）：intake 解析题面 → planner 拆步 → coder 单步 ReAct 执行（有界重试）→ verifier 两阶段校验（硬指标 + LLM 语义覆盖）→ fail 自动 Replan → summarizer 双轨总结
- **两层安全边界**：host 端命令白名单 + 路径守护 + 实时审计；重量操作（代码执行/浏览器/PDF 解析）走 MCP 进 Docker 沙箱
- **知识沉淀闭环**：每次任务蒸馏 lesson/strategy/pattern 卡片 → SQLite + Chroma + BM25（RRF 混合检索）→ 反哺后续任务的规划；`/dream` 离线治理合并去重
- **会话级隔离**：`/done` 归档 → 清场 → 重建沙箱 → 复位会话（逻辑等效进程重启，进程常驻），CLI 与 Web 共用同一复位入口
- **skills 渐进披露**：SOP 拼入提示词，详细材料/脚本按需加载；`/edit_skill` 自然语言编辑（可学习用户文风）

评估：12 题基准（coding/essay/lab_report 各 4 题）完整链路 **12/12 pass**；消融显示 Replan 贡献 +33pp、Verifier 语义判官贡献 +8pp 且省约一半 token。

## 环境要求

- Python 3.11
- Docker（用于沙箱容器）
- Paratera API Key（DeepSeek-V4-Pro + GLM-Embedding-3）

## 安装

```bash
conda create -n labhandler python=3.11 -y && conda activate labhandler
pip install -r requirements.txt
cp config/.env.example config/.env   # 编辑填入 PARATERA_API_KEY
```

## 启动

终端版：
```bash
python cli.py
```

本地 Web 界面（上传材料 / SSE 实时进度 / skill 编辑 / 偏好规则，默认 127.0.0.1:8000）：
```bash
python -m server
```

> 首次启动会自动拉起 AIO Sandbox 容器（拉镜像约 2.29GB）。设 `LAB_AUTOSTART_SANDBOX=false` 可禁用自动启动。

## 常用命令（CLI REPL）

| 命令 | 作用 |
|---|---|
| 直接输入任务 | 跑主图；后续输入作为修订约束累加 |
| `/done` | 归档知识卡片 → 清场 → 重建沙箱 → 开启新会话 |
| `/dream` | 离线治理归档卡片（LLM 合并/去重/淘汰） |
| `/edit_skill <skill> <指令>` | 自然语言编辑现有 skill（diff 确认后落盘） |
| `/remember <规则>` | 追加长期偏好规则（注入各 agent 并被 Verifier 对账） |
| `/skills` `/profile` `/help` `/quit` | 列 skills / 看画像 / 帮助 / 退出 |

