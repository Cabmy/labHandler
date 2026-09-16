---
name: essay
description: |
  Written argumentation only: essay, reading notes, term paper, review.
  No runnable code, no experiment process. Artifacts: .md / .docx / .pdf.
when_to_use: |
  The work is 写一篇 / 论述 / 不少于 N 字 / 谈谈看法, deliverable is a document.
  Not this: code + tests → coding; experiment process + report → lab_report.
---

# Essay SOP

For Pro: plan, dispatch, and judge written work. Flash never sees this file — put what it needs in the assignment.

## SPEC

Extract word-count range, required sections, stance (neutral vs take a side), citation style
(APA / GB/T 7714 / none), and theme keywords. Do not invent sources or data.

## Dispatch

Outline before prose. Typical sequence (one Flash each, not parallel writers):
1. outline (intro / arguments / optional rebuttal / conclusion / references)
2. draft the body
3. citations + self-check

Do not split one essay across parallel workers. The assignment's `spec` must include
word-count, stance, section list, and citation rules — Flash cannot see SPEC.md's rationale.

## Writing

Honor `profile.preferences.writing_style` (formality, sentence length).
No filler to hit the word count: one idea per paragraph.
Citation details: `load_skill_reference("essay", "citation.md")`.
If there are no citable sources, say so in SUMMARY; do not fabricate.

End the artifact with a short "tool usage note" (AI assistance).

## Judge

Check: word count in range, required sections present, each argument has a traceable source,
stance matches SPEC, citation format holds, no fabricated references.
Missing evidence is fail, not "close enough".
