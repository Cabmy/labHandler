"""hwHandler CLI - REPL + 命令（PLAN §12 / STEPS P6.1 / P6.3）

启动流程：
1. dotenv 加载 → 检测 profile 占位（hint 但不强制交互）
2. 检测 workspace/ 状态（空 → warning）
3. 进 REPL：每行输入 → "/" 命令分支 / 否则 → 主图 stream
4. 异常兜底：catch + dump CRASH.log

state 管理（用户决策：单进程单任务）：
- 整个进程共享一份 HwState；每条 user 输入累加到 messages / user_constraints
- 第二次起的 REPL 输入由 graph 入口路由直接进 planner（复用 prior intake_result + verifier_runs 做修订）
- /done：归档经验卡片 → workspace 内容 mv .trash/<ts>/ → 重建 sandbox 容器 → 退出 REPL（结束进程）

命令清单：/help /quit /done /show /show summary /skills /profile /remember
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from config.runtime import get_settings

# .env 集中管理在 config/ 目录；显式指定路径，
# 这样从任何 cwd 启动 cli.py 都能读到（不再依赖 cwd == 项目根）。
load_dotenv(Path(__file__).resolve().parent / "config" / ".env")

from rich.console import Console  # noqa: E402
from rich.prompt import Prompt  # noqa: E402

SETTINGS = get_settings()
WORKSPACE_DIR: Path = SETTINGS.workspace_dir
META_DIR: Path = WORKSPACE_DIR / ".hwhandler"
TRASH_DIR: Path = WORKSPACE_DIR.parent / ".trash"

console = Console()


# ─── 输入清洗（零宽字符 / BOM 等不可见 unicode 移除）───────────────
#
# 中文 IME / 终端复制粘贴偶尔会混入零宽空白（U+200B / U+FEFF / U+2060 等），
# 它们对人不可见但被 Python str.split() 当作分隔符 → " ".join() 用普通空格替换 →
# 表现为"丢字"。在 REPL 入口统一清洗一次，所有下游（命令分派 / _run_task / _cmd_remember）
# 都拿到干净串。注意：本函数解决不了 IME partial commit 真把字符吞掉的场景，
# 那是终端 + IME 层面的问题，代码无法修复。

_INVISIBLE_RE = re.compile(
    "[​‌‍⁠﻿]"
    # U+200B ZERO WIDTH SPACE / U+200C ZWNJ / U+200D ZWJ / U+2060 WORD JOINER / U+FEFF BOM
)


def _clean_input(s: str) -> str:
    return _INVISIBLE_RE.sub("", s).strip()


# ─── REPL 持久 event loop───────
#
# 整个 REPL session 共用一个 loop：Ctrl-C 只取消当前 task，loop 不关，
# 缓存的 LLM 客户端继续在同一 loop 上工作，下一个任务接着用。
_REPL_LOOP: asyncio.AbstractEventLoop | None = None


def _get_repl_loop() -> asyncio.AbstractEventLoop:
    global _REPL_LOOP
    if _REPL_LOOP is None or _REPL_LOOP.is_closed():
        _REPL_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_REPL_LOOP)
    return _REPL_LOOP


def _shutdown_repl_loop() -> None:
    """REPL 退出路径：取消 pending tasks → 关 async generators → close loop。

    异常一律吞（退出阶段 best-effort，不影响 bye）。
    """
    global _REPL_LOOP
    if _REPL_LOOP is None or _REPL_LOOP.is_closed():
        return
    try:
        pending = asyncio.all_tasks(_REPL_LOOP)
        for t in pending:
            t.cancel()
        if pending:
            _REPL_LOOP.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        _REPL_LOOP.run_until_complete(_REPL_LOOP.shutdown_asyncgens())
    except Exception:
        pass
    try:
        _REPL_LOOP.close()
    except Exception:
        pass


# ─── state 管理 ────────────────────────────────────────────────────


def _new_state(question: str = "") -> dict[str, Any]:
    return {
        "question": question,
        "iteration": 0,
        "progress_log": [],
        "messages": [],
        "user_constraints": [],
        "verifier_runs": [],
        "artifacts": [],
    }


def _append_user_message(state: dict[str, Any], text: str) -> None:
    state.setdefault("messages", []).append({"role": "user", "content": text})
    state["question"] = text  # 当前请求始终是最新 user 输入
    # 每条 REPL 输入都累加到 user_constraints，让 Verifier 阶段 2 能比对到。
    # HwState.user_constraints 用 Annotated[list[str], add]（state.py:39），多轮自动累加；
    # 重复约束在 Verifier 语义判官那一步会被 LLM 自然 dedup。
    state.setdefault("user_constraints", []).append(text)


# ─── 命令处理 ──────────────────────────────────────────────────────


_HELP_TEXT = """
[bold]hwHandler 命令：[/]
  /help                  本帮助
  /quit                  退出（不沉淀）
  /done                  归档当前任务 → 清场 workspace → 重建 sandbox → 退出
  /show                  打印当前 state 关键字段摘要
  /show summary          cat workspace/SUMMARY.md
  /skills                列出 skills/
  /profile               显示当前 profile（me.yaml）
  /remember <rule>       追加一条用户偏好规则到 profile.preferences.style_rules
                         （注入到 planner / coder / verifier / summarizer 的 system prompt）

