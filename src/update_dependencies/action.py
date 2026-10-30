"""Translate composite Action inputs to CLI arguments without shell evaluation."""

import os
from pathlib import Path

from .cli import effective_config, main, parser

# Each supported CLI setting is represented explicitly; no raw shell arguments.
BOOLEAN_INPUTS = (
    "dry-run",
    "check",
    "allow-major",
    "python-inline-scripts",
    "python-add-minimums",
    "sync",
    "python-update-build-backend",
    "docker-pin-latest",
    "no-config",
)
VALUE_INPUTS = (
    "directory",
    "config",
    "ecosystems",
    "min-age",
    "stale-days",
    "timeout",
    "retries",
    "progress",
    "format",
    "output",
)
LIST_INPUTS = ("include", "exclude")


def arguments(environment: dict[str, str]) -> list[str]:
    result = []
    for name in BOOLEAN_INPUTS:
        value = environment.get("UD_" + name.upper().replace("-", "_"), "").strip().lower()
        if not value:
            continue
        if value not in {"true", "false"}:
            raise ValueError(f"Action input {name} must be true or false")
        if name == "no-config":
            if value == "true":
                result.append("--no-config")
        else:
            result.append(f"--{name}" if value == "true" else f"--no-{name}")
    for name in VALUE_INPUTS:
        value = environment.get("UD_" + name.upper().replace("-", "_"), "")
        if value:
            result.extend((f"--{name}", value))
    for name in LIST_INPUTS:
        for value in environment.get("UD_" + name.upper(), "").splitlines():
            if value.strip():
                result.extend((f"--{name}", value.strip()))
    if "--output" not in result:
        result.extend(
            (
                "--output",
                str(Path(environment.get("RUNNER_TEMP", ".")) / "update-dependencies-report.json"),
            )
        )
    return result


def run() -> int:
    try:
        args = arguments(dict(os.environ))
    except ValueError as error:
        import sys

        print(error, file=sys.stderr)
        return 2
    import sys

    if sys.argv[1:] == ["--prepare"]:
        try:
            root, config = effective_config(parser().parse_args(args))
            from .context import RunContext, use_context
            from .discovery import inventory, matches
            from .engine import skip_reason

            files = [p for p in inventory(root) if matches(p, config.include, config.exclude)]
            with use_context(RunContext(root, config, files=files)):
                needs_docker = "docker" in config.ecosystems and skip_reason("docker") is None
            output = Path(os.environ["GITHUB_OUTPUT"])
            with output.open("a", encoding="utf-8") as stream:
                stream.write(f"docker={'true' if needs_docker else 'false'}\n")
            return 0
        except (OSError, ValueError, TypeError) as error:
            print(error, file=sys.stderr)
            return 2
    return main(args)


if __name__ == "__main__":
    raise SystemExit(run())
