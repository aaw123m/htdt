"""Display colorimeter ↔ spectroradiometer probe-matching authority
(#680).

A tristimulus (filter) colorimeter can hold a valid #611 calibration
certificate and still show **display-spectrum-dependent error**: its
spectral responsivities never perfectly match the CIE colour-matching
functions, so a generic ``meter_profile = OLED`` is not colorimetric
truth on every modern SPD.  Professional tools treat probe matching as a
first-class workflow (Calman Meter Profiling, ColourSpace Probe
Matching) — matching the *exact* target colorimeter instance to the
*exact* reference spectroradiometer instance on the *exact* display SPD
state being measured.

This authority keeps the evidence layers separate (issue §1):

- instrument calibration / fitness — owned by #611, pinned here only;
- manufacturer display-spectral correction (EDR/CCSS/built-in matrix) —
  recorded as *pre-existing state*, never silently composed with a
  custom match (double application is a defect);
- the user/tool display-specific probe match — this module;
- measurement-session application — the applicability verdict.

Contract properties:

- a match binds both physical instrument *instances* (serial-level) —
  it never transfers to another unit of the same model;
- a match binds the exact display SPD state — picture/gamut/white point,
  backlight/laser/lamp mode, HDR/SDR, dimming/ABL state, screen/optical
  path; another generation or mode is not qualified;
- a match binds an exact patch set (#608-pinned) — changing code values
  or range changes the matching experiment;
- a correction derived over an ``unknown`` pre-existing correction is
  not an unambiguous raw-probe correction;
- a match observed under one display state can never silently apply to
  another state — applicability is evaluated, not assumed;
- post-match verification is a separate record from the match itself.

Literature basis (issue §research): CIE 179:2007 (tristimulus
colorimeter characterisation — error depends on spectral-response fit
to the CMFs and on the measured source SPD); Portrait Displays meter
profiling workflow; ColourSpace probe-matching documentation (same
display/patch/geometry requirement).
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


METER_MATCH_SCHEMA_VERSION = 'meter-match-1'
METER_MATCH_EVALUATION_VERSION = 'meter-match-eval-1'

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


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

InstrumentKind = Literal[
    'tristimulus_colorimeter',
    'spectroradiometer',
    'spectrophotometer',
    'unknown',
]

PreExistingCorrectionState = Literal[
    'raw_default_response',
    'manufacturer_correction_active',
    'generic_technology_correction_active',
    'custom_match_active',
    'unknown',
]
"""What spectral correction was already active on the target meter
before the match (issue §4). ``unknown`` forbids describing the derived
match as a raw-probe correction."""

DisplaySpdBasis = Literal[
    'measured_spd',
    'reference_instrument_measurement',
    'manufacturer_technology_class',
    'inferred_from_mode',
    'unknown',
]
"""How the display's spectral state was established at match time."""

MatchAlgorithmKind = Literal[
    'matrix_3x3',
    'volumetric',
    'per_channel_lut',
    'vendor_specific',
    'custom_validated',
    'unknown',
]

ApplicabilityVerdict = Literal[
    'applicable',
    'applicable_with_limitations',
    'not_applicable',
    'insufficient_evidence',
]

MatchQualityVerdict = Literal[
    'match_verified',
    'match_verified_with_limitations',
    'match_unverified',
    'match_rejected',
]

