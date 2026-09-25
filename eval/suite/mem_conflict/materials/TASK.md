# 偏好存储

在 `settings_store.py` 实现：

```python
def save_pref(user: str, key: str, value: str) -> None: ...
def load_pref(user: str, key: str) -> str | None: ...
```

存储形状以检索到的卡片为准。冒烟只写一次再读，见 `smoke/test_smoke.py`。
