"""Publish transformed skills to StrawHub via multipart API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .crawler import CrawledSkill, _request_with_retry

logger = logging.getLogger(__name__)

STRAWHUB_TARGETS = {
    "production": "https://descriptive-crab-211.convex.site",
    "preview": "https://notable-monitor-301.convex.site",
    "local": "http://127.0.0.1:3211",
}
STRAWHUB_BASE = STRAWHUB_TARGETS["production"]

ATTRIBUTION = (
    "Originally published on ClawHub (https://clawhub.ai). "
    "Licensed under the MIT License. "
    "Copyright (c) original skill author(s)."
)


def _build_changelog(skill: CrawledSkill) -> str:
    """Build changelog with ClawHub attribution."""
    parts = []
    if skill.changelog:
        parts.append(skill.changelog)
    parts.append(f"Imported from ClawHub (v{skill.version}). {ATTRIBUTION}")
    return "\n\n".join(parts)


@dataclass
class PublishResult:
    slug: str
    success: bool
    status_code: int
    message: str


async def publish_skill(
    client: httpx.AsyncClient,
    skill: CrawledSkill,
    token: str,
    base_url: str = STRAWHUB_BASE,
) -> PublishResult:
    """Publish a single skill to StrawHub via POST /api/v1/skills."""

    # Build multipart form data
    fields: dict[str, str] = {
        "slug": skill.slug,
        "displayName": skill.display_name,
        "version": skill.version,
        "changelog": _build_changelog(skill),
    }

    # Add dependencies if the transformed SKILL.md has them
    fields["dependencies"] = json.dumps({"skills": []})

    # Add import source for ownership claim tracking
    if skill.owner and skill.owner.github_id:
        fields["importSource"] = json.dumps({
            "source": "clawhub",
            "originalOwnerHandle": skill.owner.handle,
            "originalOwnerGithubId": skill.owner.github_id,
        })

    # Build file parts
    files_list: list[tuple[str, tuple[str, bytes, str]]] = []
    for f in skill.files:
        if f.content is None:
            logger.warning("Skipping file %s/%s (no content)", skill.slug, f.path)
            continue
        content_type = "text/markdown" if f.path.endswith(".md") else "application/octet-stream"
        files_list.append(("files", (f.path, f.content, content_type)))

    if not files_list:
        return PublishResult(
            slug=skill.slug,
            success=False,
            status_code=0,
            message="No files to upload",
        )

    # Check that SKILL.md exists
    has_skill_md = any(name == "SKILL.md" for _, (name, _, _) in files_list)
    if not has_skill_md:
        return PublishResult(
            slug=skill.slug,
            success=False,
            status_code=0,
            message="Missing SKILL.md",
        )

    try:
        resp = await _request_with_retry(
            client, "POST",
            f"{base_url}/api/v1/skills",
            data=fields,
            files=files_list,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )

        logger.info("Published %s (v%s) → StrawHub", skill.slug, skill.version)
        return PublishResult(
            slug=skill.slug,
            success=True,
            status_code=resp.status_code,
            message="OK",
        )
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        # Treat duplicate version as success (idempotent import)
        if e.response.status_code == 400 and "version" in body.lower() and "already exists" in body.lower():
            logger.info("Skipped %s (v%s) — version already exists", skill.slug, skill.version)
            return PublishResult(
                slug=skill.slug,
                success=True,
                status_code=e.response.status_code,
                message="already exists",
            )
        logger.warning(
            "Failed to publish %s: %d %s", skill.slug, e.response.status_code, body
        )
        return PublishResult(
            slug=skill.slug,
            success=False,
            status_code=e.response.status_code,
            message=body,
        )
    except Exception as e:
        logger.exception("Error publishing %s", skill.slug)
        return PublishResult(
            slug=skill.slug,
            success=False,
            status_code=0,
            message=str(e),
        )


async def publish_all(
    client: httpx.AsyncClient,
    skills: list[CrawledSkill],
    token: str,
    dry_run: bool = False,
    base_url: str = STRAWHUB_BASE,
) -> list[PublishResult]:
    """Publish all skills to StrawHub."""
    results: list[PublishResult] = []

    for skill in skills:
        if dry_run:
            logger.info("[DRY RUN] Would publish: %s (v%s)", skill.slug, skill.version)
            results.append(PublishResult(
                slug=skill.slug,
                success=True,
                status_code=0,
                message="dry-run",
            ))
            continue

        result = await publish_skill(client, skill, token, base_url=base_url)
        results.append(result)

    return results
