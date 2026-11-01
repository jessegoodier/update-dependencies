# Automated releases

Once this workflow change is merged, open **Actions → Release → Run workflow** on
`main` (or the repository's current default branch). Choose a version increment:

| Choice | Example from 0.1.0 |
| --- | --- |
| patch (default) | 0.1.1 |
| minor | 0.2.0 |
| major | 1.0.0 |

The workflow uses the greater of the manifest version and existing stable `vX.Y.Z`
tags as its starting version. This avoids reusing a version if an earlier metadata
PR has not yet been merged. Prerelease tags are ignored; automated bumps require a
stable three-part project version.

## One-time permissions

1. Keep the existing PyPI Trusted Publisher for GitHub owner `jessegoodier`, repo
   `update-dependencies`, workflow `release.yml`, and environment `pypi`.
2. Keep the `pypi` environment's `v*` **tag** restriction and required reviewer.
3. In **Settings → Actions → General → Workflow permissions**, enable **Allow
   GitHub Actions to create and approve pull requests**. The workflow creates
   version PRs; it does not approve or merge them.
4. The workflow declares the narrow permissions each job needs. Preparation needs
   `contents: write`, `pull-requests: write`, and `actions: write`. Publishing alone
   receives `id-token: write`. GitHub release creation alone receives
   `contents: write`. Build/test jobs remain read-only.
5. If tag rulesets restrict creation of `v*` tags, permit the release automation's
   actor through the appropriate narrowly scoped rule/bypass, or creation will be
   rejected. Do not disable general main-branch protection for releases.

No new permanent token is required. Organization policies can impose stricter
restrictions; those must permit this repository's release automation.

## What happens

1. The preparation run verifies that it was started from the default branch.
2. `.github/scripts/bump_version.py` selects the next version and uses
   `uv version VERSION --no-sync` to update `pyproject.toml` and `uv.lock` together.
3. Lint, formatting, pytest, builds, strict Twine checks, and clean wheel/sdist
   installations must pass before a tag is created.
4. Automation commits the version bump on a unique `release/vX.Y.Z-RUN-ATTEMPT`
   branch and opens a PR to record it in the default branch. This respects the
   repository's rule against direct main pushes. The PR is not automatically merged.
5. It creates the annotated version tag on that tested commit and explicitly
   dispatches **Release** on the tag. You will see a second workflow run.
6. That tagged run verifies tag/version equality and rebuilds/tests the immutable
   tagged source. The `pypi` job then waits for the configured deployment approval.
7. After approval and successful PyPI publishing, automation creates the GitHub
   release with generated notes. Existing GitHub releases are left unchanged.

The tag must exist before publishing because the protected `pypi` environment
only accepts version tags. The GitHub release appears after PyPI succeeds. Version
bump PRs can require separate approval to start their CI when opened with
`GITHUB_TOKEN`; review and merge the metadata PR to keep `main` current. If multiple
version PRs are outstanding, keep the newest version and close stale ones.

Concurrency limits active runs for the same ref without canceling a running
release. Avoid submitting multiple bumps while one is pending: GitHub can replace
an older pending run in the same concurrency group. No tags or branches are
force-pushed. A failed validation creates neither a tag nor a release;
a later failure can leave a release branch/PR or an unpublished tag for recovery.

## Retrying and manual tags

If preparation failed before creating a tag, fix the cause and run it again.
If a tag already exists, retry its publishing run rather than requesting another
bump. From a machine authenticated with GitHub CLI:

```sh
gh workflow run release.yml --ref v0.1.1 --repo jessegoodier/update-dependencies
```

Dispatching on an existing tag skips bumping. The dropdown value is used only for
branch-based preparation. Existing user-pushed `v*` tags continue to trigger the
publishing path automatically. Tags must match `project.version` exactly.

A GitHub Actions `GITHUB_TOKEN` tag push does not trigger another push workflow.
The explicit workflow dispatch avoids that limitation and preserves the tag-based
OIDC environment identity. See [GitHub's trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

If publishing succeeds but GitHub release creation fails, rerun only the failed
GitHub release job; do not republish the same distributions. Investigate partial
uploads before retrying a failed publish. Published PyPI files cannot be replaced.

To preview version selection locally without changing files:

```sh
uv run python .github/scripts/bump_version.py --dry-run --bump patch
```

Omitting `--dry-run` updates the local manifest and lockfile only. The helper never
commits, pushes, tags, or publishes; those operations are owned by the workflow.
