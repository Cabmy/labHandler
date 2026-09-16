---
name: coding
description: |
  Primary deliverable is runnable source (functions, scripts, CLIs).
  The harness gate is Pro write_acceptance, not a worker writing test_*.py.
when_to_use: |
  Primary artifact is code: 实现 / 编程 / 力扣 / 算法 / 脚本.
  Not this: the graded hand-in is an 实验报告 → lab_report; 论文/论述 → essay.
---

# Coding SOP

For Pro: plan, dispatch, and judge coding work. Flash never sees this file — put what it needs in the assignment.

## SPEC

- Pin interfaces (function / class / CLI / file names and signatures). Acceptance imports them verbatim.
- Name files by topic or problem id (`binary_search.py`, `hw4_q1.py`). Never `solution.py` / `main.py`.
  Paths are workspace-root relative; never nest under `workspace/`.
- Do not invent constraints absent from the files you read or the user request.

## Dispatch

One problem or one shippable file = one assignment. A single-function homework is one
milestone and one dispatch — do not slice it into design / implement / edge cases / polish.
Split only at independent problems or files; a whole multi-question set in one assignment
will be half-done. Do not dispatch Flash to read MATERIALS.md — put constraints in the
assignment.

Each assignment must be self-contained: exact names, paths, forbidden libraries, boundary behavior.
If testable, declare `interfaces` and write acceptance via `write_acceptance`
(`task_id` = that assignment `id`).

## Tests

作业材料里的「单元测试 / 样例 / 能跑通 / 自写测试」是你的 `write_acceptance` 门禁，不是 Flash
任务。不要把写测试派成第二个 worker，不要把「提供单元测试」写进 step_goal。
Only if the graded hand-in names a workspace `test_*.py` may Flash write that path;
you still write the gate.

Write a gate only when the target is quantifiable and the artifact can be checked by code.
Otherwise `testable=false` — a missing gate is honest.

Test file shares the topic (`test_zuc.py` with `zuc.py`). Cover normal / boundary / exception.
Details: `load_skill_reference("coding", "testing.md")`.
Stuck on pytest or sandbox: `load_skill_reference("coding", "pitfalls.md")`.

## Style and integrity

- Honor `profile.coding_style` (type hints, docstring).
- Internet is for API docs, not answers. Note any copied snippet URL in SUMMARY.
- No other people's names, student IDs, or GitHub handles in the artifact.

## Judge

Pass means interfaces exist and the gate is green. "Looks done" is not enough.
If the worker renamed an interface, fail or `test_invalid` — do not patch the contract silently.
