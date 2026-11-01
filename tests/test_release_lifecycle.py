"""Exercise release boundaries with real Git history and mocked network writes."""

import importlib.util
import json
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

    def write(version):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "demo"\nversion = "{version}"\n')
        (tmp_path / "uv.lock").write_text(f'version = 1\n[[package]]\nname = "demo"\nversion = "{version}"\n')

    def commit(version, *, merged=True):
        write(version)
        git("add", "pyproject.toml", "uv.lock")
        git("commit", "-qm", f"Version {version}")
        if merged:
            git("update-ref", "refs/remotes/origin/main", "HEAD")
        return git("rev-parse", "HEAD")

    commit("0.1.0")
    git("tag", "v0.1.0")
    return tmp_path, git, write, commit


def network(monkeypatch, *, runs=None, prs=None, fail_push=False):
    calls = []
    original = release.run

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("git", "push"):
            if fail_push:
                raise subprocess.CalledProcessError(1, args)
            return ""
        if args[:3] == ("gh", "run", "list"):
            head = original(root, "git", "rev-parse", "HEAD")
            if runs is not None:
                return json.dumps(
                    [{**r, "headSha": head if r["headSha"] == "HEAD" else r["headSha"]} for r in runs]
                )
            return json.dumps(
                runs
                if runs is not None
                else [{"headSha": head, "status": "completed", "conclusion": "success"}]
            )
        if args[:3] == ("gh", "pr", "list"):
            return json.dumps(prs or [])
        if args[:3] == ("gh", "pr", "create"):
            return "https://github.com/owner/repo/pull/1"
        if args[:3] == ("gh", "workflow", "run"):
            return ""
        if args[0] == "gh":
            pytest.fail(f"Unexpected network operation: {args}")
        return original(root, *args)

    monkeypatch.setattr(release, "run", run)
    return calls


def test_release_requires_version_merge_and_successful_ci(repository, monkeypatch):
    root, git, _, commit = repository
    git("switch", "-qc", "release/v0.1.1")
    commit("0.1.1", merged=False)
    calls = network(monkeypatch)
    with pytest.raises(subprocess.CalledProcessError):
        release.tag_merged(root, "main")
    assert git("tag", "--list", "v0.1.1") == ""
    assert not any(call[0] == "gh" for call in calls)


def test_tag_identifies_tested_merged_commit_and_retry_does_not_republish(repository, monkeypatch):
    root, git, _, commit = repository
    head = commit("0.1.1")
    calls = network(monkeypatch)
    assert release.tag_merged(root, "main") == "v0.1.1"
    assert git("rev-parse", "v0.1.1^{commit}") == head
    push = ("git", "push", "origin", "refs/tags/v0.1.1")
    dispatch = ("gh", "workflow", "run", "release.yml", "--ref", "v0.1.1")
    assert calls.index(push) < calls.index(dispatch)
    assert release.tag_merged(root, "main") is None
    assert calls.count(dispatch) == 1
    release.verify_tag(root, "main", "v0.1.1")


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [{"headSha": "wrong", "status": "completed", "conclusion": "success"}],
        [{"headSha": "wrong", "status": "completed", "conclusion": "failure"}],
        [{"headSha": "HEAD", "status": "completed", "conclusion": "failure"}],
        [{"headSha": "HEAD", "status": "in_progress", "conclusion": ""}],
        [
            {"headSha": "HEAD", "status": "completed", "conclusion": "failure"},
            {"headSha": "HEAD", "status": "completed", "conclusion": "success"},
        ],
    ],
)
def test_failed_missing_or_wrong_commit_ci_creates_no_tag(repository, monkeypatch, runs):
    root, git, _, commit = repository
    commit("0.1.1")
    calls = network(monkeypatch, runs=runs)
    with pytest.raises(ValueError, match="CI must pass"):
        release.tag_merged(root, "main")
    assert git("tag", "--list", "v0.1.1") == ""
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_unchanged_version_after_release_does_not_tag_or_publish(repository, monkeypatch):
    root, git, _, _ = repository
    (root / "README.md").write_text("Unrelated change\n")
    git("add", "README.md")
    git("commit", "-qm", "Docs")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    calls = network(monkeypatch)
    assert release.tag_merged(root, "main") is None
    assert not any(call[0] == "gh" for call in calls)


