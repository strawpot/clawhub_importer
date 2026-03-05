# ClawHub Importer - Design Document

## Overview

The ClawHub Importer is a CLI tool that crawls skills from [ClawHub](https://clawhub.ai), transforms their metadata from `openclaw`/`clawdbot` format to `strawpot` format, and publishes them to [StrawHub](https://strawhub.dev). It tracks import state to avoid redundant work on subsequent runs.

## Architecture

```
                    ClawHub API                          StrawHub API
                   (clawhub.ai)                         (strawhub.dev)
                        |                                     ^
                        v                                     |
  +-----------+   +------------+   +--------------+   +------------+
  |  cli.py   |-->| crawler.py |-->|transformer.py|-->|publisher.py|
  | (orchest) |   | (download) |   |  (metadata)  |   |  (upload)  |
  +-----------+   +------------+   +--------------+   +------------+
        |                                                     |
        v                                                     v
  +-----------+                                       +--------------+
  | state.py  |                                       | StrawHub DB  |
  | (tracking)|                                       +--------------+
  +-----------+
```

## Modules

### `cli.py` - Orchestrator

The main entrypoint that coordinates the pipeline:

1. **List skills** from ClawHub (cheap API, 120 req/60s)
2. **Filter by state** - compare versions against local state file, skip already-imported
3. **Download zips** only for new/updated skills (expensive API, 20 req/60s)
4. **Transform** metadata from openclaw to strawpot format
5. **Publish** to StrawHub via multipart POST
6. **Update state** for successfully published skills

Supports `--slugs` for targeted imports, `--force` to re-import all, and `--dump-dir` for local inspection.

### `crawler.py` - ClawHub API Client

Handles all interaction with ClawHub:

- **`list_all_skills()`** - Paginates through `GET /api/v1/skills` to collect all skill summaries. Each summary includes `slug`, `displayName`, `latestVersion.version`, etc.
- **`fetch_skill_detail()`** - Fetches full detail for a single skill via `GET /api/v1/skills/:slug`.
- **`download_skill_zip()`** - Downloads the complete skill archive from ClawHub's Convex backend (`https://wry-manatee-359.convex.site/api/v1/download?slug=`).
- **`extract_zip()`** - Extracts all files from the zip, skipping ClawHub's internal `_meta.json`.
- **`_respect_rate_limit()`** - Adaptive throttling based on `ratelimit-remaining` and `ratelimit-reset` response headers.

**Data models:**
- `SkillFile` - A single file with `path`, `size`, and `content` (bytes).
- `CrawledSkill` - Complete skill data: slug, display name, version, changelog, metadata, files, and SKILL.md content.

### `transformer.py` - Metadata Migration

Converts metadata between formats while preserving originals:

- **`parse_openclaw_metadata()`** - Unwraps the outer `openclaw` or `clawdbot` key from metadata dicts.
- **`build_strawpot_metadata()`** - Converts `requires.bins` to `tools.<name>` with per-platform install hints (brew/apt/winget). Maps known binaries like node, python3, curl, jq, git, ffmpeg, gh.
- **`transform_frontmatter()`** - Parses SKILL.md frontmatter (handles both inline JSON and multi-line YAML), adds `metadata.strawpot` alongside the original `metadata.openclaw`/`metadata.clawdbot`.
- **`_parse_frontmatter_yaml()`** - Pre-processes inline JSON in metadata lines before YAML parsing, since ClawHub skills use both formats.

**Key design decision:** Original metadata is preserved. The transformer adds `metadata.strawpot` as a sibling to `metadata.openclaw`/`metadata.clawdbot`, not a replacement.

### `publisher.py` - StrawHub API Client

Publishes transformed skills via `POST /api/v1/skills` (multipart/form-data):

- Sends `slug`, `displayName`, `version`, `changelog`, and file uploads
- Bearer token auth via `--token` flag or `STRAWHUB_TOKEN` env var
- Supports three targets: production (`strawhub.dev`), preview (`preview.strawhub.dev`), local (`localhost:4175`)

**Data model:**
- `PublishResult` - Tracks per-skill publish outcome: slug, success, status code, message.

### `state.py` - Import State Tracking

Persists import progress to `.clawhub_importer_state.json`:

- **`ImportState`** - Tracks `slug -> SkillState(slug, version, imported_at)`.
- **`is_imported(slug, version)`** - Returns true if this exact version was already imported.
- **`mark_imported(slug, version)`** - Records a successful import with ISO 8601 timestamp.

State is checked *before* downloading zips, so re-runs only download new or updated skills. This is critical because the download API is rate-limited to 20 req/60s.

## Rate Limiting Strategy

ClawHub enforces two rate limits:

| API | Limit | Used for |
|-----|-------|----------|
| List/Detail | 120 req / 60s | Listing skills, fetching metadata |
| Download | 20 req / 60s | Downloading skill zip archives |

The importer reads `ratelimit-remaining` and `ratelimit-reset` headers from every response:
- **remaining <= 1**: Sleep for the full reset window
- **remaining <= 5**: Spread remaining requests across the reset window

For large imports (1000+ skills), the download API is the bottleneck. Filtering by state before downloading is essential to avoid wasting quota on unchanged skills.

## Publish Targets

| Flag | URL | Use case |
|------|-----|----------|
| *(default)* | `https://strawhub.dev` | Production |
| `--preview` | `https://preview.strawhub.dev` | Staging/QA |
| `--local` | `http://localhost:4175` | Local development |

## Metadata Format Mapping

### Input (ClawHub)

```yaml
# Inline JSON style
metadata: {"openclaw":{"emoji":"...","requires":{"bins":["node","curl"],"env":["API_KEY"]}}}

# Or multi-line YAML style (clawdbot)
metadata:
  clawdbot:
    requires:
      bins:
        - python3
```

### Output (StrawHub)

The original metadata is preserved, with `strawpot` added alongside:

```yaml
metadata:
  openclaw:
    emoji: "..."
    requires:
      bins: [node, curl]
      env: [API_KEY]
  strawpot:
    dependencies: []
    tools:
      node:
        description: "Required binary: node"
        install:
          macos: brew install node
          linux: apt install nodejs
          windows: winget install OpenJS.NodeJS
      curl:
        description: "Required binary: curl"
        install:
          macos: brew install curl
          linux: apt install curl
```

## State File Format

```json
{
  "skills": {
    "my-skill": {
      "slug": "my-skill",
      "version": "1.2.0",
      "imported_at": "2026-03-05T12:00:00+00:00"
    }
  }
}
```
