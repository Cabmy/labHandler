"""Compile 节点 - 负责任务执行后的产物固化、元数据记录与状态整理。

该节点在 Verifier 之后、Summarizer 之前运行，作为执行阶段到总结阶段的过渡层。它不直接操作
业务代码，而是专注于管理任务执行产生的副作用（日志、工具轨迹）和同步物理文件状态。

核心职责：
1. 元数据落盘：把任务的进度 (progress_log)、工具历史 (tool_history)、完整对话流水
   (transcript) 写入 workspace/.labhandler/runs/<ts>/ 子目录，多轮跑不互相覆盖；
   `.labhandler/latest.txt` 始终指向最近一次 run 的 ts。tool_history 不再截断，
   完整保留 args 与 content，方便事后回放和审计。
2. 产物清单扫描：递归扫描 workspace 目录，识别并刷新 state 中的 artifacts 列表，自动
   排除系统目录（如 .git, __pycache__）及临时缓存文件。
3. 状态标记：若任务因达到最大重试次数而提前终止，本节点会标记 "partial=true"，
   提示 Summarizer 生成针对"部分完成"任务的总结报告。
4. 路由转发：作为主图流水线中的整理层，确保后续的总结节点拥有最完整、最准确的物理环境视图。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from config.runtime import get_settings
from orchestrator.replan import is_partial
from orchestrator.state import HwState

WORKSPACE_DIR: Path = get_settings().workspace_dir
META_DIR_NAME = ".labhandler"

# 产物扫描排除项
_EXCLUDE_DIRS = {".labhandler", "__pycache__", ".pytest_cache", ".git", ".venv", ".idea"}
_ARTIFACT_EXTS = {".py", ".md", ".txt", ".cpp", ".c", ".h", ".java", ".js", ".ts",
                  ".sql", ".sh", ".html", ".css", ".json", ".yaml", ".yml"}


def _meta_dir() -> Path:
    d = WORKSPACE_DIR / META_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_dir(ts: str) -> Path:
    """本次 run 的日志目录：.labhandler/runs/<ts>/，多轮跑互不覆盖"""
    d = _meta_dir() / "runs" / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    """覆盖式写 jsonl（单次 Compile 输出最终全量；多轮的累加由 LangGraph state 完成）"""
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _extract_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 HwState.messages 提取 tool_call 调用与对应 tool 返回结果（content 不截断）"""
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
                    "content": m.get("content") or "",   # 完整保留，jsonl 不在乎行长
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
      - 把 progress_log / tool_history / transcript 持久化到 workspace/.labhandler/runs/<ts>/
      - 扫 workspace 把产物清单注入 state.artifacts
      - 在 progress_log 追加 compile 记录（含 partial 标记）
    """
    ts = time.strftime("%Y%m%dT%H%M%S")
    run_d = _run_dir(ts)

    progress = list(state.get("progress_log") or [])
    n_progress = _write_jsonl(run_d / "progress_log.jsonl", progress)

    messages = list(state.get("messages") or [])
    n_tools = _write_jsonl(run_d / "tool_history.jsonl", _extract_tool_history(messages))
    n_msgs = _write_jsonl(run_d / "transcript.jsonl", messages)

    # latest.txt：指向当前 run 的 ts，让 cli / 外部工具能找到最近的日志目录
    (_meta_dir() / "latest.txt").write_text(ts, encoding="utf-8")

    scanned = _scan_artifacts()
    # 现存 state.artifacts 已是累加（Annotated[list, add]）；compile 只补"扫描快照"作 attribute 区分
    snapshot_artifacts = [
        {**a, "kind": "scanned", "ts": int(time.time())} for a in scanned
    ]

    partial = is_partial(state)

    compile_log = {
        "node": "compile",
        "run_ts": ts,
        "n_progress_written": n_progress,
        "n_tool_history": n_tools,
        "n_transcript": n_msgs,
        "n_artifacts": len(snapshot_artifacts),
        "partial": partial,
    }

    return {
        "artifacts": snapshot_artifacts,
        "progress_log": [compile_log],
    }
