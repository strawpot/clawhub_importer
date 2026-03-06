"""Tests for metadata transformation."""

import yaml

from clawhub_importer.crawler import CrawledSkill, SkillFile, SkillOwner
from clawhub_importer.transformer import (
    build_strawpot_metadata,
    build_import_metadata,
    parse_openclaw_metadata,
    transform_frontmatter,
    transform_skill,
    _extract_metadata,
)


# --- parse_openclaw_metadata ---

def test_parse_openclaw_unwraps():
    raw = {"openclaw": {"emoji": "x", "requires": {"bins": ["node"]}}}
    result = parse_openclaw_metadata(raw)
    assert result == {"emoji": "x", "requires": {"bins": ["node"]}}


def test_parse_clawdbot_unwraps():
    raw = {"clawdbot": {"requires": {"bins": ["python3"]}}}
    result = parse_openclaw_metadata(raw)
    assert result == {"requires": {"bins": ["python3"]}}


def test_parse_empty():
    assert parse_openclaw_metadata(None) == {}
    assert parse_openclaw_metadata({}) == {}


def test_parse_passthrough():
    raw = {"requires": {"bins": ["curl"]}}
    assert parse_openclaw_metadata(raw) == raw


# --- build_strawpot_metadata ---

def test_build_strawpot_with_bins():
    openclaw = {"requires": {"bins": ["node", "curl"]}}
    result = build_strawpot_metadata(openclaw)

    assert result["dependencies"] == []
    assert "node" in result["tools"]
    assert "curl" in result["tools"]
    assert "install" in result["tools"]["node"]
    assert result["tools"]["node"]["install"]["macos"] == "brew install node"


def test_build_strawpot_unknown_bin():
    openclaw = {"requires": {"bins": ["my-custom-tool"]}}
    result = build_strawpot_metadata(openclaw)

    assert "my-custom-tool" in result["tools"]
    assert "install" not in result["tools"]["my-custom-tool"]
    assert result["tools"]["my-custom-tool"]["description"] == "Required binary: my-custom-tool"


def test_build_strawpot_no_bins():
    result = build_strawpot_metadata({})
    assert result == {"dependencies": []}
    assert "tools" not in result


# --- build_import_metadata ---

def test_build_import_metadata_with_owner():
    owner = SkillOwner(handle="alice", github_id="12345", display_name="Alice", avatar_url="")
    result = build_import_metadata(owner)
    assert result["source"] == "clawhub"
    assert result["originalOwner"]["handle"] == "alice"
    assert result["originalOwner"]["githubId"] == "12345"


def test_build_import_metadata_no_owner():
    result = build_import_metadata(None)
    assert result["source"] == "clawhub"
    assert "originalOwner" not in result


def test_build_import_metadata_no_github_id():
    owner = SkillOwner(handle="bob", github_id="", display_name="Bob", avatar_url="")
    result = build_import_metadata(owner)
    assert result["source"] == "clawhub"
    assert "originalOwner" not in result


# --- transform_frontmatter ---

def test_transform_inline_json_metadata():
    skill_md = '---\nname: test-skill\nmetadata: {"openclaw":{"requires":{"bins":["node"]}}}\n---\n# Hello'
    openclaw = {"requires": {"bins": ["node"]}}

    result = transform_frontmatter(skill_md, openclaw)

    assert "---" in result
    assert "# Hello" in result
    # Parse the output frontmatter
    fm_text = result.split("---")[1]
    fm = yaml.safe_load(fm_text)
    assert "openclaw" in fm["metadata"]
    assert "strawpot" in fm["metadata"]
    assert "node" in fm["metadata"]["strawpot"]["tools"]


def test_transform_multiline_yaml_metadata():
    skill_md = """---
name: test-skill
metadata:
  clawdbot:
    requires:
      bins:
        - python3
---
# Body"""
    openclaw = {"requires": {"bins": ["python3"]}}

    result = transform_frontmatter(skill_md, openclaw)
    fm_text = result.split("---")[1]
    fm = yaml.safe_load(fm_text)
    assert "clawdbot" in fm["metadata"]
    assert "strawpot" in fm["metadata"]


def test_transform_frontmatter_no_import_in_frontmatter():
    """_import metadata should NOT be in SKILL.md frontmatter (sent as separate form field)."""
    skill_md = '---\nname: test\nmetadata: {"openclaw":{"requires":{"bins":["node"]}}}\n---\n# Hi'
    openclaw = {"requires": {"bins": ["node"]}}

    result = transform_frontmatter(skill_md, openclaw)
    fm_text = result.split("---")[1]
    fm = yaml.safe_load(fm_text)
    assert "_import" not in fm["metadata"]
    assert "strawpot" in fm["metadata"]


def test_transform_no_frontmatter():
    skill_md = "# Just a markdown file"
    result = transform_frontmatter(skill_md, {})
    assert result == skill_md


def test_transform_no_metadata_key():
    skill_md = "---\nname: test\n---\n# Body"
    result = transform_frontmatter(skill_md, {})
    fm_text = result.split("---")[1]
    fm = yaml.safe_load(fm_text)
    assert "strawpot" in fm["metadata"]


# --- transform_skill ---

def test_transform_skill_updates_file_content():
    skill_md = '---\nname: test\nmetadata: {"openclaw":{"requires":{"bins":["jq"]}}}\n---\n# Hi'
    skill = CrawledSkill(
        slug="test",
        display_name="Test",
        summary="A test",
        version="1.0.0",
        changelog="",
        metadata=None,
        files=[SkillFile(path="SKILL.md", size=len(skill_md), content=skill_md.encode())],
        skill_md=skill_md,
    )

    transform_skill(skill)

    updated_content = skill.files[0].content.decode()
    assert "strawpot" in updated_content
    assert "jq" in updated_content
    assert skill.files[0].size == len(skill.files[0].content)


def test_transform_skill_fallback_to_skill_metadata():
    skill_md = "---\nname: test\n---\n# Hi"
    skill = CrawledSkill(
        slug="test",
        display_name="Test",
        summary="",
        version="1.0.0",
        changelog="",
        metadata={"openclaw": {"requires": {"bins": ["git"]}}},
        files=[SkillFile(path="SKILL.md", size=len(skill_md), content=skill_md.encode())],
        skill_md=skill_md,
    )

    transform_skill(skill)

    updated_content = skill.files[0].content.decode()
    assert "strawpot" in updated_content
    assert "git" in updated_content


# --- _extract_metadata ---

def test_extract_metadata_inline_json():
    md = '---\nmetadata: {"openclaw":{"emoji":"x"}}\n---\n# Body'
    result = _extract_metadata(md)
    assert "openclaw" in result


def test_extract_metadata_no_frontmatter():
    assert _extract_metadata("# Just markdown") == {}
