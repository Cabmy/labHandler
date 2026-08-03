---
name: essay
description: |
  Written argumentation assignments: argumentative essay, reading notes, course term paper, review/discussion questions.
  Pure textual argumentation: no runnable code, no experiment process, no screenshot evidence.
  Typical artifacts: .md / .docx / .pdf documents.
when_to_use: |
  Pick this when ANY of the following holds:
  - The assignment asks to "写一篇 / 论述 / 不少于 N 字 / 谈谈你的看法" (write an essay / discuss / no fewer than N words / share your view)
  - The assignment type is argumentative essay, reading notes, reading reflection, term paper, review, reading + reflection
  - deliverables are documents only (.md/.docx/.pdf), no code files, no experiment process
  Exclude:
  - Contains runnable code + unit tests -> coding
  - Contains experiment process / data collection / screenshot evidence + lab report requirement -> lab_report
---

# Essay Skill SOP

Guides the Coder agent (also acting as writer) to complete essay assignments: argument organization / citation norms / academic integrity.
All ground truth comes from the task context (intake_result + user_constraints + reading materials provided by the user);
fabricating non-existent references or inventing data is not allowed.
Detailed materials live in references/ (read on demand via load_skill_reference):
- `citation.md` — citation norm details (direct/indirect citation formats + examples)

## 1. Read the problem statement thoroughly

Extract: word-count range, theme keywords / key focus ("analyze" ≠ "introduce"), required sections, stance requirement
(neutral analysis or pick a side — determines whether rebuttal is needed), citation norms (minimum number of sources / APA / GB-7714).
When reading materials are PDF/DOCX, first call sandbox_convert_to_markdown to convert to markdown, then read.

## 2. Build the structure (outline before writing)

Do not start writing directly. First list an outline:
1. Introduction (why this topic + a 1-2 sentence hook)
2. Background / concept definition (define key terms, avoid ambiguity)
3. Arguments 1..N (each argument paired with 1-3 traceable pieces of evidence + causal/comparative/analogical reasoning)
4. (Optional) Opposing view / rebuttal
5. Conclusion (not just restating arguments; explain implications for practice / future research)
6. References (in the required format)

## 3. Writing style (per profile.preferences.writing_style)

- formality: low may be colloquial / medium standard academic Chinese / high leans toward thesis (passive, nominalized, more citations)
- avg_sentence_len: 18-20 readable, 25 average, 30+ shows depth
- Do not pad word count with filler: 3-5 sentences per paragraph, one core idea per sentence; do not pile up "firstly/secondly/furthermore"

## 4. Citation and academic integrity

- Direct citation: quotation marks + source, single item ≤ 30 characters; indirect citation marks `(author, year)` at sentence end (details in references/citation.md)
- The global academic-integrity constraint is already injected into the system prompt by the main graph (config/prompts.py:ACADEMIC_INTEGRITY_PROMPT);
  this skill adds: append a "tool usage note" section at the end noting AI assistance
- When there are no citable references, do not fabricate: state "based only on the problem statement + textbook X discussion", and note it in the Final Answer `待办：` line

## 5. Self-check checklist (cross-check item by item after writing)

- [ ] Word count within the problem-statement required range
- [ ] Sections complete (introduction / argumentation / conclusion / references — at least 4 items)
- [ ] Each argument has ≥1 traceable piece of evidence; citation format compliant
- [ ] Style matches the formality setting; no colloquialisms (unless formality=low)
- [ ] A "tool usage note" section at the end
- Any unmet item that cannot be fixed -> Final Answer honestly reports `step <id> needs_retry: <stuck point>`

## 6. Boundaries (each to its own)

- "Implement an algorithm / run an experiment" -> coding or lab_report; "analyze a theory / reading reflection / compare A and B (no implementation)" -> essay
- essay artifacts do not embed code blocks; explain algorithms with textual step descriptions
- Contradictory stance requirements (must be neutral yet must take a side): decide per the user's conversational supplementary intent; if necessary fall back to neutral analysis,
  and note the contradiction in the Final Answer `待办：` line
