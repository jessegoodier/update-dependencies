"""Install and smoke-test wheels and sdists outside the source checkout."""

import subprocess
import tempfile
from pathlib import Path


def main():
    directory = Path("dist").resolve()
    artifacts = sorted([*directory.glob("*.whl"), *directory.glob("*.tar.gz")])
    if len(artifacts) != 2 or {p.suffix for p in artifacts} != {".whl", ".gz"}:
        raise SystemExit("Expected exactly one wheel and one source distribution")
    for artifact in artifacts:
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
            subprocess.run(
                [
                    str(command),
                    "--directory",
                    str(working),
                    "--no-config",
                    "--dry-run",
                    "--format",
                    "json",
                ],
                cwd=working,
                check=True,
                stdout=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    main()
