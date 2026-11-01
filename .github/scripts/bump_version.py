"""Prepare a stable release version and lockfile without committing or publishing."""

import argparse
import os
import re
import subprocess
import tomllib
from pathlib import Path

VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def next_version(current: str, bump: str, tags: list[str]) -> str:
    if bump not in {"patch", "minor", "major"}:
        raise ValueError("bump must be patch, minor, or major")
    match = VERSION.fullmatch(current)
    if match is None:
        raise ValueError("Automated releases require a stable X.Y.Z project version")
    versions = [tuple(map(int, match.groups()))]
    for tag in tags:
        if tag.startswith("v") and (match := VERSION.fullmatch(tag[1:])):
            versions.append(tuple(map(int, match.groups())))
    major, minor, patch = max(versions)
    match bump:
        case "major":
            return f"{major + 1}.0.0"
        case "minor":
            return f"{major}.{minor + 1}.0"
        case _:
            return f"{major}.{minor}.{patch + 1}"


def prepare(root: Path, bump: str, *, dry_run: bool = False) -> str:
    manifest = root / "pyproject.toml"
    lock = root / "uv.lock"
    before = {path: path.read_bytes() if path.is_file() else None for path in (manifest, lock)}
    current = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["version"]
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.splitlines()
    version = next_version(current, bump, tags)
    if dry_run:
        return version
    try:
        # uv changes project metadata and re-locks without installing the target environment.
        subprocess.run(["uv", "version", version, "--no-sync"], cwd=root, check=True)
        actual = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["version"]
        if actual != version or not lock.is_file():
            raise ValueError("Version preparation did not update the manifest and lockfile")
        name = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["name"]
        packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
        matching = [package for package in packages if package.get("name") == name]
        if len(matching) != 1 or matching[0].get("version") != version:
            raise ValueError("Version preparation left inconsistent project and lockfile versions")
    except BaseException:
        for path, contents in before.items():
            if contents is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(contents)
        raise
    return version


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--directory", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    version = prepare(args.directory.resolve(), args.bump, dry_run=args.dry_run)
    print(f"Release version: {version}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"version={version}\ntag=v{version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
