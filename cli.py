"""labHandler CLI - REPL + 命令

启动流程：
1. 加载 dotenv -> 检查 profile 占位符（提示，不强制）
2. 检查 workspace/ 状态（空 -> 警告）
3. 进入 REPL：每行 -> "/" 命令分支 / 其余 -> 主图流式
4. 异常兜底：转储 CRASH.log

状态管理：
- 一个 lab 周期内共享一个 TaskSession（一份 HwState）；每条用户输入累加到 messages / user_constraints
- 第二条及以后的 REPL 输入直接路由到 planner（复用先前 intake_result + verifier_runs 做修订）
- /done：归档经验卡片 -> workspace 内容 mv 到 .trash/<ts>/ -> 重建沙箱容器 -> 复位会话迎接新 lab（进程常驻）

命令：/help /quit /done /dream /edit_skill /skills /profile /remember
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

# .env 集中于 config/ 管理；显式路径使 cli.py 在任意 cwd 下可跑。
load_dotenv(Path(__file__).resolve().parent / "config" / ".env")

from rich.console import Console  # noqa: E402
from rich.prompt import Prompt  # noqa: E402

from orchestrator.session import TaskSession  # noqa: E402

SETTINGS = get_settings()
WORKSPACE_DIR: Path = SETTINGS.workspace_dir
META_DIR: Path = WORKSPACE_DIR / ".labhandler"

console = Console()


# ─── 输入清洗（去除零宽 / BOM 不可见 unicode） ────────
#
# 中文输入法 / 终端复制粘贴可能注入零宽空白
# （U+200B / U+FEFF / U+2060 等）。人眼不可见但被 str.split() 视为
# 分隔符 -> " ".join() 替换为普通空格 ->
# 表现为“丢字”。在 REPL 入口清洗一次，使所有
# 下游（命令分发 / _run_task / _cmd_remember）拿到干净串。

_INVISIBLE_RE = re.compile(
    "[​‌‍⁠﻿]"
    # U+200B ZERO WIDTH SPACE / U+200C ZWNJ / U+200D ZWJ / U+2060 WORD JOINER / U+FEFF BOM
)


def _clean_input(s: str) -> str:
    return _INVISIBLE_RE.sub("", s).strip()


# ─── REPL 常驻事件循环 ───────
#
# 整个 REPL 会话共享一个 loop：Ctrl-C 只取消当前
# 任务，loop 保持打开，缓存的 LLM 客户端继续在同一个 loop 上跑。
_REPL_LOOP: asyncio.AbstractEventLoop | None = None


def _get_repl_loop() -> asyncio.AbstractEventLoop:
    global _REPL_LOOP
    if _REPL_LOOP is None or _REPL_LOOP.is_closed():
        _REPL_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_REPL_LOOP)
    return _REPL_LOOP


def _shutdown_repl_loop() -> None:
    """REPL 退出路径：取消待处理任务 -> 关闭异步生成器 -> 关闭 loop。

    所有异常吞掉（关闭期间尽力而为）。
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


# ─── 命令处理器 ──────────────────────────────────────────────────

# 命令注册表：简单命令（无参数提取）映射到处理器。
# 需原始行参数提取的命令（edit_skill、remember）
# 在查此字典前先行处理。
_COMMANDS: dict[str, Any] = {}

def _build_help_text() -> str:
    """从命令注册表动态构建帮助文本。"""
    cmds = sorted(_COMMANDS.keys())
    lines = ["[bold]labHandler 命令：[/]"]
    descriptions = {
        "quit": "退出（不归档）",
        "help": "本帮助",
        "done": "归档当前任务 -> 清空 workspace -> 重建沙箱 -> 新会话",
        "dream": "离线知识治理（LLM 合并/去重/淘汰 + 重建索引）",
        "skills": "列出 skills/",
        "profile": "显示当前 profile（me.yaml）",
    }
    for cmd in cmds:
        desc = descriptions.get(cmd, "")
        lines.append(f"  /{cmd:<20s} {desc}")
    lines.append("  /edit_skill <skill> <指令>")
    lines.append("                       经 LLM 判官编辑现有 skill")
    lines.append("  /remember <规则>     向 profile 追加用户偏好规则")
    lines.append("")
    lines.append("直接输入文本即发送到主图。")
    lines.append("首次输入跑完整 intake -> planner -> ... 链路；")
    lines.append("后续输入跳过 intake，直达 planner（修订模式）。")
    return "\n".join(lines)


def _cmd_quit() -> None:
    """退出 REPL，不归档。"""
    # 哨兵返回值；调用方检查 quit_requested
    pass


