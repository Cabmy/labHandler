# 测试用例设计指南（coding skill 参考材料）

## 三类用例（每类至少覆盖）

1. **正常场景**：target 在中间 / 头 / 尾；典型输入规模
2. **边界场景**：空输入、单元素、重复元素、极值（INT_MAX 级别）、目标不存在
3. **异常场景**：输入未排序 / 类型错误——按题面约束决定行为（返回 -1 还是 raise），
   题面没说就选返回哨兵值并在 docstring 注明

## 用例组织

- 一个行为一个测试函数，命名 `test_<行为>`（如 `test_empty_list` / `test_target_at_head`）
- 断言带信息：`assert res == 2, f"expect 2, got {res}"`
- 参数化批量边界用 `@pytest.mark.parametrize`，比复制粘贴 5 个函数干净

## 示例骨架（以 binary_search 为例）

```python
import pytest
from binary_search import binary_search

@pytest.mark.parametrize("arr,target,expect", [
    ([1, 2, 3, 4, 5], 3, 2),    # 中间
    ([1, 2, 3], 1, 0),          # 头
    ([1, 2, 3], 3, 2),          # 尾
    ([], 1, -1),                # 空
    ([1, 2, 3], 9, -1),         # 不存在
])
def test_search(arr, target, expect):
    assert binary_search(arr, target) == expect
```

## 验证命令

```
sandbox_execute_bash: pytest test_<name>.py -v
```

- 用 sandbox_execute_bash（同步等真实输出），不要用 sandbox_execute_code（异步 ack-only）
- 全过才发 done；部分过报 needs_retry 并写清哪个用例挂了
