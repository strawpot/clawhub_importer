"""Tests for crawler module."""

import io
import zipfile

import httpx
import pytest
import respx

from clawhub_importer.crawler import (
    CLAWHUB_BASE,
    CLAWHUB_DOWNLOAD,
    CrawledSkill,
    SkillFile,
    extract_zip,
    list_all_skills,
    fetch_skill_detail,
    crawl_skill,
    _respect_rate_limit,
)


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Create an in-memory zip archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- extract_zip ---

def test_extract_zip_basic():
    zip_bytes = _make_zip({
        "SKILL.md": b"# Hello",
        "lib/main.py": b"print('hi')",
    })
    files = extract_zip(zip_bytes)
    assert len(files) == 2
    paths = {f.path for f in files}
    assert "SKILL.md" in paths
    assert "lib/main.py" in paths


def test_extract_zip_skips_meta_json():
    zip_bytes = _make_zip({
        "SKILL.md": b"# Hello",
        "_meta.json": b'{"internal": true}',
    })
    files = extract_zip(zip_bytes)
    assert len(files) == 1
    assert files[0].path == "SKILL.md"


def test_extract_zip_skips_directories():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "# Hi")
        zf.mkdir("subdir/")
    files = extract_zip(buf.getvalue())
    assert len(files) == 1


# --- list_all_skills (with respx mock) ---

@respx.mock
async def test_list_all_skills_single_page():
    respx.get(f"{CLAWHUB_BASE}/api/v1/skills").mock(
        return_value=httpx.Response(200, json={
            "items": [{"slug": "a"}, {"slug": "b"}],
            "nextCursor": None,
        })
    )

    async with httpx.AsyncClient() as client:
        skills = await list_all_skills(client)

    assert len(skills) == 2
    assert skills[0]["slug"] == "a"


@respx.mock
async def test_list_all_skills_pagination():
    route = respx.get(f"{CLAWHUB_BASE}/api/v1/skills")
    route.side_effect = [
        httpx.Response(200, json={
            "items": [{"slug": "a"}],
            "nextCursor": "cursor1",
        }),
        httpx.Response(200, json={
            "items": [{"slug": "b"}],
            "nextCursor": None,
        }),
    ]

    async with httpx.AsyncClient() as client:
        skills = await list_all_skills(client)

    assert len(skills) == 2


# --- fetch_skill_detail ---

@respx.mock
async def test_fetch_skill_detail():
    respx.get(f"{CLAWHUB_BASE}/api/v1/skills/my-skill").mock(
        return_value=httpx.Response(200, json={"slug": "my-skill", "latestVersion": {"version": "1.0.0"}})
    )

    async with httpx.AsyncClient() as client:
        detail = await fetch_skill_detail(client, "my-skill")

    assert detail["slug"] == "my-skill"


# --- crawl_skill ---

@respx.mock
async def test_crawl_skill_with_detail():
    """When detail is provided, crawl_skill should NOT call the detail API."""
    zip_bytes = _make_zip({"SKILL.md": b"---\nname: test\n---\n# Hi"})

    # Only mock the download endpoint (detail should not be called)
    respx.get(CLAWHUB_DOWNLOAD).mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    detail = {"latestVersion": {"version": "2.0.0", "changelog": "updated"}}
    summary = {"slug": "test", "displayName": "Test"}

    async with httpx.AsyncClient() as client:
        skill = await crawl_skill(client, summary, detail=detail)

    assert skill.slug == "test"
    assert skill.version == "2.0.0"
    assert skill.skill_md is not None


@respx.mock
async def test_crawl_skill_no_skill_md():
    """Should raise ValueError if zip has no SKILL.md."""
    zip_bytes = _make_zip({"README.md": b"# Not SKILL.md"})

    respx.get(f"{CLAWHUB_BASE}/api/v1/skills/bad").mock(
        return_value=httpx.Response(200, json={"latestVersion": {"version": "1.0.0"}})
    )
    respx.get(CLAWHUB_DOWNLOAD).mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    summary = {"slug": "bad"}
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="no SKILL.md"):
            await crawl_skill(client, summary)


# --- _respect_rate_limit ---

async def test_rate_limit_no_headers():
    resp = httpx.Response(200)
    await _respect_rate_limit(resp)  # should not raise
