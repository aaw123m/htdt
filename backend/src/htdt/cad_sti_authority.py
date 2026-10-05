"""Versioned speech-intelligibility (STI) authority (#605).

STI per IEC 60268-16:2020 + COR1:2025 is an objective speech-transmission
measure over a declared path — never a "movie dialogue quality score".
A system can meet SPL/FR targets and still deliver poor intelligibility
through background noise, reverberation, occlusion or signal-chain
processing; this module makes that evidence explicit and sealed.

Literature basis (research verified 2026-10-05):

- IEC 60268-16:2020 ed.5 + COR1:2025 — STI model, test signals,
  measurement and prediction methods; Annex A MTF: 7 octave bands
  (125 Hz-8 kHz) x 14 modulation frequencies (0.63-12.5 Hz); apparent SNR
  clipping +-15 dB; male octave-band weighting/redundancy factors
  (female weighting removed in ed.5); indirect MTF derivation from the
  squared impulse response (A.3.2).
- The standard explicitly limits scope: fluctuating noise is not fully
  covered (clauses 7.13/8.9.3), voice-compression/vocoder systems
  require caution, and it prescribes no universal home-theater pass
  criterion — HTDT reports evidence, not invented thresholds.

Authority boundary:

- raw evidence (per-band MTRs or the band-filtered IR) is canonical;
  the scalar STI is a derived result pinned to profile+method+path;
- every qualified result binds the exact #580 background-noise
  measurement and the declared speech/test level — a cold/quiet room
  cannot launder an operational-noise claim;
- measured vs predicted are distinct evidence classes; simulated STI
  carries a #566 validation pin and never earns measured-equivalent
  status by default;
- no universal threshold is invented: targets must bind a declared
  measurement_standard / project_target / external_application_criterion;
- per-seat evidence is preserved before any aggregation;
- this module does not own room decay (#571), occlusion geometry (#590),
  campaign sampling (#581), prediction validation (#566), uncertainty
  propagation (#604) or codec qualification (#603).
"""

from __future__ import annotations

from cmath import exp as _cexp, pi as _pi
from math import isfinite, log10, sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)
from .clock import utc_now_iso as _utc_now


STI_SCHEMA_VERSION = 1
STI_AUTHORITY_VERSION = 'speech-intelligibility-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'
_PROFILE_ID_PATTERN = r'^stiprof:[0-9a-f]{64}$'
_MEASUREMENT_ID_PATTERN = r'^stim:[0-9a-f]{64}$'
_PREDICTION_ID_PATTERN = r'^stip:[0-9a-f]{64}$'
_ASSESSMENT_ID_PATTERN = r'^dia:[0-9a-f]{64}$'
_NOISE_MEASUREMENT_PATTERN = r'^bnm:[0-9a-f]{64}$'


STIMethod = Literal[
    'direct_sti_measurement',
    'stipa',
    'ir_derived_sti',
    'simulated_sti',
    'hybrid_calibrated_prediction',
    'unsupported',
    'unknown',
]

STIVoiceClass = Literal[
    'male', 'female_legacy', 'unspecified',
]

STISignalChainClass = Literal[
    'acoustic_only',
    'electronic_transparent',
    'codec_compressed',
    'vocoder',
    'unknown',
]

STIApplicability = Literal[
    'in_scope', 'limited', 'out_of_scope',
]

STITargetClass = Literal[
    'measurement_standard',
    'project_target',
    'external_application_criterion',
]

STIComparisonVerdict = Literal[
    'improved', 'degraded', 'unchanged', 'incomparable',
]


STI_METHOD_LABELS: dict[str, str] = {
    'direct_sti_measurement': '直接STI測定',
    'stipa': 'STIPA測定',
    'ir_derived_sti': 'IR由来STI',
    'simulated_sti': 'シミュレーションSTI',
    'hybrid_calibrated_prediction': 'ハイブリッド較正予測',
    'unsupported': '非対応手法',
    'unknown': '不明',
}

TARGET_CLASS_LABELS: dict[str, str] = {
    'measurement_standard': '測定規格',
    'project_target': 'プロジェクト目標',
    'external_application_criterion': '外部適用基準',
}


# ---------------------------------------------------------------------------
# IEC 60268-16 constants (Annex A)
# ---------------------------------------------------------------------------

#: Octave bands for full STI, Hz.
STI_OCTAVE_BANDS_HZ: tuple[float, ...] = (
    125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0,
)

