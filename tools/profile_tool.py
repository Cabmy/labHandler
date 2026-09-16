"""profile 读写（无 langchain）。"""

from __future__ import annotations

from typing import Any


def read_profile() -> dict:
    from memory import load_profile

    return load_profile()


def update_profile(path: str, value: Any) -> dict:
    from memory import update_field

    return update_field(path, value)


def add_profile_field(path: str, value: Any) -> dict:
    from memory import add_field

    return add_field(path, value)
