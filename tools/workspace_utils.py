"""共享的 workspace 文件扫描工具。"""

import re
from pathlib import Path
from typing import Callable, Iterator

# 文件读取单次最大字符数；fs_tools / memory 等模块统一引用
READ_CHAR_CAP = 80_000


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


def grep_in_roots(
    pattern: str,
    roots: list[Path],
    limit: int,
    *,
    file_filter: Callable[[Path], bool] | None = None,
    path_fmt: Callable[[Path], str] | None = None,
) -> str:
    """在多个根目录下按正则逐行搜索文件，返回格式化结果。

    参数：
        pattern：正则表达式字符串。
        roots：搜索根目录列表。
        limit：最大命中条数，达到后截断。
        file_filter：可选回调，返回 True 表示跳过该文件。
        path_fmt：可选回调，把路径格式化为 hits 中使用的字符串。
                  缺省使用 str(path)。

    非法正则返回 [ERROR/Validation]；无命中返回 "(no matches)"；
    命中格式为 ``path:lineno:line[:200]``。
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[ERROR/Validation] invalid regex: {e}"

    hits: list[str] = []
    truncated = False
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if file_filter is not None and file_filter(p):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            path_str = path_fmt(p) if path_fmt else str(p)
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{path_str}:{i}:{line[:200]}")
                    if len(hits) >= limit:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not hits:
        return "(no matches)"
    body = "\n".join(hits)
    if truncated:
        body += f"\n[truncated limit={limit}]"
    return body
