"""Ad hoc, hand-invoked comparison tool: fetch a small, explicit list of
docs' TRUE Markdown source via developer.work.weixin.qq.com's internal
docFetch/fetchCnt AJAX endpoint (discovered 2026-08-22 via browser devtools,
not documented anywhere), and diff it against this repo's HTML-scraped-and-
converted docs/wecom/<id>.md -- either to sanity-check conversion fidelity,
or to recover content for docs whose body doesn't server-render at all (a
real gap found the same day: a batch of newly-added docs render their
article body via client-side JS calling this same endpoint, so a plain GET
of /document/path/<id> returns no content for them at all).

IMPORTANT -- robots.txt does NOT allow /docFetch/. The site's robots.txt is:

    Disallow: /
    Allow: /$
    Allow: /?
    Allow: /community/...
    Allow: /tutorial
    Allow: /document
    Allow: /resource/devtool$

/docFetch/ is not in that Allow list, so it falls under the leading
`Disallow: /`. There is no scheduled pipeline built on this endpoint (a
parallel "true-source" sync was prototyped 2026-08-23 and deliberately cut
before shipping -- its only real target docs are never discovered by
nav-tree walking in the first place, so it had no actual content to
recover). THIS script is the only thing in this repo that touches
/docFetch/, and it stays a narrow, hand-run comparison/spot-check tool:
  - NEVER wired into any CI workflow -- hand-run only, so it can't be
    triggered casually or end up in a schedule of its own,
  - hard-capped at a small number of ids per invocation (MAX_IDS),
  - not a sync/recovery mechanism -- just a fidelity spot-check against
    docs/wecom/<id>.md.

Resolving the id -> doc_id mapping this endpoint needs does NOT touch
/docFetch/ at all: every ordinary /document/path/<id> page (robots.txt DOES
allow /document) embeds the whole site's flat id/doc_id/title index as raw
JSON for its own client-side JS to use, so one normal page fetch resolves
every id this run needs before a single /docFetch/ request is made.
"""
from __future__ import annotations

import argparse
import re
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_wecom_docs as fw

MAX_IDS = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doc-ids",
        required=True,
        help="Comma-separated URL-path doc ids (the number in /document/path/<id>), e.g. 101840,101805",
    )
    args = parser.parse_args()

    doc_ids = list(dict.fromkeys(d.strip() for d in args.doc_ids.split(",") if d.strip()))
    if not doc_ids:
        print("[ERROR] no doc ids given", file=sys.stderr)
        return 1
    if len(doc_ids) > MAX_IDS:
        print(f"[ERROR] {len(doc_ids)} ids requested, refusing above {MAX_IDS}", file=sys.stderr)
        return 1

    sources = fw.load_sources(fw.CONFIG_PATH)
    source = sources[0]

    print(f"[INFO] resolving id -> doc_id mapping from a single /document page fetch...")
    id_map = fw.build_id_to_doc_id_map(source)
    print(f"[INFO] resolved {len(id_map)} ids site-wide")

    for url_id in doc_ids:
        entry = id_map.get(url_id)
        if entry is None:
            print(f"[WARN] {url_id}: not found in the site's own id index, skipping")
            continue

        time.sleep(random.uniform(fw.REQUEST_DELAY_MIN_SECONDS, fw.REQUEST_DELAY_MAX_SECONDS))
        try:
            referer_url = fw.urljoin(source.site_root + "/", f"{source.doc_path_prefix.lstrip('/')}{url_id}")
            data = fw.fetch_via_doc_api(source.site_root, entry["doc_id"], referer_url)
        except Exception as exc:  # noqa: BLE001 -- best-effort comparison tool
            print(f"[WARN] {url_id} (doc_id={entry['doc_id']}): fetch failed: {exc}")
            continue

        content_md = data.get("content_md") or ""
        print(f"\n[OK] {url_id} ({entry['title']!r}, doc_id={entry['doc_id']}): {len(content_md)} chars of source Markdown")

        local_path = fw.DOCS_ROOT / source.output_subdir / f"{url_id}.md"
        if local_path.exists():
            local_md = local_path.read_text(encoding="utf-8")
            # Our local copy has the "# <title>" heading this endpoint's
            # content_md doesn't include -- strip it for a fair comparison.
            local_body = re.sub(r"^# .*\n\n", "", local_md, count=1)
            if local_body.strip() == content_md.strip():
                print(f"     matches docs/{source.output_subdir}/{url_id}.md exactly (modulo the title heading)")
            else:
                print(f"     DIFFERS from docs/{source.output_subdir}/{url_id}.md "
                      f"(local {len(local_body)} chars vs API {len(content_md)} chars)")
        else:
            print(f"     not currently in the mirror (docs/{source.output_subdir}/{url_id}.md doesn't exist)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