def _cmd_help() -> None:
    """显示动态帮助文本。"""
    console.print(_build_help_text())


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
    """显式向 profile.preferences.style_rules 追加一条用户偏好规则。

    参数 rule 为 "/remember " 后的原始文本（保留空白）。
    """
    rule = (rule or "").strip()
    if not rule:
        console.print(
            "[yellow]用法：/remember <规则>"
            "（追加到 profile.preferences.style_rules，影响所有 agent system prompt）[/]"
        )
        return
    try:
        from memory.profile import append_rule
        append_rule(rule)
        console.print(f"[green]\u2713 已记住：{rule!r}[/]")
    except Exception as e:
        console.print(f"[red]写入失败：{type(e).__name__}: {e}[/]")


def _cmd_dream() -> None:
    """离线知识治理：按 (card_type, task_type) 分组 LLM 合并/淘汰 + 重建索引。"""
    from memory.dream import run_dream

    console.print("[dim]开始离线治理归档卡片（每组一次 LLM 调用，可能耗时）...[/]")
    try:
        report = run_dream()
    except Exception as e:
        console.print(f"[red]/dream 失败：{type(e).__name__}: {e}[/]")
        return
    console.print(
        f"[green]\u2713 治理完成：{report['total_cards']} 张卡片 / {report['groups']} 组，"
        f"判定 {report['judged']} 组 -> 新合并卡片 {report['merged_created']} 张，"
        f"淘汰 {report['retired']} 张[/]"
    )
    if report.get("reindex"):
        ri = report["reindex"]
        console.print(f"[green]\u2713 索引重建：{ri.get('indexed', 0)}/{ri.get('total', 0)} 成功[/]")
    for s in report.get("promotion_suggestions") or []:
        console.print(f"[cyan]\U0001f4a1 晋升建议：{s}[/]")
    for err in report.get("errors") or []:
        console.print(f"[yellow]分组治理失败：{err}[/]")


def _cmd_edit_skill(rest: str) -> None:
    """/edit_skill：LLM 判官重写现有 skill -> 终端 diff -> y/n 确认 -> 落盘。

    参数 rest 为 "/edit_skill " 后的原始文本（保留空白，与 /remember 同模式）；
    首个 token 为 skill 名，其余为自然语言指令。
    """
    from skills.editor import apply_edit, existing_skill_names, propose_edit

    rest = rest.strip()
    parts = rest.split(maxsplit=1)
    available = existing_skill_names()
    if len(parts) < 2 or parts[0] not in available:
        console.print(
            "[yellow]用法：/edit_skill <skill> <自然语言指令>"
            f"（可编辑 skill：{available}，不支持新增）[/]"
        )
        return
    skill_name, instruction = parts[0], parts[1]
    
    console.print("[dim]编辑判官分析中（一次 LLM 调用，可能耗时）...[/]")
    try:
        proposal = propose_edit(skill_name, instruction)
    except Exception as e:
        console.print(f"[red]/edit_skill 失败：{type(e).__name__}: {e}[/]")
        return
    
    if proposal.get("style_samples"):
        console.print(f"[cyan]已读文风样本：{proposal['style_samples']}[/]")
    for fail in proposal.get("sample_failures") or []:
        console.print(f"[yellow]\u26a0\ufe0f 文风样本未使用：{fail}[/]")
    if proposal.get("summary"):
        console.print(f"[bold]提案摘要：[/]{proposal['summary']}")
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

    if Prompt.ask("是否应用上述改动？", choices=["y", "n"], default="n") != "y":
        console.print("[dim]已取消，未落盘任何改动。[/]")
        return
    try:
        report = apply_edit(skill_name, proposal["operations"])
        console.print(f"[green]\u2713 已应用：{report['applied']}[/]")
    except Exception as e:
        console.print(f"[red]落盘失败（部分或未应用，查 git diff）：{type(e).__name__}: {e}[/]")


