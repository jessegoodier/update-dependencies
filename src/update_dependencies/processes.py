"""Bounded external processes with safe diagnostics."""

import os
import re
import subprocess
import time
from pathlib import Path

from .progress import emit


def redact(text: str) -> str:
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    for key, value in os.environ.items():
        if len(value) >= 8 and any(
            word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
        ):
            text = text.replace(value, "[redacted]")
    return text


def execute(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    retries: int = 2,
    allow_not_found: bool = False,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    # Retry read-only lookups only; never repeat a mutating operation.
    read_only = command[:2] == ["gh", "api"] or command[:3] == [
        "docker",
        "buildx",
        "imagetools",
    ]
    attempts = retries + 1 if read_only else 1
    for attempt in range(attempts):
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        transient = any(
            s in result.stderr.lower() for s in ("429", "502", "503", "504", "timed out", "connection reset")
        )
        if not result.returncode or not transient or attempt == attempts - 1:
            break
        emit(f"Retrying lookup ({attempt + 2}/{attempts})")
        time.sleep(min(2**attempt, 8))
    if result.returncode and not allow_failure and not (allow_not_found and "(HTTP 404)" in result.stderr):
        raise RuntimeError(redact(f"{command[0]} failed ({result.returncode}): {result.stderr.strip()}"))
    return result


def open_url(request, *, timeout: int, retries: int):
    """Retry only transient failures of read-only HTTP requests."""
    import urllib.error
    import urllib.request

    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == retries:
                raise
        except TimeoutError, urllib.error.URLError:
            if attempt == retries:
                raise
        emit(f"Retrying HTTP lookup ({attempt + 2}/{retries + 1})")
        time.sleep(min(2**attempt, 8))
    raise RuntimeError("HTTP lookup exhausted its retry budget")
