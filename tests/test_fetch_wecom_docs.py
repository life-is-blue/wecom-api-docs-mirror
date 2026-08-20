"""Offline unit tests for scripts/fetch_wecom_docs.py.

All tests run against saved HTML fixtures in scripts/fixtures/ -- no network
access, so they're safe to run on every commit without touching the real
WeCom site (see the plan's "测试策略": live fetches only happen on the daily
cron / manual sync, not in verify-fetcher CI).
"""

from __future__ import annotations

import sys
from pathlib import Path

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
