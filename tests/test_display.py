"""Rich terminal output and explicit plain logs share the same report data."""

import io
import json

import pytest
from rich.console import Console

from update_dependencies import cli
from update_dependencies.action import arguments
from update_dependencies.cli import effective_config, main, parser
from update_dependencies.display import diagnostics, output_style
from update_dependencies.progress import emit
from update_dependencies.reporting import Report, print_report


def sample_report():
    return Report(
        changed_files=["pyproject.toml"],
        changes=[{"ecosystem": "python", "name": "example[extra]", "old": "1.0", "new": "1.1"}],
    )


def test_rich_terminal_report_has_colors_and_table():
    output = io.StringIO()
    print_report(
        sample_report(), console=Console(file=output, force_terminal=True, color_system="standard", width=110)
    )
    assert "\x1b[" in output.getvalue()
    assert "Ecosystem" in output.getvalue()
    assert "example[extra]" in output.getvalue()


def test_plain_report_disables_tables_and_colors():
    output = io.StringIO()
    print_report(sample_report(), plain=True, console=Console(file=output, force_terminal=True, width=110))
    assert "python: example[extra] 1.0 -> 1.1" in output.getvalue()
    assert "Ecosystem" not in output.getvalue()
    assert "\x1b" not in output.getvalue()


@pytest.mark.parametrize("format", ["text", "json"])
def test_cli_plain_covers_diagnostics_progress_and_results(tmp_path, monkeypatch, capsys, format):
    monkeypatch.setenv("FORCE_COLOR", "1")

    def fake_run(*args, **kwargs):
        diagnostics().print("[yellow]lookup warning[/]")
        emit("python: checking metadata")
        return sample_report()

    monkeypatch.setattr(cli, "run", fake_run)
    assert main(["--directory", str(tmp_path), "--plain", "--format", format]) == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err
    assert "lookup warning" in captured.err
    assert "python: checking metadata" in captured.err
    assert "[yellow]" not in captured.err
    if format == "json":
        assert json.loads(captured.out)["changed"] is True


def test_action_defaults_to_plain_but_cli_defaults_to_rich(tmp_path):
    _, config = effective_config(parser().parse_args(["--directory", str(tmp_path)]))
    assert config.plain is False
    _, config = effective_config(parser().parse_args(arguments({"UD_DIRECTORY": str(tmp_path)})))
    assert config.plain is True
    _, config = effective_config(
        parser().parse_args(arguments({"UD_DIRECTORY": str(tmp_path), "UD_PLAIN": "false"}))
    )
    assert config.plain is False


def test_plain_config_has_an_explicit_cli_override(tmp_path):
    (tmp_path / "update-dependencies.toml").write_text("plain = true\n")
    _, config = effective_config(parser().parse_args(["--directory", str(tmp_path), "--no-plain"]))
    assert config.plain is False


def test_output_style_is_reset_after_failure(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    with pytest.raises(ValueError, match="test"), output_style(True):
        assert diagnostics().color_system is None
        raise ValueError("test")
    # A subsequent invocation receives its own console; plain is not a global mutation.
    with output_style(False):
        assert diagnostics().color_system is not None


def test_nested_output_styles_restore_outer_invocation(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    with output_style(True):
        assert diagnostics().color_system is None
        with output_style(False):
            assert diagnostics().color_system is not None
        assert diagnostics().color_system is None


def test_rich_diagnostics_do_not_corrupt_json_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORCE_COLOR", "1")

    def fake_run(*args, **kwargs):
        diagnostics().print("[yellow]Registry warning[/]")
        return sample_report()

    monkeypatch.setattr(cli, "run", fake_run)
    assert main(["--directory", str(tmp_path), "--format", "json", "--progress", "off"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["changed_files"] == ["pyproject.toml"]
    assert "\x1b" not in captured.out
    assert "Registry warning" in captured.err
