"""用户长期偏好 profile 的工具层入口。

read_profile / update_profile / add_profile_field 委托 memory.profile；
本模块只暴露工具签名，不持有存储。
"""

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
