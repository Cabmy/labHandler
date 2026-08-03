---
name: coding
description: |
  Programming assignments: implement algorithms, data structures, engineering scripts; produce runnable code + pytest unit tests.
  Typical artifacts: .py / .cpp / .java source files + test_*.py test files.
when_to_use: |
  Pick this when ANY of the following holds:
  - The assignment asks to "实现 / 编写程序 / 写代码 / 编程" (implement / write a program / write code) and needs a runnable artifact
  - The assignment contains programming problems (e.g. LeetCode style), sorting algorithms, data structures, etc.
  - deliverables include source code files (.py/.cpp/.java/.js/.ts) or test files (test_*.py)
  Exclude:
  - Contains an experiment process + lab report requirement -> lab_report
  - Pure argumentation / reading reflection / argumentative essay -> essay
  - Pure algorithm-analysis discussion (no code implementation required) -> essay or other
---

# Coding Skill SOP

Guides the Coder agent to complete coding assignments inside the AIO Sandbox container.
All ground truth comes from the task context (intake_result + user_constraints + profile); never fabricate the problem statement.
Detailed materials live in references/ (read on demand via load_skill_reference):
- `testing.md` — test case design guide (normal / boundary / exception categories + examples)
- `pitfalls.md` — exception handling cheat sheet (sandbox unreachable / pytest repeatedly failing, etc.)

## 1. Read the problem statement thoroughly

Call sandbox_convert_to_markdown to convert PDF/DOCX lab guidance to markdown (if any), then extract from README.md:
- Function signatures, input/output contracts (parameter types / return types / exceptions)
- Performance requirements (time/space complexity, measured runtime upper bound)
- Forbidden list (certain stdlib / third-party packages not allowed)
- Boundary requirements (empty input / single element / extreme values / illegal input -> return -1 or raise)

Record every constraint into a mental checklist; later cross-check artifacts line by line. When unsure, ask the user or call archive_search to recall historical lessons — do not guess.

## 2. Think it through before writing

First walk through it mentally: main loop structure, which corner cases need separate handling, how to guarantee complexity
(e.g. binary search midpoint uses `lo+(hi-lo)//2` to prevent overflow). Write key trade-offs into the Final Answer's
`决策：` line — the Summarizer distills it into the SUMMARY "what I did" section; this is the core quality differentiator of the artifact.

## 3. Implement inside the sandbox

- Use sandbox_str_replace_editor to create source files (main authoring goes through the container; host fs_tools are for reading and patching)
- **Name files per the problem statement; when unspecified, name by algorithm/topic/problem number** (e.g. `zuc.py` / `binary_search.py` /
  `hw4_q1.py`); generic names like `solution.py` / `main.py` are forbidden (consistent with the global naming rule)
- After writing, first run a one-line import to confirm syntax + module structure, then keep adding code

## 4. Write tests (pytest)

The test file shares the source file's topic (`test_zuc.py` pairs with `zuc.py`); at least 5 cases covering the
normal / boundary / exception categories (design details in references/testing.md).
Run `sandbox_execute_bash "pytest test_<name>.py -v"`; when exit_code != 0 go back to fix code/tests
until all pass; when truly stuck, honestly report `step <id> needs_retry: <stuck point>` — do not pretend done.

## 5. Style wrap-up (per profile.coding_style)

- When type_hints=true, add type hints to function parameters + return values; docstring follows profile choice none/short/numpy
- For compiled languages (.cpp/.c/.java), after all tests pass clean up intermediates: `rm -f *.o *.obj *.class` plus compiled executables

## 6. Academic integrity (independent constraint)

- Do not copy answers from the internet; the internet is only for looking up API docs
- Referenced external code snippets (Stack Overflow / GitHub) must have their source URL noted in the Final Answer `待办：` line,
  so the Summarizer writes it into the SUMMARY todo
- No other people's names / student IDs / GitHub usernames may appear in the artifact

## When to stop

- verifier verdict = pass -> the main graph automatically Compiles + Summarizes, this skill exits
- This step cannot be fixed -> Final Answer reports needs_retry (the main graph retries with a bound, upper limit MAX_STEP_RETRY)
- verdict = fail and iteration ≥ MAX_REPLAN_ITER -> the main graph outputs a "partially complete" panel for the user to take over

Do not hard-loop on fixes; the retry and Replan limits are intentional (to prevent runaway).