#: Modulation frequencies, Hz (third-octave steps 0.63-12.5).
STI_MODULATION_FREQUENCIES_HZ: tuple[float, ...] = (
    0.63, 0.80, 1.00, 1.25, 1.60, 2.00, 2.50,
    3.15, 4.00, 5.00, 6.30, 8.00, 10.00, 12.50,
)

#: Male speech octave-band weights, IEC 60268-16 Table A.1 (edition 5
#: removed the female weighting set). beta[k] is the redundancy factor
#: between adjacent bands k and k+1 — the 7th entry is unused by the
#: pair-sum and recorded as published (0.0, cf. Steeneken Table II).
STI_MALE_ALPHA: tuple[float, ...] = (
    0.085, 0.127, 0.230, 0.233, 0.309, 0.224, 0.173,
)
STI_MALE_BETA: tuple[float, ...] = (
    0.085, 0.078, 0.065, 0.011, 0.047, 0.095, 0.0,
)

#: Edition-4 female weighting (Steeneken Table II / IEC 60268-16:2011),
#: retained so a pinned iec-60268-16@2011 profile keeps its exact
#: semantics (never silently migrated to ed.5).
STI_FEMALE_ALPHA: tuple[float, ...] = (
    0.0, 0.117, 0.223, 0.216, 0.328, 0.250, 0.194,
)
STI_FEMALE_BETA: tuple[float, ...] = (
    0.0, 0.099, 0.066, 0.062, 0.025, 0.076, 0.0,
)


class SpeechIntelligibilityProfile(BaseModel):
    """Sealed STI profile: exact IEC revision + method + weighting +
    declared corrections. A 2011 profile never produces a 2020 result."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STI_SCHEMA_VERSION
    authority_version: Literal[
        'speech-intelligibility-1'
    ] = STI_AUTHORITY_VERSION
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    profile_label: str = Field(min_length=1)
    method: STIMethod
    voice_class: STIVoiceClass = 'male'
    band_centers_hz: tuple[float, ...] = STI_OCTAVE_BANDS_HZ
    modulation_frequencies_hz: tuple[float, ...] = (
        STI_MODULATION_FREQUENCIES_HZ
    )
    alpha_weights: tuple[float, ...] = STI_MALE_ALPHA
    beta_weights: tuple[float, ...] = STI_MALE_BETA
    noise_correction_enabled: bool = True
    auditory_masking_correction: bool = False
    calculation_version: str = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_profile(self) -> 'SpeechIntelligibilityProfile':
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('speech intelligibility profile hash mismatch')
        if self.profile_id != f'stiprof:{expected}':
            raise ValueError('speech intelligibility profile id mismatch')
        if len(self.alpha_weights) != len(self.band_centers_hz):
            raise ValueError('alpha weights must align with bands')
        if len(self.beta_weights) != len(self.band_centers_hz):
            raise ValueError('beta weights must align with bands')
        if self.method == 'stipa' and len(self.band_centers_hz) != 7:
            raise ValueError('STIPA uses the 7-band set')
        if (
            self.voice_class == 'female_legacy'
            and self.standard_edition == '2020'
        ):
            raise ValueError(
                'edition 2020 removed the female weighting — pin a 2011 '
                'profile for female-voice STI'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'profile_id', 'profile_sha256'},
        )


class STIPathContext(BaseModel):
    """The exact speech-transmission path under test (issue §1): a center
    result says nothing automatic about phantom center, TV speakers or a
    soundbar preset."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    speech_source_label: str = Field(min_length=1)
    channel_id: str | None = None
    speaker_entity_ref: str | None = None
    routing_state_ref: str | None = None
    eq_state_ref: str | None = None
    room_revision: str | None = None
    seat_ref: str | None = None
    occupancy_state: str | None = None
    signal_chain_class: STISignalChainClass = 'acoustic_only'
    speech_level_db: float
    speech_level_reference_point: str = Field(min_length=1)
    calibration_ref: str | None = None

    @model_validator(mode='after')
    def valid_context(self) -> 'STIPathContext':
        if not isfinite(float(self.speech_level_db)):
            raise ValueError('speech_level_db must be finite')
        return self


