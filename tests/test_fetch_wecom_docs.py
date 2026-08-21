"""Offline unit tests for scripts/fetch_wecom_docs.py.

All tests run against saved HTML fixtures in scripts/fixtures/ -- no network
access, so they're safe to run on every commit without touching the real
WeCom site (see the plan's "测试策略": live fetches only happen on the daily
cron / manual sync, not in verify-fetcher CI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_wecom_docs as fw  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "scripts" / "fixtures"

SOURCE = fw.Source(
    source_id="wecom",
    site_root="https://developer.work.weixin.qq.com",
    seed_path="/document/path/90664",
    sentinel_ids=("90664",),
    doc_path_prefix="/document/path/",
    output_subdir="wecom",
)


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fake_success_outcome(source, page, markdown: str, html: str = "<div>x</div>") -> "fw.FetchOutcome":
    """Builds the FetchOutcome a real fetch_one_doc() would return on
    success, for tests that monkeypatch fetch_one_doc directly instead of
    exercising the real HTTP/conversion path (those live in the conversion
    tests above, against real fixture HTML)."""
    return fw.FetchOutcome(
        manifest_entry={
            "source": source.source_id,
            "doc_id": page.doc_id,
            "slug": page.doc_id,
            "label": page.label,
            "section": "s",
            "url": page.url,
            "sha256": fw.sha256_text(markdown),
            "bytes": len(markdown.encode("utf-8")),
            "html_sha256": fw.sha256_text(html),
            "html_bytes": len(html.encode("utf-8")),
            "converter_version": fw.CONVERTER_VERSION,
            "first_seen_at": fw.now_iso(),
            "last_verified_at": fw.now_iso(),
            "fetched_at": fw.now_iso(),
            "missing_since": None,
            "fetch_failures": 0,
        },
        markdown_text=markdown,
        raw_html_text=html,
    )


# --------------------------------------------------------------------------
# Nav-tree discovery
# --------------------------------------------------------------------------

def test_parse_nav_tree_finds_all_levels():
    html = load_fixture("nav_tree.html")
    soup = BeautifulSoup(html, "html.parser")
    nav_root = soup.select_one("div.ep-doc-select")
    pages = fw.parse_nav_tree(nav_root, SOURCE)

    assert len(pages) == 8
    assert pages["90664"].sections == ("开发指南",)
    assert pages["90664"].label == "开发前必读"
    # level=1 leaf inside a level=0 category with no sub-category
    assert pages["90193"].sections == ("通讯录管理",)
    # level=2 leaf nested inside a level=1 sub-category
    assert pages["100067"].sections == ("通讯录管理", "成员管理")
    assert pages["90195"].sections == ("通讯录管理", "成员管理")


def test_parse_nav_tree_builds_correct_urls():
    html = load_fixture("nav_tree.html")
    soup = BeautifulSoup(html, "html.parser")
    nav_root = soup.select_one("div.ep-doc-select")
    pages = fw.parse_nav_tree(nav_root, SOURCE)

    assert pages["90664"].url == "https://developer.work.weixin.qq.com/document/path/90664"
    assert pages["90664"].rel_path == "90664.md"


def test_validate_discovery_passes_when_sentinels_present_and_no_big_drop():
    pages = {"90664": None, "90665": None}  # values unused by validator
    fw.validate_discovery(pages, SOURCE, previous_count=2)  # should not raise


def test_validate_discovery_trips_breaker_on_missing_sentinel():
    pages = {"90665": None}
    try:
        fw.validate_discovery(pages, SOURCE, previous_count=1)
    except fw.CircuitBreaker:
        pass
    else:
        raise AssertionError("expected CircuitBreaker for missing sentinel id")


def test_validate_discovery_trips_breaker_on_large_drop():
    pages = {f"{i}": None for i in range(10)}  # 10 pages this run
    source = fw.Source(**{**SOURCE.__dict__, "sentinel_ids": ()})
    try:
        fw.validate_discovery(pages, source, previous_count=100)  # was 100, now 10: 90% drop
    except fw.CircuitBreaker:
        pass
    else:
        raise AssertionError("expected CircuitBreaker for a large discovery drop")


# --------------------------------------------------------------------------
# Page validity detection
# --------------------------------------------------------------------------

def test_extract_article_soup_finds_content_on_real_page():
    html = load_fixture("article_plain.html")
    article = fw.extract_article_soup(html)
    assert article is not None
    assert "开发文档阅读说明" in article.get_text()


def test_extract_article_soup_rejects_page_without_content_div():
    html = load_fixture("page_invalid_no_content_div.html")
    article = fw.extract_article_soup(html)
    assert article is None


# --------------------------------------------------------------------------
# HTML -> Markdown conversion rules
# --------------------------------------------------------------------------

def test_conversion_strips_toc_and_anchor_self_links():
    html = load_fixture("article_plain.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "目录" not in markdown.split("\n")[0:3]  # toc block gone from the top
    assert "## 开发文档阅读说明" in markdown


def test_conversion_preserves_code_block_language_and_dedents_syntax_spans():
    html = load_fixture("article_plain.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "```javascript" in markdown
    assert "请求方式：GET/POST（HTTPS）" in markdown
    # syntax-highlighting spans must not leak into the code text
    assert 'class="token' not in markdown
    assert "<span" not in markdown


def test_conversion_rewrites_fragment_id_links_to_local_mirror_paths():
    html = load_fixture("article_plain.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "(./10649.md)" in markdown
    assert "(./15074.md)" in markdown
    assert "#10649" not in markdown


def test_conversion_normalizes_relative_image_src():
    html = load_fixture("article_plain.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "http://p.qpic.cn/pic_wework" in markdown


def test_conversion_drops_inline_base64_admonition_icons():
    # Observed in the live corpus: cherry-markdown renders callout markers
    # (e.g. right before "注意") as a bare inline base64 SVG <img> with no
    # alt text -- pure UI decoration, not document content. Real screenshots
    # in this corpus are always external URLs, never data: URIs.
    html = (
        '<div class="ep-doc-area-cherry">'
        '<p><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0i" />注意事项说明</p>'
        '</div>'
    )
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "data:image" not in markdown
    assert "注意事项说明" in markdown


def test_conversion_keeps_data_uri_image_with_real_alt_text():
    # A data: URI alone isn't the admonition-icon signature -- meaningful
    # alt text means a human captioned it, i.e. it's actual content, not a
    # decorative marker. Must not be silently dropped like the icons are.
    html = (
        '<div class="ep-doc-area-cherry">'
        '<p><img alt="架构图" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0i" />说明文字内容</p>'
        '</div>'
    )
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "data:image" in markdown
    assert "架构图" in markdown


def test_conversion_keeps_non_svg_data_uri_image():
    # Only base64 SVG is the observed decorative-icon shape; a PNG/JPEG
    # data: URI is out of scope for that heuristic and must be kept.
    html = (
        '<div class="ep-doc-area-cherry">'
        '<p><img src="data:image/png;base64,iVBORw0KGgoAAAA" />示例截图说明</p>'
        '</div>'
    )
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "data:image/png" in markdown


def test_conversion_drops_admonition_icon_inside_complex_table_without_losing_cell_text():
    html = (
        '<div class="ep-doc-area-cherry"><table class="cherry-table">'
        '<tr><td rowspan="2">before</td>'
        '<td><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0i" />after</td></tr>'
        '<tr><td>second row second cell</td></tr>'
        '</table></div>'
    )
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "data:image" not in markdown
    assert "before" in markdown
    assert "after" in markdown


def test_conversion_keeps_simple_table_as_markdown_table():
    html = load_fixture("article_code_table.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "| 参数 | 是否必须 | 说明 |" in markdown
    assert "access_token" in markdown or "access\\_token" in markdown


def test_conversion_preserves_complex_table_as_raw_html():
    html = load_fixture("article_code_table.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    # rowspan/colspan table must survive as literal HTML, not a mangled
    # markdown table that silently drops the merged cell's data.
    assert '<table class="cherry-table">' in markdown
    assert 'rowspan="2"' in markdown
    assert 'colspan="2"' in markdown


def test_conversion_preserves_json_code_block_language():
    html = load_fixture("article_code_table.html")
    article = fw.extract_article_soup(html)
    markdown = fw.convert_article_to_markdown(article, SOURCE)

    assert "```json" in markdown
    assert '"touser" : "UserID1|UserID2|UserID3"' in markdown


def test_fetch_one_doc_prepends_the_nav_label_as_a_title_heading(monkeypatch):
    # Opening a mirrored file with nothing but its numeric doc id gives no
    # clue what it's about -- fetch_one_doc() must prepend the nav-tree
    # label as a real "# title" heading, not just leave it in the manifest.
    monkeypatch.setattr(fw, "fetch_text", lambda url: load_fixture("article_plain.html"))
    page = fw.DocPage(
        doc_id="10649", label="开发文档阅读说明", sections=("s",),
        url="https://developer.work.weixin.qq.com/document/path/10649",
        rel_path="10649.md",
    )
    outcome = fw.fetch_one_doc(SOURCE, page, existing={})

    assert not outcome.failed
    assert outcome.markdown_text.startswith("# 开发文档阅读说明\n\n")
    assert outcome.manifest_entry["sha256"] == fw.sha256_text(outcome.markdown_text)


# --------------------------------------------------------------------------
# Atomic writes + checkpoint/resume
# --------------------------------------------------------------------------

def test_write_text_atomic_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "out.md"
    fw.write_text_atomic(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert list(tmp_path.iterdir()) == [target]


def test_write_json_atomic_round_trips(tmp_path):
    target = tmp_path / "out.json"
    fw.write_json_atomic(target, {"a": 1, "b": ["x", "y"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": ["x", "y"]}


def test_load_progress_treats_corrupt_file_as_absent(tmp_path):
    target = tmp_path / "sync_progress.json"
    target.write_text("{not valid json", encoding="utf-8")
    assert fw.load_progress(target) == {}


def test_load_progress_accepts_legacy_file_with_no_schema_version(tmp_path):
    # Every real checkpoint committed before schema_version existed (the
    # ~420-doc production checkpoint in this repo included) has no such key
    # -- it must load exactly as if it were the current version, not get
    # rejected.
    target = tmp_path / "sync_progress.json"
    target.write_text(
        json.dumps({"wecom": {"done_doc_ids": ["1"], "files": {}, "failed": []}}),
        encoding="utf-8",
    )
    loaded = fw.load_progress(target)
    assert loaded == {"wecom": {"done_doc_ids": ["1"], "files": {}, "failed": []}}


def test_load_progress_rejects_mismatched_schema_version(tmp_path):
    target = tmp_path / "sync_progress.json"
    target.write_text(
        json.dumps({"schema_version": 999, "wecom": {"done_doc_ids": ["1"]}}),
        encoding="utf-8",
    )
    assert fw.load_progress(target) == {}


def test_load_existing_manifest_accepts_legacy_file_with_no_schema_version(tmp_path):
    target = tmp_path / "docs_manifest.json"
    target.write_text(json.dumps({"files": {"wecom/1.md": {"doc_id": "1"}}}), encoding="utf-8")
    assert fw.load_existing_manifest(target) == {"files": {"wecom/1.md": {"doc_id": "1"}}}


def test_load_existing_manifest_rejects_mismatched_schema_version(tmp_path):
    target = tmp_path / "docs_manifest.json"
    target.write_text(
        json.dumps({"schema_version": 999, "files": {"wecom/1.md": {"doc_id": "1"}}}),
        encoding="utf-8",
    )
    assert fw.load_existing_manifest(target) == {"files": {}}


def test_successful_fetch_writes_both_markdown_and_raw_article_html(tmp_path, monkeypatch):
    """The article body's raw HTML (not the full page -- see fetch_one_doc's
    module docstring on why) is persisted alongside the converted Markdown,
    co-located under the same id, so it can be diffed against the .md by
    hand later without needing a live re-fetch."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    pages = {
        "1": fw.DocPage(
            doc_id="1", label="doc1", sections=("s",),
            url="https://example.invalid/document/path/1", rel_path="1.md",
        ),
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    raw_html = '<div class="ep-doc-area-cherry"><p>real article content</p></div>'

    def fake_fetch_one_doc(source, page, existing):
        return fake_success_outcome(source, page, "# doc 1\n", html=raw_html)

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)
    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py"])
    rc = fw.main()

    assert rc == 0
    assert (docs_root / "wecom" / "1.md").read_text(encoding="utf-8") == "# doc 1\n"
    assert (docs_root / "wecom" / "1.html").read_text(encoding="utf-8") == raw_html

    manifest = json.loads(fw.MANIFEST_PATH.read_text(encoding="utf-8"))
    entry = manifest["files"]["wecom/1.md"]
    assert entry["html_sha256"] == fw.sha256_text(raw_html)
    assert entry["html_bytes"] == len(raw_html.encode("utf-8"))


