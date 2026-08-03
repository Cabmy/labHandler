"""Intake agent —— 负责初始 workspace 扫描、指导文件识别与结构化任务需求抽取。

本节点作为系统入口，通过多层策略解析作业上下文，并将其转化为
下游节点可消费的 intake_result。

核心逻辑：
1. workspace 扫描：递归扫描 workspace 目录，识别潜在指导文档
   （如 README、PDF 实验指导）与支撑材料。
2. 指导文件分类：
   - 文本文件：在 host 端直接读取内容（.md, .txt）。
   - 复合文档文件：通过 AIO Sandbox 异步转换为 Markdown 格式（.pdf, .docx, .pptx）。
3. 结构化抽取：用 LLM 从聚合的上下文文本中提取任务标题、类型（Coding/Essay/Report）、
   必交付物与核心约束。
4. 质量预警：识别信号不足以构成有效任务时，主动抛 IntakeRejectError提示
   用户补充材料。

输出 intake_result 结构：
  {
    "title": str,              # 核心任务标题
    "type": str,               # 任务分类（coding/essay/lab_report/other）
    "deliverables": list[str], # 预期交付文件列表
    "constraints": list[str],  # 提取的业务逻辑与环境约束
    "instruction_files": list[str], # 识别出的指导文档路径
    "support_files": list[str],     # 识别出的代码或支撑数据路径
  }
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from rich.console import Console

from llm import get_llm
from llm.invoke import invoke_llm_json, extract_response_content, record_usage, usage_log_fields
from orchestrator.state import HwState
from config.prompts import build_intake_system, parse_result_json
from config.runtime import get_settings
from tools.workspace_utils import iter_workspace_files

WORKSPACE_DIR: Path = get_settings().workspace_dir

# 节点内终端提示（如重试）—— 与 ui.live_panel 共用同一个 rich Console，
# 自然嵌入主流 "🔎 intake" 归属行下，不破坏紧凑显示
_console = Console()

# 指导文件关键词（优先级递减；匹配 = 指导文件）
_INSTRUCTION_PATTERNS = [
    re.compile(r"^readme(\.|$)", re.IGNORECASE),
    re.compile(r"^requirements?(\.|$)", re.IGNORECASE),
    re.compile(r"实验.*指导|实验要求|作业说明|作业要求|题目"),
    # 常见中文命名约定（"上机作业4.pdf" / "编程作业-1.docx" / "课程设计.pdf" / "大作业.pdf"）
    re.compile(r"上机.*作业|上机.*实验|编程.*作业|课程.*设计|程序.*设计.*作业|大作业"),
    re.compile(r"^instruction", re.IGNORECASE),
    re.compile(r"^lab\d*[._-]", re.IGNORECASE),
    re.compile(r"^homework|^assignment", re.IGNORECASE),
]

# 文本扩展名（host 端可直接读）；其他扩展名（PDF/DOCX）走 sandbox_convert_to_markdown
_TEXT_EXTS = {".md", ".txt", ".rst", ".markdown"}
_PARSEABLE_EXTS = {".pdf", ".docx", ".pptx"}  # 需沙箱解析
_SUPPORT_EXTS = {".py", ".cpp", ".c", ".h", ".java", ".js", ".ts", ".sql", ".sh"}


class IntakeRejectError(Exception):
    """当 intake 发现输入不足以构成有效任务或遇到解析异常时抛出。"""
    pass


def _scan_workspace() -> dict[str, list[Path]]:
    """扫描 workspace 并按类型分类文件。

    返回：
        dict，键为：
        - "instruction"：匹配指导模式的文本文件（可直接读）
        - "needs_parse"：匹配指导模式的文档文件（需沙箱转换）
        - "support"：其他所有文件（代码/数据等）

        回退：找不到指导文件时，从 support 中提升文本/可解析文件。
    """
    instruction_files: list[Path] = []
    support_files: list[Path] = []
    needs_parse: list[Path] = []  # PDF/DOCX 等需沙箱解析


    for p in iter_workspace_files(WORKSPACE_DIR):
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

    # 兜底：workspace 没匹配到指导文件，但 support 里有可读文本/可解析文档
    # → 全部提升（用户只丢了文档时就是想用它；不要因为命名不规范而漏掉）。
    # 文本文件（.md/.txt/.rst）进 instruction_files，PDF/DOCX 进 needs_parse。
    if not instruction_files and not needs_parse:
        promoted: list[Path] = []
        for p in support_files:
            ext = p.suffix.lower()
            if ext in _TEXT_EXTS:
                instruction_files.append(p)
                promoted.append(p)
            elif ext in _PARSEABLE_EXTS:
                needs_parse.append(p)
                promoted.append(p)
        for p in promoted:
            support_files.remove(p)

    return {
        "instruction": instruction_files,
        "needs_parse": needs_parse,
        "support": support_files,
    }


def _read_instructions(files: list[Path], max_chars: int = 8000) -> str:
    """拼接指导文件为一整段文本（带截断以保护 token 预算）。"""
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


async def _parse_with_sandbox(files: list[Path], max_chars_per_file: int = 6000) -> str:
    """通过 sandbox_convert_to_markdown 把 PDF/DOCX 文件转为 markdown 后拼接。

    每个文件独立截断（max_chars_per_file），防止单个长 PDF 吃光整个 token 预算。
    沙箱不可达或单个文件解析失败时，该文件被单独标记；仍返回拼接结果
    （可能为空）。为空时 run_intake 回退到 '仅文件名提示' 路径。
    """
    chunks: list[str] = []
    for f in files:
        try:
            # sandbox_convert_to_markdown 内部调 _call → _translate_path，会把
            # host 绝对路径翻译为 /workspace/<rel>，沙箱内可见。
            from tools.sandbox_tools import sandbox_convert_to_markdown
            md = await sandbox_convert_to_markdown(str(f))
        except Exception as e:
            chunks.append(f"### {f.name}\n[沙箱解析失败：{type(e).__name__}: {e}]")
            continue
        if not isinstance(md, str):
            md = str(md)
        rel = f.relative_to(WORKSPACE_DIR)
        chunks.append(f"### {rel}\n\n{md[:max_chars_per_file]}")
    return "\n\n---\n\n".join(chunks)


def _llm_extract(instructions_text: str) -> dict[str, Any]:
    """LLM 提取 title/type/deliverables/constraints（CoT 双段输出 + 1 次自修复重试）。

    首个 LLM 的 <result> JSON 解析失败时（常见原因：复述题目示例时未转义双引号），
    不要立即报错 —— 给 LLM 看一眼错误，让它 '只修格式不改语义' 再试一次。
    仍失败则向上抛 JSONDecodeError；run_intake 会将其转为 IntakeRejectError。
    """
    # 加载 skill 元数据用于动态构建 system prompt
    try:
        from tools.skill_tool import list_skill_meta
        skill_meta = list_skill_meta()
    except Exception:
        skill_meta = []
    system_prompt = build_intake_system(skill_meta)

    llm = get_llm()
    prompt = f"Assignment instruction document:\n\n{instructions_text}\n\nPlease output <thinking> + <result> as required by the system prompt."

    # 首次调用 —— 修复 prompt 可能需要原始 content
    resp = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
    )
    record_usage("intake", resp)   # 这条不走 invoke_llm_json，账要自己记
    content = extract_response_content(resp)

    try:
        data = parse_result_json(content)
    except (json.JSONDecodeError, ValueError) as e:
        # 告诉用户正在发生什么，避免静默 10+ 秒
        err_brief = str(e).split("\n", 1)[0][:80]
        _console.print(
            f"  [yellow]⚠️ JSON 解析失败（{err_brief}），让 LLM 修一次格式…[/]"
        )
        fix_system = (
            "The JSON inside the <result> segment you just output failed to parse. Please only fix the format error "
            "(most commonly an unescaped double quote \" or backslash \\ or newline inside a string value); "
            "**keep the original content semantics unchanged**, and re-output the complete "
            "<thinking>...</thinking><result>{...}</result> per the system prompt schema."
        )
        fix_user = (
            f"Previous output (parse failed):\n```\n{content}\n```\n\n"
            f"json.loads error message: {e}\n\n"
            f"Please only fix the format (\" / \\ / newline inside strings must be escaped as \\\" / \\\\ / \\n), "
            f"and re-output the complete <thinking> + <result>."
        )
        # 修复重试 —— 用 invoke_llm_json（这次不需要原始 content）
        data = invoke_llm_json(llm, fix_system, fix_user, agent="intake")
        _console.print("  [green]✓ 修复成功，继续 intake[/]")

    return _normalize_intake_result(data)


def _normalize_intake_result(data: dict) -> dict[str, Any]:
    """将原始 LLM JSON 输出规范化为标准 intake 字段。"""
    return {
        "title": str(data.get("title") or "未命名作业"),
        "type": (data.get("type") or "other").strip().lower(),
        "deliverables": list(data.get("deliverables") or []),
        "constraints": list(data.get("constraints") or []),
        "suggestion": str(data.get("suggestion") or "").strip(),
    }


async def run_intake(state: HwState) -> dict[str, Any]:
    """LangGraph 异步节点入口：扫描 workspace + 提取 intake_result。
    
    异步原因：沙箱解析用直接 await（消除了旧 asyncio.run 对 'LangGraph 在线程池跑
    同步节点' 的隐式依赖）；同步 LLM 提取卸载到线程池避免阻塞事件循环。
    
    返回：state diff（{intake_result: ..., progress_log: [...]}）
    """
    import asyncio

    scan = _scan_workspace()
    instr_files = scan["instruction"]
    needs_parse = scan["needs_parse"]
    support_files = scan["support"]

    # 构建 LLM 输入文本：
    #   1) 已可读的指导文本（host 端 .md/.txt）
    #   2) 用户当前请求（REPL 输入；常含关键提示如 '这是一道上机作业'）
    #   3) PDF/DOCX 正文：调 sandbox_convert_to_markdown 转为 markdown
    #      （沙箱挂掉时回退到仅文件名）
    text = _read_instructions(instr_files)
    if needs_parse:
        parsed = await _parse_with_sandbox(needs_parse)
        if parsed.strip():
            text = (
                f"### Lab guidance file body (parsed to markdown by the sandbox)\n\n"
                f"{parsed}\n\n---\n\n{text}"
            )
        else:
            # 沙箱完全不可达：回退到旧路径（仅文件名提示）
            names = "\n".join(f"- {p.relative_to(WORKSPACE_DIR)}" for p in needs_parse)
            text = (
                f"### Assignment guidance files pending parse (sandbox unreachable, filenames only)\n"
                f"{names}\n\n---\n\n{text}"
            )
    if state.get("question"):
        text = f"### User current request\n{state['question']}\n\n---\n\n{text}"
    # 支撑材料文件列表（如 data.csv）：指导文档没点名具体数据文件时，
    # 这是供 LLM 推断交付物/约束的廉价信号
    if support_files:
        names = "\n".join(
            f"- {p.relative_to(WORKSPACE_DIR)}" for p in support_files[:30]
        )
        text += f"\n\n---\n\n### workspace supporting materials (filenames only, for inferring deliverables/constraints)\n{names}"

    # 任一来源有信号（指导文件 / PDF / 用户请求）就走 LLM 提取；
    # 只有全空时才回退到硬编码 'other'。
    has_signal = bool(instr_files or needs_parse or state.get("question"))
    if has_signal:
        try:
            extracted = await asyncio.to_thread(_llm_extract, text)
            if extracted.get("suggestion"):
                raise IntakeRejectError(extracted["suggestion"])
        except IntakeRejectError:
            raise
        except Exception as e:
            raise IntakeRejectError(f"Intake LLM 解析异常，请检查是否上传了格式正确的作业文档或重新输入。({type(e).__name__}: {e})")
    else:
        raise IntakeRejectError("未发现作业说明文档，且未提供具体请求。请先将作业要求（README/PDF等）放入 workspace 目录。")

    intake = {
        **extracted,
        "instruction_files": [str(p.relative_to(WORKSPACE_DIR)) for p in instr_files],
        "needs_parse": [str(p.relative_to(WORKSPACE_DIR)) for p in needs_parse],
        "support_files": [str(p.relative_to(WORKSPACE_DIR)) for p in support_files],
    }

    log_entry: dict[str, Any] = {
        "node": "intake",
        "title": intake["title"],
        "type": intake["type"],
        "n_instruction": len(intake["instruction_files"]),
        "n_needs_parse": len(intake["needs_parse"]),
        "n_constraints": len(intake["constraints"]),
    }
    # token 真值 + 前缀缓存命中（含 JSON 自修复重试那次）
    log_entry.update(usage_log_fields("intake"))

    return {
        "intake_result": intake,
        "progress_log": [log_entry],
    }