class STIEvidenceBundle(BaseModel):
    """The raw intelligibility evidence — retained so the scalar STI
    remains a derived result, not the canonical artifact."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    #: Per-band modulation-transfer ratios, each aligned to the profile's
    #: modulation frequencies (7 x 14 for full STI).
    mtr_matrix: tuple[tuple[float, ...], ...] | None = None
    #: Optional band-filtered IR energy (h^2 samples) per octave band —
    #: rows aligned to band_centers_hz, samples at ``sample_rate_hz``.
    ir_energy_rows: tuple[tuple[float, ...], ...] | None = None
    sample_rate_hz: float | None = None
    #: Per-band noise levels (dB SPL) used for the SNR correction —
    #: normally derived from the bound #580 measurement.
    noise_band_levels_db: tuple[float, ...] | None = None
    speech_band_levels_db: tuple[float, ...] | None = None
    ir_asset_refs: tuple[str, ...] = ()
    processing_parameters: dict[str, str] = {}

    @model_validator(mode='after')
    def valid_evidence(self) -> 'STIEvidenceBundle':
        if self.mtr_matrix is None and self.ir_energy_rows is None:
            raise ValueError(
                'STI evidence requires an MTR matrix or band-filtered IR'
            )
        if self.mtr_matrix is not None:
            for row in self.mtr_matrix:
                if not all(0.0 <= float(m) <= 1.0 for m in row):
                    raise ValueError('MTR values must be in [0, 1]')
        if self.ir_energy_rows is not None and self.sample_rate_hz is None:
            raise ValueError('IR evidence requires sample_rate_hz')
        for key in (
            'noise_band_levels_db', 'speech_band_levels_db'
        ):
            values = getattr(self, key)
            if values is not None and not all(
                isfinite(float(v)) for v in values
            ):
                raise ValueError(f'{key} must be finite')
        return self


def _require_iso8601(value: str, field: str) -> str:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{field} must be ISO-8601: {value!r}') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{field} must carry a timezone: {value!r}')
    return value


class STIMeasurement(BaseModel):
    """Sealed measured/derived STI evidence at one seat on one path."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STI_SCHEMA_VERSION
    authority_version: Literal[
        'speech-intelligibility-1'
    ] = STI_AUTHORITY_VERSION
    measurement_id: str = Field(pattern=_MEASUREMENT_ID_PATTERN)
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    path: STIPathContext
    method: STIMethod
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)

    #: Mandatory #580 binding — the operational noise state is part of
    #: the result's identity.
    noise_measurement_id: str = Field(pattern=_NOISE_MEASUREMENT_PATTERN)
    noise_measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    noise_temporal_class: str | None = None

    evidence: STIEvidenceBundle
    sti_value: float | None = None
    band_snr_db: tuple[float, ...] = ()
    applicability: STIApplicability = 'in_scope'
    applicability_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    measured_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'STIMeasurement':
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('STI measurement hash mismatch')
        if self.measurement_id != f'stim:{expected}':
            raise ValueError('STI measurement id mismatch')
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.sti_value is not None and not (
            0.0 <= float(self.sti_value) <= 1.0
        ):
            raise ValueError('sti_value must be in [0, 1]')
        if self.method in ('simulated_sti', 'hybrid_calibrated_prediction'):
            raise ValueError(
                'simulated/predicted results belong in STIPrediction — '
                'measured-equivalent status is never granted implicitly'
            )
        if self.method in ('unsupported', 'unknown'):
            raise ValueError(
                'unsupported/unknown methods cannot mint a qualified '
                'measurement — declare a concrete method or keep the '
                'record as raw evidence'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'measurement_id', 'measurement_sha256'},
        )


