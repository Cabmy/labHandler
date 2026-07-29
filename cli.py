"""labHandler CLI - REPL + 命令

启动流程：
1. dotenv 加载 → 检测 profile 占位（hint 但不强制交互）
2. 检测 workspace/ 状态（空 → warning）
3. 进 REPL：每行输入 → "/" 命令分支 / 否则 → 主图 stream
4. 异常兜底：catch + dump CRASH.log

state 管理：
- 一个 lab 周期内共享一个 TaskSession（一份 HwState）；每条 user 输入累加到 messages / user_constraints
- 第二次起的 REPL 输入由 graph 入口路由直接进 planner（复用 prior intake_result + verifier_runs 做修订）
- /done：归档经验卡片 → workspace 内容 mv .trash/<ts>/ → 重建 sandbox 容器 → 会话复位开新 lab（进程常驻）

命令清单：/help /quit /done /dream /edit_skill /skills /profile /remember
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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

from orchestrator.session import TaskSession  # noqa: E402

SETTINGS = get_settings()
WORKSPACE_DIR: Path = SETTINGS.workspace_dir
META_DIR: Path = WORKSPACE_DIR / ".labhandler"

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


# ─── 命令处理 ──────────────────────────────────────────────────────


_HELP_TEXT = """
[bold]labHandler 命令：[/]
  /help                  本帮助
  /quit                  退出（不沉淀）
  /done                  归档当前任务 → 清场 workspace → 重建 sandbox → 开启新会话
  /dream                 离线治理归档知识卡片（LLM 合并/去重/淘汰 + 重建索引）
  /edit_skill <skill> <指令>
                         自然语言编辑现有 skill（仅 coding/essay/lab_report）：
                         LLM 产出修改 → 展示 diff → 确认后落盘；指令中提及
                         workspace 内文件名可作为文风样本供其学习
  /skills                列出 skills/
  /profile               显示当前 profile（me.yaml）
  /remember <rule>       追加一条用户偏好规则到 profile.preferences.style_rules
                         （注入到 planner / coder / verifier / summarizer 的 system prompt）

