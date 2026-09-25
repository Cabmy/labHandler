import time

from storage import Store

_store = Store()
_deadline: dict[str, float] = {}


def put_with_ttl(key: str, value: str, ttl_ms: int) -> None:
    _store.write(key, value)
    _deadline[key] = time.monotonic() + ttl_ms / 1000.0


def get(key: str) -> str | None:
    deadline = _deadline.get(key)
    if deadline is None or time.monotonic() > deadline:
        return None
    return _store.read(key)
