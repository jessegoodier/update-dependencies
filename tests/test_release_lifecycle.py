"""Exercise release commit and tag boundaries with real Git history and mocked pushes."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/release.py"
spec = importlib.util.spec_from_file_location("release_automation", SCRIPT)
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")

    def write(version, lock_version=None):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "demo"\nversion = "{version}"\n')
        lock_version = version if lock_version is None else lock_version
        (tmp_path / "uv.lock").write_text(
            f'version = 1\n[[package]]\nname = "demo"\nversion = "{lock_version}"\n'
        )

    def commit(version):
        write(version)
        git("add", "pyproject.toml", "uv.lock")
        git("commit", "-qm", f"Version {version}")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        return git("rev-parse", "HEAD")

    commit("0.1.0")
    git("tag", "v0.1.0")
    return tmp_path, git, write, commit


def network(monkeypatch, *, fail_push=False):
    calls = []
    original = release.run

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("git", "push"):
            if fail_push:
                raise subprocess.CalledProcessError(1, args)
            return ""
        if args[0] == "gh":
            pytest.fail(f"Unexpected network operation: {args}")
        return original(root, *args)

    monkeypatch.setattr(release, "run", run)
    return calls


def test_publish_commits_manifest_and_lock_then_pushes_main_before_tagging(repository, monkeypatch):
    root, git, write, _ = repository
    write("0.1.1")
    calls = network(monkeypatch)
    assert release.publish(root, "main") == "v0.1.1"
    commit_index = calls.index(("git", "commit", "-m", "Release v0.1.1"))
    main_push = calls.index(("git", "push", "origin", "HEAD:refs/heads/main"))
    tag = calls.index(("git", "tag", "-a", "v0.1.1", "-m", "Release v0.1.1"))
    tag_push = calls.index(("git", "push", "origin", "refs/tags/v0.1.1"))
    assert commit_index < main_push < tag < tag_push
    assert git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == [
        "pyproject.toml",
        "uv.lock",
    ]
    assert git("rev-parse", "v0.1.1^{commit}") == git("rev-parse", "HEAD")
    assert not any(call[0] == "gh" for call in calls)


def test_failed_main_push_creates_no_tag(repository, monkeypatch):
    root, git, write, _ = repository
    write("0.1.1")
    calls = network(monkeypatch, fail_push=True)
    with pytest.raises(subprocess.CalledProcessError):
        release.publish(root, "main")
    assert git("tag", "--list", "v0.1.1") == ""
    assert ("git", "push", "origin", "refs/tags/v0.1.1") not in calls


def test_existing_tag_blocks_publish_before_any_push(repository, monkeypatch):
    root, git, write, _ = repository
    git("tag", "v0.1.1")
    write("0.1.1")
    calls = network(monkeypatch)
    with pytest.raises(ValueError, match="already exists"):
        release.publish(root, "main")
    assert not any(call[:2] == ("git", "push") for call in calls)
    assert git("rev-parse", "v0.1.1") == git("rev-parse", "v0.1.0")


@pytest.mark.parametrize("kind", ["untracked", "staged", "modified"])
def test_unrelated_changes_block_publish(repository, monkeypatch, kind):
    root, git, write, _ = repository
    (root / "README.md").write_text("Original\n")
    git("add", "README.md")
    git("commit", "-qm", "Docs")
    write("0.1.1")
    path = root / ("extra.txt" if kind == "untracked" else "README.md")
    path.write_text("Do not publish\n")
    if kind == "staged":
        git("add", "README.md")
    calls = network(monkeypatch)
    with pytest.raises(ValueError, match="only manifest and lockfile"):
        release.publish(root, "main")
    assert not any(call[:2] == ("git", "push") for call in calls)
    assert git("tag", "--list", "v0.1.1") == ""


def test_publish_refuses_to_run_off_the_default_branch(repository, monkeypatch):
    root, git, write, _ = repository
    git("switch", "-qc", "feature")
    write("0.1.1")
    calls = network(monkeypatch)
    with pytest.raises(ValueError, match="default branch"):
        release.publish(root, "main")
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_lockfile_mismatch_blocks_publish(repository, monkeypatch):
    root, _, write, _ = repository
    write("0.1.1", lock_version="0.1.0")
    calls = network(monkeypatch)
    with pytest.raises(ValueError, match="lockfile must match"):
        release.publish(root, "main")
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_verify_accepts_tag_on_default_branch_commit(repository):
    root, git, _, commit = repository
    head = commit("0.1.1")
    git("tag", "v0.1.1")
    release.verify_tag(root, "main", "v0.1.1")
    assert git("rev-parse", "v0.1.1^{commit}") == head


def test_verify_rejects_mismatched_tag_or_tag_target(repository):
    root, git, _, commit = repository
    commit("0.1.1")
    with pytest.raises(ValueError, match="must match"):
        release.verify_tag(root, "main", "v0.1.0")
    git("tag", "v0.1.1", "v0.1.0")
    with pytest.raises(ValueError, match="checked-out commit"):
        release.verify_tag(root, "main", "v0.1.1")


def test_verify_rejects_tag_on_commit_not_on_default_branch(repository):
    root, git, write, _ = repository
    git("switch", "-qc", "unmerged")
    (root / "side-branch.txt").write_text("Not on main\n")
    git("add", "side-branch.txt")
    write("0.1.1")
    git("add", "pyproject.toml", "uv.lock")
    git("commit", "-qm", "Unmerged version")
    git("tag", "v0.1.1")
    with pytest.raises(subprocess.CalledProcessError):
        release.verify_tag(root, "main", "v0.1.1")