def test_delayed_deletion_removes_the_html_companion_too(tmp_path, monkeypatch):
    """When a doc is finally deleted after MISSING_CONFIRM_RUNS consecutive
    runs of not being discovered, its .html companion must go with it --
    not linger as an orphaned file with no corresponding .md/manifest
    entry."""
    docs_root = tmp_path / "docs"
    (docs_root / "wecom").mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: {})  # doc 1 has vanished
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)
    monkeypatch.setattr(fw, "fetch_one_doc", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("nothing to fetch -- discovery is empty")
    ))

    (docs_root / "wecom" / "1.md").write_text("# doc 1\n", encoding="utf-8")
    (docs_root / "wecom" / "1.html").write_text("<div>x</div>", encoding="utf-8")
    fw.write_json_atomic(
        fw.MANIFEST_PATH,
        {
            "files": {
                "wecom/1.md": {
                    "doc_id": "1", "source": "wecom", "sha256": "abc",
                    # Already missing MISSING_CONFIRM_RUNS - 1 times; this
                    # run's absence should push it over the edge.
                    "missing_since": fw.now_iso(),
                    "missing_run_count": fw.MISSING_CONFIRM_RUNS - 1,
                }
            }
        },
    )

    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py"])
    rc = fw.main()

    assert rc == 1  # "No documents in the resulting manifest" -- the only doc was deleted
    assert not (docs_root / "wecom" / "1.md").exists()
    assert not (docs_root / "wecom" / "1.html").exists()


