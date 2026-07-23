"""profile_tool - 读 / 改 profile/me.yaml

面向运行时 profile 读取与更新的工具集合。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


@tool
def read_profile() -> dict:
    """读取 profile/me.yaml 全部内容（dict）。"""
    from memory import load_profile

    return load_profile()


@tool
def update_profile(path: str, value: Any) -> dict:
    """更新 profile 字段（点号路径，路径必须已存在）。

    例：update_profile("preferences.writing_style.formality", "high")

    Returns: 更新后的 profile dict。
    """
    from memory import update_field

    return update_field(path, value)


@tool
def add_profile_field(path: str, value: Any) -> dict:
    """新增 profile 字段（点号路径，缺失父节点会自动建）。"""
    from memory import add_field

    return add_field(path, value)