def _cmd_done(session: TaskSession) -> None:
    """归档 -> 清理 -> 重建沙箱 -> 复位会话（逻辑等效进程重启，REPL 继续）。

    复位序列在 orchestrator/session.py:reset（与 Web 共用）；本函数只处理终端呈现。
    """
    report = session.reset(log=lambda m: console.print(f"[dim]{m}[/]"))

    result = report["archive"]
    if result.get("error"):
        console.print(f"[red]归档失败：{result['error']}[/]")
    else:
        console.print(f"[green]\u2713 task_id={result['task_id']}, card_ids={result['card_ids']}[/]")
        if result.get("card_ids"):
            failed = result.get("failed", 0)
            if failed:
                console.print(
                    f"[yellow]Chroma 索引失败 {failed}/{len(result['card_ids'])} 张卡片："
                    f"{result.get('errors', [])}[/]"
                )
            console.print(
                f"[green]\u2713 卡片索引：{result.get('indexed', 0)} 成功，{failed} 失败[/]"
            )
        else:
            console.print("[yellow]无有效知识卡片（跳过索引）[/]")
    
    console.print(
        f"[green]\u2713 workspace 已清空，{len(report['moved'])} 项 mv 到 "
        f"{Path(report['trashed_to']).relative_to(WORKSPACE_DIR.parent)}[/]"
    )
    
    sandbox = report["sandbox"]
    if sandbox == "ok":
        console.print("[green]\u2713 沙箱容器已重建[/]")
    else:
        console.print(f"[yellow]沙箱重建未就绪（{sandbox}）；下个任务前会重试[/]")
    
    console.print("[bold cyan]\u2713 新会话就绪，开始下一个 lab（先把材料放进 workspace/）[/]")


# ─── 主图流式 + CRASH 兜底 ──────────────────────────────────


def _maybe_resume_last_task(session: TaskSession) -> None:
    """启动断点检测：若 checkpoint 停在非 END 节点（进程崩溃/被 kill），询问是否续跑。

    设计哲学：会话级隔离（/done 复位会话，进程常驻），
    崩溃/kill 总伴随进程死亡，故不设常驻续跑命令——续跑
    只在启动时发生。用户确认后 graph.astream(None, config) 从
    checkpoint 继续；随后 graph.aget_state 以权威全态回填会话。
    """
    from orchestrator import get_graph
    from orchestrator.session import find_resumable_thread
    from ui import print_completion_panel, print_crash_panel, stream_graph

    loop = _get_repl_loop()

    async def _find():
        # get_graph 须在 loop 内调用（AsyncSqliteSaver 绑定 running loop）
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
        console.print("\n[yellow]（续跑被中断，checkpoint 保留，下次启动可再续）[/]")
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
        f.write("\n--- state 快照 ---\n")
        try:
            f.write(json.dumps(state, ensure_ascii=False, default=str)[:5000] + "\n")
        except Exception:
            f.write("(state 转储失败)\n")
    return crash


def _get_intake_reject_msg(exc: BaseException) -> str | None:
    cur: BaseException | None = exc
    while cur is not None:
        if type(cur).__name__ == "IntakeRejectError":
            return str(cur)
        cur = cur.__cause__
    return None


def _run_task(session: TaskSession, user_input: str) -> None:
    """跑一次主图，流式输出节点事件，异常时转储 CRASH.log。

    stream_graph 为 async（graph 含异步节点 run_coder，须用 graph.astream
    经 stream_mode='messages' 做 LLM token 流式）。用 REPL 常驻 loop
    （_get_repl_loop）+ run_until_complete，避免每次 asyncio.run 创建/关闭 loop
    导致缓存的 ChatOpenAI httpx 回调死循环刷屏。
    """
    from orchestrator import get_graph
    from ui import print_completion_panel, print_crash_panel, stream_graph

    # 任务前沙箱自检：/done 重建失败 / 容器中途停掉时在此报错
    try:
        from infra.sandbox_boot import ensure_sandbox
        ensure_sandbox(log=lambda m: console.print(f"[dim]{m}[/]"))
    except Exception as e:
        console.print(f"[yellow]沙箱自检失败（继续跑任务）：{type(e).__name__}: {e}[/]")

    state = session.prepare_task(user_input)

    async def _go() -> dict[str, Any]:
        # get_graph 须在 loop 内调用：首次编译创建 AsyncSqliteSaver（绑定 running loop）
        graph = get_graph()
        return await stream_graph(graph, state, config=session.run_config())

    try:
        loop = _get_repl_loop()
        new_state = loop.run_until_complete(_go())
        session.state = new_state
        print_completion_panel(new_state)
    except KeyboardInterrupt:
        console.print("\n[yellow]（任务被中断，状态保留）[/]")
    except Exception as e:
        # 沙箱致命错误（agents.errors.SandboxFatalError）：graph 节点只抛，
        # 进程退出决策属于 CLI 层——打印修复指引后退出
        from agents.errors import SandboxFatalError
        cur: BaseException | None = e
        while cur is not None:
            if isinstance(cur, SandboxFatalError):
                console.print(
                    "\n[bold red][SANDBOX_FATAL][/] 沙箱持续不可用，任务无法继续。\n"
                    "  检查容器状态：docker ps -a | grep aio-sandbox\n"
                    "  重启命令：docker rm -f aio-sandbox && python cli.py\n"
                    f"  失败详情：{cur.detail}"
                )
                sys.exit(1)
            cur = cur.__cause__

        reject_msg = _get_intake_reject_msg(e)
        if reject_msg:
            # 不退出进程（会话级隔离）：用户补充材料后可重试
            console.print(f"\n[yellow]\U0001f4a1 提示：{reject_msg}[/]")
            return

        crash = _crash_dump(e, state)
        print_crash_panel(e, crash)