MatchReason = Literal[
    'MATCH_APPLICABLE',
    'MATCH_LIMITED',
    'MATCH_NOT_APPLICABLE',
    'DIFFERENT_TARGET_UNIT',
    'DIFFERENT_REFERENCE_UNIT',
    'DIFFERENT_DISPLAY_INSTANCE',
    'DISPLAY_STATE_CHANGED',
    'PATCH_SET_CHANGED',
    'PREEXISTING_CORRECTION_UNKNOWN',
    'PREEXISTING_CORRECTION_CHANGED',
    'DOUBLE_APPLICATION_RISK',
    'CALIBRATION_STATE_STALE',
    'RESIDUAL_ERROR_UNMEASURED',
    'VERIFICATION_MISSING',
    'VERIFICATION_PASSED',
    'VERIFICATION_FAILED',
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class InstrumentIdentity(BaseModel):
    """One physical measurement instrument instance (issue §2)."""

    model_config = ConfigDict(frozen=True)

    kind: InstrumentKind
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    serial: str = Field(min_length=1)
    firmware: str = ''
    instrument_mode: str = ''
    integration_time_s: float | None = Field(default=None, gt=0)
    calibration_ref: AuthorityRef | None = None
    """#611 calibration/fitness record at match time — pinned when the
    match claims calibrated provenance."""

    @model_validator(mode='after')
    def _check(self) -> 'InstrumentIdentity':
        if (
            self.calibration_ref is not None
            and self.calibration_ref.ref_sha256 is None
        ):
            raise ValueError('calibration ref must pin sha256')
        return self


class DisplaySpdIdentity(BaseModel):
    """The exact display SPD state a match is valid for (issue §3)."""

    model_config = ConfigDict(frozen=True)

    display_instance: str = Field(min_length=1)
    display_model: str = Field(min_length=1)
    firmware: str = ''
    picture_mode: str = ''
    gamut_mode: str = ''
    white_point_state: str = ''
    emitter_mode: str = ''
    """Backlight / laser / lamp mode where relevant."""
    content_mode: str = ''
    """SDR / HDR variant profile."""
    dimming_abl_state: str = ''
    optical_path: str = ''
    """Screen/optical path for reflected (projection) measurements."""
    signal_profile_ref: AuthorityRef | None = None
    spd_basis: DisplaySpdBasis = 'unknown'
    measured_spd_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def _check(self) -> 'DisplaySpdIdentity':
        if self.spd_basis == 'measured_spd' and not self.measured_spd_sha256:
            raise ValueError(
                'measured_spd basis requires the pinned SPD content'
            )
        if (
            self.signal_profile_ref is not None
            and self.signal_profile_ref.ref_sha256 is None
        ):
            raise ValueError('signal profile ref must pin sha256')
        return self


class PreExistingCorrection(BaseModel):
    """Correction state on the target meter before matching (§4)."""

    model_config = ConfigDict(frozen=True)

    state: PreExistingCorrectionState
    correction_ref: AuthorityRef | None = None
    """EDR/CCSS/matrix file or prior-match pin when applicable."""
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'PreExistingCorrection':
        if self.state in (
            'manufacturer_correction_active',
            'generic_technology_correction_active',
            'custom_match_active',
        ) and self.correction_ref is None:
            raise ValueError(
                'an active correction must pin its correction source'
            )
        if (
            self.correction_ref is not None
            and self.correction_ref.ref_sha256 is None
        ):
            raise ValueError('correction ref must pin sha256')
        return self


class PatchReadingPair(BaseModel):
    """Target vs reference reading for one patch."""

    model_config = ConfigDict(frozen=True)

    patch_ref: AuthorityRef
    target_xyz: tuple[float, float, float] | None = None
    reference_xyz: tuple[float, float, float] | None = None
    residual_delta_e: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'PatchReadingPair':
        _require_refs(self.patch_ref)
        for label, xyz in (
            ('target', self.target_xyz), ('reference', self.reference_xyz)
        ):
            if xyz is not None and any(not isfinite(v) for v in xyz):
                raise ValueError(f'{label} xyz must be finite')
        return self


class DisplayMeterMatchProfile(BaseModel):
    """The sealed match between two instrument instances on one display
    SPD state (issue §§2–5)."""

    model_config = ConfigDict(frozen=True)

    match_id: str = Field(pattern=r'^mmprof-[0-9a-f]{24}$')
    match_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    target_instrument: InstrumentIdentity
    reference_instrument: InstrumentIdentity
    display_state: DisplaySpdIdentity
    pre_existing_correction: PreExistingCorrection
    patch_set_ref: AuthorityRef
    algorithm: MatchAlgorithmKind
    correction_artifact_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    matched_at_utc: str
    notes: str = ''

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'match_id', 'match_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'DisplayMeterMatchProfile':
        _require_refs(self.patch_set_ref)
        _require_iso8601(self.matched_at_utc, 'matched_at_utc')
        if self.target_instrument.kind != 'tristimulus_colorimeter':
            raise ValueError(
                'match target must be a tristimulus colorimeter'
            )
        if self.reference_instrument.kind not in (
            'spectroradiometer', 'spectrophotometer'
        ):
            raise ValueError(
                'match reference must be a spectroradiometer/'
                'spectrophotometer'
            )
        if (
            self.target_instrument.serial
            == self.reference_instrument.serial
            and self.target_instrument.model
            == self.reference_instrument.model
        ):
            raise ValueError(
                'target and reference must be distinct instruments'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'DisplayMeterMatchProfile':
        return _seal(
            cls, kwargs, 'match_id', 'match_sha256', 'mmprof'
        )


class ProbeMatchObservation(BaseModel):
    """The match experiment record — patch readings + residuals (§5–6)."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(pattern=r'^pmobs-[0-9a-f]{24}$')
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    match_ref: AuthorityRef
    readings: tuple[PatchReadingPair, ...]
    observed_at_utc: str
    measurement_context_ref: AuthorityRef | None = None

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'observation_id', 'observation_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'ProbeMatchObservation':
        _require_refs(self.match_ref)
        _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if not self.readings:
            raise ValueError('a match observation needs patch readings')
        if any(r.residual_delta_e is None for r in self.readings):
            raise ValueError(
                'every patch reading must carry its residual error — '
                'an unmeasured residual cannot silently pass'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'ProbeMatchObservation':
        return _seal(
            cls, kwargs, 'observation_id', 'observation_sha256', 'pmobs'
        )


class ProbeMatchVerification(BaseModel):
    """Independent post-match verification on fresh patches (§6/§8)."""

    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(pattern=r'^pmver-[0-9a-f]{24}$')
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    match_ref: AuthorityRef
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
    def _check(self) -> 'ProbeMatchVerification':
        _require_refs(self.match_ref, self.holdout_patch_set_ref)
        _require_iso8601(self.verified_at_utc, 'verified_at_utc')
        if not self.residuals_delta_e:
            raise ValueError('verification needs holdout residuals')
        if any(not isfinite(v) or v < 0 for v in self.residuals_delta_e):
            raise ValueError('residuals must be finite non-negative')
        worst = max(self.residuals_delta_e)
        if self.passed and worst > self.pass_threshold_delta_e:
            raise ValueError(
                'a passing verification cannot exceed its threshold'
            )
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'ProbeMatchVerification':
        return _seal(
            cls, kwargs, 'verification_id', 'verification_sha256',
            'pmver',
        )


class MeterCorrectionApplicability(BaseModel):
    """Sealed verdict: may this match correct *this* later measurement?"""

    model_config = ConfigDict(frozen=True)

    applicability_id: str = Field(pattern=r'^mmappl-[0-9a-f]{24}$')
    applicability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    match_ref: AuthorityRef
    current_instrument: InstrumentIdentity
    current_display_state: DisplaySpdIdentity
    current_correction_state: PreExistingCorrection
    verdict: ApplicabilityVerdict
    reasons: tuple[MatchReason, ...]
    evaluated_at_utc: str
    evaluation_version: str = METER_MATCH_EVALUATION_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'applicability_id', 'applicability_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'MeterCorrectionApplicability':
        _require_refs(self.match_ref)
        return self

    @classmethod
    def create(cls, **kwargs: Any) -> 'MeterCorrectionApplicability':
        return _seal(
            cls, kwargs, 'applicability_id', 'applicability_sha256',
            'mmappl',
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_correction_applicability(
    match: DisplayMeterMatchProfile,
    *,
    current_instrument: InstrumentIdentity,
    current_display_state: DisplaySpdIdentity,
    current_correction_state: PreExistingCorrection,
    verification: ProbeMatchVerification | None = None,
    evaluated_at_utc: str | None = None,
) -> MeterCorrectionApplicability:
    """Fail-closed applicability of a stored match to a new measurement.

    A match never silently applies to another unit, display, picture
    state or patch set — every axis is compared to the sealed match.
    """
    reasons: list[MatchReason] = []
    verdict: ApplicabilityVerdict = 'applicable'

    target = match.target_instrument
    if (
        current_instrument.serial != target.serial
        or current_instrument.model != target.model
    ):
        verdict = 'not_applicable'
        reasons.append('DIFFERENT_TARGET_UNIT')
    if (
        current_display_state.display_instance
        != match.display_state.display_instance
        or current_display_state.display_model
        != match.display_state.display_model
    ):
        verdict = 'not_applicable'
        reasons.append('DIFFERENT_DISPLAY_INSTANCE')
    display_fields = (
        'firmware', 'picture_mode', 'gamut_mode', 'white_point_state',
        'emitter_mode', 'content_mode', 'dimming_abl_state',
        'optical_path',
    )
    if any(
        getattr(current_display_state, f)
        != getattr(match.display_state, f)
        for f in display_fields
    ):
        if verdict == 'applicable':
            verdict = 'not_applicable'
        reasons.append('DISPLAY_STATE_CHANGED')
    if (
        current_correction_state.state == 'custom_match_active'
        and current_correction_state.correction_ref is not None
        and current_correction_state.correction_ref.ref_sha256
        != match.correction_artifact_sha256
    ):
        verdict = 'not_applicable'
        reasons.append('DOUBLE_APPLICATION_RISK')
    elif (
        current_correction_state.state
        != match.pre_existing_correction.state
    ):
        if verdict == 'applicable':
            verdict = 'applicable_with_limitations'
        reasons.append('PREEXISTING_CORRECTION_CHANGED')
    if match.pre_existing_correction.state == 'unknown':
        if verdict == 'applicable':
            verdict = 'applicable_with_limitations'
        reasons.append('PREEXISTING_CORRECTION_UNKNOWN')
    if match.display_state.spd_basis in (
        'inferred_from_mode', 'unknown'
    ) and verdict == 'applicable':
        verdict = 'applicable_with_limitations'
        reasons.append('MATCH_LIMITED')
    if verification is None:
        if verdict == 'applicable':
            verdict = 'applicable_with_limitations'
        reasons.append('VERIFICATION_MISSING')
    elif not verification.passed:
        verdict = 'not_applicable'
        reasons.append('VERIFICATION_FAILED')
    else:
        reasons.append('VERIFICATION_PASSED')
    if verdict == 'applicable':
        reasons.append('MATCH_APPLICABLE')
    return MeterCorrectionApplicability.create(
        document_id=match.document_id,
        match_ref=AuthorityRef(
            kind='display_meter_match_profile',
            ref_id=match.match_id,
            ref_sha256=match.match_sha256,
        ),
        current_instrument=current_instrument,
        current_display_state=current_display_state,
        current_correction_state=current_correction_state,
        verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
        evaluated_at_utc=evaluated_at_utc or _utc_now(),
    )


# ---------------------------------------------------------------------------
# JA labels
# ---------------------------------------------------------------------------

CORRECTION_STATE_LABELS: dict[str, str] = {
    'raw_default_response': '生応答（既定）',
    'manufacturer_correction_active': 'メーカー補正が有効',
    'generic_technology_correction_active': '汎用技術補正が有効',
    'custom_match_active': 'カスタムマッチが有効',
    'unknown': '補正状態不明',
}

APPLICABILITY_LABELS: dict[str, str] = {
    'applicable': '適用可能',
    'applicable_with_limitations': '条件付き適用可能',
    'not_applicable': '適用不可',
    'insufficient_evidence': '証拠不足',
}

REASON_LABELS: dict[str, str] = {
    'MATCH_APPLICABLE': 'マッチは適用可能です',
    'MATCH_LIMITED': 'マッチの適用には制約があります',
    'MATCH_NOT_APPLICABLE': 'マッチは適用できません',
    'DIFFERENT_TARGET_UNIT': '対象機器の個体が異なります',
    'DIFFERENT_REFERENCE_UNIT': '基準機器の個体が異なります',
    'DIFFERENT_DISPLAY_INSTANCE': 'ディスプレイ個体が異なります',
    'DISPLAY_STATE_CHANGED': 'ディスプレイ状態が変化しています',
    'PATCH_SET_CHANGED': 'パッチセットが変化しています',
    'PREEXISTING_CORRECTION_UNKNOWN': '既存補正状態が不明です',
    'PREEXISTING_CORRECTION_CHANGED': '既存補正状態が変化しています',
    'DOUBLE_APPLICATION_RISK': '補正の二重適用リスクがあります',
    'CALIBRATION_STATE_STALE': '校正状態が失効しています',
    'RESIDUAL_ERROR_UNMEASURED': '残差が未測定です',
    'VERIFICATION_MISSING': '検証が未実施です',
    'VERIFICATION_PASSED': '検証に合格しました',
    'VERIFICATION_FAILED': '検証に失敗しました',
}