直接输入文字即作为本轮 question，发到主图。
首次输入跑完整 intake → planner → ... 链；之后再输入会跳过 intake，
直接进 planner（用 prior intake_result + verifier_runs + 累加的 user_constraints 做修订）。
"""


def _cmd_skills() -> None:
    from skills.repository import list_skill_documents

    skills = list_skill_documents()
    if not skills:
        console.print("[yellow](skills/ 为空)[/]")
        return
    skills_dir = SETTINGS.skills_dir
    for s in skills:
        path = (skills_dir / s["file_name"]).resolve()
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


def _cmd_dream() -> None:
    """离线知识治理（P3-2）：按 (card_type, task_type) 分组 LLM 判定合并/淘汰 → 软删 + 重建索引。"""
    from memory.dream import run_dream

    console.print("[dim]开始离线治理归档卡片（每组一次 LLM 调用，可能需要一会儿）…[/]")
    try:
        report = run_dream()
    except Exception as e:
        console.print(f"[red]/dream 失败：{type(e).__name__}: {e}[/]")
        return
    console.print(
        f"[green]✓ 治理完成：{report['total_cards']} 卡 / {report['groups']} 组，"
        f"判定 {report['judged']} 组 → 新增合并卡 {report['merged_created']}，"
        f"软删 {report['retired']}[/]"
    )
    if report.get("reindex"):
        ri = report["reindex"]
        console.print(f"[green]✓ 索引重建：{ri.get('indexed', 0)}/{ri.get('total', 0)} 成功[/]")
    for s in report.get("promotion_suggestions") or []:
        console.print(f"[cyan]💡 晋升建议：{s}[/]")
    for err in report.get("errors") or []:
        console.print(f"[yellow]组治理失败：{err}[/]")


def _cmd_edit_skill(rest: str) -> None:
    """/edit_skill：LLM 编辑判官改写现有 skill → 终端 diff → y/n 确认 → 落盘。

    入参 rest 是 line 原文中 "/edit_skill " 之后的整段（保留空白，同 /remember 模式）；
    首 token 为 skill 名，余下为自然语言指令。
    """
    from skills.editor import apply_edit, existing_skill_names, propose_edit

    rest = rest.strip()
    parts = rest.split(maxsplit=1)
    available = existing_skill_names()
    if len(parts) < 2 or parts[0] not in available:
        console.print(
            "[yellow]用法：/edit_skill <skill> <自然语言指令>"
            f"（可编辑的 skill：{available}，不支持新增）[/]"
        )
        return
    skill_name, instruction = parts[0], parts[1]

    console.print("[dim]编辑判官分析中（一次 LLM 调用，可能需要一会儿）…[/]")
    try:
        proposal = propose_edit(skill_name, instruction)
    except Exception as e:
        console.print(f"[red]/edit_skill 失败：{type(e).__name__}: {e}[/]")
        return

    if proposal.get("style_samples"):
        console.print(f"[cyan]已读取文风样本：{proposal['style_samples']}[/]")
    for fail in proposal.get("sample_failures") or []:
        console.print(f"[yellow]⚠️ 文风样本未用上：{fail}[/]")
    if proposal.get("summary"):
        console.print(f"[bold]提案说明：[/]{proposal['summary']}")
    if not proposal["operations"]:
        console.print("[yellow]判官认为无需改动（operations 为空）。[/]")
        return

    from rich.panel import Panel
    from rich.syntax import Syntax
    for d in proposal["diffs"]:
        console.print(Panel(
            Syntax(d["diff"], "diff", word_wrap=True),
            title=f"skills/{skill_name}/{d['file']}",
            border_style="cyan",
        ))

    if Prompt.ask("应用以上修改？", choices=["y", "n"], default="n") != "y":
        console.print("[dim]已取消，未落盘。[/]")
        return
    try:
        report = apply_edit(skill_name, proposal["operations"])
        console.print(f"[green]✓ 已应用：{report['applied']}[/]")
    except Exception as e:
        console.print(f"[red]落盘失败（未应用或部分应用，请检查 git diff）：{type(e).__name__}: {e}[/]")


def _cmd_done(session: TaskSession) -> None:
    """归档 → 清场 → 重建 sandbox → 会话复位（逻辑等效进程重启，REPL 继续）。

    复位序列在 orchestrator/session.py:reset（与 Web 端共用）；本函数只做终端呈现。
    """
    report = session.reset(log=lambda m: console.print(f"[dim]{m}[/]"))

    result = report["archive"]
    if result.get("error"):
        console.print(f"[red]归档失败：{result['error']}[/]")
    else:
        console.print(f"[green]✓ task_id={result['task_id']}, card_ids={result['card_ids']}[/]")
        if result.get("card_ids"):
            failed = result.get("failed", 0)
            if failed:
                console.print(
                    f"[yellow]Chroma 索引失败 {failed}/{len(result['card_ids'])} 张卡片: "
                    f"{result.get('errors', [])}[/]"
                )
            console.print(
                f"[green]✓ 卡片索引完成：{result.get('indexed', 0)} 成功, {failed} 失败[/]"
            )
        else:
            console.print("[yellow]无有效知识卡片（跳过索引）[/]")

    console.print(
        f"[green]✓ workspace 清场，{len(report['moved'])} 件 mv 到 "
        f"{Path(report['trashed_to']).relative_to(WORKSPACE_DIR.parent)}[/]"
    )

    sandbox = report["sandbox"]
    if sandbox == "ok":
        console.print("[green]✓ sandbox 容器已重建[/]")
    else:
        console.print(f"[yellow]sandbox 重建未就绪（{sandbox}）；下次任务前会再次自检[/]")

    console.print("[bold cyan]✓ 新会话已就绪，可直接开始下一个 lab（先往 workspace/ 放材料）[/]")


# ─── 主图 stream + CRASH 兜底 ──────────────────────────────────────


def _maybe_resume_last_task(session: TaskSession) -> None:
    """启动时断点检测：上次进程崩溃/被 kill 时 checkpoint 停在非 END 节点 → 询问是否续跑。

    设计哲学是会话级隔离（/done 复位会话、进程常驻），崩溃/被 kill 一定伴随进程死亡，
    故不设常驻 resume 命令——续跑只发生在启动时刻。
    用户确认后 graph.astream(None, config) 从断点继续；结束后用 graph.aget_state
    取权威全量 state 回填 session（stream 的合并结果只含续跑期间的 diff）。
    """
    from orchestrator import get_graph
    from orchestrator.session import find_resumable_thread
    from ui import print_completion_panel, print_crash_panel, stream_graph

    loop = _get_repl_loop()

    async def _find():
        # get_graph 必须在 loop 内调（AsyncSqliteSaver 绑定 running loop）
        return get_graph(), await find_resumable_thread(get_graph())

    try:
        graph, info = loop.run_until_complete(_find())
    except Exception:
        return
    if not info:
        return
    tid, next_nodes = info
    console.print(
        f"[yellow]检测到未完成任务（thread={tid}，停在 {'、'.join(next_nodes)}）。[/]"
    )
    if Prompt.ask("是否从断点续跑？", choices=["y", "n"], default="n") != "y":
        return
    session.thread_id = tid
    try:
        loop.run_until_complete(
            stream_graph(graph, None, config=session.run_config())
        )
        snap = loop.run_until_complete(graph.aget_state(session.run_config()))
        values = getattr(snap, "values", None) or {}
        if values:
            session.state = dict(values)
        print_completion_panel(session.state)
    except KeyboardInterrupt:
        console.print("\n[yellow](已中断续跑，checkpoint 保留，下次启动可再续)[/]")
    except Exception as e:
        crash = _crash_dump(e, session.state)
        print_crash_panel(e, crash)


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


def _run_task(session: TaskSession, user_input: str) -> None:
    """跑一次主图，stream 节点事件，异常 dump CRASH.log

    stream_graph 现在是 async（主图含 async 节点 run_coder，需走 graph.astream
    才能让所有节点的 LLM token 流通过 stream_mode='messages' 正确透传）。
    用 REPL 持久 loop（_get_repl_loop）跑 run_until_complete，避免每次 asyncio.run
    新建/关闭 loop 导致缓存的 ChatOpenAI httpx 回调对死循环刷屏。
    """
    from orchestrator import get_graph
    from ui import print_completion_panel, print_crash_panel, stream_graph

    # 任务前沙箱自检：/done 重建失败 / 容器中途被停时在此拉起（端口已通则仅一次探活，毫秒级）
    try:
        from infra.sandbox_boot import ensure_sandbox
        ensure_sandbox(log=lambda m: console.print(f"[dim]{m}[/]"))
    except Exception as e:
        console.print(f"[yellow]沙箱自检异常（继续尝试任务）：{type(e).__name__}: {e}[/]")

    state = session.prepare_task(user_input)

    async def _go() -> dict[str, Any]:
        # get_graph 必须在 loop 内调：首次编译时创建 AsyncSqliteSaver（绑定 running loop）
        graph = get_graph()
        return await stream_graph(graph, state, config=session.run_config())

    try:
        loop = _get_repl_loop()
        new_state = loop.run_until_complete(_go())
        session.state = new_state
        print_completion_panel(new_state)
    except KeyboardInterrupt:
        console.print("\n[yellow](已中断本次任务，state 保留)[/]")
    except Exception as e:
        # 沙箱致命错误（agents.errors.SandboxFatalError）：图节点只抛异常，
        # 进程退出决策收归 CLI 层——打印修复指引后退出
        from agents.errors import SandboxFatalError
        cur: BaseException | None = e
        while cur is not None:
            if isinstance(cur, SandboxFatalError):
                console.print(
                    "\n[bold red][SANDBOX_FATAL][/] 沙箱连续不可用，任务无法继续。\n"
                    "  请检查容器状态：docker ps -a | grep aio-sandbox\n"
                    "  重启命令：docker rm -f aio-sandbox && python cli.py\n"
                    f"  失败详情：{cur.detail}"
                )
                sys.exit(1)
            cur = cur.__cause__

        reject_msg = _get_intake_reject_msg(e)
        if reject_msg:
            # 不退进程（会话级隔离）：用户补完材料后直接重试即可
            console.print(f"\n[yellow]💡 提示: {reject_msg}[/]")
            return

        crash = _crash_dump(e, state)
        print_crash_panel(e, crash)


# ─── 启动检查 ──────────────────────────────────────────────────────


def _startup_checks() -> None:
    # AIO Sandbox 容器：未跑则自动拉起（LAB_AUTOSTART_SANDBOX=false 可禁用）
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

    items = [p for p in WORKSPACE_DIR.iterdir() if p.name != ".labhandler"]
    if not items:
        console.print(
            "[yellow]提示：workspace/ 当前为空。"
            "把作业说明（README.md / 实验指导.md / .pdf 等）丢进去再开始。[/]"
        )


# ─── REPL 主循环 ───────────────────────────────────────────────────


def _print_startup_banner() -> None:
    """开屏信息框（Codex 风格）：模型 / workspace / 常用操作提示。"""
    from rich.panel import Panel
    from rich.text import Text

    provider = os.getenv("LLM_PROVIDER", "paratera")
    if provider == "ollama":
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
    else:
        model = os.getenv("PARATERA_LLM_MODEL", "DeepSeek-V4-Pro")

    body = Text()
    body.append("labHandler", style="bold")
    body.append("  把作业材料丢进 workspace/，剩下的交给 agent\n\n", style="dim")
    body.append("  model      ", style="dim")
    body.append(f"{model} ({provider})\n")
    body.append("  workspace  ", style="dim")
    body.append(f"{WORKSPACE_DIR}\n")
    body.append("  /help 查看命令 · Ctrl-D 或 /quit 退出", style="dim")
    console.print(Panel(body, border_style="dim", padding=(1, 2), expand=False))


def repl() -> None:
    _print_startup_banner()
    _startup_checks()
    session = TaskSession()

    # 断点检测：有未跑完的任务则询问是否续跑（不设常驻 resume 命令）
    _maybe_resume_last_task(session)

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
                if cmd == "quit":
                    console.print("[dim]bye[/]")
                    return
                elif cmd == "help":
                    console.print(_HELP_TEXT)
                elif cmd == "dream":
                    _cmd_dream()
                elif cmd == "edit_skill":
                    # 用 line 原文截取 "/edit_skill " 之后的整段，保留指令中的空白
                    _cmd_edit_skill(line[len("/edit_skill"):].lstrip())
                elif cmd == "skills":
                    _cmd_skills()
                elif cmd == "profile":
                    _cmd_profile()
                elif cmd == "remember":
                    # 用 line 原文截取 "/remember " 之后的整段，绕开 split+join 对零宽空白 / 多空格的折叠
                    _cmd_remember(line[len("/remember"):].lstrip())
                elif cmd == "done":
                    _cmd_done(session)
                    # 会话已复位（逻辑等效进程重启），REPL 继续接下一个 lab
                else:
                    console.print(f"[yellow]未知命令：/{cmd}（试试 /help）[/]")
                continue

            # 普通 user 输入：直接发到主图（偏好显式用 /remember 写入 profile，不再 LLM 判意图）
            _run_task(session, line)
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
