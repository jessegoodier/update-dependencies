"""Progress remains useful in logs without corrupting machine-readable results."""

import io
import json

import pytest
from rich.console import Console

from update_dependencies import engine
from update_dependencies.cli import main
from update_dependencies.progress import Progress, emit, phase


def test_cli_progress_uses_stderr_and_keeps_json_clean(tmp_path, capsys):
    assert main(["--directory", str(tmp_path), "--dry-run", "--format", "json", "--progress", "plain"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["changed"] is False
    for message in (
        "Discovering repository files",
        "Discovered 0 file(s)",
        "Preparing isolated workspace",
        "Validating staged changes",
        "Preview ready",
    ):
        assert message in captured.err
    assert "done in" in captured.err
    assert "\x1b" not in captured.err


def test_progress_can_be_disabled_in_config_and_overridden(tmp_path, capsys):
    (tmp_path / "update-dependencies.toml").write_text('progress = "off"\n')
    assert main(["--directory", str(tmp_path), "--format", "json"]) == 0
    assert capsys.readouterr().err == ""
    assert main(["--directory", str(tmp_path), "--format", "json", "--progress", "plain"]) == 0
    assert "Discovered 1 file(s)" in capsys.readouterr().err


def test_phase_failure_is_reported_and_context_is_reset():
    output = io.StringIO()
    with (
        pytest.raises(RuntimeError, match="upstream failed"),
        Progress("plain", console=Console(file=output)),
        phase("Updating python"),
    ):
        raise RuntimeError("upstream failed")
    assert "Updating python: failed after" in output.getvalue()
    before = output.getvalue()
    emit("This operation is outside the reporter")
    assert output.getvalue() == before


def test_progress_redacts_secrets_and_does_not_interpret_markup(monkeypatch):
    output = io.StringIO()
    monkeypatch.setenv("GH_TOKEN", "example-secret-token-value")
    with Progress("plain", console=Console(file=output)):
        emit("Checking [test].yaml with example-secret-token-value")
    assert "[test].yaml" in output.getvalue()
    assert "example-secret-token-value" not in output.getvalue()
    assert "[redacted]" in output.getvalue()


def test_ecosystem_progress_names_current_file_and_preserves_failure(tmp_path, monkeypatch, capsys):
    (tmp_path / "Dockerfile").write_text("FROM python:3.14\n")
    monkeypatch.setattr(engine.shutil, "which", lambda tool: f"/tools/{tool}")
    monkeypatch.setattr(engine, "image_freshness", lambda image: None)

    def fail(image, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(engine, "image_digest", fail)
    assert (
        main(["--directory", str(tmp_path), "--only", "docker", "--format", "json", "--progress", "plain"])
        == 2
    )
    captured = capsys.readouterr()
    assert "docker: checking Dockerfile" in captured.err
    assert "Updating docker: failed after" in captured.err
    assert "registry unavailable" in json.loads(captured.out)["error"]
    assert (tmp_path / "Dockerfile").read_text() == "FROM python:3.14\n"


def test_auto_mode_has_no_live_indicator_in_non_terminal():
    output = io.StringIO()
    with Progress("auto", console=Console(file=output, force_terminal=False)) as reporter:
        assert reporter.status is None
        emit("Checking dependencies")
    assert "Checking dependencies" in output.getvalue()
    assert "\x1b" not in output.getvalue()
