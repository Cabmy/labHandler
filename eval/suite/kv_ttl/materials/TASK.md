# 带过期时间的 KV

在 `kv.py` 里实现：

```python
def put_with_ttl(key: str, value: str, ttl_ms: int) -> None: ...
def get(key: str) -> str | None: ...
```

用同目录的 `storage.Store` 存值。不要修改 `storage.py`。

冒烟（也见 `smoke/test_smoke.py`）：

- `put_with_ttl("a", "one", 60000)` 之后 `get("a")` 是 `"one"`。
- `put_with_ttl("b", "two", 20)` 等待超过 20ms 后 `get("b")` 是 `None`。
