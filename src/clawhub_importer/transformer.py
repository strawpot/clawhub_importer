"""Transform metadata.openclaw / metadata.clawdbot → metadata.strawpot."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

from .crawler import CrawledSkill, SkillOwner

logger = logging.getLogger(__name__)

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def parse_openclaw_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Extract openclaw or clawdbot metadata, unwrapping the outer key."""
    if not raw:
        return {}
    if "openclaw" in raw:
        return raw["openclaw"]
    if "clawdbot" in raw:
        return raw["clawdbot"]
    return raw


# Known install commands for common binaries
_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "node": {
        "macos": "brew install node",
        "linux": "apt install nodejs",
        "windows": "winget install OpenJS.NodeJS",
    },
    "python3": {
        "macos": "brew install python3",
        "linux": "apt install python3",
        "windows": "winget install Python.Python.3",
    },
    "python": {
        "macos": "brew install python3",
        "linux": "apt install python3",
    },
    "curl": {
        "macos": "brew install curl",
        "linux": "apt install curl",
    },
    "jq": {
        "macos": "brew install jq",
        "linux": "apt install jq",
    },
    "git": {
        "macos": "brew install git",
        "linux": "apt install git",
    },
    "ffmpeg": {
        "macos": "brew install ffmpeg",
        "linux": "apt install ffmpeg",
    },
    "cwebp": {
        "macos": "brew install webp",
        "linux": "apt install webp",
    },
    "gh": {
        "macos": "brew install gh",
        "linux": "apt install gh",
        "windows": "winget install GitHub.cli",
    },
}


def build_strawpot_metadata(
    openclaw: dict[str, Any],
    requires_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Convert openclaw/clawdbot metadata to strawpot format.

    Maps:
      requires.bins    → tools.<name> with install hints
      requires_tools   → preserved as-is (agent capability requirements)
    """
    strawpot: dict[str, Any] = {"dependencies": []}
    requires = openclaw.get("requires", {})
    if not isinstance(requires, dict):
        requires = {}

    bins = requires.get("bins", [])
    if bins:
        tools: dict[str, Any] = {}
        for b in bins:
            entry: dict[str, Any] = {"description": f"Required binary: {b}"}
            if b in _INSTALL_HINTS:
                entry["install"] = _INSTALL_HINTS[b]
            tools[b] = entry
        strawpot["tools"] = tools

    return strawpot


def build_import_metadata(owner: SkillOwner | None) -> dict[str, Any]:
    """Build _import metadata for ownership tracking."""
    meta: dict[str, Any] = {"source": "clawhub"}
    if owner and owner.github_id:
        meta["originalOwner"] = {
            "handle": owner.handle,
            "githubId": owner.github_id,
        }
    return meta


def transform_frontmatter(skill_md: str, openclaw_meta: dict[str, Any], slug: str | None = None) -> str:
    """Rewrite SKILL.md frontmatter: replace metadata.openclaw with metadata.strawpot.

    Uses YAML parsing for robust handling of both inline JSON and multi-line YAML metadata.
    If slug is provided, ensures the frontmatter name matches the slug.
    """
    match = FRONTMATTER_RE.match(skill_md)
    if not match:
        return skill_md

    fm_text = match.group(1)
    body = skill_md[match.end():]
    # body starts with \n---\n or just \n
    if body.startswith("\n"):
        body = body[1:]

    # Parse the frontmatter as YAML
    try:
        fm_data = _parse_frontmatter_yaml(fm_text)
    except Exception:
        logger.warning("Failed to parse frontmatter YAML, falling back to line replacement")
        fm_data = {}

    if not isinstance(fm_data, dict):
        fm_data = {}

    # Ensure frontmatter name matches the slug
    if slug and fm_data.get("name") != slug:
        old_name = fm_data.get("name")
        if old_name:
            logger.info("Rewriting frontmatter name %r → %r", old_name, slug)
        else:
            logger.info("Setting frontmatter name to %r", slug)
        fm_data["name"] = slug

    # Extract requires_tools before removing it
    requires_tools = fm_data.get("requires_tools")
    if isinstance(requires_tools, list):
        requires_tools = [str(t) for t in requires_tools]
    else:
        requires_tools = None

    # Add strawpot metadata alongside the original openclaw/clawdbot metadata
    strawpot = build_strawpot_metadata(openclaw_meta, requires_tools=requires_tools)
    existing_meta = fm_data.get("metadata")
    if isinstance(existing_meta, dict):
        # Keep original (openclaw/clawdbot), add strawpot next to it
        existing_meta["strawpot"] = strawpot
    else:
        fm_data["metadata"] = {"strawpot": strawpot}

    # Serialize back to YAML
    new_fm = yaml.dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    # Remove trailing newline from yaml.dump
    new_fm = new_fm.rstrip("\n")

    return f"---\n{new_fm}\n---\n{body}"


def _parse_frontmatter_yaml(fm_text: str) -> dict[str, Any]:
    """Parse frontmatter text, handling inline JSON in metadata lines."""
    # Pre-process: if metadata line has inline JSON, convert to proper YAML
    lines = fm_text.split("\n")
    processed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("metadata:"):
            rest = stripped[len("metadata:"):].strip()
            if rest.startswith("{"):
                # Inline JSON — parse it and emit as YAML
                try:
                    meta_dict = json.loads(rest)
                    processed.append("metadata:")
                    meta_yaml = yaml.dump(meta_dict, default_flow_style=False, allow_unicode=True)
                    for meta_line in meta_yaml.rstrip("\n").split("\n"):
                        processed.append(f"  {meta_line}")
                    continue
                except json.JSONDecodeError:
                    pass
        processed.append(line)

    return yaml.safe_load("\n".join(processed)) or {}


def transform_skill(skill: CrawledSkill) -> CrawledSkill:
    """Transform a crawled skill's metadata from openclaw to strawpot format."""
    raw_meta = _extract_metadata(skill.skill_md) if skill.skill_md else {}
    openclaw_meta = parse_openclaw_metadata(raw_meta)

    if not openclaw_meta:
        openclaw_meta = parse_openclaw_metadata(skill.metadata)

    if skill.skill_md:
        skill.skill_md = transform_frontmatter(skill.skill_md, openclaw_meta, slug=skill.slug)
        for f in skill.files:
            if f.path == "SKILL.md" and skill.skill_md:
                f.content = skill.skill_md.encode("utf-8")
                f.size = len(f.content)

    return skill


def _extract_metadata(skill_md: str) -> dict[str, Any]:
    """Extract the metadata dict from frontmatter."""
    match = FRONTMATTER_RE.match(skill_md)
    if not match:
        return {}

    fm_text = match.group(1)
    try:
        fm_data = _parse_frontmatter_yaml(fm_text)
    except Exception:
        return {}

    if isinstance(fm_data, dict) and "metadata" in fm_data:
        meta = fm_data["metadata"]
        return meta if isinstance(meta, dict) else {}
    return {}
