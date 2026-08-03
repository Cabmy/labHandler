# Test Case Design Guide (coding skill reference material)

## Three case categories (cover at least each)

1. **Normal scenarios**: target in the middle / head / tail; typical input scale
2. **Boundary scenarios**: empty input, single element, duplicate elements, extreme values (INT_MAX level), target absent
3. **Exception scenarios**: unsorted input / wrong type — decide behavior per the problem-statement constraints (return -1 or raise);
   if the statement is silent, choose to return a sentinel value and note it in the docstring

## Case organization

- One behavior per test function, named `test_<behavior>` (e.g. `test_empty_list` / `test_target_at_head`)
- Assertions carry a message: `assert res == 2, f"expect 2, got {res}"`
- Use `@pytest.mark.parametrize` for batched boundary cases — cleaner than copy-pasting 5 functions

## Example skeleton (binary_search as example)

```python
import pytest
from binary_search import binary_search

@pytest.mark.parametrize("arr,target,expect", [
    ([1, 2, 3, 4, 5], 3, 2),    # middle
    ([1, 2, 3], 1, 0),          # head
    ([1, 2, 3], 3, 2),          # tail
    ([], 1, -1),                # empty
    ([1, 2, 3], 9, -1),         # absent
])
def test_search(arr, target, expect):
    assert binary_search(arr, target) == expect
```

## Verification command

```
sandbox_execute_bash: pytest test_<name>.py -v
```

- Use sandbox_execute_bash (synchronous, waits for real output); do not use sandbox_execute_code (asynchronous ack-only)
- Only send done when all pass; partial pass reports needs_retry and clearly states which case failed
