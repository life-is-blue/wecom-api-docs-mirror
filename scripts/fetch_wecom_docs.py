#!/usr/bin/env python3
"""Fetch WeCom (企业微信) developer docs and mirror them as Markdown.

`developer.work.weixin.qq.com` does not publish a machine-readable doc index
or raw Markdown endpoints (unlike the Antigravity site this mirror pattern
was copied from). Every `/document/path/<id>` page is server-rendered HTML:

- The left-nav tree embedded in *any* doc page lists the whole site
  (`.ep-doc-select` -> nested `.ep-doc-wrap[level=N]` divs), so one seed page
  acts as the discovery index (see `discover_doc_pages`).
- The article body lives in `div.ep-doc-area-cherry`, already rendered by
  Tencent's open-source `cherry-markdown` editor. We convert that HTML back
  to Markdown (`html_to_markdown`); this is a lossy, best-effort conversion,
  not a re-export of an original Markdown source.

Because discovery here is "parse a DOM tree we don't control" rather than
agy's "read an official index file", failure modes are wider. This script is
deliberately conservative about applying deletions: see `validate_discovery`
and the `missing_since` handling in `main` for the safeguards against a
parser regression / bad response wiping out a good local mirror.

It's also built to be interrupted: a polite per-request delay still gets
CAPTCHA-blocked by WeCom's anti-bot gate after a couple dozen requests
(empirically observed), so a full sync can take hours and may not finish in
one process lifetime. `docs/sync_progress.json` is checkpointed atomically
after every doc and committed (not gitignored, since CI runners don't
persist state between runs); pass `--resume` to continue from it instead of
re-fetching everything. See "Resumable, checkpointed syncs" in the README.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import ssl
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import certifi
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as html_to_markdown_raw

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
DOCS_ROOT = REPO_ROOT / "docs"
MANIFEST_PATH = DOCS_ROOT / "docs_manifest.json"
# Tracks per-source progress of an in-flight sync so `--resume` can continue
# after a CAPTCHA block / CI timeout / kill without re-fetching docs we
# already have. Deliberately committed to git (not gitignored): CI runners
# are ephemeral, so a checkpoint that doesn't survive in the repo itself
# can't survive between one cron run and the next. Cleared (and its removal
# committed) once a source's sync fully completes.
PROGRESS_PATH = DOCS_ROOT / "sync_progress.json"

# Format version of docs/sync_progress.json and docs_manifest.json
# respectively. Neither file has ever had more than one shape, so this is
# deliberately just a tripwire (load_*() warns and starts fresh on a
# mismatch) rather than a migration framework -- add real migration logic
# only once a version 2 actually exists. A file with no schema_version key
# at all (every real checkpoint/manifest before this was added) is treated
# as the current version, not an error.
PROGRESS_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1

USER_AGENT = (
    "wecom-api-docs-mirror/1.0 "
    "(+https://github.com/search?q=wecom-api-docs-mirror; doc-mirror bot)"
)

CONVERTER_VERSION = "3"

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 1.5
# A 1.5~3s delay still tripped the site's CAPTCHA gate after ~24 requests in
# one run (empirically observed 2026-08-20 -- see README). Pulled back hard
# in response; a full ~735-doc sync at this pace takes on the order of hours,
# which is accepted as the cost of not getting IP-blocked.
REQUEST_DELAY_MIN_SECONDS = 8.0
REQUEST_DELAY_MAX_SECONDS = 15.0

# Discovery is trusted only if it doesn't shrink by more than this fraction
# versus the previous manifest, and every configured sentinel id is present.
DISCOVERY_DROP_THRESHOLD = 0.10
# Per-run fetch failure rate above this trips the circuit breaker, measured
# over the last FAILURE_WINDOW_SIZE attempts *this process* (a true sliding
# window, not a whole-session average -- an average over every attempt since
# --resume started would let one early transient failure trip the breaker
# almost immediately, while also taking many consecutive failures to react
# once a long clean streak has built up a large denominator; a window reacts
# to a real CAPTCHA cascade within a couple of failures either way).
FAILURE_RATE_THRESHOLD = 0.05
FAILURE_WINDOW_SIZE = 30
# The failure-rate breaker above is reactive: it only stops a run *after*
# enough recent attempts have already failed. A real run at the current
# delay (2026-08-21, GitHub Actions IP) got to 199 successful fetches / 201
# total attempts before the breaker tripped -- close enough to WeCom's
# reported harder-block threshold (~210 requests) that a run getting
# slightly luckier on when failures start could blow past it before the
# reactive breaker has enough failed attempts in its window to react. This
# is a proactive, unconditional cap on top of it: stop after this many
# *successful* fetches in this process's own run, regardless of failure
# rate, leaving real margin under both the observed natural stopping point
# and the reported harder threshold. Resets every --resume invocation (see
# result.successful_pages), so it paces progress across runs rather than
# capping the backlog itself.
MAX_SUCCESSFUL_FETCHES_PER_RUN = 150
# A doc must be missing from discovery this many consecutive runs before
# its file is actually deleted.
MISSING_CONFIRM_RUNS = 3

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


@dataclass(frozen=True)
class Source:
    source_id: str
    site_root: str
    seed_path: str
    sentinel_ids: Tuple[str, ...]
    doc_path_prefix: str
    output_subdir: str


@dataclass(frozen=True)
class DocPage:
    doc_id: str
    label: str
    sections: Tuple[str, ...]  # e.g. ("通讯录管理", "成员管理"), one path per discovery
    url: str
    rel_path: str


@dataclass
class FetchOutcome:
    manifest_entry: Optional[Dict] = None
    markdown_text: str = ""
    raw_html_text: str = ""
    failed: bool = False
    error: str = ""


class CircuitBreaker(RuntimeError):
    """Raised to abort a run without touching manifest/docs on disk."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a killed process never leaves a
    half-written file at `path` -- the file is either the old content or the
    fully-new content, never a truncated in-between."""
    tmp_path = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def write_json_atomic(path: Path, data: Dict) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_progress(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt/partial progress file (e.g. process killed mid-write
        # despite the atomic rename, or hand-edited) is not worth resuming
        # from -- treat it as absent and start the source fresh.
        return {}
    if not isinstance(data, dict):
        return {}
    # No schema_version key at all means a checkpoint written before this
    # field existed -- treat that as the current version, not a mismatch.
    version = data.get("schema_version", PROGRESS_SCHEMA_VERSION)
    if version != PROGRESS_SCHEMA_VERSION:
        print(
            f"[WARN] {path} has schema_version={version!r}, expected "
            f"{PROGRESS_SCHEMA_VERSION} -- ignoring and starting fresh"
        )
        return {}
    return {key: value for key, value in data.items() if key != "schema_version"}


def load_sources(config_path: Path) -> List[Source]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    raw_sources = payload.get("sources", [])
    if not raw_sources:
        raise RuntimeError("No sources configured in config/sources.json")

    result: List[Source] = []
    for raw in raw_sources:
        source_id = raw.get("id")
        site_root = raw.get("site_root")
        seed_path = raw.get("seed_path")
        doc_path_prefix = raw.get("doc_path_prefix", "/document/path/")
        output_subdir = raw.get("output_subdir")
        sentinel_ids = tuple(raw.get("sentinel_ids", []))

        if not source_id or not site_root or not seed_path or not output_subdir:
            raise RuntimeError(f"Invalid source entry: {raw}")
        if source_id == "schema_version":
            # save_checkpoint() writes {"schema_version": N, **progress} at
            # the top level of docs/sync_progress.json -- a source literally
            # named this would collide with that key and get silently
            # clobbered into it, corrupting the version tripwire for every
            # source's checkpoint, not just this one.
            raise RuntimeError('source id "schema_version" is reserved, pick a different id')
        if not doc_path_prefix.startswith("/") or not doc_path_prefix.endswith("/"):
            raise RuntimeError(f"doc_path_prefix must look like '/document/path/': {doc_path_prefix}")

        result.append(
            Source(
                source_id=source_id,
                site_root=site_root.rstrip("/"),
                seed_path=seed_path,
                sentinel_ids=sentinel_ids,
                doc_path_prefix=doc_path_prefix,
                output_subdir=output_subdir,
            )
        )
    return result


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    return None


def fetch_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/plain,*/*",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
                return response.read()
        except HTTPError as exc:
            last_error = exc
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable:
                raise RuntimeError(f"Non-retryable HTTP {exc.code} for {url}: {exc}") from exc
            if attempt == MAX_RETRIES:
                break
            sleep_seconds = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            if sleep_seconds is None:
                sleep_seconds = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            time.sleep(sleep_seconds)
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def check_robots_allowed(site_root: str, path_prefix: str) -> bool:
    """Best-effort robots.txt check: longest matching Allow/Disallow rule wins.

    Not a full RFC 9309 implementation -- good enough for this site's simple
    single-group robots.txt, and fails closed (treats an unreadable
    robots.txt as "not allowed") rather than silently proceeding.
    """
    robots_url = urljoin(site_root + "/", "robots.txt")
    try:
        text = fetch_text(robots_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] could not fetch robots.txt: {exc}")
        return False

    rules: List[Tuple[str, str]] = []  # (directive, path)
    active_group = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            active_group = value == "*"
            continue
        if not active_group:
            continue
        if key in ("allow", "disallow") and value:
            rules.append((key, value))

    best_len = -1
    best_directive = "allow"  # default-allow if nothing matches
    for directive, path in rules:
        if path_prefix.startswith(path):
            if len(path) > best_len or (len(path) == best_len and directive == "allow"):
                best_len = len(path)
                best_directive = directive
    return best_directive == "allow"


# --------------------------------------------------------------------------
# Discovery: parse the embedded nav tree
# --------------------------------------------------------------------------

def parse_nav_tree(nav_root: Tag, source: Source) -> Dict[str, DocPage]:
    doc_id_re = re.compile(re.escape(source.doc_path_prefix) + r"(\d+)$")
    pages: Dict[str, DocPage] = {}

    def walk(wrap: Tag, path_stack: Tuple[str, ...]) -> None:
        item = wrap.find(["a", "div"], class_="ep-doc-item", recursive=False)
        if item is None:
            return

        if item.name == "a":
            href = item.get("href", "")
            match = doc_id_re.match(href)
            if not match:
                print(f"[WARN] discovery: skipping unrecognized nav link href={href!r}")
                return
            doc_id = match.group(1)
            label = item.get_text(strip=True)
            if doc_id not in pages:
                pages[doc_id] = DocPage(
                    doc_id=doc_id,
                    label=label,
                    sections=path_stack,
                    url=f"{source.site_root}{source.doc_path_prefix}{doc_id}",
                    rel_path=f"{doc_id}.md",
                )
            return

        category_name = item.get_text(strip=True)
        new_stack = path_stack + (category_name,)
        for child in wrap.find_all("div", class_="ep-doc-wrap", recursive=False):
            walk(child, new_stack)

    for top_wrap in nav_root.find_all("div", class_="ep-doc-wrap", recursive=False):
        walk(top_wrap, ())

    return pages


def discover_doc_pages(source: Source) -> Dict[str, DocPage]:
    seed_url = urljoin(source.site_root + "/", source.seed_path.lstrip("/"))
    html = fetch_text(seed_url)
    soup = BeautifulSoup(html, "html.parser")
    nav_root = soup.select_one("div.ep-doc-select")
    if nav_root is None:
        raise RuntimeError(f"Nav tree (.ep-doc-select) not found on seed page {seed_url}")
    pages = parse_nav_tree(nav_root, source)
    if not pages:
        raise RuntimeError(f"No document links discovered from {seed_url}")
    return pages


def validate_discovery(pages: Dict[str, DocPage], source: Source, previous_count: int) -> None:
    missing_sentinels = [sid for sid in source.sentinel_ids if sid not in pages]
    if missing_sentinels:
        raise CircuitBreaker(
            f"discovery missing sentinel ids {missing_sentinels}; "
            f"treating discovery as incomplete, aborting without touching docs/"
        )

    if previous_count > 0:
        drop = (previous_count - len(pages)) / previous_count
        if drop > DISCOVERY_DROP_THRESHOLD:
            raise CircuitBreaker(
                f"discovery count dropped {drop:.0%} ({previous_count} -> {len(pages)}), "
                f"exceeds {DISCOVERY_DROP_THRESHOLD:.0%} threshold; aborting without touching docs/"
            )


# --------------------------------------------------------------------------
# Page validity + HTML -> Markdown conversion
# --------------------------------------------------------------------------

def extract_article_soup(html: str) -> Optional[BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("div.ep-doc-area-cherry")
    if article is None:
        return None
    text_len = len(article.get_text(strip=True))
    if text_len < 5:
        return None
    return article


def _code_language(el: Tag) -> Optional[str]:
    for cls in el.get("class") or []:
        match = re.match(r"^language-([\w+-]+)$", cls)
        if match:
            return match.group(1)
    return None


def _clean_code_blocks(article: Tag) -> None:
    for pre in article.find_all("pre"):
        code = pre.find("code")
        if code is None:
            continue
        lines = code.find_all("span", class_="code-line")
        if lines:
            text = "\n".join(line.get_text() for line in lines)
        else:
            text = code.get_text()
        code.clear()
        code.append(text)


def _rewrite_internal_links(article: Tag, source: Source) -> None:
    doc_path_re = re.compile(re.escape(source.doc_path_prefix) + r"(\d+)$")
    fragment_id_re = re.compile(r"^#(\d+)$")
    for a in article.find_all("a", href=True):
        href = a["href"].strip()
        match = doc_path_re.match(href) or fragment_id_re.match(href)
        if match:
            a["href"] = f"./{match.group(1)}.md"


_ADMONITION_ICON_SRC_PREFIX = "data:image/svg+xml;base64,"


def _is_decorative_admonition_icon(img: Tag, src: str) -> bool:
    # Observed in the wild: cherry-markdown's admonition/callout marker icons
    # (e.g. right before "注意") are inline base64 SVGs with no alt text --
    # never real document content, since real screenshots in this corpus are
    # always external URLs. Matched narrowly on that exact signature rather
    # than "any data: URI" so a genuine embedded image (a different mime
    # type, or one with meaningful alt text) is kept, not silently dropped.
    if not src.startswith(_ADMONITION_ICON_SRC_PREFIX):
        return False
    return not (img.get("alt") or "").strip()


def _fix_images(article: Tag, site_root: str) -> None:
    for img in article.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if _is_decorative_admonition_icon(img, src):
            img.decompose()
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = site_root + src
        if src:
            img["src"] = src
        if img.has_attr("data-src"):
            del img["data-src"]


def _is_complex_table(table: Tag) -> bool:
    for cell in table.find_all(["td", "th"]):
        for attr in ("rowspan", "colspan"):
            value = cell.get(attr, "").strip()
            if value.isdigit() and int(value) > 1:
                return True
    return False


def _normalize_markdown(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    normalized: List[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 2:
                continue
        else:
            blank_run = 0
        normalized.append(line)
    return "\n".join(normalized).strip() + "\n"


def convert_article_to_markdown(article: Tag, source: Source) -> str:
    for toc in article.select("dir.toc"):
        toc.decompose()
    for anchor in article.select("a.anchor"):
        anchor.decompose()

    _clean_code_blocks(article)
    _rewrite_internal_links(article, source)
    _fix_images(article, source.site_root)

    raw_blocks: List[str] = []
    for table in article.find_all("table"):
        if _is_complex_table(table):
            placeholder = NavigableString(f"@@RAWHTML{len(raw_blocks)}@@")
            raw_blocks.append(str(table))
            table.replace_with(placeholder)

    markdown = html_to_markdown_raw(
        str(article),
        heading_style="ATX",
        code_language_callback=_code_language,
    )
    for idx, block in enumerate(raw_blocks):
        markdown = markdown.replace(f"@@RAWHTML{idx}@@", f"\n\n{block}\n\n")

    return _normalize_markdown(markdown)


# --------------------------------------------------------------------------
# Manifest helpers
# --------------------------------------------------------------------------

def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_existing_manifest(path: Path) -> Dict:
    if not path.exists():
        return {"files": {}}
    # Deliberately no try/except here, unlike load_progress(): that's a
    # pre-existing difference between the two loaders (a corrupt manifest
    # crashing the run instead of being silently treated as absent), kept
    # as-is -- adding new fallback behavior wasn't part of the schema_version
    # tripwire this was scoped to.
    data = json.loads(path.read_text(encoding="utf-8"))
    # No schema_version key at all means a manifest written before this
    # field existed -- treat that as the current version, not a mismatch.
    version = data.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if version != MANIFEST_SCHEMA_VERSION:
        print(
            f"[WARN] {path} has schema_version={version!r}, expected "
            f"{MANIFEST_SCHEMA_VERSION} -- ignoring existing manifest"
        )
        return {"files": {}}
    return data


def cleanup_stale_temp_files(root: Path) -> None:
    """Remove `write_text_atomic`/`write_json_atomic` temp files orphaned by
    a process that got SIGKILLed (CI timeout, manual TaskStop) between the
    temp-file write and the rename -- exactly the interruption scenario
    --resume exists to survive. Left alone, one would eventually get swept
    up by a later `git add docs/` and committed permanently."""
    for tmp_file in root.rglob(".*.tmp*"):
        if tmp_file.is_file():
            print(f"[INFO] removing orphaned temp file from a previous interrupted run: {tmp_file}")
            tmp_file.unlink()


def remove_empty_dirs(start: Path, stop: Path) -> None:
    current = start
    while current != stop and current.exists():
        if any(current.iterdir()):
            break
        current.rmdir()
        current = current.parent


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def fetch_one_doc(source: Source, page: DocPage, existing: Dict) -> FetchOutcome:
    try:
        html = fetch_text(page.url)
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(failed=True, error=str(exc))

    article = extract_article_soup(html)
    if article is None:
        return FetchOutcome(failed=True, error="content container not found / too short")

    # Snapshot before conversion: convert_article_to_markdown() mutates
    # `article` in place (strips the TOC, rewrites links, drops admonition
    # icons, replaces complex tables with placeholders, ...), so this is the
    # last point at which `article` still reflects what the site actually
    # sent. Deliberately just the article body, not the full page -- every
    # page also embeds the entire site nav tree, which bloats a full-page
    # save to >1.5MB per doc with no per-doc information in the extra bytes.
    raw_article_html = str(article)

    try:
        markdown = convert_article_to_markdown(article, source)
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(failed=True, error=f"conversion error: {exc}")

    # The nav-tree label, not anything from the article body: opening a
    # mirrored file with no other context (e.g. just its numeric doc id)
    # otherwise gives no clue what it's about. Prepended unconditionally,
    # even on the rare page whose body also opens with a matching heading --
    # per the project's existing "don't guess, don't delete" stance on
    # conversion (see the module docstring), a possible duplicate heading is
    # preferable to speculatively stripping content.
    if page.label.strip():
        markdown = f"# {page.label}\n\n{markdown}"

    digest = sha256_text(markdown)
    html_digest = sha256_text(raw_article_html)
    entry = {
        "source": source.source_id,
        "doc_id": page.doc_id,
        "slug": page.doc_id,
        "label": page.label,
        "section": "/".join(page.sections),
        "url": page.url,
        "sha256": digest,
        "bytes": len(markdown.encode("utf-8")),
        "html_sha256": html_digest,
        "html_bytes": len(raw_article_html.encode("utf-8")),
        "converter_version": CONVERTER_VERSION,
        "first_seen_at": existing.get("first_seen_at") or now_iso(),
        "last_verified_at": now_iso(),
        "fetched_at": now_iso(),
        "missing_since": None,
        "fetch_failures": 0,
    }
    return FetchOutcome(manifest_entry=entry, markdown_text=markdown, raw_html_text=raw_article_html)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return parsed


@dataclass
class SourceSyncResult:
    """What main() needs back from syncing one source: its contribution to
    the run-wide totals, and -- if a hard error or a circuit-breaker pause
    means the whole sync must stop right now -- the exit code to return.
    `new_files` and `failed_pages` are *not* duplicated here: sync_source()
    mutates the run-wide dict/list main() passes it, the same way the
    inlined per-source loop this was extracted from always did."""
    total_pages: int = 0
    successful_pages: int = 0
    stop_exit_code: Optional[int] = None


def sync_source(
    source: Source,
    args: argparse.Namespace,
    existing_files: Dict[str, Dict],
    progress: Dict,
    new_files: Dict[str, Dict],
    failed_pages: List[Tuple[str, str]],
    strict_fetch: bool,
) -> SourceSyncResult:
    """Discover and fetch every doc for one source. `new_files`, `failed_pages`,
    and `progress` are shared across every source in the run and mutated in
    place (matching the pre-extraction inlined loop); see SourceSyncResult
    for what's returned instead."""
    result = SourceSyncResult()
    print(f"[INFO] source={source.source_id} site={source.site_root}")

    if not check_robots_allowed(source.site_root, source.doc_path_prefix):
        print(f"[ERROR] robots.txt no longer allows {source.doc_path_prefix}; aborting")
        result.stop_exit_code = 1
        return result

    previous_count = sum(
        1 for key, entry in existing_files.items()
        if entry.get("source") == source.source_id and not entry.get("missing_since")
    )

    try:
        pages = discover_doc_pages(source)
        validate_discovery(pages, source, previous_count)
    except CircuitBreaker as exc:
        print(f"[ERROR] circuit breaker: {exc}")
        result.stop_exit_code = 1
        return result
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] discovery failed: {exc}")
        result.stop_exit_code = 1
        return result

    print(f"[INFO] source={source.source_id} discovered={len(pages)} (previous={previous_count})")
    result.total_pages = len(pages)

    source_root = DOCS_ROOT / source.output_subdir
    source_root.mkdir(parents=True, exist_ok=True)

    doc_ids = sorted(pages.keys())
    if args.limit is not None and args.limit < len(doc_ids):
        step = max(1, len(doc_ids) // args.limit)
        doc_ids = doc_ids[::step][: args.limit]
        print(f"[INFO] --limit {args.limit}: sampling {len(doc_ids)} of {len(pages)} discovered docs")

    checkpoint = progress.get(source.source_id) if args.limit is None else None
    checkpoint = checkpoint or {}
    # `source_failed_pages` / `source_files` are scoped to this source
    # (and, unlike `done_doc_ids`, persist across --resume invocations),
    # unlike the run-wide `failed_pages` / `new_files`, so a checkpoint
    # save never has to re-filter the whole cumulative `new_files` dict --
    # it already has exactly this source's slice.
    done_doc_ids = set(checkpoint.get("done_doc_ids", []))
    source_files: Dict[str, Dict] = dict(checkpoint.get("files", {}))
    # The checkpoint claims each of these keys has a written .md file
    # on disk; don't just trust that -- a hand edit, a partial
    # checkout, or a manually deleted file would otherwise let a
    # missing doc silently end up "done" in the final manifest with
    # no corresponding content. Re-fetch anything that doesn't
    # actually exist instead.
    stale_keys = {key for key in source_files if not (DOCS_ROOT / key).exists()}
    if stale_keys:
        stale_ids = {key.rsplit("/", 1)[-1].removesuffix(".md") for key in stale_keys}
        print(
            f"[WARN] --resume: {len(stale_keys)} checkpointed doc(s) for "
            f"source={source.source_id} are missing their .md file on disk "
            f"(deleted or never written out-of-band?) -- re-fetching instead of "
            f"trusting the checkpoint: {sorted(stale_ids)}"
        )
        for key in stale_keys:
            del source_files[key]
        done_doc_ids -= stale_ids
    new_files.update(source_files)
    source_failed_pages: List[Tuple[str, str]] = [
        tuple(pair) for pair in checkpoint.get("failed", [])
    ]
    failed_pages.extend(source_failed_pages)
    run_started_at = checkpoint.get("run_started_at", now_iso())
    print(f"[INFO] source={source.source_id}: {len(done_doc_ids)}/{len(doc_ids)} already attempted")

    def save_checkpoint() -> None:
        progress[source.source_id] = {
            "run_started_at": run_started_at,
            "done_doc_ids": sorted(done_doc_ids),
            "files": source_files,
            "failed": [list(pair) for pair in source_failed_pages],
        }
        write_json_atomic(PROGRESS_PATH, {"schema_version": PROGRESS_SCHEMA_VERSION, **progress})

    # Scoped to *this process's own attempts*, unlike `source_failed_pages`
    # above which accumulates across every --resume invocation for this
    # source's lifetime. The circuit breaker below must judge only this
    # session's recent failure rate: if it judged the cumulative rate
    # instead, one CAPTCHA cascade months ago would keep re-tripping the
    # breaker after just a single new attempt on every future --resume,
    # since attempted-so-far would already be well past the minimum
    # sample size. A real sliding window (not a whole-session average)
    # so one early transient failure can't trip the breaker on its own,
    # while a genuine cascade later in a long run is still caught within
    # a couple of failures instead of needing to outweigh a large
    # cumulative denominator. `min_sample` also shrinks to the number of
    # docs left for a small source, so a source with fewer total docs
    # than the window still gets checked once fully attempted instead of
    # never at all.
    recent_outcomes: "deque[bool]" = deque(maxlen=FAILURE_WINDOW_SIZE)  # True = failed
    min_sample = min(FAILURE_WINDOW_SIZE, len(doc_ids) - len(done_doc_ids))

    for doc_id in doc_ids:
        if doc_id in done_doc_ids:
            continue

        page = pages[doc_id]
        manifest_key = f"{source.output_subdir}/{page.rel_path}"
        existing_entry = existing_files.get(manifest_key, {})

        time.sleep(random.uniform(REQUEST_DELAY_MIN_SECONDS, REQUEST_DELAY_MAX_SECONDS))
        outcome = fetch_one_doc(source, page, existing_entry)

        if outcome.failed:
            print(f"[WARN] failed url={page.url} err={outcome.error}")
            failed_pages.append((page.url, outcome.error))
            source_failed_pages.append((page.url, outcome.error))
            recent_outcomes.append(True)
            if existing_entry:
                # Keep last-known-good content and manifest record; just
                # bump the failure counter so persistent failures are
                # visible without deleting anything.
                carried = dict(existing_entry)
                carried["fetch_failures"] = int(existing_entry.get("fetch_failures", 0)) + 1
                new_files[manifest_key] = carried
                source_files[manifest_key] = carried
        else:
            entry = outcome.manifest_entry
            dest = source_root / page.rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if existing_entry.get("sha256") != entry["sha256"] or not dest.exists():
                write_text_atomic(dest, outcome.markdown_text)
            html_dest = dest.with_suffix(".html")
            if existing_entry.get("html_sha256") != entry["html_sha256"] or not html_dest.exists():
                write_text_atomic(html_dest, outcome.raw_html_text)
            new_files[manifest_key] = entry
            source_files[manifest_key] = entry
            result.successful_pages += 1
            print(f"[OK] {manifest_key}")
            recent_outcomes.append(False)

        done_doc_ids.add(doc_id)
        if args.limit is None:
            save_checkpoint()

        if min_sample > 0 and len(recent_outcomes) >= min_sample:
            # Denominator is a sliding window of docs attempted in *this
            # process*, not the cumulative total across every --resume
            # invocation -- see the comment on recent_outcomes above.
            failure_rate = sum(recent_outcomes) / len(recent_outcomes)
            if failure_rate > FAILURE_RATE_THRESHOLD:
                print(
                    f"[PAUSED] circuit breaker: failure rate {failure_rate:.1%} exceeds "
                    f"{FAILURE_RATE_THRESHOLD:.0%} over the last {len(recent_outcomes)} attempts "
                    f"this run (this pattern -- a run of successes followed by a run of failures "
                    f"-- has empirically matched a CAPTCHA block, not a real breakage). Progress "
                    f"up to this point is checkpointed to {PROGRESS_PATH}; CI should commit it "
                    f"as-is and a future `--resume` run (e.g. tomorrow's cron) will continue from "
                    f"here."
                )
                # Not a hard failure in tolerant mode: this is the
                # expected shape of a multi-day resumable sync against a
                # site with a tight anti-bot budget, not a bug to alert
                # on. STRICT_FETCH=1 still treats it as a failure, since
                # that mode means "tell me about any imperfection".
                result.stop_exit_code = 1 if strict_fetch else 0
                return result

        if result.successful_pages >= MAX_SUCCESSFUL_FETCHES_PER_RUN:
            print(
                f"[PAUSED] hit the per-run cap of {MAX_SUCCESSFUL_FETCHES_PER_RUN} successful "
                f"fetches (failure rate is still fine -- this is a proactive stop, not a reaction "
                f"to a block). Progress is checkpointed to {PROGRESS_PATH}; a future `--resume` "
                f"run gets its own fresh {MAX_SUCCESSFUL_FETCHES_PER_RUN}-fetch budget."
            )
            # Always exit 0, unlike the failure-rate breaker above: this is
            # expected, deliberate pacing that fires on every run with more
            # than MAX_SUCCESSFUL_FETCHES_PER_RUN left in the backlog, not
            # a signal of a site-side problem -- STRICT_FETCH's "tell me
            # about any imperfection" contract doesn't apply to it.
            result.stop_exit_code = 0
            return result

    return result


def finalize_sync(
    sources: List[Source],
    args: argparse.Namespace,
    existing_files: Dict[str, Dict],
    new_files: Dict[str, Dict],
    total_pages: int,
    successful_pages: int,
    failed_pages: List[Tuple[str, str]],
    strict_fetch: bool,
) -> int:
    """Runs once every source's sync_source() call has completed normally
    (never on a circuit-breaker pause or hard error, which return early from
    main() before reaching this): delayed deletion, the final manifest
    write, checkpoint clearing, and the run's exit code."""
    # Delayed deletion: only drop files that have been missing for
    # MISSING_CONFIRM_RUNS consecutive runs, never on a single run.
    previous_keys = set(existing_files.keys())
    current_keys = set(new_files.keys())
    vanished_keys = previous_keys - current_keys

    for key in vanished_keys:
        old_entry = existing_files[key]
        missing_since = old_entry.get("missing_since")
        run_count = int(old_entry.get("missing_run_count", 0)) + 1
        if missing_since is None:
            missing_since = now_iso()
        carried = dict(old_entry)
        carried["missing_since"] = missing_since
        carried["missing_run_count"] = run_count
        if run_count >= MISSING_CONFIRM_RUNS:
            file_path = DOCS_ROOT / key
            if file_path.exists():
                file_path.unlink()
                remove_empty_dirs(file_path.parent, DOCS_ROOT)
            html_path = file_path.with_suffix(".html")
            if html_path.exists():
                html_path.unlink()
            print(f"[INFO] removed {key} after {run_count} consecutive runs missing")
            continue
        new_files[key] = carried
        print(f"[INFO] {key} missing this run ({run_count}/{MISSING_CONFIRM_RUNS}); keeping file for now")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "tool": "scripts/fetch_wecom_docs.py",
        "converter_version": CONVERTER_VERSION,
        "strict_fetch": strict_fetch,
        "sources": [
            {
                "id": s.source_id,
                "site_root": s.site_root,
                "seed_path": s.seed_path,
                "doc_path_prefix": s.doc_path_prefix,
                "output_subdir": s.output_subdir,
            }
            for s in sources
        ],
        "stats": {
            "total_pages": total_pages,
            "successful_pages": successful_pages,
            "failed_pages": len(failed_pages),
        },
        "failed": [{"url": url, "error": err} for url, err in failed_pages],
        "files": {k: new_files[k] for k in sorted(new_files.keys())},
    }

    write_json_atomic(MANIFEST_PATH, manifest)

    # Every source ran its doc_ids loop to completion (no circuit breaker
    # returned early above), so this sync is fully done -- the checkpoint's
    # job is finished and stale progress must not linger for a future
    # --resume to misread. Never true of a --limit run, which only ever
    # visits a sampled subset -- it must not clear a real checkpoint just
    # because *it* happened to reach the end of its own (partial) doc_ids.
    if args.limit is None and PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()

    print("\n[SUMMARY]")
    print(f"total_pages={total_pages}")
    print(f"successful_pages={successful_pages}")
    print(f"failed_pages={len(failed_pages)}")

    if failed_pages and strict_fetch:
        print("[ERROR] STRICT_FETCH=1 and failures detected")
        return 1

    if not new_files:
        # Deliberately not `successful_pages == 0`: a --resume run whose
        # remaining doc_ids were all already checkpointed as done (or where
        # every newly-attempted doc happens to fail) can legitimately finish
        # a fully-populated manifest while fetching zero *new* docs this
        # process. What actually indicates a broken sync is an empty
        # manifest, not an empty this-invocation delta.
        print("[ERROR] No documents in the resulting manifest")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help=(
            "Only fetch this many docs, evenly sampled across the discovered "
            "set (for a quick, low-traffic smoke test). Discovery itself is "
            "never limited, so the circuit breaker's drop-threshold check "
            "still sees the true site-wide count. Do not use --limit for a "
            "real sync -- it leaves most manifest entries stale. Ignores "
            "docs/sync_progress.json entirely, even if --resume is also "
            "passed: never reads, writes, or deletes it, so it's always "
            "safe to run without disturbing a real in-progress sync."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Continue an interrupted sync (e.g. one killed mid-run by a "
            "CAPTCHA block or a CI timeout) using docs/sync_progress.json "
            "instead of starting over. Safe to pass unconditionally in "
            "automation: if there's no checkpoint, this is a no-op and "
            "behaves like a normal fresh run. Discovery changes preserve "
            "checkpointed progress: newly discovered doc ids are fetched "
            "normally, while ids no longer discovered are not visited in "
            "the current run."
        ),
    )
    args = parser.parse_args()

    strict_fetch = os.environ.get("STRICT_FETCH", "0") == "1"

    DOCS_ROOT.mkdir(parents=True, exist_ok=True)
    cleanup_stale_temp_files(DOCS_ROOT)
    sources = load_sources(CONFIG_PATH)
    existing_manifest = load_existing_manifest(MANIFEST_PATH)
    existing_files: Dict[str, Dict] = existing_manifest.get("files", {})

    progress = load_progress(PROGRESS_PATH) if args.resume else {}
    if args.limit is None and not args.resume and PROGRESS_PATH.exists():
        # Fresh full run explicitly requested: don't let a stale checkpoint
        # from an old interrupted attempt leak into it. Never touch the
        # checkpoint in --limit mode -- a sampled smoke-test run is neither
        # a real resume nor a real fresh full attempt, and must not clobber
        # whatever real, in-progress sync state happens to exist.
        PROGRESS_PATH.unlink()

    new_files: Dict[str, Dict] = {}
    if args.limit is not None:
        # Sampling mode: carry every existing entry through untouched so the
        # docs we don't visit this run neither vanish from the manifest nor
        # get treated as "missing" by the delayed-deletion logic below.
        new_files.update(existing_files)
    total_pages = 0
    successful_pages = 0
    failed_pages: List[Tuple[str, str]] = []

    for source in sources:
        result = sync_source(
            source, args, existing_files, progress, new_files, failed_pages, strict_fetch
        )
        total_pages += result.total_pages
        successful_pages += result.successful_pages
        if result.stop_exit_code is not None:
            return result.stop_exit_code

    return finalize_sync(
        sources, args, existing_files, new_files, total_pages, successful_pages,
        failed_pages, strict_fetch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
