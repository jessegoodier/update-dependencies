"""Hook pinning preserves opt-outs and conservative major policy."""

import subprocess

import pytest

from update_dependencies import engine
from update_dependencies.config import Config
from update_dependencies.context import RunContext, use_context


@pytest.mark.parametrize(
    ("old", "new", "held"),
    [
        ("v1.0.0", "v2.0.0", True),
        ("v1.0.0", "v1.2.0", False),
        ("v0.2.0", "v0.3.0", True),
        ("main", "v1.0.0", True),
    ],
)
def test_hooks_hold_major_and_unknown_revisions(tmp_path, monkeypatch, old, new, held):
    path = tmp_path / ".pre-commit-config.yaml"
    original = f"repos:\n- repo: https://example.com/hooks\n  rev: {old}\n  hooks: []\n"
    path.write_text(original)
    sha = "a" * 40

    def fake_run(command, **kwargs):
        assert command[:3] == ["uv", "tool", "run"]
        path.write_text(original.replace(f"rev: {old}", f"rev: {sha}  # frozen: {new}"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(engine, "run", fake_run)
    with use_context(RunContext(tmp_path, Config(allow_major=False))):
        changes = engine.update_hooks()
    assert (path.read_text() == original) is held
    assert bool(changes) is not held


def test_repo_line_opt_out_preserves_revision_comment(tmp_path, monkeypatch):
    path = tmp_path / ".pre-commit-config.yaml"
    original = "repos:\n- repo: https://example.com/hooks  # NO_AUTO_UPDATE\n  rev: v1.0.0  # keep this comment\n  hooks: []\n"
    path.write_text(original)

    def fake_run(command, **kwargs):
        path.write_text(original.replace("v1.0.0  # keep this comment", "a" * 40 + "  # frozen: v2.0.0"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(engine, "run", fake_run)
    with use_context(RunContext(tmp_path, Config())):
        assert engine.update_hooks() == []
    assert path.read_text() == original
