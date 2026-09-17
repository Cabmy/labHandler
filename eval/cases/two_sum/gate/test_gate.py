"""外部门禁：agent 跑 lab 时看不见本文件。"""
from __future__ import annotations

from two_sum import two_sum


def test_example():
    assert sorted(two_sum([2, 7, 11, 15], 9)) == [0, 1]


def test_duplicates():
    assert sorted(two_sum([3, 3], 6)) == [0, 1]


def test_later_pair():
    assert sorted(two_sum([3, 2, 4], 6)) == [1, 2]


def test_no_solution():
    assert two_sum([1, 2, 3], 100) == []


def test_negatives():
    assert sorted(two_sum([-1, -2, -3, -4, -5], -8)) == [2, 4]
