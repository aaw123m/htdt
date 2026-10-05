"""REV55-CORRQUAL: cross-cutting correction qualification gate (issue #568).

A generated or imported room-correction filter is evidence *derived from*
other authorities — the EQP10 design policy (#201-adjacent spatial design
evidence), the FIR10 artifact, and the calibration plan. This module defines
the qualification evidence contract: under what honest evidence may HTDT
state that a correction is *qualified* for a declared listening region on a
declared playback system, and under what evidence must it instead report a
smaller claim or none at all.

Evidence ladder (increasing strength)::

    INSUFFICIENT_EVIDENCE → DESIGN_ONLY → SIMULATED
        → MEASURED_AT_CONTROL_POINTS → SPATIALLY_HOLDOUT_VERIFIED
        → DEPLOYED_AND_REMEASURED

    QUALIFIED_WITH_LIMITATIONS — a qualified rung reached with recorded
        limitations (e.g. reduced verification coverage, shallow holdout,
        banded capability margin).
    INCOMPATIBLE — hard failure of a capability or safety gate
        (over-boost, out-of-band, headroom or pre-ringing excess,
        regressions at measured positions, deployment mismatch).

Qualification scope (what the evidence honestly supports)::

    unqualified → candidate → qualified_point → qualified_region

    'qualified_region' requires spatially independent holdout verification;
    a correction measured only at its own design positions may honestly
    claim 'qualified_point' — never 'qualified_region'.

Fail-closed design rules (mechanically enforced, not advisory):

* gates may only demote a claim, never promote it
* an unobservable device state or undeclared capability leaves the gate
  'not_evaluated' or 'limitation' — never 'pass'
* holdout positions are a distinct declared set; when declared they must be
  re-measured post-correction to support any region claim
* repeatability positions may never satisfy spatial holdout (an unchanged
  speaker re-measured proves stability, not spatial generalization)
* subjective listening evidence is recorded but can never open a gate
* a material change to the filter (new coefficients), the target, the
  device, or the routing invalidates any record pinned to the old identity:
  records bind ``subject_id`` + ``semantic_sha256``, and lookups by
  ``current_for_correction`` reject stale hashes
* no synthesized pass: 'observed_match' requires an explicit
  DeploymentObservation; 'improved' deltas require a physical post set

Literature basis (issue #568 + investigation):

* Kirkeby & Nelson 1999 (JAES 47(7/8)): inversion requires regularization;
  a deep spectral null cannot be undone by unbounded boost — enforced as
  the boost gate bound from SpatialCorrectionEvidence local-null caps.
* Cecchi et al. 2018 (Applied Sciences 8(1),16): single-point EQ's spatial
  fragility, mixed-phase pre-ringing, out-of-band correction risks —
  enforced as the holdout gate, the pre-ringing gate, and the usable-band
  gate respectively.
* Stefanakis/Sarris/Jacobsen multi-point regularization work: control-set
  and holdout separation — enforced as the mechanical partition of the
  declared listening region.
* Multi-point EQ safety practice (per the REV55 investigation): ~6 dB boost
  ceiling and headroom margin are declared constraints, not defaults — the
  gate never invents a limit the operator did not declare.
"""

from __future__ import annotations

from math import isfinite
from statistics import mean, pvariance
from typing import Any, Literal, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import CadCalibrationPlan, CadTargetCurve, evaluate_biquad_db
from .cad_correction_design_policy import (
    CorrectionDesignPolicy,
    CorrectionRegularization,
    SpatialCorrectionEvidence,
    _interpolate_db,
)
from .cad_equipment import FrequencyDomain
from .cad_fir_filter import FIRFilterArtifact, evaluate_fir_artifact
from .cad_prediction_measurement_registration import (
    ComparabilityState,
    RegistrationPartition,
)
from .canonical_json import canonical_sha256 as _digest

CORRQUAL_SCHEMA_VERSION = 1
QUALIFICATION_AUTHORITY_VERSION = 'corrqual-qualification-1'
QUALIFICATION_EVALUATOR_VERSION = 'corrqual-evaluator-1'

_CORRECTION_BAND_MIN_HZ = 10.0
_IMPROVEMENT_EPSILON_DB = 1e-9
_ENERGY_EPSILON = 1e-18


def _normalize_identifier(value: str) -> str:
    return ' '.join(value.strip().split()).lower()


def _ensure_finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


