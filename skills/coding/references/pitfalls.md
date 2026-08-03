# Exception Handling Cheat Sheet (coding skill reference material)

| Failure mode | Response |
|---|---|
| task_title / deliverables missing | Call read_file to read the full workspace/README.md directly, then re-extract the constraints yourself |
| Sandbox 8080 unreachable | Fall back to host `host_bash` (guarded by _safe_path, cwd=workspace auto-locked); note "sandbox unreachable, fell back" in the Final Answer |
| pytest repeatedly fails (same error ≥3 times) | Stop hard-tuning; Final Answer reports `step <id> needs_retry: <stuck point + methods already tried>`; after retries are exhausted the main graph automatically hands to Verifier for Replan |
| Problem-statement constraints conflict | Do not force-satisfy all of them; Final Answer `待办：` line notes "constraint X conflicts with Y, user must decide priority" |
| host_bash out-of-bounds PermissionError | By design, not a bug; switch to relative paths + execute inside the sandbox |
| recursion_limit reached | Task granularity too large; report needs_retry and let the main graph handle it; this skill expects a single step ≤6 ReAct iter |
