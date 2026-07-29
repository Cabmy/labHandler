"""/edit_skill 编辑判官（仿 memory/dream.py 的单模块 LLM 判官模式）。

流程：读取 skill 全部文件 →（指令提及 workspace 文件则读入作为文风样本）→
一次 LLM 调用产出多文件 operations → 预校验 + unified diff → 调用方展示确认 →
apply_edit 落盘（skills/repository.py:apply_skill_operations，整批校验整批应用）。

约束：只编辑现有 skill（coding/essay/lab_report），禁止借编辑入口新建 skill；
触发入口：CLI /edit_skill 或 Web POST /api/edit_skill。纯离线操作，不碰主图 state。
"""

from __future__ import annotations

import difflib
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from config.prompts import EDIT_SKILL_SYSTEM, parse_result_json
from config.runtime import get_settings
from llm import get_llm
from skills.repository import (
    _validate_operation,
    apply_skill_operations,
    list_skill_documents,
    read_skill_files,
)

# 单个文风样本的截断上限（字符）
_MAX_SAMPLE_CHARS = 8000

# host 端可直接读的文本后缀；PDF/DOCX/PPTX 走 sandbox_convert_to_markdown
_TEXT_EXTS = {".md", ".txt", ".rst", ".markdown"}
_PARSEABLE_EXTS = {".pdf", ".docx", ".pptx"}


def existing_skill_names() -> list[str]:
    """现有 skill 名清单（编辑目标白名单，当前 coding/essay/lab_report）。"""
    return sorted(s["name"] for s in list_skill_documents())


def _collect_style_samples(instruction: str) -> tuple[dict[str, str], list[str]]:
    """从指令文本中匹配 workspace 一级文件名，读入作为文风样本。

    自然语言指令里直接提文件名即可（如"学习 我的实验报告.docx 的文风"），
    不设 flag；Web 端样本先经现有上传接口进 workspace。
    长文件名优先匹配并从指令中掩盖已命中片段，防 "a.md" 因是 "data.md"
    子串而被误拉入。
    Returns: ({文件名: 样本文本}, [解析失败说明])
    """
    ws = get_settings().workspace_dir
    if not ws.is_dir():
        return {}, []
    candidates = [
        p for p in ws.iterdir() if p.is_file() and not p.name.startswith(".")
    ]
    samples: dict[str, str] = {}
    failures: list[str] = []
    masked = instruction
    for p in sorted(candidates, key=lambda p: len(p.name), reverse=True):
        if p.name not in masked:
            continue
        masked = masked.replace(p.name, "\x00")
        suffix = p.suffix.lower()
        if suffix in _TEXT_EXTS:
            text = p.read_text(encoding="utf-8", errors="replace")
        elif suffix in _PARSEABLE_EXTS:
            # PDF/DOCX 复用沙箱解析（同 agents/intake.py:_parse_with_sandbox 模式）；
            # 编辑命令在主线程/工作线程同步跑，无 running loop，asyncio.run 安全
            import asyncio

            try:
                from tools.sandbox_tools import sandbox_convert_to_markdown
                text = asyncio.run(sandbox_convert_to_markdown(str(p)))
                if not isinstance(text, str):
                    text = str(text)
            except Exception as e:
                # 失败样本不进 samples：占位符喂给 LLM 无意义，且会让用户
                # 误以为文风已学习；失败原因单独透出
                failures.append(f"{p.name}：沙箱解析失败（{type(e).__name__}: {e}）")
                continue
        else:
            continue  # 其他后缀（代码/图片等）不当文风样本
        samples[p.name] = text[:_MAX_SAMPLE_CHARS]
    return samples, failures


def _build_user_msg(
    skill_name: str,
    files: dict[str, str],
    instruction: str,
    samples: dict[str, str],
) -> str:
    lines = [f"## 待编辑 skill：{skill_name}（以下为全部现有文件）", ""]
    for rel, text in files.items():
        lines.append(f"### 文件：{rel}")
        lines.append("```")
        lines.append(text)
        lines.append("```")
        lines.append("")
    lines.append("## 用户编辑指令")
    lines.append(instruction.strip())
    if samples:
        lines.append("")
        lines.append("## 用户文风样本（提炼风格特征用，禁止原文抄入 skill）")
        for name, text in samples.items():
            lines.append(f"### 样本：{name}（已截断至 {_MAX_SAMPLE_CHARS} 字符）")
            lines.append(text)
            lines.append("")
    return "\n".join(lines)


def _make_diffs(
    files: dict[str, str], operations: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """按操作集生成 per-file unified diff（新建文件旧文本为空；delete 新文本为空）。"""
    diffs: list[dict[str, str]] = []
    for op in operations:
        file = str(op["file"])
        old = files.get(file, "")
        new = str(op.get("content", "")) if op["action"] == "write" else ""
        diff = "\n".join(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{file}", tofile=f"b/{file}", lineterm="",
        ))
        diffs.append({"file": file, "diff": diff or "(无内容变化)"})
    return diffs


def propose_edit(skill_name: str, instruction: str) -> dict[str, Any]:
    """生成编辑提案（不落盘）。

    Returns: {skill_name, summary, operations, diffs, style_samples}
    Raises: FileNotFoundError（skill 不在现有清单）/ LLM 输出解析、校验异常
    """
    available = existing_skill_names()
    if skill_name not in available:
        raise FileNotFoundError(
            f"skill 不存在：{skill_name!r}（仅支持编辑现有 skill：{available}，不支持新增）"
        )
    files = read_skill_files(skill_name)
    samples, sample_failures = _collect_style_samples(instruction)

    llm = get_llm()
    resp = llm.invoke([
        SystemMessage(content=EDIT_SKILL_SYSTEM),
        HumanMessage(content=_build_user_msg(skill_name, files, instruction, samples)),
    ])
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = parse_result_json(text)

    operations_raw = data.get("operations") or []
    if not isinstance(operations_raw, list):
        raise ValueError(f"operations 必须是数组，实际为 {type(operations_raw).__name__}")
    operations: list[dict[str, Any]] = []
    for op in operations_raw:
        if not isinstance(op, dict):
            raise ValueError(f"非法 operation：{op!r}")
        norm = {
            "action": str(op.get("action", "")),
            "file": str(op.get("file", "")),
            "content": op.get("content", "") or "",
        }
        # 与 apply 同源的预校验：任何一条非法立即报错，不给用户看半合法提案
        _validate_operation(skill_name, norm)
        operations.append(norm)

    return {
        "skill_name": skill_name,
        "summary": str(data.get("summary", "")).strip(),
        "operations": operations,
        "diffs": _make_diffs(files, operations),
        "style_samples": list(samples),
        "sample_failures": sample_failures,
    }


def apply_edit(skill_name: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    """确认后落盘（整批校验整批应用）。Returns: {applied: [...]}"""
    return {"applied": apply_skill_operations(skill_name, operations)}
