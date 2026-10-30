"""Actions dependency update operations."""

import json
import re

from packaging.version import InvalidVersion, Version

from .. import engine as shared
from ..progress import emit


def update_actions(
    *,
    dry_run: bool = False,
    allow_major: bool = True,
    min_age_hours: int = shared.DEFAULT_MIN_AGE_HOURS,
) -> list[tuple[str, str, str, str, str]]:
    """Return table rows (kind, action, old, new, note) for each pin that moved."""
    rows: list[tuple[str, str, str, str, str]] = []
    noted: set[tuple[str, str, str, str]] = set()
    # Tag only until a write needs the SHA — held majors / already-current pins
    # should not pay for a commits API call.
    latest_tags: dict[str, str | None] = {}
    commits: dict[tuple[str, str], str | None] = {}
    cooldown_repos: set[str] = set()

    def commit_sha(repo: str, ref: str) -> str | None:
        if (repo, ref) not in commits:
            emit(f"actions: resolving {repo}@{ref}")
            result = shared.run(
                ["gh", "api", f"repos/{repo}/commits/{ref}"],
                allow_failure=True,
            )
            if result.returncode != 0:
                if any(code in result.stderr for code in ("401", "403", "429", "500", "502", "503", "504")):
                    raise RuntimeError(
                        f"GitHub commit lookup failed for {repo}@{ref}: {result.stderr.strip()}"
                    )
                shared.context().policy_skips.append(
                    {
                        "ecosystem": "actions",
                        "name": repo,
                        "ref": ref,
                        "reason": "Commit lookup unavailable",
                    }
                )
                shared.console.print(f"[yellow]actions: no commit for {repo}@{ref}, skipped[/]")
                commits[(repo, ref)] = None
            else:
                payload = json.loads(result.stdout)
                sha = payload.get("sha") if isinstance(payload, dict) else None
                if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
                    raise ValueError(f"Invalid GitHub commit SHA for {repo}@{ref}")
                commits[(repo, ref)] = sha
        return commits[(repo, ref)]

    def prefer_full_semver(repo: str, tag: str) -> str:
        """Replace floating majors (v3) with a same-commit full tag (v3.0.0) when one exists.

        Action repos often publish both a moving major release and a full semver tag
        on the same commit. /releases/latest can return the major (newer published_at),
        but pins should label the concrete X.Y.Z when available.
        """
        if shared.is_full_semver(tag):
            return tag
        sha = commit_sha(repo, tag)
        if not sha:
            return tag
        result = shared.run(
            ["gh", "api", f"repos/{repo}/git/matching-refs/tags/{tag}"],
            allow_not_found=True,
        )
        if result.returncode != 0:
            return tag
        best: str | None = None
        best_version: Version | None = None
        prefix = f"{tag}."
        for ref in json.loads(result.stdout):
            name = ref["ref"].removeprefix("refs/tags/")
            if not name.startswith(prefix) or not shared.is_full_semver(name):
                continue
            if commit_sha(repo, name) != sha:
                continue
            version = Version(name)
            if best_version is None or version > best_version:
                best, best_version = name, version
        return best or tag

    def note_cooldown(repo: str, latest: str, used: str | None, hours: float | None) -> None:
        if repo in cooldown_repos:
            return
        cooldown_repos.add(repo)
        age = f"{hours:.0f}h old" if hours is not None else "age unknown"
        if used:
            note = f"{age}; --min-age {min_age_hours}h"
            rows.append(("cooldown", repo, latest, used, note))
        else:
            note = f"{age}; no release meets --min-age {min_age_hours}h"
            rows.append(("cooldown", repo, latest, "held", note))

    def latest_tag(repo: str) -> str | None:
        if repo in latest_tags:
            return latest_tags[repo]
        emit(f"actions: checking releases for {repo}")

        result = shared.run(
            ["gh", "api", f"repos/{repo}/releases?per_page=100"],
            allow_not_found=True,
        )
        if result.returncode != 0:
            shared.console.print(f"[yellow]actions: no releases for {repo}, skipped[/]")
            latest_tags[repo] = None
            return None

        # tag -> (version, published_at ISO, age hours). Skip floating majors (v3);
        # full X.Y.Z releases carry the publish time we want for --min-age.
        candidates: dict[str, tuple[Version, str, float | None]] = {}
        for release in json.loads(result.stdout):
            if release.get("draft") or release.get("prerelease"):
                continue
            tag = release.get("tag_name") or ""
            if not shared.is_full_semver(tag):
                continue
            published = release.get("published_at") or ""
            try:
                version = Version(tag)
            except InvalidVersion:
                continue
            candidates[tag] = (
                version,
                published,
                shared.age_hours(published) if published else None,
            )

        if not candidates:
            # Fall back for repos that only ship floating or non-semver tags.
            latest = shared.run(["gh", "api", f"repos/{repo}/releases/latest"], allow_not_found=True)
            if latest.returncode != 0:
                shared.console.print(f"[yellow]actions: no latest release for {repo}, skipped[/]")
                latest_tags[repo] = None
                return None
            payload = json.loads(latest.stdout)
            raw = payload.get("tag_name") or ""
            tag = prefer_full_semver(repo, raw) if raw else ""
            try:
                Version(tag)
            except InvalidVersion:
                latest_tags[repo] = None
                return None
            published = payload.get("published_at") or ""
            candidates[tag] = (
                Version(tag),
                published,
                shared.age_hours(published) if published else None,
            )

        absolute_tag = max(candidates, key=lambda t: candidates[t][0])
        absolute_age = candidates[absolute_tag][2]

        if min_age_hours <= 0:
            latest_tags[repo] = absolute_tag
            return absolute_tag

        eligible = [
            tag
            for tag, (_ver, _pub, hours) in candidates.items()
            if hours is not None and hours >= min_age_hours
        ]
        if not eligible:
            note_cooldown(repo, absolute_tag, None, absolute_age)
            latest_tags[repo] = None
            return None

        chosen = max(eligible, key=lambda t: candidates[t][0])
        if chosen != absolute_tag:
            note_cooldown(repo, absolute_tag, chosen, absolute_age)
        latest_tags[repo] = chosen
        return chosen

    def note_maintenance(name: str, repo: str, rel: str, suffix: str = "") -> None:
        status = shared.github_freshness(repo)
        if status is None:
            return
        kind, old, new = status
        key = (kind, repo.lower(), rel, suffix)
        if key in noted:
            return
        noted.add(key)
        rows.append((kind, name, old, new, f"{rel} {suffix}".strip()))

    for path in shared.find_action_files():
        rel = str(path.relative_to(shared.context().root))
        emit(f"actions: checking {rel}")
        original = path.read_text()

        def replace(match: re.Match[str], rel: str = rel, text: str = original) -> str:
            action = match["action"]
            ref = match["ref"]
            repo = "/".join(action.split("/")[:2])
            watch_name, watch_repo, suffix = action, repo, ""

            def finish(line: str) -> str:
                note_maintenance(watch_name, watch_repo, rel, suffix)
                return line

            if shared.has_NO_AUTO_UPDATE(text, match.start("action")):
                shared.context().opt_out_skips.append((rel, action, ref))
                return finish(match[0])
            replacement = shared.ACTION_REPLACEMENTS.get(repo.lower())
            if replacement:
                new_action = replacement + action[len(repo) :]
                tag = latest_tag(replacement) or ""
                try:
                    Version(tag)
                except InvalidVersion:
                    tag = ""
                sha = commit_sha(replacement, tag) if tag else None
                if sha:
                    note_maintenance(action, repo, rel, "replaced")
                    watch_name, watch_repo = new_action, replacement
                    updated = (
                        f"{match['prefix']}{new_action}@{sha}{shared.action_comment(match['comment'], tag)}"
                    )
                    rows.append(("pin", new_action, ref, tag, f"{rel} replaces {action}"))
                    return finish(updated)
            pinned = re.fullmatch(r"[0-9a-f]{40}", ref) is not None
            if pinned:
                label = shared.VERSION_IN_COMMENT_RE.search(match["comment"])
                if not label:
                    return finish(match[0])
                old = label.group(0)
            else:
                old = ref
            try:
                current = Version(old)
            except InvalidVersion:
                # Branch refs have no current version, so take the latest release.
                # Reusable workflows stay on the commit of the ref they name.
                if pinned or ".github/workflows/" in action:
                    return finish(match[0])
                tag = latest_tag(repo) or ""
                if not tag:
                    return finish(match[0])
                try:
                    Version(tag)
                except InvalidVersion:
                    return finish(match[0])
                sha = commit_sha(repo, tag)
                if not sha:
                    return finish(match[0])
                updated = f"{match['prefix']}{action}@{sha}{shared.action_comment(match['comment'], tag)}"
                if updated == match[0]:
                    return finish(match[0])
                rows.append(("pin", action, old, tag, rel))
                return finish(updated)

            # Reusable workflows are pinned to their own ref, not the repo's latest release.
            in_place = ".github/workflows/" in action
            held = False
            tag = sha = ""
            if not in_place:
                tag = latest_tag(repo) or ""
                if not tag:
                    return finish(match[0])
                try:
                    newest = Version(tag)
                except InvalidVersion:
                    # Latest release is not an action version (e.g. a tool bundle).
                    newest = None
                if newest is None or newest < current:
                    in_place = True
                    tag = ""
                elif newest.major > current.major and not allow_major:
                    rows.append(
                        (
                            "major",
                            action,
                            old,
                            tag,
                            f"{rel} held by --no-major-version-updates",
                        )
                    )
                    in_place = held = True
                    tag = ""
            if in_place:
                if pinned:
                    return finish(match[0])
                sha = commit_sha(repo, ref)
                if not sha:
                    return finish(match[0])
                tag = ref
            else:
                sha = commit_sha(repo, tag)
                if not sha:
                    return finish(match[0])
            updated = f"{match['prefix']}{action}@{sha}{shared.action_comment(match['comment'], tag)}"
            if updated == match[0]:
                return finish(match[0])
            if not held:
                same_release = not pinned and current == Version(tag)
                new = sha[:12] if same_release or in_place else tag
                kind = shared.bump_type(old, tag) or "pin"
                rows.append((kind, action, old, new, rel))
            return finish(updated)

        updated = shared.ACTION_RE.sub(replace, original)
        if updated != original and not dry_run:
            path.write_text(updated)
    return rows
