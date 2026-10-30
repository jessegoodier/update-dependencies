"""Transactional behavior, isolation, and CLI reporting without registry access."""

import json
import subprocess
from pathlib import Path

import pytest

from update_dependencies import engine, runner
from update_dependencies.cli import main
from update_dependencies.config import Config
from update_dependencies.context import RunContext, use_context
from update_dependencies.discovery import inventory


@pytest.fixture
def fake_image(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda tool: f"/tools/{tool}")
    monkeypatch.setattr(engine, "image_freshness", lambda image: None)
    monkeypatch.setattr(engine, "image_digest", lambda image, **kwargs: "sha256:" + "a" * 64)


def test_preview_and_apply_leave_target_untouched_during_resolution(tmp_path, fake_image, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    original = b"FROM python:3.14-slim\r\n"
    dockerfile.write_bytes(original)

    def resolve(image, **kwargs):
        assert dockerfile.read_bytes() == original
        assert engine.context().root != tmp_path
        return "sha256:" + "a" * 64

    monkeypatch.setattr(engine, "image_digest", resolve)
    config = Config(ecosystems=("docker",), dry_run=True)
    report = runner.run(tmp_path, config)
    assert report.changed_files == ["Dockerfile"]
    assert dockerfile.read_bytes() == original
    report = runner.run(tmp_path, config.overlay({"dry_run": False}))
    assert b"@sha256:" in dockerfile.read_bytes()
    assert dockerfile.read_bytes().endswith(b"\r\n")


def test_staged_failure_discards_all_ecosystem_changes(tmp_path, fake_image, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.14\n")
    hooks = tmp_path / ".pre-commit-config.yaml"
    hooks.write_text("repos: []\n")

    def fail(**kwargs):
        assert "@sha256:" in (engine.context().root / "Dockerfile").read_text()
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(engine, "update_hooks", fail)
    with pytest.raises(RuntimeError, match="upstream"):
        runner.run(tmp_path, Config(ecosystems=("docker", "hooks")))
    assert dockerfile.read_text() == "FROM python:3.14\n"


def test_concurrent_edit_is_preserved(tmp_path, fake_image, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.14\n")

    def resolve(image, **kwargs):
        dockerfile.write_text("FROM scratch\n")
        return "sha256:" + "a" * 64

    monkeypatch.setattr(engine, "image_digest", resolve)
    with pytest.raises(ValueError, match="changed during update"):
        runner.run(tmp_path, Config(ecosystems=("docker",)))
    assert dockerfile.read_text() == "FROM scratch\n"


def test_file_filters_and_new_invocations_do_not_share_caches(tmp_path, fake_image):
    (tmp_path / "Dockerfile").write_text("FROM python:3.14\n")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "Dockerfile").write_text("FROM python:3.14\n")
    first = runner.run(tmp_path, Config(ecosystems=("docker",), exclude=("vendor/**",), dry_run=True))
    second = runner.run(tmp_path, Config(ecosystems=("docker",), include=("vendor/**",), dry_run=True))
    assert first.changed_files == ["Dockerfile"]
    assert second.changed_files == ["vendor/Dockerfile"]


def test_check_exit_status_and_json_report(tmp_path, fake_image, capsys, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM python:3.14\n")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert (
        main(
            [
                "--directory",
                str(tmp_path),
                "--only",
                "docker",
                "--check",
                "--format",
                "json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["changed_files"] == ["Dockerfile"]
    assert "changed<<" in output.read_text()
    assert "@sha256" not in (tmp_path / "Dockerfile").read_text()


def test_root_and_nested_action_metadata_discovery(tmp_path):
    for name in (
        "action.yml",
        "custom/action.yaml",
        ".github/workflows/ci.yml",
        "unrelated.yml",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("name: test\n")
    with use_context(RunContext(tmp_path, Config())):
        assert {p.relative_to(tmp_path).as_posix() for p in engine.find_action_files()} == {
            "action.yml",
            "custom/action.yaml",
            ".github/workflows/ci.yml",
        }


def test_inventory_respects_gitignore_and_symlinks(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored/Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "linked").symlink_to(tmp_path / "Dockerfile")
    assert Path("Dockerfile") in inventory(tmp_path)
    assert Path("linked") not in inventory(tmp_path)
    assert Path("ignored/Dockerfile") not in inventory(tmp_path)


@pytest.mark.parametrize(
    ("spec", "locked", "upper"),
    [
        ("foo>=1", "1.8.0", "<2"),
        ("foo>=0.4", "0.4.8", "<0.5"),
        ("foo==2.0; python_version >= '3.14'", "2.1", "<3"),
    ],
)
def test_hold_major_lines_without_existing_upper_bound(spec, locked, upper):
    text = f'[project]\ndependencies = ["{spec}"]\n'
    assert upper in engine.hold_major_lines(text, {"foo": locked}, allow_major=False)


def test_apply_failure_restores_already_written_files(tmp_path, fake_image, monkeypatch):
    originals = {}
    for name in ("Dockerfile.a", "Dockerfile.b"):
        path = tmp_path / name
        path.write_bytes(b"FROM python:3.14\n")
        originals[path] = path.read_bytes()
    original_write = Path.write_bytes
    failed = False

    def write(path, data):
        nonlocal failed
        if path == tmp_path / "Dockerfile.b" and not failed:
            failed = True
            raise OSError("simulated disk failure")
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", write)
    with pytest.raises(OSError, match="disk failure"):
        runner.run(tmp_path, Config(ecosystems=("docker",)))
    assert all(path.read_bytes() == contents for path, contents in originals.items())


def test_cooldown_reaches_project_and_inline_resolvers(tmp_path, monkeypatch):
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["foo>=1"]\n')
    script = tmp_path / "script.py"
    script.write_text('# /// script\n# dependencies = ["foo>=1"]\n# ///\n')
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        lock = Path(str(script) + ".lock") if "--script" in command else tmp_path / "uv.lock"
        lock.write_text('version = 1\n[[package]]\nname = "foo"\nversion = "1.2.0"\n')
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(engine, "run", fake_run)
    with use_context(RunContext(tmp_path, Config(min_age_hours=48))):
        engine.update_python(dry_run=True)
        engine.update_inline_script(script, dry_run=True)
    assert len(commands) == 2
    assert all("--exclude-newer" in command for command in commands)
    assert commands[0][-1] == commands[1][-1]
    assert not (tmp_path / "uv.lock").exists()
    assert not Path(str(script) + ".lock").exists()


def test_hooks_run_in_staged_git_repo_and_preserve_tracked_ignored_files(tmp_path, monkeypatch):
    import sys

    hook_cli = str(Path(sys.executable).with_name("pre-commit"))
    assert Path(hook_cli).is_file(), "Install development dependencies with uv sync"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("retained.txt\n")
    retained = tmp_path / "retained.txt"
    retained.write_text("tracked despite ignore\n")
    subprocess.run(["git", "add", "--force", str(retained)], cwd=tmp_path, check=True)
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path.parent / (tmp_path.name + "-hook-cache")))
    original_run = engine.run

    def local_tool(command, **kwargs):
        if command[:3] == ["uv", "tool", "run"]:
            command = [hook_cli, "autoupdate", "--freeze"]
        return original_run(command, **kwargs)

    monkeypatch.setattr(engine, "run", local_tool)
    report = runner.run(tmp_path, Config(ecosystems=("hooks",)))
    assert report.changed_files == []
    assert retained.read_text() == "tracked despite ignore\n"


def test_invalid_generated_yaml_is_a_cli_error(tmp_path, fake_image, monkeypatch, capsys):
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")

    def invalid(**kwargs):
        engine.context().pre_commit.write_text("repos: [\n")
        return []

    monkeypatch.setattr(engine, "update_hooks", invalid)
    assert main(["--directory", str(tmp_path), "--only", "hooks", "--format", "json"]) == 2
    assert "Invalid YAML" in json.loads(capsys.readouterr().out)["error"]
    assert (tmp_path / ".pre-commit-config.yaml").read_text() == "repos: []\n"