# ─── 启动检查 ──────────────────────────────────────────────────


def _startup_checks() -> None:
    # AIO Sandbox 容器：未跑时自动拉起（LAB_AUTOSTART_SANDBOX=false 可禁）
    try:
        from infra.sandbox_boot import ensure_sandbox
        ensure_sandbox(log=lambda m: console.print(f"[dim]{m}[/]"))
    except Exception as e:
        console.print(f"[yellow]沙箱自启检查失败（已跳过）：{e}[/]")

    # profile 占位符 -> 交互式补全（仅身份；空则跳过）
    try:
        from memory.profile import load_profile, update_field
        p = load_profile()
        identity = (p or {}).get("identity") or {}
        name_default = identity.get("name") == "\u5f20\u4e09"
        sid_default = identity.get("student_id", "").startswith("2021xxx")
        if name_default or sid_default:
            console.print(
                "[yellow]profile/me.yaml 仍为占位默认值；"
                "请补全身份信息（回车跳过，稍后用 /profile 查看或手动编辑）。[/]"
            )
            if name_default:
                new_name = Prompt.ask("姓名", default="").strip()
                if new_name:
                    try:
                        update_field("identity.name", new_name)
                    except Exception as e:
                        console.print(f"[red]姓名写入失败：{e}[/]")
            if sid_default:
                new_sid = Prompt.ask("学号", default="").strip()
                if new_sid:
                    try:
                        update_field("identity.student_id", new_sid)
                    except Exception as e:
                        console.print(f"[red]学号写入失败：{e}[/]")
    except Exception:
        pass

    if not WORKSPACE_DIR.exists():
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    items = [p for p in WORKSPACE_DIR.iterdir() if p.name != ".labhandler"]
    if not items:
        console.print(
            "[yellow]提示：workspace/ 当前为空。"
            "把作业材料（README.md / 实验指导.md / .pdf 等）放进去即可开始。[/]"
        )


# ─── REPL 主循环 ─────────────────────────────────────────────────


def _print_startup_banner() -> None:
    """启动信息面板（Codex 风格）：模型 / workspace / 常用操作。"""
    from rich.panel import Panel
    from rich.text import Text

    provider = os.getenv("LLM_PROVIDER", "paratera")
    if provider == "ollama":
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
    else:
        model = os.getenv("PARATERA_LLM_MODEL", "DeepSeek-V4-Flash-0731")

    body = Text()
    body.append("labHandler", style="bold")
    body.append("  把作业材料放进 workspace/，其余交给 agent\n\n", style="dim")
    body.append("  model      ", style="dim")
    body.append(f"{model} ({provider})\n")
    body.append("  workspace  ", style="dim")
    body.append(f"{WORKSPACE_DIR}\n")
    body.append("  /help 查看命令 \u00b7 Ctrl-D 或 /quit 退出", style="dim")
    console.print(Panel(body, border_style="dim", padding=(1, 2), expand=False))


def repl() -> None:
    _print_startup_banner()
    _startup_checks()
    session = TaskSession()

    # 填充命令注册表（在此处做以避免前向引用问题）
    _COMMANDS.update({
        "quit": _cmd_quit,
        "help": _cmd_help,
        "dream": _cmd_dream,
        "skills": _cmd_skills,
        "profile": _cmd_profile,
        "done": lambda: _cmd_done(session),
    })

    # 断点检测：存在未完成任务时询问是否续跑
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

                # 特例：需原始行参数提取
                # （保留参数中的空白，避免 split+join 折叠）
                if cmd == "edit_skill":
                    _cmd_edit_skill(line[len("/edit_skill"):].lstrip())
                elif cmd == "remember":
                    _cmd_remember(line[len("/remember"):].lstrip())
                elif cmd in _COMMANDS:
                    _COMMANDS[cmd]()
                    if cmd == "quit":
                        console.print("[dim]bye[/]")
                        return
                else:
                    console.print(f"[yellow]未知命令：/{cmd}（试试 /help）[/]")
                continue

            # 普通用户输入：直接发送到主图
            _run_task(session, line)
    finally:
        # 所有 REPL 退出路径（正常 /quit / Ctrl-D / 异常）在此关闭 loop，
        # 取消待处理任务 + 关闭异步生成器。
        _shutdown_repl_loop()


if __name__ == "__main__":
    try:
        repl()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
