"""Compile 节点 - 二阶段执行的"落地与转发"层

设计要点（PLAN §15 / STEPS P5.3，用户决策"只写元数据 + 路由转发"）：
1. Coder 已直接用 host fs_tools 写到 workspace/，本节点不做容器 download
2. 写元数据：
   - workspace/.hwhandler/progress_log.jsonl   ← state["progress_log"] 全量
   - workspace/.hwhandler/tool_history.jsonl   ← messages 里所有 tool_calls + tool 回应
3. 扫 workspace（排除 .hwhandler / __pycache__ / .pytest_cache）刷新 artifacts 列表
4. 在 progress_log 追加一条 compile 记录；如属"部分完成"，标记 partial=true 让 Summarizer 体感到
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from orchestrator.replan import is_partial
from orchestrator.state import HwState

WORKSPACE_DIR: Path = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
META_DIR_NAME = ".hwhandler"

# 产物扫描排除项
_EXCLUDE_DIRS = {".hwhandler", "__pycache__", ".pytest_cache", ".git", ".venv", ".idea"}
_ARTIFACT_EXTS = {".py", ".md", ".txt", ".cpp", ".c", ".h", ".java", ".js", ".ts",
                  ".sql", ".sh", ".html", ".css", ".json", ".yaml", ".yml"}


def _meta_dir() -> Path:
    d = WORKSPACE_DIR / META_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    """覆盖式写 jsonl（单次 Compile 输出最终全量；多轮的累加由 LangGraph state 完成）"""
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _extract_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 HwState.messages 提取 tool_call 调用与对应 tool 返回结果"""
    out: list[dict[str, Any]] = []
    for m in messages or []:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                # langchain tool_calls 结构兼容：可能 {id, name, args} 也可能 {function:{name,arguments}}
                name = tc.get("name") or (tc.get("function") or {}).get("name", "")
                args = tc.get("args") or (tc.get("function") or {}).get("arguments", {})
                out.append(
                    {
                        "kind": "call",
                        "tool_call_id": tc.get("id", ""),
                        "name": name,
                        "args": args,
                    }
                )
        elif role == "tool":
            out.append(
                {
                    "kind": "result",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "name": m.get("name", ""),
                    "content_excerpt": (m.get("content") or "")[:500],
                }
            )
    return out


def _scan_artifacts() -> list[dict[str, Any]]:
    """扫 workspace 列产物"""
    out: list[dict[str, Any]] = []
    for p in WORKSPACE_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(WORKSPACE_DIR)
        if any(part in _EXCLUDE_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if p.suffix.lower() not in _ARTIFACT_EXTS:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append(
            {
                "path": str(rel),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    return sorted(out, key=lambda x: x["path"])


def run_compile(state: HwState) -> dict[str, Any]:
    """LangGraph 节点入口。

    职责：
      - 把 progress_log / tool_history 持久化到 workspace/.hwhandler/
      - 扫 workspace 把产物清单注入 state.artifacts
      - 在 progress_log 追加 compile 记录（含 partial 标记）
    """
    meta = _meta_dir()
    progress = list(state.get("progress_log") or [])
    n_progress = _write_jsonl(meta / "progress_log.jsonl", progress)

    tool_history = _extract_tool_history(state.get("messages") or [])
    n_tools = _write_jsonl(meta / "tool_history.jsonl", tool_history)

    scanned = _scan_artifacts()
    # 现存 state.artifacts 已是累加（Annotated[list, add]）；compile 只补"扫描快照"作 attribute 区分
    snapshot_artifacts = [
        {**a, "kind": "scanned", "ts": int(time.time())} for a in scanned
    ]

    partial = is_partial(state)

    compile_log = {
        "node": "compile",
        "n_progress_written": n_progress,
        "n_tool_history": n_tools,
        "n_artifacts": len(snapshot_artifacts),
        "partial": partial,
    }

    return {
        "artifacts": snapshot_artifacts,
        "progress_log": [compile_log],
    }
