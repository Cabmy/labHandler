"""Intake agent - 负责作业环境的初始化扫描、指导文件识别及任务需求的结构化提取。

该节点作为系统的入口，通过多级策略解析作业背景，并将其转化为后续节点可理解的 intake_result。

核心逻辑：
1. 工作空间扫描：递归扫描 workspace 目录，识别潜在的指导文档（如 README、PDF 实验指导等）和支撑材料。
2. 指导文件分类：
   - 文本类：直接在宿主端读取内容（.md, .txt）。
   - 复合文档类：通过 AIO Sandbox 异步转换为 Markdown 格式（.pdf, .docx, .pptx）。
3. 结构化提取：利用 LLM 从汇总后的背景文本中提取任务标题、类型（Coding/Essay/Report）、必交付物及核心约束。
4. 质量预警：若识别到的信号不足以构成有效任务，主动抛出 IntakeRejectError 引导用户补充材料。

输出 intake_result 结构：
  {
    "title": str,              # 任务核心标题
    "type": str,               # 任务分类（coding/essay/lab_report/other）
    "deliverables": list[str], # 预期的产物文件列表
    "constraints": list[str],  # 抽取的业务逻辑与环境约束
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
from orchestrator.state import HwState
from config.prompts import build_intake_system, parse_result_json
from config.runtime import get_settings

WORKSPACE_DIR: Path = get_settings().workspace_dir

# 节点内的终端提示（如 retry）—— 和 ui.live_panel 同款 rich Console，
# 自然嵌入到主流 "🔎 intake" 归属行之下，不破坏紧凑展示
_console = Console()

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


class IntakeRejectError(Exception):
    """当 intake 发现输入不足以构成有效任务或解析异常时，主动抛出以提示用户"""
    pass


def _scan_workspace() -> dict[str, list[Path]]:
    """递归扫 workspace，按文件性质分类（忽略 .labhandler / __pycache__）

    Returns:
        dict 包含三个键：
        - "instruction": 匹配指导文件模式且为纯文本后缀(.md/.txt/.rst)的文件，
          可直接在宿主端读取内容喂给 LLM
        - "needs_parse": 匹配指导文件模式但为需解析后缀(.pdf/.docx/.pptx)的文件，
          须走 sandbox 转换为 Markdown 后再使用
        - "support": 其余所有文件（代码/数据/图片等），作为支撑材料

        若未匹配到任何指导文件，兜底逻辑会将 support 中的文本/可解析文档
        全部升级为 instruction 或 needs_parse（避免因命名不规范而漏识）。
    """
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

    # 兜底：workspace 没匹到任何指导文件，但 support 里有可读的文本/可解析文档 →
    # 全部升级（用户既然只丢了文档，那就是要用它，避免因命名不规范而漏识）。
    # 文本类（.md/.txt/.rst）进 instruction_files，PDF/DOCX 进 needs_parse。
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


def _llm_extract(instructions_text: str) -> dict[str, Any]:
    """LLM 抽 title/type/deliverables/constraints（CoT 双段输出 + 1 次自修复 retry）

    第一次 LLM 返回的 <result> JSON 若解析失败（常见原因：复述题面示例时漏转义内嵌
    双引号），不立刻抛——给 LLM 看一眼错误信息让它"只修格式不改语义"再试一次。
    再失败才向上抛 JSONDecodeError，由 run_intake 转 IntakeRejectError。
    """
    # 加载 skill 元数据，动态构建 system prompt
    try:
        from tools.skill_tool import list_skill_meta
        skill_meta = list_skill_meta()
    except Exception:
        skill_meta = []
    system_prompt = build_intake_system(skill_meta)

    llm = get_llm()
    prompt = f"作业说明文档：\n\n{instructions_text}\n\n请按 system prompt 要求输出 <thinking> + <result>。"
    resp = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
    )
    content = resp.content if isinstance(resp.content, str) else str(resp.content)

    try:
        data = parse_result_json(content)
    except (json.JSONDecodeError, ValueError) as e:
        # 告诉用户在做什么，避免终端静默 10+ 秒
        err_brief = str(e).split("\n", 1)[0][:80]
        _console.print(
            f"  [yellow]⚠️ JSON 解析失败（{err_brief}），让 LLM 修一次格式…[/]"
        )
        fix_system = (
            "你刚才输出的 <result> 段内 JSON 解析失败。请只修复格式错误（最常见是字符串值"
            "里未转义的双引号 \" 或反斜杠 \\ 或换行），**保留原内容语义不变**，"
            "重新按 system prompt 的 schema 完整输出 <thinking>...</thinking><result>{...}</result>。"
        )
        fix_user = (
            f"上次输出（解析失败）：\n```\n{content}\n```\n\n"
            f"json.loads 错误信息：{e}\n\n"
            f"请只修格式（字符串内 \" / \\ / 换行须转义为 \\\" / \\\\ / \\n），"
            f"重新输出完整的 <thinking> + <result>。"
        )
        resp2 = llm.invoke(
            [SystemMessage(content=fix_system), HumanMessage(content=fix_user)]
        )
        content2 = resp2.content if isinstance(resp2.content, str) else str(resp2.content)
        data = parse_result_json(content2)  # 再炸就让外层 IntakeRejectError 接住
        _console.print("  [green]✓ 修复成功，继续 intake[/]")

    # 字段标准化 + 默认值
    return {
        "title": str(data.get("title") or "未命名作业"),
        "type": (data.get("type") or "other").strip().lower(),
        "deliverables": list(data.get("deliverables") or []),
        "constraints": list(data.get("constraints") or []),
        "suggestion": str(data.get("suggestion") or "").strip(),
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
