"""外部门禁：agent 跑 lab 时看不见本文件。"""
from __future__ import annotations

from two_sum_sorted import two_sum_sorted


def test_example():
    assert two_sum_sorted([2, 7, 11, 15], 9) == [1, 2]


def test_ends():
    assert two_sum_sorted([2, 3, 4], 6) == [1, 3]


def test_duplicates():
    assert two_sum_sorted([1, 1, 3], 2) == [1, 2]


def test_no_solution():
    assert two_sum_sorted([1, 2, 3], 100) == []


def test_negatives():
    # [-4, -1, 0, 5] 里和为 1 的唯一一对是 -4 + 5，1-based 下标 [1, 4]。
    assert two_sum_sorted([-4, -1, 0, 5], 1) == [1, 4]
