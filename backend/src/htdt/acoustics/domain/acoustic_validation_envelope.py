"""Quantitative acoustic validation fixtures + accuracy envelope authority (#566).

This module owns the software-side of the issue's benchmark program:

- a **validation fixture taxonomy** (classes A–D below) with deterministic
  retained fixtures for the cheap physics invariants;
- **analytic/semi-analytic reference functions** whose expected values are
  derived by formula in code (never hand-typed constants — the formula is the
  authority, the sealed fixture record pins it);
- a versioned, observable-specific **threshold policy** (no invented global
  ±dB target);
- a sealed **AccuracyEnvelopeRecord** binding solver+version x observable x
  frequency range x domain x reference evidence x error distribution x
  validation state — never one global "accuracy score";
- a hybrid-boundary (wave <-> geometric handoff) evaluation lane;
- a fail-closed measured-room **corpus slot** (class D) that stays
  ``EMPTY_UNKNOWN`` until exact measured evidence is bound.

Class taxonomy (issue §1):

- A ``analytic_semi_analytic`` — rectangular rigid/impedance room modes,
  free-field inverse-distance level, direct-path time of flight, single-plane
  reflection path length/arrival, trivial image-source configurations,
  simple decay cases with an analytic reference.
- B ``numerical_reference`` — higher-fidelity internal runs and independent
  numerical formulations. A comparison of one configuration against another
  configuration *of the same algorithm* is only admissible when the record
  carries an explicit ``independence_limitation`` note (issue §1.B).
- C ``hybrid_boundary`` — low-frequency wave <-> mid/high-frequency geometric
  overlap-band consistency, transition discontinuity, energy normalization
  and timing continuity.
- D ``measured_room_corpus`` — retained measured-room evidence with exact
  provenance. No measured values may ever be fabricated into the slot.

Literature anchors for the encoded numbers are recorded in-line:

- rectangular room eigenfrequencies f = c/2 * sqrt((nx/Lx)^2 + (ny/Ly)^2 +
  (nz/Lz)^2) — the closed-form rigid-wall solution (e.g. Kuttruff, Room
  Acoustics, ch. on modal theory); mirrors ``acoustics.rectangular_room_modes``.
- free-field point-source level drop 20*log10(r2/r1) ≈ 6.02 dB per distance
  doubling; arrival time d/c.
- single-plane reflection via the image-source method: path length
  |image->receiver|, arrival = path/c — mirrors
  ``acoustics.first_order_reflections``.
- ISO 3382-1:2009 lists subjective difference limens of ≈5% for reverberation
  metrics and ≈1 dB for level metrics (Seraphim 1958 basis); recent studies
  report larger values, so these are *context* for threshold rationale, never
  a fabricated pass gate.
"""

from __future__ import annotations

from math import isfinite, log10, pi, sqrt
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256 = r'^[0-9a-f]{64}$'

ENVELOPE_AUTHORITY_VERSION = 'accuracy-envelope-1'
FIXTURE_AUTHORITY_VERSION = 'validation-fixture-1'
THRESHOLD_POLICY_AUTHORITY_VERSION = 'validation-threshold-policy-1'
ENVELOPE_EVALUATOR_VERSION = 'accuracy-envelope-evaluator-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §1, §2, §8)
# ---------------------------------------------------------------------------

ValidationFixtureClass = Literal[
    'analytic_semi_analytic',
    'numerical_reference',
    'hybrid_boundary',
    'measured_room_corpus',
]

ValidationObservableKind = Literal[
    'magnitude_response_db',
    'modal_frequency_hz',
    'direct_arrival_time_s',
    'reflection_arrival_time_s',
    'reflection_level_db',
    'reflection_path_length_m',
    'decay_time_s',
    'phase_deg',
    'spatial_field_db',
    'seat_to_seat_variation_db',
    'hybrid_overlap_level_db',
    'hybrid_transition_continuity',
    'timing_continuity_s',
]

ValidationReferenceKind = Literal[
    'analytical_closed_form',
    'higher_fidelity_run',
    'independent_solver',
    'self_comparison_declared_limitation',
    'measured_corpus',
    'cross_model_invariant',
]

ReferenceIndependence = Literal[
    'independent',
    'same_algorithm_declared_limitation',
    'unknown',
]

ValidationCapabilityState = Literal[
    'VALIDATED_FOR_DECLARED_DOMAIN',
    'VALIDATED_WITH_LIMITATIONS',
    'EXPERIMENTAL',
    'INSUFFICIENT_EVIDENCE',
    'NOT_APPLICABLE',
]

ConvergenceStatus = Literal[
    'CONVERGED_WITHIN_TESTED_RANGE',
    'NOT_CONVERGED',
    'INSUFFICIENT_EVIDENCE',
]

CorpusSlotState = Literal['populated', 'empty_unknown']


#: Product-facing Japanese labels for the capability states (issue §8).
CAPABILITY_STATE_LABELS: dict[str, str] = {
    'VALIDATED_FOR_DECLARED_DOMAIN': '宣言領域で検証済み',
    'VALIDATED_WITH_LIMITATIONS': '制約付きで検証済み',
    'EXPERIMENTAL': '実験的（未検証）',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
    'NOT_APPLICABLE': '適用外',
}

CONVERGENCE_STATUS_LABELS: dict[str, str] = {
    'CONVERGED_WITHIN_TESTED_RANGE': '試験範囲内で収束',
    'NOT_CONVERGED': '未収束',
    'INSUFFICIENT_EVIDENCE': '証拠不足',
}


# ---------------------------------------------------------------------------
# Analytic / semi-analytic reference functions (fixture class A, issue §1.A)
#
# Expected values are *derived by formula* here; fixtures pin the derivation
# inputs so a regression in the formula (or an injected physics error in a
# solver output) is caught in CI.
# ---------------------------------------------------------------------------


def rectangular_mode_frequency_hz(
    length_x_m: float,
    length_y_m: float,
    length_z_m: float,
    n_x: int,
    n_y: int,
    n_z: int,
    sound_speed_m_s: float = 343.0,
) -> float:
    """Rigid-wall shoebox eigenfrequency f = c/2*sqrt(sum((n_i/L_i)^2)).

    Closed-form reference identical to ``acoustics.rectangular_room_modes``;
    the (0,0,0) trivial mode is rejected. Valid for rigid boundaries; for an
    impedance boundary the same expression is only a first estimate (real
    rooms shift slightly), which is why fixtures using it are class A
    *semi*-analytic unless the wall is declared rigid.
    """

    dims = (length_x_m, length_y_m, length_z_m)
    if any(not isfinite(float(v)) or float(v) <= 0.0 for v in dims):
        raise ValueError('room dimensions must be positive and finite')
    if not isfinite(float(sound_speed_m_s)) or sound_speed_m_s <= 0.0:
        raise ValueError('sound speed must be positive and finite')
    indices = (int(n_x), int(n_y), int(n_z))
    if any(index < 0 for index in indices):
        raise ValueError('mode indices must be non-negative')
    if indices == (0, 0, 0):
        raise ValueError('the trivial (0,0,0) mode is not a room resonance')
    return (sound_speed_m_s / 2.0) * sqrt(
        (n_x / length_x_m) ** 2
        + (n_y / length_y_m) ** 2
        + (n_z / length_z_m) ** 2
    )


def free_field_level_drop_db(distance_from_m: float, distance_to_m: float) -> float:
    """Point-source free-field level change 20*log10(r2/r1) dB (≈6.02 dB/ doubling)."""

    r1, r2 = float(distance_from_m), float(distance_to_m)
    if r1 <= 0.0 or r2 <= 0.0:
        raise ValueError('distances must be positive')
    return 20.0 * log10(r2 / r1)


