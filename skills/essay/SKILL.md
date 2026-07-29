---
name: essay
description: |
  文字论述类作业：议论文、读书笔记、课程小论文、综述论述题。
  纯文字论证，无可执行代码、无实验过程、无截图证据。
  典型产物：.md / .docx / .pdf 文档。
when_to_use: |
  以下任一成立时选用：
  - 作业要求"写一篇 / 论述 / 不少于 N 字 / 谈谈你的看法"
  - 作业类型为议论文、读书笔记、读后感、小论文、综述、阅读+思考
  - deliverables 仅为文档（.md/.docx/.pdf），无代码文件，无实验过程
  排除：
  - 含可执行代码 + 单元测试 → coding
  - 含实验过程 / 数据采集 / 截图证据 + 实验报告要求 → lab_report
---

# Essay Skill SOP

指导 Coder agent（兼写作）完成 essay 类作业的论证组织 / 引用规范 / 学术诚信。
ground truth 全部从 task context（intake_result + user_constraints + 用户提供的阅读材料）拿；
不允许凭空引用不存在的文献、不允许造数据。
详细材料在 references/（用 load_skill_reference 按需读取）：
- `citation.md` — 引用规范细则（直接/间接引用格式 + 示例）

## 1. 读懂题面

抽出：字数区间、主题词/题眼（"分析"≠"介绍"）、必要章节、立场要求
（中立分析 or 任选一方——决定是否需要驳论）、引用规范（篇数下限 / APA / GB-7714）。
阅读材料是 PDF/DOCX 时先调 sandbox_convert_to_markdown 转 markdown 再读。

## 2. 立结构（写之前先列大纲）

不要直接开写。先列提纲：
1. 引言（why this 题 + 1-2 句钩子）
2. 背景 / 概念界定（定义关键术语，避免歧义）
3. 论点 1..N（每个论点配 1-3 条可溯源论据 + 因果/对比/类比论证）
4. （可选）反方观点 / 驳论
5. 结论（不只重复论点；说明对实践/后续研究的启示）
6. 参考文献（按要求格式）

## 3. 写作风格（按 profile.preferences.writing_style）

- formality：low 可口语 / medium 标准学术中文 / high 偏论文（被动、名词化、引文多）
- avg_sentence_len：18-20 易读，25 一般，30+ 显深度
- 不为凑字数注水：每段 3-5 句，每句 1 个核心意思；不堆砌"首先/其次/再者"

## 4. 引用与学术诚信

- 直接引用：引号 + 来源，单条 ≤ 30 字；间接引用句末标 `(作者, 年份)`（细则见 references/citation.md）
- 全局学术诚信约束已由主图注入 system prompt（config/prompts.py:ACADEMIC_INTEGRITY_PROMPT），
  本 skill 补充：末尾加「使用工具说明」段标注 AI 辅助
- 没有可引用文献时不要编造：写明「仅基于题面 + 教材 X 论述」，并在 Final Answer `待办：` 行标注

## 5. 自检 checklist（写完逐条对照）

- [ ] 字数在题面要求区间内
- [ ] 章节齐（引言 / 论证 / 结论 / 参考文献至少 4 项）
- [ ] 每个论点 ≥1 条可溯源论据；引用格式合规
- [ ] 风格符合 formality 设定；无口语（除非 formality=low）
- [ ] 末尾有「使用工具说明」段
- 有未达项修不动 → Final Answer 如实报 `step <id> needs_retry: <卡点>`

## 6. 边界（各司其职）

- "实现某算法 / 跑实验" → coding 或 lab_report；"分析某理论 / 读后感 / 对比 A B（无需实现）" → essay
- essay 产物不塞代码块；解释算法用文字描述步骤
- 立场矛盾要求（要中立又要表态）：按用户对话补充意图定，必要时 fall back 中立分析，
  并在 Final Answer `待办：` 行标注矛盾
