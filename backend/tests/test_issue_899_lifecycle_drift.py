"""Issue #899 — lifecycle drift regression: software gaps vs physical gates.

A ``physical_evidence_remaining``/``acceptance_remaining`` state with only
physical/manual gates asserts "the software is complete; only acceptance
or hardware evidence is missing". When a landed issue still has
unimplemented software (e.g. the ``WasapiAudioBackend`` stub behind #869,
the IFC/GLB intake + Room-workspace wiring behind #866, the missing
operator command path behind #868), the entry must record a ``structural``
gate — and its state must not claim only physical/acceptance work
remains.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'scripts'))

import issue_lifecycle as lc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / 'scripts' / 'issue_lifecycle_manifest.yaml'

#: Issues whose remaining work was verified on main to include
#: unimplemented software (not merely acceptance/evidence gathering).
#: Issues whose manifest entries MUST keep a ``structural`` remaining gate
#: because unimplemented software is still tracked there. Empty once the
#: last verified gap (#868 commissioning command path) landed via #1028 —
#: re-add an issue id here when a new verified software gap appears.
STRUCTURAL_ISSUES: set[int] = set()

#: States that assert nothing but acceptance/physical work is left; they
#: are dishonest when a ``structural`` gate is present.
NON_STRUCTURAL_LANDED_STATES = {
    'software_landed',
    'acceptance_remaining',
    'physical_evidence_remaining',
    'complete',
}


@pytest.fixture(scope='module')
def entries() -> dict[int, lc.LifecycleEntry]:
    return lc.load_lifecycle_manifest(MANIFEST)


def test_manifest_parses_and_gate_vocabulary_holds(
    entries: dict[int, lc.LifecycleEntry],
) -> None:
    assert entries, 'lifecycle manifest must not be empty'
    for entry in entries.values():
        assert entry.lifecycle in lc.LIFECYCLE_STATES
        for kind, description in entry.remaining_gates:
            assert kind in lc.GATE_KINDS
            assert description.strip()


def test_verified_software_gaps_record_structural_gate(
    entries: dict[int, lc.LifecycleEntry],
) -> None:
    for issue in sorted(STRUCTURAL_ISSUES):
        entry = entries.get(issue)
        assert entry is not None, f'#{issue} has no lifecycle entry'
        kinds = {kind for kind, _ in entry.remaining_gates}
        assert 'structural' in kinds, (
            f'#{issue} still has unimplemented software (verified on main) '
            f'but records no structural gate: {sorted(kinds)}'
        )
        # The structural gap must be split from the physical/manual
        # acceptance — a single collapsed gate hides which is which.
        assert kinds & {'physical', 'manual'}, (
            f'#{issue} lost its physical/manual acceptance gate'
        )


def test_structural_gate_forbids_physical_only_state(
    entries: dict[int, lc.LifecycleEntry],
) -> None:
    for entry in entries.values():
        kinds = {kind for kind, _ in entry.remaining_gates}
        if 'structural' not in kinds:
            continue
        assert entry.lifecycle not in NON_STRUCTURAL_LANDED_STATES, (
            f'#{entry.issue} has a structural (software-gap) gate but '
            f'lifecycle {entry.lifecycle!r} claims only non-software work '
            f'remains — use structural_followup_remaining'
        )


def test_landed_entries_keep_exact_refs(
    entries: dict[int, lc.LifecycleEntry],
) -> None:
    """Corrections must not regress landed credit: every landed state
    keeps its PR/commit evidence and resolvable refs."""
    for entry in entries.values():
        if entry.lifecycle not in lc.LANDED_STATES:
            continue
        assert entry.has_landed_refs, (
            f'#{entry.issue} is {entry.lifecycle} with no landed refs'
        )
        assert entry.evidence_refs, (
            f'#{entry.issue} landed with no evidence_refs'
        )
