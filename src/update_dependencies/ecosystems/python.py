"""Python dependency update operations."""

from pathlib import Path

from .. import engine as shared
from ..progress import emit


def update_python(
    *,
    allow_major: bool = True,
    dry_run: bool = False,
    add_latest: bool = False,
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    emit("python: resolving project dependencies")
    original = shared.context().pyproject.read_text(encoding="utf-8")
    original_lock = shared.context().uv_lock.read_bytes() if shared.context().uv_lock.is_file() else None

    def restore() -> None:
        shared.context().pyproject.write_text(original, encoding="utf-8")
        if original_lock is None:
            shared.context().uv_lock.unlink(missing_ok=True)
        else:
            shared.context().uv_lock.write_bytes(original_lock)

    try:
        relaxed = shared.relax_constraints(original, allow_major=allow_major)
        relaxed = shared.hold_major_lines(relaxed, shared.parse_uv_lock(), allow_major=allow_major)
        relaxed = shared.freeze_opted_out(relaxed, shared.parse_uv_lock())
        shared.context().pyproject.write_text(relaxed, encoding="utf-8")
        shared.run(["uv", "lock", "--upgrade", *shared.context().uv_age_args])
        changes = shared.update_pyproject_constraints(
            shared.locked_version_lists(shared.context().uv_lock),
            original=original,
            allow_major=allow_major,
            add_latest=add_latest,
        )
        if not dry_run:
            emit("python: refreshing project lockfile")
            shared.run(["uv", "lock"])

        after = shared.parse_uv_lock()
    except BaseException:
        restore()
        raise
    if dry_run:
        restore()
    return after, changes


def update_inline_script(
    path: Path,
    *,
    allow_major: bool = True,
    dry_run: bool = False,
    add_latest: bool = False,
) -> list[tuple[str, str, str]]:
    """Raise PEP 723 script dependency floors via `uv lock --script --upgrade`."""
    rel = str(path.relative_to(shared.context().root))
    emit(f"python: resolving inline script {rel}")
    lock_path = shared.script_lock_path(path)
    original = path.read_text(encoding="utf-8")
    had_lock = lock_path.is_file()
    original_lock = lock_path.read_bytes() if had_lock else None

    def restore() -> None:
        path.write_text(original, encoding="utf-8")
        if had_lock:
            assert original_lock is not None
            lock_path.write_bytes(original_lock)
        else:
            lock_path.unlink(missing_ok=True)

    try:
        relaxed = shared.relax_constraints(
            original, allow_major=allow_major, specs=shared.pep723_dependency_specs
        )
        relaxed = shared.hold_major_lines(
            relaxed,
            shared.parse_uv_lock_file(lock_path),
            allow_major=allow_major,
            specs=shared.pep723_dependency_specs,
        )
        relaxed = shared.freeze_opted_out(
            relaxed,
            shared.parse_uv_lock_file(lock_path),
            specs=shared.pep723_dependency_specs,
        )
        path.write_text(relaxed, encoding="utf-8")
        shared.run(
            [
                "uv",
                "lock",
                "--script",
                str(path),
                "--upgrade",
                *shared.context().uv_age_args,
            ]
        )
        text, changes = shared.update_requirement_constraints(
            shared.locked_version_lists(lock_path),
            original=original,
            allow_major=allow_major,
            add_latest=add_latest,
            specs=shared.pep723_dependency_specs,
            skip_label=rel,
        )
        if dry_run:
            restore()
            return changes
        path.write_text(text, encoding="utf-8")
        if had_lock:
            shared.run(["uv", "lock", "--script", str(path)])
        else:
            lock_path.unlink(missing_ok=True)
    except BaseException:
        restore()
        raise
    return changes


def update_inline_scripts(
    *,
    allow_major: bool = True,
    dry_run: bool = False,
    add_latest: bool = False,
) -> list[tuple[str, str, str, str]]:
    """Update every PEP 723 script; returns (name, old, new, relative path)."""
    rows: list[tuple[str, str, str, str]] = []
    for path in shared.find_pep723_scripts():
        rel = str(path.relative_to(shared.context().root))
        for name, old, new in shared.update_inline_script(
            path,
            allow_major=allow_major,
            dry_run=dry_run,
            add_latest=add_latest,
        ):
            rows.append((name, old, new, rel))
    return rows
