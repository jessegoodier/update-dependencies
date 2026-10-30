"""Update operations and formatting-preserving dependency rewrites."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .context import context
from .processes import execute, open_url
from .progress import emit

type SpecsFn = Callable[[str], Iterator[tuple[int, int, str, Requirement]]]

console = Console(stderr=True)

ECOSYSTEMS = ("python", "actions", "docker", "hooks")


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    allow_not_found: bool = False,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    return execute(
        command,
        cwd=cwd or context().root,
        timeout=context().config.timeout,
        retries=context().config.retries,
        allow_not_found=allow_not_found,
        allow_failure=allow_failure,
    )


DEPENDENCY_SECTIONS = {"project", "project.optional-dependencies", "dependency-groups"}
SPEC_RE = re.compile(r'"([^"\n]+)"')


def dependency_specs(text: str) -> Iterator[tuple[int, int, str, Requirement]]:
    """Yield requirement spans without rewriting TOML formatting or comments."""
    section = ""
    in_dependencies = False
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.strip("[]")
        if section == "project" and stripped.startswith("dependencies = ["):
            in_dependencies = True
        if section in DEPENDENCY_SECTIONS and (section != "project" or in_dependencies):
            # Comments can contain quoted examples; only inspect the TOML content.
            content = line.split("#", 1)[0]
            if "include-group" in content:
                offset += len(line)
                continue
            for match in SPEC_RE.finditer(content):
                try:
                    requirement = Requirement(match.group(1))
                except InvalidRequirement:
                    continue
                if requirement.url:
                    continue
                yield (
                    offset + match.start(1),
                    offset + match.end(1),
                    match.group(1),
                    requirement,
                )
        if section == "project" and stripped.endswith("]"):
            in_dependencies = False
        offset += len(line)


def pep723_dependency_specs(text: str) -> Iterator[tuple[int, int, str, Requirement]]:
    """Yield requirement spans from a leading PEP 723 `# /// script` block.

    Only the first script block in the file preamble (shebang, blank lines, and
    `#` comments before any code) is considered, so examples inside string
    literals later in the file are ignored.
    """
    in_script = False
    in_dependencies = False
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = stripped.rstrip("\r\n")
        is_blank = not line.strip()
        is_comment = stripped.startswith("#")
        if not in_script:
            if not is_blank and not is_comment:
                break
            if marker == "# /// script":
                in_script = True
            offset += len(line)
            continue
        if marker == "# ///":
            offset += len(line)
            break
        if not is_comment:
            offset += len(line)
            continue
        hash_at = len(line) - len(stripped)
        after_hash = stripped[1:]
        if after_hash.startswith((" ", "\t")):
            toml_line = after_hash[1:]
            toml_offset = offset + hash_at + 2
        elif after_hash in ("", "\n", "\r\n"):
            offset += len(line)
            continue
        else:
            toml_line = after_hash
            toml_offset = offset + hash_at + 1
        toml_stripped = toml_line.strip()
        if toml_stripped.startswith("dependencies = ["):
            in_dependencies = True
        if in_dependencies:
            content = toml_line.split("#", 1)[0]
            for match in SPEC_RE.finditer(content):
                try:
                    requirement = Requirement(match.group(1))
                except InvalidRequirement:
                    continue
                if requirement.url:
                    continue
                yield (
                    toml_offset + match.start(1),
                    toml_offset + match.end(1),
                    match.group(1),
                    requirement,
                )
            if toml_stripped.endswith("]"):
                in_dependencies = False
        offset += len(line)


def pep508_name(spec: str) -> str:
    name = re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pyproject_direct() -> set[str]:
    if not context().pyproject.is_file():
        return set()
    return {pep508_name(req.name) for _, _, _, req in dependency_specs(context().pyproject.read_text())}


def parse_inline_script_direct() -> set[str]:
    return {
        pep508_name(req.name)
        for path in find_pep723_scripts()
        for _, _, _, req in pep723_dependency_specs(path.read_text(encoding="utf-8"))
    }


def relax_spec(spec: str, req: Requirement, *, allow_major: bool = True) -> str:
    updated = spec
    for constraint in req.specifier:
        if constraint.operator == "==" and "*" not in constraint.version:
            updated = updated.replace(f"=={constraint.version}", f">={constraint.version}", 1)
            if req.marker:
                # Distinct pins under Python markers intentionally track different major lines.
                upper = Version(constraint.version).major + 1
                updated = updated.replace(f">={constraint.version}", f">={constraint.version},<{upper}", 1)
        elif allow_major and constraint.operator == "<" and not req.marker:
            updated = re.sub(r",?\s*<" + re.escape(constraint.version) + r"(?=,|;|$)", "", updated)
    return updated


def hold_major_lines(text: str, locked: dict[str, str], *, allow_major: bool, specs=None) -> str:
    if allow_major:
        return text
    for start, end, spec, req in reversed(list((specs or dependency_specs)(text))):
        if req.url or has_NO_AUTO_UPDATE(text, start):
            continue
        current = locked.get(pep508_name(req.name))
        if current is None:
            # A specifier floor is a fallback for a project without a lock.
            floors = [
                Version(c.version)
                for c in req.specifier
                if c.operator in {"==", ">="} and "*" not in c.version
            ]
            if not floors:
                raise ValueError(f"Cannot hold major version for {req.name} without a lock or version floor")
            version = max(floors)
        else:
            version = Version(current)
        upper = f"{version.major + 1}" if version.major else f"0.{version.minor + 1}"
        body, sep, marker = spec.partition(";")
        suffix = "," if req.specifier else ""
        bounded = body.rstrip() + suffix + f"<{upper}" + (f";{marker}" if sep else "")
        text = text[:start] + bounded + text[end:]
    return text


def frozen_requirement(spec: str, version: str) -> str:
    """Replace whatever specifier `spec` has with an exact pin, keeping extras and markers."""
    body, marker = (spec.split(";", 1) + [""])[:2]
    base = re.split(r"\s*(?:==|>=|<=|!=|~=|===|>|<)", body.strip(), maxsplit=1)[0]
    suffix = f";{marker}" if marker else ""
    return f"{base}=={version}{suffix}"


def freeze_opted_out(text: str, locked: Mapping[str, str], *, specs: SpecsFn = dependency_specs) -> str:
    """Hold NO_AUTO_UPDATE requirements at their locked version for this resolve.

    `uv lock --upgrade` will not move a requirement that is pinned to the version
    already in the lock. The original text is what gets written back afterwards.
    """
    for start, end, spec, req in reversed(list(specs(text))):
        if not has_NO_AUTO_UPDATE(text, start):
            continue
        version = locked.get(pep508_name(req.name))
        if not version:
            continue
        text = text[:start] + frozen_requirement(spec, version) + text[end:]
    return text


def relax_constraints(text: str, *, allow_major: bool = True, specs: SpecsFn = dependency_specs) -> str:
    """Let uv resolve exact pins; retain marker-specific major lines and bounds.

    A requirement whose line, or the comment on the line above, contains
    NO_AUTO_UPDATE keeps its current specifier.
    """
    for start, end, spec, req in reversed(list(specs(text))):
        if has_NO_AUTO_UPDATE(text, start):
            continue
        text = text[:start] + relax_spec(spec, req, allow_major=allow_major) + text[end:]
    return text


def add_minimum_version(spec: str, version: str) -> str:
    """Insert >=version after name/extras, before any environment marker."""
    if ";" in spec:
        left, right = spec.split(";", 1)
        return f"{left.rstrip()}>={version};{right}"
    return f"{spec}>={version}"


def update_requirement_constraints(
    locked_versions: dict[str, list[str]],
    *,
    original: str,
    allow_major: bool = True,
    add_latest: bool = False,
    specs: SpecsFn = dependency_specs,
    skip_label: str = "pyproject.toml",
) -> tuple[str, list[tuple[str, str, str]]]:
    changes = []
    text = original
    for start, end, spec, req in reversed(list(specs(original))):
        if has_NO_AUTO_UPDATE(original, start):
            context().opt_out_skips.append((skip_label, spec, "unchanged"))
            continue
        relaxed = Requirement(relax_spec(spec, req, allow_major=allow_major))
        candidates = [
            v for v in locked_versions.get(pep508_name(req.name), []) if Version(v) in relaxed.specifier
        ]
        if not candidates:
            continue
        version = max(candidates, key=Version)
        updated = spec
        if add_latest and not req.specifier and not req.url:
            updated = add_minimum_version(spec, version)
        else:
            for constraint in req.specifier:
                if constraint.operator in ("==", ">=") and "*" not in constraint.version:
                    if Version(version) > Version(constraint.version):
                        updated = updated.replace(
                            f"{constraint.operator}{constraint.version}",
                            f"{constraint.operator}{version}",
                            1,
                        )
                elif allow_major and constraint.operator == "<" and Version(version) not in req.specifier:
                    updated = updated.replace(
                        f"<{constraint.version}",
                        f"<{Version(version).major + 1}",
                        1,
                    )
        if updated != spec:
            text = text[:start] + updated + text[end:]
            changes.append((pep508_name(req.name), spec, updated))
    return text, list(reversed(changes))


def update_pyproject_constraints(
    locked_versions: dict[str, list[str]],
    *,
    original: str,
    allow_major: bool = True,
    add_latest: bool = False,
) -> list[tuple[str, str, str]]:
    text, changes = update_requirement_constraints(
        locked_versions,
        original=original,
        allow_major=allow_major,
        add_latest=add_latest,
        specs=dependency_specs,
        skip_label="pyproject.toml",
    )
    context().pyproject.write_text(text, encoding="utf-8")
    return changes


def parse_uv_lock_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return {pep508_name(pkg["name"]): pkg["version"] for pkg in data.get("package", []) if "version" in pkg}


def parse_uv_lock() -> dict[str, str]:
    return parse_uv_lock_file(context().uv_lock)


def locked_version_lists(path: Path) -> dict[str, list[str]]:
    with path.open("rb") as handle:
        packages = tomllib.load(handle).get("package", [])
    versions: dict[str, list[str]] = {}
    for package in packages:
        if "version" in package:
            versions.setdefault(pep508_name(package["name"]), []).append(package["version"])
    return versions


def script_lock_path(script: Path) -> Path:
    return Path(f"{script}.lock")


def version_parts(version: str) -> tuple[int, int, int]:
    cleaned = version.lstrip("v").split("-", 1)[0]
    parts: list[int] = []
    for piece in cleaned.split("."):
        digits = "".join(char for char in piece if char.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def bump_type(old: str, new: str) -> str | None:
    old_parts = version_parts(old)
    new_parts = version_parts(new)
    if new_parts <= old_parts:
        return None
    if new_parts[0] > old_parts[0]:
        return "major"
    if new_parts[1] > old_parts[1]:
        return "minor"
    return "patch"


def collect_changes(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    direct: set[str] | None = None,
) -> dict[str, list[tuple[str, str, str]]]:
    changes: dict[str, list[tuple[str, str, str]]] = {
        "major": [],
        "minor": [],
        "patch": [],
    }
    for name in sorted(set(before) | set(after)):
        if direct is not None and name not in direct:
            continue
        old = before.get(name)
        new = after.get(name)
        if not old or not new or old == new:
            continue
        kind = bump_type(old, new)
        if kind:
            changes[kind].append((name, old, new))
    return changes


KIND_STYLE = {
    "major": "bold red",
    "minor": "yellow",
    "patch": "blue",
    "spec": "magenta",
    "pin": "yellow",
    "digest": "blue",
    "cooldown": "blue",
    "stale": "bold red",
    "archived": "bold red",
}

# Hours a GitHub Actions release must have been published before we adopt it.
DEFAULT_MIN_AGE_HOURS = 48

# Days since the last publish/push before a dependency is reported as stale.
STALE_DAYS = 180
# Lowercase owner/repo -> maintained owner/repo, pinned to that repo's latest release.
ACTION_REPLACEMENTS = {
    "sonarsource/sonarcloud-github-action": "SonarSource/sonarqube-scan-action",
}


def short_rev(value: str) -> str:
    if re.fullmatch(r"(?:sha256:)?[0-9a-f]{12,64}", value):
        return value.removeprefix("sha256:")[:12]
    return value


def emit_section(
    title: str,
    rows: list[tuple[str, str, str, str, str]],
    *,
    extra: str = "",
) -> None:
    """Print one ecosystem as a colored count line plus borderless bump rows."""
    if not rows and not extra:
        console.print(f"[bold cyan]{title:<7}[/] [green]up to date[/]")
        return
    counts: dict[str, int] = {}
    for kind, *_rest in rows:
        counts[kind] = counts.get(kind, 0) + 1
    bits = []
    for kind, count in counts.items():
        if kind == "major":
            bits.append(f"[bold white on red] {count} {kind} [/]")
        else:
            bits.append(f"[{KIND_STYLE.get(kind, 'white')}]{count} {kind}[/]")
    if extra:
        bits.append(f"[dim]{extra}[/]")
    console.print(f"[bold cyan]{title:<7}[/] " + "  ".join(bits))
    if not rows:
        return
    table = Table(
        box=None,
        show_header=False,
        pad_edge=False,
        padding=(0, 1),
        collapse_padding=True,
        expand=False,
    )
    table.add_column(min_width=9, no_wrap=True)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(style="dim", justify="right", no_wrap=True)
    table.add_column(style="dim", width=1, no_wrap=True)
    table.add_column(style="green", no_wrap=True)
    table.add_column(style="dim italic", no_wrap=True)
    for kind, name, old, new, note in rows:
        if kind == "major":
            table.add_row(
                Text(f" {kind} ", style="bold white on red"),
                Text(name, style="bold red"),
                Text(short_rev(old), style="red"),
                Text("→", style="bold red"),
                Text(short_rev(new), style="bold red"),
                Text(note, style="red"),
            )
            continue
        table.add_row(
            Text(kind, style=KIND_STYLE.get(kind, "white")),
            name,
            short_rev(old),
            "→",
            short_rev(new),
            note,
        )
    console.print(table)


def update_python(*args, **kwargs):
    """Dispatch to the python update operations."""
    from .ecosystems.python import update_python as operation

    return operation(*args, **kwargs)


def update_inline_script(*args, **kwargs):
    """Dispatch to the python update operations."""
    from .ecosystems.python import update_inline_script as operation

    return operation(*args, **kwargs)


def update_inline_scripts(*args, **kwargs):
    """Dispatch to the python update operations."""
    from .ecosystems.python import update_inline_scripts as operation

    return operation(*args, **kwargs)


UV_IMAGE_RE = re.compile(
    r"^(?P<prefix>[ \t]*FROM[ \t]+(?:--\S+[ \t]+)*ghcr\.io/astral-sh/uv:)"
    r"(?P<version>\d+(?:\.\d+)*)(?P<digest>@sha256:[0-9a-f]{64})?(?=[ \t]|$)",
    re.MULTILINE | re.IGNORECASE,
)
UV_BUILD_RE = re.compile(r'"(?P<spec>uv[-_.]build\b[^"\n]*)"', re.IGNORECASE)


def dockerfile_uv_version() -> str | None:
    """uv version pinned by a `FROM ghcr.io/astral-sh/uv:X.Y.Z` line, if any."""
    versions = {
        match["version"] for path in find_dockerfiles() for match in UV_IMAGE_RE.finditer(path.read_text())
    }
    if len(versions) > 1:
        console.print(
            f"[yellow]python: Dockerfiles pin different uv versions "
            f"({', '.join(sorted(versions, key=Version))}), uv_build left as-is[/]"
        )
        return None
    return next(iter(versions), None)


def latest_uv_build(
    min_age_hours: int,
) -> tuple[str | None, list[tuple[str, str, str, str, str]]]:
    """Newest uv_build on PyPI that is at least min_age_hours old, plus note rows.

    Same cooldown as GitHub Actions releases: a newer release inside the window
    is reported as a cooldown row and the newest older one is used instead.
    """
    data = fetch_json("https://pypi.org/pypi/uv-build/json")
    releases = data.get("releases") if data else None
    if not isinstance(releases, dict):
        console.print("[yellow]python: could not read uv_build releases, skipped[/]")
        return None, []
    # version -> hours since first upload, for stable, un-yanked releases.
    ages: dict[Version, float | None] = {}
    for name, files in releases.items():
        try:
            version = Version(name)
        except InvalidVersion:
            continue
        live = [item for item in files if isinstance(item, dict) and not item.get("yanked")]
        if version.is_prerelease or version.is_devrelease or not live:
            continue
        stamps = [
            hours
            for item in live
            if isinstance(item.get("upload_time_iso_8601"), str)
            and (hours := age_hours(item["upload_time_iso_8601"])) is not None
        ]
        ages[version] = max(stamps) if stamps else None
    if not ages:
        return None, []
    newest = max(ages)
    if min_age_hours <= 0:
        return str(newest), []
    eligible = [v for v, hours in ages.items() if hours is not None and hours >= min_age_hours]
    chosen = max(eligible) if eligible else None
    rows: list[tuple[str, str, str, str, str]] = []
    if chosen != newest:
        hours = ages[newest]
        age = f"{hours:.0f}h old" if hours is not None else "age unknown"
        used = str(chosen) if chosen else "held"
        note = f"{age}; --min-age {min_age_hours}h"
        rows.append(("cooldown", "uv_build", str(newest), used, note))
    return (str(chosen) if chosen else None), rows


def bump_dockerfile_uv(version: str, *, dry_run: bool) -> tuple[list[tuple[str, str, str, str, str]], bool]:
    """Raise `FROM ghcr.io/astral-sh/uv:X` lines to version.

    Returns (rows, blocked). The digest is dropped because it belongs to the old
    tag; the docker ecosystem pins the new one. A line marked NO_AUTO_UPDATE is
    left alone and sets blocked.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    blocked = False
    for path in find_dockerfiles():
        rel = str(path.relative_to(context().root))
        original = path.read_text()

        def replace(match: re.Match[str], rel: str = rel, text: str = original) -> str:
            nonlocal blocked
            old = match["version"]
            if Version(version) <= Version(old):
                return match[0]
            if has_NO_AUTO_UPDATE(text, match.start()):
                context().opt_out_skips.append((rel, f"ghcr.io/astral-sh/uv:{old}", ""))
                blocked = True
                return match[0]
            rows.append(("pin", "uv image", old, version, rel))
            return f"{match['prefix']}{version}"

        updated = UV_IMAGE_RE.sub(replace, original)
        if updated != original and not dry_run:
            path.write_text(updated)
    return rows, blocked


