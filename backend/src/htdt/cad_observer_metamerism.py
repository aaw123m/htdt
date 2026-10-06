"""Display observer-metamerism authority (#626).

Two displays can measure to the same nominal tristimulus/chromaticity
target while different observers perceive a residual color mismatch —
their spectral power distributions differ and real observers do not all
match the CIE standard observer.  This is observer metamerism /
observer metameric failure, and it grows with narrow-spectrum
wide-gamut displays.

This module is the fail-closed authority over that claim:

- :class:`DisplaySpectralState` — the measured spectral identity of one
  display under one state: exact SPD samples (wavelength/value pairs),
  spectral range and sampling, radiometric normalization, instrument
  binding, stimulus binding.  Tristimulus-only evidence is honestly
  labelled and can never feed a strict observer-metamerism metric.
- :class:`ObserverModelProfile` — the exact calculation profile: IEC TS
  61966-13:2023+COR1:2025, ANSI/CTA-6035 (2026 US adoption), a CIE
  standard-observer reference, or a project/research observer-variability
  model.  Profiles declare the display system kinds they apply to — IEC
  TS 61966-13 applies to light-emitting or backlit transmitting displays
  under dark-room measurement and is never silently extended to
  reflected projection-screen systems.
- :class:`ObserverMetamerismEvaluation` — a *pair* result: reference
  spectral state vs DUT spectral state under one profile, with the
  metric value, units and semantics.  There is no source-less
  ``observer metamerism = 3`` product scalar.
- :class:`PerceptualMatchRecord` — an optional controlled human-match
  record: procedure, adaptation environment, observer identity *class*
  (never medical/health data), final DUT adjustment and repeatability.
  A perceptual white offset is a project/observer match state — it
  never redefines the nominal standard target.
- :func:`evaluate_observer_metamerism` — seals an
  :class:`ObserverMetamerismQualification` verdict for a declared
  cross-display matching goal.

Contract properties:

- instrument spectral mismatch (#283/#611), standard-observer
  colorimetric difference and observer metameric failure are three
  separate classes — ``metamerism`` is never a catch-all for a bad
  meter profile;
- strict evaluation requires spectral evidence on both sides — x/y
  alone returns ``insufficient_spectral_evidence``;
- an exact SPD is never reconstructed from gamut coordinates;
- ``meter_corrected``/traceable never implies
  ``perceptually_identical_for_all_observers``;
- one calibrator's visual match is never promoted to a universal
  human-perception truth;
- technology labels (WOLED/QD-OLED/…) never substitute for measured
  SPD evidence;
- a user-chosen perceptual white offset never mutates the nominal
  standards target (D65 stays D65);
- no health/genetic/vision-condition data is required or retained —
  observer identity is a reproducibility class only.

Literature basis (issue §research): IEC TS 61966-13:2023 + COR1:2025
(objective observer-metamerism colour-difference metric for displays
with different SPDs, dark-room, light-emitting/backlit scope);
ANSI/CTA-6035 (April 2026 US national adoption); SID/ICDM IDMS v1.3
observer-metamerism framework; CIE 2006 cone fundamentals as the
observer-variability research basis.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _digest, canonicalize_payload as _canon


_SHA256 = r'^[0-9a-f]{64}$'

OBSERVER_METAMERISM_AUTHORITY_VERSION = 'observer-metamerism-1'


# ---------------------------------------------------------------------------
# Taxonomies (issue §1–§5, §10, §11)
# ---------------------------------------------------------------------------

DisplaySystemKind = Literal[
    'direct_view_emissive',
    'direct_view_backlit',
    'projected_reflected',
    'other',
    'unknown',
]

SpdEvidenceClass = Literal[
    'spectroradiometer_measured',
    'spectral_model_from_measured_primaries',
    'manufacturer_declared',
    'tristimulus_only',
    'unknown',
]

ObserverProfileKind = Literal[
    'iec_ts_61966_13_2023_cor1_2025',
    'ansi_cta_6035_2026',
    'cie_standard_observer_reference',
    'cie_2006_cone_fundamentals',
    'project_research_model',
    'other_versioned',
    'unversioned',
    'unknown',
]

ObserverSetKind = Literal[
    'cie_1931_2deg',
    'cie_1964_10deg',
    'cie_2006_cone_fundamentals',
    'iec_61966_13_observer_set',
    'custom_observer_model',
    'unknown',
]

MismatchClass = Literal[
    'instrument_spectral_mismatch',
    'standard_observer_colorimetric_difference',
    'observer_metameric_failure',
    'unknown',
]

MatchGoal = Literal[
    'cross_display_perceptual_match',
    'reference_monitor_matching',
    'project_visual_white_match',
    'informational_only',
]

AdaptationEnvironment = Literal[
    'dark_room',
    'd65_surround',
    'project_environment',
    'uncontrolled',
    'unknown',
]

ObserverIdentityClass = Literal[
    'single_calibrator',
    'observer_panel_pseudonymous',
    'anonymous_sample',
    'not_applicable',
]

QualificationVerdict = Literal[
    'verified_within_profile',
    'verified_with_limitations',
    'unverified_residual_risk',
    'insufficient_spectral_evidence',
    'profile_inapplicable',
    'instrument_mismatch_explains',
    'conflicting_evidence',
]

ObserverMetamerismReason = Literal[
    'VERIFIED_WITHIN_PROFILE',
    'INSUFFICIENT_SPECTRAL_EVIDENCE',
    'TRISTIMULUS_ONLY_EVIDENCE',
    'PROFILE_INAPPLICABLE_TO_SYSTEM_KIND',
    'PROJECTION_OUT_OF_PROFILE_SCOPE',
    'PROFILE_REVISION_MISSING',
    'OBSERVER_MODEL_MISMATCH',
    'METER_CORRECTION_NOT_OBSERVER_PROOF',
    'SINGLE_OBSERVER_NOT_UNIVERSAL',
    'PERCEPTUAL_OFFSET_RECORDED',
    'TECHNOLOGY_LABEL_NOT_SPD',
    'UNPAIRED_SCALAR_REJECTED',
    'INSTRUMENT_MISMATCH_SUSPECTED',
    'MULTI_OBSERVER_DISPERSION_PRESENT',
    'SAME_SPD_PAIR',
    'LIMITATIONS_DECLARED',
    'GOAL_INFORMATIONAL_ONLY',
]


# ---------------------------------------------------------------------------
# Japanese product labels
# ---------------------------------------------------------------------------

SYSTEM_KIND_LABELS: dict[str, str] = {
    'direct_view_emissive': '直視型自発光',
    'direct_view_backlit': '直視型バックライト',
    'projected_reflected': '投射+反射スクリーン',
    'other': 'その他',
    'unknown': '不明',
}

SPD_EVIDENCE_LABELS: dict[str, str] = {
    'spectroradiometer_measured': '分光放射計実測',
    'spectral_model_from_measured_primaries': '実測原色からの分光モデル',
    'manufacturer_declared': 'メーカー宣言',
    'tristimulus_only': '三刺激値のみ',
    'unknown': '不明',
}

PROFILE_KIND_LABELS: dict[str, str] = {
    'iec_ts_61966_13_2023_cor1_2025': 'IEC TS 61966-13:2023+COR1:2025',
    'ansi_cta_6035_2026': 'ANSI/CTA-6035 (2026)',
    'cie_standard_observer_reference': 'CIE 標準観察者参照',
    'cie_2006_cone_fundamentals': 'CIE 2006 錐体 fundamentals',
    'project_research_model': 'プロジェクト/研究モデル',
    'other_versioned': 'その他版管理プロファイル',
    'unversioned': '版管理なし',
    'unknown': '不明',
}

MISMATCH_CLASS_LABELS: dict[str, str] = {
    'instrument_spectral_mismatch': '計器分光ミスマッチ',
    'standard_observer_colorimetric_difference': '標準観察者色差',
    'observer_metameric_failure': '観察者メタメリズム',
    'unknown': '不明',
}

VERDICT_LABELS: dict[str, str] = {
    'verified_within_profile': 'プロファイル内で検証済み',
    'verified_with_limitations': '制限付き検証済み',
    'unverified_residual_risk': '未検証（残留リスクあり）',
    'insufficient_spectral_evidence': '分光証拠不足',
    'profile_inapplicable': 'プロファイル適用外',
    'instrument_mismatch_explains': '計器ミスマッチで説明可能',
    'conflicting_evidence': '証拠が矛盾',
}

REASON_LABELS: dict[str, str] = {
    'VERIFIED_WITHIN_PROFILE': 'プロファイル内で検証済み',
    'INSUFFICIENT_SPECTRAL_EVIDENCE': '分光証拠不足',
    'TRISTIMULUS_ONLY_EVIDENCE': '三刺激値のみの証拠',
    'PROFILE_INAPPLICABLE_TO_SYSTEM_KIND': 'システム種別にプロファイル非適用',
    'PROJECTION_OUT_OF_PROFILE_SCOPE': '投射系はプロファイル適用範囲外',
    'PROFILE_REVISION_MISSING': 'プロファイル版欠落',
    'OBSERVER_MODEL_MISMATCH': '観察者モデル不一致',
    'METER_CORRECTION_NOT_OBSERVER_PROOF': '計器補正は観察者差を消去しない',
    'SINGLE_OBSERVER_NOT_UNIVERSAL': '単一観察者は普遍的真実でない',
    'PERCEPTUAL_OFFSET_RECORDED': '知覚オフセット記録済み（標準ターゲットは不変）',
    'TECHNOLOGY_LABEL_NOT_SPD': '技術ラベルは実測SPDの代用にならない',
    'UNPAIRED_SCALAR_REJECTED': 'ペアなしスカラーは拒否',
    'INSTRUMENT_MISMATCH_SUSPECTED': '計器ミスマッチの疑い',
    'MULTI_OBSERVER_DISPERSION_PRESENT': '複数観察者のばらつきあり',
    'SAME_SPD_PAIR': '同一SPDペア',
    'LIMITATIONS_DECLARED': '制限事項宣言済み',
    'GOAL_INFORMATIONAL_ONLY': '参考目的のみ',
}

OBSERVER_SET_LABELS: dict[str, str] = {
    'cie_1931_2deg': 'CIE 1931 2°',
    'cie_1964_10deg': 'CIE 1964 10°',
    'cie_2006_cone_fundamentals': 'CIE 2006 錐体 fundamentals',
    'iec_61966_13_observer_set': 'IEC 61966-13 観察者セット',
    'custom_observer_model': 'カスタム観察者モデル',
    'unknown': '不明',
}


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


class SpdSample(BaseModel):
    """One spectral sample — wavelength nm paired with the measured
    radiometric quantity in the declared normalization units."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    wavelength_nm: float = Field(gt=0.0)
    value: float

    @model_validator(mode='after')
    def finite(self) -> 'SpdSample':
        if not isfinite(float(self.wavelength_nm)) or not isfinite(
            float(self.value)
        ):
            raise ValueError('SPD samples must be finite')
        return self


