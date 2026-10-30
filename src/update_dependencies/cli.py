"""Command line interface; explicit CLI values override repository configuration."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ECOSYSTEMS, Config, load_config
from .processes import redact
from .progress import Progress
from .reporting import github_report
from .runner import run


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Update Python dependencies, GitHub Actions, Docker digests, and pre-commit hooks."
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    result.add_argument(
        "--directory",
        type=Path,
        default=Path.cwd(),
        help="Target repository (default: current directory)",
    )
    group = result.add_mutually_exclusive_group()
    group.add_argument("--config", type=Path)
    group.add_argument("--no-config", action="store_true")
    group = result.add_mutually_exclusive_group()
    group.add_argument(
        "--ecosystems",
        type=lambda s: tuple(s.split(",")),
        help="Comma-separated ecosystem names",
    )
    group.add_argument(
        "--only",
        choices=ECOSYSTEMS,
        help="Compatibility alias for selecting one ecosystem",
    )
    booleans = {
        "dry-run": "Preview without changing target files",
        "check": "Preview; exit 1 when updates are available",
        "allow-major": "Allow major updates (default: enabled)",
        "python-inline-scripts": "Update PEP 723 script metadata",
        "python-add-minimums": "Add floors to bare Python requirements",
        "sync": "Install target environment after applying updates",
        "python-update-build-backend": "Raise the uv_build floor independently",
        "docker-pin-latest": "Pin latest and untagged Docker images",
    }
    for name, help_text in booleans.items():
        result.add_argument(
            f"--{name}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=help_text,
        )
    result.add_argument(
        "--no-major-version-updates",
        dest="allow_major",
        action="store_false",
        default=None,
        help="Alias for --no-allow-major",
    )
    result.add_argument(
        "--enable-docker-latest-to-sha-updates",
        dest="docker_pin_latest",
        action="store_true",
        default=None,
        help="Alias for --docker-pin-latest",
    )
    result.add_argument(
        "--add-python-latest-version",
        dest="python_add_minimums",
        action="store_true",
        default=None,
        help="Alias for --python-add-minimums",
    )
    result.add_argument(
        "--no-update-python-inline-script-metadata",
        dest="python_inline_scripts",
        action="store_false",
        default=None,
        help="Alias for --no-python-inline-scripts",
    )
    result.add_argument(
        "--update-docker-tags",
        action="store_true",
        help="Deprecated compatibility option (has no effect)",
    )
    result.add_argument("--min-age", dest="min_age_hours", type=int, default=None, metavar="HOURS")
    result.add_argument("--stale-days", type=int, default=None)
    result.add_argument("--timeout", type=int, default=None, help="Per-command timeout in seconds")
    result.add_argument(
        "--retries",
        type=int,
        default=None,
        help="Retries for transient read-only lookups",
    )
    result.add_argument(
        "--include",
        action="append",
        default=None,
        help="File glob; repeat to add patterns",
    )
    result.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="File glob; repeat to add patterns",
    )
    result.add_argument(
        "--progress",
        choices=("auto", "plain", "off"),
        default=None,
        help="Progress on stderr: auto, plain, or off",
    )
    result.add_argument("--format", choices=("text", "json"), default="text")
    result.add_argument("--output", type=Path, help="Write the JSON report to this path")
    result.add_argument(
        "--print-config",
        action="store_true",
        help="Print effective configuration as JSON and exit",
    )
    result.add_argument(
        "--tui",
        action="store_true",
        help="Configure interactively (requires the tui extra and a terminal)",
    )
    return result


def interactive(config: Config, root: Path) -> Config:
    from .context import RunContext, use_context

    try:
        import questionary  # noqa: F401

        from .tui import prompt_tui
    except ImportError as error:
        raise ValueError("Install update-dependencies[tui] to use --tui") from error
    values = argparse.Namespace(
        only=None,
        ecosystems=config.ecosystems,
        dry_run=config.dry_run,
        allow_major=config.allow_major,
        min_age=config.min_age_hours,
        add_python_latest_version=config.python_add_minimums,
        update_python_inline_script_metadata=config.python_inline_scripts,
        enable_docker_latest_to_sha_updates=config.docker_pin_latest,
    )
    with use_context(RunContext(root, config)):
        values = prompt_tui(values)
    return config.overlay(
        {
            "ecosystems": tuple(values.ecosystems),
            "dry_run": values.dry_run,
            "allow_major": values.allow_major,
            "min_age_hours": values.min_age,
            "python_add_minimums": values.add_python_latest_version,
            "python_inline_scripts": values.update_python_inline_script_metadata,
            "docker_pin_latest": values.enable_docker_latest_to_sha_updates,
        }
    )


def effective_config(args) -> tuple[Path, Config]:
    root = args.directory.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("--directory must refer to a directory")
    config = load_config(root, args.config, args.no_config)
    overrides = {key: getattr(args, key) for key in Config.__dataclass_fields__ if hasattr(args, key)}
    for key in ("include", "exclude"):
        if overrides.get(key) is not None:
            overrides[key] = tuple(overrides[key])
    if args.only:
        overrides["ecosystems"] = (args.only,)
    overrides["python_sync"] = args.sync
    config = config.overlay(overrides)
    return root, config


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        root, config = effective_config(args)
        if args.update_docker_tags:
            print(
                "warning: --update-docker-tags is deprecated and has no effect",
                file=sys.stderr,
            )
        if args.tui:
            config = interactive(config, root)
        if args.print_config:
            print(json.dumps(config.to_dict(), indent=2))
            return 0
        output = args.output.expanduser().absolute() if args.output else None
        if output and output.exists():
            if output.is_symlink():
                raise ValueError("--output cannot overwrite a symlink")
            try:
                previous = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError("--output refuses to overwrite an existing non-report file") from error
            if (
                not isinstance(previous, dict)
                or "changed_files" not in previous
                or previous.get("schema_version") != 1
            ):
                raise ValueError("--output refuses to overwrite an existing non-report file")
        output = output.resolve() if output else None
        with Progress(config.progress):
            report = run(root, config, explicit=bool(args.only or args.ecosystems))
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(report.json(), encoding="utf-8")
        github_report(report, output)
        if args.format == "json":
            print(report.json(), end="")
        else:
            mode = "Would update" if report.dry_run else "Updated"
            print(f"{mode} {len(report.changed_files)} file(s).")
            for path in report.changed_files:
                print(f"  {path}")
            for change in report.changes:
                note = f" ({change['note']})" if change.get("note") else ""
                print(f"  {change['ecosystem']}: {change['name']} {change['old']} -> {change['new']}{note}")
            for item in report.skipped:
                print(f"Skipped {item.get('ecosystem', item.get('file', ''))}: {item['reason']}")
        return 1 if config.check and report.changed_files else 0
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        if args.format == "json":
            print(json.dumps({"schema_version": 1, "error": redact(str(error))}))
        else:
            print(f"error: {redact(str(error))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
