"""Sealed evidence authority for owned-Windows UX acceptance runs (#880).

The UX160 acceptance matrix (#804) keeps visual/interaction judgment as a
physical owned-Windows gate. What the runner automates is evidence
*acquisition*: exact build/DPI binding, launch into a required initial
state, golden-path checkpoints, canonical screenshots, geometry/widget
visibility, focus-order and disabled-reason assertions, runtime/GL
capture, and geometry-vs-baseline comparison.

A retained bundle record is evidence, never an acceptance verdict:

- ``verdict`` describes what the runner managed to *capture*
  (``evidence_captured`` / ``capture_incomplete`` / ``capture_failed``),
  never whether the row is accepted.
- ``review_state`` stays ``manual_review_remaining`` whenever the row
  still has manual checklist items — the model rejects a record that
  claims automated evidence discharges human review.
- ``capture_mode`` separates ``owned_windows`` runs from
  ``offscreen_fixture`` self-test bundles; fixture evidence can never
  masquerade as owned-Windows acceptance.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash
from .cad_delegated_provider import _require_iso8601, _seal

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: Runner identity stamped on every record — the harness version is part
#: of the evidence so a harness change never silently rewrites history.
UX_ACCEPTANCE_RUNNER_VERSION = 'ux160-acceptance-run-1'

UxCaptureMode = Literal['owned_windows', 'offscreen_fixture']
"""Where the bundle was produced. ``offscreen_fixture`` is harness
self-test evidence only — never owned-Windows acceptance."""

UxBundleVerdict = Literal[
    'evidence_captured', 'capture_incomplete', 'capture_failed']
"""What the automated lane captured — NOT an acceptance verdict.

- ``evidence_captured``: every requested checkpoint executed; manual
  review items still remain.
- ``capture_incomplete``: the run finished but at least one checkpoint
  was blocked (e.g. navigation refused, dialog never opened).
- ``capture_failed``: the run did not complete (crash/timeout/launch
  failure); whatever partial evidence exists is retained as evidence of
  the failed attempt.
"""

UxCheckpointStatus = Literal['pass', 'finding', 'blocked', 'skipped']
"""One machine-checkable checkpoint outcome. ``finding`` is captured
evidence of a real problem (e.g. overflow), not a capture failure.
``blocked`` means the check could not execute; ``skipped`` marks a
checkpoint that was in scope but intentionally not run this attempt."""

UxReviewState = Literal['manual_review_remaining', 'manual_review_none']
"""Whether human checklist items remain. ``manual_review_none`` is only
valid for a row that declares zero manual items."""

UxBaselineVerdict = Literal['matched', 'diverged', 'not_run']
"""Geometry/baseline comparison outcome for deterministic scalars."""


class UxArtifactRef(BaseModel):
    """One file in the evidence bundle, bound by content hash."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    kind: Literal[
        'screenshot', 'manifest', 'report', 'driver_result',
        'driver_log', 'other',
    ]
    sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_count: int = Field(ge=0)


class UxCheckpointOutcome(BaseModel):
    """One executed checkpoint inside a bundle: status + bound artifacts."""

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    target: str | None = None
    status: UxCheckpointStatus
    detail: str | None = None
    evidence: tuple[UxArtifactRef, ...] = ()


class UxScreenInfo(BaseModel):
    """One display's geometry/DPI facts as observed by Qt."""

    model_config = ConfigDict(frozen=True)

    name: str | None = None
    geometry: tuple[int, int, int, int] | None = None
    available_geometry: tuple[int, int, int, int] | None = None
    logical_dpi: float | None = None
    device_pixel_ratio: float | None = None


class UxEnvironmentBinding(BaseModel):
    """Exact producing-runtime identity for a bundle (#833 pattern).

    ``None``/``'unknown'`` values stay as recorded — the probe result is
    evidence either way and is never silently resolved.
    """

    model_config = ConfigDict(frozen=True)

    build_version: str = Field(min_length=1)
    build_display_version: str = Field(min_length=1)
    build_commit_sha: str | None = None
    build_dirty: bool = False
    build_source: str = 'unknown'
    python_version: str | None = None
    python_implementation: str | None = None
    platform: str | None = None
    machine: str | None = None
    os_version: str | None = None
    environment_fingerprint: str | None = None
    lock_file_path: str | None = None
    lock_file_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    qt_version: str | None = None
    pyside_version: str | None = None
    qpa_platform: str | None = None
    qt_scale_factor_env: str | None = None
    renderer_id: str | None = None
    screens: tuple[UxScreenInfo, ...] = ()


class UxManualChecklistItem(BaseModel):
    """One review item only a human can answer — never auto-resolved."""

    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requires_human: bool = True


