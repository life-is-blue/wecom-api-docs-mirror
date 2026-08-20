# WeCom API Docs Mirror

Local mirror for official **企业微信 (WeCom) 开发者中心** docs
(`https://developer.work.weixin.qq.com/document/...`), converted to Markdown
for agent-oriented document ingestion and retrieval.

## Open-Source Positioning

- Canonical source remains the official WeCom developer docs site
  (`https://developer.work.weixin.qq.com/document/...`). Where this mirror
  and the official site disagree, the official site is authoritative.
- Copyright in the mirrored content belongs to Tencent / WeCom. This
  repository redistributes it only as a retrieval mirror (检索镜像) for
  agent tooling, per `robots.txt`'s explicit `Allow: /document` (checked
  again on every sync run; see `scripts/fetch_wecom_docs.py`).
- **The Markdown here is a lossy conversion**, not an official export. The
  site does not publish raw Markdown or a machine-readable doc index (unlike
  some doc sites that expose an `llms.txt`); every page is server-rendered
  HTML produced by Tencent's open-source `cherry-markdown` editor, and this
  mirror converts that rendered HTML back to Markdown. Formatting, complex
  tables, and code blocks may differ slightly from the original page.
- Each mirrored file keeps source metadata (`doc_id`, `section`, `url`,
  `sha256`, `converter_version`, `first_seen_at`, `last_verified_at`) in
  `docs/docs_manifest.json`.

## How discovery works

`developer.work.weixin.qq.com` has no `llms.txt` or Markdown endpoints, but
every `/document/path/<id>` page embeds the site's full left-nav tree in its
HTML (`div.ep-doc-select` -> nested `div.ep-doc-wrap[level=N]`). One "seed"
page therefore doubles as a complete site index — the same role `llms.txt`
plays for Markdown-native doc sites.

`scripts/fetch_wecom_docs.py`:

1. fetches the configured `seed_path` and recursively parses its embedded
   nav tree into `{doc_id: (sections, label)}`,
2. validates the result isn't suspiciously smaller than the last run and
   that configured "sentinel" doc ids are present (protects against a bad
   response or a parser regression being mistaken for mass doc removal),
3. fetches each `/document/path/<id>` page, extracts the
   `div.ep-doc-area-cherry` article body, and converts it to Markdown
   (code blocks, tables, images, and cross-doc links get specific handling
   — see the module docstring in `scripts/fetch_wecom_docs.py`),
4. writes `docs/<output_subdir>/<id>.md` and `docs/docs_manifest.json`.

A doc id that disappears from discovery is **not** deleted immediately — it
has to be missing for several consecutive runs first (`missing_since` /
`missing_run_count` in the manifest), so a single bad crawl can't wipe out
a good local mirror. A run whose failure rate or discovery drop exceeds a
threshold aborts entirely without touching `docs/` (see `CircuitBreaker` in
the script).

## Sources

Configured in `config/sources.json`:
- `https://developer.work.weixin.qq.com` (`seed_path=/document/path/90664`,
  doc pages under `/document/path/<id>`)

## Layout

- `scripts/fetch_wecom_docs.py`: fetcher + HTML→Markdown converter + manifest generator
- `scripts/fixtures/`: saved HTML samples used by the offline test suite
- `tests/test_fetch_wecom_docs.py`: offline unit tests (no network access)
- `config/sources.json`: source definitions
- `docs/`: mirrored markdown content and manifest
- `.cnb.yml`: CNB scheduled + manual sync workflow (offline tests run on every push/PR; live fetch only on cron / manual trigger)
- `.cnb/web_trigger.yml`: CNB page button configuration
- `.github/workflows/update-docs.yml`: same split, for GitHub Actions

## Run locally

```bash
pip install -r scripts/requirements-dev.txt
pytest tests/                       # offline, no network
python3 scripts/fetch_wecom_docs.py # live fetch
```

Optional strict mode (fails the run on any per-page fetch error):

```bash
STRICT_FETCH=1 python3 scripts/fetch_wecom_docs.py
```

## Automation

- CNB scheduled sync daily: `main -> "crontab: 0 0 * * *"`
- CNB manual sync button on `main` branch page: **Sync WeCom API Docs**
- GitHub Actions scheduled sync daily: `.github/workflows/update-docs.yml`
- Push / PR validation on `main` runs the offline test suite only
  (`scripts/**`, `config/**`, `tests/**`, CI files) — it never hits the live
  site, to avoid tripping WeCom's anti-bot protections on every commit.

## Notes

- Source content remains property of Tencent / WeCom.
- This repository stores mirrored, converted copies to support
  machine-readable indexing and agent retrieval workflows.
- Official docs should always be treated as the source of truth when
  discrepancies appear.
- `robots.txt` permission is a crawling signal, not a copyright license —
  if you fork this for another target site, re-check both before syncing.

## Roadmap

1. Keep a stable daily sync baseline with the safe-deletion / circuit-breaker
   semantics in place.
2. Preserve manual sync triggers for urgent refreshes.
3. Widen conversion coverage (image `srcset`, admonition-style blocks) as
   real pages surface cases the current converter doesn't handle well.
4. Keep CNB and GitHub Actions workflows aligned with the same daily sync
   policy.
