# hwHandler

> 中文大学作业 / Lab 自动化 AI agent。把作业材料丢进 `workspace/`，agent 在沙箱内完成「实现 → 测试 → 校验 → 总结」的闭环。

## 环境要求

- Python 3.11
- Docker（用于沙箱容器）
- Paratera API Key（DeepSeek-V4-Pro + GLM-Embedding-3）

## 安装

```bash
conda create -n hwhandler python=3.11 -y && conda activate hwhandler
pip install -r requirements.txt
cp config/.env.example config/.env   # 编辑填入 PARATERA_API_KEY
```

## 启动

```bash
python cli.py
```

> 首次启动会自动拉起 AIO Sandbox 容器（拉镜像约 2.29GB）。设 `HW_AUTOSTART_SANDBOX=false` 可禁用自动启动。
