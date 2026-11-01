"""Per-invocation output styling shared by progress, results, and diagnostics."""

from contextlib import contextmanager
from contextvars import ContextVar

from rich.console import Console

_plain: ContextVar[bool] = ContextVar("update_dependencies_plain", default=False)
_diagnostics: ContextVar[Console | None] = ContextVar("update_dependencies_diagnostics", default=None)


def make_console(*, stderr: bool = False, plain: bool = False) -> Console:
    if plain or _plain.get():
        # Disable terminal controls too, even when FORCE_COLOR is set by a runner.
        return Console(stderr=stderr, color_system=None, no_color=True, force_terminal=False)
    return Console(stderr=stderr)


def diagnostics() -> Console:
    console = _diagnostics.get()
    return console if console is not None else make_console(stderr=True)


@contextmanager
def output_style(plain: bool):
    token = _plain.set(plain)
    console_token = _diagnostics.set(make_console(stderr=True))
    try:
        yield
    finally:
        _diagnostics.reset(console_token)
        _plain.reset(token)
