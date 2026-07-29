---
name: coding
description: |
  编程类作业：实现算法、数据结构、工程脚本，产出可执行代码 + pytest 单元测试。
  典型产物：.py / .cpp / .java 源文件 + test_*.py 测试文件。
when_to_use: |
  以下任一成立时选用：
  - 作业要求"实现 / 编写程序 / 写代码 / 编程"且需要可执行产物
  - 作业含编程题（如 LeetCode 风格）、排序算法、数据结构等
  - deliverables 包含源代码文件（.py/.cpp/.java/.js/.ts）或测试文件（test_*.py）
  排除：
  - 含实验过程 + 实验报告要求 → lab_report
  - 纯论述 / 读后感 / 议论文 → essay
  - 纯算法分析论述题（无需实现代码）→ essay 或 other
---

# Coding Skill SOP

指导 Coder agent 在 AIO Sandbox 容器内完成 coding 类作业。
ground truth 全部从 task context（intake_result + user_constraints + profile）拿，不凭空臆造题面。
详细材料在 references/（用 load_skill_reference 按需读取）：
- `testing.md` — 测试用例设计指南（正常/边界/异常三类 + 示例）
- `pitfalls.md` — 异常处理速查表（沙箱不可达 / pytest 反复失败等）

## 1. 读懂题面

调 sandbox_convert_to_markdown 把 PDF/DOCX 实验指导转 markdown（如有），结合 README.md 抽：
- 函数签名、输入输出契约（参数类型 / 返回类型 / 异常）
- 性能要求（时间/空间复杂度、实测耗时上限）
- 禁用清单（不允许用某些标准库 / 第三方包）
- 边界要求（空输入 / 单元素 / 极值 / 非法输入是返回 -1 还是 raise）

每条约束记进心理 checklist；后面的产物逐行对照。不确定的地方问 user 或调 archive_search 召回历史 lessons，不要猜。

## 2. 先想清楚再写

先在脑里走一遍：主循环结构、哪些 corner case 要单独处理、复杂度怎么保证
（如二分中点用 `lo+(hi-lo)//2` 防溢出）。关键取舍写进 Final Answer 的
`决策：` 行——Summarizer 会把它蒸馏进 SUMMARY「我做了什么」，这是产物质量的核心区分点。

## 3. 在 sandbox 内实现

- 用 sandbox_str_replace_editor 创建源文件（主创作走容器；host fs_tools 用于读与补丁）
- **文件命名按题面；题面没指定时按算法/主题/题号命名**（如 `zuc.py` / `binary_search.py` /
  `hw4_q1.py`），禁止 `solution.py` / `main.py` 这类通用名（与全局命名规则一致）
- 写完先跑一行 import 确认语法 + 模块结构，再继续堆代码

## 4. 写测试（pytest）

测试文件与源文件同主题（`test_zuc.py` 配 `zuc.py`），至少 5 个用例覆盖
正常 / 边界 / 异常三类（设计细节见 references/testing.md）。
跑 `sandbox_execute_bash "pytest test_<name>.py -v"`；exit_code != 0 时回去修代码/测试，
直到全过；修不动时如实报 `step <id> needs_retry: <卡点>`，不要假装 done。

## 5. 风格收尾（按 profile.coding_style）

- type_hints=true 时函数参数 + 返回值都加 type hints；docstring 按 profile 选 none/short/numpy
- 编译型语言（.cpp/.c/.java）测试全过后清理中间件：`rm -f *.o *.obj *.class` 及编译出的可执行文件

## 6. 学术诚信（独立约束）

- 不复制网络答案；网络仅用于查 API 文档
- 引用外部代码片段（Stack Overflow / GitHub）须在 Final Answer `待办：` 行标注来源 URL，
  让 Summarizer 写进 SUMMARY 待办
- 不在产物里出现别人的姓名 / 学号 / GitHub 用户名

## 何时停止

- verifier verdict = pass → 主图自动 Compile + Summarize，本 skill 退出
- 本步修不动 → Final Answer 报 needs_retry（主图有界重试，上限 MAX_STEP_RETRY）
- verdict = fail 且 iteration ≥ MAX_REPLAN_ITER → 主图输出「部分完成」面板让用户接手

不要硬循环修；重试与 Replan 上限是有意限制的（防失控）。
