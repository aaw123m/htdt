from __future__ import annotations

from itertools import combinations
import json
from math import isfinite, sqrt
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .comparison import FrequencyResponse, compare_frequency_responses
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _canonical_sha256


ValidationGate = Literal['pass', 'fail', 'insufficient']






class CadObjectiveValidationSample(BaseModel):
    """One candidate/objective prediction vs measured observation."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    split: Literal['calibration', 'holdout']
    objective_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    predicted_evaluation_id: str = Field(min_length=1)
    measured_evaluation_id: str = Field(min_length=1)
    predicted_value: float
    measured_value: float

    @model_validator(mode='after')
    def finite_values(self) -> 'CadObjectiveValidationSample':
        if not isfinite(float(self.predicted_value)) or not isfinite(float(self.measured_value)):
            raise ValueError('objective validation values must be finite')
        return self


class CadTrendCheck(BaseModel):
    """Pairwise ordering agreement for one independent objective on holdout candidates."""

    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    holdout_candidate_ids: tuple[str, ...] = Field(min_length=2)
    tie_tolerance: float = Field(ge=0)
    min_comparable_pairs: int = Field(ge=1)
    min_agreement_ratio: float = Field(ge=0, le=1)
    comparable_pairs: int = Field(ge=0)
    concordant_pairs: int = Field(ge=0)
    discordant_pairs: int = Field(ge=0)
    ambiguous_pairs: int = Field(ge=0)
    agreement_ratio: float | None = Field(default=None, ge=0, le=1)
    gate: ValidationGate

    @model_validator(mode='after')
    def valid_counts(self) -> 'CadTrendCheck':
        total = self.concordant_pairs + self.discordant_pairs
        if total != self.comparable_pairs:
            raise ValueError('trend comparable pair count mismatch')
        if self.agreement_ratio is None:
            if self.comparable_pairs:
                raise ValueError('trend agreement ratio missing for comparable pairs')
        else:
            expected = self.concordant_pairs / self.comparable_pairs
            if abs(self.agreement_ratio - expected) > 1e-12:
                raise ValueError('trend agreement ratio mismatch')
        expected_gate: ValidationGate
        if self.comparable_pairs < self.min_comparable_pairs:
            expected_gate = 'insufficient'
        elif self.agreement_ratio is not None and self.agreement_ratio >= self.min_agreement_ratio:
            expected_gate = 'pass'
        else:
            expected_gate = 'fail'
        if self.gate != expected_gate:
            raise ValueError('trend gate mismatch')
        return self


class CadSensitivityCheck(BaseModel):
    """Objective response to an explicit nearby placement perturbation."""

    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    candidate_a_id: str = Field(min_length=1)
    candidate_b_id: str = Field(min_length=1)
    placement_delta_m: float = Field(gt=0)
    predicted_delta: float
    measured_delta: float
    observed_sensitivity_per_m: float = Field(ge=0)
    model_error_per_m: float = Field(ge=0)
    max_observed_sensitivity_per_m: float = Field(gt=0)
    max_model_error_per_m: float = Field(gt=0)
    gate: Literal['pass', 'fail']

    @model_validator(mode='after')
    def valid_sensitivity(self) -> 'CadSensitivityCheck':
        values = (
            self.predicted_delta,
            self.measured_delta,
            self.observed_sensitivity_per_m,
            self.model_error_per_m,
        )
        if not all(isfinite(float(value)) for value in values):
            raise ValueError('sensitivity values must be finite')
        expected_observed = max(abs(self.predicted_delta), abs(self.measured_delta)) / self.placement_delta_m
        expected_error = abs(self.measured_delta - self.predicted_delta) / self.placement_delta_m
        if abs(self.observed_sensitivity_per_m - expected_observed) > 1e-12:
            raise ValueError('observed sensitivity mismatch')
        if abs(self.model_error_per_m - expected_error) > 1e-12:
            raise ValueError('sensitivity model error mismatch')
        expected_gate = (
            'pass'
            if (
                self.observed_sensitivity_per_m <= self.max_observed_sensitivity_per_m
                and self.model_error_per_m <= self.max_model_error_per_m
            )
            else 'fail'
        )
        if self.gate != expected_gate:
            raise ValueError('sensitivity gate mismatch')
        return self


class CadRepeatabilityPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    measurement_a_id: str = Field(min_length=1)
    measurement_b_id: str = Field(min_length=1)
    rms_difference_db: float = Field(ge=0)
    shape_rms_db: float | None = Field(default=None, ge=0)


class CadRepeatabilityCheck(BaseModel):
    """Noise/repeatability floor from repeated measurements of one exact SceneRevision."""

    model_config = ConfigDict(frozen=True)

    scene_revision_id: str = Field(min_length=1)
    measurement_ids: tuple[str, ...] = Field(min_length=2)
    requested_band_hz: tuple[float, float]
    reference_band_hz: tuple[float, float] | None = None
    pairs: tuple[CadRepeatabilityPair, ...] = Field(min_length=1)
    rms_floor_db: float = Field(ge=0)
    shape_floor_db: float | None = Field(default=None, ge=0)
    gate: Literal['pass'] = 'pass'

    @model_validator(mode='after')
    def valid_repeatability(self) -> 'CadRepeatabilityCheck':
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('repeatability measurement ids must be unique')
        expected_pairs = len(self.measurement_ids) * (len(self.measurement_ids) - 1) // 2
        if len(self.pairs) != expected_pairs:
            raise ValueError('repeatability pair count mismatch')
        low_hz, high_hz = self.requested_band_hz
        if low_hz <= 0 or high_hz <= low_hz:
            raise ValueError('repeatability frequency band is invalid')
        if self.reference_band_hz is not None:
            ref_low, ref_high = self.reference_band_hz
            if ref_low <= 0 or ref_high <= ref_low:
                raise ValueError('repeatability reference band is invalid')
        expected_rms = sqrt(
            sum(pair.rms_difference_db ** 2 for pair in self.pairs) / len(self.pairs)
        )
        if abs(self.rms_floor_db - expected_rms) > 1e-12:
            raise ValueError('repeatability RMS floor mismatch')
        shapes = [pair.shape_rms_db for pair in self.pairs if pair.shape_rms_db is not None]
        if shapes:
            expected_shape = sqrt(sum(value ** 2 for value in shapes) / len(shapes))
            if self.shape_floor_db is None or abs(self.shape_floor_db - expected_shape) > 1e-12:
                raise ValueError('repeatability shape floor mismatch')
        elif self.shape_floor_db is not None:
            raise ValueError('repeatability shape floor requires shape data')
        return self


class CadCandidateSeparationCheck(BaseModel):
    """Whether a measured candidate difference is larger than repeatability noise."""

    model_config = ConfigDict(frozen=True)

    candidate_a_id: str = Field(min_length=1)
    candidate_b_id: str = Field(min_length=1)
    measurement_a_id: str = Field(min_length=1)
    measurement_b_id: str = Field(min_length=1)
    requested_band_hz: tuple[float, float]
    response_difference_rms_db: float = Field(ge=0)
    repeatability_floor_db: float = Field(ge=0)
    min_repeatability_multiple: float = Field(gt=0)
    separation_ratio: float | None = Field(default=None, ge=0)
    gate: Literal['pass', 'fail']

    @model_validator(mode='after')
    def valid_separation(self) -> 'CadCandidateSeparationCheck':
        if self.candidate_a_id == self.candidate_b_id:
            raise ValueError('candidate separation requires two candidates')
        low_hz, high_hz = self.requested_band_hz
        if low_hz <= 0 or high_hz <= low_hz:
            raise ValueError('candidate separation frequency band is invalid')
        if self.repeatability_floor_db == 0:
            expected_ratio = None
            expected_gate = 'pass' if self.response_difference_rms_db > 0 else 'fail'
        else:
            expected_ratio = self.response_difference_rms_db / self.repeatability_floor_db
            expected_gate = (
                'pass'
                if expected_ratio > self.min_repeatability_multiple
                else 'fail'
            )
        if expected_ratio is None:
            if self.separation_ratio is not None:
                raise ValueError('zero repeatability floor requires separation_ratio=None')
        elif self.separation_ratio is None or abs(self.separation_ratio - expected_ratio) > 1e-12:
            raise ValueError('candidate separation ratio mismatch')
        if self.gate != expected_gate:
            raise ValueError('candidate separation gate mismatch')
        return self


class CadApplicabilityEvidenceRef(BaseModel):
    """One exact source authority an applicability decision binds to."""

    model_config = ConfigDict(frozen=True)

    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class CadApplicabilityCheck(BaseModel):
    """One O60 applicability decision bound to exact evaluator/source authority.

    ``passed`` together with a free-form note is never authoritative: the check
    carries the registered evaluator identity and version, the canonical
    evaluated ``subject`` scope, and typed ``evidence_refs`` sealed by
    ``decision_sha256``. The validation repository re-derives every persisted
    check through its registered evaluator before saving and on authoritative
    reads, so a claim that cannot be reproduced from the referenced evidence
    fails closed.
    """

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    passed: bool
    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    subject_json: str = Field(min_length=2)
    subject_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_refs: tuple[CadApplicabilityEvidenceRef, ...] = Field(min_length=1)
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    detail: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_check(self) -> 'CadApplicabilityCheck':
        try:
            subject = json.loads(self.subject_json)
        except json.JSONDecodeError as exc:
            raise ValueError('applicability check subject must be JSON') from exc
        if not isinstance(subject, dict):
            raise ValueError('applicability check subject must be a JSON object')
        if _canonical_json(subject) != self.subject_json:
            raise ValueError('applicability check subject must be canonical JSON')
        if _canonical_sha256(subject) != self.subject_sha256:
            raise ValueError('applicability check subject hash mismatch')
        ref_keys = [
            (ref.source_kind, ref.source_id) for ref in self.evidence_refs
        ]
        if len(ref_keys) != len(set(ref_keys)):
            raise ValueError('applicability evidence refs must be unique')
        if _canonical_sha256(self.decision_payload()) != self.decision_sha256:
            raise ValueError('applicability decision hash mismatch')
        return self

    def subject(self) -> dict[str, Any]:
        """Parsed canonical subject scope this decision was evaluated over."""
        return json.loads(self.subject_json)

    def decision_payload(self) -> dict[str, Any]:
        """Canonical decision content sealed by ``decision_sha256``.

        The optional human ``detail`` note is intentionally not part of the
        authoritative decision payload.
        """
        return {
            'code': self.code,
            'passed': self.passed,
            'evaluator_id': self.evaluator_id,
            'evaluator_version': self.evaluator_version,
            'subject': json.loads(self.subject_json),
            'evidence_refs': [
                ref.model_dump(mode='json') for ref in self.evidence_refs
            ],
        }


def build_applicability_check(
    *,
    code: str,
    passed: bool,
    evaluator_id: str,
    evaluator_version: str,
    subject: Mapping[str, Any],
    evidence_refs: Sequence[CadApplicabilityEvidenceRef | Mapping[str, Any]],
    detail: str | None = None,
) -> CadApplicabilityCheck:
    """Assemble an evidence-bound applicability check with canonical hashes."""
    refs = tuple(
        sorted(
            (
                ref
                if isinstance(ref, CadApplicabilityEvidenceRef)
                else CadApplicabilityEvidenceRef.model_validate(ref)
                for ref in evidence_refs
            ),
            key=lambda ref: (ref.source_kind, ref.source_id, ref.source_sha256),
        )
    )
    provisional = CadApplicabilityCheck.model_construct(
        code=code,
        passed=bool(passed),
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        subject_json=_canonical_json(subject),
        subject_sha256=_canonical_sha256(subject),
        evidence_refs=refs,
        decision_sha256='0' * 64,
        detail=detail,
    )
    return CadApplicabilityCheck(
        **provisional.model_dump(exclude={'decision_sha256'}),
        decision_sha256=_canonical_sha256(provisional.decision_payload()),
    )


def build_trend_checks(
    samples: Sequence[CadObjectiveValidationSample],
    *,
    tie_tolerance_by_objective: Mapping[str, float] | None = None,
    min_comparable_pairs: int = 1,
    min_agreement_ratio: float = 0.75,
) -> tuple[CadTrendCheck, ...]:
    if min_comparable_pairs < 1:
        raise ValueError('min_comparable_pairs must be positive')
    if not 0 <= min_agreement_ratio <= 1:
        raise ValueError('min_agreement_ratio must be between 0 and 1')
    tolerance_map = dict(tie_tolerance_by_objective or {})

    grouped: dict[str, list[CadObjectiveValidationSample]] = {}
    order: list[str] = []
    for sample in samples:
        if sample.split != 'holdout':
            continue
        if sample.objective_id not in grouped:
            grouped[sample.objective_id] = []
            order.append(sample.objective_id)
        grouped[sample.objective_id].append(sample)

    checks: list[CadTrendCheck] = []
    for objective_id in order:
        group = grouped[objective_id]
        units = {sample.unit for sample in group}
        if len(units) != 1:
            raise ValueError(f'objective trend units mismatch: {objective_id}')
        candidate_ids = [sample.candidate_id for sample in group]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError(f'duplicate holdout objective sample: {objective_id}')
        if len(group) < 2:
            continue
        tolerance = float(tolerance_map.get(objective_id, 0.0))
        if tolerance < 0 or not isfinite(tolerance):
            raise ValueError('trend tie tolerance must be finite and non-negative')
        concordant = discordant = ambiguous = 0
        for left, right in combinations(group, 2):
            predicted_delta = left.predicted_value - right.predicted_value
            measured_delta = left.measured_value - right.measured_value
            if abs(predicted_delta) <= tolerance or abs(measured_delta) <= tolerance:
                ambiguous += 1
            elif (predicted_delta < 0) == (measured_delta < 0):
                concordant += 1
            else:
                discordant += 1
        comparable = concordant + discordant
        ratio = None if comparable == 0 else concordant / comparable
        gate: ValidationGate
        if comparable < min_comparable_pairs:
            gate = 'insufficient'
        elif ratio is not None and ratio >= min_agreement_ratio:
            gate = 'pass'
        else:
            gate = 'fail'
        checks.append(CadTrendCheck(
            objective_id=objective_id,
            unit=next(iter(units)),
            holdout_candidate_ids=tuple(candidate_ids),
            tie_tolerance=tolerance,
            min_comparable_pairs=min_comparable_pairs,
            min_agreement_ratio=min_agreement_ratio,
            comparable_pairs=comparable,
            concordant_pairs=concordant,
            discordant_pairs=discordant,
            ambiguous_pairs=ambiguous,
            agreement_ratio=ratio,
            gate=gate,
        ))
    return tuple(checks)


def build_sensitivity_check(
    *,
    objective_id: str,
    unit: str,
    candidate_a_id: str,
    candidate_b_id: str,
    placement_delta_m: float,
    predicted_a: float,
    predicted_b: float,
    measured_a: float,
    measured_b: float,
    max_observed_sensitivity_per_m: float,
    max_model_error_per_m: float,
) -> CadSensitivityCheck:
    if candidate_a_id == candidate_b_id:
        raise ValueError('sensitivity requires two candidates')
    placement_delta_m = float(placement_delta_m)
    if placement_delta_m <= 0 or not isfinite(placement_delta_m):
        raise ValueError('sensitivity placement delta must be finite and positive')
    predicted_delta = float(predicted_b) - float(predicted_a)
    measured_delta = float(measured_b) - float(measured_a)
    observed = max(abs(predicted_delta), abs(measured_delta)) / placement_delta_m
    error = abs(measured_delta - predicted_delta) / placement_delta_m
    gate = (
        'pass'
        if observed <= max_observed_sensitivity_per_m and error <= max_model_error_per_m
        else 'fail'
    )
    return CadSensitivityCheck(
        objective_id=objective_id,
        unit=unit,
        candidate_a_id=candidate_a_id,
        candidate_b_id=candidate_b_id,
        placement_delta_m=placement_delta_m,
        predicted_delta=predicted_delta,
        measured_delta=measured_delta,
        observed_sensitivity_per_m=observed,
        model_error_per_m=error,
        max_observed_sensitivity_per_m=max_observed_sensitivity_per_m,
        max_model_error_per_m=max_model_error_per_m,
        gate=gate,
    )


def build_repeatability_check(
    *,
    scene_revision_id: str,
    measurements: Sequence[tuple[str, FrequencyResponse]],
    low_hz: float,
    high_hz: float,
    reference_band_hz: tuple[float, float] | None = None,
) -> CadRepeatabilityCheck:
    if len(measurements) < 2:
        raise ValueError('repeatability requires at least two measurements')
    ids = [measurement_id for measurement_id, _response in measurements]
    if len(ids) != len(set(ids)):
        raise ValueError('repeatability measurement ids must be unique')
    pairs: list[CadRepeatabilityPair] = []
    for (left_id, left), (right_id, right) in combinations(measurements, 2):
        result = compare_frequency_responses(
            left,
            right,
            low_hz,
            high_hz,
            reference_band_hz=reference_band_hz,
        )
        if result.rms_difference_db is None:
            raise ValueError('repeatability pair has insufficient overlapping response data')
        pairs.append(CadRepeatabilityPair(
            measurement_a_id=left_id,
            measurement_b_id=right_id,
            rms_difference_db=result.rms_difference_db,
            shape_rms_db=result.shape_rms_db,
        ))
    rms_floor = sqrt(sum(pair.rms_difference_db ** 2 for pair in pairs) / len(pairs))
    shapes = [pair.shape_rms_db for pair in pairs if pair.shape_rms_db is not None]
    shape_floor = None if not shapes else sqrt(sum(value ** 2 for value in shapes) / len(shapes))
    return CadRepeatabilityCheck(
        scene_revision_id=scene_revision_id,
        measurement_ids=tuple(ids),
        requested_band_hz=(float(low_hz), float(high_hz)),
        reference_band_hz=reference_band_hz,
        pairs=tuple(pairs),
        rms_floor_db=rms_floor,
        shape_floor_db=shape_floor,
    )


def build_candidate_separation_check(
    *,
    candidate_a_id: str,
    candidate_b_id: str,
    measurement_a_id: str,
    measurement_b_id: str,
    response_a: FrequencyResponse,
    response_b: FrequencyResponse,
    low_hz: float,
    high_hz: float,
    repeatability_floor_db: float,
    min_repeatability_multiple: float = 1.0,
) -> CadCandidateSeparationCheck:
    result = compare_frequency_responses(response_a, response_b, low_hz, high_hz)
    if result.rms_difference_db is None:
        raise ValueError('candidate separation has insufficient overlapping response data')
    floor = float(repeatability_floor_db)
    if floor < 0 or not isfinite(floor):
        raise ValueError('repeatability floor must be finite and non-negative')
    multiple = float(min_repeatability_multiple)
    if multiple <= 0 or not isfinite(multiple):
        raise ValueError('repeatability multiple must be finite and positive')
    if floor == 0:
        ratio = None
        gate = 'pass' if result.rms_difference_db > 0 else 'fail'
    else:
        ratio = result.rms_difference_db / floor
        gate = 'pass' if ratio > multiple else 'fail'
    return CadCandidateSeparationCheck(
        candidate_a_id=candidate_a_id,
        candidate_b_id=candidate_b_id,
        measurement_a_id=measurement_a_id,
        measurement_b_id=measurement_b_id,
        requested_band_hz=(float(low_hz), float(high_hz)),
        response_difference_rms_db=result.rms_difference_db,
        repeatability_floor_db=floor,
        min_repeatability_multiple=multiple,
        separation_ratio=ratio,
        gate=gate,
    )
