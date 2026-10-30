"""State isolated to one invocation, including discovery and registry caches."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Config


@dataclass
class RunContext:
    root: Path
    config: Config
    files: list[Path] | None = None
    pyproject: Path = field(init=False)
    uv_lock: Path = field(init=False)
    pre_commit: Path = field(init=False)
    action_files: list[Path] | None = None
    dockerfiles: list[Path] | None = None
    inline_scripts: list[Path] | None = None
    opt_out_skips: list = field(default_factory=list)
    docker_latest_skips: list = field(default_factory=list)
    github_meta: dict = field(default_factory=dict)
    policy_skips: list[dict] = field(default_factory=list)
    uv_age_args: list[str] = field(init=False)

    def __post_init__(self):
        self.pyproject = self.root / "pyproject.toml"
        self.uv_lock = self.root / "uv.lock"
        self.pre_commit = self.root / ".pre-commit-config.yaml"
        cutoff = datetime.now(UTC) - timedelta(hours=self.config.min_age_hours)
        self.uv_age_args = ["--exclude-newer", cutoff.isoformat()] if self.config.min_age_hours else []


_current: ContextVar[RunContext] = ContextVar("update_dependencies_context")


def context() -> RunContext:
    return _current.get()


@contextmanager
def use_context(value: RunContext):
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)