def _sha_or_none(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if len(value) != 64:
        raise ValueError(f'{field_name} must be 64 hex characters')
    int(value, 16)
    return value


def _hash_payload(payload: dict[str, Any]) -> str:
    return _digest(payload)


CorrectionEvidenceKind = Literal[
    'physical_measurement',
    'simulated_prediction',
    'dsp_backend_observation',
    'subjective_listening',
]

CorrectionQualificationGate = Literal[
    'identity',
    'boost_cut',
    'usable_band',
    'headroom',
    'pre_ringing',
    'control_fidelity',
    'holdout_independence',
    'closed_loop_deployment',
]
_GATE_ORDER: tuple[CorrectionQualificationGate, ...] = (
    'identity',
    'boost_cut',
    'usable_band',
    'headroom',
    'pre_ringing',
    'control_fidelity',
    'holdout_independence',
    'closed_loop_deployment',
)

CorrectionQualificationState = Literal[
    'INSUFFICIENT_EVIDENCE',
    'DESIGN_ONLY',
    'SIMULATED',
    'MEASURED_AT_CONTROL_POINTS',
    'SPATIALLY_HOLDOUT_VERIFIED',
    'DEPLOYED_AND_REMEASURED',
    'QUALIFIED_WITH_LIMITATIONS',
    'INCOMPATIBLE',
]
_QUALIFICATION_STATE_ORDER: dict[CorrectionQualificationState, int] = {
    'INSUFFICIENT_EVIDENCE': 0,
    'DESIGN_ONLY': 1,
    'SIMULATED': 2,
    'MEASURED_AT_CONTROL_POINTS': 3,
    'SPATIALLY_HOLDOUT_VERIFIED': 4,
    'DEPLOYED_AND_REMEASURED': 5,
    'QUALIFIED_WITH_LIMITATIONS': 5,
    'INCOMPATIBLE': -1,
}

CorrectionQualificationScope = Literal[
    'unqualified',
    'candidate',
    'qualified_point',
    'qualified_region',
]
_SCOPE_ORDER: dict[CorrectionQualificationScope, int] = {
    'unqualified': 0,
    'candidate': 1,
    'qualified_point': 2,
    'qualified_region': 3,
}

CorrectionGateStatus = Literal['pass', 'limitation', 'fail', 'not_evaluated']
CorrectionObservableStatus = Literal[
    'improved', 'regressed', 'unchanged', 'within_limits',
    'exceeded', 'not_evaluated', 'unknown',
]
CorrectionObservable = Literal[
    'target_deviation',
    'seat_to_seat_variance',
    'worst_seat_deviation',
    'group_delay',
    'pre_ringing',
    'latency',
    'peak_boost',
    'headroom_margin',
    'capability_margin',
    'before_after_measurement',
    'deployed_state_match',
    'repeatability',
]
_OBSERVABLE_DOMAINS: dict[CorrectionObservable, str] = {
    'target_deviation': 'frequency',
    'seat_to_seat_variance': 'spatial',
    'worst_seat_deviation': 'frequency',
    'group_delay': 'time_phase',
    'pre_ringing': 'time_phase',
    'latency': 'system_stress',
    'peak_boost': 'system_stress',
    'headroom_margin': 'system_stress',
    'capability_margin': 'system_stress',
    'before_after_measurement': 'physical_verification',
    'deployed_state_match': 'physical_verification',
    'repeatability': 'physical_verification',
}

# Japanese UI labels — every state/scope/gate status is honest and specific.
QUALIFICATION_STATE_LABELS: dict[str, str] = {
    'INSUFFICIENT_EVIDENCE': '証拠不足',
    'DESIGN_ONLY': '設計のみ',
    'SIMULATED': 'シミュレーション済み',
    'MEASURED_AT_CONTROL_POINTS': '制御点実測済み',
    'SPATIALLY_HOLDOUT_VERIFIED': '空間ホールドアウト検証済み',
    'DEPLOYED_AND_REMEASURED': 'デプロイ済み・再測定済み',
    'QUALIFIED_WITH_LIMITATIONS': '修飾済み（制限あり）',
    'INCOMPATIBLE': '不適合',
}
QUALIFICATION_SCOPE_LABELS: dict[str, str] = {
    'unqualified': '未修飾',
    'candidate': '候補',
    'qualified_point': '単一点修飾済み',
    'qualified_region': '領域修飾済み',
}
GATE_STATUS_LABELS: dict[str, str] = {
    'pass': '通過',
    'limitation': '制限あり',
    'fail': '不合格',
    'not_evaluated': '未評価',
}


def qualification_state_label(state: CorrectionQualificationState) -> str:
    return QUALIFICATION_STATE_LABELS[state]


def qualification_scope_label(scope: CorrectionQualificationScope) -> str:
    return QUALIFICATION_SCOPE_LABELS[scope]


def gate_status_label(status: CorrectionGateStatus) -> str:
    return GATE_STATUS_LABELS[status]


class ListeningRegion(BaseModel):
    """Declares which positions belong to which evidence partition.

    The partition is the mechanical separation the holdout gate enforces:
    design positions drive the filter design, holdout positions are the
    spatially-independent verification set, and repeatability positions
    answer "is the speaker stable" — which can never answer "does the
    correction generalize across the region".
    """

    model_config = ConfigDict(frozen=True)

    design_position_ids: tuple[str, ...]
    holdout_position_ids: tuple[str, ...] = ()
    repeatability_position_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_region(self) -> 'ListeningRegion':
        for name, values in (
            ('design_position_ids', self.design_position_ids),
            ('holdout_position_ids', self.holdout_position_ids),
            ('repeatability_position_ids', self.repeatability_position_ids),
        ):
            normalized = {_normalize_identifier(value) for value in values}
            if len(normalized) != len(values):
                raise ValueError(f'{name} must be unique')
        design = {_normalize_identifier(value) for value in self.design_position_ids}
        if not design:
            raise ValueError('design_position_ids must not be empty')
        holdout = {_normalize_identifier(value) for value in self.holdout_position_ids}
        repeatability = {
            _normalize_identifier(value) for value in self.repeatability_position_ids
        }
        if design & holdout:
            raise ValueError('design and holdout positions must be disjoint')
        if holdout & repeatability:
            raise ValueError('holdout and repeatability positions must be disjoint')
        # design ∩ repeatability is allowed: re-measuring a design seat is
        # exactly what repeatability evidence is — it proves stability of
        # the chain, never spatial generalization.
        return self


class ConstraintDeclaration(BaseModel):
    """Operator-declared constraint surfaces — None is honest unknown.

    A constraint that was never declared is not a waived constraint: the
    corresponding gate evaluates to 'not_evaluated', and the record keeps a
    limitation. The evaluator never invents a bound.
    """

    model_config = ConfigDict(frozen=True)

    max_boost_db: float | None = None
    max_cut_db: float | None = None
    usable_band_hz: FrequencyDomain | None = None
    headroom_db: float | None = None
    max_pre_ringing_ratio: float | None = None
    max_latency_s: float | None = None

    @model_validator(mode='after')
    def valid_constraints(self) -> 'ConstraintDeclaration':
        for name, value in (
            ('max_boost_db', self.max_boost_db),
            ('max_cut_db', self.max_cut_db),
            ('headroom_db', self.headroom_db),
            ('max_pre_ringing_ratio', self.max_pre_ringing_ratio),
            ('max_latency_s', self.max_latency_s),
        ):
            if value is None:
                continue
            _ensure_finite(value, name)
            if value < 0:
                raise ValueError(f'{name} must be >= 0')
        return self


class CorrectionSubjectRef(BaseModel):
    """Identity pin for the correction under qualification."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['calibration_plan', 'fir_artifact']
    subject_id: str = Field(min_length=1)
    subject_sha256: str = Field(min_length=64, max_length=64)
    topology: Literal['peq', 'fir', 'unknown'] = 'unknown'
    regularization: CorrectionRegularization | None = None
    regularization_source: Literal[
        'eqp10_policy', 'declared', 'none'
    ] = 'none'

    @model_validator(mode='after')
    def valid_subject(self) -> 'CorrectionSubjectRef':
        int(self.subject_sha256, 16)
        if self.regularization is None and self.regularization_source != 'none':
            raise ValueError(
                'regularization_source must be none when no regularization is pinned'
            )
        if self.regularization is not None and self.regularization_source == 'none':
            raise ValueError(
                'regularization pinned without declaring its source'
            )
        return self


class PositionResponseSet(BaseModel):
    """One homogeneous measured-or-predicted set of position responses.

    ``evidence_kind`` is what keeps simulated predictions honest: a post set
    built from the solver's predicted responses can drive the SIMULATED rung
    but can never open control_fidelity or holdout_independence.
    """

    model_config = ConfigDict(frozen=True)

    evidence_kind: Literal['physical_measurement', 'simulated_prediction']
    set_id: str | None = None
    set_sha256: str | None = None
    position_ids: tuple[str, ...]
    # frequency_hz[i] aligns with magnitudes_db[i] for every sample.
    # Each inner tuple is one position's magnitude-vs-frequency curve.
    samples_frequencies_hz: tuple[tuple[float, ...], ...]
    samples_magnitudes_db: tuple[tuple[float, ...], ...]

    @model_validator(mode='after')
    def valid_set(self) -> 'PositionResponseSet':
        if self.set_id is None and self.set_sha256 is not None:
            raise ValueError('set_sha256 requires set_id')
        if self.set_id is not None and self.set_sha256 is None:
            raise ValueError('set_sha256 required when set_id is pinned')
        _sha_or_none(self.set_sha256, 'set_sha256')
        if len(self.position_ids) != len(self.samples_frequencies_hz):
            raise ValueError('position_ids must align with samples_frequencies_hz')
        if len(self.position_ids) != len(self.samples_magnitudes_db):
            raise ValueError('position_ids must align with samples_magnitudes_db')
        normalized = {_normalize_identifier(value) for value in self.position_ids}
        if len(normalized) != len(self.position_ids):
            raise ValueError('position_ids must be unique')
        for index, (freqs, mags) in enumerate(
            zip(self.samples_frequencies_hz, self.samples_magnitudes_db)
        ):
            if len(freqs) != len(mags):
                raise ValueError(
                    f'sample {index}: frequencies/magnitudes must align'
                )
            if len(freqs) < 2:
                raise ValueError(f'sample {index}: at least two points required')
            for f in freqs:
                _ensure_finite(f, f'sample {index} frequency')
                if f <= 0:
                    raise ValueError(f'sample {index}: frequencies must be > 0')
            for a, b in zip(freqs, freqs[1:]):
                if b <= a:
                    raise ValueError(
                        f'sample {index}: frequencies must be strictly increasing'
                    )
            for m in mags:
                _ensure_finite(m, f'sample {index} magnitude')
        return self


class RegistrationEvidence(BaseModel):
    """Pinned #564 registration for one compared evidence pair.

    The qualification evaluator never recomputes comparability — it binds
    the registration by identity and reads its sealed comparability verdict.
    When no registration is bound the record keeps ``comparability=None``:
    honest unregistered comparison.
    """

    model_config = ConfigDict(frozen=True)

    registration_id: str = Field(min_length=1)
    semantic_sha256: str = Field(min_length=64, max_length=64)
    partition: RegistrationPartition
    comparability: ComparabilityState

    @model_validator(mode='after')
    def valid_registration(self) -> 'RegistrationEvidence':
        int(self.semantic_sha256, 16)
        return self


class DeploymentObservation(BaseModel):
    """Observed deployed correction state — what the device actually holds.

    'observed_match' is only meaningful when something read the applied
    state back (e.g. the CamillaDSP read-back contract); it can never be
    derived from the command succeeding.
    """

    model_config = ConfigDict(frozen=True)

    state: Literal[
        'observed_match', 'observed_mismatch', 'not_observable', 'not_evaluated'
    ]
    observed_ref_id: str | None = None
    observed_ref_sha256: str | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def valid_observation(self) -> 'DeploymentObservation':
        if self.observed_ref_sha256 is not None:
            _sha_or_none(self.observed_ref_sha256, 'observed_ref_sha256')
            if self.observed_ref_id is None:
                raise ValueError('observed_ref_sha256 requires observed_ref_id')
        if self.state == 'observed_match' and self.observed_ref_id is None:
            raise ValueError(
                'observed_match requires pinning the observed deployed reference'
            )
        return self


class SubjectiveEvidenceRef(BaseModel):
    """Listening-impression evidence — recorded, never gating."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    note: str | None = None


class CorrectionGateResult(BaseModel):
    """Outcome of one qualification gate."""

    model_config = ConfigDict(frozen=True)

    gate: CorrectionQualificationGate
    status: CorrectionGateStatus
    reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_result(self) -> 'CorrectionGateResult':
        if self.status == 'fail' and not self.reasons:
            raise ValueError('fail status requires reasons')
        return self


class CorrectionObservableResult(BaseModel):
    """One reported observable — an independent axis, never a verdict."""

    model_config = ConfigDict(frozen=True)

    observable: CorrectionObservable
    status: CorrectionObservableStatus
    value: float | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def valid_observable(self) -> 'CorrectionObservableResult':
        if self.value is not None:
            _ensure_finite(self.value, 'value')
        return self


class CorrectionQualificationRecord(BaseModel):
    """Sealed immutable qualification record for one correction identity.

    The record is the evidence contract: every claim it makes is either a
    gate verdict computed by the evaluator or an honest absence. Persisted
    rows re-validate on read, so a payload whose declared state, scope, or
    sealed hash no longer matches its content fails closed.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = CORRQUAL_SCHEMA_VERSION
    authority_version: str = QUALIFICATION_AUTHORITY_VERSION
    evaluator_version: str = QUALIFICATION_EVALUATOR_VERSION
    qualification_id: str = Field(min_length=1)
    semantic_sha256: str = Field(min_length=64, max_length=64)
    recorded_at_utc: str = Field(min_length=1)

    document_id: str | None = None
    scene_revision_id: str | None = None
    system_variant_id: str | None = None
    system_variant_sha256: str | None = None

    subject: CorrectionSubjectRef
    correction_bands: tuple[tuple[float, float], ...] = ()
    target: CadTargetCurve | None = None
    region: ListeningRegion
    constraints: ConstraintDeclaration
    baseline: PositionResponseSet | None = None
    post: PositionResponseSet | None = None
    registrations: tuple[RegistrationEvidence, ...] = ()
    deployment: DeploymentObservation | None = None
    subjective_evidence: tuple[SubjectiveEvidenceRef, ...] = ()

    design_policy_id: str | None = None
    design_policy_sha256: str | None = None
    spatial_evidence_id: str | None = None
    spatial_evidence_sha256: str | None = None

    gates: tuple[CorrectionGateResult, ...]
    observables: tuple[CorrectionObservableResult, ...]
    evidence_kinds: tuple[CorrectionEvidenceKind, ...]
    state: CorrectionQualificationState
    scope: CorrectionQualificationScope
    limitations: tuple[str, ...] = ()
    disqualifiers: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_record(self) -> 'CorrectionQualificationRecord':
        if self.schema_version != CORRQUAL_SCHEMA_VERSION:
            raise ValueError('unsupported correction qualification schema')
        if self.authority_version != QUALIFICATION_AUTHORITY_VERSION:
            raise ValueError('unsupported qualification authority version')
        if self.evaluator_version != QUALIFICATION_EVALUATOR_VERSION:
            raise ValueError('unsupported qualification evaluator version')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('CorrectionQualificationRecord hash mismatch')
        expected_id = f'correction-qualification:{self.semantic_sha256}'
        if self.qualification_id != expected_id:
            raise ValueError('CorrectionQualificationRecord id mismatch')
        for name, value in (
            ('system_variant_sha256', self.system_variant_sha256),
            ('design_policy_sha256', self.design_policy_sha256),
            ('spatial_evidence_sha256', self.spatial_evidence_sha256),
        ):
            _sha_or_none(value, name)
        if self.system_variant_sha256 is not None and self.system_variant_id is None:
            raise ValueError('system_variant_sha256 requires system_variant_id')
        if self.design_policy_sha256 is not None and self.design_policy_id is None:
            raise ValueError('design_policy_sha256 requires design_policy_id')
        if self.spatial_evidence_sha256 is not None and self.spatial_evidence_id is None:
            raise ValueError('spatial_evidence_sha256 requires spatial_evidence_id')
        gate_names = tuple(result.gate for result in self.gates)
        if gate_names != _GATE_ORDER:
            raise ValueError('gates must cover every gate exactly once, in order')
        if self.state == 'INCOMPATIBLE' and not self.disqualifiers:
            raise ValueError('INCOMPATIBLE requires disqualifier reasons')
        if self.state == 'QUALIFIED_WITH_LIMITATIONS' and not self.limitations:
            raise ValueError('QUALIFIED_WITH_LIMITATIONS requires limitations')
        state_scope = expected_scope_for(
            gates=self.gates,
            region=self.region,
            post=self.post,
            deployment=self.deployment,
            evidence_kinds=self.evidence_kinds,
            state=self.state,
        )
        if self.scope != state_scope:
            raise ValueError(
                f'scope {self.scope} inconsistent with gate evidence '
                f'(expected {state_scope})'
            )
        if self.scope == 'qualified_region':
            if not self.region.holdout_position_ids:
                raise ValueError('qualified_region requires declared holdout positions')
            if self.post is None:
                raise ValueError('qualified_region requires a post set')
            holdout = {
                _normalize_identifier(value)
                for value in self.region.holdout_position_ids
            }
            measured = {
                _normalize_identifier(value) for value in self.post.position_ids
            }
            if not holdout <= measured:
                raise ValueError(
                    'qualified_region requires post measurement at every '
                    'declared holdout position'
                )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'evaluator_version': self.evaluator_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'subject': self.subject.model_dump(mode='json'),
            'correction_bands': [list(pair) for pair in self.correction_bands],
            'target': (
                self.target.model_dump(mode='json') if self.target is not None else None
            ),
            'region': self.region.model_dump(mode='json'),
            'constraints': self.constraints.model_dump(mode='json'),
            'baseline': (
                self.baseline.model_dump(mode='json')
                if self.baseline is not None else None
            ),
            'post': (
                self.post.model_dump(mode='json') if self.post is not None else None
            ),
            'registrations': [r.model_dump(mode='json') for r in self.registrations],
            'deployment': (
                self.deployment.model_dump(mode='json')
                if self.deployment is not None else None
            ),
            'subjective_evidence': [
                item.model_dump(mode='json') for item in self.subjective_evidence
            ],
            'design_policy_id': self.design_policy_id,
            'design_policy_sha256': self.design_policy_sha256,
            'spatial_evidence_id': self.spatial_evidence_id,
            'spatial_evidence_sha256': self.spatial_evidence_sha256,
            'gates': [gate.model_dump(mode='json') for gate in self.gates],
            'observables': [
                observable.model_dump(mode='json')
                for observable in self.observables
            ],
            'evidence_kinds': list(self.evidence_kinds),
            'state': self.state,
            'scope': self.scope,
            'limitations': list(self.limitations),
            'disqualifiers': list(self.disqualifiers),
        }

    def gate(self, name: CorrectionQualificationGate) -> CorrectionGateResult:
        for result in self.gates:
            if result.gate == name:
                return result
        raise KeyError(name)


def expected_scope_for(
    *,
    gates: tuple[CorrectionGateResult, ...],
    region: ListeningRegion,
    post: PositionResponseSet | None,
    deployment: DeploymentObservation | None,
    evidence_kinds: tuple[CorrectionEvidenceKind, ...],
    state: CorrectionQualificationState,
) -> CorrectionQualificationScope:
    """Recompute the honest scope from gates — never a stored claim alone."""

    by_gate = {result.gate: result for result in gates}
    if any(result.status == 'fail' for result in gates):
        return 'unqualified'
    if state in ('INCOMPATIBLE', 'INSUFFICIENT_EVIDENCE'):
        return 'unqualified'
    if state in ('DESIGN_ONLY', 'SIMULATED'):
        return 'candidate'
    holdout_gate = by_gate.get('holdout_independence')
    control_gate = by_gate.get('control_fidelity')
    if (
        holdout_gate is not None
        and holdout_gate.status == 'pass'
        and region.holdout_position_ids
        and post is not None
        and post.evidence_kind == 'physical_measurement'
    ):
        holdout = {
            _normalize_identifier(value)
            for value in region.holdout_position_ids
        }
        measured = {_normalize_identifier(value) for value in post.position_ids}
        if holdout <= measured:
            return 'qualified_region'
    if control_gate is not None and control_gate.status == 'pass':
        return 'qualified_point'
    return 'candidate'


def _sample_map(samples: PositionResponseSet) -> dict[str, tuple[tuple[float, ...], tuple[float, ...]]]:
    return {
        _normalize_identifier(position_id): (freqs, mags)
        for position_id, freqs, mags in zip(
            samples.position_ids,
            samples.samples_frequencies_hz,
            samples.samples_magnitudes_db,
        )
    }


def _residual_db(
    target: CadTargetCurve,
    frequencies_hz: tuple[float, ...],
    magnitudes_db: tuple[float, ...],
    bands: tuple[tuple[float, float], ...],
) -> float:
    """Mean |measured - target| over sample points inside correction bands."""

    target_points = tuple(
        (point.frequency_hz, point.level_db) for point in target.points
    )
    deviations: list[float] = []
    for f, magnitude in zip(frequencies_hz, magnitudes_db):
        if bands and not any(low <= f <= high for low, high in bands):
            continue
        target_db = _interpolate_db(target_points, f)
        deviations.append(abs(magnitude - (target_db or 0.0)))
    if not deviations:
        return float('nan')
    return mean(deviations)


def _correction_response_envelopes(
    subject: CorrectionSubjectRef,
    plan: CadCalibrationPlan | None,
    fir: FIRFilterArtifact | None,
    frequencies_hz: tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """Per-frequency (max, min) correction gain across channels.

    For a calibration plan: each channel's cascade of gain + every peq;
    the worst channel drives each side of the envelope. For a FIR
    artifact: the artifact's response is both envelopes. Returns None
    when the subject cannot be evaluated.
    """

    if plan is not None:
        max_response: list[float] | None = None
        min_response: list[float] | None = None
        for channel in plan.channels:
            channel_response = [
                channel.gain_db + sum(
                    evaluate_biquad_db(peq, f) for peq in channel.peq
                )
                for f in frequencies_hz
            ]
            if max_response is None:
                max_response = list(channel_response)
                min_response = list(channel_response)
            else:
                max_response = [
                    max(a, b) for a, b in zip(max_response, channel_response)
                ]
                min_response = [
                    min(a, b) for a, b in zip(min_response, channel_response)
                ]
        if max_response is None or min_response is None:
            return None
        return tuple(max_response), tuple(min_response)
    if fir is not None:
        diagnostics = evaluate_fir_artifact(fir, list(frequencies_hz))
        response = tuple(float(value) for value in diagnostics.magnitude_db)
        return response, response
    return None


def _peak_boost_cut(
    envelopes: tuple[tuple[float, ...], tuple[float, ...]] | None,
) -> tuple[float | None, float | None]:
    if envelopes is None or not envelopes[0]:
        return None, None
    max_response, min_response = envelopes
    boost = max(max_response)
    cut = -min(min_response)
    return max(boost, 0.0), max(cut, 0.0)


def evaluate_correction_qualification(
    *,
    subject: CorrectionSubjectRef,
    region: ListeningRegion,
    constraints: ConstraintDeclaration | None = None,
    target: CadTargetCurve | None = None,
    correction_bands: tuple[tuple[float, float], ...] = (),
    plan: CadCalibrationPlan | None = None,
    fir: FIRFilterArtifact | None = None,
    design_policy: CorrectionDesignPolicy | None = None,
    spatial_evidence: SpatialCorrectionEvidence | None = None,
    baseline: PositionResponseSet | None = None,
    post: PositionResponseSet | None = None,
    registrations: tuple[RegistrationEvidence, ...] = (),
    deployment: DeploymentObservation | None = None,
    subjective_evidence: tuple[SubjectiveEvidenceRef, ...] = (),
    document_id: str | None = None,
    scene_revision_id: str | None = None,
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    recorded_at_utc: str,
) -> CorrectionQualificationRecord:
    """Run every qualification gate and seal the resulting record.

    The evaluator is pure: same inputs → same record → same sealed
    ``qualification_id``. Any inconsistency between the pinned subject
    identity and the bound authorities fails loudly (``ValueError``) before
    a record is built.
    """

    constraints = constraints or ConstraintDeclaration()
    deployment = deployment or DeploymentObservation(state='not_evaluated')

    # -- consistency checks (fail loudly, never a gate) ------------------
    if plan is not None and fir is not None:
        raise ValueError('subject cannot be both calibration plan and fir artifact')
    if plan is not None:
        if subject.kind != 'calibration_plan':
            raise ValueError('subject kind mismatch with bound plan')
        if plan.plan_id != subject.subject_id:
            raise ValueError('subject_id does not match bound plan')
        if plan.plan_semantic_sha256 != subject.subject_sha256:
            raise ValueError('subject_sha256 does not match bound plan')
        if subject.topology not in ('peq',):
            raise ValueError('calibration plan subjects are peq topology')
    if fir is not None:
        if subject.kind != 'fir_artifact':
            raise ValueError('subject kind mismatch with bound fir artifact')
        if fir.artifact_id != subject.subject_id:
            raise ValueError('subject_id does not match bound fir artifact')
        if fir.semantic_sha256 != subject.subject_sha256:
            raise ValueError('subject_sha256 does not match bound fir artifact')
        if subject.topology not in ('fir',):
            raise ValueError('fir artifact subjects are fir topology')
    if design_policy is not None:
        if spatial_evidence is not None:
            if spatial_evidence.policy_id != design_policy.policy_id:
                raise ValueError('spatial evidence pins a different design policy')
            if spatial_evidence.policy_sha256 != design_policy.semantic_sha256:
                raise ValueError('spatial evidence pins a different policy sha')
        if (
            subject.regularization is not None
            and design_policy.regularization != subject.regularization
        ):
            raise ValueError(
                'subject regularization diverges from bound design policy — '
                'different regularization is different evidence identity'
            )
    declared_positions = (
        {_normalize_identifier(value) for value in region.design_position_ids}
        | {_normalize_identifier(value) for value in region.holdout_position_ids}
        | {
            _normalize_identifier(value)
            for value in region.repeatability_position_ids
        }
    )
    for label, response_set in (('baseline', baseline), ('post', post)):
        if response_set is None:
            continue
        unknown = {
            _normalize_identifier(value) for value in response_set.position_ids
        } - declared_positions
        if unknown:
            raise ValueError(
                f'{label} set contains undeclared positions: {sorted(unknown)}'
            )

    # -- identity gate ----------------------------------------------------
    gates: list[CorrectionGateResult] = []
    disqualifiers: list[str] = []
    limitations: list[str] = []
    if plan is not None and plan.support_state != 'SUPPORTED':
        gates.append(CorrectionGateResult(
            gate='identity',
            status='fail',
            reasons=(
                'bound calibration plan is UNSUPPORTED: '
                + '; '.join(plan.unsupported_reasons),
            ),
        ))
        disqualifiers.append('subject plan unsupported')
    else:
        gates.append(CorrectionGateResult(gate='identity', status='pass'))

    # -- correction response on the evaluation grid -----------------------
    if correction_bands:
        for low, high in correction_bands:
            _ensure_finite(low, 'correction band low')
            _ensure_finite(high, 'correction band high')
            if low >= high:
                raise ValueError('correction bands must be rising')
        for a, b in zip(correction_bands, correction_bands[1:]):
            if b[0] <= a[1]:
                raise ValueError('correction bands must not overlap')
        eval_low = min(low for low, _ in correction_bands)
        eval_high = max(high for _, high in correction_bands)
    elif target is not None:
        eval_low = target.points[0].frequency_hz
        eval_high = target.points[-1].frequency_hz
    elif plan is not None and plan.channels:
        freqs = sorted(
            peq.frequency_hz
            for channel in plan.channels
            for peq in channel.peq
        )
        eval_low = max(_CORRECTION_BAND_MIN_HZ, freqs[0] * 0.5) if freqs else 20.0
        eval_high = freqs[-1] * 2.0 if freqs else 200.0
    elif fir is not None:
        eval_low = 20.0
        eval_high = min(20000.0, fir.sample_rate_hz / 2.0 - 1.0)
    else:
        eval_low = eval_high = 0.0
    if eval_low >= eval_high:
        grid: tuple[float, ...] = ()
    else:
        grid = tuple(
            float(v)
            for v in np.geomspace(eval_low, eval_high, 64)
        )
    envelopes = _correction_response_envelopes(subject, plan, fir, grid)
    peak_boost_db, peak_cut_db = _peak_boost_cut(envelopes)

    # -- boost/cut gate ----------------------------------------------------
    boost_failures: list[str] = []
    boost_checked = False
    if envelopes is None:
        gates.append(CorrectionGateResult(
            gate='boost_cut',
            status='not_evaluated',
            reasons=('correction response not computable',),
        ))
        limitations.append('boost/cut unverifiable')
    if spatial_evidence is not None and envelopes is not None and grid:
        boost_checked = True
        max_response, min_response = envelopes
        for band in spatial_evidence.bands:
            band_boosts = [
                resp for f, resp in zip(grid, max_response)
                if band.low_hz <= f <= band.high_hz
            ]
            band_cuts = [
                resp for f, resp in zip(grid, min_response)
                if band.low_hz <= f <= band.high_hz
            ]
            if not band_boosts:
                continue
            band_max = max(band_boosts)
            band_min = min(band_cuts)
            if band_max > band.max_allowed_boost_db + _IMPROVEMENT_EPSILON_DB:
                boost_failures.append(
                    f'boost {band_max:.2f} dB exceeds allowed '
                    f'{band.max_allowed_boost_db:.2f} dB in '
                    f'{band.low_hz:.1f}-{band.high_hz:.1f} Hz '
                    f'({band.classification})'
                )
            if -band_min > band.max_allowed_cut_db + _IMPROVEMENT_EPSILON_DB:
                boost_failures.append(
                    f'cut {-band_min:.2f} dB exceeds allowed '
                    f'{band.max_allowed_cut_db:.2f} dB in '
                    f'{band.low_hz:.1f}-{band.high_hz:.1f} Hz '
                    f'({band.classification})'
                )
    if envelopes is not None:
        if constraints.max_boost_db is not None:
            boost_checked = True
            if (
                peak_boost_db is not None
                and peak_boost_db > constraints.max_boost_db + _IMPROVEMENT_EPSILON_DB
            ):
                boost_failures.append(
                    f'peak boost {peak_boost_db:.2f} dB exceeds declared '
                    f'{constraints.max_boost_db:.2f} dB'
                )
        if constraints.max_cut_db is not None:
            boost_checked = True
            if (
                peak_cut_db is not None
                and peak_cut_db > constraints.max_cut_db + _IMPROVEMENT_EPSILON_DB
            ):
                boost_failures.append(
                    f'peak cut {peak_cut_db:.2f} dB exceeds declared '
                    f'{constraints.max_cut_db:.2f} dB'
                )
        if plan is not None:
            device = plan.device_constraints
            if device.max_boost_db is not None:
                boost_checked = True
                if (
                    peak_boost_db is not None
                    and peak_boost_db > device.max_boost_db + _IMPROVEMENT_EPSILON_DB
                ):
                    boost_failures.append(
                        f'peak boost {peak_boost_db:.2f} dB exceeds device '
                        f'capability {device.max_boost_db:.2f} dB'
                    )
            if device.max_cut_db is not None:
                boost_checked = True
                if (
                    peak_cut_db is not None
                    and peak_cut_db > device.max_cut_db + _IMPROVEMENT_EPSILON_DB
                ):
                    boost_failures.append(
                        f'peak cut {peak_cut_db:.2f} dB exceeds device '
                        f'capability {device.max_cut_db:.2f} dB'
                    )
    if boost_failures:
        gates.append(CorrectionGateResult(
            gate='boost_cut', status='fail', reasons=tuple(boost_failures)
        ))
        disqualifiers.extend(boost_failures)
    elif envelopes is None:
        pass  # already recorded not_evaluated above
    elif boost_checked:
        gates.append(CorrectionGateResult(gate='boost_cut', status='pass'))
    else:
        gates.append(CorrectionGateResult(
            gate='boost_cut',
            status='not_evaluated',
            reasons=('no declared or evidence-derived boost/cut bound',),
        ))
        limitations.append('no boost/cut bound declared')

    # -- usable band gate --------------------------------------------------
    band_failures: list[str] = []
    band_limitations: list[str] = []
    if constraints.usable_band_hz is None:
        gates.append(CorrectionGateResult(
            gate='usable_band',
            status='not_evaluated',
            reasons=('no usable speaker band declared',),
        ))
        limitations.append('usable band undeclared')
    elif not correction_bands:
        gates.append(CorrectionGateResult(
            gate='usable_band',
            status='not_evaluated',
            reasons=('no correction bands declared',),
        ))
        limitations.append('correction bands undeclared')
    else:
        usable = constraints.usable_band_hz
        for low, high in correction_bands:
            if low >= usable.minimum_hz and high <= usable.maximum_hz:
                continue
            if high <= usable.minimum_hz or low >= usable.maximum_hz:
                band_failures.append(
                    f'correction band {low:.1f}-{high:.1f} Hz fully outside '
                    f'usable band {usable.minimum_hz:.1f}-'
                    f'{usable.maximum_hz:.1f} Hz'
                )
            else:
                band_limitations.append(
                    f'correction band {low:.1f}-{high:.1f} Hz partially '
                    f'outside usable band {usable.minimum_hz:.1f}-'
                    f'{usable.maximum_hz:.1f} Hz'
                )
        if band_failures:
            gates.append(CorrectionGateResult(
                gate='usable_band', status='fail', reasons=tuple(band_failures)
            ))
            disqualifiers.extend(band_failures)
        elif band_limitations:
            gates.append(CorrectionGateResult(
                gate='usable_band',
                status='limitation',
                reasons=tuple(band_limitations),
            ))
            limitations.extend(band_limitations)
        else:
            gates.append(CorrectionGateResult(gate='usable_band', status='pass'))

    # -- headroom gate ------------------------------------------------------
    if constraints.headroom_db is None:
        gates.append(CorrectionGateResult(
            gate='headroom',
            status='not_evaluated',
            reasons=('no headroom authority declared',),
        ))
        limitations.append('headroom undeclared')
    elif peak_boost_db is None:
        gates.append(CorrectionGateResult(
            gate='headroom',
            status='not_evaluated',
            reasons=('correction response not computable',),
        ))
        limitations.append('headroom unverifiable')
    elif peak_boost_db > constraints.headroom_db + _IMPROVEMENT_EPSILON_DB:
        gates.append(CorrectionGateResult(
            gate='headroom',
            status='fail',
            reasons=(
                f'peak boost {peak_boost_db:.2f} dB exceeds declared headroom '
                f'{constraints.headroom_db:.2f} dB',
            ),
        ))
        disqualifiers.append(
            f'headroom {constraints.headroom_db:.2f} dB < required '
            f'{peak_boost_db:.2f} dB'
        )
    else:
        gates.append(CorrectionGateResult(gate='headroom', status='pass'))

    # -- pre-ringing gate ---------------------------------------------------
    pre_ringing_ratio: float | None = None
    latency_s: float | None = None
    group_delay_max_s: float | None = None
    if fir is not None and grid:
        diagnostics = evaluate_fir_artifact(fir, list(grid))
        total_energy = (
            diagnostics.pre_ringing_energy + diagnostics.post_ringing_energy
        )
        if total_energy > _ENERGY_EPSILON:
            pre_ringing_ratio = (
                diagnostics.pre_ringing_energy / total_energy
            )
        latency_s = diagnostics.total_latency_s
        group_delay_max_s = (
            float(max(abs(value) for value in diagnostics.group_delay_s))
            if len(diagnostics.group_delay_s) else None
        )
        pre_failures: list[str] = []
        if (
            pre_ringing_ratio is not None
            and constraints.max_pre_ringing_ratio is not None
            and pre_ringing_ratio
            > constraints.max_pre_ringing_ratio + _IMPROVEMENT_EPSILON_DB
        ):
            pre_failures.append(
                f'pre-ringing ratio {pre_ringing_ratio:.4f} exceeds '
                f'declared {constraints.max_pre_ringing_ratio:.4f}'
            )
        if (
            latency_s is not None
            and constraints.max_latency_s is not None
            and latency_s > constraints.max_latency_s + _IMPROVEMENT_EPSILON_DB
        ):
            pre_failures.append(
                f'latency {latency_s:.6f} s exceeds declared '
                f'{constraints.max_latency_s:.6f} s'
            )
        if pre_failures:
            gates.append(CorrectionGateResult(
                gate='pre_ringing', status='fail',
                reasons=tuple(pre_failures),
            ))
            disqualifiers.extend(pre_failures)
        elif (
            constraints.max_pre_ringing_ratio is None
            and pre_ringing_ratio is not None
            and pre_ringing_ratio > _ENERGY_EPSILON
        ):
            gates.append(CorrectionGateResult(
                gate='pre_ringing',
                status='limitation',
                reasons=(
                    f'pre-ringing ratio {pre_ringing_ratio:.4f} with no '
                    'declared bound',
                ),
            ))
            limitations.append('pre-ringing bound undeclared')
        else:
            gates.append(CorrectionGateResult(
                gate='pre_ringing', status='pass'
            ))
    elif subject.topology == 'fir':
        gates.append(CorrectionGateResult(
            gate='pre_ringing',
            status='not_evaluated',
            reasons=('fir artifact not bound for inspection',),
        ))
        limitations.append('pre-ringing uninspected')
    elif subject.topology == 'peq':
        gates.append(CorrectionGateResult(
            gate='pre_ringing',
            status='pass',
            reasons=('peq topology has no pre-response',),
        ))
    else:
        gates.append(CorrectionGateResult(
            gate='pre_ringing',
            status='not_evaluated',
            reasons=('unknown topology cannot be inspected',),
        ))
        limitations.append('pre-ringing uninspected')

    # -- measurement coverage ----------------------------------------------
    baseline_map = (
        _sample_map(baseline) if baseline is not None else {}
    )
    post_map = _sample_map(post) if post is not None else {}
    design_ids = {
        _normalize_identifier(value) for value in region.design_position_ids
    }
    holdout_ids = {
        _normalize_identifier(value) for value in region.holdout_position_ids
    }
    repeatability_ids = {
        _normalize_identifier(value)
        for value in region.repeatability_position_ids
    }
    covered_design = sorted(design_ids & post_map.keys() & baseline_map.keys())
    covered_holdout = sorted(holdout_ids & post_map.keys() & baseline_map.keys())
    covered_repeatability = sorted(
        repeatability_ids & post_map.keys() & baseline_map.keys()
    )

    def _delta(position_id: str) -> float | None:
        """before-minus-after mean |residual|; >0 = improvement."""
        if target is None:
            return None
        before_freqs, before_mags = baseline_map[position_id]
        after_freqs, after_mags = post_map[position_id]
        before = _residual_db(target, before_freqs, before_mags, correction_bands)
        after = _residual_db(target, after_freqs, after_mags, correction_bands)
        if not isfinite(before) or not isfinite(after):
            return None
        return before - after

    design_deltas = {
        position_id: _delta(position_id) for position_id in covered_design
    }
    holdout_deltas = {
        position_id: _delta(position_id) for position_id in covered_holdout
    }

    physical_post = (
        post is not None
        and post.evidence_kind == 'physical_measurement'
    )

    # -- control fidelity gate ---------------------------------------------
    if not physical_post or not covered_design or target is None:
        reason = (
            'no physical post-correction measurement at design positions'
            if not physical_post or not covered_design
            else 'no target curve declared'
        )
        gates.append(CorrectionGateResult(
            gate='control_fidelity',
            status='not_evaluated',
            reasons=(reason,),
        ))
        if physical_post and covered_design:
            limitations.append('control fidelity unverifiable without target')
    else:
        regressed = [
            position_id
            for position_id, delta in design_deltas.items()
            if delta is not None and delta < -_IMPROVEMENT_EPSILON_DB
        ]
        improvements = [
            delta for delta in design_deltas.values()
            if delta is not None and delta > _IMPROVEMENT_EPSILON_DB
        ]
        if regressed:
            gates.append(CorrectionGateResult(
                gate='control_fidelity',
                status='fail',
                reasons=(
                    'correction regressed at design positions: '
                    + ', '.join(regressed),
                ),
            ))
            disqualifiers.append(
                f'correction regressed at design positions: {regressed}'
            )
        elif improvements:
            gates.append(CorrectionGateResult(
                gate='control_fidelity', status='pass'
            ))
        else:
            gates.append(CorrectionGateResult(
                gate='control_fidelity',
                status='limitation',
                reasons=('no measured improvement at design positions',),
            ))
            limitations.append('no measured improvement at design positions')

    # -- holdout independence gate ------------------------------------------
    if not holdout_ids:
        gates.append(CorrectionGateResult(
            gate='holdout_independence',
            status='limitation',
            reasons=('no spatial holdout positions declared',),
        ))
        limitations.append('no spatial holdout declared')
    elif not physical_post or post is None:
        gates.append(CorrectionGateResult(
            gate='holdout_independence',
            status='not_evaluated',
            reasons=(
                'holdout verification requires a physical post-correction '
                'measurement set',
            ),
        ))
        limitations.append('holdout not physically measured')
    elif not covered_holdout:
        gates.append(CorrectionGateResult(
            gate='holdout_independence',
            status='limitation',
            reasons=(
                'declared holdout positions were not re-measured after '
                'correction',
            ),
        ))
        limitations.append('holdout positions unmeasured')
    elif len(covered_holdout) < len(holdout_ids):
        uncovered = sorted(holdout_ids - set(covered_holdout))
        gates.append(CorrectionGateResult(
            gate='holdout_independence',
            status='limitation',
            reasons=(
                'holdout coverage incomplete — unmeasured: '
                + ', '.join(uncovered),
            ),
        ))
        limitations.append('holdout coverage incomplete')
    elif target is None:
        gates.append(CorrectionGateResult(
            gate='holdout_independence',
            status='not_evaluated',
            reasons=('no target curve declared',),
        ))
        limitations.append('holdout unverifiable without target')
    else:
        regressed = [
            position_id
            for position_id, delta in holdout_deltas.items()
            if delta is not None and delta < -_IMPROVEMENT_EPSILON_DB
        ]
        improvements = [
            delta for delta in holdout_deltas.values()
            if delta is not None and delta > _IMPROVEMENT_EPSILON_DB
        ]
        if regressed:
            gates.append(CorrectionGateResult(
                gate='holdout_independence',
                status='fail',
                reasons=(
                    'correction regressed at holdout positions: '
                    + ', '.join(regressed),
                ),
            ))
            disqualifiers.append(
                f'correction regressed at holdout positions: {regressed}'
            )
        elif improvements:
            gates.append(CorrectionGateResult(
                gate='holdout_independence', status='pass'
            ))
        else:
            gates.append(CorrectionGateResult(
                gate='holdout_independence',
                status='limitation',
                reasons=('no measured improvement at holdout positions',),
            ))
            limitations.append('no measured improvement at holdout positions')

    # -- closed-loop deployment gate -----------------------------------------
    if deployment.state == 'observed_mismatch':
        gates.append(CorrectionGateResult(
            gate='closed_loop_deployment',
            status='fail',
            reasons=(
                deployment.detail or 'deployed correction state mismatch',
            ),
        ))
        disqualifiers.append('deployed state mismatch')
    elif deployment.state == 'observed_match':
        gates.append(CorrectionGateResult(
            gate='closed_loop_deployment', status='pass'
        ))
    elif deployment.state == 'not_observable':
        gates.append(CorrectionGateResult(
            gate='closed_loop_deployment',
            status='limitation',
            reasons=(
                deployment.detail or 'deployed state not observable',
            ),
        ))
        limitations.append('deployed state not observable')
    else:
        gates.append(CorrectionGateResult(
            gate='closed_loop_deployment',
            status='not_evaluated',
            reasons=('no deployment observation',),
        ))

    # -- observables ----------------------------------------------------------
    observables: list[CorrectionObservableResult] = []

    if design_deltas and any(v is not None for v in design_deltas.values()):
        delta_values = [v for v in design_deltas.values() if v is not None]
        mean_delta = mean(delta_values)
        status: CorrectionObservableStatus = (
            'improved' if mean_delta > _IMPROVEMENT_EPSILON_DB
            else 'regressed' if mean_delta < -_IMPROVEMENT_EPSILON_DB
            else 'unchanged'
        )
        observables.append(CorrectionObservableResult(
            observable='target_deviation', status=status, value=mean_delta
        ))
    else:
        observables.append(CorrectionObservableResult(
            observable='target_deviation', status='not_evaluated'
        ))

    after_residuals: dict[str, float] = {}
    if target is not None and post is not None:
        for position_id in post_map:
            freqs, mags = post_map[position_id]
            residual = _residual_db(target, freqs, mags, correction_bands)
            if isfinite(residual):
                after_residuals[position_id] = residual
    if len(after_residuals) >= 2:
        observables.append(CorrectionObservableResult(
            observable='seat_to_seat_variance',
            status='within_limits',
            value=pvariance(list(after_residuals.values())),
        ))
    else:
        observables.append(CorrectionObservableResult(
            observable='seat_to_seat_variance', status='not_evaluated'
        ))
    if after_residuals:
        observables.append(CorrectionObservableResult(
            observable='worst_seat_deviation',
            status='within_limits',
            value=max(after_residuals.values()),
        ))
    else:
        observables.append(CorrectionObservableResult(
            observable='worst_seat_deviation', status='not_evaluated'
        ))

    observables.append(CorrectionObservableResult(
        observable='group_delay',
        status='within_limits' if group_delay_max_s is not None else 'not_evaluated',
        value=group_delay_max_s,
    ))
    observables.append(CorrectionObservableResult(
        observable='pre_ringing',
        status=(
            'exceeded'
            if pre_ringing_ratio is not None
            and constraints.max_pre_ringing_ratio is not None
            and pre_ringing_ratio
            > constraints.max_pre_ringing_ratio + _IMPROVEMENT_EPSILON_DB
            else 'within_limits' if pre_ringing_ratio is not None
            else 'not_evaluated'
        ),
        value=pre_ringing_ratio,
    ))
    observables.append(CorrectionObservableResult(
        observable='latency',
        status=(
            'exceeded'
            if latency_s is not None
            and constraints.max_latency_s is not None
            and latency_s > constraints.max_latency_s + _IMPROVEMENT_EPSILON_DB
            else 'within_limits' if latency_s is not None
            else 'not_evaluated'
        ),
        value=latency_s,
    ))
    observables.append(CorrectionObservableResult(
        observable='peak_boost',
        status=(
            'exceeded'
            if peak_boost_db is not None
            and constraints.max_boost_db is not None
            and peak_boost_db
            > constraints.max_boost_db + _IMPROVEMENT_EPSILON_DB
            else 'within_limits' if peak_boost_db is not None
            else 'not_evaluated'
        ),
        value=peak_boost_db,
    ))
    if constraints.headroom_db is not None and peak_boost_db is not None:
        margin = constraints.headroom_db - peak_boost_db
        observables.append(CorrectionObservableResult(
            observable='headroom_margin',
            status='within_limits' if margin >= 0 else 'exceeded',
            value=margin,
        ))
    else:
        observables.append(CorrectionObservableResult(
            observable='headroom_margin', status='not_evaluated'
        ))
    if plan is not None and plan.device_constraints.max_boost_db is not None:
        margin = plan.device_constraints.max_boost_db - (peak_boost_db or 0.0)
        observables.append(CorrectionObservableResult(
            observable='capability_margin',
            status='within_limits' if margin >= 0 else 'exceeded',
            value=margin,
        ))
    else:
        observables.append(CorrectionObservableResult(
            observable='capability_margin', status='not_evaluated'
        ))

    if physical_post and (design_deltas or holdout_deltas):
        all_deltas = [
            v for v in list(design_deltas.values()) + list(holdout_deltas.values())
            if v is not None
        ]
        if all_deltas:
            mean_delta = mean(all_deltas)
            observables.append(CorrectionObservableResult(
                observable='before_after_measurement',
                status=(
                    'improved' if mean_delta > _IMPROVEMENT_EPSILON_DB
                    else 'regressed' if mean_delta < -_IMPROVEMENT_EPSILON_DB
                    else 'unchanged'
                ),
                value=mean_delta,
            ))
        else:
            observables.append(CorrectionObservableResult(
                observable='before_after_measurement', status='unknown'
            ))
    else:
        observables.append(CorrectionObservableResult(
            observable='before_after_measurement', status='not_evaluated'
        ))

    observables.append(CorrectionObservableResult(
        observable='deployed_state_match',
        status=(
            'within_limits' if deployment.state == 'observed_match'
            else 'exceeded' if deployment.state == 'observed_mismatch'
            else 'unknown' if deployment.state == 'not_observable'
            else 'not_evaluated'
        ),
        detail=deployment.detail,
    ))

    if physical_post and covered_repeatability and target is not None:
        repeatability_deltas = [
            delta
            for position_id in covered_repeatability
            for delta in [_delta(position_id)]
            if delta is not None
        ]
        if repeatability_deltas:
            mean_delta = mean(repeatability_deltas)
            observables.append(CorrectionObservableResult(
                observable='repeatability',
                status=(
                    'unchanged'
                    if abs(mean_delta) <= _IMPROVEMENT_EPSILON_DB
                    else 'improved' if mean_delta > 0
                    else 'regressed'
                ),
                value=mean_delta,
                detail='repeatability positions are stability evidence, '
                       'never spatial generalization',
            ))
        else:
            observables.append(CorrectionObservableResult(
                observable='repeatability', status='unknown'
            ))
    else:
        observables.append(CorrectionObservableResult(
            observable='repeatability', status='not_evaluated'
        ))

    # -- registrations → comparability -----------------------------------------
    comparability_limitations = {
        'comparable': None,
        'comparable_with_limitations': 'registration comparable with limitations',
        'insufficient_evidence': 'registration insufficient evidence',
        'incomparable': 'registration pair incomparable',
    }
    for registration in registrations:
        note = comparability_limitations[registration.comparability]
        if registration.comparability == 'incomparable':
            disqualifiers.append(
                f'registration {registration.registration_id} is incomparable'
            )
        elif note is not None:
            limitations.append(
                f'{note} ({registration.registration_id})'
            )

    # -- state + scope -----------------------------------------------------------
    evidence_kinds: set[CorrectionEvidenceKind] = set()
    if post is not None:
        evidence_kinds.add(cast(CorrectionEvidenceKind, post.evidence_kind))
    if baseline is not None:
        evidence_kinds.add(cast(CorrectionEvidenceKind, baseline.evidence_kind))
    if deployment.state == 'observed_match':
        evidence_kinds.add('dsp_backend_observation')
    if subjective_evidence:
        evidence_kinds.add('subjective_listening')

    has_hard_fail = bool(disqualifiers)
    if has_hard_fail:
        state: CorrectionQualificationState = 'INCOMPATIBLE'
    elif baseline is None and spatial_evidence is None:
        state = 'INSUFFICIENT_EVIDENCE'
    elif post is None or not (
        set(post_map) & (design_ids | holdout_ids | repeatability_ids)
    ):
        state = 'DESIGN_ONLY'
    elif post is not None and post.evidence_kind == 'simulated_prediction':
        state = 'SIMULATED'
    else:
        holdout_gate = next(
            result for result in gates
            if result.gate == 'holdout_independence'
        )
        deployment_gate = next(
            result for result in gates
            if result.gate == 'closed_loop_deployment'
        )
        if (
            holdout_gate.status == 'pass'
            and deployment_gate.status == 'pass'
        ):
            state = 'DEPLOYED_AND_REMEASURED'
        elif holdout_gate.status == 'pass':
            state = 'SPATIALLY_HOLDOUT_VERIFIED'
        elif covered_design:
            state = 'MEASURED_AT_CONTROL_POINTS'
        else:
            state = 'DESIGN_ONLY'
            limitations.append(
                'post measurement does not cover design positions'
            )
    if (
        state in (
            'MEASURED_AT_CONTROL_POINTS',
            'SPATIALLY_HOLDOUT_VERIFIED',
            'DEPLOYED_AND_REMEASURED',
        )
        and limitations
    ):
        # Only demote to QUALIFIED_WITH_LIMITATIONS when the scope is a
        # qualified claim; unqualified-scope limitations stay on the ladder.
        provisional_scope = expected_scope_for(
            gates=tuple(gates),
            region=region,
            post=post,
            deployment=deployment,
            evidence_kinds=tuple(sorted(evidence_kinds)),
            state=state,
        )
        if provisional_scope in ('qualified_point', 'qualified_region'):
            state = 'QUALIFIED_WITH_LIMITATIONS'

    scope = expected_scope_for(
        gates=tuple(gates),
        region=region,
        post=post,
        deployment=deployment,
        evidence_kinds=tuple(sorted(evidence_kinds)),
        state=state,
    )
    if scope in ('qualified_point', 'qualified_region') and state not in (
        'QUALIFIED_WITH_LIMITATIONS',
        'MEASURED_AT_CONTROL_POINTS',
        'SPATIALLY_HOLDOUT_VERIFIED',
        'DEPLOYED_AND_REMEASURED',
    ):
        raise ValueError('inconsistent state/scope derivation')

    record = CorrectionQualificationRecord.model_construct(
        schema_version=CORRQUAL_SCHEMA_VERSION,
        authority_version=QUALIFICATION_AUTHORITY_VERSION,
        evaluator_version=QUALIFICATION_EVALUATOR_VERSION,
        qualification_id='correction-qualification:' + '0' * 64,
        semantic_sha256='0' * 64,
        recorded_at_utc=recorded_at_utc,
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        system_variant_id=system_variant_id,
        system_variant_sha256=system_variant_sha256,
        subject=subject,
        correction_bands=correction_bands,
        target=target,
        region=region,
        constraints=constraints,
        baseline=baseline,
        post=post,
        registrations=registrations,
        deployment=deployment,
        subjective_evidence=subjective_evidence,
        design_policy_id=(
            design_policy.policy_id if design_policy is not None else None
        ),
        design_policy_sha256=(
            design_policy.semantic_sha256 if design_policy is not None else None
        ),
        spatial_evidence_id=(
            spatial_evidence.evidence_id
            if spatial_evidence is not None else None
        ),
        spatial_evidence_sha256=(
            spatial_evidence.semantic_sha256
            if spatial_evidence is not None else None
        ),
        gates=tuple(gates),
        observables=tuple(observables),
        evidence_kinds=tuple(sorted(evidence_kinds)),
        state=state,
        scope=scope,
        limitations=tuple(dict.fromkeys(limitations)),
        disqualifiers=tuple(dict.fromkeys(disqualifiers)),
    )
    semantic_sha256 = _digest(record.semantic_payload())
    return CorrectionQualificationRecord(
        schema_version=record.schema_version,
        authority_version=record.authority_version,
        evaluator_version=record.evaluator_version,
        qualification_id=f'correction-qualification:{semantic_sha256}',
        semantic_sha256=semantic_sha256,
        recorded_at_utc=recorded_at_utc,
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        system_variant_id=system_variant_id,
        system_variant_sha256=system_variant_sha256,
        subject=subject,
        correction_bands=correction_bands,
        target=target,
        region=region,
        constraints=constraints,
        baseline=baseline,
        post=post,
        registrations=registrations,
        deployment=deployment,
        subjective_evidence=subjective_evidence,
        design_policy_id=record.design_policy_id,
        design_policy_sha256=record.design_policy_sha256,
        spatial_evidence_id=record.spatial_evidence_id,
        spatial_evidence_sha256=record.spatial_evidence_sha256,
        gates=tuple(gates),
        observables=tuple(observables),
        evidence_kinds=tuple(sorted(evidence_kinds)),
        state=state,
        scope=scope,
        limitations=record.limitations,
        disqualifiers=record.disqualifiers,
    )