class CadUxAcceptanceBundleRecord(BaseModel):
    """Sealed record retaining one matrix-row evidence bundle.

    This record asserts *that evidence exists and is intact* — it is not
    a UX160 acceptance claim. A row is accepted only when a human closes
    out ``manual_items`` on the owned-Windows surface.
    """

    model_config = ConfigDict(frozen=True)

    bundle_id: str = Field(min_length=1)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)

    matrix_id: str = Field(min_length=1)
    row_id: str = Field(min_length=1)
    run_attempt: int = Field(ge=1)
    scenario: str = Field(min_length=1)
    scale_factor: str = Field(min_length=1)
    capture_mode: UxCaptureMode
    runner_version: str = Field(min_length=1)

    environment: UxEnvironmentBinding

    verdict: UxBundleVerdict
    review_state: UxReviewState
    checkpoints: tuple[UxCheckpointOutcome, ...] = ()
    checkpoints_total: int = Field(ge=0)
    checkpoints_passed: int = Field(ge=0)
    checkpoints_finding: int = Field(ge=0)
    checkpoints_blocked: int = Field(ge=0)
    checkpoints_skipped: int = Field(ge=0)
    failure_detail: str | None = None

    # On-disk bundle binding — ``bundle_ref`` is the deterministic
    # workspace-relative path (``<matrix>/<row>/attempt-<NNN>``).
    bundle_ref: str = Field(min_length=1)
    manifest_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    artifacts: tuple[UxArtifactRef, ...] = ()

    manual_items: tuple[UxManualChecklistItem, ...] = ()

    baseline_ref: AuthorityRef | None = None
    baseline_verdict: UxBaselineVerdict = 'not_run'
    baseline_divergences: tuple[str, ...] = ()

    non_claims: tuple[str, ...] = ()

    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CadUxAcceptanceBundleRecord':
        _require_iso8601(self.started_at_utc, 'started_at_utc')
        _require_iso8601(self.finished_at_utc, 'finished_at_utc')

        # Checkpoint counts must agree with the embedded outcomes.
        counts = {'pass': 0, 'finding': 0, 'blocked': 0, 'skipped': 0}
        for checkpoint in self.checkpoints:
            counts[checkpoint.status] += 1
        if self.checkpoints_total != len(self.checkpoints):
            raise ValueError('checkpoints_total disagrees with checkpoints')
        if self.checkpoints_passed != counts['pass']:
            raise ValueError('checkpoints_passed disagrees with checkpoints')
        if self.checkpoints_finding != counts['finding']:
            raise ValueError('checkpoints_finding disagrees with checkpoints')
        if self.checkpoints_blocked != counts['blocked']:
            raise ValueError('checkpoints_blocked disagrees with checkpoints')
        if self.checkpoints_skipped != counts['skipped']:
            raise ValueError('checkpoints_skipped disagrees with checkpoints')

        # Verdict honesty: a captured-evidence verdict requires every
        # emitted checkpoint to have actually executed; a failed run must
        # say why.
        if self.verdict == 'evidence_captured' and (
                self.checkpoints_blocked or self.checkpoints_skipped):
            raise ValueError(
                'evidence_captured requires zero blocked/skipped '
                'checkpoints')
        if self.verdict == 'capture_failed' and not (
                self.failure_detail and self.failure_detail.strip()):
            raise ValueError('capture_failed requires failure_detail')

        # Manual review is structural: automated evidence can never
        # discharge a human checklist item.
        if self.manual_items:
            if self.review_state != 'manual_review_remaining':
                raise ValueError(
                    'rows with manual items must remain '
                    'manual_review_remaining')
        elif self.review_state != 'manual_review_none':
            raise ValueError(
                'rows without manual items must record '
                'manual_review_none')

        # Fixture captures must say so — an offscreen bundle carrying a
        # hardware claim is overclaim, rejected at record validation.
        if self.capture_mode == 'offscreen_fixture':
            claims = '\n'.join(self.non_claims)
            if 'offscreen' not in claims:
                raise ValueError(
                    'offscreen_fixture records must carry an offscreen '
                    'non-claim')

        if self.bundle_sha256 != _hash(self.identity_payload()):
            raise ValueError('ux acceptance bundle hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'matrix_id': self.matrix_id,
            'row_id': self.row_id,
            'run_attempt': self.run_attempt,
            'scenario': self.scenario,
            'scale_factor': self.scale_factor,
            'capture_mode': self.capture_mode,
            'runner_version': self.runner_version,
            'environment': self.environment.model_dump(mode='json'),
            'verdict': self.verdict,
            'review_state': self.review_state,
            'checkpoints': [
                c.model_dump(mode='json') for c in self.checkpoints],
            'failure_detail': self.failure_detail,
            'bundle_ref': self.bundle_ref,
            'manifest_sha256': self.manifest_sha256,
            'report_sha256': self.report_sha256,
            'artifacts': [a.model_dump(mode='json') for a in self.artifacts],
            'manual_items': [
                m.model_dump(mode='json') for m in self.manual_items],
            'baseline_ref': (
                self.baseline_ref.model_dump(mode='json')
                if self.baseline_ref is not None else None
            ),
            'baseline_verdict': self.baseline_verdict,
            'baseline_divergences': list(self.baseline_divergences),
            'non_claims': list(self.non_claims),
            'started_at_utc': self.started_at_utc,
            'finished_at_utc': self.finished_at_utc,
        }


