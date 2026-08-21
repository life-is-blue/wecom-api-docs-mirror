"""Ad hoc audit tool: fetch a small, explicit list of doc ids' *raw* HTML
(no conversion) and save it under raw_html/<section path>/<id>.html, so a
human/agent can diff it against the corresponding docs/wecom/<id>.md.

Not part of the daily sync -- this is for spot-checking conversion quality,
so it deliberately:
  - requires an explicit, short id list (no "fetch everything" mode),
    de-duplicated so a repeated id doesn't cost a repeated request,
  - refuses to run above MAX_IDS *documents* -- note that's not a hard cap
    on requests: retries on failure (fetch_wecom_docs.MAX_RETRIES) can add
    more per id, plus one for nav discovery. Kept deliberately small (well
    under, not just under, the ~210-request range where the site's anti-bot
    gate has empirically kicked in) so even a worst-case retry storm across
    every id stays far short of it,
  - reuses fetch_wecom_docs's robots check, retry/backoff, and the same
    8-15s inter-request delay -- this is still live traffic to the same
    site and should behave like a polite single client.

Output is gitignored: raw HTML is not part of this repo's public mirror
(that's the converted Markdown), just local/CI scratch for comparison.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_wecom_docs as fw

MAX_IDS = 15

UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_component(name: str) -> str:
    name = UNSAFE_PATH_CHARS.sub("_", name).strip()
    return name or "_"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doc-ids",
        required=True,
        help="Comma-separated doc ids to capture, e.g. 97322,97323",
    )
    parser.add_argument(
        "--out-dir",
        default=str(fw.REPO_ROOT / "raw_html"),
        help="Output root directory (default: <repo>/raw_html)",
    )
    args = parser.parse_args()

    doc_ids = list(dict.fromkeys(d.strip() for d in args.doc_ids.split(",") if d.strip()))
    if not doc_ids:
        print("[ERROR] no doc ids given", file=sys.stderr)
        return 1
    if len(doc_ids) > MAX_IDS:
        print(
            f"[ERROR] {len(doc_ids)} ids requested, refusing above {MAX_IDS} "
            "(this tool is for targeted spot-checks, not bulk capture -- "
            "use the real fetcher with --resume for that)",
            file=sys.stderr,
        )
        return 1

    sources = fw.load_sources(fw.CONFIG_PATH)
    out_root = Path(args.out_dir)
    captured = 0

    for source in sources:
        if not fw.check_robots_allowed(source.site_root, source.doc_path_prefix):
            print(f"[ERROR] robots.txt disallows {source.doc_path_prefix} on {source.site_root}")
            return 1

        print(f"[INFO] discovering nav tree for {source.source_id} to resolve section paths...")
        pages = fw.discover_doc_pages(source)

        for doc_id in doc_ids:
            page = pages.get(doc_id)
            if page is None:
                print(f"[WARN] doc id {doc_id} not found in {source.source_id}'s nav tree, skipping")
                continue

            section_parts = [sanitize_component(s) for s in page.sections]
            dest_dir = out_root.joinpath(source.output_subdir, *section_parts)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{doc_id}.html"

            time.sleep(random.uniform(fw.REQUEST_DELAY_MIN_SECONDS, fw.REQUEST_DELAY_MAX_SECONDS))
            try:
                html = fw.fetch_text(page.url)
            except Exception as exc:  # noqa: BLE001 -- best-effort audit tool
                print(f"[WARN] failed to fetch {page.url}: {exc}")
                continue

            dest.write_text(html, encoding="utf-8")
            print(f"[OK] {page.url} -> {dest.relative_to(out_root)} ({len(html)} bytes)")
            captured += 1

    if captured == 0:
        print("[ERROR] captured nothing (no requested id matched any source, or every fetch failed)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
