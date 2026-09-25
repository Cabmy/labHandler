# 带过期时间的 KV

在 `kv.py` 实现 `put_with_ttl(key: str, value: str, ttl_ms: int)` 和 `get(key: str) -> str | None`。
用 `storage.Store` 存值，不要改 `storage.py`。

冒烟见 `smoke/test_smoke.py`：写入后能读到；ttl 很短时过期后读到 None。
