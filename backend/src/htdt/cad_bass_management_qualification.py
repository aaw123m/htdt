"""REV56-BASSSTIM: bass-management/crossover qualification (issue #574).

:class:`BassManagementProfile` (#633) is the routing/topology authority.
This module is the *qualification* contract: under what evidence may HTDT
say a main↔sub splice, the LFE path and the sub headroom budget are
qualified for the deployed system — and under what evidence must it report
a smaller claim or none.

What the evidence has to show (all mechanical, never advisory):

- **Splice summation** (issue §5/§10): isolated main and sub path captures
  plus a measured sum. ``worst_splice_margin_db`` = the deepest point of
  ``summed − max(main, sub)`` inside the declared crossover band. Constructive
  summation sits above ~0 dB margin; a deep negative margin is the measured
  signature of phase cancellation (path-length/polarity/delay mismatch — the
  λ/2 condition of the alignment literature). Magnitude-only evidence can
  still detect cancellation but cannot explain it; phase/delay evidence
  upgrades the diagnosis to a specific reason.
- **Usable band** (§6): the crossover must sit where *both* sides have
  evidenced usable output — main extension below fc and sub reach above fc —
  under a declared margin. A nominal manufacturer −3 dB point is not
  evidence.
- **Headroom** (§8): the sub path carries redirected bass from every
  managed channel *plus* LFE *plus* declared EQ boost. The requirement is
  the coherent worst-case sum; the capability is a declared headroom
  figure — neither is invented.
- **Multi-seat** (§12): ``qualified_region`` needs holdout-seat splice
  evidence; a splice verified only at the measurement seat may claim
  ``qualified_point`` — the MLP is not the listening area.
- **Deployed state** (§11): full qualification requires the *applied*
  profile's hash to pin the evidence — a nominal setting that was never
  observed on the device caps the claim at ``candidate``.

Failure reasons are the issue's taxonomy, not a generic score.

Literature basis (issue #574 + investigation): Dirac Live Bass Control
phase-alignment practice (choose a crossover region where both sides carry
usable energy; an apparently reasonable nominal crossover can produce a
deep splice cancellation), Dolby bass-management semantics (discrete LFE
vs redirected bass stay separate signal concepts), Trinnov Optimizer
routing/headroom warnings, CEDIA/CTA-RP22 bass-management definition and
typical 80–120 Hz redirect range, THX-style 4th-order-LP/2nd-order-HP
topology, +10 dB in-band LFE headroom practice, and the field-measurement
workflow "measure each source solo, align, measure combined and verify".
"""

from __future__ import annotations

from math import isfinite, log10
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_bass_management import (
    BassManagementProfile,
    CrossoverSpec,
    evaluate_bass_management,
)
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


