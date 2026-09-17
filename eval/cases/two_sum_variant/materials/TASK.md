# 作业：有序数组的两数之和

只交一个 Python 源文件，文件名必须是 `two_sum_sorted.py`。不要交 `solution.py` 或 `main.py`。
这不是实验课，不需要实验报告，不需要截图。

## 函数

```python
def two_sum_sorted(numbers: list[int], target: int) -> list[int]:
    """numbers 已按非降序排好。返回两个下标，使对应元素之和为 target。"""
```

## 约定

- 下标从 **1** 开始（不是 0）。
- 恰好存在一对解；`i != j`。若实现上遇到无解，返回 `[]`。
- 返回的两个下标按升序排列，例如 `[1, 2]`。
- 数组已排序，请使用双指针，不要再套一遍哈希表作业的写法充数。
- 不要提交 `test_*.py`。

## 样例

输入 `numbers = [2, 7, 11, 15], target = 9`，返回 `[1, 2]`。
输入 `numbers = [2, 3, 4], target = 6`，返回 `[1, 3]`。