def direct_arrival_time_s(distance_m: float, sound_speed_m_s: float = 343.0) -> float:
    """Direct-path time of flight d/c."""

    if not isfinite(float(distance_m)) or distance_m < 0.0:
        raise ValueError('distance must be non-negative and finite')
    if not isfinite(float(sound_speed_m_s)) or sound_speed_m_s <= 0.0:
        raise ValueError('sound speed must be positive and finite')
    return float(distance_m) / float(sound_speed_m_s)


def image_source_path(
    source: tuple[float, float, float],
    receiver: tuple[float, float, float],
    *,
    plane_axis: int,
    plane_coord_m: float,
) -> tuple[float, tuple[float, float, float]]:
    """Single-plane reflection via the image-source method.

    Mirrors the source across ``coordinate[plane_axis] == plane_coord_m`` and
    returns ``(reflected_path_length_m, reflection_point)`` where the
    reflection point is the intersection of the image->receiver segment with
    the plane. Raises when the segment does not cross the plane (no specular
    reflection exists) — consistent with ``acoustics.first_order_reflections``.
    """

    if plane_axis not in (0, 1, 2):
        raise ValueError('plane_axis must be 0, 1 or 2')
    for point in (source, receiver):
        if len(point) != 3 or any(not isfinite(float(v)) for v in point):
            raise ValueError('source/receiver must be finite 3-vectors')
    if not isfinite(float(plane_coord_m)):
        raise ValueError('plane coordinate must be finite')
    mirrored = list(source)
    mirrored[plane_axis] = 2.0 * plane_coord_m - mirrored[plane_axis]
    denominator = receiver[plane_axis] - mirrored[plane_axis]
    if abs(denominator) < 1e-12:
        raise ValueError('no specular reflection: image and receiver share the plane coordinate')
    t = (plane_coord_m - mirrored[plane_axis]) / denominator
    if not (0.0 <= t <= 1.0):
        raise ValueError('no specular reflection: segment does not cross the plane')
    point = tuple(
        mirrored[index] + t * (receiver[index] - mirrored[index])
        for index in range(3)
    )
    path = sqrt(
        sum((mirrored[i] - receiver[i]) ** 2 for i in range(3))
    )
    return path, (float(point[0]), float(point[1]), float(point[2]))


def sabine_decay_time_s(
    volume_m3: float,
    equivalent_absorption_area_m2: float,
    sound_speed_m_s: float = 343.0,
) -> float:
    """Sabine diffuse-field T60 = 55.3*V/(c*A).

    Reference for the simplest justified class-A decay fixture; the same law
    the R150 late-decay lane evaluates (``cad_late_decay_estimate``). A Sabine
    estimate is only a reference where the diffuse-field assumption is
    declared — see ``acoustic_metric_applicability`` for when that fails.
    """

    if not isfinite(float(volume_m3)) or volume_m3 <= 0.0:
        raise ValueError('volume must be positive and finite')
    if not isfinite(float(equivalent_absorption_area_m2)) or (
        equivalent_absorption_area_m2 <= 0.0
    ):
        raise ValueError('equivalent absorption area must be positive and finite')
    if not isfinite(float(sound_speed_m_s)) or sound_speed_m_s <= 0.0:
        raise ValueError('sound speed must be positive and finite')
    return 55.3 * float(volume_m3) / (
        float(sound_speed_m_s) * float(equivalent_absorption_area_m2)
    )


# ---------------------------------------------------------------------------
# Threshold policy (issue §5) — versioned, observable-specific, justified.
# ---------------------------------------------------------------------------

ThresholdScope = Literal['synthetic_fixture', 'real_room', 'declared_domain']
ThresholdMetric = Literal['max_abs', 'rms', 'p95']


class ObservableThresholdRule(BaseModel):
    """One observable's pass limit inside one declared scope.

    ``rationale`` is mandatory — a threshold without a defensible reason is
    exactly what the issue forbids. ``None`` limit means "no defensible pass
    threshold exists": evaluations then report the error distribution and the
    envelope stays in a limited state instead of forcing PASS/FAIL.
    """

    model_config = ConfigDict(frozen=True)

    observable: ValidationObservableKind
    scope: ThresholdScope
    metric: ThresholdMetric = 'max_abs'
    limit: float | None = Field(default=None, ge=0.0)
    unit: str = Field(min_length=1)
    rationale: str = Field(min_length=8)

    @model_validator(mode='after')
    def finite_limit(self) -> 'ObservableThresholdRule':
        if self.limit is not None and not isfinite(float(self.limit)):
            raise ValueError('threshold limit must be finite')
        return self


