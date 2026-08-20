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

USER_AGENT = (
    "wecom-api-docs-mirror/1.0 "
    "(+https://github.com/search?q=wecom-api-docs-mirror; doc-mirror bot)"
)

CONVERTER_VERSION = "1"

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
# Per-run fetch failure rate above this trips the circuit breaker.
FAILURE_RATE_THRESHOLD = 0.05
# A doc must be missing from discovery this many consecutive runs before
# its file is actually deleted.
MISSING_CONFIRM_RUNS = 3

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

DOC_ID_RE = re.compile(r"^(\d+)$")


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
    failed: bool = False
    error: str = ""
    invalid_page: bool = False


class CircuitBreaker(RuntimeError):
    """Raised to abort a run without touching manifest/docs on disk."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _fix_images(article: Tag, site_root: str) -> None:
    for img in article.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
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
    return json.loads(path.read_text(encoding="utf-8"))


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
        return FetchOutcome(failed=True, invalid_page=True, error="content container not found / too short")

    try:
        markdown = convert_article_to_markdown(article, source)
    except Exception as exc:  # noqa: BLE001
        return FetchOutcome(failed=True, error=f"conversion error: {exc}")

    digest = sha256_text(markdown)
    entry = {
        "source": source.source_id,
        "doc_id": page.doc_id,
        "slug": page.doc_id,
        "label": page.label,
        "section": "/".join(page.sections),
        "all_sections": ["/".join(page.sections)],
        "url": page.url,
        "sha256": digest,
        "bytes": len(markdown.encode("utf-8")),
        "converter_version": CONVERTER_VERSION,
        "first_seen_at": existing.get("first_seen_at") or now_iso(),
        "last_verified_at": now_iso(),
        "fetched_at": now_iso(),
        "missing_since": None,
        "fetch_failures": 0,
    }
    return FetchOutcome(manifest_entry=entry, markdown_text=markdown)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Only fetch this many docs, evenly sampled across the discovered "
            "set (for a quick, low-traffic smoke test). Discovery itself is "
            "never limited, so the circuit breaker's drop-threshold check "
            "still sees the true site-wide count; only the failure-rate "
            "check becomes meaningless in this mode since the denominator "
            "is the full count, not the sampled one. Do not use --limit for "
            "a real sync -- it leaves most manifest entries stale."
        ),
    )
    args = parser.parse_args()

    strict_fetch = os.environ.get("STRICT_FETCH", "0") == "1"

    DOCS_ROOT.mkdir(parents=True, exist_ok=True)
    sources = load_sources(CONFIG_PATH)
    existing_manifest = load_existing_manifest(MANIFEST_PATH)
    existing_files: Dict[str, Dict] = existing_manifest.get("files", {})

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
        print(f"[INFO] source={source.source_id} site={source.site_root}")

        if not check_robots_allowed(source.site_root, source.doc_path_prefix):
            print(f"[ERROR] robots.txt no longer allows {source.doc_path_prefix}; aborting")
            return 1

        previous_count = sum(
            1 for key, entry in existing_files.items()
            if entry.get("source") == source.source_id and not entry.get("missing_since")
        )

        try:
            pages = discover_doc_pages(source)
            validate_discovery(pages, source, previous_count)
        except CircuitBreaker as exc:
            print(f"[ERROR] circuit breaker: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] discovery failed: {exc}")
            return 1

        print(f"[INFO] source={source.source_id} discovered={len(pages)} (previous={previous_count})")
        total_pages += len(pages)

        source_root = DOCS_ROOT / source.output_subdir
        source_root.mkdir(parents=True, exist_ok=True)

        doc_ids = sorted(pages.keys())
        if args.limit is not None and args.limit < len(doc_ids):
            step = max(1, len(doc_ids) // args.limit)
            doc_ids = doc_ids[::step][: args.limit]
            print(f"[INFO] --limit {args.limit}: sampling {len(doc_ids)} of {len(pages)} discovered docs")

        for doc_id in doc_ids:
            page = pages[doc_id]
            manifest_key = f"{source.output_subdir}/{page.rel_path}"
            existing_entry = existing_files.get(manifest_key, {})

            time.sleep(random.uniform(REQUEST_DELAY_MIN_SECONDS, REQUEST_DELAY_MAX_SECONDS))
            outcome = fetch_one_doc(source, page, existing_entry)

            if outcome.failed:
                print(f"[WARN] failed url={page.url} err={outcome.error}")
                failed_pages.append((page.url, outcome.error))
                if existing_entry:
                    # Keep last-known-good content and manifest record; just
                    # bump the failure counter so persistent failures are
                    # visible without deleting anything.
                    carried = dict(existing_entry)
                    carried["fetch_failures"] = int(existing_entry.get("fetch_failures", 0)) + 1
                    new_files[manifest_key] = carried
                continue

            entry = outcome.manifest_entry
            dest = source_root / page.rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if existing_entry.get("sha256") != entry["sha256"] or not dest.exists():
                dest.write_text(outcome.markdown_text, encoding="utf-8")
            new_files[manifest_key] = entry
            successful_pages += 1
            print(f"[OK] {manifest_key}")

        failure_rate = len(failed_pages) / total_pages if total_pages else 0.0
        if failure_rate > FAILURE_RATE_THRESHOLD:
            print(
                f"[ERROR] circuit breaker: failure rate {failure_rate:.1%} exceeds "
                f"{FAILURE_RATE_THRESHOLD:.0%}; aborting without updating manifest or deleting files"
            )
            return 1

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
            print(f"[INFO] removed {key} after {run_count} consecutive runs missing")
            continue
        new_files[key] = carried
        print(f"[INFO] {key} missing this run ({run_count}/{MISSING_CONFIRM_RUNS}); keeping file for now")

    manifest = {
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

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n[SUMMARY]")
    print(f"total_pages={total_pages}")
    print(f"successful_pages={successful_pages}")
    print(f"failed_pages={len(failed_pages)}")

    if failed_pages and strict_fetch:
        print("[ERROR] STRICT_FETCH=1 and failures detected")
        return 1

    if successful_pages == 0:
        print("[ERROR] No documents fetched successfully")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