def derive_bundle_verdict(
    checkpoints: tuple[UxCheckpointOutcome, ...],
    *,
    driver_completed: bool,
    failure_detail: str | None = None,
) -> UxBundleVerdict:
    """Fail-closed verdict over emitted checkpoints.

    ``finding`` outcomes never downgrade the verdict — a finding is
    captured evidence, which is the point of the run. Anything not
    executed is ``capture_incomplete``; a dead driver is
    ``capture_failed``.
    """

    if not driver_completed:
        return 'capture_failed'
    if any(c.status in ('blocked', 'skipped') for c in checkpoints):
        return 'capture_incomplete'
    return 'evidence_captured'


def derive_review_state(
    manual_items: tuple[UxManualChecklistItem, ...],
) -> UxReviewState:
    return (
        'manual_review_remaining' if manual_items else 'manual_review_none')


def build_ux_acceptance_bundle_record(
    *,
    document_id: str,
    matrix_id: str,
    row_id: str,
    run_attempt: int,
    scenario: str,
    scale_factor: str,
    capture_mode: UxCaptureMode,
    environment: UxEnvironmentBinding | dict[str, Any],
    verdict: UxBundleVerdict,
    checkpoints: tuple[UxCheckpointOutcome, ...] | list[UxCheckpointOutcome],
    bundle_ref: str,
    manifest_sha256: str | None = None,
    report_sha256: str | None = None,
    artifacts: tuple[UxArtifactRef, ...] | list[UxArtifactRef] = (),
    manual_items: (
        tuple[UxManualChecklistItem, ...] | list[UxManualChecklistItem]
    ) = (),
    baseline_ref: AuthorityRef | None = None,
    baseline_verdict: UxBaselineVerdict = 'not_run',
    baseline_divergences: tuple[str, ...] | list[str] = (),
    failure_detail: str | None = None,
    non_claims: tuple[str, ...] | list[str] = (),
    runner_version: str = UX_ACCEPTANCE_RUNNER_VERSION,
    started_at_utc: str,
    finished_at_utc: str,
) -> CadUxAcceptanceBundleRecord:
    """Seal one bundle record; counts and review state derive here so a
    caller can never seal inconsistent tallies."""

    checkpoints = tuple(checkpoints)
    counts = {'pass': 0, 'finding': 0, 'blocked': 0, 'skipped': 0}
    for checkpoint in checkpoints:
        counts[checkpoint.status] += 1
    manual_items = tuple(manual_items)
    return _seal(
        CadUxAcceptanceBundleRecord,
        {
            'document_id': document_id,
            'matrix_id': matrix_id,
            'row_id': row_id,
            'run_attempt': run_attempt,
            'scenario': scenario,
            'scale_factor': scale_factor,
            'capture_mode': capture_mode,
            'runner_version': runner_version,
            'environment': (
                environment.model_dump(mode='python')
                if isinstance(environment, UxEnvironmentBinding)
                else environment
            ),
            'verdict': verdict,
            'review_state': derive_review_state(manual_items),
            'checkpoints': [c.model_dump(mode='python') for c in checkpoints],
            'checkpoints_total': len(checkpoints),
            'checkpoints_passed': counts['pass'],
            'checkpoints_finding': counts['finding'],
            'checkpoints_blocked': counts['blocked'],
            'checkpoints_skipped': counts['skipped'],
            'failure_detail': failure_detail,
            'bundle_ref': bundle_ref,
            'manifest_sha256': manifest_sha256,
            'report_sha256': report_sha256,
            'artifacts': [a.model_dump(mode='python') for a in artifacts],
            'manual_items': [
                m.model_dump(mode='python') for m in manual_items],
            'baseline_ref': (
                baseline_ref.model_dump(mode='python')
                if baseline_ref is not None else None
            ),
            'baseline_verdict': baseline_verdict,
            'baseline_divergences': list(baseline_divergences),
            'non_claims': list(non_claims),
            'started_at_utc': started_at_utc,
            'finished_at_utc': finished_at_utc,
        },
        'bundle_id', 'bundle_sha256', 'uxbnd',
    )


__all__ = [
    'CadUxAcceptanceBundleRecord',
    'UX_ACCEPTANCE_RUNNER_VERSION',
    'UxArtifactRef',
    'UxBaselineVerdict',
    'UxBundleVerdict',
    'UxCaptureMode',
    'UxCheckpointOutcome',
    'UxCheckpointStatus',
    'UxEnvironmentBinding',
    'UxManualChecklistItem',
    'UxReviewState',
    'UxScreenInfo',
    'build_ux_acceptance_bundle_record',
    'derive_bundle_verdict',
    'derive_review_state',
]
