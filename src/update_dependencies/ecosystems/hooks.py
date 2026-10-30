"""Pre-commit updates run outside the target project's environment."""

from packaging.version import InvalidVersion, Version

from .. import engine as shared
from ..progress import emit


def revisions(text: str) -> dict[str, tuple[int, int, str, str]]:
    result = {}
    for start, end, repo in shared.precommit_repo_spans(text):
        match = shared.HOOK_REV_RE.search(text, start, end)
        if match:
            line_end = text.find("\n", match.end(), end)
            line_end = end if line_end < 0 else line_end
            result[repo] = (
                match.start(),
                line_end,
                text[match.start() : line_end],
                match["rev"],
            )
    return result


def version_label(line: str):
    match = shared.VERSION_IN_COMMENT_RE.search(line)
    if not match:
        return None
    try:
        return Version(match.group())
    except InvalidVersion:
        return None


def update_hooks(*, dry_run: bool = False) -> list[tuple[str, str, str]]:
    path = shared.context().pre_commit
    emit("hooks: checking pre-commit revisions (installing isolated tool if needed)")
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    old_revisions = revisions(original)
    protected = shared.protected_hook_revs(original)
    try:
        shared.run(
            [
                "uv",
                "tool",
                "run",
                "--python",
                "3.14",
                "--from",
                "pre-commit==4.7.0",
                "pre-commit",
                "autoupdate",
                "--freeze",
            ]
        )
        updated = path.read_text(encoding="utf-8")
        new_revisions = revisions(updated)
        changes = []
        for repo, (start, end, line, rev) in sorted(
            new_revisions.items(), key=lambda entry: entry[1][0], reverse=True
        ):
            if repo not in old_revisions:
                continue
            _, _, old_line, old_rev = old_revisions[repo]
            hold = repo in protected
            if not hold and not shared.context().config.allow_major:
                old_version, new_version = version_label(old_line), version_label(line)
                hold = old_version is None or new_version is None
                if not hold:
                    hold = old_version.major != new_version.major or (
                        old_version.major == 0 and old_version.minor != new_version.minor
                    )
                if hold and line != old_line:
                    shared.context().policy_skips.append(
                        {
                            "ecosystem": "hooks",
                            "name": shared.repo_label(repo),
                            "reason": "Major or unversioned hook update held",
                        }
                    )
            if hold:
                updated = updated[:start] + old_line + updated[end:]
            elif rev != old_rev:
                changes.append((shared.repo_label(repo), old_rev, rev))
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
    finally:
        if dry_run:
            path.write_bytes(original_bytes)
    for repo, rev in protected.items():
        shared.context().opt_out_skips.append((".pre-commit-config.yaml", shared.repo_label(repo), rev))
    return list(reversed(changes))
