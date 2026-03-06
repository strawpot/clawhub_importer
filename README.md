# clawhub-importer

Crawl every skill from [ClawHub](https://clawhub.ai), migrate `metadata.openclaw` to `metadata.strawpot`, and publish to [StrawHub](https://strawhub.dev).

See [DESIGN.md](DESIGN.md) for detailed architecture.

## Install

```bash
pip install -e .
```

Requires Python 3.10+.

## Usage

### Dry run (crawl + transform, no publish)

```bash
clawhub-importer --dry-run
```

### Dump transformed skills to disk for inspection

```bash
clawhub-importer --dump-dir ./output
```

### Publish to StrawHub

```bash
export STRAWHUB_URL="https://your-strawhub-instance.dev"
export STRAWHUB_TOKEN="your-api-token"
clawhub-importer --publish
```

Or pass the target URL explicitly:

```bash
clawhub-importer --publish --target https://your-strawhub-instance.dev --token your-api-token
```

### Import specific skills only

```bash
clawhub-importer --publish --slugs gridtrx 12306
```

### Force re-import (ignore state)

```bash
clawhub-importer --publish --force
```

### All options

```
clawhub-importer [OPTIONS]

  --target URL         StrawHub base URL (or set STRAWHUB_URL env var)
  --token TOKEN        StrawHub API Bearer token (or set STRAWHUB_TOKEN env var)
  --dry-run            Crawl and transform but don't actually publish
  --publish            Actually publish to StrawHub (requires --token and --target)
  --dump-dir DIR       Directory to dump transformed skills for inspection
  --slugs SLUG [...]   Only process specific skill slugs
  --force              Re-import all skills, ignoring previous state
  --state-file PATH    Path to import state file (default: .clawhub_importer_state.json)
  -v, --verbose        Enable debug logging
```

## Incremental imports

The importer tracks which skills have been imported and at which version in `.clawhub_importer_state.json`. On subsequent runs, it skips unchanged skills and only downloads new or updated ones. Use `--force` to re-import everything.

Skills claimed by another user on StrawHub (400 "You do not own this skill") are permanently added to a `skipped_slugs` list in the state file, so they are never re-downloaded or re-attempted.

## CI / GitHub Actions

A daily import workflow runs via `.github/workflows/import.yml`:

- **06:00 UTC** — imports to the preview environment
- **08:00 UTC** — imports to production

The workflow uses `actions/cache` to persist the state file between runs. Manual dispatch is also supported via `workflow_dispatch` with a target selector (preview or production).

Required repository secrets: `STRAWHUB_PREVIEW_URL`, `STRAWHUB_PREVIEW_TOKEN`, `STRAWHUB_PROD_URL`, `STRAWHUB_PROD_TOKEN`.

## Metadata migration

The importer preserves original `metadata.openclaw` / `metadata.clawdbot` and adds `metadata.strawpot` alongside it:

**Before (ClawHub):**
```yaml
metadata: {"openclaw":{"emoji":"...","requires":{"bins":["node"],"env":["API_KEY"]}}}
```

**After (StrawHub):**
```yaml
metadata:
  openclaw:
    emoji: "..."
    requires:
      bins: [node]
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
```

## Rate limiting

The importer respects ClawHub's rate limits (120 req/60s for list API, 20 req/60s for downloads) by reading response headers and throttling automatically. Filtering by state before downloading avoids wasting the expensive download quota on unchanged skills.

## License

[MIT](LICENSE)
