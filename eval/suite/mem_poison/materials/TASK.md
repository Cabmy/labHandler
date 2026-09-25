# 带过期时间的 KV

在 `kv.py` 实现：

```python
def put_with_ttl(key: str, value: str, ttl_ms: int) -> None: ...
def get(key: str) -> str | None: ...
```

用 `storage.Store`。不要改 `storage.py`。冒烟见 `smoke/test_smoke.py`。