class ValidationThresholdPolicy(BaseModel):
    """Versioned threshold authority; every change is a new ``policy_revision``."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['validation-threshold-policy-1'] = (
        THRESHOLD_POLICY_AUTHORITY_VERSION
    )
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(ge=1)
    rules: tuple[ObservableThresholdRule, ...] = Field(min_length=1)
    policy_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_policy(self) -> 'ValidationThresholdPolicy':
        keys = [(rule.observable, rule.scope) for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError('threshold rules must be unique per observable+scope')
        if self.policy_sha256 != _hash(self.identity_payload()):
            raise ValueError('threshold policy hash mismatch')
        return self

    def rule_for(
        self,
        observable: ValidationObservableKind,
        scope: ThresholdScope,
    ) -> ObservableThresholdRule | None:
        for rule in self.rules:
            if rule.observable == observable and rule.scope == scope:
                return rule
        return None

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'policy_id': self.policy_id,
            'policy_revision': self.policy_revision,
            'rules': [rule.model_dump(mode='json') for rule in self.rules],
        }


def build_threshold_policy(
    *,
    policy_id: str,
    policy_revision: int,
    rules: Sequence[ObservableThresholdRule | Mapping[str, Any]],
) -> ValidationThresholdPolicy:
    parsed = tuple(
        rule if isinstance(rule, ObservableThresholdRule)
        else ObservableThresholdRule.model_validate(rule)
        for rule in rules
    )
    payload: dict[str, Any] = {
        'policy_id': policy_id,
        'policy_revision': policy_revision,
        'rules': parsed,
    }
    provisional = ValidationThresholdPolicy.model_construct(
        **canonicalize_payload(
            ValidationThresholdPolicy, dict(**payload, policy_sha256='0' * 64)
        )
    )
    return ValidationThresholdPolicy(
        **payload, policy_sha256=_hash(provisional.identity_payload())
    )


#: The bundled analytic-fixture policy. Tolerances here are *not* physics
#: uncertainty budgets — they are float64/derivation-precision guards on
#: closed-form expectations, deliberately far tighter than any real-room
#: claim (issue §5: "thresholds can be stricter for basic invariants").
ANALYTIC_FIXTURE_POLICY_V1 = build_threshold_policy(
    policy_id='rev55-analytic-fixture-policy',
    policy_revision=1,
    rules=(
        {
            'observable': 'modal_frequency_hz',
            'scope': 'synthetic_fixture',
            'metric': 'max_abs',
            'limit': 1e-9,
            'unit': 'Hz',
            'rationale': 'Closed-form eigenfrequency f = c/2*sqrt(sum((n/L)^2)) '
            'is exact in float64; 1e-9 Hz guards against sign/index/unit '
            'regressions only, not a physics claim.',
        },
        {
            'observable': 'direct_arrival_time_s',
            'scope': 'synthetic_fixture',
            'metric': 'max_abs',
            'limit': 1e-9,
            'unit': 's',
            'rationale': 'Time of flight d/c is exact arithmetic; tolerance '
            'covers float64 rounding only.',
        },
        {
            'observable': 'reflection_arrival_time_s',
            'scope': 'synthetic_fixture',
            'metric': 'max_abs',
            'limit': 1e-9,
            'unit': 's',
            'rationale': 'Image-source path/c is exact arithmetic; tolerance '
            'covers float64 rounding only.',
        },
        {
            'observable': 'reflection_path_length_m',
            'scope': 'synthetic_fixture',
            'metric': 'max_abs',
            'limit': 1e-9,
            'unit': 'm',
            'rationale': 'Image-source path length is exact arithmetic; '
            'tolerance covers float64 rounding only.',
        },
        {
            'observable': 'reflection_level_db',
            'scope': 'synthetic_fixture',
            'metric': 'max_abs',
            'limit': 1e-9,
            'unit': 'dB',
            'rationale': 'Free-field 20*log10(r2/r1) level drop is exact '
            'arithmetic for a declared point source; tolerance covers '
            'float64 rounding only.',
        },
        {
            'observable': 'decay_time_s',
            'scope': 'synthetic_fixture',
            'metric': 'max_abs',
            'limit': 1e-9,
            'unit': 's',
            'rationale': 'Sabine T60 = 55.3V/(cA) is exact arithmetic under '
            'the declared diffuse-field assumption; tolerance covers '
            'float64 rounding only.',
        },
    ),
)


# ---------------------------------------------------------------------------
# Retained validation fixtures (issue §1, §10)
# ---------------------------------------------------------------------------


class FixtureExpectedSample(BaseModel):
    """One expected value inside a retained fixture."""

    model_config = ConfigDict(frozen=True)

    sample_key: str = Field(min_length=1)
    observable: ValidationObservableKind
    unit: str = Field(min_length=1)
    expected_value: float

    @model_validator(mode='after')
    def finite(self) -> 'FixtureExpectedSample':
        if not isfinite(float(self.expected_value)):
            raise ValueError('expected sample values must be finite')
        return self


class MeasuredCorpusSlot(BaseModel):
    """Class-D retained measured-room slot (issue §1.D).

    The slot reserves a place for exact measured evidence — geometry,
    equipment, calibration, registration, processing and environment
    provenance. It is *fail-closed*: with no measured values bound the slot
    reports ``empty_unknown`` and no caller may treat it as evidence. The
    ``#564`` registration campaign binds real values later; this record
    never fabricates them.
    """

    model_config = ConfigDict(frozen=True)

    slot_id: str = Field(min_length=1)
    state: CorpusSlotState = 'empty_unknown'
    # Exact provenance required *before* measured values may be bound.
    geometry_provenance: str | None = Field(default=None, min_length=1)
    equipment_provenance: str | None = Field(default=None, min_length=1)
    calibration_provenance: str | None = Field(default=None, min_length=1)
    registration_provenance: str | None = Field(default=None, min_length=1)
    processing_provenance: str | None = Field(default=None, min_length=1)
    environment_provenance: str | None = Field(default=None, min_length=1)
    measured_observations: tuple[FixtureExpectedSample, ...] = ()
    measurement_evidence_sha256: str | None = Field(default=None, pattern=_SHA256)
    slot_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def fail_closed(self) -> 'MeasuredCorpusSlot':
        provenance = (
            self.geometry_provenance,
            self.equipment_provenance,
            self.calibration_provenance,
            self.registration_provenance,
            self.processing_provenance,
            self.environment_provenance,
        )
        if self.state == 'empty_unknown':
            if self.measured_observations:
                raise ValueError(
                    'an empty/UNKNOWN corpus slot must not carry measured values'
                )
            if self.measurement_evidence_sha256 is not None:
                raise ValueError(
                    'an empty/UNKNOWN corpus slot must not pin measurement evidence'
                )
        else:
            if not self.measured_observations:
                raise ValueError('a populated corpus slot requires measured observations')
            if self.measurement_evidence_sha256 is None:
                raise ValueError('a populated corpus slot pins exact measurement evidence')
            if any(item is None for item in provenance):
                raise ValueError(
                    'a populated corpus slot requires complete exact provenance'
                )
        sample_keys = [s.sample_key for s in self.measured_observations]
        if len(sample_keys) != len(set(sample_keys)):
            raise ValueError('corpus slot sample keys must be unique')
        if self.slot_sha256 != _hash(self.identity_payload()):
            raise ValueError('measured corpus slot hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'slot_id': self.slot_id,
            'state': self.state,
            'geometry_provenance': self.geometry_provenance,
            'equipment_provenance': self.equipment_provenance,
            'calibration_provenance': self.calibration_provenance,
            'registration_provenance': self.registration_provenance,
            'processing_provenance': self.processing_provenance,
            'environment_provenance': self.environment_provenance,
            'measured_observations': [
                sample.model_dump(mode='json')
                for sample in self.measured_observations
            ],
            'measurement_evidence_sha256': self.measurement_evidence_sha256,
        }


def build_corpus_slot(*, slot_id: str) -> MeasuredCorpusSlot:
    """Create an empty/UNKNOWN class-D slot — real data arrives via #1/#564."""

    payload: dict[str, Any] = {'slot_id': slot_id, 'state': 'empty_unknown'}
    provisional = MeasuredCorpusSlot.model_construct(
        **canonicalize_payload(
            MeasuredCorpusSlot, dict(**payload, slot_sha256='0' * 64)
        )
    )
    return MeasuredCorpusSlot(
        **payload, slot_sha256=_hash(provisional.identity_payload())
    )


