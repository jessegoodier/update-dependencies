"""Exercise the update PR commit boundary against a real temporary Git repo."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/dependency_pr.py"
spec = importlib.util.spec_from_file_location("dependency_pr", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (tmp_path / "uv.lock").write_text("old\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    (tmp_path / "uv.lock").write_text("updated\n")
    report = tmp_path / ".git/report.json"
    report.write_text(json.dumps({"dry_run": False, "changed_files": ["uv.lock"]}))
    return tmp_path, report, git


def test_stages_exact_report(repository):
    _, report, git = repository
    assert module.stage_report(report) == ["uv.lock"]
    assert git("diff", "--cached", "--name-only") == "uv.lock"


@pytest.mark.parametrize("kind", ["modified", "untracked", "staged"])
def test_refuses_unrelated_changes(repository, kind):
    root, report, git = repository
    if kind == "modified":
        git("checkout", "--", "uv.lock")
    else:
        (root / "unrelated.txt").write_text("do not commit\n")
        if kind == "staged":
            git("add", "unrelated.txt")
    with pytest.raises(ValueError, match="Working tree"):
        module.stage_report(report)


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", ".git/config", "", 42])
def test_refuses_unsafe_paths(repository, name):
    _, report, git = repository
    report.write_text(json.dumps({"dry_run": False, "changed_files": [name]}))
    with pytest.raises((ValueError, TypeError)):
        module.stage_report(report)
    assert git("diff", "--cached", "--name-only") == ""


def test_refuses_symlink(repository):
    root, report, _ = repository
    (root / "uv.lock").unlink()
    (root / "uv.lock").symlink_to(report)
    with pytest.raises(ValueError, match="regular file"):
        module.stage_report(report)


@pytest.mark.parametrize(
    "report_data",
    [
        {"dry_run": True, "changed_files": ["uv.lock"]},
        {"changed_files": ["uv.lock"]},
        {"dry_run": False, "changed_files": []},
    ],
)
def test_refuses_preview_and_empty_reports(repository, report_data):
    _, report, _ = repository
    report.write_text(json.dumps(report_data))
    with pytest.raises((ValueError, TypeError)):
        module.stage_report(report)


def test_main_stops_before_publish_when_report_is_invalid(repository, monkeypatch):
    _, report, _ = repository
    report.write_text(json.dumps({"dry_run": True, "changed_files": ["uv.lock"]}))
    monkeypatch.setenv("UPDATE_REPORT", str(report))
    calls = []
    monkeypatch.setattr(module, "run", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="applied"):
        module.main()
    assert calls == []


def test_main_dispatches_ci_on_published_branch(repository, monkeypatch):
    _, report, _ = repository
    monkeypatch.setenv("UPDATE_REPORT", str(report))
    monkeypatch.setenv("UPDATE_BRANCH", "automation/dependencies-123-1")
    monkeypatch.setenv("BASE_BRANCH", "main")
    monkeypatch.setattr(module, "stage_report", lambda path: ["uv.lock"])
    calls = []

    def run(*args):
        calls.append(args)
        return "https://github.com/owner/repo/pull/1" if args[:3] == ("gh", "pr", "create") else ""

    monkeypatch.setattr(module, "run", run)
    module.main()
    push = ("git", "push", "origin", "automation/dependencies-123-1")
    dispatch = ("gh", "workflow", "run", "ci.yml", "--ref", "automation/dependencies-123-1")
    assert push in calls and calls[-1] == dispatch
    create = next(call for call in calls if call[:3] == ("gh", "pr", "create"))
    assert create[create.index("--base") + 1] == "main"
    assert calls.index(push) < calls.index(create) < calls.index(dispatch)
