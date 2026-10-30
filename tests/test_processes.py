"""Bounded retries and secret-safe diagnostics for external tools."""

import subprocess

import pytest

from update_dependencies.processes import execute


@pytest.mark.parametrize(
    ("command", "expected_attempts"),
    [(["gh", "api", "repos/example/repo"], 3), (["uv", "lock", "--upgrade"], 1)],
)
def test_only_read_only_transient_operations_are_retried(tmp_path, monkeypatch, command, expected_attempts):
    attempts = []

    def fake_run(argv, **kwargs):
        assert kwargs["timeout"] == 7
        attempts.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 503")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("update_dependencies.processes.time.sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="503"):
        execute(command, cwd=tmp_path, timeout=7, retries=2)
    assert len(attempts) == expected_attempts


def test_failed_process_redacts_token_and_url_password(tmp_path, monkeypatch):
    credential = "test-token-value-12345"
    monkeypatch.setenv("GH_TOKEN", credential)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=f"{credential} https://username:password@example.com"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as caught:
        execute(["gh", "api", "repos/example/repo"], cwd=tmp_path)
    assert credential not in str(caught.value)
    assert "password" not in str(caught.value)
