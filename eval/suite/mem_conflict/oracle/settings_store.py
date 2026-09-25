_data: dict[tuple[str, str], str] = {}


def save_pref(user: str, key: str, value: str) -> None:
    _data[(user, key)] = value


def load_pref(user: str, key: str) -> str | None:
    return _data.get((user, key))
