"""副作用探针：恢复幂等的抽象层。

probe 负责两件事：worker 完成时「捕获指纹」，续跑时「判定指纹是否仍然成立」。
EffectLedger 只管 node_id -> {probe: fingerprint} 的账本，每次变更写进 journal，
崩溃后由 replay 重建。要覆盖文件以外的副作用（网络、DB、包安装），实现
SideEffectProbe 并注册进来即可，flow/step 不需要改动。
"""

import hashlib
from pathlib import Path
from typing import Any, Protocol

from runtime.lab.journal import Journal


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class SideEffectProbe(Protocol):
    """一类副作用的幂等判定。capture 返回 None 表示这份 brief 没碰这类副作用。"""

    name: str

    def capture(self, workspace: Path,
                brief: dict[str, Any]) -> dict[str, Any] | None: ...

    def satisfied(self, workspace: Path,
                  fingerprint: dict[str, Any]) -> bool: ...


class FileWriteProbe:
    """文件副作用：产出内容的 sha256 指纹。指纹对不上说明产物被改过或已丢。"""

    name = "file_write"

    @staticmethod
    def _resolve(workspace: Path, rel: str) -> Path | None:
        candidate = (workspace / rel).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def capture(self, workspace: Path, brief: dict[str, Any]) -> dict[str, Any] | None:
        files = brief.get("changed_files") or []
        fingerprints: dict[str, str] = {}
        for rel in files:
            path = self._resolve(workspace, str(rel))
            if path is None:
                continue
            try:
                fingerprints[str(rel)] = sha256_file(path)
            except OSError:
                continue
        return {"files": fingerprints} if fingerprints else None

    def satisfied(self, workspace: Path, fingerprint: dict[str, Any]) -> bool:
        files = fingerprint.get("files")
        if not isinstance(files, dict) or not files:
            return False
        for rel, digest in files.items():
            path = self._resolve(workspace, str(rel))
            if path is None:
                return False
            try:
                if sha256_file(path) != digest:
                    return False
            except OSError:
                return False
        return True


def default_probes() -> dict[str, SideEffectProbe]:
    return {FileWriteProbe.name: FileWriteProbe()}


class EffectLedger:
    """node_id -> {probe: fingerprint}。

    worker 完成后记录它实际产出的副作用指纹；续跑时若所有探针都判定指纹
    仍然成立，说明这个节点的副作用已经落地，不必重跑。账本本身不碰文件，
    持久化全部走 journal 的 effect_record / effect_drop 事件。
    """

    def __init__(
        self,
        probes: dict[str, SideEffectProbe],
        journal: Journal,
        workspace: Path,
        entries: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.probes = probes
        self.journal = journal
        self.workspace = workspace
        self.entries: dict[str, dict[str, Any]] = dict(entries or {})

    def record(self, node_id: str, brief: dict[str, Any]) -> None:
        prints: dict[str, Any] = {}
        for name, probe in self.probes.items():
            fingerprint = probe.capture(self.workspace, brief)
            if fingerprint:
                prints[name] = fingerprint
        if not prints:
            return
        self.entries[node_id] = prints
        self.journal.append(
            "effect_record", {"node_id": node_id, "prints": prints})

    def satisfied(self, node_id: str) -> bool:
        """节点副作用是否仍然完好。无记录、探针缺失或指纹对不上都返回 False。"""
        prints = self.entries.get(node_id)
        if not prints:
            return False
        for name, fingerprint in prints.items():
            probe = self.probes.get(name)
            if probe is None or not probe.satisfied(self.workspace, fingerprint):
                return False
        return True

    def drop(self, node_id: str) -> None:
        if self.entries.pop(node_id, None) is not None:
            self.journal.append("effect_drop", {"node_id": node_id})