def test_resume_skips_already_done_docs_and_completes(tmp_path, monkeypatch):
    """End-to-end main() run simulating a checkpoint left by an earlier,
    interrupted attempt: docs 1 and 2 are already marked done, so a
    `--resume` run should only fetch 3, 4, 5, carry the checkpointed 1/2
    entries through untouched, and clear the checkpoint once the source
    fully completes. (All-success on purpose: a mixed-outcome variant lives
    in the dedicated circuit-breaker tests below, since a small sample with
    even one failure is exactly what's supposed to trip the breaker now --
    see min_sample in main().)
    """
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / ".sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)

    source = fw.Source(
        source_id="wecom",
        site_root="https://example.invalid",
        seed_path="/document/path/1",
        sentinel_ids=(),
        doc_path_prefix="/document/path/",
        output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    pages = {
        str(i): fw.DocPage(
            doc_id=str(i),
            label=f"doc{i}",
            sections=("s",),
            url=f"https://example.invalid/document/path/{i}",
            rel_path=f"{i}.md",
        )
        for i in range(1, 6)
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    fetched_ids = []

    def fake_fetch_one_doc(source, page, existing):
        fetched_ids.append(page.doc_id)
        return fake_success_outcome(source, page, f"# doc {page.doc_id}\n")

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)

    doc_ids = sorted(pages.keys())
    # Checkpointed as done must also actually exist on disk -- main() now
    # verifies that on resume rather than trusting the checkpoint blindly
    # (see the stale_keys handling there), so a realistic fixture needs the
    # files, not just the JSON claiming they exist.
    (docs_root / "wecom").mkdir(parents=True, exist_ok=True)
    (docs_root / "wecom" / "1.md").write_text("# doc 1\n", encoding="utf-8")
    (docs_root / "wecom" / "2.md").write_text("# doc 2\n", encoding="utf-8")
    fw.write_json_atomic(
        fw.PROGRESS_PATH,
        {
            "wecom": {
                "run_started_at": fw.now_iso(),
                "discovered_doc_ids": doc_ids,
                "done_doc_ids": ["1", "2"],
                "files": {
                    "wecom/1.md": {"doc_id": "1", "sha256": "seed1", "section": "s"},
                    "wecom/2.md": {"doc_id": "2", "sha256": "seed2", "section": "s"},
                },
                "failed": [],
            }
        },
    )

    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py", "--resume"])
    rc = fw.main()

    assert rc == 0
    assert fetched_ids == ["3", "4", "5"]  # 1 and 2 were skipped, not refetched

    manifest = json.loads(fw.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == fw.MANIFEST_SCHEMA_VERSION
    files = manifest["files"]
    assert set(files.keys()) == {"wecom/1.md", "wecom/2.md", "wecom/3.md", "wecom/4.md", "wecom/5.md"}
    assert files["wecom/1.md"]["sha256"] == "seed1"  # carried through from checkpoint untouched

    # Sync fully completed (ran every discovered doc_id), so the checkpoint
    # must be cleared rather than left around for a future --resume to
    # misread as still in progress.
    assert not fw.PROGRESS_PATH.exists()


def test_resume_refetches_checkpointed_doc_whose_file_is_missing_on_disk(tmp_path, monkeypatch):
    """Regression test: a checkpoint claiming doc 1 is done must not be
    trusted blindly if wecom/1.md doesn't actually exist (hand-edited
    checkpoint, partial checkout, manually deleted file, ...) -- it should
    be re-fetched instead of silently ending up "done" with no content."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    pages = {
        "1": fw.DocPage(
            doc_id="1", label="doc1", sections=("s",),
            url="https://example.invalid/document/path/1", rel_path="1.md",
        ),
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    fetched_ids = []

    def fake_fetch_one_doc(source, page, existing):
        fetched_ids.append(page.doc_id)
        return fake_success_outcome(source, page, "# doc 1\n")

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)

    # Checkpoint claims doc 1 is done, but deliberately don't create
    # wecom/1.md -- that's the drift being tested.
    fw.write_json_atomic(
        fw.PROGRESS_PATH,
        {
            "wecom": {
                "run_started_at": fw.now_iso(),
                "discovered_doc_ids": ["1"],
                "done_doc_ids": ["1"],
                "files": {"wecom/1.md": {"doc_id": "1", "sha256": "seed1", "section": "s"}},
                "failed": [],
            }
        },
    )

    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py", "--resume"])
    rc = fw.main()

    assert rc == 0
    assert fetched_ids == ["1"]  # re-fetched despite being "done" in the checkpoint
    assert (docs_root / "wecom" / "1.md").exists()


def _run_main_with_one_failure_in_ten(tmp_path, monkeypatch, strict_fetch):
    """Shared setup for the two mid-loop circuit-breaker tests below: 10
    docs, 1 fails -- a 10% failure rate, which exceeds FAILURE_RATE_THRESHOLD
    (5%) right as the minimum sample size (10 attempts) is reached."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)
    monkeypatch.setenv("STRICT_FETCH", "1" if strict_fetch else "0")

    source = fw.Source(
        source_id="wecom",
        site_root="https://example.invalid",
        seed_path="/document/path/1",
        sentinel_ids=(),
        doc_path_prefix="/document/path/",
        output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    pages = {
        str(i): fw.DocPage(
            doc_id=str(i), label=f"doc{i}", sections=("s",),
            url=f"https://example.invalid/document/path/{i}", rel_path=f"{i}.md",
        )
        for i in range(1, 11)
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    def fake_fetch_one_doc(source, page, existing):
        if page.doc_id == "5":
            return fw.FetchOutcome(failed=True, error="simulated CAPTCHA-shaped failure")
        return fake_success_outcome(source, page, f"# doc {page.doc_id}\n")

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)
    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py"])
    return fw.main()


def test_mid_sync_failure_spike_pauses_without_error_in_tolerant_mode(tmp_path, monkeypatch):
    rc = _run_main_with_one_failure_in_ten(tmp_path, monkeypatch, strict_fetch=False)

    assert rc == 0  # a paused, resumable sync is not a CI failure in tolerant mode
    assert not fw.MANIFEST_PATH.exists()  # never reached the finalize step
    assert fw.PROGRESS_PATH.exists()  # checkpoint preserved for a future --resume

    checkpoint = json.loads(fw.PROGRESS_PATH.read_text(encoding="utf-8"))
    assert checkpoint["schema_version"] == fw.PROGRESS_SCHEMA_VERSION
    assert len(checkpoint["wecom"]["done_doc_ids"]) == 10


def test_mid_sync_failure_spike_fails_the_job_under_strict_fetch(tmp_path, monkeypatch):
    rc = _run_main_with_one_failure_in_ten(tmp_path, monkeypatch, strict_fetch=True)

    assert rc == 1  # STRICT_FETCH=1 means "tell me about any imperfection"
    assert fw.PROGRESS_PATH.exists()  # still checkpointed, so --resume still works next time


def test_breaker_forgives_an_early_failure_once_it_ages_out_of_the_window(tmp_path, monkeypatch):
    """The breaker measures a sliding window (FAILURE_WINDOW_SIZE), not a
    whole-session average -- a single failure right at the start must not
    keep counting against the run forever once enough later successes have
    pushed it out of the window."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)
    monkeypatch.setenv("STRICT_FETCH", "0")

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    total = fw.FAILURE_WINDOW_SIZE * 3  # comfortably larger than the window
    width = len(str(total))  # main() does doc_ids = sorted(pages.keys()) -- a
    # *string* sort, so ids must be zero-padded for that order to match the
    # intended numeric processing order this test depends on.
    doc_id = lambda i: str(i).zfill(width)
    pages = {
        doc_id(i): fw.DocPage(
            doc_id=doc_id(i), label=f"doc{i}", sections=("s",),
            url=f"https://example.invalid/document/path/{i}", rel_path=f"{doc_id(i)}.md",
        )
        for i in range(1, total + 1)
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    def fake_fetch_one_doc(source, page, existing):
        if page.doc_id == doc_id(1):  # only the very first attempt fails
            return fw.FetchOutcome(failed=True, error="one-off transient error")
        return fake_success_outcome(source, page, f"# doc {page.doc_id}\n")

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)
    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py"])
    rc = fw.main()

    assert rc == 0
    assert fw.MANIFEST_PATH.exists()  # ran to completion, breaker never tripped
    manifest = json.loads(fw.MANIFEST_PATH.read_text(encoding="utf-8"))
    # total - 1: doc 1 failed on its very first-ever attempt (no prior
    # content to carry forward), so it legitimately has no manifest entry --
    # the point of this test is that the *run* completes despite it, not
    # that the failed doc grows one.
    assert len(manifest["files"]) == total - 1


def test_breaker_reacts_quickly_to_a_cascade_after_a_long_clean_streak(tmp_path, monkeypatch):
    """A cumulative whole-session average would need many consecutive
    failures to outweigh a long prior success streak; the sliding window
    should react within roughly a couple of failures instead, since that's
    what actually matters for not hammering a live CAPTCHA wall."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)
    monkeypatch.setenv("STRICT_FETCH", "0")

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    window = fw.FAILURE_WINDOW_SIZE
    n_success = window * 4  # a long clean streak before the cascade starts
    # Minimum consecutive failures for the window's rate to cross the
    # threshold, computed from the real constants rather than hardcoded.
    fails_needed = 0
    while fails_needed / window <= fw.FAILURE_RATE_THRESHOLD:
        fails_needed += 1
    total = n_success + fails_needed + 5  # a few extra the run should never reach
    width = len(str(total))  # see the matching comment in the "ages out of
    # the window" test above -- doc_id order must match numeric i order.
    doc_id = lambda i: str(i).zfill(width)

    pages = {
        doc_id(i): fw.DocPage(
            doc_id=doc_id(i), label=f"doc{i}", sections=("s",),
            url=f"https://example.invalid/document/path/{i}", rel_path=f"{doc_id(i)}.md",
        )
        for i in range(1, total + 1)
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    def fake_fetch_one_doc(source, page, existing):
        if int(page.doc_id) > n_success:  # everything after the clean streak fails
            return fw.FetchOutcome(failed=True, error="simulated CAPTCHA-shaped failure")
        return fake_success_outcome(source, page, f"# doc {page.doc_id}\n")

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)
    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py"])
    rc = fw.main()

    assert rc == 0  # paused tolerantly, not a hard CI failure
    assert not fw.MANIFEST_PATH.exists()  # never reached the finalize step
    checkpoint = json.loads(fw.PROGRESS_PATH.read_text(encoding="utf-8"))
    # Stopped right at the trip point -- not after grinding through every
    # failing doc up to `total`, and nowhere near the ~11-failure point a
    # whole-session cumulative average would have needed here.
    assert len(checkpoint["wecom"]["done_doc_ids"]) == n_success + fails_needed


def test_limit_rejects_non_positive_values(monkeypatch):
    for bad_value in ("0", "-1"):
        monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py", "--limit", bad_value])
        with pytest.raises(SystemExit):
            fw.main()


def test_resume_where_every_doc_was_already_done_still_succeeds(tmp_path, monkeypatch):
    """Regression test: a --resume run whose entire doc_ids set is already
    in the checkpoint's done_doc_ids fetches nothing new this process, but
    must still finish successfully -- `successful_pages` (this invocation's
    count) staying 0 must not be confused with the manifest being empty."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    pages = {
        str(i): fw.DocPage(
            doc_id=str(i), label=f"doc{i}", sections=("s",),
            url=f"https://example.invalid/document/path/{i}", rel_path=f"{i}.md",
        )
        for i in range(1, 4)
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    def fail_if_called(*a, **k):
        raise AssertionError("fetch_one_doc should not be called -- every doc is already done")

    monkeypatch.setattr(fw, "fetch_one_doc", fail_if_called)

    doc_ids = sorted(pages.keys())
    # See the matching comment in test_resume_skips_already_done_docs_and_completes:
    # a checkpointed "done" doc must actually have its file on disk, or main()
    # now (correctly) treats it as stale and re-fetches it.
    (docs_root / "wecom").mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (docs_root / "wecom" / f"{i}.md").write_text(f"# doc {i}\n", encoding="utf-8")
    fw.write_json_atomic(
        fw.PROGRESS_PATH,
        {
            "wecom": {
                "run_started_at": fw.now_iso(),
                "discovered_doc_ids": doc_ids,
                "done_doc_ids": doc_ids,
                "files": {
                    f"wecom/{i}.md": {"doc_id": str(i), "sha256": f"seed{i}", "section": "s"}
                    for i in range(1, 4)
                },
                "failed": [],
            }
        },
    )

    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py", "--resume"])
    rc = fw.main()

    assert rc == 0  # NOT 1 -- would previously false-fail on successful_pages == 0
    manifest = json.loads(fw.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert set(manifest["files"].keys()) == {"wecom/1.md", "wecom/2.md", "wecom/3.md"}
    assert not fw.PROGRESS_PATH.exists()  # fully completed, checkpoint cleared


def test_limit_run_does_not_touch_an_unrelated_real_checkpoint(tmp_path, monkeypatch):
    """Regression test: `--limit` (a low-traffic smoke test, per --help) must
    never delete or overwrite a real in-progress sync's checkpoint, whether
    at startup (the "fresh run" cleanup) or at the end (the "sync fully
    completed" cleanup) -- a --limit run never represents a full attempt."""
    docs_root = tmp_path / "docs"
    docs_root.mkdir(parents=True)
    monkeypatch.setattr(fw, "DOCS_ROOT", docs_root)
    monkeypatch.setattr(fw, "MANIFEST_PATH", docs_root / "docs_manifest.json")
    monkeypatch.setattr(fw, "PROGRESS_PATH", docs_root / "sync_progress.json")
    monkeypatch.setattr(fw, "REQUEST_DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(fw, "REQUEST_DELAY_MAX_SECONDS", 0.0)

    source = fw.Source(
        source_id="wecom", site_root="https://example.invalid",
        seed_path="/document/path/1", sentinel_ids=(),
        doc_path_prefix="/document/path/", output_subdir="wecom",
    )
    monkeypatch.setattr(fw, "load_sources", lambda path: [source])
    monkeypatch.setattr(fw, "check_robots_allowed", lambda *a, **k: True)

    pages = {
        str(i): fw.DocPage(
            doc_id=str(i), label=f"doc{i}", sections=("s",),
            url=f"https://example.invalid/document/path/{i}", rel_path=f"{i}.md",
        )
        for i in range(1, 6)
    }
    monkeypatch.setattr(fw, "discover_doc_pages", lambda src: pages)
    monkeypatch.setattr(fw, "validate_discovery", lambda *a, **k: None)

    def fake_fetch_one_doc(source, page, existing):
        return fake_success_outcome(source, page, f"# doc {page.doc_id}\n")

    monkeypatch.setattr(fw, "fetch_one_doc", fake_fetch_one_doc)

    real_checkpoint = {
        "wecom": {
            "run_started_at": fw.now_iso(),
            "discovered_doc_ids": ["1", "2", "3", "4", "5", "6", "7"],  # an unrelated, larger sync
            "done_doc_ids": ["1"],
            "files": {"wecom/1.md": {"doc_id": "1", "sha256": "real-progress", "section": "s"}},
            "failed": [],
        }
    }
    fw.write_json_atomic(fw.PROGRESS_PATH, real_checkpoint)

    monkeypatch.setattr(sys, "argv", ["fetch_wecom_docs.py", "--limit", "2"])
    rc = fw.main()

    assert rc == 0
    assert fw.PROGRESS_PATH.exists()
    assert json.loads(fw.PROGRESS_PATH.read_text(encoding="utf-8")) == real_checkpoint


def test_cleanup_stale_temp_files_removes_orphans_but_not_real_files(tmp_path):
    docs_root = tmp_path / "docs"
    (docs_root / "wecom").mkdir(parents=True)
    real_md = docs_root / "wecom" / "90664.md"
    real_md.write_text("# real content\n", encoding="utf-8")
    orphan_top = docs_root / ".sync_progress.json.tmp12345"
    orphan_top.write_text("{incomplete", encoding="utf-8")
    orphan_nested = docs_root / "wecom" / ".90664.md.tmp6789"
    orphan_nested.write_text("half-written", encoding="utf-8")

    fw.cleanup_stale_temp_files(docs_root)

    assert real_md.exists()
    assert not orphan_top.exists()
    assert not orphan_nested.exists()
