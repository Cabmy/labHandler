"""一次 lab 的流水线：ingest → SPEC → 派发 Flash → Judge → Summary。"""

__all__ = ["LabRunner"]


def __getattr__(name: str):
    if name == "LabRunner":
        from runtime.lab.runner import LabRunner

        return LabRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
