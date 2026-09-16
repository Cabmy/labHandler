---
name: coding
description: |
  Runnable code plus tests. Artifacts: source files and test_*.py.
when_to_use: |
  The work needs a program (实现 / 写代码 / 编程, algorithms, scripts).
  Not this: experiment + lab report → lab_report; pure argumentation → essay.
---

# Coding SOP

For Pro: plan, dispatch, and judge coding work. Flash never sees this file — put what it needs in the assignment.

## SPEC

- Pin interfaces (function / class / CLI / file names and signatures). Acceptance imports them verbatim.
- Name files by topic or problem id (`binary_search.py`, `hw4_q1.py`). Never `solution.py` / `main.py`.
  Paths are workspace-root relative; never nest under `workspace/`.
- Do not invent constraints absent from the files you read or the user request.

## Dispatch

Slice so one Flash finishes one assignment: read constraints → implement one unit → run tests.
A whole homework in one assignment will be half-done and marked success.

Each assignment must be self-contained: exact names, paths, forbidden libraries, boundary behavior.
If testable, declare `interfaces` and write acceptance via `write_acceptance`.

## Tests

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
