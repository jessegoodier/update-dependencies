#!/usr/bin/env python3
"""Compatibility launcher. Prefer the installed update-dependencies command."""

from update_dependencies.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
