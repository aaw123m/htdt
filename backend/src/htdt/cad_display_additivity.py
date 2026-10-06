"""Display additivity / RGB-separation qualification authority (#686).

A display is not guaranteed to be colorimetrically additive: measured
primary components need not sum to the measured combined colour, and RGB
channels may be cross-coupled.  A sparse matrix / quick-profile
calibration model is mathematically convenient yet structurally wrong on
a non-additive display — so model-family eligibility is *gated by
measured channel independence*, never inferred from panel class or
marketing category.

Distinct capabilities (issue §3) — passing one never upgrades the rest:

- ``PEAK_ADDITIVITY_CHECK`` — component XYZ vs combined XYZ at declared
  stimulus levels;
- ``RAMP_RGB_SEPARATION`` — per-channel ramps vs expected/measured
  combined response across levels (cross-coupling evidence);
- ``SPARSE_IN_GAMUT_CHECK`` — in-gamut probe of the sparse model;
- ``VOLUMETRIC_CHARACTERISATION`` — dense colour-volume evidence;
- ``HOLDOUT_VERIFICATION`` — independent patches confirm the model.

Contract properties:

- additivity is an observed property of a sealed display state (#625
  ``DirectViewDisplayState`` pin), not a product label;
- component measurements and the measured combined colour are stored —
  never only a percent-error scalar;
- cross-coupling is its own residual — never collapsed into generic ΔE;
- reasonable RGB separation does not rule out volumetric non-linearity —
  eligibility verdicts keep the capabilities separate;
- results do not transfer across picture modes or HDR/SDR without
  evidence;
- a characterisation plan derives required density from the evidence —
  sparse-pass never upgrades the whole volume to linear.

Literature basis (issue §research): Portrait Displays additivity article
(primary tristimulus energy summation; sparse Matrix/Lightning LUT
poorly suited to non-additive displays); ColourSpace RGB Separation +
volumetric characterisation guidance (quick profiling only for
well-behaved displays; RGB separation alone does not rule out
volumetric non-linearity). Vendor-neutral conclusion: test the model
assumptions before selecting a sparse calibration model.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


ADDITIVITY_SCHEMA_VERSION = 'display-additivity-1'
ADDITIVITY_EVALUATION_VERSION = 'display-additivity-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _xyz(values: tuple[float, float, float]) -> tuple[float, float, float]:
    if any(not isfinite(v) for v in values):
        raise ValueError('XYZ components must be finite')
    return values


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

CharacterizationCapability = Literal[
    'peak_additivity_check',
    'ramp_rgb_separation',
    'sparse_in_gamut_check',
    'volumetric_characterisation',
    'holdout_verification',
]
"""The five distinct checks — a pass in one never upgrades the others."""

CalibrationModelFamily = Literal[
    'matrix_1d',
    'sparse_lightning_lut',
    'shaper_3d_lut',
    'fixed_grid_volumetric_lut',
    'direct_device_write',
    'vendor_specific',
    'unknown',
]
"""Calibration model families whose eligibility is gated by evidence."""

EligibilityVerdict = Literal[
    'eligible',
    'eligible_with_limitations',
    'insufficient_evidence',
    'inappropriate',
]

AdditivityReason = Literal[
    'ADDITIVE_WITHIN_TOLERANCE',
    'NON_ADDITIVE_MEASURED',
    'CHANNEL_COUPLING_MEASURED',
    'CHANNELS_INDEPENDENT',
    'VOLUMETRIC_NONLINEARITY_MEASURED',
    'VOLUMETRIC_LINEAR',
    'HOLDOUT_CONFIRMED',
    'HOLDOUT_FAILED',
    'EVIDENCE_MISSING',
    'DISPLAY_STATE_MISMATCH',
    'LEVELS_UNDECLARED',
    'SPARSE_MODEL_REQUIRES_INDEPENDENCE',
    'VOLUMETRIC_REQUIRES_DENSE_MEASUREMENT',
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class AdditivityPoint(BaseModel):
    """One component-vs-combined observation at a stimulus level (§1)."""

    model_config = ConfigDict(frozen=True)

    level_percent: float = Field(ge=0.0, le=100.0)
    component_xyz: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    """XYZ for R, G, B measured separately."""
    combined_xyz: tuple[float, float, float]
    """Measured XYZ of the combined (e.g. white) stimulus."""

    @model_validator(mode='after')
    def _check(self) -> 'AdditivityPoint':
        for comp in self.component_xyz:
            _xyz(comp)
        _xyz(self.combined_xyz)
        return self


class DisplayAdditivityObservation(BaseModel):
    """Component vs combined measurements on one display state."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(pattern=r'^daobs-[0-9a-f]{24}$')
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    """Pins the #625 DirectViewDisplayState — additivity is observed on
    one exact state and never transfers across picture modes."""
    stimulus_refs: tuple[AuthorityRef, ...]
    points: tuple[AdditivityPoint, ...]
    meter_refs: tuple[AuthorityRef, ...] = ()
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'DisplayAdditivityObservation':
        _require_refs(self.display_state_ref)
        if not self.points:
            raise ValueError('additivity needs at least one level')
        if not self.stimulus_refs:
            raise ValueError('additivity must pin its stimulus set')
        for ref in self.stimulus_refs + self.meter_refs:
            _require_refs(ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'DisplayAdditivityObservation':
        return _seal(
            cls, kwargs, 'observation_id', 'observation_sha256', 'daobs'
        )


class ChannelRampPoint(BaseModel):
    """One ramp step: nominal drive vs measured response (§2)."""

    model_config = ConfigDict(frozen=True)

    drive_percent: float = Field(ge=0.0, le=100.0)
    channel_xyz: tuple[float, float, float]
    """Measured XYZ with only this channel driven."""
    combined_expected_xyz: tuple[float, float, float] | None = None
    combined_measured_xyz: tuple[float, float, float] | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ChannelRampPoint':
        _xyz(self.channel_xyz)
        if self.combined_expected_xyz is not None:
            _xyz(self.combined_expected_xyz)
        if self.combined_measured_xyz is not None:
            _xyz(self.combined_measured_xyz)
        return self


class RGBSeparationAssessment(BaseModel):
    """Per-channel ramp evidence — channel independence (§2)."""

    model_config = ConfigDict(frozen=True)

    assessment_id: str = Field(pattern=r'^rgbs-[0-9a-f]{24}$')
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    stimulus_refs: tuple[AuthorityRef, ...]
    ramps: dict[str, tuple[ChannelRampPoint, ...]]
    """Keys 'r'/'g'/'b'/'neutral' → ramp points."""
    max_coupling_residual_delta_e: float | None = Field(
        default=None, ge=0.0
    )
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'assessment_id', 'assessment_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'RGBSeparationAssessment':
        _require_refs(self.display_state_ref)
        required = {'r', 'g', 'b'}
        if not required.issubset(self.ramps.keys()):
            raise ValueError(
                'separation assessment needs r, g and b ramps'
            )
        for ref in self.stimulus_refs:
            _require_refs(ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'RGBSeparationAssessment':
        return _seal(
            cls, kwargs, 'assessment_id', 'assessment_sha256', 'rgbs'
        )


class VolumetricCharacterisation(BaseModel):
    """Dense colour-volume measurement evidence (§3)."""

    model_config = ConfigDict(frozen=True)

    characterisation_id: str = Field(pattern=r'^dvol-[0-9a-f]{24}$')
    characterisation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    patch_set_ref: AuthorityRef
    grid_size: int = Field(ge=2)
    measured_count: int = Field(ge=1)
    max_error_delta_e: float | None = Field(default=None, ge=0.0)
    measured_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'characterisation_id', 'characterisation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'VolumetricCharacterisation':
        _require_refs(self.display_state_ref, self.patch_set_ref)
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.measured_count < self.grid_size ** 3:
            raise ValueError(
                'volumetric characterisation must cover the declared '
                'grid — a sparse pass is not a volume'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'VolumetricCharacterisation':
        return _seal(
            cls, kwargs,
            'characterisation_id', 'characterisation_sha256', 'dvol',
        )


class HoldoutVerification(BaseModel):
    """Independent-patch verification of a selected model (§3)."""

    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(pattern=r'^dhold-[0-9a-f]{24}$')
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    model_family: CalibrationModelFamily
    holdout_patch_set_ref: AuthorityRef
    residuals_delta_e: tuple[float, ...]
    pass_threshold_delta_e: float = Field(gt=0)
    verified_at_utc: str
    passed: bool

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'verification_id', 'verification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'HoldoutVerification':
        _require_refs(self.display_state_ref, self.holdout_patch_set_ref)
        _require_iso8601(self.verified_at_utc, 'verified_at_utc')
        if not self.residuals_delta_e:
            raise ValueError('holdout verification needs residuals')
        if any(not isfinite(v) or v < 0 for v in self.residuals_delta_e):
            raise ValueError('residuals must be finite non-negative')
        if self.passed and max(self.residuals_delta_e) > self.pass_threshold_delta_e:
            raise ValueError(
                'a passing holdout cannot exceed its threshold'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'HoldoutVerification':
        return _seal(
            cls, kwargs, 'verification_id', 'verification_sha256', 'dhold'
        )


class CalibrationModelEligibility(BaseModel):
    """Sealed per-model-family verdict (issue goal)."""

    model_config = ConfigDict(frozen=True)

    eligibility_id: str = Field(pattern=r'^cmelig-[0-9a-f]{24}$')
    eligibility_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    model_family: CalibrationModelFamily
    capability_evidence: dict[str, AuthorityRef]
    """capability name → evidence record pin."""
    verdict: EligibilityVerdict
    reasons: tuple[AdditivityReason, ...]
    evaluated_at_utc: str
    evaluation_version: str = ADDITIVITY_EVALUATION_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'eligibility_id', 'eligibility_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'CalibrationModelEligibility':
        _require_refs(self.display_state_ref)
        for ref in self.capability_evidence.values():
            _require_refs(ref)
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'CalibrationModelEligibility':
        return _seal(
            cls, kwargs, 'eligibility_id', 'eligibility_sha256', 'cmelig'
        )


class CharacterisationPlan(BaseModel):
    """Evidence-derived minimum characterisation density (§4)."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(pattern=r'^charplan-[0-9a-f]{24}$')
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    display_state_ref: AuthorityRef
    required_capability: CharacterizationCapability
    minimum_grid_size: int = Field(ge=2)
    requires_holdout: bool = True
    basis: str = Field(min_length=1)
    planned_at_utc: str

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'plan_id', 'plan_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'CharacterisationPlan':
        _require_refs(self.display_state_ref)
        _require_iso8601(self.planned_at_utc, 'planned_at_utc')
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'CharacterisationPlan':
        return _seal(
            cls, kwargs, 'plan_id', 'plan_sha256', 'charplan'
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _additivity_residual(point: AdditivityPoint) -> float:
    """Simple sum-mismatch magnitude for the sealed residual."""
    summed = tuple(
        sum(comp[i] for comp in point.component_xyz) for i in range(3)
    )
    combined = point.combined_xyz
    scale = max(sum(combined), 1e-9)
    return (
        sum((s - c) ** 2 for s, c in zip(summed, combined)) ** 0.5
    ) / scale


def evaluate_additivity(
    observation: DisplayAdditivityObservation,
    *,
    tolerance: float,
) -> tuple[bool, float]:
    """Additive within tolerance? Returns (is_additive, worst_residual)."""
    residuals = [_additivity_residual(p) for p in observation.points]
    worst = max(residuals)
    return worst <= tolerance, worst


def evaluate_model_eligibility(
    *,
    document_id: str,
    display_state_ref: AuthorityRef,
    model_family: CalibrationModelFamily,
    additivity: DisplayAdditivityObservation | None,
    separation: RGBSeparationAssessment | None,
    volumetric: VolumetricCharacterisation | None,
    holdout: HoldoutVerification | None,
    additivity_tolerance: float,
    coupling_tolerance_delta_e: float,
    evaluated_at_utc: str | None = None,
) -> CalibrationModelEligibility:
    """Gate a calibration model family on measured independence.

    Sparse/matrix families require both additivity and ramp separation
    evidence; volumetric LUTs require dense characterisation; no family
    claims eligibility without holdout unless explicitly limited.
    """
    _require_refs(display_state_ref)
    reasons: list[AdditivityReason] = []
    evidence: dict[str, AuthorityRef] = {}

    additive_ok: bool | None = None
    if additivity is not None:
        additive_ok, _worst = evaluate_additivity(
            additivity, tolerance=additivity_tolerance
        )
        evidence['peak_additivity_check'] = AuthorityRef(
            kind='display_additivity_observation',
            ref_id=additivity.observation_id,
            ref_sha256=additivity.observation_sha256,
        )
        reasons.append(
            'ADDITIVE_WITHIN_TOLERANCE' if additive_ok
            else 'NON_ADDITIVE_MEASURED'
        )
    separation_ok: bool | None = None
    if separation is not None:
        residual = separation.max_coupling_residual_delta_e
        separation_ok = (
            residual is not None and residual <= coupling_tolerance_delta_e
        )
        evidence['ramp_rgb_separation'] = AuthorityRef(
            kind='rgb_separation_assessment',
            ref_id=separation.assessment_id,
            ref_sha256=separation.assessment_sha256,
        )
        reasons.append(
            'CHANNELS_INDEPENDENT' if separation_ok
            else 'CHANNEL_COUPLING_MEASURED'
        )
    volumetric_ok: bool | None = None
    if volumetric is not None:
        volumetric_ok = (
            volumetric.max_error_delta_e is not None
            and volumetric.max_error_delta_e
            <= coupling_tolerance_delta_e
        )
        evidence['volumetric_characterisation'] = AuthorityRef(
            kind='volumetric_characterisation',
            ref_id=volumetric.characterisation_id,
            ref_sha256=volumetric.characterisation_sha256,
        )
        reasons.append(
            'VOLUMETRIC_LINEAR' if volumetric_ok
            else 'VOLUMETRIC_NONLINEARITY_MEASURED'
        )
    holdout_ok: bool | None = None
    if holdout is not None:
        holdout_ok = holdout.passed
        evidence['holdout_verification'] = AuthorityRef(
            kind='holdout_verification',
            ref_id=holdout.verification_id,
            ref_sha256=holdout.verification_sha256,
        )
        reasons.append(
            'HOLDOUT_CONFIRMED' if holdout_ok else 'HOLDOUT_FAILED'
        )

    sparse_families = {
        'matrix_1d', 'sparse_lightning_lut', 'shaper_3d_lut',
    }
    verdict: EligibilityVerdict
    if model_family in sparse_families:
        reasons.append('SPARSE_MODEL_REQUIRES_INDEPENDENCE')
        if additive_ok is None or separation_ok is None:
            verdict = 'insufficient_evidence'
            reasons.append('EVIDENCE_MISSING')
        elif not additive_ok or not separation_ok:
            verdict = 'inappropriate'
        elif holdout_ok is False:
            verdict = 'inappropriate'
        elif holdout_ok is None or volumetric_ok is None:
            verdict = 'eligible_with_limitations'
            reasons.append('EVIDENCE_MISSING')
        else:
            verdict = 'eligible'
    elif model_family == 'fixed_grid_volumetric_lut':
        reasons.append('VOLUMETRIC_REQUIRES_DENSE_MEASUREMENT')
        if volumetric_ok is None:
            verdict = 'insufficient_evidence'
            reasons.append('EVIDENCE_MISSING')
        elif not volumetric_ok or holdout_ok is False:
            verdict = 'inappropriate'
        elif holdout_ok is None:
            verdict = 'eligible_with_limitations'
            reasons.append('EVIDENCE_MISSING')
        else:
            verdict = 'eligible'
    else:
        if not evidence:
            verdict = 'insufficient_evidence'
            reasons.append('EVIDENCE_MISSING')
        elif holdout_ok is False:
            verdict = 'inappropriate'
        else:
            verdict = 'eligible_with_limitations'
    return CalibrationModelEligibility.create(
        document_id=document_id,
        display_state_ref=display_state_ref,
        model_family=model_family,
        capability_evidence=evidence,
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
    )


# ---------------------------------------------------------------------------
# JA labels
# ---------------------------------------------------------------------------

MODEL_FAMILY_LABELS: dict[str, str] = {
    'matrix_1d': '1D+マトリクス較正',
    'sparse_lightning_lut': '疎LUT較正',
    'shaper_3d_lut': 'シェーパー+3D LUT',
    'fixed_grid_volumetric_lut': '固定格子ボリュームLUT',
    'direct_device_write': 'デバイス直接書込み',
    'vendor_specific': 'ベンダー固有',
    'unknown': '不明',
}

VERDICT_LABELS: dict[str, str] = {
    'eligible': '較正モデル適格',
    'eligible_with_limitations': '較正モデル条件付き適格',
    'insufficient_evidence': '較正モデル適格性: 証拠不足',
    'inappropriate': '較正モデル不適格',
}

CAPABILITY_LABELS: dict[str, str] = {
    'peak_additivity_check': 'ピーク加法性チェック',
    'ramp_rgb_separation': 'RGB分離ランプ',
    'sparse_in_gamut_check': '疎ガマット内チェック',
    'volumetric_characterisation': 'ボリューム特性評価',
    'holdout_verification': 'ホールドアウト検証',
}

REASON_LABELS: dict[str, str] = {
    'ADDITIVE_WITHIN_TOLERANCE': '加法性は許容内です',
    'NON_ADDITIVE_MEASURED': '非加法性が実測されました',
    'CHANNEL_COUPLING_MEASURED': 'チャネル間結合が実測されました',
    'CHANNELS_INDEPENDENT': 'チャネルは独立です',
    'VOLUMETRIC_NONLINEARITY_MEASURED': 'ボリューム非線形性が実測されました',
    'VOLUMETRIC_LINEAR': 'ボリュームは線形です',
    'HOLDOUT_CONFIRMED': 'ホールドアウト検証に合格しました',
    'HOLDOUT_FAILED': 'ホールドアウト検証に失敗しました',
    'EVIDENCE_MISSING': '証拠が不足しています',
    'DISPLAY_STATE_MISMATCH': 'ディスプレイ状態が一致しません',
    'LEVELS_UNDECLARED': 'レベルが未宣言です',
    'SPARSE_MODEL_REQUIRES_INDEPENDENCE': '疎モデルには独立性の実測が必要です',
    'VOLUMETRIC_REQUIRES_DENSE_MEASUREMENT': 'ボリュームモデルには密な測定が必要です',
}