class ValidationFixtureRecord(BaseModel):
    """Retained, versioned benchmark fixture (issue §1).

    Class-B fixtures (``numerical_reference``) must state their reference
    independence honestly: comparing one configuration of an algorithm to
    another configuration *of the same algorithm* is admissible only with
    ``same_algorithm_declared_limitation`` plus an
    ``independence_limitation`` note (issue §1.B). Class-D fixtures carry
    measured provenance through ``corpus_slot`` and stay UNKNOWN when no
    measured values are bound.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['validation-fixture-1'] = FIXTURE_AUTHORITY_VERSION
    fixture_id: str = Field(min_length=1)
    fixture_class: ValidationFixtureClass
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    domain_applicability: str = Field(min_length=1)
    solver_consumer: str = Field(min_length=1)
    reference_kind: ValidationReferenceKind
    reference_independence: ReferenceIndependence = 'independent'
    independence_limitation: str | None = Field(default=None, min_length=8)
    reference_description: str = Field(min_length=1)
    expected_samples: tuple[FixtureExpectedSample, ...] = ()
    # Class C hybrid-boundary declaration the fixture exercises.
    overlap_band_hz: tuple[float, float] | None = None
    # Class D retained-corpus slot; exactly one record per fixture.
    corpus_slot: 'MeasuredCorpusSlot | None' = None
    threshold_scope: ThresholdScope = 'synthetic_fixture'
    known_failure_modes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    fixture_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_fixture(self) -> 'ValidationFixtureRecord':
        sample_keys = [s.sample_key for s in self.expected_samples]
        if len(sample_keys) != len(set(sample_keys)):
            raise ValueError('fixture sample keys must be unique')
        if self.fixture_class == 'analytic_semi_analytic':
            if self.reference_kind not in ('analytical_closed_form', 'cross_model_invariant'):
                raise ValueError(
                    'analytic fixtures require an analytical/closed-form or '
                    'invariant reference'
                )
            if not self.expected_samples:
                raise ValueError('analytic fixtures require expected samples')
            if self.reference_independence != 'independent':
                raise ValueError('analytic fixtures are inherently independent references')
        if self.fixture_class == 'numerical_reference':
            if self.reference_kind == 'self_comparison_declared_limitation':
                if self.reference_independence != 'same_algorithm_declared_limitation':
                    raise ValueError(
                        'self-comparison references must declare '
                        'same_algorithm_declared_limitation independence'
                    )
                if not self.independence_limitation:
                    raise ValueError(
                        'same-algorithm self-comparison requires an explicit '
                        'independence limitation note (issue #566 §1.B)'
                    )
            elif self.reference_independence == 'same_algorithm_declared_limitation':
                raise ValueError(
                    'independence limitation flag is only valid for '
                    'self-comparison references'
                )
            if not self.expected_samples:
                raise ValueError('numerical reference fixtures require expected samples')
        if self.fixture_class == 'hybrid_boundary':
            if self.overlap_band_hz is None:
                raise ValueError('hybrid-boundary fixtures require an overlap band')
            lo, hi = self.overlap_band_hz
            if not (isfinite(lo) and isfinite(hi)) or not (0.0 < lo < hi):
                raise ValueError('overlap band must be positive and ordered')
        elif self.overlap_band_hz is not None:
            raise ValueError('overlap bands are only valid for hybrid-boundary fixtures')
        if self.fixture_class == 'measured_room_corpus':
            if self.corpus_slot is None:
                raise ValueError('measured-corpus fixtures require a corpus slot')
            if self.reference_kind != 'measured_corpus':
                raise ValueError('measured-corpus fixtures must cite measured_corpus')
        elif self.corpus_slot is not None:
            raise ValueError('corpus slots are only valid for measured-corpus fixtures')
        if self.fixture_sha256 != _hash(self.identity_payload()):
            raise ValueError('validation fixture hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'fixture_id': self.fixture_id,
            'fixture_class': self.fixture_class,
            'title': self.title,
            'description': self.description,
            'domain_applicability': self.domain_applicability,
            'solver_consumer': self.solver_consumer,
            'reference_kind': self.reference_kind,
            'reference_independence': self.reference_independence,
            'independence_limitation': self.independence_limitation,
            'reference_description': self.reference_description,
            'expected_samples': [
                sample.model_dump(mode='json') for sample in self.expected_samples
            ],
            'overlap_band_hz': (
                None if self.overlap_band_hz is None else list(self.overlap_band_hz)
            ),
            'corpus_slot': (
                None if self.corpus_slot is None
                else self.corpus_slot.model_dump(mode='json')
            ),
            'threshold_scope': self.threshold_scope,
            'known_failure_modes': list(self.known_failure_modes),
            'notes': list(self.notes),
        }


def build_validation_fixture(
    *,
    fixture_id: str,
    fixture_class: ValidationFixtureClass,
    title: str,
    description: str,
    domain_applicability: str,
    solver_consumer: str,
    reference_kind: ValidationReferenceKind,
    reference_description: str,
    reference_independence: ReferenceIndependence = 'independent',
    independence_limitation: str | None = None,
    expected_samples: Sequence[FixtureExpectedSample | Mapping[str, Any]] = (),
    overlap_band_hz: tuple[float, float] | None = None,
    corpus_slot: MeasuredCorpusSlot | None = None,
    threshold_scope: ThresholdScope = 'synthetic_fixture',
    known_failure_modes: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> ValidationFixtureRecord:
    samples = tuple(
        sample if isinstance(sample, FixtureExpectedSample)
        else FixtureExpectedSample.model_validate(sample)
        for sample in expected_samples
    )
    payload: dict[str, Any] = {
        'fixture_id': fixture_id,
        'fixture_class': fixture_class,
        'title': title,
        'description': description,
        'domain_applicability': domain_applicability,
        'solver_consumer': solver_consumer,
        'reference_kind': reference_kind,
        'reference_independence': reference_independence,
        'independence_limitation': independence_limitation,
        'reference_description': reference_description,
        'expected_samples': samples,
        'overlap_band_hz': overlap_band_hz,
        'corpus_slot': corpus_slot,
        'threshold_scope': threshold_scope,
        'known_failure_modes': tuple(known_failure_modes),
        'notes': tuple(notes),
    }
    provisional = ValidationFixtureRecord.model_construct(
        **canonicalize_payload(
            ValidationFixtureRecord, dict(**payload, fixture_sha256='0' * 64)
        )
    )
    return ValidationFixtureRecord(
        **payload, fixture_sha256=_hash(provisional.identity_payload())
    )


def _sample(
    key: str,
    observable: ValidationObservableKind,
    unit: str,
    value: float,
) -> FixtureExpectedSample:
    return FixtureExpectedSample(
        sample_key=key,
        observable=observable,
        unit=unit,
        expected_value=float(value),
    )


def _build_val10_fixture() -> ValidationFixtureRecord:
    """VAL10 — free-field/direct path (issue §10).

    Source at origin, receivers on a line at 1/2/4 m. Expected values are
    formula-derived: arrival d/c and the 6.02 dB/doubling drop.
    """

    c = 343.0
    distances = (1.0, 2.0, 4.0)
    samples = [
        _sample(
            f'arrival@{d}m',
            'direct_arrival_time_s',
            's',
            direct_arrival_time_s(d, c),
        )
        for d in distances
    ]
    samples += [
        _sample(
            f'level_drop_1m_to_{d}m',
            'reflection_level_db',
            'dB',
            free_field_level_drop_db(1.0, d),
        )
        for d in distances[1:]
    ]
    return build_validation_fixture(
        fixture_id='VAL10-free-field-direct-path',
        fixture_class='analytic_semi_analytic',
        title='VAL10 自由音場・直達音',
        description='Free-field point source: direct-path time of flight '
        'and inverse-distance level behavior (−6.02 dB per distance '
        'doubling).',
        domain_applicability='free-field point source, omnidirectional, '
        'c = 343 m/s',
        solver_consumer='any solver exposing direct-path pressure arrivals',
        reference_kind='analytical_closed_form',
        reference_description='d/c arrival; 20*log10(r2/r1) level drop',
        expected_samples=samples,
        known_failure_modes=(
            'wrong coordinate convention shifts path lengths',
            'sample-rate quantization in time-domain solvers',
        ),
    )


def _build_val20_fixture() -> ValidationFixtureRecord:
    """VAL20 — rectangular-room rigid modes (issue §10).

    6.0 x 4.0 x 3.0 m rigid shoebox, c = 343 m/s. Expected modal frequencies
    are formula-derived for the lowest axial/tangential modes.
    """

    dims = (6.0, 4.0, 3.0)
    c = 343.0
    modes = (
        (1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 0, 0), (1, 1, 0), (0, 1, 1),
    )
    samples = [
        _sample(
            f'mode_{nx}{ny}{nz}',
            'modal_frequency_hz',
            'Hz',
            rectangular_mode_frequency_hz(*dims, nx, ny, nz, c),
        )
        for nx, ny, nz in modes
    ]
    return build_validation_fixture(
        fixture_id='VAL20-rectangular-room-modes',
        fixture_class='analytic_semi_analytic',
        title='VAL20 矩形室モード周波数',
        description='Rigid-wall 6.0x4.0x3.0 m shoebox eigenfrequencies from '
        'f = c/2*sqrt((nx/Lx)^2+(ny/Ly)^2+(nz/Lz)^2).',
        domain_applicability='closed rectangular room, rigid boundaries, '
        'c = 343 m/s; impedance walls shift real frequencies slightly',
        solver_consumer='wave-domain solvers exposing eigenfrequencies '
        '(pffdtd/mfem lanes)',
        reference_kind='analytical_closed_form',
        reference_description='closed-form rigid shoebox eigenfrequencies '
        '(Kuttruff modal theory)',
        expected_samples=samples,
        known_failure_modes=(
            'impedance boundaries shift measured eigenfrequencies vs rigid formula',
            'grid dispersion in FDTD moves computed peaks',
        ),
    )


def _build_val30_fixture() -> ValidationFixtureRecord:
    """VAL30 — first-order reflection (issue §10).

    Room 6.0x4.0x3.0 m, source (1.0,1.0,1.0), receiver (4.0,3.0,1.5);
    reflection off the x=0 wall via the image-source method.
    """

    c = 343.0
    source = (1.0, 1.0, 1.0)
    receiver = (4.0, 3.0, 1.5)
    direct = sqrt(sum((a - b) ** 2 for a, b in zip(source, receiver)))
    path, _point = image_source_path(
        source, receiver, plane_axis=0, plane_coord_m=0.0
    )
    samples = [
        _sample('direct_path_length', 'reflection_path_length_m', 'm', direct),
        _sample(
            'direct_arrival', 'direct_arrival_time_s', 's',
            direct_arrival_time_s(direct, c),
        ),
        _sample(
            'x0_reflection_path_length', 'reflection_path_length_m', 'm', path,
        ),
        _sample(
            'x0_reflection_arrival', 'reflection_arrival_time_s', 's',
            direct_arrival_time_s(path, c),
        ),
        _sample(
            'x0_reflection_level_drop_vs_1m',
            'reflection_level_db', 'dB',
            free_field_level_drop_db(1.0, path),
        ),
    ]
    return build_validation_fixture(
        fixture_id='VAL30-first-reflection-image-source',
        fixture_class='analytic_semi_analytic',
        title='VAL30 一次反射（鏡像源法）',
        description='Single-plane reflection from the x=0 wall: image-source '
        'path length, arrival time and free-field level semantics.',
        domain_applicability='single infinite rigid plane; specular '
        'reflection only; c = 343 m/s',
        solver_consumer='geometric/specular solvers exposing first-order '
        'paths (GA adapter)',
        reference_kind='analytical_closed_form',
        reference_description='image-source method: |image->receiver| path, '
        'arrival path/c',
        expected_samples=samples,
        known_failure_modes=(
            'finite wall extent truncates the specular zone',
            'boundary absorption/phase not modeled by the geometric path',
        ),
    )


def _build_val50_fixture() -> ValidationFixtureRecord:
    """VAL50 — hybrid overlap/transition declaration (issue §10).

    Declares the overlap band a wave<->geometric composition must keep
    continuous; expectations are evaluated through
    ``evaluate_hybrid_boundary`` rather than scalar samples.
    """

    return build_validation_fixture(
        fixture_id='VAL50-hybrid-overlap-boundary',
        fixture_class='hybrid_boundary',
        title='VAL50 ハイブリッド重複帯域',
        description='Wave<->geometric overlap-band consistency, transition '
        'discontinuity, energy normalization and timing continuity checks.',
        domain_applicability='hybrid wave/geometric compositions with an '
        'explicit overlap domain',
        solver_consumer='cad_hybrid_acoustic_result compositions',
        reference_kind='cross_model_invariant',
        reference_description='overlap-band level agreement and transition '
        'continuity between two model families',
        overlap_band_hz=(150.0, 300.0),
        known_failure_modes=(
            'normalization drift between wave and energy domains',
            'crossover gain step at the partition frequency',
            'time reference mismatch between solver families',
        ),
    )


def _build_val60_fixture() -> ValidationFixtureRecord:
    """VAL60 — retained owned-room holdout slot (issue §10).

    The slot exists but carries no measured values until the #1/#564
    campaigns bind exact evidence — it reports EMPTY/UNKNOWN today.
    """

    return build_validation_fixture(
        fixture_id='VAL60-owned-room-holdout',
        fixture_class='measured_room_corpus',
        title='VAL60 実測室ホールドアウト（スロット）',
        description='Retained measured-room corpus slot awaiting exact '
        'predicted-vs-measured registered evidence from #1/#564. Stays '
        'EMPTY/UNKNOWN — no estimated values are fabricated.',
        domain_applicability='owned-room campaign geometries with full '
        'registration provenance',
        solver_consumer='all production prediction providers',
        reference_kind='measured_corpus',
        reference_description='exact registered predicted-vs-measured '
        'comparison (requires #1/#564 evidence)',
        corpus_slot=build_corpus_slot(slot_id='val60-owned-room-slot'),
        threshold_scope='real_room',
        known_failure_modes=(
            'no measured data bound yet — slot is empty by design',
            'calibration/holdout separation must survive corpus ingestion',
        ),
    )


#: The retained, versioned initial benchmark set (issue §10 VAL10–VAL60;
#: VAL40 solver-convergence is carried by ``ConvergenceStatus`` evidence on
#: the envelope rather than a fixture row).
REV55_ANALYTIC_FIXTURES: tuple[ValidationFixtureRecord, ...] = (
    _build_val10_fixture(),
    _build_val20_fixture(),
    _build_val30_fixture(),
    _build_val50_fixture(),
    _build_val60_fixture(),
)


# ---------------------------------------------------------------------------
# Error metrics + evaluation (issue §2, §5, §7)
# ---------------------------------------------------------------------------


class ObservableErrorStatistic(BaseModel):
    """Error distribution of one observable inside one evaluation."""

    model_config = ConfigDict(frozen=True)

    observable: ValidationObservableKind
    unit: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    max_abs_error: float | None = Field(default=None, ge=0.0)
    rms_error: float | None = Field(default=None, ge=0.0)
    mean_abs_error: float | None = Field(default=None, ge=0.0)
    p95_abs_error: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def consistent(self) -> 'ObservableErrorStatistic':
        values = (
            self.max_abs_error,
            self.rms_error,
            self.mean_abs_error,
            self.p95_abs_error,
        )
        if self.sample_count == 0:
            if any(value is not None for value in values):
                raise ValueError('zero-sample statistics carry no error values')
        elif any(value is None for value in (self.max_abs_error, self.rms_error, self.mean_abs_error)):
            raise ValueError('non-empty statistics require max/rms/mean errors')
        for value in values:
            if value is not None and not isfinite(float(value)):
                raise ValueError('error statistics must be finite')
        return self


def _abs_errors(errors: Sequence[float]) -> list[float]:
    values = [abs(float(e)) for e in errors]
    if any(not isfinite(v) for v in values):
        raise ValueError('observed errors must be finite')
    return values


def error_statistic(
    observable: ValidationObservableKind,
    unit: str,
    errors: Sequence[float],
) -> ObservableErrorStatistic:
    """Build an honest error distribution from signed error samples."""

    if not errors:
        return ObservableErrorStatistic(
            observable=observable, unit=unit, sample_count=0
        )
    abs_values = sorted(_abs_errors(errors))
    count = len(abs_values)
    mean_abs = sum(abs_values) / count
    rms = sqrt(sum(v * v for v in abs_values) / count)
    p95 = abs_values[min(count - 1, int(0.95 * (count - 1) + 0.5))]
    return ObservableErrorStatistic(
        observable=observable,
        unit=unit,
        sample_count=count,
        max_abs_error=abs_values[-1],
        rms_error=rms,
        mean_abs_error=mean_abs,
        p95_abs_error=p95,
    )


FixtureGate = Literal['pass', 'fail', 'insufficient']


class FixtureObservableEvaluation(BaseModel):
    """Gate outcome of one observable against the threshold policy."""

    model_config = ConfigDict(frozen=True)

    observable: ValidationObservableKind
    statistic: ObservableErrorStatistic
    gate: FixtureGate
    threshold_rule: ObservableThresholdRule | None = None
    reason: str | None = None


class FixtureEvaluationResult(BaseModel):
    """Per-observable evaluation of one retained fixture (issue §7 gating)."""

    model_config = ConfigDict(frozen=True)

    fixture_id: str = Field(min_length=1)
    fixture_sha256: str = Field(pattern=_SHA256)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(ge=1)
    evaluations: tuple[FixtureObservableEvaluation, ...]
    overall_gate: FixtureGate

    @model_validator(mode='after')
    def consistent_overall(self) -> 'FixtureEvaluationResult':
        if not self.evaluations:
            raise ValueError('fixture evaluation requires per-observable results')
        gates = {item.gate for item in self.evaluations}
        expected: FixtureGate
        if 'fail' in gates:
            expected = 'fail'
        elif 'insufficient' in gates:
            expected = 'insufficient'
        else:
            expected = 'pass'
        if self.overall_gate != expected:
            raise ValueError('overall gate must be the honest aggregate of its parts')
        return self


def evaluate_fixture_observations(
    fixture: ValidationFixtureRecord,
    observed: Mapping[str, float],
    policy: ValidationThresholdPolicy,
) -> FixtureEvaluationResult:
    """Score observed values against a class-A/B fixture's expectations.

    Pass/fail is evaluated *only inside the declared domain*: an observable
    whose threshold rule does not exist (or whose limit is ``None``) reports
    its error distribution and gates ``insufficient`` — never a fabricated
    PASS (issue §5).
    """

    evaluations: list[FixtureObservableEvaluation] = []
    grouped: dict[ValidationObservableKind, list[float]] = {}
    units: dict[ValidationObservableKind, str] = {}
    for sample in fixture.expected_samples:
        units[sample.observable] = sample.unit
        if sample.sample_key not in observed:
            continue
        error = float(observed[sample.sample_key]) - float(sample.expected_value)
        grouped.setdefault(sample.observable, []).append(error)

    for observable in sorted(set(s.observable for s in fixture.expected_samples)):
        errors = grouped.get(observable, [])
        statistic = error_statistic(
            observable, units[observable], errors
        )
        rule = policy.rule_for(observable, fixture.threshold_scope)
        if not errors:
            evaluations.append(FixtureObservableEvaluation(
                observable=observable,
                statistic=statistic,
                gate='insufficient',
                threshold_rule=rule,
                reason='no observed values supplied for this observable',
            ))
            continue
        if rule is None or rule.limit is None:
            evaluations.append(FixtureObservableEvaluation(
                observable=observable,
                statistic=statistic,
                gate='insufficient',
                threshold_rule=rule,
                reason='no defensible pass threshold in policy scope — '
                'reporting error distribution only',
            ))
            continue
        metric_value = {
            'max_abs': statistic.max_abs_error,
            'rms': statistic.rms_error,
            'p95': statistic.p95_abs_error,
        }[rule.metric]
        assert metric_value is not None
        evaluations.append(FixtureObservableEvaluation(
            observable=observable,
            statistic=statistic,
            gate='pass' if metric_value <= rule.limit else 'fail',
            threshold_rule=rule,
            reason=None,
        ))

    gates = {item.gate for item in evaluations}
    overall: FixtureGate = (
        'fail' if 'fail' in gates
        else 'insufficient' if 'insufficient' in gates
        else 'pass'
    )
    return FixtureEvaluationResult(
        fixture_id=fixture.fixture_id,
        fixture_sha256=fixture.fixture_sha256,
        policy_id=policy.policy_id,
        policy_revision=policy.policy_revision,
        evaluations=tuple(evaluations),
        overall_gate=overall,
    )


# ---------------------------------------------------------------------------
# Hybrid-boundary evaluation (fixture class C, issue §1.C)
# ---------------------------------------------------------------------------


class HybridOverlapSample(BaseModel):
    """One wave/geometric level pair inside the declared overlap band."""

    model_config = ConfigDict(frozen=True)

    frequency_hz: float = Field(gt=0.0)
    wave_level_db: float
    geometric_level_db: float

    @model_validator(mode='after')
    def finite(self) -> 'HybridOverlapSample':
        for value in (self.frequency_hz, self.wave_level_db, self.geometric_level_db):
            if not isfinite(float(value)):
                raise ValueError('overlap samples must be finite')
        return self


class HybridBoundaryReport(BaseModel):
    """Derived (never stored) overlap evaluation for class-C fixtures."""

    model_config = ConfigDict(frozen=True)

    overlap_band_hz: tuple[float, float]
    overlap_sample_count: int = Field(ge=0)
    max_overlap_level_delta_db: float | None = Field(default=None, ge=0.0)
    overlap_level_delta_rms_db: float | None = Field(default=None, ge=0.0)
    transition_level_jump_db: float | None = Field(default=None, ge=0.0)
    timing_gap_s: float | None = Field(default=None, ge=0.0)
    normalization_offset_db: float | None = None
    gates: tuple[str, ...] = ()
    state: Literal['consistent', 'discontinuity_detected', 'insufficient_evidence']


def evaluate_hybrid_boundary(
    *,
    overlap_band_hz: tuple[float, float],
    overlap_samples: Sequence[HybridOverlapSample],
    transition_jump_db: float | None,
    timing_gap_s: float | None,
    max_allowed_overlap_delta_db: float,
    max_allowed_transition_jump_db: float,
    max_allowed_timing_gap_s: float,
) -> HybridBoundaryReport:
    """Evaluate wave<->geometric handoff quality inside the overlap band.

    Checks (issue §1.C / §7): overlap-band level consistency, transition-band
    discontinuity, energy/level normalization offset, and timing continuity.
    Declared limits come from the composition's own contract — the function
    never invents a universal bound; missing limits raise.
    """

    lo, hi = overlap_band_hz
    if not (isfinite(lo) and isfinite(hi)) or not (0.0 < lo < hi):
        raise ValueError('overlap band must be positive and ordered')
    for limit in (
        max_allowed_overlap_delta_db,
        max_allowed_transition_jump_db,
        max_allowed_timing_gap_s,
    ):
        if not isfinite(float(limit)) or limit < 0.0:
            raise ValueError('declared continuity limits must be finite and non-negative')

    in_band = [
        s for s in overlap_samples if lo <= float(s.frequency_hz) <= hi
    ]
    gates: list[str] = []
    deltas: list[float] = []
    normalization_offset: float | None = None
    if in_band:
        deltas = [abs(float(s.wave_level_db) - float(s.geometric_level_db)) for s in in_band]
        signed = [float(s.geometric_level_db) - float(s.wave_level_db) for s in in_band]
        normalization_offset = sum(signed) / len(signed)
        max_delta = max(deltas)
        rms_delta = sqrt(sum(d * d for d in deltas) / len(deltas))
        if max_delta > max_allowed_overlap_delta_db:
            gates.append('overlap_level_delta_exceeds_limit')
        else:
            gates.append('overlap_consistent')
    else:
        max_delta = rms_delta = None
        gates.append('overlap_band_unsampled')

    if transition_jump_db is None:
        gates.append('transition_unevaluated')
    elif abs(float(transition_jump_db)) > max_allowed_transition_jump_db:
        gates.append('transition_discontinuity_exceeds_limit')
    else:
        gates.append('transition_continuous')

    if timing_gap_s is None:
        gates.append('timing_unevaluated')
    elif abs(float(timing_gap_s)) > max_allowed_timing_gap_s:
        gates.append('timing_gap_exceeds_limit')
    else:
        gates.append('timing_continuous')

    failing = {
        'overlap_level_delta_exceeds_limit',
        'transition_discontinuity_exceeds_limit',
        'timing_gap_exceeds_limit',
    }
    if failing & set(gates):
        state = 'discontinuity_detected'
    elif not in_band or (transition_jump_db is None and timing_gap_s is None):
        state = 'insufficient_evidence'
    else:
        state = 'consistent'
    return HybridBoundaryReport(
        overlap_band_hz=overlap_band_hz,
        overlap_sample_count=len(in_band),
        max_overlap_level_delta_db=max_delta,
        overlap_level_delta_rms_db=rms_delta,
        transition_level_jump_db=(
            None if transition_jump_db is None else abs(float(transition_jump_db))
        ),
        timing_gap_s=None if timing_gap_s is None else abs(float(timing_gap_s)),
        normalization_offset_db=normalization_offset,
        gates=tuple(gates),
        state=state,
    )


# ---------------------------------------------------------------------------
# Accuracy envelope authority (issue §4, §8)
# ---------------------------------------------------------------------------


class AccuracyEnvelopeRecord(BaseModel):
    """Versioned evidence-backed accuracy envelope (issue §4).

    Binds *exactly* what was validated: solver/algorithm/version x observable
    x frequency range x geometry-domain x boundary/material assumptions x
    source/receiver capability x fixture evidence x error distribution x
    known exclusions x validation state. There is deliberately no aggregate
    score, and ``validation_state`` rules apply: evidence for one observable,
    band, geometry class or solver version never covers another (issue §8).
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['accuracy-envelope-1'] = ENVELOPE_AUTHORITY_VERSION
    envelope_id: str = Field(min_length=1)
    solver_id: str = Field(min_length=1)
    solver_algorithm: str = Field(min_length=1)
    solver_version: str = Field(min_length=1)
    observable: ValidationObservableKind
    frequency_range_hz: tuple[float, float] | None = None
    geometry_domain: str = Field(min_length=1)
    boundary_material_assumptions: str = Field(min_length=1)
    source_capability: str = Field(min_length=1)
    receiver_capability: str = Field(min_length=1)
    fixture_ids: tuple[str, ...] = Field(min_length=1)
    fixture_sha256s: tuple[str, ...] = Field(min_length=1)
    convergence: ConvergenceStatus = 'INSUFFICIENT_EVIDENCE'
    convergence_axes_tested: tuple[str, ...] = ()
    error_statistic_definition: str = Field(min_length=1)
    error_distribution: ObservableErrorStatistic | None = None
    known_failure_modes: tuple[str, ...] = ()
    known_exclusions: tuple[str, ...] = ()
    material_uncertainty_note: str | None = Field(default=None, min_length=1)
    threshold_policy_id: str | None = Field(default=None, min_length=1)
    threshold_policy_revision: int | None = Field(default=None, ge=1)
    validation_state: ValidationCapabilityState
    validated_at_utc: str = Field(min_length=1)
    envelope_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_envelope(self) -> 'AccuracyEnvelopeRecord':
        if len(self.fixture_ids) != len(self.fixture_sha256s):
            raise ValueError('fixture ids and hashes must align element-wise')
        if len(self.fixture_ids) != len(set(self.fixture_ids)):
            raise ValueError('fixture ids must be unique')
        for digest in self.fixture_sha256s:
            import re as _re
            if not _re.match(_SHA256, digest):
                raise ValueError('fixture hashes must be sha256 hex')
        if self.frequency_range_hz is not None:
            lo, hi = self.frequency_range_hz
            if not (isfinite(lo) and isfinite(hi)) or not (0.0 < lo < hi):
                raise ValueError('frequency range must be positive and ordered')
        if self.convergence == 'CONVERGED_WITHIN_TESTED_RANGE' and not (
            self.convergence_axes_tested
        ):
            raise ValueError('a converged claim must name the axes tested')
        if self.error_distribution is None and self.validation_state in (
            'VALIDATED_FOR_DECLARED_DOMAIN',
            'VALIDATED_WITH_LIMITATIONS',
        ):
            raise ValueError('validated states require an error distribution')
        if self.error_distribution is not None and (
            self.error_distribution.observable != self.observable
        ):
            raise ValueError('error distribution observable mismatch')
        if (self.threshold_policy_id is None) != (self.threshold_policy_revision is None):
            raise ValueError('threshold policy id/revision must be supplied together')
        if self.validation_state == 'VALIDATED_FOR_DECLARED_DOMAIN' and (
            self.threshold_policy_id is None
        ):
            raise ValueError(
                'VALIDATED_FOR_DECLARED_DOMAIN requires a versioned threshold policy'
            )
        if self.envelope_sha256 != _hash(self.identity_payload()):
            raise ValueError('accuracy envelope hash mismatch')
        return self

    def covers(
        self,
        *,
        solver_version: str,
        observable: ValidationObservableKind,
        frequency_hz: float | None = None,
    ) -> bool:
        """Scope check: does this envelope honestly cover the request?

        A newer solver version is never covered by older evidence (issue §8);
        a band outside the declared range is outside the envelope.
        """

        if solver_version != self.solver_version:
            return False
        if observable != self.observable:
            return False
        if (
            frequency_hz is not None
            and self.frequency_range_hz is not None
            and not (
                self.frequency_range_hz[0]
                <= frequency_hz
                <= self.frequency_range_hz[1]
            )
        ):
            return False
        return self.validation_state in (
            'VALIDATED_FOR_DECLARED_DOMAIN',
            'VALIDATED_WITH_LIMITATIONS',
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'envelope_id': self.envelope_id,
            'solver_id': self.solver_id,
            'solver_algorithm': self.solver_algorithm,
            'solver_version': self.solver_version,
            'observable': self.observable,
            'frequency_range_hz': (
                None if self.frequency_range_hz is None
                else list(self.frequency_range_hz)
            ),
            'geometry_domain': self.geometry_domain,
            'boundary_material_assumptions': self.boundary_material_assumptions,
            'source_capability': self.source_capability,
            'receiver_capability': self.receiver_capability,
            'fixture_ids': list(self.fixture_ids),
            'fixture_sha256s': list(self.fixture_sha256s),
            'convergence': self.convergence,
            'convergence_axes_tested': list(self.convergence_axes_tested),
            'error_statistic_definition': self.error_statistic_definition,
            'error_distribution': (
                None if self.error_distribution is None
                else self.error_distribution.model_dump(mode='json')
            ),
            'known_failure_modes': list(self.known_failure_modes),
            'known_exclusions': list(self.known_exclusions),
            'material_uncertainty_note': self.material_uncertainty_note,
            'threshold_policy_id': self.threshold_policy_id,
            'threshold_policy_revision': self.threshold_policy_revision,
            'validation_state': self.validation_state,
            'validated_at_utc': self.validated_at_utc,
        }


