# 向量缩放

在 `vec.py` 实现，签名一个字都不能改：

```python
def scale_vec(xs: list[float], factor: float) -> list[float]: ...
```

冒烟见 `smoke/test_smoke.py`：`scale_vec([1.0, 2.0], 3.0)` 得到 `[3.0, 6.0]`，`scale_vec([0.0], 1.0)` 得到 `[0.0]`。