def sync_uv_build(
    *, dry_run: bool = False, add_latest: bool = False, min_age_hours: int = 0
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str, str]]]:
    """Keep the uv_build floor in [build-system] and the Dockerfile uv image aligned.

    Default: the Dockerfile's uv version wins, and the uv_build floor is set to it.
    With add_latest: the newest uv_build release that clears the min-age cooldown
    becomes the target. The Dockerfile uv image is raised to it too (never lowered),
    and the floor then follows the image. With no Dockerfile pin, the floor is only
    raised.
    """
    notes: list[tuple[str, str, str, str, str]] = []
    image = dockerfile_uv_version()
    version = image
    if add_latest:
        latest, notes = latest_uv_build(min_age_hours)
        candidates = [v for v in (image, latest) if v is not None]
        version = max(candidates, key=Version) if candidates else None
        if version is not None and image is not None:
            rows, blocked = bump_dockerfile_uv(version, dry_run=dry_run)
            notes = notes + rows
            if blocked:
                version = image
    if version is None:
        return [], notes
    return _write_uv_build_floor(version, dry_run, raise_only=image is None), notes


def _write_uv_build_floor(version: str, dry_run: bool, *, raise_only: bool) -> list[tuple[str, str, str]]:
    text = context().pyproject.read_text(encoding="utf-8")
    header = re.search(r"^\[build-system\][ \t]*$", text, re.MULTILINE)
    if header is None:
        return []
    next_table = re.compile(r"^\[", re.MULTILINE).search(text, header.end())
    section_end = next_table.start() if next_table else len(text)
    match = UV_BUILD_RE.search(text, header.end(), section_end)
    if match is None:
        return []
    start = match.start("spec")
    spec = match["spec"]
    if has_NO_AUTO_UPDATE(text, start):
        context().opt_out_skips.append(("pyproject.toml", spec, "unchanged"))
        return []
    requirement = Requirement(spec)
    floors = [c for c in requirement.specifier if c.operator == ">="]
    if len(floors) != 1:
        console.print(f"[yellow]python: {spec!r} has no single >= floor, skipped[/]")
        return []
    if Version(version) not in SpecifierSet(
        ",".join(str(c) for c in requirement.specifier if c.operator != ">=")
    ):
        console.print(f"[yellow]python: uv {version} is outside the bounds of {spec!r}, skipped[/]")
        return []
    if raise_only and Version(version) <= Version(floors[0].version):
        return []
    updated = spec.replace(str(floors[0]), f">={version}", 1)
    if updated == spec:
        return []
    if not dry_run:
        end = start + len(spec)
        context().pyproject.write_text(text[:start] + updated + text[end:], encoding="utf-8")
    return [("uv_build", spec, updated)]


