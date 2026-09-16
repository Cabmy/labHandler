---
name: lab_report
description: |
  Lab report with experiment process: objective / principle / steps / results / analysis / conclusion.
  Artifacts: report document plus optional code or data.
when_to_use: |
  写实验报告 / Lab N / 实验指导, or a lab that must submit process + evidence.
  Not this: implement + tests only → coding; argumentation with no experiment → essay.
---

# Lab Report SOP

For Pro: plan, dispatch, and judge lab-report work. Flash never sees this file — put what it needs in the assignment.
**Never fabricate** runs, screenshots, or observations that did not happen.

## SPEC

Extract: one-sentence objective, environment (OS / container / topology / tool versions),
Task list, bonus, submission rules, rubric. Main deliverable is the report, not the helper code.

Required sections (all must appear):

```
1. 实验目的     5. 实验结果（数据表 + 原始记录）
2. 实验原理     6. 结果分析与讨论（与预期对比 + 意外现象）
3. 实验环境     7. 结论
4. 实验步骤     8. 思考题（指导书有则写）
```

## Dispatch

Typical sequence, one Flash at a time:
1. reproduce / collect real output (commands, numbers, errors)
2. write process sections (thinking → command → phenomenon per Task)
3. results table + analysis + conclusion

Writing style and table templates: `load_skill_reference("lab_report", "writing_guide.md")`.

Agent cannot capture screenshots — use `（此处建议附 XX 截图）` placeholders for the user.
Paste code inline at the step it belongs to, with one sentence of purpose before the block.
If a run did not succeed: write 实测未跑通（现象 + 已试方法 + 卡点）, do not invent a passing table.

## Integrity

Lab guidance may be quoted as the object of analysis. Other sources: quote + citation, ≤30 chars.
Do not expose others' student IDs, real IPs, or credentials.
End with a "tool usage note" (which sections AI drafted; screenshots pending).

## Judge

Fail if any of the five keywords is missing (实验目的 / 实验原理 / 实验步骤 / 实验结果 / 结论),
if a Task has no process→phenomenon→conclusion chain, or if results look invented.
Unexpected measurements belong in 分析, not a silent rewrite of the data.
