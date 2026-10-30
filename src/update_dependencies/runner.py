"""Prepare updates in an isolated checkout, validate, then apply as one operation."""

import shutil
import tempfile
import tomllib
from pathlib import Path

import yaml

from . import engine
from .config import Config
from .context import RunContext, use_context
from .discovery import inventory, matches, walk
from .processes import execute
from .progress import emit, phase
from .reporting import Report


def update_selected(config: Config, report: Report, *, explicit: bool = False):
    ctx = engine.context()
    for ecosystem in config.ecosystems:
        reason = engine.skip_reason(ecosystem, update_inline_scripts=config.python_inline_scripts)
        if reason:
            if explicit:
                raise ValueError(f"{ecosystem}: {reason}")
            emit(f"{ecosystem}: skipped ({reason})")
            report.skipped.append({"ecosystem": ecosystem, "reason": reason})
            continue
        tools = {
            "python": ["uv"],
            "actions": ["gh"],
            "docker": ["docker"],
            "hooks": ["uv", "git"],
        }[ecosystem]
        for tool in tools:
            if shutil.which(tool) is None:
                raise ValueError(f"{ecosystem} requires {tool} on PATH")
        with phase(f"Updating {ecosystem}"):
            if ecosystem == "python":
                if ctx.pyproject.is_file():
                    _, changes = engine.update_python(
                        allow_major=config.allow_major,
                        add_latest=config.python_add_minimums,
                    )
                    report.changes.extend(
                        {"ecosystem": ecosystem, "name": n, "old": o, "new": v} for n, o, v in changes
                    )
                    if config.python_update_build_backend:
                        # Build backend policy is independent of missing dependency floors and Docker tags.
                        version, notes = engine.latest_uv_build(min_age_hours=config.min_age_hours)
                        report.changes.extend(
                            {
                                "ecosystem": ecosystem,
                                "kind": k,
                                "name": n,
                                "old": o,
                                "new": v,
                                "note": note,
                            }
                            for k, n, o, v, note in notes
                        )
                        if version:
                            backend_changes = engine._write_uv_build_floor(version, False, raise_only=True)
                            report.changes.extend(
                                {"ecosystem": ecosystem, "name": n, "old": o, "new": v}
                                for n, o, v in backend_changes
                            )
                            if backend_changes:
                                engine.run(["uv", "lock", *ctx.uv_age_args])
                if config.python_inline_scripts:
                    for n, old, new, path in engine.update_inline_scripts(
                        allow_major=config.allow_major,
                        add_latest=config.python_add_minimums,
                    ):
                        report.changes.append(
                            {
                                "ecosystem": ecosystem,
                                "name": n,
                                "old": old,
                                "new": new,
                                "file": path,
                            }
                        )
                direct = engine.parse_pyproject_direct() if ctx.pyproject.is_file() else set()
                if config.python_inline_scripts:
                    direct |= engine.parse_inline_script_direct()
                report.changes.extend(
                    {
                        "ecosystem": ecosystem,
                        "kind": k,
                        "name": n,
                        "old": o,
                        "new": v,
                        "note": note,
                    }
                    for k, n, o, v, note in engine.python_maintenance(direct)
                )
            elif ecosystem == "actions":
                rows = engine.update_actions(
                    allow_major=config.allow_major, min_age_hours=config.min_age_hours
                )
                report.changes.extend(
                    {
                        "ecosystem": ecosystem,
                        "kind": k,
                        "name": n,
                        "old": o,
                        "new": v,
                        "note": note,
                    }
                    for k, n, o, v, note in rows
                )
            elif ecosystem == "docker":
                rows = engine.update_docker(enable_latest=config.docker_pin_latest)
                report.changes.extend(
                    {
                        "ecosystem": ecosystem,
                        "kind": k,
                        "name": n,
                        "old": o,
                        "new": v,
                        "note": note,
                    }
                    for k, n, o, v, note in rows
                )
            else:
                rows = engine.update_hooks()
                report.changes.extend(
                    {"ecosystem": ecosystem, "name": n, "old": o, "new": v} for n, o, v in rows
                )
    report.skipped.extend(ctx.policy_skips)
    report.skipped.extend(
        {"file": p, "name": n, "ref": r, "reason": "NO_AUTO_UPDATE"} for p, n, r in ctx.opt_out_skips
    )
    report.skipped.extend(
        {"file": p, "name": n, "ref": r, "reason": "Docker latest disabled"}
        for p, n, r in ctx.docker_latest_skips
    )


