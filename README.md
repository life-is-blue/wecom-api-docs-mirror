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
- Each `<id>.md` has a same-named `<id>.html` alongside it: the *raw*
  article-body HTML (just `div.ep-doc-area-cherry`'s contents, not the full
  page -- every page also embeds the entire site nav tree, which would
  otherwise balloon a per-doc save to >1.5MB with no per-doc information in
  the extra bytes) captured before any conversion, so the Markdown can be
  diffed against exactly what the site sent without a live re-fetch.
  Not backfilled for docs mirrored before this existed (2026-08-21) --
  those simply lack an `.html` file until they're naturally re-fetched.
- Each mirrored file keeps source metadata (`doc_id`, `section`, `url`,
  `sha256`, `html_sha256`, `converter_version`, `first_seen_at`,
  `last_verified_at`) in `docs/docs_manifest.json`.

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
4. writes `docs/<output_subdir>/<id>.md`, its raw-HTML companion
   `docs/<output_subdir>/<id>.html`, and `docs/docs_manifest.json`.

A doc id that disappears from discovery is **not** deleted immediately — it
has to be missing for several consecutive runs first (`missing_since` /
`missing_run_count` in the manifest), so a single bad crawl can't wipe out
a good local mirror. A discovery-time drop or missing-sentinel check aborts
entirely without touching `docs/` (see `CircuitBreaker` in the script).

## Resumable, checkpointed syncs

Empirically (2026-08-20), even a polite per-request delay (1.5–3s) got the
fetcher CAPTCHA-blocked by WeCom's anti-bot gate after ~24 requests. Pulled
the delay back hard (8–15s) in response, which means a full sync over ~700+
docs can take hours — and may still get blocked mid-way, since we don't
actually know the site's exact tolerance. The fetcher is built around that
reality rather than assuming a sync completes in one shot:

- **Checkpointed as it goes**: after every doc, `docs/sync_progress.json`
  (per source: which doc ids are done, their manifest entries so far, which
  failed) is rewritten atomically. `docs/docs_manifest.json` and each `.md`
  file are also written via atomic temp-file-then-rename, so a killed
  process never leaves a half-written file behind.
- **`--resume` continues instead of restarting**: skips doc ids already
  checkpointed, so a second attempt doesn't re-spend request budget on docs
  it already has. It's a no-op (identical to a fresh run) when there's no
  matching checkpoint, so it's safe to pass unconditionally — both CI
  workflows always pass it.
- **A failure-rate spike mid-sync pauses, it doesn't fail the job**: if the
  recent per-doc failure rate crosses the threshold (the empirical shape of
  a CAPTCHA cascade — a run of successes followed by a run of failures), the
  fetcher stops early, exits 0 in tolerant mode, and leaves the checkpoint
  in place for the next `--resume` run to pick up. `STRICT_FETCH=1` still
  treats this as a failure, since that mode means "tell me about any
  imperfection."
- **A per-run cap on successful fetches, independent of the breaker above**:
  the failure-rate breaker is reactive — it only stops a run after enough
  recent attempts have already failed. A real run (2026-08-21, GitHub
  Actions IP) got to 199 successful fetches / 201 total attempts before it
  tripped, uncomfortably close to WeCom's reported harder-block threshold
  around 210 requests; a run getting slightly luckier on when failures
  start could blow past that before the breaker has enough failed attempts
  in its window to react. `MAX_SUCCESSFUL_FETCHES_PER_RUN` (150) is a
  proactive, unconditional stop on top of it — once *this process* has
  fetched that many docs successfully, it checkpoints and exits 0
  regardless of failure rate, even under `STRICT_FETCH=1` (this is
  deliberate pacing, not a signal of a site-side problem). Resets every
  `--resume` invocation, so it paces progress across runs rather than
  capping the backlog itself.
- **`docs/sync_progress.json` is committed, not gitignored**: CI runners are
  ephemeral, so a checkpoint that only lives on local disk wouldn't survive
  between one day's cron run and the next. Its presence in the repo means a
  sync is mid-flight, and `docs_manifest.json` lags behind the `.md` files
  it should describe until the source finishes a full pass over every
  discovered doc id and the checkpoint is cleared -- on a fresh clone of
  this repo, that means **`docs/docs_manifest.json` may not exist at all
  yet**, even though `docs/<output_subdir>/*.md` already has real content.
  Check for `docs/sync_progress.json` first if the manifest looks missing
  or stale; its presence explains why.
- **Deletions still wait for a fully completed pass**: the missing-doc /
  delayed-deletion bookkeeping only runs after every discovered doc id has
  been attempted (possibly across several `--resume` runs), never mid-sync.

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
- `.cnb.yml`: CNB scheduled + manual sync workflow (offline tests run on every push/PR; live fetch only on cron / manual trigger) -- **not currently wired to a live CNB trigger**; GitHub Actions is the only automation actually running in production right now
- `.cnb/web_trigger.yml`: CNB page button configuration
- `.github/workflows/update-docs.yml`: same verify/sync split as `.cnb.yml`, plus a third `debug-capture-raw-html` job (manual-only, see "Debugging / spot-checking conversion quality" below) that CNB doesn't have

## Run locally

```bash
pip install -r scripts/requirements-dev.txt
pytest tests/                                # offline, no network
python3 scripts/fetch_wecom_docs.py --resume # live fetch, continues any checkpoint
```

Other useful flags:

```bash
STRICT_FETCH=1 python3 scripts/fetch_wecom_docs.py         # fail on any per-page fetch error
python3 scripts/fetch_wecom_docs.py --limit 20              # quick low-traffic smoke test;
                                                              # see --help, not meant for a real sync
```

## Automation

- **GitHub Actions is the only live automation right now**: scheduled sync
  daily via `.github/workflows/update-docs.yml`, plus manual
  `workflow_dispatch` for urgent refreshes.
- `.cnb.yml` defines the equivalent CNB scheduled sync
  (`main -> "crontab: 0 0 * * *"`) and manual sync button (**Sync WeCom API
  Docs**), but CNB isn't actually connected to a live trigger yet — treat
  those as "ready to enable," not "currently running."
- Push / PR validation on `main` runs the offline test suite only
  (`scripts/**`, `config/**`, `tests/**`, CI files) — it never hits the live
  site, to avoid tripping WeCom's anti-bot protections on every commit.
- Both sync workflows always pass `--resume` (see "Resumable, checkpointed
  syncs" above) and commit whatever changed in `docs/` — including a
  still-in-progress `sync_progress.json` when a run paused mid-sync, not
  just a fully completed manifest.
- The `sync` job's GitHub Actions concurrency group has
  `cancel-in-progress: false`: the checkpoint is only durable once that
  job's commit step runs, so a second trigger (e.g. a manual dispatch
  racing the daily cron) queues behind an in-flight sync instead of
  cancelling it and silently losing that run's progress.

### Debugging / spot-checking conversion quality

`scripts/debug_capture_raw_html.py` fetches a small, explicit list of doc
ids' *raw* HTML (no conversion) into `raw_html/<section path>/<id>.html`, so
it can be diffed against the corresponding `docs/wecom/<id>.md` by hand.
Not part of the daily sync -- trigger it via the `debug-capture-raw-html`
GitHub Actions job (`workflow_dispatch` with the `debug_doc_ids` input, e.g.
`97322,93651`), then download the `raw-html-capture` artifact. Capped at 15
ids per run and kept well under the site's empirical anti-bot request
budget; output is gitignored (`/raw_html/`), it's a local/CI scratch aid,
not part of the public mirror.

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
