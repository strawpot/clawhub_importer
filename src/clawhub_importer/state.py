"""Track which skills have been imported and at which version."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = ".clawhub_importer_state.json"


@dataclass
class SkillState:
    slug: str
    version: str
    imported_at: str  # ISO 8601 timestamp


@dataclass
class ImportState:
    skills: dict[str, SkillState] = field(default_factory=dict)
    skipped_slugs: set[str] = field(default_factory=set)

    def is_skipped(self, slug: str) -> bool:
        """Check if a slug is permanently skipped (e.g. claimed by another user)."""
        return slug in self.skipped_slugs

    def mark_skipped(self, slug: str) -> None:
        """Mark a slug as permanently skipped."""
        self.skipped_slugs.add(slug)

    def is_imported(self, slug: str, version: str) -> bool:
        """Check if a skill at this exact version was already imported."""
        entry = self.skills.get(slug)
        return entry is not None and entry.version == version

    def is_newer(self, slug: str, version: str) -> bool:
        """Check if the given version is different from what was imported."""
        entry = self.skills.get(slug)
        if entry is None:
            return True  # never imported
        return entry.version != version

    def mark_imported(self, slug: str, version: str) -> None:
        """Record that a skill version was successfully imported."""
        from datetime import datetime, timezone

        self.skills[slug] = SkillState(
            slug=slug,
            version=version,
            imported_at=datetime.now(timezone.utc).isoformat(),
        )

    def summary(self) -> dict[str, int]:
        """Return counts for logging."""
        return {"total_imported": len(self.skills)}


def load_state(path: str) -> ImportState:
    """Load import state from a JSON file."""
    if not os.path.exists(path):
        logger.info("No existing state file at %s, starting fresh", path)
        return ImportState()

    try:
        with open(path) as f:
            data = json.load(f)

        skills: dict[str, SkillState] = {}
        for slug, entry in data.get("skills", {}).items():
            skills[slug] = SkillState(
                slug=entry["slug"],
                version=entry["version"],
                imported_at=entry.get("imported_at", ""),
            )

        skipped = set(data.get("skipped_slugs", []))

        state = ImportState(skills=skills, skipped_slugs=skipped)
        logger.info("Loaded state: %d skills previously imported, %d skipped", len(skills), len(skipped))
        return state
    except Exception:
        logger.exception("Failed to load state from %s, starting fresh", path)
        return ImportState()


def save_state(state: ImportState, path: str) -> None:
    """Save import state to a JSON file."""
    data: dict[str, Any] = {
        "skills": {slug: asdict(s) for slug, s in state.skills.items()},
        "skipped_slugs": sorted(state.skipped_slugs),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    logger.info("Saved state: %d skills tracked", len(state.skills))
