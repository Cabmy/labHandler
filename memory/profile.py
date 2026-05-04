"""Profile 模块 - 读写 profile/me.yaml + 注入 agent system prompt

设计要点（PLAN §13 / STEPS P2.2）：
1. 单一文件 me.yaml 作为 source of truth；无 DB（个人画像不需要历史版本）
2. update_field / add_field **原子写**：先 .tmp 再 rename，避免崩溃留半成品
3. inject_for_agent: 把 profile 关键字段拼成简短文本插进 system prompt
4. 路径用点号 "identity.name" / "preferences.writing_style.formality"
5. 不引入 pydantic，YAML → dict 即可（保持轻量）
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

import yaml

PROFILE_PATH = os.getenv("PROFILE_PATH", "./profile/me.yaml")


class Profile:
    """用户画像 - 单文件 YAML"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path: Path = Path(path or PROFILE_PATH).resolve()
        # 不在 __init__ 强制创建文件：缺失时 load_profile 走默认空 dict

    # ─── 读 ────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """读 YAML；文件不存在或解析失败时返回空 dict（不抛）"""
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return {}
            return data
        except Exception:
            return {}

    # ─── 写（原子）────────────────────────────────────────────

    def _atomic_write(self, data: dict[str, Any]) -> None:
        """先 .tmp 再 rename（POSIX 原子性）"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        # POSIX rename 原子；Windows shutil.move 兜底
        try:
            os.replace(tmp, self.path)
        except Exception:
            shutil.move(str(tmp), str(self.path))

    def update_field(self, dotted_path: str, value: Any) -> dict[str, Any]:
        """按点号路径更新（路径必须已存在）

        例：update_field("preferences.writing_style.formality", "high")
        """
        data = self.load()
        parts = dotted_path.split(".")
        cur = data
        for p in parts[:-1]:
            if not isinstance(cur, dict) or p not in cur:
                raise KeyError(f"路径不存在：{dotted_path}（在 {p} 处断）")
            cur = cur[p]
        if not isinstance(cur, dict):
            raise KeyError(f"路径不是 dict：{dotted_path}")
        cur[parts[-1]] = value
        self._atomic_write(data)
        return data

    def add_field(self, dotted_path: str, value: Any) -> dict[str, Any]:
        """按点号路径添加（缺失父节点会自动建 dict）"""
        data = self.load()
        parts = dotted_path.split(".")
        cur = data
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value
        self._atomic_write(data)
        return data

    # ─── 注入 system prompt ───────────────────────────────────

    def inject_for_agent(self, agent_name: str, system_prompt: str) -> str:
        """把 profile 关键字段拼成段落插到 system prompt 末尾。

        注入对象：所有产物 agent（coder / planner / verifier / summarizer）；
        intake 不注入（只解析题面，不写产物）。
        """
        agent = agent_name.lower()
        if agent not in {"coder", "planner", "verifier", "summarizer"}:
            return system_prompt

        data = self.load()
        if not data:
            return system_prompt

        identity = data.get("identity") or {}
        prefs = data.get("preferences") or {}

        lines = ["", "## 关于用户偏好（profile）"]
        if identity:
            name = identity.get("name", "")
            sid = identity.get("student_id", "")
            if name or sid:
                lines.append(f"- 用户：{name}（学号 {sid}）")
        if prefs:
            lang = prefs.get("language")
            if lang:
                lines.append(f"- 语言：{lang}")
            ws = prefs.get("writing_style") or {}
            if ws:
                formality = ws.get("formality", "medium")
                avg_len = ws.get("avg_sentence_len", 25)
                lines.append(
                    f"- 写作风格：正式度={formality}，平均句长≈{avg_len}"
                )
            cs = prefs.get("coding_style") or {}
            if cs:
                th = "需要 type hints" if cs.get("type_hints") else "可省 type hints"
                ds = cs.get("docstring", "short")
                lines.append(f"- 代码风格：{th}；docstring={ds}")

        # 自由文本规则（/remember 累积）
        rules = (prefs.get("style_rules") if isinstance(prefs, dict) else None) or []
        rules = [str(r).strip() for r in rules if str(r).strip()]
        if rules:
            lines.append("")
            lines.append(
                "## 用户长期规则（按 /remember 累积；本轮 user 指令冲突时以本轮为准）"
            )
            for r in rules:
                lines.append(f"- {r}")

        return system_prompt + "\n".join(lines)


# ─── 全局单例 + 便捷函数 ──────────────────────────────────────

_default_profile: Optional[Profile] = None


def get_profile() -> Profile:
    global _default_profile
    if _default_profile is None:
        _default_profile = Profile()
    return _default_profile


def load_profile() -> dict[str, Any]:
    return get_profile().load()


def inject_for_agent(agent_name: str, system_prompt: str) -> str:
    return get_profile().inject_for_agent(agent_name, system_prompt)


def update_field(dotted_path: str, value: Any) -> dict[str, Any]:
    return get_profile().update_field(dotted_path, value)


def add_field(dotted_path: str, value: Any) -> dict[str, Any]:
    return get_profile().add_field(dotted_path, value)


def append_rule(rule: str) -> dict[str, Any]:
    """追加一条自由文本规则到 preferences.style_rules（list）。

    /remember 命令的后端：复用 _atomic_write，避免覆盖型 add_field。
    """
    text = (rule or "").strip()
    if not text:
        raise ValueError("rule 为空")
    prof = get_profile()
    data = prof.load()
    prefs = data.setdefault("preferences", {})
    if not isinstance(prefs, dict):
        prefs = {}
        data["preferences"] = prefs
    rules = prefs.get("style_rules")
    if not isinstance(rules, list):
        rules = []
    rules.append(text)
    prefs["style_rules"] = rules
    prof._atomic_write(data)
    return data
