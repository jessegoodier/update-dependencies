"""Repository inventory with git-ignore semantics and explicit path filters."""

import os
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".tox",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}


def walk(root: Path):
    for directory, names, files in os.walk(root):
        names[:] = [n for n in names if n not in SKIP_DIRS and not (Path(directory) / n).is_symlink()]
        yield from ((Path(directory) / name).relative_to(root) for name in files)


def inventory(root: Path) -> list[Path]:
    try:
        listed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        listed = None
    candidates = (
        (Path(p) for p in listed.stdout.split("\0") if p)
        if listed is not None and listed.returncode == 0
        else walk(root)
    )
    result = set()
    for relative in candidates:
        if relative.is_absolute() or ".." in relative.parts or set(relative.parts) & SKIP_DIRS:
            continue
        path = root / relative
        if not path.is_file() or any(
            (root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)
        ):
            continue
        result.add(relative)
    return sorted(result)


def matches(path: Path, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    name = path.as_posix()

    def matched(pattern):
        return fnmatchcase(name, pattern) or (pattern.startswith("**/") and fnmatchcase(name, pattern[3:]))

    return (not include or any(matched(p) for p in include)) and not any(matched(p) for p in exclude)
