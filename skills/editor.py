"""/edit_skill 编辑判官（仿 memory/dream.py 的单模块 LLM 判官模式）。

流程：读取全部 skill 文件 ->（若指令提及 workspace 文件，读作文风
样本）-> 一次 LLM 调用产出多文件操作 -> 预校验 + 统一 diff ->
调用方展示确认 -> apply_edit 落盘
（skills/repository.py:apply_skill_operations，整批校验整批落盘）。

约束：仅能编辑现有 skill（coding/essay/lab_report）；禁止经编辑
入口新增 skill。触发入口：CLI /edit_skill 或 Web POST /api/edit_skill。
纯离线操作，不触碰主图状态。
"""

from __future__ import annotations

import difflib
from typing import Any

from config.prompts import EDIT_SKILL_SYSTEM
from config.runtime import get_settings
from runtime.llm import LLMGateway
from runtime.schema_call import SKILL_EDIT_SCHEMA, SUBMIT_SKILL_EDIT, oneshot_schema
from skills.repository import (
    apply_skill_operations,
    list_skill_documents,
    read_skill_files,
    validate_operation,
)

# 单个文风样本的截断上限（字符数）
_MAX_SAMPLE_CHARS = 8000

# host 侧可直接读的文本后缀；PDF/DOCX/PPTX 走 sandbox_convert_to_markdown
_TEXT_EXTS = {".md", ".txt", ".rst", ".markdown"}
_PARSEABLE_EXTS = {".pdf", ".docx", ".pptx"}


def existing_skill_names() -> list[str]:
    """现有 skill 名列表（编辑目标白名单，当前为 coding/essay/lab_report）。"""
    return sorted(s["name"] for s in list_skill_documents())


async def _collect_style_samples(instruction: str) -> tuple[dict[str, str], list[str]]:
    """从指令文本中匹配 workspace 顶层文件名，读作文风样本。

    自然语言指令可直接提及文件名（如“学一下我的实验报告.docx 的文风”），
    无需标志位；Web 侧样本经现有上传接口进入 workspace。长文件名优先匹配，
    匹配到的片段从指令中掩蔽，防止“a.md”作为“data.md”子串被误拉入。
    返回：({文件名: 样本文本}, [解析失败描述])
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
            try:
                from tools.sandbox_tools import sandbox_convert_to_markdown
                text = await sandbox_convert_to_markdown(str(p))
                if not isinstance(text, str):
                    text = str(text)
            except Exception as e:
                # 失败样本不入 samples：占位符喂给 LLM 无意义，
                # 且会误导用户以为已学到文风；失败原因单独曝出
                failures.append(f"{p.name}：沙箱解析失败（{type(e).__name__}: {e}）")
                continue
        else:
            continue  # 其他后缀（代码/图片等）不作为文风样本
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
    """按操作集逐文件生成 unified diff（新建文件旧文本为空；删除新文本为空）。"""
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


async def propose_edit(skill_name: str, instruction: str) -> dict[str, Any]:
    """生成编辑提案（不落盘）。"""
    available = existing_skill_names()
    if skill_name not in available:
        raise FileNotFoundError(
            f"skill 不存在：{skill_name!r}（仅支持编辑现有 skill：{available}，不支持新增）"
        )
    files = read_skill_files(skill_name)
    samples, sample_failures = await _collect_style_samples(instruction)

    settings = get_settings()
    llm = LLMGateway(settings)
    data = await oneshot_schema(
        llm,
        model=settings.pro_model,
        system=EDIT_SKILL_SYSTEM,
        user=_build_user_msg(skill_name, files, instruction, samples),
        name=SUBMIT_SKILL_EDIT,
        schema=SKILL_EDIT_SCHEMA,
        description="Submit skill file operations",
    )

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
        validate_operation(skill_name, norm)
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
    """确认后落盘（整批校验整批落盘）。返回：{applied: [...]}"""
    return {"applied": apply_skill_operations(skill_name, operations)}