ACTION_RE = re.compile(
    r"(?P<prefix>^\s*(?:-\s*)?uses:\s+)(?P<action>[\w.-]+/[\w./-]+)@"
    r"(?P<ref>[\w.-]+)(?P<comment>[^\n]*)",
    re.MULTILINE,
)
FROM_RE = re.compile(
    r"^(?P<prefix>[ \t]*FROM[ \t]+(?:--\S+[ \t]+)*)(?P<ref>[^\s@]+)"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?(?P<rest>[^\n]*)",
    re.MULTILINE | re.IGNORECASE,
)
STAGE_RE = re.compile(r"\bAS[ \t]+(\S+)", re.IGNORECASE)
VERSION_IN_COMMENT_RE = re.compile(r"(?<![\w.])(?:v\d+(?:\.\d+){0,2}|\d+\.\d+(?:\.\d+)?)\b", re.IGNORECASE)

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".tox", "__pycache__"}
COMMENT_STARTS = ("#", "//", ";", "--", "/*", "*", "<!--", "%", "'", "REM ")
# (file, name, ref) for every line left alone because of NO_AUTO_UPDATE.
# (file, image, digest_or_unpinned) for :latest images left alone by default.
# Per-run caches so skip_reason and the updaters share one file walk each.


def find_files(match) -> list[Path]:
    """Tracked and untracked-but-not-ignored files, or a plain walk outside git."""
    if context().files is not None:
        return [
            context().root / p for p in context().files if (context().root / p).is_file() and match(p.name)
        ]
    listed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=context().root,
        timeout=context().config.timeout,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode == 0:
        paths = (context().root / name for name in listed.stdout.split("\0") if name)
        return sorted(p for p in paths if p.is_file() and match(p.name))
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(context().root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        found += [Path(dirpath) / n for n in sorted(filenames) if match(n)]
    return found


def is_dockerfile(name: str) -> bool:
    lower = name.lower()
    return lower.startswith(("dockerfile", "containerfile")) or lower.endswith(".dockerfile")


def find_dockerfiles() -> list[Path]:
    if context().dockerfiles is None:
        context().dockerfiles = find_files(is_dockerfile)
    return context().dockerfiles


def find_action_files() -> list[Path]:
    if context().action_files is None:
        context().action_files = [
            p
            for p in find_files(lambda n: n.endswith((".yml", ".yaml")))
            if p.relative_to(context().root).parts[:1] == (".github",)
            or p.name in {"action.yml", "action.yaml"}
        ]
    return context().action_files


def find_pep723_scripts() -> list[Path]:
    """Python files with at least one PEP 723 `# /// script` dependency."""
    if context().inline_scripts is None:
        found: list[Path] = []
        for path in find_files(lambda n: n.endswith(".py")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if next(pep723_dependency_specs(text), None) is not None:
                found.append(path)
        context().inline_scripts = found
    return context().inline_scripts


def has_NO_AUTO_UPDATE(text: str, pos: int) -> bool:
    """NO_AUTO_UPDATE on the line holding `pos`, or in a comment on the line above."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if "NO_AUTO_UPDATE" in text[start : end if end != -1 else len(text)]:
        return True
    if start == 0:
        return False
    above = text[: start - 1].rsplit("\n", 1)[-1].strip()
    return "NO_AUTO_UPDATE" in above and above.startswith(COMMENT_STARTS)


def parse_timestamp(stamp: str) -> datetime | None:
    text = stamp.replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when


def age_days(stamp: str) -> int | None:
    when = parse_timestamp(stamp)
    if when is None:
        return None
    return (datetime.now(UTC) - when).days


def age_hours(stamp: str) -> float | None:
    when = parse_timestamp(stamp)
    if when is None:
        return None
    return (datetime.now(UTC) - when).total_seconds() / 3600


def freshness(when: str | None, *, archived: bool, status: str = "") -> tuple[str, str, str] | None:
    """Red-row fields for an archived project or one quiet for STALE_DAYS."""
    days = age_days(when) if when else None
    age = f"{days}d" if days is not None else "unknown"
    if archived:
        return ("archived", status or "archived", age)
    if days is not None and days > context().config.stale_days:
        return ("stale", "last update", age)
    return None


def fetch_json(url: str) -> dict | None:
    try:
        with open_url(url, timeout=context().config.timeout, retries=context().config.retries) as response:
            payload = json.load(response)
    except (OSError, json.JSONDecodeError):  # fmt: skip
        return None
    return payload if isinstance(payload, dict) else None


def github_meta(repo: str) -> dict[str, object] | None:
    key = repo.lower()
    if key not in context().github_meta:
        if shutil.which("gh") is None:
            context().github_meta[key] = None
        else:
            result = run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}",
                    "--jq",
                    "{archived: .archived, pushed_at: .pushed_at}",
                ],
                allow_failure=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                context().github_meta[key] = None
            else:
                try:
                    context().github_meta[key] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    context().github_meta[key] = None
    return context().github_meta[key]


def github_freshness(repo: str) -> tuple[str, str, str] | None:
    meta = github_meta(repo)
    if not meta:
        return None
    pushed = meta.get("pushed_at")
    return freshness(
        pushed if isinstance(pushed, str) else None,
        archived=bool(meta.get("archived")),
    )


def pypi_freshness(name: str) -> tuple[str, str, str] | None:
    data = fetch_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if not data:
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    version = info.get("version") if isinstance(info.get("version"), str) else ""
    releases = data.get("releases") if isinstance(data.get("releases"), dict) else {}
    files = releases.get(version) if isinstance(releases.get(version), list) else []
    when: str | None = None
    yanked = bool(info.get("yanked"))
    for item in files:
        if not isinstance(item, dict):
            continue
        yanked = yanked or bool(item.get("yanked"))
        stamp = item.get("upload_time_iso_8601") or item.get("upload_time")
        if isinstance(stamp, str) and (when is None or stamp > when):
            when = stamp
    classifiers = info.get("classifiers") if isinstance(info.get("classifiers"), list) else []
    inactive = any(
        isinstance(item, str) and item.startswith("Development Status :: 7") for item in classifiers
    )
    status = "yanked" if yanked else "inactive" if inactive else ""
    return freshness(when, archived=bool(status), status=status)


def image_repo(image: str) -> tuple[str, str]:
    """Return (registry, repository) for a tagged image reference."""
    ref = image.split("@", 1)[0]
    name, _, _tag = ref.rpartition(":")
    if not name or "/" in _tag:
        name = ref
    parts = name.split("/")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        return parts[0].lower(), "/".join(parts[1:])
    return "docker.io", name


def image_freshness(image: str) -> tuple[str, str, str] | None:
    registry, repository = image_repo(image)
    if registry in {"docker.io", "index.docker.io"}:
        path = repository if "/" in repository else f"library/{repository}"
        data = fetch_json(f"https://hub.docker.com/v2/repositories/{path}/")
        if not data:
            return None
        desc = data.get("status_description")
        status = desc if isinstance(desc, str) else ""
        updated = data.get("last_updated")
        return freshness(
            updated if isinstance(updated, str) else None,
            archived=bool(status) and status.casefold() != "active",
            status=status or "archived",
        )
    if registry == "ghcr.io":
        owner_repo = "/".join(repository.split("/")[:2])
        if owner_repo.count("/") != 1:
            return None
        return github_freshness(owner_repo)
    return None


def is_full_semver(tag: str) -> bool:
    """True when tag is X.Y.Z (or vX.Y.Z), not a floating major like v3."""
    try:
        return len(Version(tag).release) >= 3
    except InvalidVersion:
        return False


def action_comment(existing: str, tag: str) -> str:
    """Comment after a pinned SHA naming the release that commit belongs to."""
    if VERSION_IN_COMMENT_RE.search(existing):
        return VERSION_IN_COMMENT_RE.sub(tag, existing, count=1)
    body = existing.strip()
    if body.startswith("#"):
        body = body[1:].strip()
    if body:
        return f" # {tag} {body}"
    return f" # {tag}"


def update_actions(*args, **kwargs):
    """Dispatch to the actions update operations."""
    from .ecosystems.actions import update_actions as operation

    return operation(*args, **kwargs)


def image_digest(*args, **kwargs):
    """Dispatch to the docker update operations."""
    from .ecosystems.docker import image_digest as operation

    return operation(*args, **kwargs)


def image_tag(image: str) -> str:
    """Return the tag portion of a name:tag reference (default latest if untagged)."""
    name = image.rsplit("/", 1)[-1]
    if ":" in name:
        return name.rsplit(":", 1)[-1]
    return "latest"


def is_latest_image(image: str) -> bool:
    """True when the image tag is the floating latest tag."""
    return image_tag(image).casefold() == "latest"


def update_docker(*args, **kwargs):
    """Dispatch to the docker update operations."""
    from .ecosystems.docker import update_docker as operation

    return operation(*args, **kwargs)


HOOK_UPDATE_RE = re.compile(r"Updating\s+(?P<repo>\S+).*?\bupdating\s+(?P<old>\S+)\s+->\s+(?P<new>\S+)")
HOOK_REPO_RE = re.compile(r"(?m)^[ \t]*-[ \t]*repo:[ \t]*(?P<repo>[^#\s]+)(?:[ \t]+#[^\n]*)?[ \t]*$")
HOOK_REV_RE = re.compile(r"(?m)^[ \t]*rev:[ \t]*(?P<rev>\S+)")


def repo_label(repo: str) -> str:
    repo = repo.removesuffix(".git")
    for prefix in ("https://github.com/", "https://gitlab.com/", "git@"):
        if repo.startswith(prefix):
            return repo.removeprefix(prefix)
    return repo


def precommit_repo_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, repo url) for each `- repo:` block in a pre-commit config."""
    matches = list(HOOK_REPO_RE.finditer(text))
    spans: list[tuple[int, int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        spans.append((match.start(), end, match["repo"]))
    return spans


def _line_starts(text: str, start: int, end: int) -> list[int]:
    positions = []
    pos = start
    while pos < end:
        positions.append(pos)
        newline = text.find("\n", pos, end)
        if newline == -1:
            break
        pos = newline + 1
    return positions


def protected_hook_revs(text: str) -> dict[str, str]:
    """Repo URL -> current rev, for repos opted out with NO_AUTO_UPDATE.

    The marker counts on the repo line, on any line in that repo's block, or in
    a comment on the line directly above the repo.
    """
    protected: dict[str, str] = {}
    for start, end, repo in precommit_repo_spans(text):
        if not any(has_NO_AUTO_UPDATE(text, pos) for pos in _line_starts(text, start, end)):
            continue
        rev = HOOK_REV_RE.search(text, start, end)
        if rev:
            protected[repo] = rev["rev"]
    return protected


def restore_protected_hook_revs(original: str, updated: str) -> str:
    """Put opted-out repos back on the rev they had before autoupdate."""
    protected = protected_hook_revs(original)
    for start, end, repo in reversed(precommit_repo_spans(updated)):
        if repo not in protected:
            continue
        rev = HOOK_REV_RE.search(updated, start, end)
        if not rev or rev["rev"] == protected[repo]:
            continue
        updated = updated[: rev.start("rev")] + protected[repo] + updated[rev.end("rev") :]
    return updated


def update_hooks(*args, **kwargs):
    """Dispatch to the hooks update operations."""
    from .ecosystems.hooks import update_hooks as operation

    return operation(*args, **kwargs)


def python_maintenance(names: set[str]) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for index, name in enumerate(sorted(names), 1):
        emit(f"python: checking maintenance {index}/{len(names)}: {name}")
        status = pypi_freshness(name)
        if status:
            rows.append((status[0], name, status[1], status[2], "pypi"))
    return rows


def print_python(
    *,
    python_direct: set[str],
    python_before: dict[str, str],
    python_after: dict[str, str],
    pyproject_constraint_changes: list[tuple[str, str, str]],
    maintenance: list[tuple[str, str, str, str, str]],
    inline_script_constraint_changes: list[tuple[str, str, str, str]] | None = None,
) -> None:
    changes = collect_changes(python_before, python_after)
    rows: list[tuple[str, str, str, str, str]] = []
    listed: set[str] = set()
    for kind in ("major", "minor", "patch"):
        for name, old, new in changes[kind]:
            direct = name in python_direct
            if kind == "patch" and not direct:
                continue
            rows.append((kind, name, old, new, "direct" if direct else ""))
            listed.add(name)
    for _name, old, new in pyproject_constraint_changes:
        package = pep508_name(old)
        if package in listed:
            continue
        rows.append(("spec", package, old, new, "pyproject"))
        listed.add(package)
    for _name, old, new, source in inline_script_constraint_changes or []:
        rows.append(("spec", pep508_name(old), old, new, source))
    rows.extend(maintenance)
    hidden = sum(1 for name, _old, _new in changes["patch"] if name not in python_direct)
    extra = ""
    if hidden:
        extra = f"{hidden} transitive patch" if hidden == 1 else f"{hidden} transitive patches"
    emit_section("python", rows, extra=extra)


def print_actions(rows: list[tuple[str, str, str, str, str]]) -> None:
    emit_section("actions", rows)


def print_docker(rows: list[tuple[str, str, str, str, str]]) -> None:
    emit_section("docker", rows)


def print_NO_AUTO_UPDATE() -> None:
    for path, name, ref in context().opt_out_skips:
        console.print(f"[bold yellow]NO_AUTO_UPDATE[/]  {name}@{short_rev(ref)} left as-is [dim]({path})[/]")


def print_docker_latest_skips() -> None:
    for path, image, ref in context().docker_latest_skips:
        console.print(
            f"[bold yellow]latest[/]  {image}@{short_rev(ref)} left as-is "
            f"[dim]({path}; --enable-docker-latest-to-sha-updates)[/]"
        )


def print_hooks(changes: list[tuple[str, str, str]]) -> None:
    rows = []
    for repo, old, new in changes:
        rows.append((bump_type(old, new) or "pin", repo, old, new, ""))
    emit_section("hooks", rows)


def skip_reason(ecosystem: str, *, update_inline_scripts: bool = True) -> str | None:
    """Ecosystems with nothing to update stay out of the run.

    This file's own dependencies are declared in its script header, so a
    repository does not need a project lockfile for the script to run.
    """
    if ecosystem == "python":
        has_project = context().pyproject.is_file()
        has_scripts = update_inline_scripts and bool(find_pep723_scripts())
        if not has_project and not has_scripts:
            if update_inline_scripts:
                return "no pyproject.toml or PEP 723 scripts"
            return "no pyproject.toml"
    if ecosystem == "actions" and not find_action_files():
        return "no .github workflow files"
    if ecosystem == "docker" and not find_dockerfiles():
        return "no Dockerfiles"
    if ecosystem == "hooks" and not context().pre_commit.is_file():
        return "no .pre-commit-config.yaml"
    return None


def default_ecosystems(args: argparse.Namespace) -> list[str]:
    """Ecosystems implied by CLI flags before any TUI override."""
    if getattr(args, "ecosystems", None):
        return list(args.ecosystems)
    if args.only:
        return [args.only]
    return list(ECOSYSTEMS)


def resolve_ecosystems(args: argparse.Namespace, *, only_mode: bool) -> set[str]:
    """Return ecosystems that will run; print skips for unavailable selections."""
    selected = (
        list(args.ecosystems) if getattr(args, "ecosystems", None) is not None else default_ecosystems(args)
    )
    ecosystems: set[str] = set()
    for ecosystem in selected:
        reason = skip_reason(
            ecosystem,
            update_inline_scripts=args.update_python_inline_script_metadata,
        )
        if reason is None:
            ecosystems.add(ecosystem)
            continue
        if only_mode:
            raise SystemExit(f"{ecosystem} updates need a project file ({reason})")
        console.print(f"[bold cyan]{ecosystem:<7}[/] [dim]skipped ({reason})[/]")
    return ecosystems
