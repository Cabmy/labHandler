"""Verifier agent - 硬指标 + LLM 语义覆盖

输出 verifier_run（追加到 HwState.verifier_runs）：
  {
    "verdict":          "pass" | "fail",
    "stage1_failures":  list[str],   # 硬指标失败项（致命）
    "stage2_warnings":  list[str],   # 语义覆盖告警（缺哪条约束）
    "coverage":         dict,        # {covered: [...], missing: [...]}（LLM 输出）
    "suggested_fix":    str,         # 一行式修复建议
  }

verdict 规则（二元化）：
- 任何 stage1_failures 或 stage2_warnings → fail
- 都没问题 → pass

设计要点：
1. 阶段 1 硬指标按 intake.type 分支：
   - coding: 检查 intake.deliverables 全部存在 + sandbox 跑 pytest（如有 test_*.py）
   - lab_report/essay: 检查必备章节（lab_report 7 章节；essay 不强制）
2. 阶段 2 LLM 语义覆盖：把 intake.constraints + user_constraints + artifacts 内容喂 LLM
   输出 coverage map（每条约束 covered/missing + 证据指针）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent, load_profile
from orchestrator.state import HwState
from prompts import VERIFIER_COVERAGE_SYSTEM, extract_result

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()

# lab_report 必备章节关键字
_LAB_REQUIRED_SECTIONS = ["实验目的", "实验原理", "实验步骤", "实验结果", "结论"]

# 已知文件后缀（用于判 deliverable 是文件名还是描述性条目）
_FILENAME_EXTS = (
    ".py", ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml",
    ".pdf", ".docx", ".pptx", ".cpp", ".c", ".h", ".java",
    ".js", ".ts", ".html", ".css", ".sql", ".sh",
)


def _looks_like_filename(s: str) -> bool:
    """deliverable 字符串是否「明显是文件名」（按已知后缀判）。

    True  → 由阶段 1 _check_files_exist 严格查存在
    False → 当成"描述性交付物"，转交阶段 2 LLM 语义覆盖判官
    """
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    # 含中英文括号 / 空格 / 中文逗号顿号 → 多半是描述（除非是含 `/` 的相对路径且仍带后缀）
    has_descriptive_chars = any(ch in s for ch in "（）()，、 ")
    if has_descriptive_chars and not ("/" in s and s.lower().endswith(_FILENAME_EXTS)):
        return False
    return s.lower().endswith(_FILENAME_EXTS)


def _split_deliverables(deliverables: list[str]) -> tuple[list[str], list[str]]:
    """把 deliverables 拆成 (文件名类, 描述类) 两组。"""
    files: list[str] = []
    descs: list[str] = []
    for d in deliverables:
        (files if _looks_like_filename(d) else descs).append(d)
    return files, descs


# ─── 阶段 1：硬指标 ───────────────────────────────────────────────


def _check_files_exist(deliverables: list[str]) -> list[str]:
    """检查"文件名类"交付物是否在 workspace 内存在。

    描述性 deliverable（如「源代码文件（含 ZUC_Init...）」）跳过严格匹配，
    交给阶段 2 LLM 语义覆盖判官判（避免硬指标误报缺失）。
    """
    failures: list[str] = []
    file_names, _descs = _split_deliverables(deliverables)
    for d in file_names:
        # deliverables 可能是文件名或相对路径；都按 workspace 内查
        path = WORKSPACE_DIR / d
        if not path.exists():
            # 尝试 rglob 模糊匹配（用户可能把文件丢在子目录）
            matches = list(WORKSPACE_DIR.rglob(d))
            if not matches:
                failures.append(f"交付物缺失：{d}")
    return failures


def _check_lab_sections(deliverables: list[str]) -> list[str]:
    """lab_report 类：扫描可能的 lab_report.md/docx，检查必备章节"""
    failures: list[str] = []
    candidates = [
        WORKSPACE_DIR / d for d in deliverables
        if d.lower().endswith((".md", ".markdown"))
    ]
    if not candidates:
        candidates = list(WORKSPACE_DIR.rglob("*report*.md")) + list(
            WORKSPACE_DIR.rglob("*实验*.md")
        )
    if not candidates:
        return failures  # 没文件时 _check_files_exist 已经报过
    text = ""
    for p in candidates:
        try:
            text += "\n" + p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    for sec in _LAB_REQUIRED_SECTIONS:
        if sec not in text:
            failures.append(f"lab_report 缺章节：{sec}")
    return failures


def _run_pytest_in_workspace(timeout: int = 60) -> tuple[bool, str]:
    """在 workspace 跑 pytest（host_bash 受 fs_tools 边界保护）"""
    try:
        from tools.fs_tools import host_bash
        # 用 host_bash 跑，自动 cwd=WORKSPACE_DIR
        out = host_bash.invoke({"cmd": "pytest -q --tb=short", "timeout": timeout})
        # exit=0 视为通过
        if "[exit=0]" in out:
            return True, out[-500:]
        return False, out[-1000:]
    except PermissionError as e:
        return False, f"host_bash 越界：{e}"
    except Exception as e:
        return False, f"pytest 调用异常：{type(e).__name__}: {e}"


def _stage1_hard_checks(state: HwState) -> tuple[list[str], dict[str, Any]]:
    """硬指标检查。Returns: (failures, evidence_dict)"""
    intake = state.get("intake_result") or {}
    ttype = (intake.get("type") or "other").lower()
    deliv = intake.get("deliverables") or []
    failures: list[str] = []
    evidence: dict[str, Any] = {}

    # 1) 交付物文件存在
    failures.extend(_check_files_exist(deliv))
    evidence["deliverables_checked"] = deliv

    # 2) coding 类：跑 pytest（如有 test_*.py）
    if ttype == "coding":
        has_test = any(
            (WORKSPACE_DIR / d).exists() and "test" in d.lower() for d in deliv
        ) or bool(list(WORKSPACE_DIR.rglob("test_*.py"))) or bool(list(WORKSPACE_DIR.rglob("*_test.py")))
        if has_test:
            ok, out = _run_pytest_in_workspace()
            evidence["pytest_output_tail"] = out
            if not ok:
                failures.append("pytest 未通过（详见 evidence.pytest_output_tail）")
        else:
            evidence["pytest_skipped_reason"] = "未找到 test_*.py / *_test.py"

    # 3) lab_report 类：必备章节
    if ttype == "lab_report":
        failures.extend(_check_lab_sections(deliv))

    return failures, evidence


# ─── 阶段 2：LLM 语义覆盖 ────────────────────────────────────────


_COVERAGE_SYSTEM = VERIFIER_COVERAGE_SYSTEM  # 局部引用名兼容


def _gather_artifacts_text(state: HwState, max_chars: int = 6000) -> str:
    """把 artifacts + workspace 主要文本文件拼起来给 LLM"""
    chunks: list[str] = []
    total = 0
    seen: set[Path] = set()

    # 优先取 artifacts 列表里登记的文件
    for art in state.get("artifacts") or []:
        p = WORKSPACE_DIR / str(art.get("path", ""))
        if p.exists() and p.is_file() and p.suffix.lower() in {
            ".py", ".md", ".txt", ".cpp", ".c", ".h", ".java"
        }:
            seen.add(p)

    # 兜底：扫 workspace 文本类文件（限文件名以非"."开头）
    for p in WORKSPACE_DIR.rglob("*"):
        if p in seen:
            continue
        if not p.is_file():
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(WORKSPACE_DIR).parts):
            continue
        if p.suffix.lower() in {".py", ".md", ".txt", ".cpp", ".c", ".h", ".java"}:
            seen.add(p)

    for p in sorted(seen):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = p.relative_to(WORKSPACE_DIR)
        chunks.append(f"### {rel}\n{text}")
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n---\n\n".join(chunks)[:max_chars]


def _parse_json(content: str) -> dict[str, Any]:
    """从 <result> 抽 JSON；找不到 fallback 老路径"""
    text = extract_result(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        l, r = text.find("{"), text.rfind("}")
        if l >= 0 and r > l:
            return json.loads(text[l : r + 1])
        raise


def _stage2_llm_coverage(state: HwState, stage1_failures: list[str]) -> dict[str, Any]:
    intake = state.get("intake_result") or {}
    constraints = list(intake.get("constraints") or [])
    user_cons = list(state.get("user_constraints") or [])

    # 描述性 deliverables（非文件名）转交阶段 2：加 [交付物] 前缀混入约束列表，
    # 让 LLM 判官按"workspace 里有没有满足这个产物的描述"判覆盖；
    # 文件名类 deliverable 已在阶段 1 严格查存在，此处不重复判。
    deliverables = list(intake.get("deliverables") or [])
    _, desc_deliverables = _split_deliverables(deliverables)
    deliv_cons = [f"[交付物] {d}" for d in desc_deliverables]

    # 用户长期偏好规则（profile.preferences.style_rules，由 /remember 累积）
    # 进硬审计清单：让 verifier 在 covered/missing 列表里逐条对账。
    # identity（姓名/学号）不进——只通过 inject_for_agent 注入到 system prompt，
    # 是否需要写进产物完全看题面/用户当轮指令。
    prof = load_profile() or {}
    profile_rules = ((prof.get("preferences") or {}).get("style_rules") or [])
    profile_cons = [f"[长期规则] {str(r).strip()}" for r in profile_rules if str(r).strip()]

    all_cons = constraints + user_cons + deliv_cons + profile_cons

    if not all_cons:
        return {"covered": [], "missing": [], "suggested_fix": ""}

    artifacts_text = _gather_artifacts_text(state)
    if not artifacts_text:
        return {
            "covered": [],
            "missing": [{"constraint": c, "reason": "workspace 无可读产物"} for c in all_cons],
            "suggested_fix": "先产出文件再进 Verifier",
        }

    stage1_block = (
        "## 阶段 1 硬指标结果（事实，不要乐观假设其反面）\n"
        + (
            "\n".join(f"- {f}" for f in stage1_failures)
            if stage1_failures
            else "- 全部通过"
        )
        + "\n\n"
    )

    # 历史相似任务的教训（reference block，不进硬清单——让 LLM 判断本次是否适用）
    hist_block = ""
    try:
        from memory import search_archive

        hist_query = (
            intake.get("title")
            or state.get("question")
            or " ".join(constraints[:3])
            or ""
        )
        hist = search_archive(hist_query, limit=2) if hist_query else []
        hist_lines: list[str] = []
        for h in hist:
            lesson = (h.get("lessons") or "").strip()
            if lesson:
                hist_lines.append(
                    f"- [{h.get('task_type','')}] {h.get('task_title','')}：{lesson[:300]}"
                )
        if hist_lines:
            hist_block = (
                "## 历史相似任务的教训（参考；如本次产物明显违反某条教训，"
                "请在 missing 中加 [历史教训] 前缀的条目）\n"
                + "\n".join(hist_lines)
                + "\n\n"
            )
    except Exception:
        pass

    user_msg = (
        stage1_block
        + "## 约束条目（题面 + 用户补充 + 长期规则）\n"
        + "\n".join(f"- {c}" for c in all_cons)
        + "\n\n"
        + hist_block
        + "## 产物内容\n\n"
        + artifacts_text
    )
    llm = get_llm()
    try:
        resp = llm.invoke(
            [SystemMessage(content=inject_for_agent("verifier", _COVERAGE_SYSTEM)), HumanMessage(content=user_msg)]
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = _parse_json(content)
    except Exception as e:
        return {
            "covered": [],
            "missing": [],
            "suggested_fix": f"[语义覆盖 LLM 失败：{type(e).__name__}]",
            "_llm_error": str(e),
        }

    return {
        "covered": list(data.get("covered") or []),
        "missing": list(data.get("missing") or []),
        "suggested_fix": str(data.get("suggested_fix") or ""),
    }


# ─── 主入口 ───────────────────────────────────────────────────────


def run_verifier(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口。

    Returns: {verifier_runs: [<新增运行>], progress_log: [...]}
    """
    # Stage 1
    stage1_failures, evidence = _stage1_hard_checks(state)

    # Stage 2
    coverage = _stage2_llm_coverage(state, stage1_failures)
    stage2_warnings = [
        f"未覆盖约束：{m.get('constraint')} （{m.get('reason','')}）"
        for m in coverage.get("missing", [])
    ]

    # Verdict（二元化）
    if stage1_failures or stage2_warnings:
        verdict = "fail"
    else:
        verdict = "pass"

    suggested = coverage.get("suggested_fix") or ""
    if stage1_failures and not suggested:
        suggested = stage1_failures[0]

    run = {
        "verdict": verdict,
        "stage1_failures": stage1_failures,
        "stage2_warnings": stage2_warnings,
        "coverage": coverage,
        "evidence": evidence,
        "suggested_fix": suggested,
    }

    return {
        "verifier_runs": [run],
        "progress_log": [
            {
                "node": "verifier",
                "verdict": verdict,
                "n_failures": len(stage1_failures),
                "n_warnings": len(stage2_warnings),
            }
        ],
    }