class STIPrediction(BaseModel):
    """Sealed predicted/simulated STI — a distinct evidence class that
    requires a #566 validation pin and #604 uncertainty propagation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STI_SCHEMA_VERSION
    authority_version: Literal[
        'speech-intelligibility-1'
    ] = STI_AUTHORITY_VERSION
    prediction_id: str = Field(pattern=_PREDICTION_ID_PATTERN)
    prediction_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    path: STIPathContext
    method: Literal['ir_derived_sti', 'simulated_sti',
                    'hybrid_calibrated_prediction']
    profile_id: str = Field(pattern=_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    standard_id: str = Field(min_length=1)
    standard_edition: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    validation_ref: str | None = None
    uncertainty_db: float | None = None
    uncertainty_ref: str | None = None

    noise_measurement_id: str = Field(pattern=_NOISE_MEASUREMENT_PATTERN)
    noise_measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    noise_temporal_class: str | None = None

    evidence: STIEvidenceBundle | None = None
    sti_value: float | None = None
    band_snr_db: tuple[float, ...] = ()
    applicability: STIApplicability = 'in_scope'
    applicability_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    predicted_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_prediction(self) -> 'STIPrediction':
        expected = _hash(self.identity_payload())
        if self.prediction_sha256 != expected:
            raise ValueError('STI prediction hash mismatch')
        if self.prediction_id != f'stip:{expected}':
            raise ValueError('STI prediction id mismatch')
        _require_iso8601(self.predicted_at_utc, 'predicted_at_utc')
        if self.sti_value is not None and not (
            0.0 <= float(self.sti_value) <= 1.0
        ):
            raise ValueError('sti_value must be in [0, 1]')
        if self.method == 'simulated_sti' and self.validation_ref is None:
            raise ValueError(
                'simulated STI requires a #566 validation reference — '
                'unvalidated simulation never earns a sealed result'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'prediction_id', 'prediction_sha256'},
        )


class STISeatResult(BaseModel):
    """One seat's STI inside a distribution record — raw per-seat values
    stay visible (a strong MLP never hides a blocked rear seat)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_label: str = Field(min_length=1)
    role: Literal[
        'reference', 'listening_area', 'holdout', 'auxiliary'
    ] = 'listening_area'
    sti_value: float | None = None
    measurement_id: str | None = None
    prediction_id: str | None = None
    occlusion_flag: bool = False
    note: str | None = None

    @model_validator(mode='after')
    def valid_seat(self) -> 'STISeatResult':
        if self.sti_value is not None and not (
            0.0 <= float(self.sti_value) <= 1.0
        ):
            raise ValueError('sti_value must be in [0, 1]')
        return self