def test_ci_fix_after_version_merge_still_releases_the_tested_commit(repository):
    root, git, _, commit = repository
    commit("0.1.1")
    (root / "README.md").write_text("Fix following failed CI\n")
    git("add", "README.md")
    git("commit", "-qm", "Fix CI")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    assert release.release_tag(root, "main") == "v0.1.1"


def test_downgrade_is_rejected(repository):
    root, _, _, commit = repository
    commit("0.0.9")
    with pytest.raises(ValueError, match="behind"):
        release.release_tag(root, "main")


def test_existing_tag_outside_main_is_never_moved(repository):
    root, git, _, commit = repository
    git("switch", "-qc", "unmerged")
    (root / "side-branch.txt").write_text("Not merged\n")
    git("add", "side-branch.txt")
    unmerged = commit("0.1.1", merged=False)
    git("tag", "v0.1.1")
    git("switch", "-q", "main")
    commit("0.1.1")
    with pytest.raises(ValueError, match="refusing to move"):
        release.release_tag(root, "main")
    assert git("rev-parse", "v0.1.1") == unmerged


def test_publishing_rejects_mismatched_tag_or_tag_target(repository):
    root, git, _, commit = repository
    commit("0.1.1")
    with pytest.raises(ValueError, match="must match"):
        release.verify_tag(root, "main", "v0.1.0")
    git("tag", "v0.1.1", "v0.1.0")
    with pytest.raises(ValueError, match="checked-out commit"):
        release.verify_tag(root, "main", "v0.1.1")


def test_lockfile_mismatch_blocks_release(repository):
    root, git, _, commit = repository
    commit("0.1.1")
    (root / "uv.lock").write_text('version = 1\n[[package]]\nname = "demo"\nversion = "0.1.0"\n')
    git("add", "uv.lock")
    git("commit", "-qm", "Invalid lock")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    with pytest.raises(ValueError, match="lockfile must match"):
        release.release_tag(root, "main")


def test_existing_release_pr_blocks_duplicate_preparation(repository, monkeypatch):
    root, _, _, _ = repository
    network(monkeypatch, prs=[{"headRefName": "release/v0.1.1-1-1", "url": "https://example.com/pr/1"}])
    with pytest.raises(ValueError, match="existing release PR"):
        release.check_pending(root, "main")


def test_open_pr_never_tags_or_publishes(repository, monkeypatch):
    root, git, write, _ = repository
    write("0.1.1")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    calls = network(monkeypatch)
    assert release.open_pr(root, "main") == "https://github.com/owner/repo/pull/1"
    assert git("tag", "--list") == "v0.1.0"
    assert git("diff", "--name-only", "HEAD^", "HEAD").splitlines() == ["pyproject.toml", "uv.lock"]
    assert ("gh", "workflow", "run", "ci.yml", "--ref", "release/v0.1.1-123-1") in calls
    assert not any(call[:2] == ("git", "tag") or "release.yml" in call for call in calls)


@pytest.mark.parametrize("kind", ["untracked", "staged", "modified"])
def test_unrelated_changes_block_version_pr(repository, monkeypatch, kind):
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
        release.open_pr(root, "main")
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_failed_push_cannot_start_publishing(repository, monkeypatch):
    root, _, _, commit = repository
    commit("0.1.1")
    calls = network(monkeypatch, fail_push=True)
    with pytest.raises(subprocess.CalledProcessError):
        release.tag_merged(root, "main")
    assert not any(call[:3] == ("gh", "workflow", "run") for call in calls)
