"""Verifier agent - 负责作业产物的质量把控，结合硬性指标检查与 LLM 语义审计。

该节点通过两阶段验证确保作业符合所有要求，并根据失败原因提供修复建议。

验证流程：
1. 阶段 1：硬性指标检查 (Rule-based)
   - Coding 类：核对交付物文件是否存在，并自动执行单元测试 (pytest) 获取运行结果。
   - 报告类：针对 lab_report 检查是否包含必要的实验章节。
2. 阶段 2：语义覆盖审计 (LLM-based)
   - 提取 workspace 中所有相关产物的文本内容。
   - 将题面约束、用户补充指令及长期规则喂给 LLM，判断产物是否在逻辑和内容上完全覆盖了这些要求。
   - 处理指令冲突：遵循“后令覆盖前令”的语义规则。

输出 verifier_run 结构：
  {
    "verdict": "pass" | "fail",     # 最终判定结论
    "stage1_failures": list[str],    # 硬指标缺失（如文件未找到、测试未通过）
    "stage2_warnings": list[str],    # 语义缺失（如某项功能逻辑未实现）
    "coverage": dict,                # 详细的约束覆盖地图
    "suggested_fix": str,            # 针对性的修复建议，引导 Planner/Coder 修正错误
  }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from memory.profile import inject_for_agent, load_profile
from orchestrator.state import HwState
from config.prompts import VERIFIER_COVERAGE_SYSTEM, parse_result_json
from config.runtime import get_settings

WORKSPACE_DIR: Path = get_settings().workspace_dir

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

    路径规整：剥掉常见的 "workspace/" 前缀（intake LLM 经常从题面里直抄
    "workspace/x.py" 这种带前缀的写法），再做存在检查；rglob 兜底用 basename，
    避免 pattern 含路径分隔符不匹配。

    描述性 deliverable（如「源代码文件（含 ZUC_Init...）」）跳过严格匹配，
    交给阶段 2 LLM 语义覆盖判官判（避免硬指标误报缺失）。
    """
    failures: list[str] = []
    file_names, _ = _split_deliverables(deliverables)
    for d in file_names:
        # 剥前缀: "workspace/x" / "./workspace/x" / "/workspace/x" → "x"
        d_norm = d
        for prefix in ("workspace/", "./workspace/", "/workspace/"):
            if d_norm.startswith(prefix):
                d_norm = d_norm[len(prefix):]
                break
        path = WORKSPACE_DIR / d_norm
        if path.exists():
            continue
        # rglob 兜底用 basename（pattern 含 "/" 时 rglob 不匹配）
        matches = list(WORKSPACE_DIR.rglob(Path(d_norm).name))
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


def _classify_pytest_failure(output: str) -> str:
    """从 pytest 输出中分类失败原因。

    返回分类标签，用于指导 Replan 方向：
    - "IMPORT_ERROR": 测试文件导入/编译错误 → 修复测试文件
    - "SYNTAX_ERROR": 语法错误 → 修复测试文件或源码
    - "TEST_FAILURE": 断言失败 → 源码有 bug，修复实现
    - "NO_TESTS":     pytest 未收集到任何测试 → Coder 漏写了测试
    - "UNKNOWN":      无法分类
    - "":             未失败
    """
    if not output:
        return ""
    for kw in ("ImportError", "ModuleNotFoundError", "Error while importing"):
        if kw in output:
            return "IMPORT_ERROR"
    for kw in ("SyntaxError", "IndentationError"):
        if kw in output:
            return "SYNTAX_ERROR"
    if "FAILED" in output and ("AssertionError" in output or " assert " in output):
        return "TEST_FAILURE"
    if "no tests ran" in output or "collected 0 items" in output:
        return "NO_TESTS"
    return "UNKNOWN"


_FAILURE_TYPE_HINTS = {
    "IMPORT_ERROR": "测试文件导入/编译错误，请检查测试文件的 import 语句和依赖",
    "SYNTAX_ERROR": "文件存在语法错误，请检查并修复",
    "TEST_FAILURE": "测试断言未通过，被测代码存在 bug，请检查实现逻辑",
    "NO_TESTS":     "pytest 未找到测试用例，请确认测试文件命名和内容",
    "UNKNOWN":      "请查看 pytest 输出详细信息",
}


