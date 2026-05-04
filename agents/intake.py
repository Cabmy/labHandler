"""Intake agent - 扫 workspace + 关键字定位实验指导 + LLM 抽结构化字段

输出 intake_result（HwState 子结构）：
  {
    "title":        str,       # 任务标题（"实现二分查找" 等）
    "type":         str,       # coding / essay / lab_report / other
    "deliverables": list[str], # 必交付物的文件名/类型（如 ["solution.py", "test_solution.py"]）
    "constraints":  list[str], # 题面/作业要求里抽出的约束（"不许使用 numpy" 等）
    "instruction_files": list[str],  # 实验指导文件路径列表
    "support_files":     list[str],  # 其他参考文件
  }

设计要点（PLAN §8.1 / STEPS P4.1）：
1. 关键字定位：README* / requirements* / 实验指导* / lab*.md|pdf|docx / instruction* 等
2. PDF/DOCX 不在 host 端解（不装 PyMuPDF），交给 sandbox_convert_to_markdown（P4.3 Coder 调）
3. LLM 兜底分类：把指导文件全文喂 LLM，输出 JSON
4. type 枚举：coding / essay / lab_report / other（4 选 1）
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from llm import get_llm
from orchestrator.state import HwState
from prompts import INTAKE_SYSTEM, extract_result

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()

# 实验指导关键字（按优先级降序；命中即视为指导文件）
_INSTRUCTION_PATTERNS = [
    re.compile(r"^readme(\.|$)", re.IGNORECASE),
    re.compile(r"^requirements?(\.|$)", re.IGNORECASE),
    re.compile(r"实验.*指导|实验要求|作业说明|作业要求|题目"),
    # 中文常见叫法（"上机作业4.pdf" / "编程作业-1.docx" / "课程设计.pdf" / "大作业.pdf"）
    re.compile(r"上机.*作业|上机.*实验|编程.*作业|课程.*设计|程序.*设计.*作业|大作业"),
    re.compile(r"^instruction", re.IGNORECASE),
    re.compile(r"^lab\d*[._-]", re.IGNORECASE),
    re.compile(r"^homework|^assignment", re.IGNORECASE),
]

# 文本类后缀（host 端可直接读）；其他后缀（PDF/DOCX）走 sandbox_convert_to_markdown
_TEXT_EXTS = {".md", ".txt", ".rst", ".markdown"}
_PARSEABLE_EXTS = {".pdf", ".docx", ".pptx"}  # 需要 sandbox 解析
_SUPPORT_EXTS = {".py", ".cpp", ".c", ".h", ".java", ".js", ".ts", ".sql", ".sh"}


def _scan_workspace() -> dict[str, list[Path]]:
    """递归扫 workspace，按文件性质分类（忽略 .hwhandler / __pycache__）"""
    instruction_files: list[Path] = []
    support_files: list[Path] = []
    needs_parse: list[Path] = []  # PDF/DOCX 等需要 sandbox 解析的指导文件
    for p in WORKSPACE_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(WORKSPACE_DIR)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        name = p.name
        is_instruction = any(pat.search(name) for pat in _INSTRUCTION_PATTERNS)
        if is_instruction and p.suffix.lower() in _TEXT_EXTS:
            instruction_files.append(p)
        elif is_instruction and p.suffix.lower() in _PARSEABLE_EXTS:
            needs_parse.append(p)
        elif p.suffix.lower() in _SUPPORT_EXTS:
            support_files.append(p)
        else:
            support_files.append(p)

    # 兜底：workspace 没匹到任何指导文件，但只有"一个" PDF/DOCX 候选 → 升级为 needs_parse
    # 理由：用户既然只丢了一个可解析文档，那就是要用它（避免因命名不规范而漏识）
    if not instruction_files and not needs_parse:
        parseable_in_support = [
            p for p in support_files if p.suffix.lower() in _PARSEABLE_EXTS
        ]
        if len(parseable_in_support) == 1:
            promoted = parseable_in_support[0]
            needs_parse.append(promoted)
            support_files.remove(promoted)

    return {
        "instruction": instruction_files,
        "needs_parse": needs_parse,
        "support": support_files,
    }


def _read_instructions(files: list[Path], max_chars: int = 8000) -> str:
    """把指导文件拼成一个长文本（截断保护 token 预算）"""
    chunks: list[str] = []
    total = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = f.relative_to(WORKSPACE_DIR)
        chunks.append(f"### {rel}\n\n{text}")
        total += len(text)
        if total >= max_chars:
            break
    full = "\n\n---\n\n".join(chunks)
    return full[:max_chars]


def _parse_with_sandbox(files: list[Path], max_chars_per_file: int = 6000) -> str:
    """把 PDF/DOCX 通过 sandbox_convert_to_markdown 转 markdown 再拼接。

    每个文件单独截断（max_chars_per_file），避免一篇长 PDF 吃掉整个 token 预算。
    沙箱不可达 / 单文件解析失败时单条标记，整体仍返回拼接结果（可能为空）。
    返回空串时 run_intake 会降级到「仅文件名提示」路径。
    """
    import asyncio

    chunks: list[str] = []
    for f in files:
        try:
            # sandbox_convert_to_markdown 内部 _call → _translate_path 会把 host 绝对路径
            # 翻译为 /workspace/<rel>，沙箱可见。
            from tools.sandbox_tools import sandbox_convert_to_markdown
            md = asyncio.run(sandbox_convert_to_markdown(str(f)))
        except Exception as e:
            chunks.append(f"### {f.name}\n[沙箱解析失败：{type(e).__name__}: {e}]")
            continue
        if not isinstance(md, str):
            md = str(md)
        rel = f.relative_to(WORKSPACE_DIR)
        chunks.append(f"### {rel}\n\n{md[:max_chars_per_file]}")
    return "\n\n---\n\n".join(chunks)


_INTAKE_SYSTEM = INTAKE_SYSTEM  # 保持局部引用名兼容


def _llm_extract(instructions_text: str) -> dict[str, Any]:
    """LLM 抽 title/type/deliverables/constraints（带 CoT 双段输出 + 容错抽取）"""
    llm = get_llm()
    prompt = f"作业说明文档：\n\n{instructions_text}\n\n请按 system prompt 要求输出 <thinking> + <result>。"
    resp = llm.invoke(
        [SystemMessage(content=_INTAKE_SYSTEM), HumanMessage(content=prompt)]
    )
    content = resp.content if isinstance(resp.content, str) else str(resp.content)

    # 从 <result> 抽 JSON 段（剥 thinking + 兼容 markdown 包裹）
    text = extract_result(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：找首末大括号再试
        l, r = text.find("{"), text.rfind("}")
        if l >= 0 and r > l:
            data = json.loads(text[l : r + 1])
        else:
            raise

    # 字段标准化 + 默认值
    return {
        "title": str(data.get("title") or "未命名作业"),
        "type": (data.get("type") or "other").strip().lower(),
        "deliverables": list(data.get("deliverables") or []),
        "constraints": list(data.get("constraints") or []),
    }


def run_intake(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口：扫描 workspace + 抽 intake_result。

    Returns: state diff（{intake_result: ..., progress_log: [...]}）
    """
    scan = _scan_workspace()
    instr_files = scan["instruction"]
    needs_parse = scan["needs_parse"]
    support_files = scan["support"]

    # 拼 LLM 输入文本：
    #   1) 已可读的 instruction 文本（host 端 .md/.txt）
    #   2) 用户当前请求（REPL 输入；常含"这是上机作业"这类关键提示）
    #   3) PDF/DOCX 正文：调 sandbox_convert_to_markdown 转 markdown（沙箱挂时降级到只给文件名）
    text = _read_instructions(instr_files)
    if needs_parse:
        parsed = _parse_with_sandbox(needs_parse)
        if parsed.strip():
            text = (
                f"### 实验指导文件正文（已由沙箱解析为 markdown）\n\n"
                f"{parsed}\n\n---\n\n{text}"
            )
        else:
            # 沙箱完全不可达：降级到旧路径（仅文件名提示）
            names = "\n".join(f"- {p.relative_to(WORKSPACE_DIR)}" for p in needs_parse)
            text = (
                f"### 待解析的作业指导文件（沙箱不可达，仅提供文件名）\n"
                f"{names}\n\n---\n\n{text}"
            )
    if state.get("question"):
        text = f"### 用户当前请求\n{state['question']}\n\n---\n\n{text}"

    # 只要有任一来源（指导文件 / PDF / 用户请求），就走 LLM 抽取；
    # 全空才退化到硬编码 "other"。
    has_signal = bool(instr_files or needs_parse or state.get("question"))
    if has_signal:
        try:
            extracted = _llm_extract(text)
        except Exception as e:
            extracted = {
                "title": state.get("question") or "未命名作业",
                "type": "other",
                "deliverables": [],
                "constraints": [f"[Intake LLM 解析失败：{type(e).__name__}]"],
            }
    else:
        extracted = {
            "title": "未命名作业",
            "type": "other",
            "deliverables": [],
            "constraints": [],
        }

    intake = {
        **extracted,
        "instruction_files": [str(p.relative_to(WORKSPACE_DIR)) for p in instr_files],
        "needs_parse": [str(p.relative_to(WORKSPACE_DIR)) for p in needs_parse],
        "support_files": [str(p.relative_to(WORKSPACE_DIR)) for p in support_files],
    }

    return {
        "intake_result": intake,
        "progress_log": [
            {
                "node": "intake",
                "title": intake["title"],
                "type": intake["type"],
                "n_instruction": len(intake["instruction_files"]),
                "n_needs_parse": len(intake["needs_parse"]),
                "n_constraints": len(intake["constraints"]),
            }
        ],
    }
