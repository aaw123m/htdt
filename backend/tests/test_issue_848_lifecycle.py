"""#848 — evidence-aware issue lifecycle classification + drift check.

GitHub open/closed is binary; HTDT's completion model is not. The
lifecycle manifest records what landed and which gate remains; the
checker must catch contradictions without inferring physical/manual
acceptance from software tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lc_mod = _load('issue_lifecycle', ROOT / 'scripts' / 'issue_lifecycle.py')

LIFECYCLE_MANIFEST = ROOT / 'scripts' / 'issue_lifecycle_manifest.yaml'


def _entry(**over):
    defaults = dict(
        issue=1, lifecycle='in_progress', prs=(), commits=(),
        remaining_gates=(), evidence_refs=(), close_when_gates_clear=True,
        superseded_by=None,
    )
    return lc_mod.LifecycleEntry(**(defaults | over))


# --- manifest parses fail-closed -----------------------------------------


def test_real_lifecycle_manifest_parses() -> None:
    entries = lc_mod.load_lifecycle_manifest(LIFECYCLE_MANIFEST)
    assert len(entries) >= 20
    # The six issues #848 names must be classifiable without commit history.
    for issue in (804, 805, 806, 807, 815, 833):
        assert issue in entries
        assert entries[issue].has_landed_refs


def test_manifest_rejects_unknown_lifecycle(tmp_path: Path) -> None:
    bad = tmp_path / 'bad.yaml'
    bad.write_text(
        'version: 1\nentries:\n- issue: 1\n  lifecycle: mostly_done\n',
        encoding='utf-8',
    )
    with pytest.raises(SystemExit, match='unknown lifecycle'):
        lc_mod.load_lifecycle_manifest(bad)


def test_manifest_rejects_unknown_gate_kind(tmp_path: Path) -> None:
    bad = tmp_path / 'bad.yaml'
    bad.write_text(
        'version: 1\nentries:\n- issue: 1\n  lifecycle: in_progress\n'
        '  remaining_gates:\n  - {kind: vibes, description: x}\n',
        encoding='utf-8',
    )
    with pytest.raises(SystemExit, match='gate kind'):
        lc_mod.load_lifecycle_manifest(bad)


# --- drift findings --------------------------------------------------------


def test_landed_state_requires_refs() -> None:
    findings = lc_mod.drift_findings(
        {1: _entry(lifecycle='software_landed')}, {},
    )
    assert any(f.code == 'landed_without_refs' for f in findings)


def test_complete_cannot_have_gates() -> None:
    findings = lc_mod.drift_findings(
        {1: _entry(
            lifecycle='complete', prs=(1,),
            remaining_gates=(('manual', 'x'),))},
        {},
    )
    assert any(f.code == 'complete_with_gates' for f in findings)


def test_closed_github_issue_must_not_be_active() -> None:
    findings = lc_mod.drift_findings(
        {1: _entry(lifecycle='in_progress')}, {},
        github_states={1: 'closed'},
    )
    assert any(f.code == 'closed_but_active' for f in findings)


def test_verification_issue_without_lifecycle_is_unclassified() -> None:
    findings = lc_mod.drift_findings(
        {1: _entry()}, {2: {'issue': 2}},
    )
    assert any(f.code == 'unclassified' and f.issue == 2 for f in findings)


def test_landed_with_no_gate_should_be_complete() -> None:
    findings = lc_mod.drift_findings(
        {1: _entry(lifecycle='software_landed', prs=(5,))}, {},
    )
    assert any(f.code == 'landed_no_gate_not_complete' for f in findings)


# --- classification --------------------------------------------------------


def test_green_software_never_implies_completion() -> None:
    cls = lc_mod.classify_issue(
        _entry(lifecycle='acceptance_remaining', prs=(1,),
               remaining_gates=(('manual', '実機受理'),)),
        check_statuses=('passed', 'passed'),
    )
    assert cls.bucket == 'gate_remaining'


def test_missing_implementation_vs_red_implementation() -> None:
    missing = lc_mod.classify_issue(
        _entry(lifecycle='in_progress'), check_statuses=('failed',))
    red = lc_mod.classify_issue(
        _entry(lifecycle='software_landed', prs=(3,),
               remaining_gates=(('manual', 'x'),)),
        check_statuses=('failed',))
    assert missing.bucket == 'implementation_missing'
    assert red.bucket == 'implementation_present_checks_red'


def test_complete_is_closeable() -> None:
    cls = lc_mod.classify_issue(
        _entry(lifecycle='complete', prs=(1,)), check_statuses=('passed',))
    assert cls.bucket == 'closeable'


def test_real_manifest_has_no_error_findings() -> None:
    entries = lc_mod.load_lifecycle_manifest(LIFECYCLE_MANIFEST)
    verification = lc_mod.load_verification_issues(
        ROOT / 'scripts' / 'issue_verification_manifest.yaml')
    findings = lc_mod.drift_findings(entries, verification, {})
    errors = [f for f in findings if f.severity == 'error']
    assert errors == []


def test_named_issues_classify_to_expected_buckets() -> None:
    entries = lc_mod.load_lifecycle_manifest(LIFECYCLE_MANIFEST)
    expected = {
        804: 'gate_remaining',   # UX160 manual rows
        805: 'gate_remaining',   # structural follow-up
        806: 'gate_remaining',
        807: 'gate_remaining',
        815: 'gate_remaining',
        833: 'closeable',        # software gate landed
    }
    for issue, bucket in expected.items():
        cls = lc_mod.classify_issue(
            entries[issue], check_statuses=('passed',))
        assert cls.bucket == bucket, (issue, cls)
