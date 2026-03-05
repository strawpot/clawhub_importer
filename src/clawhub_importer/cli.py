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
from .publisher import STRAWHUB_TARGETS, publish_all
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

    # Resolve target
    if args.local:
        target = "local"
    elif args.preview:
        target = "preview"
    else:
        target = "production"
    base_url = STRAWHUB_TARGETS[target]
    logger.info("Target: %s (%s)", target, base_url)

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

        # 3. Download zips only for new/updated skills (expensive — 20 req/60s)
        logger.info("Downloading %d skills...", len(to_download))
        skills = []
        for summary in to_download:
            try:
                skill = await crawl_skill(client, summary, detail=summary.get("_detail"))
                skills.append(skill)
            except Exception:
                logger.exception("Failed to download skill %s", summary.get("slug"))

        logger.info("Downloaded %d skills.", len(skills))

        # 4. Transform
        logger.info("Transforming metadata: openclaw → strawpot...")
        for skill in skills:
            transform_skill(skill)

        # 5. Dump (if requested)
        if args.dump_dir:
            _dump_skills(skills, args.dump_dir)
            logger.info("Dumped transformed skills to %s", args.dump_dir)
            if not args.publish:
                return 0

        # 6. Publish
        if args.publish or args.dry_run:
            logger.info(
                "%s %d skills to StrawHub...",
                "Publishing" if not args.dry_run else "[DRY RUN] Would publish",
                len(skills),
            )
            results = await publish_all(client, skills, token, dry_run=args.dry_run, base_url=base_url)

            succeeded = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            logger.info("Done. %d succeeded, %d failed.", succeeded, failed)

            # Update state for successfully published skills
            for r in results:
                if r.success:
                    matching = next((s for s in skills if s.slug == r.slug), None)
                    if matching and not args.dry_run:
                        state.mark_imported(matching.slug, matching.version)

            for r in results:
                if not r.success:
                    logger.warning("  FAILED: %s — %s", r.slug, r.message)

            # Save state after publish (even if some failed)
            if not args.dry_run:
                save_state(state, state_path)

            return 0 if failed == 0 else 1

        # Default: just list what was crawled
        logger.info("Skills to import (use --publish or --dry-run to push to StrawHub):")
        for s in skills:
            logger.info("  %s (v%s) — %s", s.slug, s.version, s.display_name)

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

    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--local",
        action="store_true",
        help=f"Publish to local dev server ({STRAWHUB_TARGETS['local']})",
    )
    target_group.add_argument(
        "--preview",
        action="store_true",
        help=f"Publish to preview server ({STRAWHUB_TARGETS['preview']})",
    )

    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
