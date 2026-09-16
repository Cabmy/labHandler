# Exception Handling (coding)

| Failure | Response |
|---|---|
| Constraints not in the assignment | `read_file` the workspace README / guidance; do not guess |
| Sandbox unreachable | Gate maps this to `test_invalid`. Do not pretend the tests passed on the host |
| pytest same error ≥3 times | Stop retuning. Next dispatch should shrink the assignment or rewrite tests (`test_invalid` if the gate itself is wrong) |
| Conflicting constraints | Do not force both. Note the conflict in SPEC / SUMMARY and pick the problem statement over the skill |
| `host_bash` PermissionError | Stay inside workspace relative paths, or run in `sandbox_execute_bash` |
| Assignment too large | Split. One Flash, one unit of work |