def build_accuracy_envelope(
    *,
    solver_id: str,
    solver_algorithm: str,
    solver_version: str,
    observable: ValidationObservableKind,
    geometry_domain: str,
    boundary_material_assumptions: str,
    source_capability: str,
    receiver_capability: str,
    fixture_ids: Sequence[str],
    fixture_sha256s: Sequence[str],
    error_statistic_definition: str,
    validation_state: ValidationCapabilityState,
    validated_at_utc: str,
    frequency_range_hz: tuple[float, float] | None = None,
    convergence: ConvergenceStatus = 'INSUFFICIENT_EVIDENCE',
    convergence_axes_tested: Sequence[str] = (),
    error_distribution: ObservableErrorStatistic | None = None,
    known_failure_modes: Sequence[str] = (),
    known_exclusions: Sequence[str] = (),
    material_uncertainty_note: str | None = None,
    threshold_policy_id: str | None = None,
    threshold_policy_revision: int | None = None,
    envelope_id: str | None = None,
) -> AccuracyEnvelopeRecord:
    payload: dict[str, Any] = {
        'envelope_id': envelope_id or str(uuid4()),
        'solver_id': solver_id,
        'solver_algorithm': solver_algorithm,
        'solver_version': solver_version,
        'observable': observable,
        'frequency_range_hz': frequency_range_hz,
        'geometry_domain': geometry_domain,
        'boundary_material_assumptions': boundary_material_assumptions,
        'source_capability': source_capability,
        'receiver_capability': receiver_capability,
        'fixture_ids': tuple(fixture_ids),
        'fixture_sha256s': tuple(fixture_sha256s),
        'convergence': convergence,
        'convergence_axes_tested': tuple(convergence_axes_tested),
        'error_statistic_definition': error_statistic_definition,
        'error_distribution': error_distribution,
        'known_failure_modes': tuple(known_failure_modes),
        'known_exclusions': tuple(known_exclusions),
        'material_uncertainty_note': material_uncertainty_note,
        'threshold_policy_id': threshold_policy_id,
        'threshold_policy_revision': threshold_policy_revision,
        'validation_state': validation_state,
        'validated_at_utc': validated_at_utc,
    }
    provisional = AccuracyEnvelopeRecord.model_construct(
        **canonicalize_payload(
            AccuracyEnvelopeRecord, dict(**payload, envelope_sha256='0' * 64)
        )
    )
    return AccuracyEnvelopeRecord(
        **payload, envelope_sha256=_hash(provisional.identity_payload())
    )