class SpdBlock(BaseModel):
    """The measured spectral power distribution evidence (#626 §3).

    Samples are raw measured points.  ``normalization`` declares the
    radiometric convention (e.g. ``peak_normalized``,
    ``radiometric_W_sr_m2_nm``); the artifact hash anchors the raw
    capture where lawful/practical.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    samples: tuple[SpdSample, ...]
    wavelength_min_nm: float = Field(gt=0.0)
    wavelength_max_nm: float = Field(gt=0.0)
    sampling_nm: float | None = Field(default=None, gt=0.0)
    normalization: str = Field(min_length=1)
    raw_artifact_sha256: str | None = Field(default=None, pattern=_SHA256)

    @model_validator(mode='after')
    def valid_spd(self) -> 'SpdBlock':
        if self.wavelength_max_nm <= self.wavelength_min_nm:
            raise ValueError('SPD wavelength range must be increasing')
        if not self.samples:
            raise ValueError('an SPD block needs samples')
        wavelengths = [s.wavelength_nm for s in self.samples]
        if wavelengths != sorted(wavelengths):
            raise ValueError('SPD samples must be wavelength-ordered')
        if len(set(wavelengths)) != len(wavelengths):
            raise ValueError('SPD sample wavelengths must be unique')
        if min(wavelengths) < self.wavelength_min_nm or (
            max(wavelengths) > self.wavelength_max_nm
        ):
            raise ValueError('SPD samples must sit inside the declared range')
        if self.sampling_nm is not None and not isfinite(
            float(self.sampling_nm)
        ):
            raise ValueError('SPD sampling must be finite')
        return self


class ChromaticityPoint(BaseModel):
    """A tristimulus/xy summary — never a substitute for the SPD."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    luminance_cd_m2: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def finite(self) -> 'ChromaticityPoint':
        for v in (self.x, self.y, self.luminance_cd_m2):
            if v is not None and not isfinite(float(v)):
                raise ValueError('chromaticity values must be finite')
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class DisplaySpectralState(BaseModel):
    """Exact spectral identity of one display under one state (#626 §2).

    Binds the display/system kind, operating state, stimulus and the
    measured SPD evidence.  Changing picture mode, panel state or
    spectral-emission mode produces a different spectral state — and a
    different metamerism result.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['observer-metamerism-1'] = (
        OBSERVER_METAMERISM_AUTHORITY_VERSION
    )
    spectral_state_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_ref: str = Field(min_length=1)
    system_kind: DisplaySystemKind = 'unknown'
    display_state_ref: str | None = Field(default=None, min_length=1)
    display_state_sha256: str | None = Field(default=None, pattern=_SHA256)
    stimulus_ref: str | None = Field(default=None, min_length=1)
    stimulus_sha256: str | None = Field(default=None, pattern=_SHA256)
    picture_mode: str | None = Field(default=None, min_length=1)
    firmware: str | None = Field(default=None, min_length=1)
    white_point: ChromaticityPoint | None = None
    evidence_class: SpdEvidenceClass = 'unknown'
    spd: SpdBlock | None = None
    instrument_ref: str | None = Field(default=None, min_length=1)
    instrument_sha256: str | None = Field(default=None, pattern=_SHA256)
    measurement_geometry: str | None = Field(default=None, min_length=1)
    measured_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    spectral_state_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_state(self) -> 'DisplaySpectralState':
        if self.evidence_class == 'spectroradiometer_measured':
            if self.spd is None:
                raise ValueError(
                    'spectroradiometer-measured evidence requires the SPD'
                )
            if self.instrument_ref is None:
                raise ValueError(
                    'spectroradiometer-measured evidence requires the '
                    'instrument binding (#611)'
                )
        if self.evidence_class in ('tristimulus_only', 'unknown') and (
            self.spd is not None
        ):
            raise ValueError(
                'tristimulus-only/unknown evidence cannot carry an SPD — '
                'declare the real evidence class'
            )
        if self.spectral_state_sha256 != _digest(self.identity_payload()):
            raise ValueError('spectral state hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('spectral_state_id', None)
        payload.pop('spectral_state_sha256', None)
        return payload

    @property
    def has_spd(self) -> bool:
        return self.spd is not None

    @property
    def spectral_evaluable(self) -> bool:
        """True only when real spectral evidence exists — tristimulus
        coordinates alone can never feed a metamerism metric."""
        return self.evidence_class in (
            'spectroradiometer_measured',
            'spectral_model_from_measured_primaries',
            'manufacturer_declared',
        ) and self.spd is not None


class ObserverModelProfile(BaseModel):
    """The exact calculation profile (#626 §4, §15).

    ``applicable_system_kinds`` pins the profile's declared scope: the
    IEC TS 61966-13 / ANSI CTA-6035 profiles apply to light-emitting or
    backlit transmitting displays measured under dark-room conditions —
    never silently extended to reflected projection-screen systems.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['observer-metamerism-1'] = (
        OBSERVER_METAMERISM_AUTHORITY_VERSION
    )
    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: ObserverProfileKind = 'unknown'
    revision: str | None = Field(default=None, min_length=1)
    observer_set: ObserverSetKind = 'unknown'
    implementation_version: str | None = Field(
        default=None, min_length=1
    )
    applicable_system_kinds: tuple[DisplaySystemKind, ...] = ()
    external_standard_ref: str | None = Field(default=None, min_length=1)
    dark_room_required: bool = True
    limitations: tuple[str, ...] = ()
    profile_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_profile(self) -> 'ObserverModelProfile':
        versioned = {
            'iec_ts_61966_13_2023_cor1_2025',
            'ansi_cta_6035_2026',
            'cie_2006_cone_fundamentals',
            'other_versioned',
            'project_research_model',
        }
        if self.kind in versioned and self.revision is None:
            raise ValueError(
                'a versioned observer profile requires its revision'
            )
        if self.kind in ('unversioned', 'unknown') and (
            self.revision is not None
        ):
            raise ValueError(
                'unversioned/unknown profiles cannot claim a revision'
            )
        if self.kind in (
            'iec_ts_61966_13_2023_cor1_2025', 'ansi_cta_6035_2026'
        ):
            allowed = {'direct_view_emissive', 'direct_view_backlit'}
            if not set(self.applicable_system_kinds) <= allowed:
                raise ValueError(
                    'IEC TS 61966-13 / ANSI CTA-6035 apply to '
                    'light-emitting or backlit transmitting displays only '
                    '— never silently extend them to reflected '
                    'projection-screen systems'
                )
        if len(set(self.applicable_system_kinds)) != len(
            self.applicable_system_kinds
        ):
            raise ValueError('applicable system kinds must be unique')
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError('profile limitations must be unique')
        if self.profile_sha256 != _digest(self.identity_payload()):
            raise ValueError('observer profile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('profile_id', None)
        payload.pop('profile_sha256', None)
        return payload

    def is_applicable_to(self, system_kind: DisplaySystemKind) -> bool:
        return system_kind in set(self.applicable_system_kinds)


class ObserverMetamerismEvaluation(BaseModel):
    """A pair-result evaluation (#626 §5).

    Binds reference and DUT spectral states plus the exact observer
    profile.  The result class keeps instrument spectral mismatch,
    standard-observer difference and observer metameric failure
    separate.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['observer-metamerism-1'] = (
        OBSERVER_METAMERISM_AUTHORITY_VERSION
    )
    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    reference_state_id: str = Field(min_length=1)
    reference_state_sha256: str = Field(pattern=_SHA256)
    dut_state_id: str = Field(min_length=1)
    dut_state_sha256: str = Field(pattern=_SHA256)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)
    result_class: MismatchClass = 'unknown'
    metric_value: float | None = Field(default=None, ge=0.0)
    metric_units: str | None = Field(default=None, min_length=1)
    metric_semantics: str | None = Field(default=None, min_length=1)
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'ObserverMetamerismEvaluation':
        if self.metric_value is not None and not isfinite(
            float(self.metric_value)
        ):
            raise ValueError('metric value must be finite')
        if self.metric_value is not None and self.metric_units is None:
            raise ValueError(
                'a metric value requires declared units/semantics'
            )
        if self.metric_value is None and (
            self.result_class == 'observer_metameric_failure'
        ):
            # A failed classification still records why.
            if not self.limitations and self.notes is None:
                raise ValueError(
                    'a metric-less classification must record why'
                )
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError('evaluation limitations must be unique')
        if self.evaluation_sha256 != _digest(self.identity_payload()):
            raise ValueError('evaluation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('evaluation_id', None)
        payload.pop('evaluation_sha256', None)
        return payload


class PerceptualMatchRecord(BaseModel):
    """A controlled human-match record (#626 §7–§9).

    The nominal standard target is bound and immutable; the selected
    perceptual target is a *delta* recorded against it — a user-chosen
    white offset never redefines D65.  Observer identity is a
    reproducibility class only; health/genetic/vision data is never
    retained.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['observer-metamerism-1'] = (
        OBSERVER_METAMERISM_AUTHORITY_VERSION
    )
    match_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    reference_state_id: str = Field(min_length=1)
    dut_state_id: str = Field(min_length=1)
    viewing_geometry_ref: str | None = Field(default=None, min_length=1)
    adaptation_environment: AdaptationEnvironment = 'unknown'
    observer_identity_class: ObserverIdentityClass = 'not_applicable'
    observer_count: int = Field(default=1, ge=1)
    procedure: str | None = Field(default=None, min_length=1)
    nominal_target: ChromaticityPoint
    selected_target: ChromaticityPoint | None = None
    repeatability: float | None = Field(default=None, ge=0.0)
    dispersion: float | None = Field(default=None, ge=0.0)
    outlier_policy: str | None = Field(default=None, min_length=1)
    subjective_notes: str | None = Field(default=None, min_length=1)
    recorded_at_utc: str = Field(min_length=1)
    match_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_match(self) -> 'PerceptualMatchRecord':
        for v in (self.repeatability, self.dispersion):
            if v is not None and not isfinite(float(v)):
                raise ValueError('match dispersion values must be finite')
        if self.observer_count > 1 and (
            self.observer_identity_class
            in ('single_calibrator', 'not_applicable')
        ):
            raise ValueError(
                'a multi-observer record needs a panel/sample identity '
                'class — a single calibrator is never a distribution'
            )
        if self.match_sha256 != _digest(self.identity_payload()):
            raise ValueError('perceptual match hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('match_id', None)
        payload.pop('match_sha256', None)
        return payload


class ObserverMetamerismQualification(BaseModel):
    """Sealed fail-closed verdict for one cross-display matching goal."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['observer-metamerism-1'] = (
        OBSERVER_METAMERISM_AUTHORITY_VERSION
    )
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    goal: MatchGoal
    reference_state_id: str = Field(min_length=1)
    reference_state_sha256: str = Field(pattern=_SHA256)
    dut_state_id: str = Field(min_length=1)
    dut_state_sha256: str = Field(pattern=_SHA256)
    verdict: QualificationVerdict
    evaluation_id: str | None = Field(default=None, min_length=1)
    evaluation_sha256: str | None = Field(default=None, pattern=_SHA256)
    perceptual_match_ids: tuple[str, ...] = ()
    reasons: tuple[ObserverMetamerismReason, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'ObserverMetamerismQualification':
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError('qualification reasons must be unique')
        if len(set(self.perceptual_match_ids)) != len(
            self.perceptual_match_ids
        ):
            raise ValueError('perceptual match refs must be unique')
        if self.verdict == 'verified_within_profile' and (
            self.evaluation_id is None
        ):
            raise ValueError(
                'a verified verdict requires a bound evaluation'
            )
        if self.qualification_sha256 != _digest(
            self.identity_payload()
        ):
            raise ValueError('qualification hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('qualification_id', None)
        payload.pop('qualification_sha256', None)
        return payload


# ---------------------------------------------------------------------------
# Builders (content-sealed construction)
# ---------------------------------------------------------------------------


def _seal(model: type[BaseModel], payload: dict[str, Any], id_field: str, sha_field: str, prefix: str) -> BaseModel:
    probe = model.model_construct(
        **_canon(model, dict(**payload, **{id_field: '', sha_field: ''}))
    )
    sha = _digest(probe.identity_payload())  # type: ignore[attr-defined]
    return model(
        **probe.model_dump(exclude={id_field, sha_field}),
        **{id_field: f'{prefix}{sha}', sha_field: sha},
    )


def build_spectral_state(
    *,
    document_id: str,
    display_ref: str,
    measured_at_utc: str,
    **fields: Any,
) -> DisplaySpectralState:
    return _seal(
        DisplaySpectralState,
        dict(
            document_id=document_id,
            display_ref=display_ref,
            measured_at_utc=measured_at_utc,
            **fields,
        ),
        'spectral_state_id',
        'spectral_state_sha256',
        'dss:',
    )  # type: ignore[return-value]


def build_observer_profile(
    *,
    document_id: str,
    label: str,
    kind: ObserverProfileKind,
    **fields: Any,
) -> ObserverModelProfile:
    return _seal(
        ObserverModelProfile,
        dict(
            document_id=document_id,
            label=label,
            kind=kind,
            **fields,
        ),
        'profile_id',
        'profile_sha256',
        'omp:',
    )  # type: ignore[return-value]


def build_metamerism_evaluation(
    *,
    document_id: str,
    reference_state: DisplaySpectralState,
    dut_state: DisplaySpectralState,
    profile: ObserverModelProfile,
    evaluated_at_utc: str,
    **fields: Any,
) -> ObserverMetamerismEvaluation:
    """Seal a pair evaluation — fail-closed on profile scope.

    A metric computed under a profile that does not apply to either
    display's system kind can never be recorded: the evaluation refuses
    construction rather than carrying false compliance (OMF60).
    """
    for state, role in (
        (reference_state, 'reference'), (dut_state, 'DUT')
    ):
        if not profile.is_applicable_to(state.system_kind):
            raise ValueError(
                f'observer profile {profile.kind} is not applicable to '
                f'{role} system kind {state.system_kind}'
            )
        if not state.spectral_evaluable:
            raise ValueError(
                f'{role} spectral state has no evaluable SPD — '
                'tristimulus-only evidence cannot feed a metric'
            )
    return _seal(
        ObserverMetamerismEvaluation,
        dict(
            document_id=document_id,
            reference_state_id=reference_state.spectral_state_id,
            reference_state_sha256=reference_state.spectral_state_sha256,
            dut_state_id=dut_state.spectral_state_id,
            dut_state_sha256=dut_state.spectral_state_sha256,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            evaluated_at_utc=evaluated_at_utc,
            **fields,
        ),
        'evaluation_id',
        'evaluation_sha256',
        'ome:',
    )  # type: ignore[return-value]


def build_perceptual_match(
    *,
    document_id: str,
    reference_state_id: str,
    dut_state_id: str,
    nominal_target: ChromaticityPoint,
    recorded_at_utc: str,
    **fields: Any,
) -> PerceptualMatchRecord:
    return _seal(
        PerceptualMatchRecord,
        dict(
            document_id=document_id,
            reference_state_id=reference_state_id,
            dut_state_id=dut_state_id,
            nominal_target=nominal_target,
            recorded_at_utc=recorded_at_utc,
            **fields,
        ),
        'match_id',
        'match_sha256',
        'pmr:',
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_observer_metamerism(
    *,
    document_id: str,
    goal: MatchGoal,
    reference_state: DisplaySpectralState,
    dut_state: DisplaySpectralState,
    profile: ObserverModelProfile | None = None,
    evaluation: ObserverMetamerismEvaluation | None = None,
    perceptual_matches: Sequence[PerceptualMatchRecord] = (),
    instrument_mismatch_suspected: bool = False,
    evaluated_at_utc: str,
) -> ObserverMetamerismQualification:
    """Fail-closed observer-metamerism qualification (#626 §13/§14).

    Verdicts:

    - ``insufficient_spectral_evidence`` — either side carries only
      tristimulus coordinates or no SPD (x/y alone is never enough);
    - ``profile_inapplicable`` — the selected profile does not cover the
      display system kinds (e.g. an IEC TS 61966-13 profile applied to
      a reflected projection-screen system);
    - ``instrument_mismatch_explains`` — a meter-correction difference
      already accounts for the perceived delta: classify as instrument
      spectral mismatch, not observer metamerism;
    - ``verified_within_profile`` / ``verified_with_limitations`` — a
      bound pair evaluation exists and supports the goal *within the
      declared profile and observer model* — never a claim of identical
      perception for every human observer;
    - ``unverified_residual_risk`` — standard-observer match exists but
      no observer-metamerism evidence was produced.
    """
    reasons: list[ObserverMetamerismReason] = []
    limitations: list[str] = []
    verdict: QualificationVerdict

    pair_bound = (
        evaluation is not None
        and evaluation.reference_state_sha256
        == reference_state.spectral_state_sha256
        and evaluation.dut_state_sha256
        == dut_state.spectral_state_sha256
        and (
            profile is None
            or evaluation.profile_sha256 == profile.profile_sha256
        )
    )

    matches = tuple(
        m for m in perceptual_matches
        if m.reference_state_id == reference_state.spectral_state_id
        and m.dut_state_id == dut_state.spectral_state_id
    )

    if profile is not None:
        inapplicable = [
            (role, s.system_kind)
            for role, s in (
                ('reference', reference_state),
                ('dut', dut_state),
            )
            if not profile.is_applicable_to(s.system_kind)
        ]
    else:
        inapplicable = []

    same_spd = (
        reference_state.spd is not None
        and dut_state.spd is not None
        and reference_state.spd == dut_state.spd
    )

    if inapplicable:
        verdict = 'profile_inapplicable'
        reasons.append('PROFILE_INAPPLICABLE_TO_SYSTEM_KIND')
        if any(kind == 'projected_reflected' for _, kind in inapplicable):
            reasons.append('PROJECTION_OUT_OF_PROFILE_SCOPE')
    elif not (
        reference_state.spectral_evaluable
        and dut_state.spectral_evaluable
    ):
        verdict = 'insufficient_spectral_evidence'
        reasons.append('INSUFFICIENT_SPECTRAL_EVIDENCE')
        if (
            reference_state.evidence_class == 'tristimulus_only'
            or dut_state.evidence_class == 'tristimulus_only'
        ):
            reasons.append('TRISTIMULUS_ONLY_EVIDENCE')
    elif instrument_mismatch_suspected:
        verdict = 'instrument_mismatch_explains'
        reasons.append('INSTRUMENT_MISMATCH_SUSPECTED')
    elif evaluation is not None and pair_bound:
        verdict = 'verified_within_profile'
        reasons.append('VERIFIED_WITHIN_PROFILE')
        if same_spd:
            # Identical SPDs cannot split under any observer model —
            # the residual risk is proven absent for this pair.
            reasons.append('SAME_SPD_PAIR')
        # Meter correction plus a profile metric never implies identical
        # perception for every human observer — always recorded.
        reasons.append('METER_CORRECTION_NOT_OBSERVER_PROOF')
        if evaluation.result_class == 'instrument_spectral_mismatch':
            reasons.append('INSTRUMENT_MISMATCH_SUSPECTED')
            verdict = 'instrument_mismatch_explains'
        elif evaluation.metric_value is None:
            verdict = 'verified_with_limitations'
            reasons.append('LIMITATIONS_DECLARED')
        if evaluation.limitations:
            verdict = 'verified_with_limitations'
            limitations.extend(evaluation.limitations)
            reasons.append('LIMITATIONS_DECLARED')
    elif evaluation is not None and not pair_bound:
        verdict = 'conflicting_evidence'
        reasons.append('UNPAIRED_SCALAR_REJECTED')
    else:
        verdict = 'unverified_residual_risk'
        reasons.append('METER_CORRECTION_NOT_OBSERVER_PROOF')
        reasons.append('INSUFFICIENT_SPECTRAL_EVIDENCE' if not (
            reference_state.has_spd and dut_state.has_spd
        ) else 'LIMITATIONS_DECLARED')

    if matches:
        reasons.append('PERCEPTUAL_OFFSET_RECORDED')
        if any(
            m.observer_identity_class == 'single_calibrator'
            or m.observer_count == 1
            for m in matches
        ):
            reasons.append('SINGLE_OBSERVER_NOT_UNIVERSAL')
            if verdict == 'verified_within_profile':
                verdict = 'verified_with_limitations'
        if any(m.dispersion is not None for m in matches):
            reasons.append('MULTI_OBSERVER_DISPERSION_PRESENT')
    if goal == 'informational_only':
        reasons.append('GOAL_INFORMATIONAL_ONLY')
        if verdict == 'verified_within_profile':
            verdict = 'verified_with_limitations'
    if profile is not None and profile.limitations:
        limitations.extend(profile.limitations)

    return _seal(
        ObserverMetamerismQualification,
        dict(
            document_id=document_id,
            goal=goal,
            reference_state_id=reference_state.spectral_state_id,
            reference_state_sha256=reference_state.spectral_state_sha256,
            dut_state_id=dut_state.spectral_state_id,
            dut_state_sha256=dut_state.spectral_state_sha256,
            verdict=verdict,
            evaluation_id=(
                None if evaluation is None else evaluation.evaluation_id
            ),
            evaluation_sha256=(
                None if evaluation is None else evaluation.evaluation_sha256
            ),
            perceptual_match_ids=tuple(m.match_id for m in matches),
            reasons=tuple(dict.fromkeys(reasons)),
            limitations=tuple(dict.fromkeys(limitations)),
            evaluated_at_utc=evaluated_at_utc,
        ),
        'qualification_id',
        'qualification_sha256',
        'omq:',
    )  # type: ignore[return-value]


__all__ = [
    'OBSERVER_METAMERISM_AUTHORITY_VERSION',
    'ChromaticityPoint',
    'DisplaySpectralState',
    'MISMATCH_CLASS_LABELS',
    'OBSERVER_SET_LABELS',
    'ObserverMetamerismEvaluation',
    'ObserverMetamerismQualification',
    'ObserverModelProfile',
    'PROFILE_KIND_LABELS',
    'PerceptualMatchRecord',
    'REASON_LABELS',
    'SPD_EVIDENCE_LABELS',
    'SYSTEM_KIND_LABELS',
    'SpdBlock',
    'SpdSample',
    'VERDICT_LABELS',
    'build_metamerism_evaluation',
    'build_observer_profile',
    'build_perceptual_match',
    'build_spectral_state',
    'evaluate_observer_metamerism',
]
