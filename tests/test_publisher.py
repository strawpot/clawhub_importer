"""Tests for publisher module."""

import httpx
import respx

from clawhub_importer.crawler import CrawledSkill, SkillFile
from clawhub_importer.publisher import publish_skill, publish_all, _build_changelog

TEST_BASE_URL = "https://test.example.com"


def _make_skill(slug: str = "test-skill") -> CrawledSkill:
    return CrawledSkill(
        slug=slug,
        display_name="Test Skill",
        summary="A test",
        version="1.0.0",
        changelog="Initial",
        metadata=None,
        files=[
            SkillFile(path="SKILL.md", size=5, content=b"# Hi"),
            SkillFile(path="lib/main.py", size=10, content=b"print('hi')"),
        ],
    )


# --- publish_skill ---

@respx.mock
async def test_publish_skill_success():
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(201, json={"slug": "test-skill"})
    )

    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, _make_skill(), "fake-token", base_url=TEST_BASE_URL)

    assert result.success
    assert result.status_code == 201


@respx.mock
async def test_publish_skill_failure():
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(400, text="Bad request")
    )

    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, _make_skill(), "fake-token", base_url=TEST_BASE_URL)

    assert not result.success
    assert result.status_code == 400


@respx.mock
async def test_publish_skill_custom_base_url():
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )

    async with httpx.AsyncClient() as client:
        result = await publish_skill(
            client, _make_skill(), "fake-token",
            base_url=TEST_BASE_URL,
        )

    assert result.success


async def test_publish_skill_no_files():
    skill = CrawledSkill(
        slug="empty", display_name="Empty", summary="", version="1.0.0",
        changelog="", metadata=None, files=[],
    )
    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, skill, "token", base_url=TEST_BASE_URL)

    assert not result.success
    assert "No files" in result.message


async def test_publish_skill_missing_skill_md():
    skill = CrawledSkill(
        slug="no-md", display_name="No MD", summary="", version="1.0.0",
        changelog="", metadata=None,
        files=[SkillFile(path="other.txt", size=3, content=b"hi!")],
    )
    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, skill, "token", base_url=TEST_BASE_URL)

    assert not result.success
    assert "Missing SKILL.md" in result.message


# --- publish_all ---

async def test_publish_all_dry_run():
    skills = [_make_skill("a"), _make_skill("b")]

    async with httpx.AsyncClient() as client:
        results = await publish_all(client, skills, "token", dry_run=True)

    assert len(results) == 2
    assert all(r.success for r in results)
    assert all(r.message == "dry-run" for r in results)


@respx.mock
async def test_publish_all_with_preview_target():
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )

    async with httpx.AsyncClient() as client:
        results = await publish_all(
            client, [_make_skill()], "token",
            base_url=TEST_BASE_URL,
        )

    assert len(results) == 1
    assert results[0].success


# --- _build_changelog ---

def test_build_changelog_with_existing():
    skill = _make_skill()
    skill.changelog = "Fixed a bug"
    result = _build_changelog(skill)
    assert "Fixed a bug" in result
    assert "Imported from ClawHub" in result
    assert "MIT License" in result


def test_build_changelog_empty():
    skill = _make_skill()
    skill.changelog = ""
    result = _build_changelog(skill)
    assert "Imported from ClawHub" in result
    assert "MIT License" in result
    assert "clawhub.ai" in result


# --- rate limit retry for publish ---

@respx.mock
async def test_publish_skill_retries_on_429():
    """Publisher should retry on 429 from StrawHub."""
    route = respx.post(f"{TEST_BASE_URL}/api/v1/skills")
    route.side_effect = [
        httpx.Response(429, headers={"ratelimit-reset": "1"}),
        httpx.Response(201, json={"slug": "test-skill"}),
    ]

    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, _make_skill(), "fake-token", base_url=TEST_BASE_URL)

    assert result.success
    assert result.status_code == 201
    assert route.call_count == 2


# --- duplicate version handling ---

@respx.mock
async def test_publish_skill_duplicate_version_treated_as_success():
    """A 400 with 'Version X already exists' should be treated as success for idempotent imports."""
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(400, json={"error": "Version 1.0.0 already exists"})
    )

    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, _make_skill(), "fake-token", base_url=TEST_BASE_URL)

    assert result.success
    assert result.status_code == 400
    assert result.message == "already exists"


@respx.mock
async def test_publish_skill_claimed_treated_as_success():
    """A 400 with 'You do not own this skill' should be treated as success to skip on future runs."""
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(400, json={"error": "You do not own this skill"})
    )

    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, _make_skill(), "fake-token", base_url=TEST_BASE_URL)

    assert not result.success
    assert result.status_code == 400
    assert result.message == "claimed"


@respx.mock
async def test_publish_skill_other_400_not_treated_as_success():
    """A 400 that is NOT about duplicate versions should still fail."""
    respx.post(f"{TEST_BASE_URL}/api/v1/skills").mock(
        return_value=httpx.Response(400, json={"error": "slug is invalid"})
    )

    async with httpx.AsyncClient() as client:
        result = await publish_skill(client, _make_skill(), "fake-token", base_url=TEST_BASE_URL)

    assert not result.success
    assert result.status_code == 400
