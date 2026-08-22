"""Offline test for debug_compare_via_api.py's id -> doc_id index parsing.

Everything else in that script is a live network call against an endpoint
outside robots.txt's Allow list, deliberately not exercised in CI -- see
the module docstring in scripts/debug_compare_via_api.py for why this tool
exists and its hand-run-only, low-frequency usage constraint.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import debug_compare_via_api as dc  # noqa: E402


def test_index_entry_regex_extracts_id_doc_id_and_title():
    # A trimmed, realistic snippet of the raw JS data blob every
    # /document/path/<id> page embeds -- not valid standalone JSON (it's
    # embedded inside a larger JS expression), hence the regex approach
    # instead of json.loads.
    blob = (
        '...var docIndex = [{"id":90573,"category_id":91143,"doc_id":10990,'
        '"parent_id":90592,"time":1540799096,"author":"warrenchen",'
        '"type":1,"status":2,"title":"通讯录权限体系","order_id":4096,'
        '"gray_status":0},'
        '{"id":97322,"category_id":97322,"doc_id":43090,'
        '"parent_id":97321,"time":1667892111,"author":"chengzuo",'
        '"type":1,"status":2,"title":"小程序下单","order_id":1024,'
        '"gray_status":0}];...'
    )
    mapping = {
        m.group("id"): {"doc_id": m.group("doc_id"), "title": m.group("title")}
        for m in dc._INDEX_ENTRY_RE.finditer(blob)
    }

    assert mapping["90573"] == {"doc_id": "10990", "title": "通讯录权限体系"}
    assert mapping["97322"] == {"doc_id": "43090", "title": "小程序下单"}