直接输入文字即作为本轮 question，发到主图。
首次输入跑完整 intake → planner → ... 链；之后再输入会跳过 intake，
直接进 planner（用 prior intake_result + verifier_runs + 累加的 user_constraints 做修订）。
"""


def _cmd_show(state: dict[str, Any], args: list[str]) -> None:
    if args and args[0] == "summary":
        sp = WORKSPACE_DIR / "SUMMARY.md"
        if sp.exists():
            console.print(sp.read_text(encoding="utf-8"))
        else:
            console.print("[yellow](无 SUMMARY.md，先跑一次任务再看)[/]")
        return
    intake = state.get("intake_result") or {}
    runs = state.get("verifier_runs") or []
    arts = state.get("artifacts") or []
    msgs = state.get("messages") or []
    console.print("[bold]当前 state 摘要：[/]")
    console.print(f"  question      : {state.get('question','')!r}")
    console.print(f"  iteration     : {state.get('iteration', 0)}")
    console.print(f"  intake.type   : {intake.get('type','-')} / title={intake.get('title','-')!r}")
    console.print(f"  messages      : {len(msgs)} 条")
    console.print(f"  artifacts     : {len(arts)} 件")
    console.print(f"  verifier_runs : {[r.get('verdict') for r in runs]}")


def _cmd_skills() -> None:
    from skills.repository import list_skill_documents

    skills = list_skill_documents()
    if not skills:
        console.print("[yellow](skills/ 为空)[/]")
        return
    skills_dir = SETTINGS.skills_dir
    for s in skills:
        path = (skills_dir / s.get("file_name", f"{s['name']}.md")).resolve()
        console.print(f"  • [cyan]{s['name']}[/]  ({path.relative_to(WORKSPACE_DIR.parent)})")


def _cmd_profile() -> None:
    from memory.profile import load_profile

    p = load_profile()
    if not p:
        console.print("[yellow](profile 为空)[/]")
        return
    console.print_json(data=p)


def _cmd_remember(rule: str) -> None:
    """显式追加一条用户偏好规则到 profile.preferences.style_rules（list）。

    取代了早期版本的 LLM 意图判别：用户用 /remember 显式触发，
    不再每条普通输入都付一次 LLM 时延。注入路径见 memory.profile.inject_for_agent。

    入参 rule 是 line 原文中 "/remember " 之后的整段（保留所有空白和零宽字符；
    避免 split + " ".join 折叠多空格 / 零宽空格导致丢字）。
    """
    rule = (rule or "").strip()
    if not rule:
        console.print(
            "[yellow]用法：/remember <rule>"
            "（追加到 profile.preferences.style_rules，影响所有产物 agent 的 system prompt）[/]"
        )
        return
    try:
        from memory.profile import append_rule
        append_rule(rule)
        console.print(f"[green]✓ 已记忆：{rule!r}[/]")
    except Exception as e:
        console.print(f"[red]写入失败：{type(e).__name__}: {e}[/]")


def _cmd_done(state: dict[str, Any]) -> None:
    """归档 → workspace 清场 → 重建 sandbox。调用方应在调完后退出 REPL。

    归档流程：memory.archive.create_task → create_cards → rag.archive_retriever.index_cards
    """
    from memory.archive import get_task_archive
    from rag.archive_retriever import index_cards

    intake = state.get("intake_result") or {}
    title = intake.get("title") or state.get("question") or "未命名任务"
    ttype = intake.get("type") or "other"
    summary_text = state.get("summary") or ""
    knowledge_cards = state.get("knowledge_cards") or []

    try:
        archive = get_task_archive()
        task_id = archive.create_task(title, ttype, summary_text[:4000])
        card_ids = archive.create_cards(task_id, knowledge_cards, title, ttype)
        console.print(f"[green]✓ task_id={task_id}, card_ids={card_ids}[/]")

        if card_ids:
            result = index_cards(card_ids)
            indexed = result.get("indexed", 0)
            failed = result.get("failed", 0)
            if failed:
                console.print(f"[yellow]Chroma 索引失败 {failed}/{len(card_ids)} 张卡片: {result.get('errors', [])}[/]")
            console.print(f"[green]✓ 卡片索引完成：{indexed} 成功, {failed} 失败[/]")
        else:
            console.print("[yellow]无有效知识卡片（跳过索引）[/]")
    except Exception as e:
        console.print(f"[red]归档失败：{type(e).__name__}: {e}[/]")

    # workspace 内容 mv 到 .trash/<ts>
    ts = time.strftime("%Y%m%d_%H%M%S")
    bucket = TRASH_DIR / ts
    bucket.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for p in WORKSPACE_DIR.iterdir():
        target = bucket / p.name
        shutil.move(str(p), str(target))
        moved.append(p.name)
    console.print(
        f"[green]✓ workspace 清场，{len(moved)} 件 mv 到 "
        f"{bucket.relative_to(WORKSPACE_DIR.parent)}[/]"
    )

    # 一并重建 sandbox 容器，清掉 pip 全局包 / /tmp / 长跑进程残留
    # ensure_sandbox 同步阻塞等就绪（最多 60s）；失败也让 REPL 正常退出
    try:
        from infra.sandbox_boot import recreate_sandbox
        ok = recreate_sandbox(log=lambda m: console.print(f"[dim]{m}[/]"))
        if ok:
            console.print("[green]✓ sandbox 容器已重建[/]")
        else:
            console.print(
                "[yellow]sandbox 重建未就绪；下次启动 cli 时会再次自检[/]"
            )
    except Exception as e:
        console.print(f"[yellow]sandbox 重建失败：{type(e).__name__}: {e}[/]")


# ─── 主图 stream + CRASH 兜底 ──────────────────────────────────────


def _crash_dump(exc: BaseException, state: dict[str, Any]) -> Path:
    META_DIR.mkdir(parents=True, exist_ok=True)
    crash = META_DIR / "CRASH.log"
    with crash.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        f.write(f"{type(exc).__name__}: {exc}\n")
        f.write(traceback.format_exc())
        f.write("\n--- state snapshot ---\n")
        try:
            f.write(json.dumps(state, ensure_ascii=False, default=str)[:5000] + "\n")
        except Exception:
            f.write("(state dump failed)\n")
    return crash


def _get_intake_reject_msg(exc: BaseException) -> str | None:
    cur: BaseException | None = exc
    while cur is not None:
        if type(cur).__name__ == "IntakeRejectError":
            return str(cur)
        cur = cur.__cause__
    return None


def _run_task(state: dict[str, Any], user_input: str) -> dict[str, Any]:
    """跑一次主图，stream 节点事件，异常 dump CRASH.log

    stream_graph 现在是 async（主图含 async 节点 run_coder，需走 graph.astream
    才能让所有节点的 LLM token 流通过 stream_mode='messages' 正确透传）。
    用 REPL 持久 loop（_get_repl_loop）跑 run_until_complete，避免每次 asyncio.run
    新建/关闭 loop 导致缓存的 ChatOpenAI httpx 回调对死循环刷屏。
    """
    from orchestrator import get_graph
    from ui import print_completion_panel, print_crash_panel, stream_graph

    _append_user_message(state, user_input)
    # refine 路径（state 已有 prior 任务的 verifier_runs）：把 iteration 重置回 0，
    # 让 planner.iteration += 1 后仍在 MAX_REPLAN_ITER 预算内（否则首轮跑满后第二次输入会立刻撞顶）。
    if state.get("verifier_runs"):
        state["iteration"] = 0
    graph = get_graph()
    try:
        loop = _get_repl_loop()
        new_state = loop.run_until_complete(stream_graph(graph, state))
        print_completion_panel(new_state)
        return new_state
    except KeyboardInterrupt:
        console.print("\n[yellow](已中断本次任务，state 保留)[/]")
        return state
    except Exception as e:
        reject_msg = _get_intake_reject_msg(e)
        if reject_msg:
            console.print(f"\n[yellow]💡 提示: {reject_msg}[/]")
            sys.exit(1)
            
        crash = _crash_dump(e, state)
        print_crash_panel(e, crash)
        return state


# ─── 启动检查 ──────────────────────────────────────────────────────


def _startup_checks() -> None:
    # AIO Sandbox 容器：未跑则自动拉起（HW_AUTOSTART_SANDBOX=false 可禁用）
    try:
        from infra.sandbox_boot import ensure_sandbox
        ensure_sandbox(log=lambda m: console.print(f"[dim]{m}[/]"))
    except Exception as e:
        console.print(f"[yellow]sandbox 自动启动检查失败（已跳过）：{e}[/]")

    # profile 占位 → 交互式补全（仅 identity；空值跳过）
    try:
        from memory.profile import load_profile, update_field
        p = load_profile()
        identity = (p or {}).get("identity") or {}
        name_default = identity.get("name") == "张三"
        sid_default = identity.get("student_id", "").startswith("2021xxx")
        if name_default or sid_default:
            console.print(
                "[yellow]检测到 profile/me.yaml 仍是占位默认；"
                "请补全身份信息（直接回车跳过该字段，稍后可 /profile 查看或手动编辑）。[/]"
            )
            if name_default:
                new_name = Prompt.ask("姓名", default="").strip()
                if new_name:
                    try:
                        update_field("identity.name", new_name)
                    except Exception as e:
                        console.print(f"[red]写入 name 失败：{e}[/]")
            if sid_default:
                new_sid = Prompt.ask("学号", default="").strip()
                if new_sid:
                    try:
                        update_field("identity.student_id", new_sid)
                    except Exception as e:
                        console.print(f"[red]写入 student_id 失败：{e}[/]")
    except Exception:
        pass

    if not WORKSPACE_DIR.exists():
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    items = [p for p in WORKSPACE_DIR.iterdir() if p.name != ".hwhandler"]
    if not items:
        console.print(
            "[yellow]提示：workspace/ 当前为空。"
            "把作业说明（README.md / 实验指导.md / .pdf 等）丢进去再开始。[/]"
        )


# ─── REPL 主循环 ───────────────────────────────────────────────────


def repl() -> None:
    console.print(
        "[bold cyan]hwHandler[/] · type [bold]/help[/] for commands · "
        "[dim]Ctrl-D / /quit to exit[/]"
    )
    _startup_checks()
    state: dict[str, Any] = _new_state()

    try:
        while True:
            try:
                line = _clean_input(Prompt.ask("[bold]›[/]"))
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]bye[/]")
                return

            if not line:
                continue

            if line.startswith("/"):
                parts = line[1:].split()
                cmd = parts[0] if parts else ""
                args = parts[1:]
                if cmd == "quit":
                    console.print("[dim]bye[/]")
                    return
                elif cmd == "help":
                    console.print(_HELP_TEXT)
                elif cmd == "show":
                    _cmd_show(state, args)
                elif cmd == "skills":
                    _cmd_skills()
                elif cmd == "profile":
                    _cmd_profile()
                elif cmd == "remember":
                    # 用 line 原文截取 "/remember " 之后的整段，绕开 split+join 对零宽空白 / 多空格的折叠
                    _cmd_remember(line[len("/remember"):].lstrip())
                elif cmd == "done":
                    _cmd_done(state)
                    console.print("[dim]bye[/]")
                    return  # 单进程单任务：/done 后退出 REPL，finally 走 _shutdown_repl_loop
                else:
                    console.print(f"[yellow]未知命令：/{cmd}（试试 /help）[/]")
                continue

            # 普通 user 输入：直接发到主图（偏好显式用 /remember 写入 profile，不再 LLM 判意图）
            state = _run_task(state, line)
    finally:
        # REPL 任意路径退出（正常 /quit / Ctrl-D / 异常）都走这里关 loop，
        # 取消 pending tasks + 关 async generators，避免主程序退出后还在 trace。
        _shutdown_repl_loop()


if __name__ == "__main__":
    try:
        repl()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
