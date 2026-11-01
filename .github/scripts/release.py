"""Open release PRs and allow tags/publishing only for merged, validated code."""

import argparse
import json
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
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default_branch}"],
        cwd=root,
        check=True,
    )


def require_ci(root: Path, default_branch: str) -> None:
    head = run(root, "git", "rev-parse", "HEAD")
    runs = json.loads(
        run(
            root,
            "gh",
            "run",
            "list",
            "--workflow",
            "ci.yml",
            "--branch",
            default_branch,
            "--commit",
            head,
            "--event",
            "push",
            "--limit",
            "100",
            "--json",
            "headSha,status,conclusion",
        )
    )
    # GitHub lists newest runs first. An older success must not hide a newer failure or pending run.
    latest = next((r for r in runs if r["headSha"] == head), None)
    if latest is None or latest["status"] != "completed" or latest["conclusion"] != "success":
        raise ValueError("CI must pass on this exact default-branch commit before releasing")


def release_tag(root: Path, default_branch: str) -> str | None:
    """Plan from a tested main commit, including recovery after an earlier CI failure."""
    require_merged(root, default_branch)
    version = committed_version(root)
    tag = f"v{version}"
    reachable = run(root, "git", "tag", "--merged", "HEAD", "--list", "v*").splitlines()
    stable = [version_tuple(t[1:]) for t in reachable if VERSION.fullmatch(t[1:])]
    current = version_tuple(version)
    if stable:
        if current < max(stable):
            raise ValueError("Project version is behind a released version")
        if current == max(stable):
            return None
    else:
        # Installing the workflow must not publish an existing, unchanged version.
        previous = tomllib.loads(run(root, "git", "show", "HEAD^:pyproject.toml"))["project"]["version"]
        if current <= version_tuple(previous):
            return None
    all_tags = run(root, "git", "tag", "--list", tag).splitlines()
    if tag in all_tags:
        raise ValueError("Release tag already exists outside the merged history; refusing to move it")
    return tag


def verify_tag(root: Path, default_branch: str, tag: str) -> None:
    require_merged(root, default_branch)
    if tag != f"v{committed_version(root)}":
        raise ValueError("Release tag must match project.version")
    target = run(root, "git", "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if target != run(root, "git", "rev-parse", "HEAD"):
        raise ValueError("Release tag must identify the checked-out commit")


def check_pending(root: Path, default_branch: str) -> None:
    prs = json.loads(
        run(
            root,
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            default_branch,
            "--limit",
            "1000",
            "--json",
            "headRefName,url",
        )
    )
    pending = [p["url"] for p in prs if p["headRefName"].startswith("release/v")]
    if pending:
        raise ValueError("Resolve the existing release PR before starting another: " + ", ".join(pending))


def open_pr(root: Path, default_branch: str) -> str:
    check_pending(root, default_branch)
    version = metadata_version((root / "pyproject.toml").read_text(), (root / "uv.lock").read_text())
    changed = set(filter(None, run(root, "git", "diff", "--name-only", "-z").split("\0")))
    staged = run(root, "git", "diff", "--cached", "--name-only")
    untracked = run(root, "git", "ls-files", "--others", "--exclude-standard")
    if staged or untracked or changed != {"pyproject.toml", "uv.lock"}:
        raise ValueError("Release PR must contain only manifest and lockfile changes")
    branch = f"release/v{version}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    run(root, "git", "check-ref-format", "--branch", branch)
    run(root, "git", "switch", "-c", branch)
    bot_identity(root)
    run(root, "git", "add", "--", "pyproject.toml", "uv.lock")
    run(root, "git", "commit", "-m", f"Bump version to v{version}")
    run(root, "git", "push", "origin", branch)
    url = run(
        root,
        "gh",
        "pr",
        "create",
        "--base",
        default_branch,
        "--head",
        branch,
        "--title",
        f"Bump version to v{version}",
        "--body",
        "Merge this validated version PR to start the release. No tag or package has been published. "
        "After merge, CI on the default branch must pass before tagging and publishing can start.",
    )
    run(root, "gh", "workflow", "run", "ci.yml", "--ref", branch)
    return url


def bot_identity(root: Path) -> None:
    run(root, "git", "config", "user.name", "github-actions[bot]")
    run(root, "git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")


def tag_merged(root: Path, default_branch: str) -> str | None:
    tag = release_tag(root, default_branch)
    if tag is None:
        return None
    require_ci(root, default_branch)
    bot_identity(root)
    run(root, "git", "tag", "-a", tag, "-m", f"Release {tag}")
    run(root, "git", "push", "origin", f"refs/tags/{tag}")
    # Token-created tag pushes do not trigger push workflows; dispatch the immutable tag.
    run(root, "gh", "workflow", "run", "release.yml", "--ref", tag)
    return tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check-pending", "open-pr", "tag", "verify"))
    args = parser.parse_args()
    root = Path.cwd()
    default_branch = os.environ["DEFAULT_BRANCH"]
    match args.command:
        case "check-pending":
            check_pending(root, default_branch)
        case "open-pr":
            print(open_pr(root, default_branch))
        case "tag":
            print(tag_merged(root, default_branch) or "No unreleased version; no tag created")
        case "verify":
            verify_tag(root, default_branch, os.environ["RELEASE_TAG"])
            require_ci(root, default_branch)


if __name__ == "__main__":
    main()
