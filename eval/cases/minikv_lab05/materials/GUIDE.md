# 实验五 MiniKV 指导书

本实验是操作系统课程的实验课作业。提交物包括可运行代码和一份实验报告。

## 实验目的

实现一个进程内的迷你键值存储 MiniKV，理解「内存表 + 快照落盘」的基本结构。

## 实验环境

Linux，Python 3.11。不需要 Docker 集群。

## 任务

实现文件 `minikv.py`，类名与方法签名如下（不得改名）：

```python
class MiniKV:
    def __init__(self) -> None: ...
    def set(self, key: str, value: str) -> None: ...
    def get(self, key: str) -> str | None: ...
    def delete(self, key: str) -> bool: ...
    def keys(self) -> list[str]: ...
    def save(self, path: str) -> None: ...
    def load(self, path: str) -> None: ...
```

约定：

- `get` 缺失键返回 `None`。
- `delete` 删除成功返回 `True`，键不存在返回 `False`。
- `keys` 返回当前所有键，顺序不限。
- `save` / `load` 使用 JSON 文件，值为字符串。

## 实验步骤

1. 实现内存表。
2. 用若干 `set/get/delete` 验证正确性，记录命令与输出。
3. `save` 后再 `load` 到新实例，核对键值一致。
4. 把过程写入实验报告。

## 实验结果要求

报告必须包含以下八节，标题需出现对应关键词：

1. 实验目的
2. 实验原理
3. 实验环境
4. 实验步骤
5. 实验结果
6. 结果分析与讨论
7. 结论
8. 思考题

思考题：若进程在 `save` 中途崩溃，可能出现什么不一致？你的实现如何面对？

## 提交

- 代码：`minikv.py`
- 报告：`实验报告.md`
- 无法截图时，必须使用占位句「（此处建议附 XX 截图）」，禁止假装已经贴了真实截图。
- 引用本指导书原文时使用 markdown 引用块（行首 `>`）。
