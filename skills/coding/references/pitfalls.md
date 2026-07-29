# 异常处理速查表（coding skill 参考材料）

| 失败模式 | 应对 |
|---|---|
| task_title / deliverables 缺 | 调 read_file 直接读 workspace/README.md 全文，自己再抽一遍约束 |
| 沙箱 8080 不可达 | 降级到 host `host_bash`（受 _safe_path 守护，cwd=workspace 自动锁），Final Answer 标注「沙箱不可达，已降级」 |
| pytest 反复 fail（≥3 次同一错） | 停止硬调，Final Answer 报 `step <id> needs_retry: <卡点 + 已尝试方法>`；重试耗尽后主图自动交 Verifier 走 Replan |
| 题面约束冲突 | 不要硬满足所有；Final Answer `待办：` 行标「约束 X 与 Y 冲突，需用户决定优先级」 |
| host_bash 越界 PermissionError | 设计行为不是 bug；改用相对路径 + 沙箱内执行 |
| recursion_limit reached | 任务粒度太大，报 needs_retry 让主图处理；本 skill 期望单步 ≤6 ReAct iter |
