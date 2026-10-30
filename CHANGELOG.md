# Changelog

## 0.1.0

- Package the updater with uv_build and a Python 3.14 CLI.
- Share validated TOML configuration between the CLI and composite GitHub Action.
- Stage updates before applying; add check mode, JSON reports, and file filters.
- Separate ecosystem operations and isolate per-run state.
- Make environment synchronization and build-backend updating opt-in.
- Add CI, clean artifact installation checks, and PyPI Trusted Publishing.
- Report stage and per-file progress on stderr with elapsed times and configurable verbosity.
- Format Python code with a 110-character Ruff line length.
