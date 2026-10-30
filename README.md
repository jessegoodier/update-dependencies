# update-dependencies

Update Python dependencies and PEP 723 scripts, pin GitHub Actions to commit SHAs,
refresh Docker image digests, and freeze pre-commit hook revisions.

Requires **Python 3.14** (`>=3.14,<3.15`). Development and GitHub Actions also use
3.14. Managed repositories may target other Python versions; resolution follows
their own metadata.

```sh
uv tool install update-dependencies
update-dependencies --directory /path/to/repository --dry-run
update-dependencies --ecosystems python,actions --no-allow-major
```

Before the first PyPI release, install from this checkout with `uv tool install .`.
For interactive prompts, install `uv tool install 'update-dependencies[tui]'` and
run `update-dependencies --tui`.

All ecosystems are selected by default. Major updates remain enabled for
compatibility; set `allow-major = false` for a conservative policy. `NO_AUTO_UPDATE`
in a same-line or immediately preceding comment preserves individual exclusions.
Docker `latest` tags are left alone unless explicitly enabled.

Progress on stderr shows the current ecosystem, files/lookups, and elapsed times.
Use `--progress plain` for logs or `--progress off` for quiet runs; JSON output
stays on stdout.

Updates are prepared in a temporary copy before applying changes. Dry runs and
checks leave target files untouched. Environment installation is opt-in (`--sync`).
Python updates currently support uv projects/lockfiles and PEP 723 scripts;
requirements.txt, Poetry, and Pipenv lockfiles are not supported.

## GitHub Action

```yaml
permissions:
  contents: read

steps:
  - uses: actions/checkout@v7
  - uses: jessegoodier/update-dependencies@v0.1.0
    with:
      config: update-dependencies.toml
      dry-run: 'true'
    env:
      GH_TOKEN: ${{ github.token }}
```

The release reference above becomes usable after the first release is pushed.
Pin actions to full commit SHAs in production. The Action supports Linux hosted
runners initially; it installs its own isolated Python tool environment and uses
the package from the Action revision. It does not commit or open pull requests.
See [Action usage](docs/github-action.md) and [configuration](docs/configuration.md).

## Development and releases

Ruff formats Python code with a line length of 110 characters.

```sh
uv sync --locked --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

`scripts/update_dependencies.py` is a compatibility launcher for an installed
checkout. Run it with `uv run python scripts/update_dependencies.py --help`.
The old root filename was moved to avoid shadowing the installed Python package.

Release by updating `project.version`, regenerating `uv.lock`, and pushing a
matching `vX.Y.Z` tag. The release workflow builds, checks, installs, and smoke-tests
both distribution formats before publishing through PyPI Trusted Publishing.
Configure the PyPI trusted publisher for this repository, `release.yml`, and the
`pypi` GitHub environment before publishing. No stored PyPI API token is needed.
PyPI project-name availability/ownership must be confirmed by the publisher.
