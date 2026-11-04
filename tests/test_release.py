"""Release version planning, default choice, and failure recovery."""

import runpy
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/bump_version.py"
release = runpy.run_path(str(SCRIPT))


@pytest.mark.parametrize(("bump", "expected"), [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")])
def test_release_bump_semantics(bump, expected):
    assert release["next_version"]("1.2.3", bump, []) == expected


def test_unmerged_version_pr_does_not_reuse_an_existing_tag():
    assert (
        release["next_version"]("0.1.0", "patch", ["v0.1.0", "v0.1.1", "v1.0.0rc1", "unrelated"]) == "0.1.2"
    )


@pytest.mark.parametrize("current", ["1.2.3rc1", "1.2", "01.2.3"])
def test_unsupported_project_versions_are_rejected(current):
    with pytest.raises(ValueError, match="stable"):
        release["next_version"](current, "patch", [])


def test_failed_uv_bump_restores_manifest_and_lock(tmp_path, monkeypatch):
    project = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    original = '[project]\nname = "demo"\nversion = "0.1.0"\n'
    project.write_text(original)
    lock.write_text("version = 1\n")

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="v0.1.0\n", stderr="")
        assert command == ["uv", "version", "0.1.1", "--no-sync"]
        project.write_text(original.replace("0.1.0", "0.1.1"))
        lock.unlink()
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        release["prepare"](tmp_path, "patch")
    assert project.read_text() == original
    assert lock.read_text() == "version = 1\n"


def test_dry_run_defaults_to_patch_without_writing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    project = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "0.1.0"\n'
    project.write_text(original)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    assert release["main"](["--directory", str(tmp_path), "--dry-run"]) == 0
    assert "0.1.1" in capsys.readouterr().out
    assert project.read_text() == original


def test_release_workflow_publishes_directly_and_gates_only_on_pypi_environment():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    bump = workflow["on"]["workflow_dispatch"]["inputs"]["bump"]
    assert bump == {
        "description": "Version increment for the release",
        "type": "choice",
        "required": "true",
        "default": "patch",
        "options": ["patch", "minor", "major"],
    }
    # Neither a PR gate nor a CI workflow_run gate: the only approval is the pypi environment.
    assert set(workflow["on"]) == {"workflow_dispatch", "push"}
    assert workflow["on"]["push"] == {"tags": ["v*"]}

    jobs = workflow["jobs"]
    assert jobs["prepare"]["permissions"] == {"contents": "write"}
    prepare_commands = "\n".join(step.get("run", "") for step in jobs["prepare"]["steps"])
    assert "release.py publish" in prepare_commands
    assert "open-pr" not in prepare_commands
    assert "gh pr" not in prepare_commands
    assert "git tag" not in prepare_commands

    assert jobs["build"]["needs"] == "prepare"
    assert jobs["build"]["permissions"] == {"contents": "read"}
    build_commands = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    assert "release.py verify" in build_commands
    assert "github.ref_type == 'tag'" in jobs["build"]["if"]
    assert "needs.prepare.result == 'success'" in jobs["build"]["if"]

    assert jobs["publish"]["needs"] == "build"
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["release"]["needs"] == ["build", "publish"]
    assert "needs.build.outputs.tag" in jobs["release"]["steps"][0]["env"]["RELEASE_TAG"]
    assert all("timeout-minutes" in job for job in jobs.values())


def test_real_uv_bump_updates_lock_without_sync(tmp_path, monkeypatch):
    import sys
    import tomllib

    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_OFFLINE", "true")
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "release-fixture"\nversion = "0.1.0"\nrequires-python = ">=3.14,<3.15"\ndependencies = []\n[tool.uv]\npackage = false\n'
    )
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    assert release["prepare"](tmp_path, "minor") == "0.2.0"
    assert tomllib.loads(project.read_text())["project"]["version"] == "0.2.0"
    packages = tomllib.loads((tmp_path / "uv.lock").read_text())["package"]
    assert next(package for package in packages if package["name"] == "release-fixture")["version"] == "0.2.0"
    assert not (tmp_path / ".venv").exists()


def test_successful_uv_command_with_stale_lock_is_rolled_back(tmp_path, monkeypatch):
    project = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    original = '[project]\nname = "demo"\nversion = "0.1.0"\n'
    original_lock = 'version = 1\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
    project.write_text(original)
    lock.write_text(original_lock)

    def fake_run(command, **kwargs):
        if command[0] != "git":
            project.write_text(original.replace("0.1.0", "0.1.1"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="inconsistent"):
        release["prepare"](tmp_path, "patch")
    assert project.read_text() == original
    assert lock.read_text() == original_lock
