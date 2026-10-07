# 生命周期与恢复

labHandler 只维护一个会话生命周期。执行位置、执行历史、产品质量分别由自己的事实表达，不再共用 TaskStatus。

## 状态模型

持久化模型只有 **open → closed**：

- `begin(version=1)` 创建会话；没有 `session_closed` 就允许恢复。
- `session_closed(result)` 表示流程已收尾，禁止继续执行。
- `run_started`、`run_paused(reason)` 记录每次运行和中断原因，不改变会话是否终结。

用户看到的状态是视图，不写回磁盘：

| 条件 | 状态 | 含义 |
| --- | --- | --- |
| LabRunner 持有当前执行协程 | RUNNING | 正在执行或等待取消清理结束 |
| 会话 open，执行协程不存在 | PAUSED | 可以从 Journal 断点继续 |
| 会话 closed | FINISHED | 本场流程已收尾 |

首次运行前没有会话，状态为空。进程崩溃后没有活协程，未收尾的会话自然呈现 PAUSED，不需要修改历史节点、猜测 RUNNING 是否过期或增加恢复状态。

正常路径：运行 → 收尾。中断路径：运行 → 暂停 → 新的一次运行。不存在 FINISHED → RUNNING。

## 唯一职责

| 模块 | 拥有的事实 |
| --- | --- |
| `runtime/lab/runner.py` | 当前运行协程、取消与退出；唯一会话关闭入口 |
| `runtime/lab/journal.py` | 生命周期、执行记录、阶段进度、对话和副作用的持久化事实及投影 |
| `runtime/lab/flow.py` | Remember / SPEC / Dispatch / Judge / Summary 顺序与业务决策 |
| `runtime/lab/execution.py` | 一次 Pro/Flash 执行的开始、返回、异常和取消记录 |
| `runtime/task.py` | 一次执行的身份、权限、预算计数、模型可见语义事件 |
| `runtime/lab/scheduler.py` | 有界并发、结果即时提交、子协程清理 |
| brief / gate / verdict | 业务完成情况、验收质量和会话结果 |

`RuntimeTask` 不保存生命周期，不序列化成树，不允许恢复旧对象。`TaskStatus`、`TaskTree`、根节点及 `STATE.json` 已删除。无需为节点维护通用流转表。

## 子任务是执行记录

进入 agent loop 前写 `attempt_started(task_id, kind, assignment)`；返回后写 `attempt_ended(task_id, reason, submit, brief, metrics)`。异常和取消同样写结束记录，然后继续传播异常。

重试产生新 task_id；业务 assignment id 保持稳定。每次执行的计数、结果和错误互不覆盖。排队中的 assignment 还没有开始，不需要一个 PENDING 节点。硬崩溃留下的未配对开始记录是历史中断证据，不会被恢复为活任务。

执行返回和业务成功是不同事实：Flash 已提交 brief，但 gate=fail，仍是一份正常返回的执行记录；产品需要修改由 Judge 决定。预算耗尽、鉴权或致命错误导致 loop 提前返回，由 reason 记录，不再复制成另一个 FAILED 状态。

## 退出约束

1. `request_stop()` 取消当前 run 协程，取消沿 await 传播到正在运行的 Pro、Flash、工具与验收调用；重复 stop 不重复注入取消。
2. 调度器取消并等待所有子协程退出后，runner 才返回 paused。调用者主动取消则在落盘后继续抛出 CancelledError。
3. worker 正常返回或发生普通异常，其最终 brief 立即提交。兄弟取消生成 blocked brief；整波取消不替未完成任务伪造提交，因此恢复能补做这些任务。
4. 任意 Pro 阶段因鉴权、预算等原因未交卷，通过 RunPaused 暂停；Summary 正文为空也暂停。未捕获异常记录暂停原因并继续抛出。
5. Summary 正文必须先原子写入 workspace，随后才写 `session_closed`。写盘失败仍可恢复。已有 Summary 提交但未关闭时，续跑复用提交补写文件，不重新调用模型。
6. `need_user`、`sandbox_unreachable`、验收 fail 等业务结果在成功生成 Summary 后仍然收尾。这沿用“一场 lab 生成诚实的最终报告”的产品行为；FINISHED 不承诺作业通过验收。

取消本地协程并不回滚已执行的文件写入，也不承诺撤销已发往 Docker/外部工具的命令。续跑依赖 Journal 提交点与副作用探针；未提交的工作可能重新执行，不能宣称任意外部副作用恰好执行一次。

## 恢复与落盘

恢复扫描只读 Journal：有 begin、没有 session_closed 即可恢复。恢复位置由 stage 和 dispatch/brief/judge 提交点确定；已经完成的 SPEC 取最新成功提交，不能回退到第一版。Pro 对话在每轮和退出时落盘。当前尝试的预算计数不跨新尝试延续。

Journal 逐行 append + fsync。完整换行是记录边界；读者忽略尾部半行，写者重新打开时截掉未提交尾部，避免新事件粘到坏行。已经提交的坏行拒绝恢复，不能静默丢掉一个 close 或 brief。

当前应用是单进程、单会话执行、单 Journal 写者。多进程部署需要额外的执行租约或互斥锁，本设计不通过持久化 RUNNING 冒充跨进程锁。

新格式要求 begin.version=1；不读取旧 Task 树、不迁移旧会话、不提供兼容分支。

## 验证

`python -m pytest -q eval/test_lifecycle.py` 覆盖停止、外部取消、排队取消、并发清理、异常提交、关闭后禁止恢复、日志半行、坏行、最新 SPEC、执行结果与验收分离，以及 Summary 失败/恢复。
