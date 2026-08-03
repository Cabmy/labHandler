---
name: lab_report
description: |
  Lab report assignments: contain experiment objective / principle / steps / results / analysis / conclusion, a 6+ section structure.
  Characteristics: includes an experiment process (doing the experiment), may contain code snippets / data / screenshot evidence; the TA grades by section completeness + reproducibility.
  Typical artifacts: lab report document (.md/.docx/.pdf) + possible code / data files.
when_to_use: |
  Pick this when ANY of the following holds:
  - The assignment asks to "写实验报告 / Lab N / 实验 N" (write a lab report), or explicitly requires submitting a report containing the experiment process
  - The assignment involves network security labs, OS labs, computer network labs, SEED Lab, packet capture analysis, or other lab courses
  - workspace contains documents like "实验指导.pdf / lab_*.md / lab_*.docx"
  - deliverables include a lab report + possible code / data files
  Exclude:
  - Pure algorithm implementation + unit tests (no lab report requirement) -> coding
  - Pure argumentation / reading reflection / argumentative essay (no experiment process) -> essay
---

# Lab Report Skill SOP

Guides the Coder agent to complete lab report assignments: experiment reproduction / data organization / report writing.
All ground truth comes from the task context (intake_result + original lab guidance + experiment environment provided by the user);
**fabricating experiment data never run / screenshots never captured / phenomena never observed is absolutely forbidden**.
Detailed materials live in references/ (read on demand via load_skill_reference):
- `writing_guide.md` — long examples of the experiment-process section writing style + environment-prerequisite samples + result table templates

## 1. Read the lab guidance thoroughly

Call sandbox_convert_to_markdown to convert PDF/DOCX lab guidance to markdown before reading; extract:
experiment objective (one sentence), experiment environment (OS/container/topology/tool versions), required task list (Task 1.1, 1.2, ...),
bonus tasks, submission requirements, grading rubric.
Expected artifact: **complete sections + every Task has a full "process + phenomenon + conclusion" chain** — none may be missing.

## 2. Required sections (Verifier Stage 1 hard metrics)

```
1. 实验目的            5. 实验结果（数据表 + 原始记录）
2. 实验原理            6. 结果分析与讨论（与预期对比 + 意外现象）
3. 实验环境            7. 结论
4. 实验步骤（按 Task 分小节）  8. 思考题（如实验指导附了）
```

The Verifier scans that at least these 5 keywords are all present: 实验目的 / 实验原理 / 实验步骤 / 实验结果 / 结论.

## 3. Writing points (details and long examples in references/writing_guide.md)

- **Show the thinking process**: before each step, 1-2 sentences explaining "why do it this way, what alternatives were considered" —
  not just piling commands + screenshots — this is the biggest difference between lab_report and coding/essay
- Use `（此处建议附 XX 截图）` placeholders for screenshot positions (the agent cannot produce screenshots; the user fills them in)
- Paste code/attack scripts inline within the corresponding step text (not dumped in an appendix at the end); one sentence before each code block stating what it does + parameter rationale
- When docker / system config must be changed to make it run, write a separate "environment prerequisites" section explaining why
- Use bold sparingly; put results into markdown tables where possible; discuss unexpected phenomena separately (this is a scoring point)

## 4. Citation and academic integrity

- The original lab guidance may be quoted directly as the analysis object; other sources (textbook/RFC/paper) use quotation marks + source, single item ≤ 30 characters
- Discussing ideas within the same group is fine; code/reports must not be copied from each other; do not expose others' student IDs/names/real IPs/credentials
- A "tool usage note" section at the end noting AI assistance (which sections AI helped generate, screenshots pending user supplement)

## 5. Self-check checklist

- [ ] The 5 key sections complete; experiment environment has OS/container/tool versions
- [ ] Every Task has a "process + phenomenon + conclusion" chain, with thinking explanations before each step
- [ ] Screenshot placeholders all placed; code pasted inline; data authentic
- [ ] Result analysis includes comparison with expectations + explanation of unexpected phenomena
- [ ] A "tool usage note" section at the end

## 6. Exception handling

- Experiment did not actually run through: **do not fabricate results** — in the corresponding Task section write "实测未跑通：错误现象 + 已尝试方法 + 卡点" (not run through: error phenomenon + methods tried + stuck point),
  Final Answer reports `step <id> needs_retry: <stuck point>`; after retries are exhausted it naturally hands to Verifier
- Experiment data has surprises (e.g. 100× slower than expected): discuss separately in the "analysis" section + hypothesize causes — scores better than flat data
- English SEED Lab: the report body uses Chinese; when quoting the original use English + a Chinese paraphrase
- Boundary: only "implement + test" -> coding; "do the experiment + write a report" -> this skill (the report is the main deliverable)
