"""Configuration precedence, validation, and Action/CLI contract."""

import json
from pathlib import Path

import pytest
import yaml

from update_dependencies.action import (
    BOOLEAN_INPUTS,
    LIST_INPUTS,
    VALUE_INPUTS,
    arguments,
)
from update_dependencies.cli import effective_config, main, parser
from update_dependencies.config import Config, load_config


def test_config_discovery_and_explicit_false(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.update-dependencies]\nallow-major = true\n")
    (tmp_path / "update-dependencies.toml").write_text(
        'allow-major = false\necosystems = ["docker"]\n[python]\ninline-scripts = false\n'
    )
    _, config = effective_config(parser().parse_args(["--directory", str(tmp_path)]))
    assert config.allow_major is False
    assert config.python_inline_scripts is False
    _, config = effective_config(
        parser().parse_args(["--directory", str(tmp_path), "--allow-major", "--python-inline-scripts"])
    )
    assert config.allow_major is True
    assert config.python_inline_scripts is True
    assert load_config(tmp_path, disabled=True) == Config()
    assert load_config(tmp_path, Path("pyproject.toml")).allow_major is True


@pytest.mark.parametrize(
    "text",
    [
        "typo = true",
        'allow-major = "false"',
        "min-age-hours = -1",
        "timeout = 0",
        "schema-version = 2",
        'ecosystems = ["node"]',
        'exclude = "vendor"',
        'progress = "verbose"',
        "[python]\nunknown = true",
    ],
)
def test_invalid_configuration_rejected(tmp_path, text):
    (tmp_path / "update-dependencies.toml").write_text(text)
    with pytest.raises((ValueError, TypeError)):
        load_config(tmp_path)


def test_action_false_overrides_config_and_keeps_values_literal(tmp_path):
    (tmp_path / "update-dependencies.toml").write_text("allow-major = true\n")
    argv = arguments(
        {
            "UD_DIRECTORY": str(tmp_path),
            "UD_ALLOW_MAJOR": "false",
            "UD_EXCLUDE": "vendor/**\n$(touch never-run)/**",
        }
    )
    _, config = effective_config(parser().parse_args(argv))
    assert config.allow_major is False
    assert config.exclude == ("vendor/**", "$(touch never-run)/**")
    with pytest.raises(ValueError, match="true or false"):
        arguments({"UD_DRY_RUN": "yes"})


def test_action_inputs_match_adapter_and_config_fields():
    root = Path(__file__).resolve().parents[1]
    metadata = yaml.safe_load((root / "action.yml").read_text())
    assert set(metadata["inputs"]) == set(BOOLEAN_INPUTS + VALUE_INPUTS + LIST_INPUTS)
    destinations = {a.dest for a in parser()._actions}
    assert set(Config.__dataclass_fields__) - {"schema_version", "python_sync"} <= destinations
    for name, definition in metadata["inputs"].items():
        if name in BOOLEAN_INPUTS:
            assert definition["default"] == ""


def test_print_config_has_no_registry_side_effects(tmp_path, capsys):
    assert main(["--directory", str(tmp_path), "--no-allow-major", "--print-config"]) == 0
    assert json.loads(capsys.readouterr().out)["allow_major"] is False


def test_no_applicable_files_is_success(tmp_path, capsys):
    assert main(["--directory", str(tmp_path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] is False
    assert len(report["skipped"]) == 4
    assert main(["--directory", str(tmp_path), "--only", "actions"]) == 2


def test_report_output_cannot_overwrite_a_manifest(tmp_path):
    path = tmp_path / "pyproject.toml"
    original = '[project]\nname = "example"\nversion = "0.1.0"\n'
    path.write_text(original)
    assert main(["--directory", str(tmp_path), "--output", str(path)]) == 2
    assert path.read_text() == original


def test_action_preparation_only_sets_up_docker_when_applicable(tmp_path, monkeypatch):
    import sys

    from update_dependencies.action import run

    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("UD_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("UD_ECOSYSTEMS", "docker")
    monkeypatch.setattr(sys, "argv", ["action", "--prepare"])
    assert run() == 0
    assert output.read_text() == "docker=false\n"
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    assert run() == 0
    assert output.read_text().endswith("docker=true\n")
