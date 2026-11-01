"""Install and smoke-test wheels and sdists outside the source checkout."""

import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path


def main():
    directory = Path("dist").resolve()
    artifacts = sorted([*directory.glob("*.whl"), *directory.glob("*.tar.gz")])
    if len(artifacts) != 2 or {p.suffix for p in artifacts} != {".whl", ".gz"}:
        raise SystemExit("Expected exactly one wheel and one source distribution")
    for artifact in artifacts:
        if artifact.suffix == ".gz":
            with tarfile.open(artifact) as archive:
                names = {Path(*Path(member.name).parts[1:]).as_posix() for member in archive.getmembers()}
            required = {
                ".github/scripts/bump_version.py",
                ".github/scripts/release.py",
                ".github/scripts/dependency_pr.py",
                ".github/workflows/release.yml",
                "scripts/pre_commit_hooks/check_yaml.py",
                "action.yml",
                "uv.lock",
                "tests/test_release.py",
                "tests/test_release_lifecycle.py",
                "tests/test_display.py",
            }
            if missing := required - names:
                raise SystemExit(f"Source distribution is missing required files: {sorted(missing)}")
        with tempfile.TemporaryDirectory(prefix="verify-update-dependencies-") as directory:
            root = Path(directory)
            env = root / "env"
            subprocess.run(["uv", "venv", "--python", "3.14", str(env)], check=True)
            python = env / "bin" / "python"
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), str(artifact)],
                cwd=root,
                check=True,
            )
            working = root / "working"
            working.mkdir()
            command = env / "bin" / "update-dependencies"
            subprocess.run([str(command), "--version"], cwd=root, check=True)
            subprocess.run(
                [str(python), "-m", "update_dependencies", "--help"],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [str(command), "--no-config", "--print-config"],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            result = subprocess.run(
                [
                    str(command),
                    "--directory",
                    str(working),
                    "--no-config",
                    "--dry-run",
                    "--plain",
                    "--format",
                    "json",
                ],
                cwd=working,
                check=True,
                env={**os.environ, "FORCE_COLOR": "1"},
                capture_output=True,
                text=True,
            )
            report = json.loads(result.stdout)
            if report.get("schema_version") != 1 or report.get("changed") is not False:
                raise SystemExit("Installed distribution did not produce the expected JSON report")
            if "\x1b" in result.stdout + result.stderr:
                raise SystemExit("Plain installed CLI emitted terminal control codes")


if __name__ == "__main__":
    main()