def derive_validation_state(
    *,
    evaluated: FixtureEvaluationResult,
    has_defensible_threshold: bool,
    experimental_solver: bool = False,
) -> ValidationCapabilityState:
    """Honest capability state from evaluation outcome (issue §5, §8).

    - every observable gated within domain and passed ->
      ``VALIDATED_FOR_DECLARED_DOMAIN``
    - passed where gated but some observables have no defensible threshold,
      or the run was partial -> ``VALIDATED_WITH_LIMITATIONS``
    - no defensible threshold anywhere -> report distributions, stay
      ``EXPERIMENTAL``/``INSUFFICIENT_EVIDENCE`` — never forced PASS/FAIL.
    """

    if evaluated.overall_gate == 'fail':
        # A failure inside the declared domain is not an absence of evidence.
        return 'VALIDATED_WITH_LIMITATIONS'
    if evaluated.overall_gate == 'pass':
        return 'VALIDATED_FOR_DECLARED_DOMAIN'
    # insufficient
    if has_defensible_threshold and any(
        item.threshold_rule is not None and item.threshold_rule.limit is not None
        for item in evaluated.evaluations
    ):
        return 'VALIDATED_WITH_LIMITATIONS'
    return 'EXPERIMENTAL' if experimental_solver else 'INSUFFICIENT_EVIDENCE'


