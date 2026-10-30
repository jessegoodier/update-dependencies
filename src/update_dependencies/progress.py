"""Human-readable progress on stderr, with an optional live terminal indicator."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from rich.console import Console
from rich.text import Text

# Kept separate from repository state so discovery and final apply can report too.
_current: ContextVar[Progress | None] = ContextVar("update_dependencies_progress", default=None)


def emit(message: str) -> None:
    reporter = _current.get()
    if reporter is not None:
        reporter.emit(message)


@contextmanager
def phase(label: str):
    started = perf_counter()
    emit(f"{label}...")
    try:
        yield
    except BaseException:
        emit(f"{label}: failed after {perf_counter() - started:.1f}s")
        raise
    else:
        emit(f"{label}: done in {perf_counter() - started:.1f}s")


class Progress:
    def __init__(self, mode: str = "auto", *, console: Console | None = None):
        self.mode = mode
        self.console = console if console is not None else Console(stderr=True)
        self.started = 0.0
        self.status = None
        self.token = None

    def __enter__(self):
        self.started = perf_counter()
        self.token = _current.set(self)
        if self.mode == "auto" and self.console.is_terminal and not os.environ.get("CI"):
            self.status = self.console.status("Preparing dependency updates", spinner="dots")
            self.status.start()
        return self

    def __exit__(self, *exc_info):
        try:
            if self.status is not None:
                self.status.stop()
        finally:
            if self.token is not None:
                _current.reset(self.token)
        return False

    def emit(self, message: str):
        if self.mode == "off":
            return
        # Lazy import avoids a dependency cycle with process retry reporting.
        from .processes import redact

        text = Text(f"[{perf_counter() - self.started:6.1f}s] {redact(message)}")
        self.console.print(text, soft_wrap=True)
        if self.status is not None:
            self.status.update(Text(redact(message)))
