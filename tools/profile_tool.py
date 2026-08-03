"""profile_tool - 读 / 更新 profile/me.yaml。

运行时 profile 读写工具集。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


@tool
def read_profile() -> dict:
    """Read the full content of profile/me.yaml (dict)."""
    from memory import load_profile

    return load_profile()


@tool
def update_profile(path: str, value: Any) -> dict:
    """Update a profile field (dotted path; path must already exist).

    Example: update_profile("preferences.writing_style.formality", "high")

    Returns: the updated profile dict.
    """
    from memory import update_field

    return update_field(path, value)


@tool
def add_profile_field(path: str, value: Any) -> dict:
    """Add a profile field (dotted path; missing parent nodes are created automatically)."""
    from memory import add_field

    return add_field(path, value)
