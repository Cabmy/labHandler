"""Compile 节点——任务执行后的产物汇总、元数据记录与状态整理。

本节点跑在 Verifier 之后、Summarizer 之前，是从执行阶段到总结阶段的
过渡层。不直接操作业务代码，专注于管理任务执行产生的副作用
（日志、工具轨迹）并同步物理文件状态。

核心职责：
1. 元数据持久化：将任务进度（progress_log）、工具历史（tool_history）与完整
   对话转录（transcript）写入 workspace/.labhandler/runs/<ts>/ 子目录；
   多次运行互不覆盖。`.labhandler/latest.txt` 始终指向最近一次运行的 ts。
   messages 是归约字段，所以 transcript 覆盖**全部 step**（不再只剩最后一步），
   每条带 step_id / iteration 归属；tool_history 同样带 step_id，便于定位某次
   工具调用属于哪一步。工具返回内容在写入 state 时有 8000 字符预算
   （见 agents/coder.py:_TOOL_CONTENT_BUDGET），超长会带 `…[truncated N chars]` 标记。
2. 产物列表扫描：递归扫描 workspace 目录，识别并刷新 state 中的 artifacts 列表，
   自动排除系统目录（如 .git、__pycache__）与临时缓存文件。
3. 状态标记：若任务因达到最大重试次数而提前终止，本节点标记
   'partial=true'，提示 Summarizer 为 '部分完成' 的任务生成总结报告。
4. 路由转发：作为主图流水线中的清理层，确保下游总结节点
   拿到最完整准确的物理环境视图。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from config.runtime import get_settings
from orchestrator.replan import is_partial
from orchestrator.state import HwState
from tools.workspace_utils import iter_workspace_files

WORKSPACE_DIR: Path = get_settings().workspace_dir
META_DIR_NAME = ".labhandler"

# 扫描时纳入的产物文件扩展名
_ARTIFACT_EXTS = {".py", ".md", ".txt", ".cpp", ".c", ".h", ".java", ".js", ".ts",
                  ".sql", ".sh", ".html", ".css", ".json", ".yaml", ".yml"}


def _meta_dir() -> Path:
    d = WORKSPACE_DIR / META_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_dir(ts: str) -> Path:
    """本次运行的日志目录：.labhandler/runs/<ts>/，多次运行互不覆盖。"""
    d = _meta_dir() / "runs" / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    """覆盖写 jsonl（单次 Compile 输出最终全量；多轮累积由 LangGraph state 负责）。"""
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _extract_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 HwState.messages 提取 tool_call 调用及对应的 tool 返回结果。

    每条带 step_id：messages 跨 step 累积后，没有归属就无法判断某次工具调用
    属于 DAG 的哪一步。content 保留写入 state 时的原样（含可能的截断标记）。
    """
    out: list[dict[str, Any]] = []
    for m in messages or []:
        role = m.get("role")
        step_id = m.get("step_id", "")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                # langchain tool_calls 结构兼容：可能是 {id, name, args} 或 {function:{name,arguments}}
                name = tc.get("name") or (tc.get("function") or {}).get("name", "")
                args = tc.get("args") or (tc.get("function") or {}).get("arguments", {})
                out.append(
                    {
                        "kind": "call",
                        "step_id": step_id,
                        "tool_call_id": tc.get("id", ""),
                        "name": name,
                        "args": args,
                    }
                )
        elif role == "tool":
            out.append(
                {
                    "kind": "result",
                    "step_id": step_id,
                    "tool_call_id": m.get("tool_call_id", ""),
                    "name": m.get("name", ""),
                    "content": m.get("content") or "",
                }
            )
    return out


def _scan_artifacts() -> list[dict[str, Any]]:
    """扫描 workspace 中带元数据的产物文件。"""
    out: list[dict[str, Any]] = []
    for p in iter_workspace_files(WORKSPACE_DIR, extensions=_ARTIFACT_EXTS):
        rel = p.relative_to(WORKSPACE_DIR)
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
      - 持久化 progress_log / tool_history / transcript 到 workspace/.labhandler/runs/<ts>/
      - 扫描 workspace 并将产物列表注入 state.artifacts
      - 向 progress_log 追加 compile 记录（含 partial 标志）
    """
    ts = time.strftime("%Y%m%dT%H%M%S")
    run_d = _run_dir(ts)

    progress = list(state.get("progress_log") or [])
    n_progress = _write_jsonl(run_d / "progress_log.jsonl", progress)

    messages = list(state.get("messages") or [])
    n_tools = _write_jsonl(run_d / "tool_history.jsonl", _extract_tool_history(messages))
    n_msgs = _write_jsonl(run_d / "transcript.jsonl", messages)

    # latest.txt：指向本次运行的 ts，让 cli / 外部工具找到最近的日志目录
    (_meta_dir() / "latest.txt").write_text(ts, encoding="utf-8")

    scanned = _scan_artifacts()
    # 当前 state.artifacts 已是累积的（Annotated[list, add]）；compile 只加 '扫描快照' 作属性区分
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
