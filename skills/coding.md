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

本 SOP 指导 Coder agent 在 AIO Sandbox 容器内完成 coding 类作业的实现、测试、归档。
ground truth 全部从 task context（intake_result + user_constraints + profile）拿，不要凭空臆造题面。

## 1. 读懂题面（先做这一步，再写代码）

调 sandbox_convert_to_markdown 把 PDF/DOCX 实验指导转 markdown（如有），结合 README.md 抽：
- 函数签名、输入输出契约（参数类型 / 返回类型 / 异常）
- 性能要求（时间复杂度 / 空间复杂度 / 实测耗时上限）
- 禁用清单（不允许用某些标准库 / 第三方包）
- 边界要求（空输入 / 单元素 / 极值 / 非法输入是返回 -1 还是 raise）

把每条约束记到一个心理 checklist；后面的产物每一行都对照这个 checklist。

不确定的地方直接问 user 或调 archive_search 召回历史 lessons，不要猜。

## 2. 思考实现路径（在脑里先跑一遍）

不是写代码再调试。先在脑里走一遍流程：
- 主循环结构是什么
- 哪些是 corner case 需要单独处理（空数组 / 单元素 / 重复元素 / 越界）
- 时间复杂度怎么保证（如二分查找用 lo+(hi-lo)//2 而不是 (lo+hi)//2 防溢出）

把思考过程写到 SUMMARY.md 第 3 章「实现关键步骤」每一步前的 1-2 句话；不要直接跳到代码贴出来。
这是产物质量的核心区分点：让助教/面试官看到「为什么这么写」。

## 3. 在 sandbox 内实现

用 sandbox_str_replace_editor 创建 solution.py（不要用 host write_file 写到 workspace；
host fs_tools 是给读和补丁用的，主创作走容器）。

写完先 sandbox_execute_code 跑一行 import 确认语法 + 模块结构对：

```python
from solution import binary_search
print(binary_search([1,2,3], 2))
```

确认 import 通了再继续，不要堆完所有代码再发现 import 链条断了。

## 4. 写测试（pytest）

test_solution.py 至少 5 个用例：
- 正常场景（target 在中间 / 头 / 尾）
- 边界场景（空数组 / 单元素 / target 不在数组）
- 异常场景（输入未排序 / target 类型错——按题面约束行为决定）

跑 sandbox_execute_bash "pytest test_solution.py -v"。
exit_code != 0 时不要直接结束；回 step 2 修代码 / 修测试，直到全过。

## 5. 风格收尾（按 profile.coding_style）

- 如 type_hints=true：函数参数 + 返回值都加 type hints
- docstring 按 profile.coding_style.docstring 选 none / short / numpy 风格
- **编译型语言额外清理**（.cpp/.c/.java）：测试全过后调 sandbox_execute_bash 跑
  `rm -f *.o *.obj *.class` 以及编译出的可执行文件（同名无后缀或 `-o` 指定的名字）。
  compile 节点的产物清单（_ARTIFACT_EXTS 白名单）只收源码与测试，中间编译件留在
  workspace 只会让用户审阅时困惑。

## 6. 写 SUMMARY 时的「思考过程」段落要求

Summarizer 节点会基于 progress_log + tool_history 自动写 SUMMARY.md 7 章节，但
本 skill 期望产出在 SUMMARY 第 3 章「实现关键步骤」每一步前**附 1-2 句思考解释**：
- 不是「我做了什么」，而是「我为什么这么做、考虑过什么 alternative」
- 例如：「考虑过用 list.index() 但题面禁用，所以手写二分」
- 例如：「中点用 lo+(hi-lo)//2 防 lo+hi 溢出（虽然 Python int 不会溢出，但保持移植到 C 的习惯）」

如果产物会被截图给助教/面试官，留出截图位置（用户可在 SUMMARY.md 文末附实际截图）：
- 「（此处建议附 pytest 全过截图）」
- 「（此处建议附 sandbox_get_packages 输出，证明无禁用库）」

## 7. 学术诚信（独立约束，不依赖 essay/lab_report skill）

- 不复制网络答案；网络仅用于查 API 文档（需要时调 browser_navigate + browser_get_markdown）
- 引用任何外部代码片段（Stack Overflow / GitHub）必须在 SUMMARY.md「## 7. 后续待办」章节标注来源 URL
- 不在产物里出现别人的姓名 / 学号 / GitHub 用户名

## 何时停止

- verifier_runs 最后一次 verdict = pass → 主图自动 Compile + Summarize，本 skill 退出
- verifier verdict = fail 且 iteration < MAX_REPLAN_ITER → 主图自动回 Planner，本 skill 复用
- verifier verdict = fail 且 iteration ≥ MAX_REPLAN_ITER → 输出「部分完成」面板让用户接手

不要尝试硬循环修；MAX_REPLAN_ITER=2 是有意限制的（防失控）。

## 异常处理速查

| 失败模式 | 应对 |
|---|---|
| task_title / deliverables 缺 | 调 read_file 直接读 workspace/README.md 全文，自己再抽一遍约束 |
| 沙箱 8080 不可达 | 降级到 host `host_bash`（受 _safe_path 守护，cwd=workspace 自动锁），SUMMARY 第 7 章标注「沙箱不可达，已降级」 |
| pytest 反复 fail（≥3 次同一错） | 停止硬调，SUMMARY 第 6 章写下卡点 + suggested_fix，让 Verifier 标 fail 走 Replan |
| 题面约束冲突 | 不要硬满足所有；SUMMARY 第 7 章标「约束 X 与 Y 冲突，需用户决定优先级」 |
| host_bash 越界 PermissionError | 设计行为不是 bug；改用相对路径 + 沙箱内执行 |
| recursion_limit reached | 任务粒度太大，让 Planner 拆细；本 skill 期望 ≤6 ReAct iter |