def _run_pytest_in_workspace(timeout: int = 60) -> tuple[bool, str, str]:
    """在 workspace 跑 pytest（host_bash 受 fs_tools 边界保护）
    Returns: (passed, output_tail, failure_type)
    """
    try:
        from tools.fs_tools import host_bash
        # 用 host_bash 跑，自动 cwd=WORKSPACE_DIR
        # host_bash 越界不再 raise，而是返回 "[ERROR/PermissionError] ..." 字符串，
        # 这里靠下面的 "[exit=0]" not in out 自然兜住（视为失败）
        out = host_bash.invoke({"cmd": "pytest -q --tb=short", "timeout": timeout})
        # exit=0 视为通过
        if "[exit=0]" in out:
            return True, out[-500:], ""
        tail = out[-1000:]
        ftype = _classify_pytest_failure(tail)
        return False, tail, ftype
    except Exception as e:
        return False, f"pytest 调用异常：{type(e).__name__}: {e}", "UNKNOWN"


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

    # 2) 通用：如有 test_*.py 文件就跑 pytest（不论类型，lab_report 也可能含代码）
    has_test = any(
        (WORKSPACE_DIR / d).exists() and "test" in d.lower() for d in deliv
    ) or bool(list(WORKSPACE_DIR.rglob("test_*.py"))) or bool(list(WORKSPACE_DIR.rglob("*_test.py")))
    if has_test:
        ok, out, ftype = _run_pytest_in_workspace()
        evidence["pytest_output_tail"] = out
        evidence["pytest_failure_type"] = ftype
        if not ok:
            hint = _FAILURE_TYPE_HINTS.get(ftype, "")
            failures.append(f"pytest {ftype}：{hint}")
    else:
        evidence["pytest_skipped_reason"] = "未找到 test_*.py / *_test.py"

    # 3) lab_report 类：必备章节
    if ttype == "lab_report":
        failures.extend(_check_lab_sections(deliv))

    return failures, evidence


# ─── 阶段 2：LLM 语义覆盖 ────────────────────────────────────────


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


def _stage2_llm_coverage(state: HwState, stage1_failures: list[str]) -> dict[str, Any]:
    intake = state.get("intake_result") or {}
    constraints = list(intake.get("constraints") or [])
    user_cons = list(state.get("user_constraints") or [])
    task_type = str(intake.get("type") or "other")
    task_title = str(intake.get("title") or "")

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

    # 历史相似任务的卡片（reference block，不进硬清单——让 LLM 判断本次是否适用）
    hist_block = ""
    task_dag = state.get("task_dag") or {}
    retrieved_cards = task_dag.get("retrieved_cards") or []
    lesson_cards = [c for c in retrieved_cards if "[lesson]" in c]
    if lesson_cards:
        hist_lines = [f"- {l}" for l in lesson_cards]
        hist_block = (
            "## 历史相似任务的卡片（参考；如本次产物明显违反某条教训，"
            "请在 missing 中加 [历史教训] 前缀的条目）\n"
            + "\n".join(hist_lines)
            + "\n\n"
        )

    # 约束分节展示 + 覆盖优先级声明：
    # user_constraints 按 append 时序编号（最末条 = 最新一轮用户指令）；
    # 让 LLM 自行处理"后令覆盖前令"和"用户指令覆盖题面"的语义，避免被作废条目仍判 missing。
    cons_block_lines: list[str] = [
        "## 约束条目（已分节；判定前先读下方覆盖规则）",
        "",
        f"### 当前任务类型：{task_type}（title={task_title or '（空）'}）",
        "",
        "### 覆盖规则（必读）",
        "- 用户补充约束按对话时间顺序编号；**编号靠后者**与靠前者矛盾时，以靠后者为准，靠前者作废。",
        "- 用户补充约束整体覆盖题面约束中的同主题条目（用户后续的修订指令可推翻题面默认要求）。",
        "- 被覆盖作废的条目**不计入 missing，也不要为其找证据**；在 covered/missing 数组里直接省略。",
        "- 描述性交付物（[交付物]）独立判定。",
        "- 长期规则（[长期规则]）独立判定；但若该规则与当前任务**完全不相关**"
        "（例如「实验报告截图占位」对一道纯算法题），直接判 covered，evidence 写"
        "「N/A：与当前任务不相关」，不要列入 missing。",
        "",
        "### 题面约束（intake.constraints）",
    ]
    cons_block_lines += [f"- {c}" for c in constraints] or ["- （无）"]
    cons_block_lines += ["", "### 用户补充约束（user_constraints，按时间顺序）"]
    if user_cons:
        for i, c in enumerate(user_cons, 1):
            tag = "（最新一轮用户指令）" if i == len(user_cons) else ""
            cons_block_lines.append(f"- {i}) {c}{tag}")
    else:
        cons_block_lines.append("- （无）")
    cons_block_lines += ["", "### 描述性交付物（[交付物]）"]
    cons_block_lines += [f"- {c}" for c in deliv_cons] or ["- （无）"]
    cons_block_lines += ["", "### 长期规则（[长期规则]）"]
    cons_block_lines += [f"- {c}" for c in profile_cons] or ["- （无）"]

    user_msg = (
        stage1_block
        + "\n".join(cons_block_lines)
        + "\n\n"
        + hist_block
        + "## 产物内容\n\n"
        + artifacts_text
    )
    llm = get_llm()
    try:
        resp = llm.invoke(
            [SystemMessage(content=inject_for_agent("verifier", VERIFIER_COVERAGE_SYSTEM)), HumanMessage(content=user_msg)]
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = parse_result_json(content)
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