_SHA256_PATTERN = r'^[0-9a-f]{64}$'
_ENERGY_EPSILON = 1e-18


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _sha_or_none(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if len(value) != 64:
        raise ValueError(f'{label} must be 64 hex characters')
    int(value, 16)
    return value


# ---------------------------------------------------------------------------
# Taxonomy


SplicePath = Literal[
    'main_only',
    'sub_only',
    'main_plus_sub_summed',
    'lfe_only',
    'redirected_bass_only',
]
"""Isolated vs summed capture paths (issue §10) — needed to tell a main/sub
phase notch apart from a routing error or a room mode."""


SeatRole = Literal['control', 'holdout', 'repeatability']
"""Measurement-seat partition: ``control`` seats are the seats evidence is
allowed to qualify; ``holdout`` seats verify spatial generalization;
``repeatability`` proves stability, never spatial coverage."""


BassQualificationFailure = Literal[
    'main_too_weak_below_crossover',
    'sub_too_weak_above_crossover',
    'phase_cancellation_at_splice',
    'polarity_mismatch',
    'delay_mismatch',
    'lfe_routing_error',
    'redirected_bass_routing_error',
    'double_bass',
    'insufficient_sub_headroom',
    'unknown_device_filter_topology',
    'device_state_mismatch',
    'multi_seat_instability',
]
"""Issue §14 failure taxonomy — every reason is specific."""


BassQualificationScope = Literal[
    'unqualified',
    'candidate',
    'qualified_point',
    'qualified_region',
]


BassQualificationStatus = Literal[
    'qualified',
    'qualified_with_limitations',
    'not_qualified',
    'insufficient_evidence',
]


GateStatus = Literal['pass', 'fail', 'limitation', 'not_evaluated']


ProcessingStageKind = Literal[
    'channel_correction',
    'bass_management_hpf_lpf',
    'sub_summation_alignment',
    'system_target_eq',
    'limiter_protection',
    'device_specific_other',
]
"""Issue §13 processing order — filter order changes phase, headroom and
the final response, so the order is part of the qualified identity."""


ObservedDeployState = Literal[
    'nominal_declared',
    'deployed_observed',
    'remeasured_post_apply',
]


# ---------------------------------------------------------------------------
# Evidence records


class ResponseCurve(BaseModel):
    """One measured transfer curve for a splice path.

    ``phase_deg`` is optional — magnitude-only evidence can still detect a
    cancellation but cannot diagnose *why* (delay vs polarity). Every
    sample axis is part of the evidence; missing phase is honest UNKNOWN,
    never reconstructed.
    """

    model_config = ConfigDict(frozen=True)

    frequencies_hz: tuple[float, ...] = Field(min_length=2)
    magnitude_db: tuple[float, ...] = Field(min_length=2)
    phase_deg: tuple[float, ...] | None = None
    delay_s_applied: float | None = None
    polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def _check(self) -> 'ResponseCurve':
        if len(self.frequencies_hz) != len(self.magnitude_db):
            raise ValueError('response curve axes must have equal length')
        if self.phase_deg is not None and (
            len(self.phase_deg) != len(self.frequencies_hz)
        ):
            raise ValueError('phase axis must match frequency axis length')
        prev = -1.0
        for value in self.frequencies_hz:
            if not isfinite(value) or value <= 0:
                raise ValueError('response frequencies must be positive')
            if value <= prev:
                raise ValueError('response frequencies must be increasing')
            prev = value
        for value in self.magnitude_db:
            if not isfinite(value):
                raise ValueError('response magnitude must be finite')
        if self.phase_deg is not None:
            for value in self.phase_deg:
                if not isfinite(value):
                    raise ValueError('response phase must be finite')
        if self.delay_s_applied is not None:
            if not isfinite(self.delay_s_applied):
                raise ValueError('applied delay must be finite')
        return self

    def magnitude_at(self, frequency_hz: float) -> float:
        """Linear interpolation between measured samples."""
        return float(
            np.interp(
                frequency_hz,
                np.asarray(self.frequencies_hz, dtype=float),
                np.asarray(self.magnitude_db, dtype=float),
            )
        )


class SplicePathEvidence(BaseModel):
    """One captured path response bound to role/sub/seat and (optionally) a
    stimulus pin and dataset — a notch without a pinned stimulus is still
    evidence, but a stimulus pin strengthens reproducibility claims."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    sub_group_id: str = Field(min_length=1)
    seat_id: str = Field(min_length=1)
    seat_role: SeatRole = 'control'
    path: SplicePath
    curve: ResponseCurve
    stimulus_pin_id: str | None = None
    measurement_dataset_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    observed_state: ObservedDeployState = 'nominal_declared'
    captured_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'evidence_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'SplicePathEvidence':
        _require_iso8601(self.captured_at_utc, 'splice evidence captured_at_utc')
        if self.evidence_sha256 != _hash(self.semantic_payload()):
            raise ValueError('splice evidence hash mismatch')
        return self


def build_splice_evidence(
    *,
    evidence_id: str,
    document_id: str,
    role_id: str,
    sub_group_id: str,
    seat_id: str,
    path: SplicePath,
    curve: ResponseCurve,
    seat_role: SeatRole = 'control',
    stimulus_pin_id: str | None = None,
    measurement_dataset_sha256: str | None = None,
    observed_state: ObservedDeployState = 'nominal_declared',
    captured_at_utc: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> SplicePathEvidence:
    provisional = SplicePathEvidence.model_construct(**canonicalize_payload(SplicePathEvidence, dict(
        evidence_id=evidence_id,
        document_id=document_id,
        role_id=role_id,
        sub_group_id=sub_group_id,
        seat_id=seat_id,
        seat_role=seat_role,
        path=path,
        curve=curve,
        stimulus_pin_id=stimulus_pin_id,
        measurement_dataset_sha256=measurement_dataset_sha256,
        observed_state=observed_state,
        captured_at_utc=captured_at_utc or _utc_now(),
        provenance=tuple(provenance),
        evidence_sha256='0' * 64,
    )))
    return SplicePathEvidence(
        **provisional.model_dump(mode='python', exclude={'evidence_sha256'}),
        evidence_sha256=_hash(provisional.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Splice summation analysis


class SpliceMetrics(BaseModel):
    """Measured splice behaviour inside the crossover band."""

    model_config = ConfigDict(frozen=True)

    crossover_band_low_hz: float
    crossover_band_high_hz: float
    worst_splice_margin_db: float | None = None
    """min over the band of ``summed − max(main, sub)`` — constructive sum
    is ≥ ~0 dB; a deep negative value is measured cancellation."""
    worst_margin_frequency_hz: float | None = None
    predicted_vs_measured_error_db: float | None = None
    """RMS of |measured − predicted complex sum| — only when both isolated
    paths carry phase. ``None`` when phase evidence is absent (honest
    UNKNOWN, not a synthesized prediction)."""


class SpliceVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal['constructive', 'acceptable', 'cancellation', 'unknown']
    failure_reasons: tuple[BassQualificationFailure, ...] = ()
    metrics: SpliceMetrics
    detail: str | None = None


def _band_indices(
    frequencies: tuple[float, ...], low_hz: float, high_hz: float
) -> np.ndarray:
    freq = np.asarray(frequencies, dtype=float)
    return np.nonzero((freq >= low_hz) & (freq <= high_hz))[0]


def evaluate_splice(
    *,
    main_curve: ResponseCurve | None,
    sub_curve: ResponseCurve | None,
    summed_curve: ResponseCurve | None,
    crossover_band_hz: tuple[float, float],
    cancellation_threshold_db: float = 6.0,
    prediction_tolerance_db: float = 3.0,
) -> SpliceVerdict:
    """Qualify one main↔sub splice from measured evidence.

    ``cancellation_threshold_db`` is a declared margin, not a hidden magic
    number: a summed response dipping more than this below the stronger of
    the two isolated paths is *measured* cancellation (the λ/2 path-length
    / polarity / delay condition of the alignment literature).
    """
    low_hz, high_hz = crossover_band_hz
    if not (0 < low_hz < high_hz):
        raise ValueError('crossover band must satisfy 0 < low < high')
    if main_curve is None or sub_curve is None:
        return SpliceVerdict(
            status='unknown',
            metrics=SpliceMetrics(
                crossover_band_low_hz=low_hz,
                crossover_band_high_hz=high_hz,
            ),
            detail='isolated main/sub path evidence is required — a summed '
            'response alone cannot attribute a splice defect',
        )
    if summed_curve is None:
        return SpliceVerdict(
            status='unknown',
            metrics=SpliceMetrics(
                crossover_band_low_hz=low_hz,
                crossover_band_high_hz=high_hz,
            ),
            detail='no measured summed response — predicted alignment is '
            'not verification',
        )

    # Magnitude check on the summed-vs-isolated margin across the band.
    # Only summed samples inside BOTH isolated curves' measured range
    # count — interpolation clamps flat outside, so anything beyond the
    # shared domain would compare against a fabricated endpoint level.
    shared_low = max(
        main_curve.frequencies_hz[0], sub_curve.frequencies_hz[0]
    )
    shared_high = min(
        main_curve.frequencies_hz[-1], sub_curve.frequencies_hz[-1]
    )
    band_low = max(low_hz, shared_low)
    band_high = min(high_hz, shared_high)
    idx = _band_indices(summed_curve.frequencies_hz, band_low, band_high)
    if not idx.size:
        return SpliceVerdict(
            status='unknown',
            metrics=SpliceMetrics(
                crossover_band_low_hz=low_hz,
                crossover_band_high_hz=high_hz,
            ),
            detail='isolated main/sub measurements do not jointly cover '
            'any summed sample inside the crossover band',
        )
    margins = np.array(
        [
            summed_curve.magnitude_db[int(i)]
            - max(
                main_curve.magnitude_at(
                    summed_curve.frequencies_hz[int(i)]
                ),
                sub_curve.magnitude_at(
                    summed_curve.frequencies_hz[int(i)]
                ),
            )
            for i in idx
        ]
    )
    worst = float(margins.min())
    worst_at = float(summed_curve.frequencies_hz[int(idx[int(margins.argmin())])])

    # Predicted-vs-measured complex sum — only with real phase evidence.
    prediction_error: float | None = None
    phase_verdict_note: str | None = None
    if (
        main_curve.phase_deg is not None
        and sub_curve.phase_deg is not None
    ):
        band_freq = np.asarray(
            [summed_curve.frequencies_hz[int(i)] for i in idx]
        )
        main_phase = np.deg2rad(
            np.interp(
                band_freq,
                np.asarray(main_curve.frequencies_hz),
                np.asarray(main_curve.phase_deg),
            )
        )
        sub_phase = np.deg2rad(
            np.interp(
                band_freq,
                np.asarray(sub_curve.frequencies_hz),
                np.asarray(sub_curve.phase_deg),
            )
        )
        # Declared delay shifts the sub phase by −2πf·Δt; declared polarity
        # inversion adds π.
        delay = sub_curve.delay_s_applied or 0.0
        if delay:
            sub_phase = sub_phase - 2.0 * np.pi * band_freq * delay
        if sub_curve.polarity == 'inverted':
            sub_phase = sub_phase + np.pi
        if main_curve.polarity == 'inverted':
            main_phase = main_phase + np.pi
        main_lin = 10.0 ** (
            np.interp(
                band_freq,
                np.asarray(main_curve.frequencies_hz),
                np.asarray(main_curve.magnitude_db),
            )
            / 20.0
        )
        sub_lin = 10.0 ** (
            np.interp(
                band_freq,
                np.asarray(sub_curve.frequencies_hz),
                np.asarray(sub_curve.magnitude_db),
            )
            / 20.0
        )
        predicted = np.abs(
            main_lin * np.exp(1j * main_phase)
            + sub_lin * np.exp(1j * sub_phase)
        )
        predicted_db = 20.0 * np.log10(np.maximum(predicted, _ENERGY_EPSILON))
        measured_db = np.asarray(
            [summed_curve.magnitude_db[int(i)] for i in idx]
        )
        prediction_error = float(
            np.sqrt(np.mean((measured_db - predicted_db) ** 2))
        )
        if prediction_error > prediction_tolerance_db:
            phase_verdict_note = (
                'measured sum departs from the isolated-path prediction '
                f'by {prediction_error:.1f} dB RMS — deployed state differs '
                'from declared (level/delay/polarity applied differently '
                'than recorded)'
            )
    else:
        phase_verdict_note = (
            'isolated paths carry no phase evidence — cancellation is '
            'detectable but not attributable to delay vs polarity'
        )

    failures: list[BassQualificationFailure] = []
    if worst <= -cancellation_threshold_db:
        # Attribute the cancellation when the evidence supports it.
        if (
            sub_curve.polarity == 'inverted'
            or main_curve.polarity == 'inverted'
        ):
            failures.append('polarity_mismatch')
        elif sub_curve.delay_s_applied and delay_cancels(
            sub_curve.delay_s_applied, worst_at
        ):
            failures.append('delay_mismatch')
        else:
            failures.append('phase_cancellation_at_splice')
    if prediction_error is not None and (
        prediction_error > prediction_tolerance_db
    ):
        failures.append('device_state_mismatch')

    status: Literal['constructive', 'acceptable', 'cancellation', 'unknown']
    if worst <= -cancellation_threshold_db:
        status = 'cancellation'
    elif worst < -cancellation_threshold_db / 2.0:
        status = 'acceptable'
    else:
        status = 'constructive'

    detail_bits = [
        f'worst splice margin {worst:.1f} dB at {worst_at:.0f} Hz'
    ]
    if phase_verdict_note:
        detail_bits.append(phase_verdict_note)
    return SpliceVerdict(
        status=status,
        failure_reasons=tuple(failures),
        metrics=SpliceMetrics(
            crossover_band_low_hz=low_hz,
            crossover_band_high_hz=high_hz,
            worst_splice_margin_db=worst,
            worst_margin_frequency_hz=worst_at,
            predicted_vs_measured_error_db=prediction_error,
        ),
        detail='; '.join(detail_bits),
    )


def delay_cancels(delay_s: float, notch_hz: float) -> bool:
    """Whether a delay puts the sub ~λ/2 out of phase at the notch: the
    half-wavelength cancellation condition (within ±25% of a half-period
    offset, modulo the period — an odd multiple of a *full* period is
    in-phase, not cancelling)."""
    if delay_s <= 0 or notch_hz <= 0:
        return False
    period = 1.0 / notch_hz
    fractional = (delay_s / period) % 1.0
    return abs(fractional - 0.5) < 0.25


# ---------------------------------------------------------------------------
# Usable-band + headroom gates


class UsableBandEvidence(BaseModel):
    """Declared/measured extension of each side around the crossover."""

    model_config = ConfigDict(frozen=True)

    main_usable_low_hz: float | None = None
    """Lowest frequency the main speaker evidenced usable output at."""
    sub_usable_high_hz: float | None = None
    """Highest frequency the sub path evidenced usable output at."""
    margin_hz: float = Field(default=5.0, ge=0.0)
    """Required clearance between the crossover and each edge."""


def evaluate_usable_band(
    *,
    crossover_hz: float,
    evidence: UsableBandEvidence | None,
) -> tuple[GateStatus, tuple[BassQualificationFailure, ...], str]:
    if evidence is None:
        return (
            'not_evaluated',
            (),
            'no usable-band evidence declared — crossover eligibility '
            'cannot be established',
        )
    failures: list[BassQualificationFailure] = []
    if evidence.main_usable_low_hz is None:
        return (
            'not_evaluated',
            ('main_too_weak_below_crossover',),
            'main-speaker low-frequency extension is unrecorded',
        )
    if evidence.sub_usable_high_hz is None:
        return (
            'not_evaluated',
            ('sub_too_weak_above_crossover',),
            'subwoofer upper usable range is unrecorded',
        )
    if evidence.main_usable_low_hz + evidence.margin_hz > crossover_hz:
        failures.append('main_too_weak_below_crossover')
    if evidence.sub_usable_high_hz - evidence.margin_hz < crossover_hz:
        failures.append('sub_too_weak_above_crossover')
    if failures:
        return (
            'fail',
            tuple(failures),
            f'crossover {crossover_hz:.0f} Hz sits outside the evidenced '
            'usable band of one side',
        )
    return (
        'pass',
        (),
        f'crossover {crossover_hz:.0f} Hz inside both usable bands with '
        f'{evidence.margin_hz:.0f} Hz margin',
    )


class SubPathLoad(BaseModel):
    """One simultaneous load on a sub output (issue §8)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['redirected_bass', 'lfe', 'eq_boost', 'distribution_gain']
    level_db: float
    """In-band worst-case level contribution (dB re the budget's reference
    axis — the qualification declares the axis, never mixes dBFS and SPL)."""

    @model_validator(mode='after')
    def _check(self) -> 'SubPathLoad':
        if not isfinite(self.level_db):
            raise ValueError('sub path load level must be finite')
        return self


class SubHeadroomBudget(BaseModel):
    """Combined signal stress vs declared capability on one sub path."""

    model_config = ConfigDict(frozen=True)

    sub_group_id: str = Field(min_length=1)
    loads: tuple[SubPathLoad, ...] = ()
    capability_headroom_db: float | None = None
    """Declared headroom of the sub path above the reference level — from
    capability evidence, never an assumed number."""
    level_axis: str = Field(min_length=1)
    """'dbfs' / 'db_spl' / … — the axis the loads and capability share."""
    required_margin_db: float = Field(default=0.0, ge=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'SubHeadroomBudget':
        if self.capability_headroom_db is not None and not isfinite(
            self.capability_headroom_db
        ):
            raise ValueError('capability headroom must be finite')
        return self

    def combined_load_db(self) -> float | None:
        """Coherent worst-case sum of all loads (worst-case in-phase
        addition — the conservative bound a protection gate needs)."""
        if not self.loads:
            return None
        total = sum(
            10.0 ** (load.level_db / 20.0) for load in self.loads
        )
        return 20.0 * log10(max(total, _ENERGY_EPSILON))


def evaluate_sub_headroom(
    budget: SubHeadroomBudget | None,
) -> tuple[GateStatus, tuple[BassQualificationFailure, ...], str]:
    if budget is None or not budget.loads:
        return (
            'not_evaluated',
            (),
            'no sub headroom budget — redirected+LFE+boost stress is '
            'unevaluated',
        )
    combined = budget.combined_load_db()
    if combined is None or budget.capability_headroom_db is None:
        return (
            'not_evaluated',
            (),
            'sub capability headroom is unrecorded — a flat low-level '
            'sweep is not program-level headroom evidence',
        )
    excess = combined - (
        budget.capability_headroom_db - budget.required_margin_db
    )
    if excess > 0:
        return (
            'fail',
            ('insufficient_sub_headroom',),
            f'combined sub-path load {combined:.1f} dB exceeds declared '
            f'headroom {budget.capability_headroom_db:.1f} dB '
            f'(margin {budget.required_margin_db:.1f} dB) by '
            f'{excess:.1f} dB',
        )
    return (
        'pass',
        (),
        f'combined sub-path load {combined:.1f} dB within declared '
        f'headroom {budget.capability_headroom_db:.1f} dB',
    )


# ---------------------------------------------------------------------------
# Processing order + qualification record


class ProcessingStage(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ProcessingStageKind
    detail: str | None = None


class SpliceGroupQualification(BaseModel):
    """Per (main role ↔ sub group) splice result."""

    model_config = ConfigDict(frozen=True)

    role_id: str = Field(min_length=1)
    sub_group_id: str = Field(min_length=1)
    seat_id: str = Field(min_length=1)
    seat_role: SeatRole = 'control'
    splice: SpliceVerdict
    usable_band_status: GateStatus = 'not_evaluated'
    usable_band_reasons: tuple[BassQualificationFailure, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class BassQualificationGate(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate: str = Field(min_length=1)
    status: GateStatus
    reason: str | None = None


class BassManagementQualification(BaseModel):
    """Sealed qualification verdict for a bass-management profile.

    The qualification binds the *profile hash* — a profile edited after
    qualification is stale evidence (``device_state_mismatch`` territory),
    never silently re-qualified.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['bass-qualification-1'] = 'bass-qualification-1'
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    lifecycle_at_evaluation: Literal['proposed', 'current', 'applied']
    processing_order: tuple[ProcessingStage, ...] = ()
    gates: tuple[BassQualificationGate, ...] = ()
    splice_groups: tuple[SpliceGroupQualification, ...] = ()
    status: BassQualificationStatus
    scope: BassQualificationScope
    failure_reasons: tuple[BassQualificationFailure, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_sha256', 'qualification_id'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'BassManagementQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        digest = _hash(self.semantic_payload())
        if self.qualification_sha256 != digest:
            raise ValueError('bass qualification hash mismatch')
        if self.qualification_id != 'bmq-' + digest[:24]:
            raise ValueError('bass qualification id mismatch')
        return self


def evaluate_bass_management_qualification(
    *,
    profile: BassManagementProfile,
    document_id: str,
    evidence: tuple[SplicePathEvidence, ...] = (),
    usable_band: dict[str, UsableBandEvidence] | None = None,
    headroom_budgets: dict[str, SubHeadroomBudget] | None = None,
    crossover_band_octaves: float = 0.5,
    cancellation_threshold_db: float = 6.0,
    prediction_tolerance_db: float = 3.0,
    processing_order: tuple[ProcessingStage, ...] = (),
    double_bass_roles: tuple[str, ...] = (),
    expected_role_ids: tuple[str, ...] = (),
    known_destination_ids: tuple[str, ...] = (),
    evaluated_at_utc: str | None = None,
) -> BassManagementQualification:
    """Evaluate a bass-management profile against measured evidence.

    Gates only demote, never promote:

    * ``routing`` — the #633 profile evaluation (redirect destinations
      recorded+resolved, LFE destinations, duplication policy).
    * ``usable_band`` — each high-passed role's crossover sits inside both
      sides' evidenced usable band.
    * ``splice`` — per (role, sub destination, seat) the measured
      summed-vs-isolated margin; a control-seat ``cancellation`` fails the
      role, a holdout-seat ``cancellation`` caps scope at best
      ``qualified_point`` and records ``multi_seat_instability``.
    * ``headroom`` — per destination group the coherent combined load fits
      declared capability.
    * ``deployed_state`` — full qualification needs evidence captured with
      ``observed_state='remeasured_post_apply'`` under an ``applied``
      profile; nominal-declared evidence caps scope at ``candidate``.
    * ``filter_topology`` — hidden device filters (``unknown`` family with
      no slope) are a recorded limitation, not a hidden pass.
    """
    gates: list[BassQualificationGate] = []
    failure_reasons: list[BassQualificationFailure] = []
    limitations: list[str] = []
    band = dict(usable_band or {})
    budgets = dict(headroom_budgets or {})

    # --- routing gate (reuse the profile evaluator, keep reasons specific)
    routing_eval = evaluate_bass_management(
        profile=profile,
        expected_role_ids=expected_role_ids,
        known_destination_ids=known_destination_ids,
    )
    routing_failed = any(c.status == 'FAIL' for c in routing_eval.checks)
    routing_unknown = any(c.status == 'UNKNOWN' for c in routing_eval.checks)
    for check in routing_eval.checks:
        if check.status == 'FAIL':
            if check.check.startswith('lfe'):
                failure_reasons.append('lfe_routing_error')
            else:
                failure_reasons.append('redirected_bass_routing_error')
    gates.append(
        BassQualificationGate(
            gate='routing',
            status=(
                'fail'
                if routing_failed
                else ('limitation' if routing_unknown else 'pass')
            ),
            reason=(
                'bass-management routing checks pass'
                if not (routing_failed or routing_unknown)
                else '; '.join(
                    f'{c.check}: {c.reason}'
                    for c in routing_eval.checks
                    if c.status in ('FAIL', 'UNKNOWN')
                )
            ),
        )
    )

    # --- double-bass detection -------------------------------------------
    double_bass_hit = False
    for role_id in double_bass_roles:
        rule = next(
            (r for r in profile.main_rules if r.logical_role_id == role_id),
            None,
        )
        if rule is not None and rule.handling == 'high_pass':
            double_bass_hit = True
    if double_bass_hit:
        failure_reasons.append('double_bass')
    gates.append(
        BassQualificationGate(
            gate='duplicate_low_frequency_path',
            status='fail' if double_bass_hit else 'pass',
            reason=(
                'declared roles reproduce redirected bass on both main and '
                'sub paths (LFE+Main semantics)'
                if double_bass_hit
                else 'no duplicated low-frequency reproduction path'
            ),
        )
    )

    # --- per-role usable band + splice -----------------------------------
    splice_groups: list[SpliceGroupQualification] = []
    band_any_fail = False
    band_any_unknown = False
    for rule in profile.main_rules:
        if rule.handling != 'high_pass' or rule.high_pass is None:
            continue
        xo = rule.high_pass
        gate_status, reasons, detail = evaluate_usable_band(
            crossover_hz=xo.frequency_hz,
            evidence=band.get(rule.logical_role_id),
        )
        band_any_fail = band_any_fail or gate_status == 'fail'
        band_any_unknown = (
            band_any_unknown or gate_status == 'not_evaluated'
        )
        for failure in reasons:
            if failure not in failure_reasons:
                failure_reasons.append(failure)
        gates.append(
            BassQualificationGate(
                gate=f'usable_band[{rule.logical_role_id}]',
                status=gate_status,
                reason=detail,
            )
        )
        # topology visibility limitation
        if xo.filter_family == 'unknown':
            limitations.append(
                f'{rule.logical_role_id}: device filter topology unknown — '
                'nominal crossover only'
            )
    gates.append(
        BassQualificationGate(
            gate='filter_topology',
            status='limitation' if limitations else 'pass',
            reason=(
                'some crossovers carry nominal-only device settings'
                if limitations
                else 'all crossover topologies recorded'
            ),
        )
    )

    # Splice evaluation per (role, destination group, seat).
    evidence_index: dict[tuple[str, str, str, SplicePath], SplicePathEvidence] = {}
    for ev in evidence:
        evidence_index[
            (ev.role_id, ev.sub_group_id, ev.seat_id, ev.path)
        ] = ev
    splice_any_fail = False
    splice_any_unknown = False
    holdout_cancel = False
    remeasured_seen = False
    for rule in profile.main_rules:
        if rule.handling != 'high_pass' or rule.high_pass is None:
            continue
        if not rule.redirected_destinations:
            continue
        xo = rule.high_pass.frequency_hz
        band_low = xo / (2.0 ** crossover_band_octaves)
        band_high = xo * (2.0 ** crossover_band_octaves)
        for dest in rule.redirected_destinations:
            seats = sorted(
                {
                    ev.seat_id
                    for ev in evidence
                    if ev.role_id == rule.logical_role_id
                    and ev.sub_group_id == dest
                }
            )
            if not seats:
                splice_any_unknown = True
                splice_groups.append(
                    SpliceGroupQualification(
                        role_id=rule.logical_role_id,
                        sub_group_id=dest,
                        seat_id='(none)',
                        splice=SpliceVerdict(
                            status='unknown',
                            metrics=SpliceMetrics(
                                crossover_band_low_hz=band_low,
                                crossover_band_high_hz=band_high,
                            ),
                            detail='no splice measurement for this '
                            'role↔sub pair',
                        ),
                        evidence_ids=(),
                    )
                )
                continue
            for seat in seats:
                def _get(path: SplicePath) -> SplicePathEvidence | None:
                    return evidence_index.get(
                        (rule.logical_role_id, dest, seat, path)
                    )

                seat_role = next(
                    ev.seat_role
                    for ev in evidence
                    if ev.role_id == rule.logical_role_id
                    and ev.sub_group_id == dest
                    and ev.seat_id == seat
                )
                if any(
                    (ev.observed_state == 'remeasured_post_apply')
                    for key, ev in evidence_index.items()
                    if key[0] == rule.logical_role_id
                    and key[1] == dest
                    and key[2] == seat
                ):
                    remeasured_seen = True
                verdict = evaluate_splice(
                    main_curve=(
                        _get('main_only').curve if _get('main_only') else None
                    ),
                    sub_curve=(
                        _get('sub_only').curve if _get('sub_only') else None
                    ),
                    summed_curve=(
                        _get('main_plus_sub_summed').curve
                        if _get('main_plus_sub_summed')
                        else None
                    ),
                    crossover_band_hz=(band_low, band_high),
                    cancellation_threshold_db=cancellation_threshold_db,
                    prediction_tolerance_db=prediction_tolerance_db,
                )
                for failure in verdict.failure_reasons:
                    if failure not in failure_reasons:
                        failure_reasons.append(failure)
                if verdict.status == 'cancellation':
                    if seat_role == 'holdout':
                        holdout_cancel = True
                    else:
                        splice_any_fail = True
                elif verdict.status == 'unknown':
                    splice_any_unknown = True
                used = tuple(
                    ev.evidence_id
                    for ev in (
                        _get('main_only'),
                        _get('sub_only'),
                        _get('main_plus_sub_summed'),
                    )
                    if ev is not None
                )
                splice_groups.append(
                    SpliceGroupQualification(
                        role_id=rule.logical_role_id,
                        sub_group_id=dest,
                        seat_id=seat,
                        seat_role=seat_role,
                        splice=verdict,
                        usable_band_status=gate_status_for(
                            gates, f'usable_band[{rule.logical_role_id}]'
                        ),
                        usable_band_reasons=(),
                        evidence_ids=used,
                    )
                )
    if holdout_cancel:
        failure_reasons.append('multi_seat_instability')
    splice_gate_status: GateStatus = 'pass'
    if splice_any_fail:
        splice_gate_status = 'fail'
    elif splice_any_unknown or holdout_cancel:
        splice_gate_status = 'limitation'
    gates.append(
        BassQualificationGate(
            gate='splice_summation',
            status=splice_gate_status,
            reason=(
                f'{sum(1 for g in splice_groups if g.splice.status == "cancellation")} '
                f'of {len(splice_groups)} splice measurements show '
                'cancellation in the crossover band'
            ),
        )
    )

    # --- headroom per destination group -----------------------------------
    headroom_fail = False
    headroom_unknown = False
    destination_ids = sorted(
        {
            dest
            for rule in profile.main_rules
            for dest in rule.redirected_destinations
        }
        | (
            set(profile.lfe_path.destinations)
            if profile.lfe_path is not None
            else set()
        )
    )
    for dest in destination_ids:
        gate_status, reasons, detail = evaluate_sub_headroom(
            budgets.get(dest)
        )
        headroom_fail = headroom_fail or gate_status == 'fail'
        headroom_unknown = headroom_unknown or gate_status == 'not_evaluated'
        for failure in reasons:
            if failure not in failure_reasons:
                failure_reasons.append(failure)
        gates.append(
            BassQualificationGate(
                gate=f'sub_headroom[{dest}]',
                status=gate_status,
                reason=detail,
            )
        )

    # --- deployed state ----------------------------------------------------
    if profile.lifecycle != 'applied':
        deployed_status: GateStatus = 'limitation'
        deployed_reason = (
            f'profile lifecycle is {profile.lifecycle!r} — evidence was not '
            'captured under a verified applied state'
        )
    elif not remeasured_seen:
        deployed_status = 'limitation'
        deployed_reason = (
            'profile is applied but no evidence is marked '
            "'remeasured_post_apply' — deployed state is assumed, not "
            'observed'
        )
    else:
        deployed_status = 'pass'
        deployed_reason = 'post-apply remeasure evidence present'
    gates.append(
        BassQualificationGate(
            gate='deployed_state',
            status=deployed_status,
            reason=deployed_reason,
        )
    )

    # --- verdict synthesis --------------------------------------------------
    hard_fail = (
        routing_failed
        or double_bass_hit
        or band_any_fail
        or splice_any_fail
        or headroom_fail
    )
    evidence_gaps = (
        routing_unknown
        or band_any_unknown
        or splice_any_unknown
        or headroom_unknown
    )
    if hard_fail:
        status: BassQualificationStatus = 'not_qualified'
        scope: BassQualificationScope = 'unqualified'
    elif evidence_gaps:
        status = 'insufficient_evidence'
        scope = 'candidate'
    else:
        holdout_evidence = any(
            g.seat_role == 'holdout' and g.splice.status in (
                'constructive', 'acceptable'
            )
            for g in splice_groups
        )
        all_control_constructive = all(
            g.splice.status in ('constructive', 'acceptable')
            for g in splice_groups
            if g.seat_role == 'control'
        ) and any(g.seat_role == 'control' for g in splice_groups)
        if (
            deployed_status == 'pass'
            and holdout_evidence
            and all_control_constructive
            and not holdout_cancel
        ):
            scope = 'qualified_region'
            status = 'qualified' if not limitations else (
                'qualified_with_limitations'
            )
        elif all_control_constructive:
            scope = (
                'qualified_point' if deployed_status == 'pass' else 'candidate'
            )
            status = 'qualified_with_limitations' if (
                scope == 'qualified_point'
            ) else 'insufficient_evidence'
        else:
            scope = 'candidate'
            status = 'insufficient_evidence'

    probe = BassManagementQualification.model_construct(**canonicalize_payload(BassManagementQualification, dict(
        qualification_id='',
        document_id=document_id,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        lifecycle_at_evaluation=profile.lifecycle,
        processing_order=tuple(processing_order),
        gates=tuple(gates),
        splice_groups=tuple(splice_groups),
        status=status,
        scope=scope,
        failure_reasons=tuple(dict.fromkeys(failure_reasons)),
        limitations=tuple(limitations),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
        qualification_sha256='0' * 64,
    )))
    digest = _hash(probe.semantic_payload())
    return BassManagementQualification(
        **probe.model_dump(
            mode='python',
            exclude={'qualification_sha256', 'qualification_id'},
        ),
        qualification_id='bmq-' + digest[:24],
        qualification_sha256=digest,
    )


def gate_status_for(
    gates: list[BassQualificationGate] | tuple[BassQualificationGate, ...],
    name: str,
) -> GateStatus:
    for gate in gates:
        if gate.gate == name:
            return gate.status
    return 'not_evaluated'


__all__ = [
    'BassManagementQualification',
    'BassQualificationFailure',
    'BassQualificationGate',
    'BassQualificationScope',
    'BassQualificationStatus',
    'GateStatus',
    'ObservedDeployState',
    'ProcessingStage',
    'ProcessingStageKind',
    'ResponseCurve',
    'SeatRole',
    'SpliceGroupQualification',
    'SpliceMetrics',
    'SplicePath',
    'SplicePathEvidence',
    'SpliceVerdict',
    'SubHeadroomBudget',
    'SubPathLoad',
    'UsableBandEvidence',
    'build_splice_evidence',
    'delay_cancels',
    'evaluate_bass_management_qualification',
    'evaluate_splice',
    'evaluate_sub_headroom',
    'evaluate_usable_band',
    'gate_status_for',
]
