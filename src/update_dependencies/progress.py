"""Human-readable progress on stderr, with an optional live terminal indicator."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from rich.console import Console
from rich.text import Text

from .display import make_console

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
        self.console = console if console is not None else make_console(stderr=True, plain=mode == "plain")
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

        message = redact(message)
        text = Text()
        text.append(
            f"[{perf_counter() - self.started:6.1f}s] ", style="dim" if self.mode != "plain" else None
        )
        style = None
        if self.mode != "plain":
            style = (
                "red"
                if "failed" in message
                else "green"
                if "done in" in message
                else "yellow"
                if "skipped" in message
                else "cyan"
            )
        text.append(message, style=style)
        self.console.print(text, soft_wrap=True)
        if self.status is not None:
            self.status.update(Text(redact(message)))
