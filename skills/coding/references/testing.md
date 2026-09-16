# Test Case Design (coding)

Cover at least one of each:

1. **Normal**: typical input; target in the middle / head / tail
2. **Boundary**: empty, single element, duplicates, extreme values, target absent
3. **Exception**: wrong type / unsorted — follow the problem statement (return sentinel or raise). If silent, pick a sentinel and document it

## Organization

- One behavior per test: `test_<behavior>`
- Assertion message: `assert res == 2, f"expect 2, got {res}"`
- Batch boundaries with `@pytest.mark.parametrize`

```python
import pytest
from binary_search import binary_search

@pytest.mark.parametrize("arr,target,expect", [
    ([1, 2, 3, 4, 5], 3, 2),
    ([1, 2, 3], 1, 0),
    ([1, 2, 3], 3, 2),
    ([], 1, -1),
    ([1, 2, 3], 9, -1),
])
def test_search(arr, target, expect):
    assert binary_search(arr, target) == expect
```

Run with `sandbox_execute_bash`: `pytest test_<name>.py -v`.
Write the gate through `write_acceptance` against SPEC interfaces. Partial pass is fail, not done.
