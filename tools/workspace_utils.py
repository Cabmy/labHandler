"""共享的 workspace 文件扫描工具。"""

from pathlib import Path
from typing import Iterator


def is_excluded_path(rel_path: Path) -> bool:
    """检查某个相对路径是否应从 workspace 扫描中排除。

    排除 parts 中含隐藏段（以 '.' 开头）或 '__pycache__' 的任何路径。
    """
    return any(part.startswith(".") or part == "__pycache__" for part in rel_path.parts)


def iter_workspace_files(
    root: Path,
    extensions: set[str] | None = None,
    max_files: int = 0,
) -> Iterator[Path]:
    """产出 workspace 文件，排除隐藏目录与 __pycache__。

    参数：
        root：要扫描的 workspace 根目录。
        extensions：若设置，只产出带这些后缀的文件（如 {'.py', '.md'}）。
                    后缀比较不区分大小写。
        max_files：产出这么多文件后停止。0 表示不限。
    """
    count = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_excluded_path(rel):
            continue
        if extensions is not None and p.suffix.lower() not in extensions:
            continue
        yield p
        count += 1
        if max_files > 0 and count >= max_files:
            return