def validate_file(path: Path):
    if path.suffix == ".toml" or path.name.endswith(".lock"):
        tomllib.loads(path.read_text(encoding="utf-8"))
    elif path.suffix in {".yml", ".yaml"}:
        try:
            list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid YAML in {path.name}: {error}") from error


def run(root: Path, config: Config, *, explicit: bool = False) -> Report:
    config.validate()
    with phase("Discovering repository files"):
        files = inventory(root)
        before = {p: (root / p).read_bytes() for p in files}
    emit(f"Discovered {len(files)} file(s)")
    report = Report(dry_run=config.dry_run or config.check)
    with tempfile.TemporaryDirectory(prefix="update-dependencies-") as temp:
        staging = Path(temp)
        with phase("Preparing isolated workspace"):
            for relative in files:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / relative, target)
        eligible = [p for p in files if matches(p, config.include, config.exclude)]
        ctx = RunContext(staging, config, eligible)
        if Path("pyproject.toml") not in eligible:
            ctx.pyproject = staging / ".excluded-pyproject.toml"
        if Path(".pre-commit-config.yaml") not in eligible:
            ctx.pre_commit = staging / ".excluded-pre-commit.yaml"
        if "hooks" in config.ecosystems and ctx.pre_commit.is_file():
            # pre-commit requires a repository, but the target's Git metadata stays private.
            execute(
                ["git", "-c", "init.templateDir=", "init", "--quiet"],
                cwd=staging,
                timeout=config.timeout,
                retries=0,
            )
        with use_context(ctx):
            update_selected(config, report, explicit=explicit)
        with phase("Validating staged changes"):
            # Preserve tracked files even when .gitignore would hide them in this fresh repo.
            after_paths = sorted(walk(staging))
            for relative in after_paths:
                if (staging / relative).is_symlink():
                    raise ValueError(f"Resolver produced a symlink: {relative}")
            after = {p: (staging / p).read_bytes() for p in after_paths}
            changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
            # The updater writes text; preserve original CRLF convention.
            for path in changed:
                if (
                    path in before
                    and path in after
                    and b"\r\n" in before[path]
                    and b"\n" not in before[path].replace(b"\r\n", b"")
                ):
                    after[path] = after[path].replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                    (staging / path).write_bytes(after[path])
            changed = [p for p in changed if before.get(p) != after.get(p)]
            for path in changed:
                if path in after:
                    validate_file(staging / path)
            report.changed_files = [p.as_posix() for p in changed]
        if report.dry_run:
            emit(f"Preview ready: {len(changed)} file(s) would change; target files untouched")
            return report
        with phase(f"Applying {len(changed)} changed file(s)"):
            # Detect concurrent edits before touching any target file.
            for path in changed:
                target = root / path
                if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                    raise ValueError(f"Refusing symlink target: {path}")
                current = target.read_bytes() if target.is_file() else None
                if current != before.get(path):
                    raise ValueError(f"File changed during update: {path}")
            applied = []
            try:
                for path in changed:
                    target = root / path
                    applied.append(path)
                    if path in after:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(after[path])
                    else:
                        target.unlink()
            except BaseException:
                for path in reversed(applied):
                    target = root / path
                    if path in before:
                        target.write_bytes(before[path])
                    else:
                        target.unlink(missing_ok=True)
                raise
    if config.python_sync and "python" in config.ecosystems and (root / "pyproject.toml").is_file():
        with phase("Synchronizing target environment"):
            execute(["uv", "sync"], cwd=root, timeout=config.timeout, retries=0)
    return report
