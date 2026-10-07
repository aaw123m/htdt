# -*- coding: utf-8 -*-
"""Tests for the migration-traceability reference validator (issue #803).

Covers: manifest parsing, ambiguity detection, unavailable/superseded
marking, merge-sha evidence, and a synthetic doc pass/fail matrix.
The validator lives at scripts/validate_canonical_references.py and is
imported here as a module (functions, not script-only).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import scripts.validate_canonical_references as vcr  # noqa: E402

LEGACY = "bolph71656-ai/Home-Theater-Digital-Twin"
CAPLEG = "bolph71656-ai/HTDT-Capture"
CUR = "ka0923s-a11y/HTDT"

VALID_KINDS = {"issue", "pull_request", "ci_run", "release_artifact",
               "diagnostic_run"}
VALID_STATUS = {"current", "historical", "imported", "unavailable",
                "superseded"}


# ----------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def manifest():
    return vcr.load_manifest(ROOT / vcr.MANIFEST_NAME)


@pytest.fixture()
def mini_manifest():
    """Synthetic manifest for unit tests — independent of the real one."""
    return {
        "doc": {"migration": {"current_repo": CUR}},
        "refs": {
            (LEGACY, 63): [{"kind": "issue", "status": "historical",
                            "original_number": 63}],
            (LEGACY, 64): [{"kind": "pull_request", "status": "historical",
                            "original_number": 64,
                            "merge_sha": "2ca8755b66af5521c2ed4fc98d39ce1aeb732c64"}],
            (LEGACY, 37): [{"kind": "pull_request", "status": "unavailable",
                            "original_number": 37}],
            (LEGACY, 76): [{"kind": "pull_request", "status": "superseded",
                            "original_number": 76}],
            (LEGACY, 83): [{"kind": "issue", "status": "imported",
                            "original_number": 83,
                            "current_mapping": {"repo": CUR, "issue": 1}}],
            (LEGACY, 512): [{"kind": "issue", "status": "historical",
                             "original_number": 512},
                            {"kind": "ci_run", "status": "unavailable",
                             "original_number": 512}],
            (CUR, 538): [{"kind": "issue", "status": "current",
                          "original_number": 538}],
            (CUR, 1): [{"kind": "issue", "status": "current",
                        "original_number": 1}],
            (LEGACY, 538): [{"kind": "issue", "status": "historical",
                             "original_number": 538}],
        },
    }


# ------------------------------------------------------------- manifest spec

def test_manifest_loads_and_covers_required_fields(manifest):
    doc = manifest["doc"]
    assert doc["migration"]["current_repo"] == CUR
    assert doc["migration"]["original_repo"] == LEGACY
    assert doc["references"], "manifest must list references"
    for e in doc["references"]:
        assert e["kind"] in VALID_KINDS
        assert e["status"] in VALID_STATUS
        assert isinstance(e["original_number"], int)
        if e["status"] == "imported":
            assert e.get("current_mapping", {}).get("issue"), (
                "imported refs must map to a current issue number")


def test_manifest_no_duplicate_identities(manifest):
    keys = [(e["original_repo"], e["original_number"], e["kind"])
            for e in manifest["doc"]["references"]]
    assert len(keys) == len(set(keys))


def test_manifest_records_collisions(manifest):
    collisions = manifest["doc"]["number_collisions"]
    nums = {c["number"] for c in collisions}
    # verified real collisions across the migration boundary
    assert {140, 432, 538, 792, 803} <= nums


def test_imported_issues_have_current_mapping(manifest):
    imported = {e["original_number"]: e["current_mapping"]["issue"]
                for e in manifest["doc"]["references"]
                if e["status"] == "imported" and e["kind"] == "issue"}
    # spot-check the verified import map
    assert imported[83] == 1
    assert imported[101] == 2
    assert imported[118] == 3
    assert imported[792] == 64
    assert imported[1084] == 304


# --------------------------------------------------------------- scan syntax

def test_bare_reference_fails(mini_manifest):
    problems = vcr.validate_text("Issue #63 done.", "t.md", mini_manifest)
    assert any("bare reference #63" in p for p in problems)


def test_qualified_reference_passes(mini_manifest):
    assert vcr.validate_text(
        "Issue %s#63 done." % LEGACY, "t.md", mini_manifest) == []


def test_chain_members_inherit_repo(mini_manifest):
    ok = vcr.validate_text(
        "Issue %s#63/#83 done." % LEGACY, "t.md", mini_manifest)
    assert ok == []


def test_range_members_inherit_repo(mini_manifest):
    ok = vcr.validate_text(
        "Issue %s#63–#83 done." % LEGACY, "t.md", mini_manifest)
    assert ok == []


def test_orphan_chain_member_fails(mini_manifest):
    problems = vcr.validate_text(
        "see /#63 for details", "t.md", mini_manifest)
    assert any("chain member #63" in p for p in problems)


def test_bare_run_syntax_fails(mini_manifest):
    for bad in ("run #76", "Run #76", "workflow run #4"):
        problems = vcr.validate_text(bad, "t.md", mini_manifest)
        assert problems, bad


def test_dash_run_syntax_passes(mini_manifest):
    assert vcr.validate_text("run-76 pin confirmed", "t.md",
                             mini_manifest) == []


def test_unregistered_reference_fails(mini_manifest):
    problems = vcr.validate_text(
        "Issue %s#999 done." % LEGACY, "t.md", mini_manifest)
    assert any("not in manifest" in p for p in problems)


def test_link_target_not_scanned(mini_manifest):
    # #N inside a ](...) link target is a URL anchor, not a ref
    assert vcr.validate_text(
        "[x](docs/a.md#4d-foo)", "t.md", mini_manifest) == []


# -------------------------------------------------------- merge-sha evidence

def test_merge_claim_requires_sha(mini_manifest):
    problems = vcr.validate_text(
        "PR %s#64 landed." % LEGACY, "t.md", mini_manifest)
    assert any("merge claim without recorded sha" in p for p in problems)


def test_merge_claim_accepts_short_sha(mini_manifest):
    assert vcr.validate_text(
        "PR %s#64 (merge 2ca8755b) landed." % LEGACY, "t.md",
        mini_manifest) == []


def test_merge_claim_accepts_inline_full_sha(mini_manifest):
    assert vcr.validate_text(
        "PR %s#64 merge `2ca8755b66af5521c2ed4fc98d39ce1aeb732c64`." % LEGACY,
        "t.md", mini_manifest) == []


def test_merge_sha_mismatch_fails(mini_manifest):
    problems = vcr.validate_text(
        "PR %s#64 (merge deadbeef) landed." % LEGACY, "t.md", mini_manifest)
    assert any("merge sha mismatch" in p for p in problems)


# --------------------------------------------------------- status markers

def test_unavailable_ref_requires_marker(mini_manifest):
    problems = vcr.validate_text(
        "PR %s#37 policy." % LEGACY, "t.md", mini_manifest)
    assert any("unavailable ref presented without marker" in p
               for p in problems)


def test_unavailable_ref_with_marker_passes(mini_manifest):
    assert vcr.validate_text(
        "PR %s#37 (unavailable) policy." % LEGACY, "t.md",
        mini_manifest) == []


def test_superseded_ref_requires_marker(mini_manifest):
    problems = vcr.validate_text(
        "PR %s#76 merged." % LEGACY, "t.md", mini_manifest)
    assert any("superseded ref presented without marker" in p
               for p in problems)


def test_superseded_ref_with_marker_passes(mini_manifest):
    assert vcr.validate_text(
        "PR %s#76 (superseded) history." % LEGACY, "t.md",
        mini_manifest) == []


# -------------------------------------------------------- kind disambiguation

def test_ambiguous_number_needs_kind_hint(mini_manifest):
    problems = vcr.validate_text(
        "**%s#512 Analysis export**" % LEGACY, "t.md", mini_manifest)
    assert any("ambiguous kind" in p for p in problems)


def test_issue_hint_resolves_ambiguous_number(mini_manifest):
    assert vcr.validate_text(
        "Issue %s#512 Analysis export" % LEGACY, "t.md",
        mini_manifest) == []


def test_ci_hint_resolves_but_needs_unavailable_marker(mini_manifest):
    problems = vcr.validate_text(
        "CI %s#512 PASS" % LEGACY, "t.md", mini_manifest)
    assert problems == [] or any("unavailable" in p for p in problems)
    # strict model: unavailable CI record must carry the marker
    assert any("unavailable ref presented without marker" in p
               for p in problems)


def test_same_number_different_repo_resolves_independently(mini_manifest):
    assert vcr.validate_text(
        "Issue %s#538 cable runs / Issue %s#538 auralization" % (
            LEGACY, CUR), "t.md", mini_manifest) == []


# -------------------------------------------------------- synthetic doc set

def test_synthetic_doc_matrix(mini_manifest):
    cases = [
        ("Issue %s#63" % LEGACY, 0),
        ("Issue #63", 1),
        ("PR %s#64 (merge 2ca8755b)" % LEGACY, 0),
        ("PR %s#64" % LEGACY, 1),
        ("PR %s#37 (unavailable)" % LEGACY, 0),
        ("PR %s#37" % LEGACY, 1),
        ("Issue %s#83 owned-room" % LEGACY, 0),
        ("Issue %s#83 -> imported to %s#1" % (LEGACY, CUR), 0),
        ("run #76 diagnostic", 1),
        ("run-76 diagnostic", 0),
        ("see %s#999" % LEGACY, 1),
    ]
    for text, expected in cases:
        problems = vcr.validate_text(text, "s.md", mini_manifest)
        assert (len(problems) > 0) == bool(expected), (text, problems)


# --------------------------------------------------------- real corpus gate

def test_canonical_docs_clean_against_manifest():
    """The real docs must validate — this is the whole point of #803."""
    problems = vcr.validate_all(ROOT)
    assert problems == [], "canonical docs have %d violations:\n%s" % (
        len(problems), "\n".join(problems[:20]))


def test_dead_original_repo_links_flagged(mini_manifest):
    text = "see https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/83"
    # validate_text catches raw URLs? no — validate_all does via _URL_RE.
    # emulate the validate_all dead-link check directly:
    m = vcr._URL_RE.search(text)
    assert m is not None and "bolph71656-ai" in m.group(1)