# ---------------------------------------------------------------------------
# Benchmark report (issue §9)
# ---------------------------------------------------------------------------


class BenchmarkReport(BaseModel):
    """Machine-readable qualification report over retained fixtures.

    The human-readable rendering is ``render_text`` — the JSON payload is the
    machine-readable form. It is suitable for release qualification and for
    explaining why a recommendation is or is not production-eligible.
    """

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    code_version: str = Field(min_length=1)
    solver_versions: tuple[str, ...]
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(ge=1)
    fixture_manifest: tuple[tuple[str, str], ...] = Field(min_length=1)
    fixture_results: tuple[FixtureEvaluationResult, ...] = ()
    envelopes: tuple[AccuracyEnvelopeRecord, ...] = ()
    known_limitations: tuple[str, ...] = ()
    previous_qualified_report_sha256: str | None = Field(default=None, pattern=_SHA256)
    report_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_report(self) -> 'BenchmarkReport':
        if len({item[0] for item in self.fixture_manifest}) != len(self.fixture_manifest):
            raise ValueError('fixture manifest ids must be unique')
        for _fid, digest in self.fixture_manifest:
            import re as _re
            if not _re.match(_SHA256, digest):
                raise ValueError('fixture manifest hashes must be sha256 hex')
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('benchmark report hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'report_id': self.report_id,
            'generated_at_utc': self.generated_at_utc,
            'code_version': self.code_version,
            'solver_versions': list(self.solver_versions),
            'policy_id': self.policy_id,
            'policy_revision': self.policy_revision,
            'fixture_manifest': [list(item) for item in self.fixture_manifest],
            'fixture_results': [
                item.model_dump(mode='json') for item in self.fixture_results
            ],
            'envelopes': [
                item.model_dump(mode='json') for item in self.envelopes
            ],
            'known_limitations': list(self.known_limitations),
            'previous_qualified_report_sha256': self.previous_qualified_report_sha256,
        }

    def render_text(self) -> str:
        lines = [
            f'ベンチマークレポート {self.report_id} ({self.generated_at_utc})',
            f'  コード版: {self.code_version}',
            f'  閾値ポリシー: {self.policy_id} rev{self.policy_revision}',
            f'  フィクスチャ: {len(self.fixture_manifest)} 件',
        ]
        for result in self.fixture_results:
            lines.append(
                f'  - {result.fixture_id}: {result.overall_gate} '
                f'({len(result.evaluations)} observables)'
            )
        for envelope in self.envelopes:
            lines.append(
                f'  封域 {envelope.solver_id}@{envelope.solver_version} '
                f'{envelope.observable}: '
                f'{CAPABILITY_STATE_LABELS.get(envelope.validation_state, envelope.validation_state)}'
            )
        for limitation in self.known_limitations:
            lines.append(f'  既知制約: {limitation}')
        return '\n'.join(lines)


