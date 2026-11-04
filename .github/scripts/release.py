"""Commit a validated version to the default branch, tag it, and verify it before publishing."""

import argparse
import os
import re
import subprocess
import tomllib
from pathlib import Path

VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def run(root: Path, *args: str) -> str:
    return subprocess.check_output(args, cwd=root, text=True).strip()


def version_tuple(version: str) -> tuple[int, ...]:
    if not VERSION.fullmatch(version):
        raise ValueError("Releases require a stable X.Y.Z version")
    return tuple(map(int, version.split(".")))


def metadata_version(project: str, lock: str) -> str:
    project = tomllib.loads(project)["project"]
    version = project["version"]
    version_tuple(version)
    packages = tomllib.loads(lock).get("package", [])
    matches = [p for p in packages if p.get("name") == project["name"]]
    if len(matches) != 1 or matches[0].get("version") != version:
        raise ValueError("Project version and lockfile must match")
    return version


def committed_version(root: Path) -> str:
    return metadata_version(
        run(root, "git", "show", "HEAD:pyproject.toml"), run(root, "git", "show", "HEAD:uv.lock")
    )


def require_merged(root: Path, default_branch: str) -> None:
    """Reject tags that do not point at code already on the default branch."""
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default_branch}"],
        cwd=root,
        check=True,
    )


def bot_identity(root: Path) -> None:
    run(root, "git", "config", "user.name", "github-actions[bot]")
    run(root, "git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")


def publish(root: Path, default_branch: str) -> str:
    """Commit the prepared manifest and lockfile to the default branch, then tag that commit.

    The branch push is non-fast-forward safe: if the default branch moved since checkout, the
    push fails and no tag is created. The tag is pushed only after the commit is on the branch.
    """
    if run(root, "git", "symbolic-ref", "--short", "HEAD") != default_branch:
        raise ValueError("Releases can only be committed from the default branch")
    version = metadata_version((root / "pyproject.toml").read_text(), (root / "uv.lock").read_text())
    tag = f"v{version}"
    if run(root, "git", "tag", "--list", tag):
        raise ValueError(f"Tag {tag} already exists; refusing to reuse a released version")
    changed = set(filter(None, run(root, "git", "diff", "--name-only", "-z").split("\0")))
    staged = run(root, "git", "diff", "--cached", "--name-only")
    untracked = run(root, "git", "ls-files", "--others", "--exclude-standard")
    if staged or untracked or changed != {"pyproject.toml", "uv.lock"}:
        raise ValueError("Release commit must contain only manifest and lockfile changes")
    bot_identity(root)
    run(root, "git", "add", "--", "pyproject.toml", "uv.lock")
    run(root, "git", "commit", "-m", f"Release {tag}")
    run(root, "git", "push", "origin", f"HEAD:refs/heads/{default_branch}")
    run(root, "git", "tag", "-a", tag, "-m", f"Release {tag}")
    run(root, "git", "push", "origin", f"refs/tags/{tag}")
    return tag


def verify_tag(root: Path, default_branch: str, tag: str) -> None:
    """Confirm the checked-out tag is a default-branch commit whose metadata matches the tag."""
    require_merged(root, default_branch)
    if tag != f"v{committed_version(root)}":
        raise ValueError("Release tag must match project.version")
    target = run(root, "git", "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if target != run(root, "git", "rev-parse", "HEAD"):
        raise ValueError("Release tag must identify the checked-out commit")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish", "verify"))
    args = parser.parse_args()
    root = Path.cwd()
    default_branch = os.environ["DEFAULT_BRANCH"]
    match args.command:
        case "publish":
            print(publish(root, default_branch))
        case "verify":
            verify_tag(root, default_branch, os.environ["RELEASE_TAG"])


if __name__ == "__main__":
    main()
