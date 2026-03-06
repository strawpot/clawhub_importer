"""CLI entrypoint for the ClawHub → StrawHub importer."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

import httpx

from .crawler import crawl_skill, fetch_skill_detail, list_all_skills
from .publisher import publish_skill
from .state import DEFAULT_STATE_FILE, load_state, save_state
from .transformer import transform_skill

logger = logging.getLogger(__name__)


def _get_version(summary: dict) -> str:
    """Extract version string from a skill summary."""
    lv = summary.get("latestVersion")
    if isinstance(lv, dict):
        return lv.get("version", "0.0.1")
    # tags.latest fallback
    tags = summary.get("tags", {})
    if isinstance(tags, dict):
        return tags.get("latest", "0.0.1")
    return "0.0.1"


async def run(args: argparse.Namespace) -> int:
    """Main async entrypoint."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    # Resolve target URL
    base_url = args.target or os.environ.get("STRAWHUB_URL", "")
    if not base_url and not args.dry_run:
        logger.error("StrawHub URL required. Set --target or STRAWHUB_URL env var.")
        return 1
    logger.info("Target: %s", base_url)

    token = args.token or os.environ.get("STRAWHUB_TOKEN", "")
    if not token and not args.dry_run:
        logger.error("StrawHub API token required. Set --token or STRAWHUB_TOKEN env var.")
        return 1

    state_path = args.state_file
    state = load_state(state_path)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. List skills (cheap — uses the paginated list API at 120 req/60s)
        if args.slugs:
            logger.info("Fetching metadata for %d specific skills...", len(args.slugs))
            summaries = []
            for slug in args.slugs:
                try:
                    detail = await fetch_skill_detail(client, slug)
                    summaries.append({
                        "slug": slug,
                        "displayName": detail.get("skill", {}).get("displayName", slug),
                        "summary": detail.get("skill", {}).get("summary", ""),
                        "latestVersion": detail.get("latestVersion"),
                        "metadata": detail.get("metadata"),
                        "_detail": detail,  # stash for crawl_skill to avoid re-fetch
                    })
                except Exception:
                    logger.exception("Failed to fetch skill %s", slug)
        else:
            logger.info("Listing all skills from ClawHub...")
            summaries = await list_all_skills(client)

        logger.info("Found %d skills on ClawHub.", len(summaries))

        # 2. Filter BEFORE downloading: compare versions from list API against state
        if not args.force:
            to_download = []
            skipped = 0
            for s in summaries:
                slug = s["slug"]
                version = _get_version(s)
                if state.is_imported(slug, version):
                    skipped += 1
                    logger.debug("Skipping %s v%s (already imported)", slug, version)
                else:
                    reason = "new" if slug not in state.skills else "updated"
                    logger.info("%s: %s (v%s)", reason.capitalize(), slug, version)
                    to_download.append(s)
            if skipped:
                logger.info(
                    "Skipped %d already-imported, %d new/updated to download",
                    skipped, len(to_download),
                )
            if not to_download:
                logger.info("Nothing new to import. Use --force to re-import all.")
                return 0
        else:
            to_download = summaries

        # 3. Stream pipeline: download → transform → publish, one skill at a time
        logger.info("Processing %d skills...", len(to_download))
        succeeded = 0
        failed = 0
        dump_skills = []

        for i, summary in enumerate(to_download, 1):
            slug = summary.get("slug", "?")
            logger.info("[%d/%d] Processing %s...", i, len(to_download), slug)

            # Download
            try:
                skill = await crawl_skill(client, summary, detail=summary.get("_detail"))
            except Exception:
                logger.exception("Failed to download skill %s", slug)
                failed += 1
                continue

            # Transform
            transform_skill(skill)

            # Dump (if requested)
            if args.dump_dir:
                dump_skills.append(skill)

            # Publish
            if args.publish or args.dry_run:
                if args.dry_run:
                    logger.info("[DRY RUN] Would publish: %s (v%s)", skill.slug, skill.version)
                    succeeded += 1
                else:
                    result = await publish_skill(client, skill, token, base_url=base_url)
                    if result.success:
                        succeeded += 1
                        state.mark_imported(skill.slug, skill.version)
                        save_state(state, state_path)
                    else:
                        failed += 1
                        logger.warning("  FAILED: %s — %s", result.slug, result.message)
            else:
                logger.info("  %s (v%s) — %s", skill.slug, skill.version, skill.display_name)

        if args.dump_dir and dump_skills:
            _dump_skills(dump_skills, args.dump_dir)
            logger.info("Dumped %d transformed skills to %s", len(dump_skills), args.dump_dir)

        if args.publish or args.dry_run:
            logger.info("Done. %d succeeded, %d failed.", succeeded, failed)
            return 0 if failed == 0 else 1

    return 0


def _dump_skills(skills: list, dump_dir: str) -> None:
    """Write transformed skills to disk for inspection."""
    os.makedirs(dump_dir, exist_ok=True)
    for skill in skills:
        skill_dir = os.path.join(dump_dir, skill.slug)
        os.makedirs(skill_dir, exist_ok=True)

        meta = {
            "slug": skill.slug,
            "displayName": skill.display_name,
            "version": skill.version,
            "changelog": skill.changelog,
            "files": [f.path for f in skill.files],
            "owner": {
                "handle": skill.owner.handle,
                "githubId": skill.owner.github_id,
                "displayName": skill.owner.display_name,
            } if skill.owner else None,
        }
        with open(os.path.join(skill_dir, "_meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        for sf in skill.files:
            if sf.content is not None:
                file_path = os.path.join(skill_dir, sf.path)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(sf.content)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="clawhub-importer",
        description="Crawl ClawHub skills and import them into StrawHub",
    )
    parser.add_argument(
        "--token",
        help="StrawHub API Bearer token (or set STRAWHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl and transform but don't actually publish",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Actually publish to StrawHub (requires --token)",
    )
    parser.add_argument(
        "--dump-dir",
        help="Directory to dump transformed skills for inspection",
    )
    parser.add_argument(
        "--slugs",
        nargs="*",
        help="Only process specific skill slugs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-import all skills, ignoring previous state",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"Path to import state file (default: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    parser.add_argument(
        "--target",
        help="StrawHub base URL (or set STRAWHUB_URL env var)",
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
