# Configuration

Precedence is built-in defaults, repository configuration, then explicit CLI or
Action inputs. Unset boolean Action inputs inherit config; `false` overrides it.
List overrides replace the configured list. Unknown keys and invalid values fail
before any updating begins.

Discovery checks `update-dependencies.toml` first, then
`[tool.update-dependencies]` in `pyproject.toml`. Exactly one file is loaded.
`--config PATH` selects a file explicitly, relative to `--directory`.
`--no-config` disables discovery. `--print-config` prints effective JSON without
network calls. `--directory` defaults to the caller's current working directory.

```toml
# update-dependencies.toml
schema-version = 1
ecosystems = ["python", "actions", "docker", "hooks"]
allow-major = false
min-age-hours = 48
stale-days = 180
dry-run = false
check = false
timeout = 120
retries = 2
progress = "auto"
include = []
exclude = ["vendor/**", "tests/fixtures/**"]

[python]
inline-scripts = true
add-missing-minimums = false
update-build-backend = false
sync = false

[docker]
pin-latest = false
```

For `pyproject.toml`, prefix all tables with `tool.update-dependencies`:

```toml
[tool.update-dependencies]
allow-major = false

[tool.update-dependencies.python]
inline-scripts = true
```

| Configuration key | CLI | Default |
| --- | --- | --- |
| `schema-version` | Config only | `1` |
| `ecosystems` | `--ecosystems python,actions` or `--only python` | All four |
| `allow-major` | `--allow-major` / `--no-allow-major` | `true` |
| `min-age-hours` | `--min-age HOURS` | `48` |
| `stale-days` | `--stale-days DAYS` | `180` |
| `dry-run` | `--dry-run` / `--no-dry-run` | `false` |
| `check` | `--check` / `--no-check` | `false` |
| `timeout` | `--timeout SECONDS` | `120` |
| `retries` | `--retries COUNT` | `2` |
| `progress` | `--progress auto\|plain\|off` | `auto` |
| `include` | Repeatable `--include GLOB` | All discovered files |
| `exclude` | Repeatable `--exclude GLOB` | Empty |
| `python.inline-scripts` | `--python-inline-scripts` / `--no-python-inline-scripts` | `true` |
| `python.add-missing-minimums` | `--python-add-minimums` / `--no-python-add-minimums` | `false` |
| `python.update-build-backend` | `--python-update-build-backend` / `--no-python-update-build-backend` | `false` |
| `python.sync` | `--sync` / `--no-sync` | `false` |
| `docker.pin-latest` | `--docker-pin-latest` / `--no-docker-pin-latest` | `false` |

Progress is written to stderr, including elapsed time, ecosystem start/completion,
current files or dependency lookups, retries, validation, and application. `auto`
adds a spinner in an interactive terminal and emits plain lines in CI or redirected
logs. `plain` always emits plain lines; `off` disables progress messages. Errors and
warnings still appear. Long commands remain bounded by the configured timeout;
progress does not stream subprocess stdout/stderr or change update behavior.
Use ecosystem completion timings to identify which updates take the most time.

`--format text|json` selects console output. `--output PATH` also writes a JSON
report. `--version` prints the installed package version. `--tui` enables the
optional interactive interface and seeds its choices from the effective config.

Legacy aliases remain: `--no-major-version-updates`,
`--enable-docker-latest-to-sha-updates`, `--add-python-latest-version`, and
`--no-update-python-inline-script-metadata`. `--update-docker-tags` is deprecated
and emits a warning; Docker digest updates have always been the default.

## Policy and scope

Python updates cover a root uv project and PEP 723 scripts anywhere under the
target directory. Requirements files, Poetry, and Pipenv are not supported.
GitHub updates cover `.github/**/*.yml`, `.github/**/*.yaml`, and all root/nested
`action.yml` and `action.yaml` files. Docker updates refresh digests for existing
tags, without choosing newer image tags. Hooks use `pre-commit autoupdate --freeze`.

`allow-major = false` holds Python direct dependency major lines using the current
lock (or a requirement's version floor when no lock exists). For `0.x` dependencies,
the minor line is held. Bare requirements without a lock cannot be safely held and
produce an error. Transitive dependencies remain subject to the resolver. Actions
hold major upgrades and can pin their current ref. Hooks obey the major policy
when both old and new revisions have version labels; revisions that cannot be
compared are held when major updates are disabled. Docker digests retain tags.

Cooldowns apply to GitHub Action releases, uv Python resolution, PEP 723 resolution,
and explicit uv_build updates. Docker digests and pre-commit revisions do not have
a universal release-time source, so `min-age` does not apply to those ecosystems.
Maintenance age is informational, not a judgment that a project is unmaintained.

The build-backend flag independently raises an existing `uv_build` floor within
its existing bounds. It does not change Docker uv image tags. Adding missing
Python minimums only affects bare dependency requirements.

Git inventory respects tracked files and untracked files not ignored by Git.
Outside Git, common environment/build directories are excluded. Symlinks are
skipped. Globs match repository-relative paths using shell-style matching;
`*` also matches `/`, and leading `**/` can match root files. Filters control
manifest discovery; paired lockfiles are updated together with their manifests.
The complete nonignored inventory is copied for resolution so local paths inside
the repository remain available. Hook updates initialize a fresh staging Git
repository; original Git history, tags, and worktree metadata are not copied. Paths outside the target directory and external
Git worktrees are not mirrored; choose the enclosing repository as `--directory`.
Ignored local dependencies must be made available as nonignored files first.

## Results and failures

Exit codes: `0` success (including no applicable files), `1` updates available in
check mode, `2` configuration/tool/update failure, `130` interruption.
Explicit `--only` or `--ecosystems` selections fail if an ecosystem has no applicable
files; automatic defaults report those ecosystems as skipped.

JSON reports have `schema_version`, `changed`, `change_count`, `changed_files`,
`changes`, `skipped`, and `dry_run`. `change_count` counts changed **files**; change
rows may also contain informational or held updates. Optional maintenance lookups
can be unavailable without blocking updates. Required resolution errors abort the
run; no staged changes are applied. Process diagnostics redact credential-like
environment values and URL passwords.

Updates resolve in a temporary copy, validate modified TOML/YAML, and check for
concurrent target edits before applying. Apply failures attempt to restore written
files. This is not a crash-safe filesystem transaction: forced termination or a
machine crash during final writes can leave a partial apply. Existing dirty files
are treated as the starting point and preserved unless the updater intentionally
changes them. Text updates retain original CRLF conventions.

`--sync` installs the real target environment after successful application. It can
execute target build machinery; if sync fails, manifest updates remain applied.
Neither dry-run nor check performs sync. Dependency resolution itself can invoke
build backends to obtain package metadata; use trusted repository inputs. Registry and subprocess timeouts apply
per request/command, not to the entire run. Retries apply only to read-only,
transient lookups; mutating commands are never retried.
