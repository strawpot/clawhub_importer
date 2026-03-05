"""Crawl all skills from ClawHub API."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CLAWHUB_BASE = "https://clawhub.ai"
CLAWHUB_DOWNLOAD = "https://wry-manatee-359.convex.site/api/v1/download"

# Rate limits (from response headers):
#   List API:     120 req / 60s window
#   Download API:  20 req / 60s window
# We respect these by checking remaining quota and sleeping when needed.


@dataclass
class SkillFile:
    path: str
    size: int
    content: bytes | None = None


@dataclass
class CrawledSkill:
    slug: str
    display_name: str
    summary: str
    version: str
    changelog: str
    metadata: dict[str, Any] | None
    files: list[SkillFile] = field(default_factory=list)
    skill_md: str | None = None


MAX_RETRIES = 5


async def _respect_rate_limit(resp: httpx.Response) -> None:
    """Sleep to spread requests evenly across the rate limit window.

    ClawHub returns these headers on every response:
        ratelimit-limit:     120 (list) or 20 (download)
        ratelimit-remaining: requests left in current window
        ratelimit-reset:     seconds until window resets
    """
    remaining = resp.headers.get("ratelimit-remaining")
    reset = resp.headers.get("ratelimit-reset")
    if remaining is None or reset is None:
        return

    remaining_int = int(remaining)
    reset_secs = int(reset)

    if remaining_int <= 1:
        # Out of quota — wait for the full reset window
        wait = max(reset_secs, 1)
        logger.warning("Rate limit nearly exhausted (%d remaining), waiting %ds", remaining_int, wait)
        await asyncio.sleep(wait)
    elif remaining_int <= 10:
        # Running low — spread remaining requests across the reset window
        wait = max(reset_secs / max(remaining_int, 1), 1)
        logger.info("Rate limit low (%d remaining), throttling %.1fs", remaining_int, wait)
        await asyncio.sleep(wait)
    else:
        # Proactive pacing: ensure we don't exceed the rate limit
        # e.g. 20 req/60s → at least 3s between requests
        limit = resp.headers.get("ratelimit-limit")
        if limit is not None:
            min_interval = reset_secs / max(int(limit), 1)
            if min_interval >= 1.0:
                logger.debug("Pacing: %.1fs delay (%s remaining)", min_interval, remaining)
                await asyncio.sleep(min_interval)


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Make an HTTP request with automatic retry on 429 Too Many Requests."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = await client.request(method, url, **kwargs)

        if resp.status_code != 429:
            await _respect_rate_limit(resp)
            resp.raise_for_status()
            return resp

        # 429 — extract wait time from headers or use exponential backoff
        reset = resp.headers.get("ratelimit-reset") or resp.headers.get("retry-after")
        if reset is not None:
            wait = max(int(reset), 1)
        else:
            wait = min(2 ** attempt, 120)

        logger.warning(
            "Rate limited (429), attempt %d/%d, waiting %ds before retry",
            attempt, MAX_RETRIES, wait,
        )
        await asyncio.sleep(wait)

    # Final attempt — let it raise if still 429
    resp = await client.request(method, url, **kwargs)
    resp.raise_for_status()
    return resp


async def list_all_skills(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Paginate through /api/v1/skills to collect all skill summaries."""
    all_skills: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        params: dict[str, str] = {}
        if cursor:
            params["cursor"] = cursor

        resp = await _request_with_retry(client, "GET", f"{CLAWHUB_BASE}/api/v1/skills", params=params)
        data = resp.json()

        items = data.get("items", [])
        all_skills.extend(items)
        logger.info("Fetched %d skills (total so far: %d)", len(items), len(all_skills))

        cursor = data.get("nextCursor")
        if not cursor or not items:
            break

    return all_skills


async def fetch_skill_detail(client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
    """Fetch full detail for a single skill."""
    resp = await _request_with_retry(client, "GET", f"{CLAWHUB_BASE}/api/v1/skills/{slug}")
    return resp.json()


async def download_skill_zip(client: httpx.AsyncClient, slug: str) -> bytes:
    """Download the full skill zip archive."""
    resp = await _request_with_retry(
        client, "GET", CLAWHUB_DOWNLOAD,
        params={"slug": slug},
        timeout=60.0,
    )
    return resp.content


def extract_zip(zip_bytes: bytes) -> list[SkillFile]:
    """Extract all files from a skill zip archive.

    Skips the _meta.json file (ClawHub internal metadata).
    """
    files: list[SkillFile] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            path = info.filename
            # Skip ClawHub's internal metadata
            if path == "_meta.json":
                continue
            content = zf.read(path)
            files.append(SkillFile(path=path, size=len(content), content=content))
    return files


async def crawl_skill(
    client: httpx.AsyncClient,
    skill_summary: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> CrawledSkill:
    """Crawl a single skill: fetch detail + download zip with all files."""
    slug = skill_summary["slug"]
    logger.info("Crawling skill: %s", slug)

    if detail is None:
        detail = await fetch_skill_detail(client, slug)
    latest = detail.get("latestVersion") or {}

    # Download the full zip archive (rate-limited at 20 req/60s)
    zip_bytes = await download_skill_zip(client, slug)

    files = extract_zip(zip_bytes)
    logger.info(
        "Downloaded %s: %d files, %d bytes",
        slug, len(files), len(zip_bytes),
    )

    # Extract SKILL.md content
    skill_md: str | None = None
    for f in files:
        if f.path == "SKILL.md":
            skill_md = f.content.decode("utf-8", errors="replace") if f.content else None
            break

    if skill_md is None:
        raise ValueError(f"Skill {slug} has no SKILL.md in its zip archive")

    return CrawledSkill(
        slug=slug,
        display_name=skill_summary.get("displayName", slug),
        summary=skill_summary.get("summary", ""),
        version=latest.get("version", "0.0.1"),
        changelog=latest.get("changelog", ""),
        metadata=detail.get("metadata"),
        files=files,
        skill_md=skill_md,
    )


async def crawl_all(client: httpx.AsyncClient) -> list[CrawledSkill]:
    """Crawl every skill from ClawHub."""
    summaries = await list_all_skills(client)
    skills: list[CrawledSkill] = []
    for summary in summaries:
        try:
            skill = await crawl_skill(client, summary)
            skills.append(skill)
        except Exception:
            logger.exception("Failed to crawl skill %s", summary.get("slug"))
    return skills
