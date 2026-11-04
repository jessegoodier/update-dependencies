# Automated releases

Start **Actions → Release → Run workflow** on the default branch. Choose `patch`
(default), `minor`, or `major`. The workflow bumps the version, validates it,
commits it directly to the default branch, tags that commit, and then waits for
approval in the protected `pypi` environment before publishing. The pypi approval
is the only manual gate.

## Release sequence

1. Preparation requires the default branch. The next version is based on the
   greater of `project.version` and existing stable `vX.Y.Z` tags; prerelease tags
   are ignored.
2. The version helper updates `pyproject.toml` and `uv.lock` together. Inconsistent
   versions or a failed update restore both files. Lint, formatting, tests,
   builds, strict Twine checks, and distribution installation checks must pass.
3. `release.py publish` checks that only the manifest and lockfile changed, commits
   them, and pushes to the default branch with the workflow token. If the default
   branch moved since checkout, the push fails and nothing is tagged.
4. The helper creates an annotated `vX.Y.Z` tag on that commit and pushes it. It
   refuses to reuse an existing tag.
5. The build job checks out the tag and verifies that it matches `project.version`,
   points at the checked-out commit, and is reachable from the default branch. It
   then reruns the checks and builds the distributions from that tagged commit.
6. Publishing waits for approval in the protected `pypi` environment. After approval,
   the job uploads through PyPI Trusted Publishing.
7. After PyPI publication succeeds, the workflow creates the GitHub release with
   generated notes. An existing GitHub release is left unchanged.

Preparation pushes directly to the default branch, so no pull request or CI pass
gates the version commit. The release job runs ruff, pytest, the build, and the
distribution checks on Ubuntu before pushing. The full CI matrix (macOS, workflow
linting, and the Action fixture) does not run automatically on the release commit,
because pushes made with the workflow token do not start other workflows.

## Permissions and setup

- Keep the PyPI Trusted Publisher for owner `jessegoodier`, repository
  `update-dependencies`, workflow `release.yml`, and environment `pypi`.
- Keep the `pypi` environment's `v*` tag restriction and required reviewer.
  Consider turning off **Allow administrators to bypass configured protection
  rules** on that environment, since the approval is the only gate.
- The repository has no rulesets or branch protection on `main`, so the workflow
  token can push the version commit. If you add a rule that requires pull requests
  or status checks, the push will fail. At that point, add a GitHub App to the
  rule's bypass list and use its token for the push.
- Keep **Allow GitHub Actions to create and approve pull requests** enabled. The
  dependency update workflow still opens pull requests.
- Preparation requests `contents: write`. Tagged builds request `contents: read`.
  Only publishing receives `id-token: write`, and only GitHub release creation
  receives its own `contents: write`. No permanent PyPI token is needed.
- If tag rulesets restrict `v*` creation, narrowly permit the release actor.

## Recovery and manual tags

If validation fails before the commit, nothing is pushed. Fix the cause and run
the release again.

If the push to the default branch fails, nothing is tagged. Run the release again.

If the version commit reached `main` but the tag push failed, do not run the release
again, because that would bump past the committed version. Tag the commit manually
from a current checkout of the default branch. A tag pushed by a person starts the
publishing path:

```sh
git tag -a v0.1.2 -m "Release v0.1.2"
git push origin v0.1.2
```

If the tag exists but approval was rejected, failed, or publishing did not run,
retry publishing on the same tag:

```sh
gh workflow run release.yml --ref v0.1.2 --repo jessegoodier/update-dependencies
```

The bump dropdown is ignored when dispatching on a tag. Rejecting approval leaves
the version commit and tag on the default branch, and the next release bumps past
that version.

If PyPI succeeds but GitHub release creation fails, rerun only the failed release
job. Do not republish the same distributions. Investigate partial uploads before
retrying a failed publish, because published PyPI files cannot be replaced.

Tags are never moved. The workflow does not force-push or delete commits or tags.

Preview version selection locally without modifying files:

```sh
uv run python .github/scripts/bump_version.py --dry-run --bump patch
```

Without `--dry-run`, this helper updates local metadata only.
