"""agents 专用异常类型。

图节点不该有进程生杀权：节点内致命错误抛专用异常，由 CLI / Server 入口层
决定如何呈现与退出（分层原则，见 AGENTS.md）。
"""

from __future__ import annotations


class SandboxFatalError(RuntimeError):
    """沙箱连续不可用（[SANDBOX_UNREACHABLE]）导致任务无法继续。

    携带首条失败详情字符串；由 cli._run_task 捕获后打印修复指引并退出进程。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
