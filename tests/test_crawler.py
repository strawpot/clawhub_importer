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
    SkillOwner,
    extract_zip,
    extract_github_id,
    parse_owner,
    list_all_skills,
    fetch_skill_detail,
    crawl_skill,
    _respect_rate_limit,
    _request_with_retry,
)


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Create an in-memory zip archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- extract_github_id / parse_owner ---

def test_extract_github_id():
    url = "https://avatars.githubusercontent.com/u/71600332?v=4"
    assert extract_github_id(url) == "71600332"


def test_extract_github_id_no_match():
    assert extract_github_id("https://example.com/avatar.png") is None
    assert extract_github_id("") is None


def test_parse_owner():
    data = {
        "handle": "sonerbo",
        "userId": "kn7f03p6pyw9xssr5fy22wtpcd82bh0r",
        "displayName": "sonerbo",
        "image": "https://avatars.githubusercontent.com/u/71600332?v=4",
    }
    owner = parse_owner(data)
    assert owner is not None
    assert owner.handle == "sonerbo"
    assert owner.github_id == "71600332"
    assert owner.display_name == "sonerbo"


def test_parse_owner_none():
    assert parse_owner(None) is None


def test_parse_owner_no_avatar():
    owner = parse_owner({"handle": "test", "displayName": "Test", "image": ""})
    assert owner is not None
    assert owner.github_id == ""


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
        # Create a directory entry (compatible with Python 3.10+)
        dir_info = zipfile.ZipInfo("subdir/")
        zf.writestr(dir_info, "")
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

    detail = {
        "latestVersion": {"version": "2.0.0", "changelog": "updated"},
        "owner": {
            "handle": "testuser",
            "displayName": "Test User",
            "image": "https://avatars.githubusercontent.com/u/12345?v=4",
        },
    }
    summary = {"slug": "test", "displayName": "Test"}

    async with httpx.AsyncClient() as client:
        skill = await crawl_skill(client, summary, detail=detail)

    assert skill.slug == "test"
    assert skill.version == "2.0.0"
    assert skill.skill_md is not None
    assert skill.owner is not None
    assert skill.owner.github_id == "12345"
    assert skill.owner.handle == "testuser"


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


# --- _request_with_retry ---

@respx.mock
async def test_retry_on_429():
    """Should retry after 429 and succeed on the next attempt."""
    route = respx.get(f"{CLAWHUB_BASE}/api/v1/skills/test")
    route.side_effect = [
        httpx.Response(429, headers={"ratelimit-reset": "1"}),
        httpx.Response(200, json={"slug": "test"}),
    ]

    async with httpx.AsyncClient() as client:
        resp = await _request_with_retry(client, "GET", f"{CLAWHUB_BASE}/api/v1/skills/test")

    assert resp.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_retry_on_429_uses_retry_after():
    """Should use retry-after header when ratelimit-reset is missing."""
    route = respx.get(f"{CLAWHUB_BASE}/test")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(200, json={"ok": True}),
    ]

    async with httpx.AsyncClient() as client:
        resp = await _request_with_retry(client, "GET", f"{CLAWHUB_BASE}/test")

    assert resp.status_code == 200


@respx.mock
async def test_no_retry_on_success():
    """Should not retry on successful responses."""
    route = respx.get(f"{CLAWHUB_BASE}/test")
    route.mock(return_value=httpx.Response(200, json={"ok": True}))

    async with httpx.AsyncClient() as client:
        resp = await _request_with_retry(client, "GET", f"{CLAWHUB_BASE}/test")

    assert resp.status_code == 200
    assert route.call_count == 1
