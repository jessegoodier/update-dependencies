"""Docker dependency update operations."""

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from .. import engine as shared
from ..progress import emit


def image_digest(image: str, *, ecr_tokens: dict[str, str]) -> str:
    """Resolve a registry digest. ECR auth tokens are cached per repository for the run."""
    emit(f"docker: resolving digest for {image}")
    if image.startswith("public.ecr.aws/"):
        # Public ECR supports anonymous pulls. Prefer a token fetched this run over
        # Docker's potentially expired operator credentials.
        repository, tag = image.removeprefix("public.ecr.aws/").rsplit(":", 1)
        if repository not in ecr_tokens:
            query = urllib.parse.urlencode(
                {
                    "service": "public.ecr.aws",
                    "scope": f"repository:{repository}:pull",
                    "t": time.time_ns(),
                }
            )
            with shared.open_url(
                f"https://public.ecr.aws/token/?{query}",
                timeout=shared.context().config.timeout,
                retries=shared.context().config.retries,
            ) as response:
                ecr_tokens[repository] = json.load(response)["token"]
        request = urllib.request.Request(
            f"https://public.ecr.aws/v2/{repository}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {ecr_tokens[repository]}",
                "Accept": "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.oci.image.manifest.v1+json, "
                "application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        with shared.open_url(
            request,
            timeout=shared.context().config.timeout,
            retries=shared.context().config.retries,
        ) as response:
            return "sha256:" + hashlib.sha256(response.read()).hexdigest()
    manifest = json.loads(
        shared.run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                image,
                "--format",
                "{{json .Manifest}}",
            ]
        ).stdout
    )
    return manifest["digest"]


def update_docker(
    *, dry_run: bool = False, enable_latest: bool = False
) -> list[tuple[str, str, str, str, str]]:
    """Pin or refresh image digests. Return rows (kind, image, old, new, dockerfile)."""
    rows: list[tuple[str, str, str, str, str]] = []
    digests: dict[str, str] = {}
    ecr_tokens: dict[str, str] = {}
    noted: set[tuple[str, str]] = set()
    for path in shared.find_dockerfiles():
        rel = str(path.relative_to(shared.context().root))
        emit(f"docker: checking {rel}")
        original = path.read_text()
        stages: set[str] = set()

        def replace(
            match: re.Match[str],
            rel: str = rel,
            text: str = original,
            stages: set[str] = stages,
        ) -> str:
            ref = match["ref"]
            is_stage = ref.lower() in stages
            stages.update(s.lower() for s in shared.STAGE_RE.findall(match["rest"]))
            if is_stage or ref.lower() == "scratch" or "$" in ref:
                return match[0]
            image = ref if ":" in ref.rsplit("/", 1)[-1] else f"{ref}:latest"
            if (image, rel) not in noted:
                noted.add((image, rel))
                status = shared.image_freshness(image)
                if status:
                    rows.append((status[0], image, status[1], status[2], rel))
            if shared.has_NO_AUTO_UPDATE(text, match.start()):
                shared.context().opt_out_skips.append((rel, ref, match["digest"] or "unpinned"))
                return match[0]
            if shared.is_latest_image(image) and not enable_latest:
                shared.context().docker_latest_skips.append((rel, image, match["digest"] or "unpinned"))
                return match[0]
            if image not in digests:
                digest = shared.image_digest(image, ecr_tokens=ecr_tokens)
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                    raise ValueError(f"Invalid registry digest for {image}: {digest}")
                digests[image] = digest
            if match["digest"] == digests[image] and image == ref:
                return match[0]
            rows.append(
                (
                    "digest" if match["digest"] else "pin",
                    image,
                    match["digest"] or "unpinned",
                    digests[image],
                    rel,
                )
            )
            return f"{match['prefix']}{image}@{digests[image]}{match['rest']}"

        updated = shared.FROM_RE.sub(replace, original)
        if updated != original and not dry_run:
            path.write_text(updated)
    return rows
