# Automated releases

Start **Actions → Release → Run workflow** on the default branch. Choose `patch`
(default), `minor`, or `major`. This prepares a version PR; it does not create a
tag, publish a package, or create a GitHub release.

## Release sequence

1. Preparation refuses to start while another `release/v*` PR is open. Resolve
   that PR first. The next version is based on the greater of `project.version`
   and existing stable `vX.Y.Z` tags; prerelease tags are ignored.
2. The version helper updates `pyproject.toml` and `uv.lock` together. Inconsistent
   versions or a failed update restore both files. Lint, formatting, tests,
   builds, strict Twine checks, and distribution installation checks must pass.
3. Automation commits only the manifest and lockfile on a unique release branch,
   opens a PR, and explicitly dispatches full CI for that branch. Review and
   merge the version PR. Closing it without merging cancels the proposed release.
4. **After merge**, CI runs on the default branch. Only successful push-triggered
   CI on the exact merged commit can start tagging. Failed, cancelled, manual,
   fork, and PR CI runs cannot start a release.
5. The release helper verifies default-branch ancestry and matching manifest and
   lockfile versions, then creates an annotated `vX.Y.Z` tag on the tested merged
   commit. It never moves an existing tag. Already-tagged versions are a no-op.
6. Publishing is explicitly dispatched on that tag. The tagged build checks the
   version, tag target, default-branch ancestry, and successful default-branch CI
   again before rebuilding and testing the immutable source.
7. The protected `pypi` environment requires deployment approval. After approval
   and successful PyPI publication, automation creates the GitHub release with
   generated notes. Existing GitHub releases are left unchanged.

The tag is created only after merge and successful CI. The GitHub release is
created only after publication. An unrelated commit with an already-released
version does not start another release. If a version merge fails CI, a subsequent
fix on the default branch can release that same version after its own CI passes.

## Permissions and setup

- Keep the PyPI Trusted Publisher for owner `jessegoodier`, repository
  `update-dependencies`, workflow `release.yml`, and environment `pypi`.
- Keep the `pypi` environment's `v*` tag restriction and required reviewer.
- Keep **Allow GitHub Actions to create and approve pull requests** enabled.
  Automation creates version PRs; it does not approve or merge them.
- Preparation requests contents/PR/actions write access. Tagging requests contents
  and actions write access. Tagged builds request contents/actions read access.
  Only publishing receives `id-token: write`; only GitHub release creation
  receives its own contents write permission. No permanent PyPI token is needed.
- If tag rulesets restrict `v*` creation, narrowly permit the release actor. Keep
  default-branch protections in place.

Token-created PRs can require approval for their PR-event CI. Explicitly dispatched
CI runs separately, but required PR checks may still need **Approve workflows to
run** before the PR can merge. Preparation and tagging share default-branch
concurrency without cancelling active runs. Finish one release before starting
another; GitHub may replace an older pending run in the same concurrency group.

## Recovery and manual tags

If validation fails before the version PR is opened, fix the cause and prepare
again. If the PR exists, fix or close it rather than preparing a duplicate. If
CI on the merged commit fails, fix the code on the default branch; no tag is
created until CI passes.

If tagging fails before the tag is pushed, rerun the failed tag job. If the tag
exists but dispatching or publishing failed, retry publishing on the same tag:

```sh
gh workflow run release.yml --ref v0.1.1 --repo jessegoodier/update-dependencies
```

The bump dropdown is ignored when dispatching on a tag. Automatic CI reruns do
not redispatch an existing tag. A manually pushed tag also takes the publishing
path and must identify a merged default-branch commit with matching metadata and
successful push CI. A tag for an unmerged version branch is rejected.

For a repository without a stable release tag, automatic tagging requires a
version increase on the tested commit; an unchanged existing version is not
published merely by installing this workflow. This repository already has its
`v0.1.0` baseline.

If PyPI succeeds but GitHub release creation fails, rerun only the failed release
job. Do not republish the same distributions. Investigate partial uploads before
retrying a failed publish: published PyPI files cannot be replaced. A failure
between steps can leave a PR, branch, or unpublished tag for recovery; automation
does not force-push or delete them.

Preview version selection locally without modifying files:

```sh
uv run python .github/scripts/bump_version.py --dry-run --bump patch
```

Without `--dry-run`, this helper updates local metadata only. Committing, pushing,
tagging, and publishing are separate operations in the workflow.

Trigger behavior and `workflow_run` event handling were checked against the
[current GitHub documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
and [event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