class STITargetBinding(BaseModel):
    """An explicitly declared criterion — HTDT never invents a universal
    home-theater STI pass/fail (issue §12)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    target_class: STITargetClass
    source: str = Field(min_length=1)
    threshold: float | None = None
    wording: str | None = None

    @model_validator(mode='after')
    def valid_target(self) -> 'STITargetBinding':
        if self.threshold is not None and not (
            0.0 <= float(self.threshold) <= 1.0
        ):
            raise ValueError('threshold must be in [0, 1]')
        return self


class DialogueIntelligibilityAssessment(BaseModel):
    """Sealed diagnostic view over STI evidence (issue §11): possible
    causes stay independent — no automatic single-cause diagnosis."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = STI_SCHEMA_VERSION
    authority_version: Literal[
        'speech-intelligibility-1'
    ] = STI_AUTHORITY_VERSION
    assessment_id: str = Field(pattern=_ASSESSMENT_ID_PATTERN)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    seat_results: tuple[STISeatResult, ...] = Field(min_length=1)
    reference_seat_label: str | None = None
    worst_seat_label: str | None = None
    percentile_90_worst: float | None = None
    target: STITargetBinding | None = None
    candidate_causes: tuple[str, ...] = ()
    independent_observations: tuple[str, ...] = ()
    wording_class: Literal[
        'objective_speech_transmission_intelligibility_evidence'
    ] = 'objective_speech_transmission_intelligibility_evidence'
    limitations: tuple[str, ...] = ()
    assessed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'DialogueIntelligibilityAssessment':
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('dialogue intelligibility assessment hash')
        if self.assessment_id != f'dia:{expected}':
            raise ValueError('dialogue intelligibility assessment id')
        labels = [s.seat_label for s in self.seat_results]
        if len(set(labels)) != len(labels):
            raise ValueError('seat labels must be unique')
        _require_iso8601(self.assessed_at_utc, 'assessed_at_utc')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'assessment_id', 'assessment_sha256'},
        )

    @property
    def worst_seat(self) -> STISeatResult | None:
        rated = [s for s in self.seat_results if s.sti_value is not None]
        return min(rated, key=lambda s: float(s.sti_value), default=None)

    @property
    def percentile_10(self) -> float | None:
        """10th-percentile STI across seats — the honest "some seats are
        much worse" statistic."""
        rated = sorted(
            float(s.sti_value)
            for s in self.seat_results
            if s.sti_value is not None
        )
        if not rated:
            return None
        idx = max(0, round(0.1 * (len(rated) - 1)))
        return rated[idx]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_sti_profile(**kwargs: Any) -> SpeechIntelligibilityProfile:
    probe = SpeechIntelligibilityProfile.model_construct(
        **canonicalize_payload(
            SpeechIntelligibilityProfile,
            dict(
                schema_version=STI_SCHEMA_VERSION,
                authority_version=STI_AUTHORITY_VERSION,
                profile_id='',
                profile_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return SpeechIntelligibilityProfile(
        **probe.model_dump(exclude={'profile_id', 'profile_sha256'}),
        profile_id=f'stiprof:{sha}',
        profile_sha256=sha,
    )


def _seal_sti_measurement(**kwargs: Any) -> STIMeasurement:
    probe = STIMeasurement.model_construct(
        **canonicalize_payload(
            STIMeasurement,
            dict(
                schema_version=STI_SCHEMA_VERSION,
                authority_version=STI_AUTHORITY_VERSION,
                measurement_id='',
                measurement_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return STIMeasurement(
        **probe.model_dump(exclude={'measurement_id', 'measurement_sha256'}),
        measurement_id=f'stim:{sha}',
        measurement_sha256=sha,
    )


def _seal_sti_prediction(**kwargs: Any) -> STIPrediction:
    probe = STIPrediction.model_construct(
        **canonicalize_payload(
            STIPrediction,
            dict(
                schema_version=STI_SCHEMA_VERSION,
                authority_version=STI_AUTHORITY_VERSION,
                prediction_id='',
                prediction_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return STIPrediction(
        **probe.model_dump(exclude={'prediction_id', 'prediction_sha256'}),
        prediction_id=f'stip:{sha}',
        prediction_sha256=sha,
    )


def _seal_assessment(**kwargs: Any) -> DialogueIntelligibilityAssessment:
    probe = DialogueIntelligibilityAssessment.model_construct(
        **canonicalize_payload(
            DialogueIntelligibilityAssessment,
            dict(
                schema_version=STI_SCHEMA_VERSION,
                authority_version=STI_AUTHORITY_VERSION,
                assessment_id='',
                assessment_sha256='',
                **kwargs,
            ),
        )
    )
    sha = _hash(probe.identity_payload())
    return DialogueIntelligibilityAssessment(
        **probe.model_dump(exclude={'assessment_id', 'assessment_sha256'}),
        assessment_id=f'dia:{sha}',
        assessment_sha256=sha,
    )


def seed_speech_intelligibility_profiles(
    *, document_id: str, created_at_utc: str | None = None
) -> tuple[SpeechIntelligibilityProfile, ...]:
    """Built-in profiles pinned to IEC 60268-16 editions via #599."""
    created = created_at_utc or _utc_now()
    common = dict(
        document_id=document_id,
        standard_id='iec-60268-16',
        publisher='IEC',
        band_centers_hz=STI_OCTAVE_BANDS_HZ,
        modulation_frequencies_hz=STI_MODULATION_FREQUENCIES_HZ,
        created_at_utc=created,
    )
    return (
        build_sti_profile(
            **common,
            standard_edition='2020',
            profile_label='IEC 60268-16:2020+COR1:2025 direct STI',
            method='direct_sti_measurement',
            calculation_version='iec-60268-16-ed5-mtf-1',
            limitations=(
                'fluctuating_noise_not_covered_by_standard_model',
                'vocoder_compression_paths_require_caution',
                'no_universal_home_theater_pass_criterion',
            ),
        ),
        build_sti_profile(
            **common,
            standard_edition='2020',
            profile_label='IEC 60268-16:2020 STIPA',
            method='stipa',
            calculation_version='iec-60268-16-ed5-stipa-1',
            limitations=(
                'stipa_subset_of_full_sti',
                'fluctuating_noise_not_covered_by_standard_model',
            ),
        ),
        build_sti_profile(
            **common,
            standard_edition='2020',
            profile_label='IEC 60268-16:2020 IR-derived STI',
            method='ir_derived_sti',
            calculation_version='iec-60268-16-ed5-indirect-1',
            limitations=(
                'ir_must_be_band_filtered_eligible',
                'fluctuating_noise_not_covered_by_standard_model',
            ),
        ),
        build_sti_profile(
            **common,
            standard_edition='2020',
            profile_label='IEC 60268-16:2020 simulated STI',
            method='simulated_sti',
            calculation_version='iec-60268-16-ed5-prediction-1',
            limitations=(
                'requires_566_validation',
                'requires_604_uncertainty_propagation',
            ),
        ),
        build_sti_profile(
            **common,
            standard_edition='2011',
            profile_label='IEC 60268-16:2011 legacy direct STI (female)',
            method='direct_sti_measurement',
            voice_class='female_legacy',
            alpha_weights=STI_FEMALE_ALPHA,
            beta_weights=STI_FEMALE_BETA,
            calculation_version='iec-60268-16-ed4-mtf-1',
            limitations=(
                'superseded_edition_historical_only',
                'female_weighting_removed_in_edition_5',
            ),
        ),
    )


# ---------------------------------------------------------------------------
# IEC 60268-16 math (Annex A): MTR -> apparent SNR -> weighted STI
# ---------------------------------------------------------------------------


def _clip_snr(snr_db: float) -> float:
    return max(-15.0, min(15.0, snr_db))


def mtr_to_snr_db(mtr: float) -> float:
    """Apparent SNR for one modulation-transfer ratio (A.2.2)."""
    if mtr >= 1.0:
        return 15.0
    if mtr <= 0.0:
        return -15.0
    return _clip_snr(10.0 * log10(mtr / (1.0 - mtr)))


def apply_noise_correction(
    mtr: float, snr_db: float | None
) -> float:
    """IEC MTF noise correction: m / (1 + 10^(-SNR/10))."""
    if snr_db is None:
        return mtr
    return mtr / (1.0 + 10.0 ** (-float(snr_db) / 10.0))


def mtf_band_to_sti_terms(
    mtr_row: tuple[float, ...],
    *,
    noise_snr_db: float | None = None,
) -> float:
    """Mean transmission-index term for one octave band."""
    terms = [
        (mtr_to_snr_db(apply_noise_correction(m, noise_snr_db)) + 15.0)
        / 30.0
        for m in mtr_row
    ]
    return sum(terms) / len(terms)


def compute_sti(
    mtr_matrix: tuple[tuple[float, ...], ...],
    profile: SpeechIntelligibilityProfile,
    *,
    noise_band_snr_db: tuple[float, ...] | None = None,
) -> tuple[float, tuple[float, ...]]:
    """Full STI (IEC 60268-16 A.2.1): per-band mean TI, alpha-weighted
    sum minus the adjacent-band redundancy correction
    ``beta_k * sqrt(TI_k * TI_{k+1})`` for k=1..6.

    Returns ``(sti, per-band mean apparent SNR dB)``.
    """
    if len(mtr_matrix) != len(profile.band_centers_hz):
        raise ValueError('MTR matrix must align with profile bands')
    ti_bars: list[float] = []
    band_snr: list[float] = []
    for k, row in enumerate(mtr_matrix):
        if len(row) != len(profile.modulation_frequencies_hz):
            raise ValueError(
                f'MTR row {k} must have '
                f'{len(profile.modulation_frequencies_hz)} entries'
            )
        snr = (
            noise_band_snr_db[k]
            if noise_band_snr_db is not None
            else None
        )
        ti_bar = mtf_band_to_sti_terms(tuple(row), noise_snr_db=snr)
        ti_bars.append(ti_bar)
        snrs = [
            mtr_to_snr_db(apply_noise_correction(m, snr)) for m in row
        ]
        band_snr.append(sum(snrs) / len(snrs))
    sti = sum(
        profile.alpha_weights[k] * ti_bar
        for k, ti_bar in enumerate(ti_bars)
    )
    for k in range(len(ti_bars) - 1):
        sti -= profile.beta_weights[k] * sqrt(
            max(ti_bars[k] * ti_bars[k + 1], 0.0)
        )
    return max(0.0, min(1.0, sti)), tuple(band_snr)


def mtr_from_band_ir(
    energy: tuple[float, ...],
    sample_rate_hz: float,
    modulation_frequencies_hz: tuple[float, ...] =
    STI_MODULATION_FREQUENCIES_HZ,
) -> tuple[float, ...]:
    """Indirect MTF from the squared impulse response (A.3.2):

    ``m(f_m) = |Σ h²[n] · e^{-j·2π·f_m·n/fs}| / Σ h²[n]``
    """
    if not energy or not all(isfinite(float(v)) for v in energy):
        raise ValueError('IR energy must be finite samples')
    total = sum(energy)
    if total <= 0:
        raise ValueError('IR energy must be positive')
    out: list[float] = []
    for fm in modulation_frequencies_hz:
        omega = 2.0 * _pi * fm / sample_rate_hz
        acc = 0j
        for n, e in enumerate(energy):
            acc += e * _cexp(-1j * omega * n)
        out.append(min(1.0, abs(acc) / total))
    return tuple(out)


def evaluate_sti_measurement(
    *,
    profile: SpeechIntelligibilityProfile,
    path: STIPathContext,
    evidence: STIEvidenceBundle,
    document_id: str,
    noise_measurement_id: str,
    noise_measurement_sha256: str,
    noise_temporal_class: str | None = None,
    measured_at_utc: str | None = None,
) -> STIMeasurement:
    """Compute + seal a measured/derived STI result with its eligibility
    gates (noise state bound, signal-chain class honored, fluctuating
    noise limitation surfaced)."""
    reasons: list[str] = []
    limitations = list(profile.limitations)
    if path.signal_chain_class in ('codec_compressed', 'vocoder'):
        reasons.append('signal_chain_requires_caution')
        limitations.append('sti_not_validated_for_compressed_chain')
    if noise_temporal_class in (
        'low_frequency_fluctuating', 'intermittent', 'impulsive', 'mixed'
    ):
        reasons.append('fluctuating_noise_bound')
        limitations.append('sti_valid_for_captured_steady_state_only')
    mtr = evidence.mtr_matrix
    if mtr is None and evidence.ir_energy_rows is not None:
        assert evidence.sample_rate_hz is not None
        mtr = tuple(
            mtr_from_band_ir(
                tuple(row), evidence.sample_rate_hz,
                profile.modulation_frequencies_hz,
            )
            for row in evidence.ir_energy_rows
        )
    sti_value: float | None = None
    band_snr: tuple[float, ...] = ()
    applicability: STIApplicability = 'in_scope'
    if mtr is None:
        applicability = 'limited'
        reasons.append('no_eligible_mtr_evidence')
    else:
        snr_per_band: tuple[float, ...] | None = None
        if (
            evidence.noise_band_levels_db is not None
            and evidence.speech_band_levels_db is not None
            and profile.noise_correction_enabled
        ):
            if (
                len(evidence.noise_band_levels_db) != len(mtr)
                or len(evidence.speech_band_levels_db) != len(mtr)
            ):
                raise ValueError('noise/speech band levels must align')
            snr_per_band = tuple(
                s - n for s, n in zip(
                    evidence.speech_band_levels_db,
                    evidence.noise_band_levels_db,
                )
            )
        elif profile.noise_correction_enabled:
            reasons.append('noise_correction_unavailable')
            limitations.append('snr_correction_not_applied')
        sti_value, band_snr = compute_sti(
            mtr, profile, noise_band_snr_db=snr_per_band
        )
    if any('requires_caution' in r or 'fluctuating' in r for r in reasons):
        applicability = 'limited' if applicability == 'in_scope' else applicability
    return _seal_sti_measurement(
        document_id=document_id,
        path=path,
        method=profile.method,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        standard_id=profile.standard_id,
        standard_edition=profile.standard_edition,
        calculation_version=profile.calculation_version,
        noise_measurement_id=noise_measurement_id,
        noise_measurement_sha256=noise_measurement_sha256,
        noise_temporal_class=noise_temporal_class,
        evidence=evidence,
        sti_value=sti_value,
        band_snr_db=band_snr,
        applicability=applicability,
        applicability_reasons=tuple(reasons),
        limitations=tuple(dict.fromkeys(limitations)),
        measured_at_utc=measured_at_utc or _utc_now(),
    )


def evaluate_sti_prediction(
    *,
    profile: SpeechIntelligibilityProfile,
    path: STIPathContext,
    document_id: str,
    model_version: str,
    noise_measurement_id: str,
    noise_measurement_sha256: str,
    evidence: STIEvidenceBundle | None = None,
    validation_ref: str | None = None,
    uncertainty_db: float | None = None,
    uncertainty_ref: str | None = None,
    noise_temporal_class: str | None = None,
    predicted_at_utc: str | None = None,
) -> STIPrediction:
    """Sealed predicted STI — never measured-equivalent without its
    declared #566 validation pin."""
    if profile.method not in (
        'ir_derived_sti', 'simulated_sti', 'hybrid_calibrated_prediction'
    ):
        raise ValueError(
            'prediction requires a prediction-capable profile method'
        )
    limitations = list(profile.limitations) + ['predicted_not_measured']
    sti_value: float | None = None
    band_snr: tuple[float, ...] = ()
    if evidence is not None and evidence.mtr_matrix is not None:
        sti_value, band_snr = compute_sti(evidence.mtr_matrix, profile)
    return _seal_sti_prediction(
        document_id=document_id,
        path=path,
        method=profile.method,
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        standard_id=profile.standard_id,
        standard_edition=profile.standard_edition,
        calculation_version=profile.calculation_version,
        model_version=model_version,
        validation_ref=validation_ref,
        uncertainty_db=uncertainty_db,
        uncertainty_ref=uncertainty_ref,
        noise_measurement_id=noise_measurement_id,
        noise_measurement_sha256=noise_measurement_sha256,
        noise_temporal_class=noise_temporal_class,
        evidence=evidence,
        sti_value=sti_value,
        band_snr_db=band_snr,
        limitations=tuple(dict.fromkeys(limitations)),
        predicted_at_utc=predicted_at_utc or _utc_now(),
    )


def compare_sti_evidence(
    before: STIMeasurement | STIPrediction,
    after: STIMeasurement | STIPrediction,
    *,
    tolerance: float = 1e-9,
) -> STIComparisonVerdict:
    """Before/after commissioning comparison (issue §15): incompatible
    method/profile/geometry/level/state ⇒ ``incomparable``, never a
    fabricated improvement delta."""
    if type(before) is not type(after):
        return 'incomparable'
    pairs = (
        (before.method, after.method),
        (before.profile_sha256, after.profile_sha256),
        (before.path.seat_ref, after.path.seat_ref),
        (before.path.speaker_entity_ref, after.path.speaker_entity_ref),
        (before.path.routing_state_ref, after.path.routing_state_ref),
        (before.path.eq_state_ref, after.path.eq_state_ref),
        (before.path.room_revision, after.path.room_revision),
        (before.path.speech_level_db, after.path.speech_level_db),
        (before.path.speech_level_reference_point,
         after.path.speech_level_reference_point),
        (before.path.calibration_ref, after.path.calibration_ref),
        (before.noise_measurement_id, after.noise_measurement_id),
    )
    if any(a != b for a, b in pairs):
        return 'incomparable'
    if before.sti_value is None or after.sti_value is None:
        return 'incomparable'
    delta = float(after.sti_value) - float(before.sti_value)
    if delta > tolerance:
        return 'improved'
    if delta < -tolerance:
        return 'degraded'
    return 'unchanged'


def build_dialogue_intelligibility_assessment(
    *,
    document_id: str,
    seat_results: tuple[STISeatResult, ...] | list[STISeatResult],
    target: STITargetBinding | None = None,
    reference_seat_label: str | None = None,
    candidate_causes: tuple[str, ...] = (),
    independent_observations: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    assessed_at_utc: str | None = None,
) -> DialogueIntelligibilityAssessment:
    """Aggregate per-seat evidence honestly; no invented threshold,
    no single automatic diagnosis."""
    rated = [s for s in seat_results if s.sti_value is not None]
    worst_label = (
        min(rated, key=lambda s: float(s.sti_value)).seat_label
        if rated else None
    )
    sorted_vals = sorted(float(s.sti_value) for s in rated)
    pct90_worst = (
        sorted_vals[max(0, round(0.1 * (len(sorted_vals) - 1)))]
        if sorted_vals else None
    )
    return _seal_assessment(
        document_id=document_id,
        seat_results=tuple(seat_results),
        reference_seat_label=reference_seat_label,
        worst_seat_label=worst_label,
        percentile_90_worst=pct90_worst,
        target=target,
        candidate_causes=tuple(candidate_causes),
        independent_observations=tuple(independent_observations),
        limitations=tuple(limitations),
        assessed_at_utc=assessed_at_utc or _utc_now(),
    )


__all__ = [
    'DialogueIntelligibilityAssessment',
    'STIApplicability',
    'STIComparisonVerdict',
    'STIEvidenceBundle',
    'STIMethod',
    'STIMeasurement',
    'STIPathContext',
    'STIPrediction',
    'STISeatResult',
    'STISignalChainClass',
    'STITargetBinding',
    'STITargetClass',
    'STIVoiceClass',
    'STI_AUTHORITY_VERSION',
    'STI_FEMALE_ALPHA',
    'STI_FEMALE_BETA',
    'STI_MALE_ALPHA',
    'STI_MALE_BETA',
    'STI_METHOD_LABELS',
    'STI_MODULATION_FREQUENCIES_HZ',
    'STI_OCTAVE_BANDS_HZ',
    'STI_SCHEMA_VERSION',
    'TARGET_CLASS_LABELS',
    'SpeechIntelligibilityProfile',
    'apply_noise_correction',
    'build_dialogue_intelligibility_assessment',
    'build_sti_profile',
    'compare_sti_evidence',
    'compute_sti',
    'evaluate_sti_measurement',
    'evaluate_sti_prediction',
    'mtr_from_band_ir',
    'mtr_to_snr_db',
    'mtf_band_to_sti_terms',
    'seed_speech_intelligibility_profiles',
]