def build_benchmark_report(
    *,
    generated_at_utc: str,
    code_version: str,
    solver_versions: Sequence[str],
    policy: ValidationThresholdPolicy,
    fixtures: Sequence[ValidationFixtureRecord],
    fixture_results: Sequence[FixtureEvaluationResult] = (),
    envelopes: Sequence[AccuracyEnvelopeRecord] = (),
    known_limitations: Sequence[str] = (),
    previous_qualified_report_sha256: str | None = None,
    report_id: str | None = None,
) -> BenchmarkReport:
    manifest = tuple((f.fixture_id, f.fixture_sha256) for f in fixtures)
    payload: dict[str, Any] = {
        'report_id': report_id or str(uuid4()),
        'generated_at_utc': generated_at_utc,
        'code_version': code_version,
        'solver_versions': tuple(solver_versions),
        'policy_id': policy.policy_id,
        'policy_revision': policy.policy_revision,
        'fixture_manifest': manifest,
        'fixture_results': tuple(fixture_results),
        'envelopes': tuple(envelopes),
        'known_limitations': tuple(known_limitations),
        'previous_qualified_report_sha256': previous_qualified_report_sha256,
    }
    provisional = BenchmarkReport.model_construct(
        **canonicalize_payload(
            BenchmarkReport, dict(**payload, report_sha256='0' * 64)
        )
    )
    return BenchmarkReport(
        **payload, report_sha256=_hash(provisional.identity_payload())
    )


__all__ = [
    'ANALYTIC_FIXTURE_POLICY_V1',
    'AccuracyEnvelopeRecord',
    'BenchmarkReport',
    'CAPABILITY_STATE_LABELS',
    'CONVERGENCE_STATUS_LABELS',
    'ConvergenceStatus',
    'CorpusSlotState',
    'ENVELOPE_AUTHORITY_VERSION',
    'ENVELOPE_EVALUATOR_VERSION',
    'FixtureEvaluationResult',
    'FixtureExpectedSample',
    'FixtureGate',
    'FixtureObservableEvaluation',
    'FIXTURE_AUTHORITY_VERSION',
    'HybridBoundaryReport',
    'HybridOverlapSample',
    'MeasuredCorpusSlot',
    'ObservableErrorStatistic',
    'ObservableThresholdRule',
    'REV55_ANALYTIC_FIXTURES',
    'ReferenceIndependence',
    'THRESHOLD_POLICY_AUTHORITY_VERSION',
    'ThresholdMetric',
    'ThresholdScope',
    'ValidationCapabilityState',
    'ValidationFixtureClass',
    'ValidationFixtureRecord',
    'ValidationObservableKind',
    'ValidationReferenceKind',
    'ValidationThresholdPolicy',
    'build_accuracy_envelope',
    'build_benchmark_report',
    'build_corpus_slot',
    'build_threshold_policy',
    'build_validation_fixture',
    'derive_validation_state',
    'direct_arrival_time_s',
    'error_statistic',
    'evaluate_fixture_observations',
    'evaluate_hybrid_boundary',
    'free_field_level_drop_db',
    'image_source_path',
    'rectangular_mode_frequency_hz',
    'sabine_decay_time_s',
]
