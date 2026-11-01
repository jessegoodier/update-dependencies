# GitHub Action

The root `action.yml` is a composite action for Linux runners. It installs uv
0.13.6 and Python 3.14, installs this Action revision's package and locked runtime
dependencies into a unique isolated environment, reads configuration, optionally
sets up Docker Buildx, then runs the same CLI shipped on PyPI.

GitHub-hosted Ubuntu includes GitHub CLI and Docker. Self-hosted Linux runners
must supply Git, GitHub CLI for Actions updates, and Docker for image updates.
Pre-commit is run through uv's separate tool environment; it does not need to be
installed in the target project. Registry access is needed to provision tools and
resolve updates. Docker authentication uses the caller's existing Docker config;
public ECR lookups use anonymous/public token access.

```yaml
name: Dependency updates
on:
  workflow_dispatch:
  schedule:
    - cron: '23 9 * * 1'
permissions:
  contents: read
concurrency:
  group: dependency-updates
  cancel-in-progress: false
jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: jessegoodier/update-dependencies@v0.1.0
        id: update
        with:
          config: update-dependencies.toml
          allow-major: 'false'
          dry-run: 'true'
          progress: plain
          plain: 'true'
        env:
          GH_TOKEN: ${{ github.token }}
```

The first versioned reference becomes available after release. Prefer a full
release commit SHA for production. The repository's own scheduled workflow uses
its checked-out action and uploads a preview report. GitHub Actions publishing
and PyPI publishing are separate: this action can run directly from a repository
commit even before the first PyPI release.

The Action defaults to `plain: 'true'` for colorless logs without tables or live
indicators. Set `plain: 'false'` to use normal terminal detection instead.

Inputs mirror [configuration CLI options](configuration.md). Every boolean accepts
`'true'` or `'false'`; empty means inherit. The `plain` input is the exception
to the empty default: it defaults to `'true'`. `ecosystems` is comma-separated;
`include` and `exclude` are newline-separated globs. Runtime-only inputs are
`directory`, `config`, `no-config`, `format`, and `output`. Interactive prompting,
printing config, and legacy no-op aliases are intentionally absent from the Action.
Secrets belong in environment variables (`GH_TOKEN`, registry credentials via a
login step), not committed TOML or argument strings. The Action passes input
values through environment variables and constructs Python argument lists.

Outputs:

| Output | Meaning |
| --- | --- |
| `changed` | `true` if files changed or would change in a preview |
| `change-count` | Changed file count |
| `changed-files` | JSON array of repository-relative paths |
| `report-path` | Absolute JSON report path |

A default report is written to `$RUNNER_TEMP/update-dependencies-report.json`;
set `output` for distinct reports when running multiple instances in one job.
A step summary is written automatically. The `progress` input accepts `auto`,
`plain`, or `off`, and defaults to inheriting repository configuration. CI progress
is written as plain stderr lines with elapsed times; JSON stdout remains separate.
Check mode returns a failing step when
updates exist; use `continue-on-error` when the caller intends to inspect that
outcome and continue.

The action edits the checkout when dry-run/check are disabled. It does not commit,
push, open PRs, or merge changes. A caller can use the report and changed files in
its own PR workflow, granting write permissions only there. A PR bot token may be
needed if the resulting PR must trigger other workflows; workflow-file changes
may require additional token permissions. Run write-enabled updating only on
trusted branches and never execute untrusted PR code with privileged credentials.

## Release setup

Create the `pypi` GitHub environment, and configure PyPI Trusted Publishing with:

- PyPI project: `update-dependencies`
- GitHub owner: `jessegoodier`
- Repository: `update-dependencies`
- Workflow: `release.yml`
- Environment: `pypi`

Use **Actions → Release → Run workflow**, select the default branch, and choose
`patch` (default), `minor`, or `major`. The workflow updates the version and lockfile,
validates them, creates a release branch and metadata PR, then tags the validated
commit. It explicitly dispatches publishing on that tag, retaining the protected
`pypi` environment. The GitHub release is created after publishing succeeds.

See [release automation](releasing.md) for repository permissions, version PRs,
manual tags, and retry instructions. The existing PyPI trusted publisher continues
to use `release.yml` and environment `pypi`; no additional PyPI token is needed.

Current official references verified during the 2026 migration:
[uv build backend](https://docs.astral.sh/uv/concepts/build-backend/),
[GitHub Action metadata](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
