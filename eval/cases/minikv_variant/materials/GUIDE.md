# 实验六 MiniKV 带 TTL 指导书

本实验是操作系统课程的实验课作业，在实验五内存表基础上增加按键过期。提交物包括代码和实验报告。

## 实验目的

为 MiniKV 增加 `expire` / `ttl`，观察过期键在读取与落盘时的行为。

## 实验环境

Linux，Python 3.11。

## 任务

实现文件 `minikv.py`：

```python
class MiniKV:
    def __init__(self) -> None: ...
    def set(self, key: str, value: str) -> None: ...
    def get(self, key: str) -> str | None: ...
    def delete(self, key: str) -> bool: ...
    def keys(self) -> list[str]: ...
    def expire(self, key: str, ttl_s: float) -> bool: ...
    def ttl(self, key: str) -> float | None: ...
    def save(self, path: str) -> None: ...
    def load(self, path: str) -> None: ...
```

约定：

- `expire`：键存在则设置从现在起 `ttl_s` 秒后过期，返回 `True`；键不存在返回 `False`。
- `ttl`：未设置过期返回 `None`；已过期或不存在返回 `None`；否则返回剩余秒数（允许浮点误差 0.05）。
- `get` / `keys` 不得返回已过期的键。过期后 `get` 为 `None`。
- `save` 不要把已过期的键写进 JSON。

## 实验步骤

1. 实现 TTL。
2. `set` 后 `expire(key, 0.2)`，睡眠 0.3 秒，确认 `get` 为 `None`。
3. 记录命令、输出，写入八节实验报告。

## 报告

必须含：实验目的、实验原理、实验环境、实验步骤、实验结果、结果分析与讨论、结论、思考题。

思考题：过期检查放在 `get` 时惰性删除，与后台线程主动删除各有什么代价？

## 提交

- `minikv.py` 与 `实验报告.md`
- 截图占位必须写成「（此处建议附 XX 截图）」
- 引用本指导书用 markdown 引用块（行首 `>`）
