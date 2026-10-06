"""Measurement-chain linearity / overload authority (#695, REV58-MEASCHAIN).

A microphone can be correctly sensitivity-calibrated yet become nonlinear or
overloaded at the actual test SPL — and a preamplifier/interface/ADC can clip
or distort before the recorded samples visibly reach digital full scale.
``samples < 0 dBFS`` therefore never proves the acquisition chain was linear,
and a #611 sensitivity calibration never promotes a chain to
measurement-chain-qualified on its own.

This module makes acquisition-chain linearity a sealed, fail-closed
authority:

- :class:`CadMeasChainLinearityProfile` — the declared acquisition-chain
  graph (microphone capsule → mic internal electronics → external preamp /
  conditioning → pad / gain stage → analog interface input → ADC →
  driver / acquisition software → raw samples) plus the chain's *evidenced*
  linearity envelope: per-band upper-level declarations that keep their exact
  SPL/distortion/frequency semantics, self-noise, and every declared
  dynamic-processing element (AGC / limiter / noise suppression /
  auto-ranging / SRC / HPF) that can silently compress the measurement.
- :class:`CadAcquisitionOverloadObservation` — sealed per-capture evidence:
  observed digital peak and its semantics, the typed overload mechanism
  (analog front-end clip vs ADC numeric full-scale vs ADC internal/spurious
  vs software limiting), which stage is suspected, and an optional
  two-level/alternate-gain linearity check.
- :class:`CadMeasChainQualification` — the fail-closed verdict binding a
  chain profile and a capture observation to the capability flags every
  downstream result must consult.

Composition:

- #611 calibration lifecycle: ``calibration_ref`` pins the sensitivity /
  frequency calibration — it is *distinct* evidence from linearity and never
  refreshes an absent overload qualification.
- #608 stimulus identity: the observation's ``stimulus_ref`` binds the exact
  stimulus whose crest factor / spectrum drove the chain.
- #609 timebase: orthogonal — timing validity is decided there, not here.
- #192 system nonlinearity: DUT distortion claims are gated by the
  ``distortion_attribution`` this authority returns.
- #575 transforms: a downstream transform can downgrade these capability
  flags but never silently upgrade them.
- The older :mod:`cad_input_chain_capability` (#1007) is an unversioned
  in-memory gate prototype; this module is the sealed, schema-registered
  authority and does not reuse it.

Honesty rules baked into the models and the evaluator:

- A clean digital peak never asserts ``no_overload_observed`` for upstream
  analog stages — an explicit mechanism requires evidence.
- An overload/clip *indicator* is useful evidence but is not universally
  equivalent to full-chain linearity proof; conversely an asserted flag
  must stale affected quantitative claims.
- Hidden dynamic processing that cannot be disabled or characterized makes
  the affected claims ``nonlinear_measurement_ineligible`` — never measure
  loudspeaker compression through a chain that is itself level-compressing.
- Microphone upper-level capability keeps its exact definition (3% THD,
  1% THD, clip point, peak vs RMS, sinusoidal vs broadband) — the module
  never normalizes it into one unqualified ``max_spl_db``.
- Linearity evidence is frequency-dependent: a qualification around 1 kHz
  does not extrapolate into 20–40 Hz high-pressure or ultrasonic claims.
- A measured SPL plateau is never auto-attributed to loudspeaker
  compression — capsule/preamp/ADC saturation remain distinct hypotheses
  until isolated.

Literature / standards basis
----------------------------
- IEC 61094-4:1995 (working-standard measurement microphones): separates
  sensitivity calibration from the *upper limit of microphone dynamic
  range* (SPL at 3% THD over 160 Hz–1 kHz) and the *linearity range of
  sensitivity level* — sensitivity calibration is not linearity evidence.
- IEC 61672-1/-2/-3 (sound-level meters): level linearity, overload
  behaviour, pattern evaluation and periodic verification are independent
  performance properties — calibration, linear range and overload
  indication are separate evidence classes.
- IEC 60268-4:2018 (sound-system microphones): microphone dynamic range is
  a first-class measurand, not a consequence of sensitivity calibration.
- AES E-Library, *Practical Considerations on Overload-Distortion of
  Analog-to-Digital Converters* (id=6851): ADC overload can produce
  non-harmonic/spurious components — an overloaded converter contaminates
  distortion results in ways misattributable to the DUT.
- KLIPPEL TBM documentation (docs.klippel.de tone-burst measurement):
  high-level acoustic tests can be limited by amplifier or *microphone*
  saturation — a measured peak-SPL plateau is a measurement-system failure
  mode until proven otherwise.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


MEASCHAIN_AUTHORITY_SCHEMA_VERSION = 'measchain-mcl-1'
MEASCHAIN_EVALUATION_VERSION = 'measchain-mcl-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#695)
# ---------------------------------------------------------------------------

AcquisitionStageKind = Literal[
    'microphone_capsule',
    'microphone_internal_electronics',
    'external_preamp_conditioning',
    'pad_gain_stage',
    'analog_interface_input',
    'adc',
    'driver_acquisition_software',
    'unknown_internal_stage',
]

#: Canonical acoustic-field → raw-sample ordering. A declared chain must be
#: non-decreasing in this order so "which stage clips first" stays legible;
#: ``unknown_internal_stage`` may sit anywhere (opaque internals stay
#: honest instead of being invented).
_STAGE_ORDER: dict[AcquisitionStageKind, int] = {
    'microphone_capsule': 0,
    'microphone_internal_electronics': 1,
    'external_preamp_conditioning': 2,
    'pad_gain_stage': 3,
    'analog_interface_input': 4,
    'adc': 5,
    'driver_acquisition_software': 6,
    'unknown_internal_stage': 7,
}

StageOverloadIndicator = Literal[
    'clear',
    'asserted',
    'unavailable',
    'unknown',
]

UpperLevelThresholdKind = Literal[
    'thd_3pct',
    'thd_1pct',
    'thd_other_pct',
    'clip_point',
    'manufacturer_max_spl',
    'unknown',
]

LevelSemantics = Literal['peak', 'rms', 'unknown']

SignalClass = Literal['sinusoidal', 'broadband', 'impulse', 'unknown']

LinearityEvidenceBasis = Literal[
    'manufacturer_specification',
    'laboratory_measurement',
    'standard_qualification',
    'field_two_level_check',
    'assumed',
    'unknown',
]

#: Evidence bases that can carry a numeric upper-level / linearity claim.
STRONG_LINEARITY_BASES: frozenset[LinearityEvidenceBasis] = frozenset(
    {
        'manufacturer_specification',
        'laboratory_measurement',
        'standard_qualification',
        'field_two_level_check',
    }
)

OverloadMechanism = Literal[
    'analog_front_end_clip',
    'adc_numeric_full_scale',
    'adc_internal_overload_spurious',
    'software_dsp_limiting',
    'no_overload_observed',
    'unknown',
]

#: Mechanisms that are confirmed overload states (not merely suspicion).
_CONFIRMED_OVERLOAD: frozenset[OverloadMechanism] = frozenset(
    {
        'analog_front_end_clip',
        'adc_numeric_full_scale',
        'adc_internal_overload_spurious',
        'software_dsp_limiting',
    }
)

DynamicProcessingKind = Literal[
    'agc',
    'limiter',
    'noise_suppression',
    'auto_range_switching',
    'sample_rate_conversion',
    'high_pass_filter',
    'other',
    'unknown_present',
]

DynamicProcessingState = Literal[
    'disabled',
    'enabled',
    'cannot_disable',
    'unknown',
]

#: Processing states that make nonlinear/level-sensitive claims ineligible:
#: the processing is either running or cannot be shown to be off.
_LEVEL_COMPRESSING_STATES: frozenset[DynamicProcessingState] = frozenset(
    {'enabled', 'cannot_disable'}
)

TwoLevelCheckVerdict = Literal[
    'chain_linear_within_tested_range',
    'dut_or_chain_nonlinearity_present',
    'chain_gain_state_dependent',
    'overload_suspected',
    'indeterminate',
    'not_performed',
]

ChainQualificationState = Literal[
    'chain_qualified_within_declared_range',
    'chain_qualified_with_limitations',
    'overload_suspected',
    'overload_observed',
    'nonlinear_measurement_ineligible',
    'unqualified_insufficient_evidence',
]

DistortionAttribution = Literal[
    'dut_distortion_eligible',
    'chain_distortion_limited',
    'chain_overload_suspected',
    'chain_overload_confirmed',
    'indeterminate',
]

RequestedMeasurementClass = Literal[
    'standard_fr_ir',
    'high_level_spl',
    'dut_nonlinear',
    'peak_transient',
]

#: Requested claim classes that require a *proven* linear/upper-level
#: envelope rather than merely an absence of overload flags.
_HIGH_LEVEL_CLASSES: frozenset[RequestedMeasurementClass] = frozenset(
    {'high_level_spl', 'dut_nonlinear', 'peak_transient'}
)

MeasChainCapability = Literal[
    'absolute_spl_valid',
    'linear_magnitude_valid',
    'phase_valid',
    'high_level_spl_valid',
    'dut_thd_valid',
    'dut_compression_valid',
    'peak_transient_valid',
    'chain_overload_not_excluded',
]

ALL_MEASCHAIN_CAPABILITIES: tuple[MeasChainCapability, ...] = (
    'absolute_spl_valid',
    'linear_magnitude_valid',
    'phase_valid',
    'high_level_spl_valid',
    'dut_thd_valid',
    'dut_compression_valid',
    'peak_transient_valid',
    'chain_overload_not_excluded',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadAcquisitionStage(BaseModel):
    """One stage of the declared acquisition chain, in signal order.

    Only quantities with evidence are populated — an ``unknown`` headroom
    is honest, an invented one is not. ``stage_gain_db``/``pad_db`` record
    the *actual* settings used (the exact gain state is part of the
    authority, not a footnote), and ``overload_indicator`` records what the
    stage reported — a stage with no indicator is ``unavailable``, never
    silently ``clear``.
    """

    model_config = ConfigDict(frozen=True)

    stage_kind: AcquisitionStageKind
    device_identity: str | None = None
    model_or_label: str | None = None
    gain_db: float | None = None
    pad_db: float | None = None
    max_input_level_dbfs_or_spl: float | None = None
    max_input_level_unit: str | None = None
    output_headroom_db: float | None = None
    supply_state: str | None = None
    overload_indicator: StageOverloadIndicator = 'unknown'
    notes: str | None = None

    @model_validator(mode='after')
    def valid_stage(self) -> 'CadAcquisitionStage':
        for label, value in (
            ('gain_db', self.gain_db),
            ('pad_db', self.pad_db),
            ('max_input_level_dbfs_or_spl', self.max_input_level_dbfs_or_spl),
            ('output_headroom_db', self.output_headroom_db),
        ):
            if value is not None:
                _require_finite(value, f'acquisition stage {label}')
        if self.pad_db is not None and self.pad_db < 0:
            raise ValueError('acquisition stage pad_db must be non-negative')
        if (
            self.max_input_level_dbfs_or_spl is not None
            and self.max_input_level_unit is None
        ):
            raise ValueError(
                'a stage maximum input level requires its unit — an '
                'unlabeled number silently mixes dBFS, dBu and dB SPL'
            )
        return self


class CadUpperLevelSpec(BaseModel):
    """One declared upper-level / overload boundary for a chain or stage.

    Keeps the exact semantics of the limit: which distortion criterion
    (``threshold_kind`` + ``thd_percent``), peak vs RMS, the signal class
    the figure applies to, the frequency band it was established over, and
    the evidence basis. A ``manufacturer_max_spl`` with no band is honest —
    its ``band_low_hz``/``band_high_hz`` stay ``None`` and the evaluator
    refuses to extend it into a band it never covered.
    """

    model_config = ConfigDict(frozen=True)

    applies_to_stage_index: int | None = None
    level_db: float
    level_unit: Literal['db_spl', 'dbfs', 'dbu', 'dbv', 'other']
    threshold_kind: UpperLevelThresholdKind
    thd_percent: float | None = None
    level_semantics: LevelSemantics = 'unknown'
    signal_class: SignalClass = 'unknown'
    band_low_hz: float | None = None
    band_high_hz: float | None = None
    evidence_basis: LinearityEvidenceBasis = 'unknown'
    evidence_source: str | None = None
    uncertainty_db: float | None = None

    @model_validator(mode='after')
    def valid_spec(self) -> 'CadUpperLevelSpec':
        _require_finite(self.level_db, 'upper-level spec level_db')
        if self.threshold_kind == 'thd_other_pct' and (
            self.thd_percent is None
        ):
            raise ValueError(
                'thd_other_pct requires the exact distortion percentage — '
                'an unnamed THD criterion is not a limit definition'
            )
        if self.thd_percent is not None:
            _require_finite(self.thd_percent, 'upper-level thd_percent')
            if not (0 < self.thd_percent < 100):
                raise ValueError('thd_percent must be within (0, 100)')
        if self.thd_percent is not None and self.threshold_kind in (
            'clip_point',
            'unknown',
        ):
            raise ValueError(
                'thd_percent is meaningless under a non-THD threshold kind'
            )
        for label, value in (
            ('band_low_hz', self.band_low_hz),
            ('band_high_hz', self.band_high_hz),
        ):
            if value is not None:
                _require_finite(value, f'upper-level {label}')
                if value <= 0:
                    raise ValueError(f'upper-level {label} must be positive')
        if (
            self.band_low_hz is not None
            and self.band_high_hz is not None
            and self.band_high_hz <= self.band_low_hz
        ):
            raise ValueError('upper-level band must be ascending')
        if self.uncertainty_db is not None:
            _require_finite(self.uncertainty_db, 'upper-level uncertainty_db')
            if self.uncertainty_db < 0:
                raise ValueError('uncertainty_db must be non-negative')
        return self

    def covers_frequency_hz(self, hz: float) -> bool:
        """Whether this spec's declared band covers ``hz``.

        A spec with no declared band covers nothing beyond honesty: its
        limit was established somewhere unstated, so it can never be
        auto-extended into the queried band.
        """
        if self.band_low_hz is None or self.band_high_hz is None:
            return False
        return self.band_low_hz <= hz <= self.band_high_hz


class CadLinearityBand(BaseModel):
    """A frequency band over which the chain has linearity evidence."""

    model_config = ConfigDict(frozen=True)

    band_low_hz: float
    band_high_hz: float
    max_linear_level_db: float | None = None
    level_unit: str | None = None
    evidence_basis: LinearityEvidenceBasis
    evidence_source: str | None = None
    uncertainty_db: float | None = None

    @model_validator(mode='after')
    def valid_band(self) -> 'CadLinearityBand':
        for label, value in (
            ('band_low_hz', self.band_low_hz),
            ('band_high_hz', self.band_high_hz),
            ('max_linear_level_db', self.max_linear_level_db),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'linearity band {label}')
        if self.band_low_hz <= 0 or self.band_high_hz <= 0:
            raise ValueError('linearity band edges must be positive')
        if self.band_high_hz <= self.band_low_hz:
            raise ValueError('linearity band must be ascending')
        if self.uncertainty_db is not None and self.uncertainty_db < 0:
            raise ValueError('linearity band uncertainty must be >= 0')
        return self

    def covers_frequency_hz(self, hz: float) -> bool:
        return self.band_low_hz <= hz <= self.band_high_hz


class CadDynamicProcessingBlock(BaseModel):
    """One declared dynamic-processing element in the chain.

    ``state`` is honest: ``cannot_disable``/``unknown`` are explicit
    limitations, not defaults to ignore. ``characterized`` records whether
    the processing's effect on measurements is actually quantified — an
    enabled-but-characterized high-pass differs from a hidden limiter.
    """

    model_config = ConfigDict(frozen=True)

    processing_kind: DynamicProcessingKind
    stage_index: int | None = None
    state: DynamicProcessingState
    characterized: bool = False
    detail: str | None = None


class CadTwoLevelCheck(BaseModel):
    """A two-level / alternate-gain linearity diagnostic.

    Same DUT/state/geometry/stimulus captured at two or more levels or
    gain settings, normalized by the expected linear scaling, with the
    residual compared. The verdict is declared by whoever ran the check —
    the diagnostic alone cannot isolate DUT vs chain nonlinearity
    (``dut_or_chain_nonlinearity_present``), so the verdict vocabulary
    keeps that ambiguity explicit.
    """

    model_config = ConfigDict(frozen=True)

    levels_db: tuple[float, ...] = ()
    gain_settings: tuple[str, ...] = ()
    normalized_residual_db: float | None = None
    reference_capture_ref: AuthorityRef | None = None
    verdict: TwoLevelCheckVerdict = 'not_performed'

    @model_validator(mode='after')
    def valid_check(self) -> 'CadTwoLevelCheck':
        if len(self.levels_db) < 2:
            raise ValueError(
                'a two-level check requires at least two levels'
            )
        for value in self.levels_db:
            _require_finite(value, 'two-level levels_db')
        if self.normalized_residual_db is not None:
            _require_finite(
                self.normalized_residual_db,
                'two-level normalized_residual_db',
            )
            if self.normalized_residual_db < 0:
                raise ValueError('normalized_residual_db must be >= 0')
        if self.verdict == 'not_performed':
            raise ValueError(
                'a CadTwoLevelCheck with verdict not_performed must not be '
                'constructed — omit the block instead of recording a '
                'check that never ran'
            )
        if self.reference_capture_ref is not None and (
            self.reference_capture_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the reference capture pin must carry its sha256'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadMeasChainLinearityProfile(BaseModel):
    """Sealed acquisition-chain linearity authority for one measurement
    chain.

    ``stages`` is the ordered acoustic→digital graph; ``upper_level_specs``
    and ``linearity_bands`` carry the *evidenced* overload/linear envelope
    with its exact semantics; ``dynamic_processing`` declares every AGC /
    limiter / DSP element whose state can silently compress a measurement;
    ``calibration_ref`` optionally pins the #611 sensitivity calibration —
    present as provenance, never as linearity proof.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chain_label: str = Field(min_length=1)
    stages: tuple[CadAcquisitionStage, ...]
    upper_level_specs: tuple[CadUpperLevelSpec, ...] = ()
    linearity_bands: tuple[CadLinearityBand, ...] = ()
    dynamic_processing: tuple[CadDynamicProcessingBlock, ...] = ()
    self_noise_floor_db: float | None = None
    self_noise_floor_unit: str | None = None
    calibration_ref: AuthorityRef | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadMeasChainLinearityProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if not self.stages:
            raise ValueError(
                'a measurement-chain profile requires at least one stage'
            )
        order = [_STAGE_ORDER[s.stage_kind] for s in self.stages]
        if order != sorted(order):
            raise ValueError(
                'acquisition stages must follow the canonical '
                'acoustic→digital order — an unordered chain hides which '
                'stage is closest to overload'
            )
        if self.self_noise_floor_db is not None:
            _require_finite(
                self.self_noise_floor_db, 'self_noise_floor_db'
            )
            if self.self_noise_floor_unit is None:
                raise ValueError(
                    'self-noise floor requires its unit'
                )
        for index, spec in enumerate(self.upper_level_specs):
            if spec.applies_to_stage_index is not None and not (
                0 <= spec.applies_to_stage_index < len(self.stages)
            ):
                raise ValueError(
                    f'upper-level spec {index} points outside the stage '
                    'graph'
                )
        for index, block in enumerate(self.dynamic_processing):
            if block.stage_index is not None and not (
                0 <= block.stage_index < len(self.stages)
            ):
                raise ValueError(
                    f'dynamic-processing block {index} points outside '
                    'the stage graph'
                )
        if self.calibration_ref is not None and (
            self.calibration_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #611 calibration pin must carry its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('chain profile hash mismatch')
        if self.profile_id != _semantic_id('mchain', expected):
            raise ValueError('chain profile id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'chain_label': self.chain_label,
            'stages': [
                s.model_dump(mode='json') for s in self.stages
            ],
            'upper_level_specs': [
                s.model_dump(mode='json') for s in self.upper_level_specs
            ],
            'linearity_bands': [
                b.model_dump(mode='json') for b in self.linearity_bands
            ],
            'dynamic_processing': [
                p.model_dump(mode='json') for p in self.dynamic_processing
            ],
            'self_noise_floor_db': self.self_noise_floor_db,
            'self_noise_floor_unit': self.self_noise_floor_unit,
            'calibration_ref': (
                self.calibration_ref.model_dump(mode='json')
                if self.calibration_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def active_dynamic_processing(
        self,
    ) -> tuple[CadDynamicProcessingBlock, ...]:
        """Processing elements that are running or cannot be shown off."""
        return tuple(
            block
            for block in self.dynamic_processing
            if block.state in _LEVEL_COMPRESSING_STATES
        )

    def linearity_evidence_at(self, hz: float) -> tuple[CadLinearityBand, ...]:
        return tuple(
            band
            for band in self.linearity_bands
            if band.covers_frequency_hz(hz)
        )


def measchain_profile_binding(
    profile: CadMeasChainLinearityProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='measchain_linearity_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class CadAcquisitionOverloadObservation(BaseModel):
    """Sealed per-capture overload / level evidence.

    ``sample_peak_dbfs`` is the observed digital peak *with its level
    semantics*; ``overload_mechanism`` is typed — analog front-end clip,
    ADC numeric full-scale, ADC internal/spurious overload and software/DSP
    limiting are never one generic "clipped" label. ``overload_stage_index``
    names the suspected stage when evidence localizes it; ``two_level_check``
    optionally carries the gain-change diagnostic.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chain_ref: AuthorityRef
    capture_ref: AuthorityRef | None = None
    stimulus_ref: AuthorityRef | None = None
    observed_at_utc: str | None = None
    sample_peak_dbfs: float | None = None
    peak_semantics: LevelSemantics = 'unknown'
    overload_mechanism: OverloadMechanism = 'unknown'
    overload_stage_index: int | None = None
    overload_indicator_state: StageOverloadIndicator = 'unknown'
    stage_reported_overload: bool = False
    measured_thd_percent: float | None = None
    noise_floor_margin_db: float | None = None
    two_level_check: CadTwoLevelCheck | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadAcquisitionOverloadObservation':
        _require_iso8601(
            self.declared_at_utc, 'observation declared_at_utc'
        )
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        for label, value in (
            ('sample_peak_dbfs', self.sample_peak_dbfs),
            ('measured_thd_percent', self.measured_thd_percent),
            ('noise_floor_margin_db', self.noise_floor_margin_db),
        ):
            if value is not None:
                _require_finite(value, f'observation {label}')
        if self.measured_thd_percent is not None and (
            self.measured_thd_percent < 0
        ):
            raise ValueError('measured_thd_percent must be non-negative')
        # A clean digital peak is never evidence that upstream analog
        # stages stayed linear — 'no_overload_observed' requires either an
        # explicit clear indicator or stronger staged evidence.
        if (
            self.overload_mechanism == 'no_overload_observed'
            and self.overload_indicator_state == 'unknown'
            and self.overload_stage_index is None
            and not self.stage_reported_overload
            and self.sample_peak_dbfs is None
        ):
            raise ValueError(
                'no_overload_observed requires some evidence — a bare '
                'assertion with no peak reading, indicator state or stage '
                'localization is not an overload verdict'
            )
        if self.overload_mechanism in _CONFIRMED_OVERLOAD and (
            self.overload_indicator_state in ('unavailable', 'unknown')
            and self.overload_stage_index is None
            and self.sample_peak_dbfs is None
            and self.two_level_check is None
        ):
            raise ValueError(
                'a confirmed overload mechanism requires localization '
                'evidence (stage index, indicator, peak reading or a '
                'two-level check) — never a bare mechanism label'
            )
        if self.capture_ref is not None and (
            self.capture_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the capture pin must carry its sha256'
            )
        if self.stimulus_ref is not None and (
            self.stimulus_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #608 stimulus pin must carry its sha256'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('observation hash mismatch')
        if self.observation_id != _semantic_id('mclobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'chain_ref': self.chain_ref.model_dump(mode='json'),
            'capture_ref': (
                self.capture_ref.model_dump(mode='json')
                if self.capture_ref is not None
                else None
            ),
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None
                else None
            ),
            'observed_at_utc': self.observed_at_utc,
            'sample_peak_dbfs': self.sample_peak_dbfs,
            'peak_semantics': self.peak_semantics,
            'overload_mechanism': self.overload_mechanism,
            'overload_stage_index': self.overload_stage_index,
            'overload_indicator_state': self.overload_indicator_state,
            'stage_reported_overload': self.stage_reported_overload,
            'measured_thd_percent': self.measured_thd_percent,
            'noise_floor_margin_db': self.noise_floor_margin_db,
            'two_level_check': (
                self.two_level_check.model_dump(mode='json')
                if self.two_level_check is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def measchain_observation_binding(
    observation: CadAcquisitionOverloadObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='measchain_overload_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


class CadMeasChainQualification(BaseModel):
    """Sealed fail-closed verdict for one chain × capture × request.

    Every assessment reports all eight capability flags — an omitted flag
    would silently read as "fine". ``chain_overload_not_excluded`` is the
    honest adverse flag: ``valid`` means overload genuinely cannot be
    excluded (a limitation), ``invalid`` means it can.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chain_ref: AuthorityRef
    observation_ref: AuthorityRef | None = None
    requested_class: RequestedMeasurementClass
    requested_band_low_hz: float | None = None
    requested_band_high_hz: float | None = None
    requested_level_db: float | None = None
    state: ChainQualificationState
    distortion_attribution: DistortionAttribution
    capabilities: tuple[tuple[MeasChainCapability, CapabilityState], ...]
    limiting_stage_index: int | None = None
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadMeasChainQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.chain_ref.ref_sha256 is None:
            raise ValueError('qualification must pin the chain sha256')
        if self.observation_ref is not None and (
            self.observation_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the observation pin must carry its sha256'
            )
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_MEASCHAIN_CAPABILITIES):
            raise ValueError(
                'a chain qualification must report every capability — '
                'an omitted flag would silently read as valid'
            )
        if len(self.capabilities) != len(set(ALL_MEASCHAIN_CAPABILITIES)):
            raise ValueError('duplicate capability entries')
        for label, value in (
            ('requested_band_low_hz', self.requested_band_low_hz),
            ('requested_band_high_hz', self.requested_band_high_hz),
            ('requested_level_db', self.requested_level_db),
        ):
            if value is not None:
                _require_finite(value, f'qualification {label}')
        if (
            self.requested_band_low_hz is not None
            and self.requested_band_high_hz is not None
            and self.requested_band_high_hz <= self.requested_band_low_hz
        ):
            raise ValueError('requested band must be ascending')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('mclqual', expected):
            raise ValueError(
                'qualification id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'chain_ref': self.chain_ref.model_dump(mode='json'),
            'observation_ref': (
                self.observation_ref.model_dump(mode='json')
                if self.observation_ref is not None
                else None
            ),
            'requested_class': self.requested_class,
            'requested_band_low_hz': self.requested_band_low_hz,
            'requested_band_high_hz': self.requested_band_high_hz,
            'requested_level_db': self.requested_level_db,
            'state': self.state,
            'distortion_attribution': self.distortion_attribution,
            'capabilities': [list(item) for item in self.capabilities],
            'limiting_stage_index': self.limiting_stage_index,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: MeasChainCapability
    ) -> CapabilityState:
        return dict(self.capabilities)[capability]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_measchain_profile(
    *,
    document_id: str,
    chain_label: str,
    stages: tuple[CadAcquisitionStage, ...] | list[CadAcquisitionStage],
    upper_level_specs: tuple[CadUpperLevelSpec, ...]
    | list[CadUpperLevelSpec] = (),
    linearity_bands: tuple[CadLinearityBand, ...]
    | list[CadLinearityBand] = (),
    dynamic_processing: tuple[CadDynamicProcessingBlock, ...]
    | list[CadDynamicProcessingBlock] = (),
    self_noise_floor_db: float | None = None,
    self_noise_floor_unit: str | None = None,
    calibration_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadMeasChainLinearityProfile:
    """Seal a measurement-chain linearity profile."""
    payload = dict(
        document_id=document_id,
        chain_label=chain_label,
        stages=tuple(stages),
        upper_level_specs=tuple(upper_level_specs),
        linearity_bands=tuple(linearity_bands),
        dynamic_processing=tuple(dynamic_processing),
        self_noise_floor_db=self_noise_floor_db,
        self_noise_floor_unit=self_noise_floor_unit,
        calibration_ref=calibration_ref,
        authority_version=MEASCHAIN_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadMeasChainLinearityProfile, payload,
        'profile_id', 'profile_sha256', 'mchain',
    )


def build_overload_observation(
    *,
    document_id: str,
    chain_ref: AuthorityRef | CadMeasChainLinearityProfile,
    capture_ref: AuthorityRef | None = None,
    stimulus_ref: AuthorityRef | None = None,
    observed_at_utc: str | None = None,
    sample_peak_dbfs: float | None = None,
    peak_semantics: LevelSemantics = 'unknown',
    overload_mechanism: OverloadMechanism = 'unknown',
    overload_stage_index: int | None = None,
    overload_indicator_state: StageOverloadIndicator = 'unknown',
    stage_reported_overload: bool = False,
    measured_thd_percent: float | None = None,
    noise_floor_margin_db: float | None = None,
    two_level_check: CadTwoLevelCheck | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadAcquisitionOverloadObservation:
    """Seal a per-capture overload / level observation."""
    if isinstance(chain_ref, CadMeasChainLinearityProfile):
        chain_ref = measchain_profile_binding(chain_ref)
    payload = dict(
        document_id=document_id,
        chain_ref=chain_ref,
        capture_ref=capture_ref,
        stimulus_ref=stimulus_ref,
        observed_at_utc=observed_at_utc,
        sample_peak_dbfs=sample_peak_dbfs,
        peak_semantics=peak_semantics,
        overload_mechanism=overload_mechanism,
        overload_stage_index=overload_stage_index,
        overload_indicator_state=overload_indicator_state,
        stage_reported_overload=stage_reported_overload,
        measured_thd_percent=measured_thd_percent,
        noise_floor_margin_db=noise_floor_margin_db,
        two_level_check=two_level_check,
        authority_version=MEASCHAIN_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadAcquisitionOverloadObservation, payload,
        'observation_id', 'observation_sha256', 'mclobs',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _band_covers_request(
    band_low: float, band_high: float,
    requested_low: float | None, requested_high: float | None,
) -> bool:
    """Whether one declared band covers the requested band.

    A request with no declared band can only be checked against the union
    of bands — handled by the caller via per-edge coverage.
    """
    low = requested_low if requested_low is not None else band_low
    high = requested_high if requested_high is not None else band_high
    return band_low <= low and high <= band_high


def _evidenced_envelope_at(
    profile: CadMeasChainLinearityProfile, hz: float
) -> tuple[CadUpperLevelSpec, ...]:
    """Upper-level specs whose declared band covers ``hz``."""
    return tuple(
        spec
        for spec in profile.upper_level_specs
        if spec.covers_frequency_hz(hz)
    )


def evaluate_measchain_qualification(
    *,
    document_id: str,
    profile: CadMeasChainLinearityProfile,
    observation: CadAcquisitionOverloadObservation | None = None,
    requested_class: RequestedMeasurementClass = 'standard_fr_ir',
    requested_band_low_hz: float | None = None,
    requested_band_high_hz: float | None = None,
    requested_level_db: float | None = None,
    evaluated_at_utc: str | None = None,
) -> CadMeasChainQualification:
    """Fail-closed chain-linearity verdict for one measurement request.

    Derivation order matters: confirmed overload evidence beats suspicion,
    suspicion beats a clean bill, and an absence of linearity *evidence*
    is never an absence of overload. ``requested_level_db`` is compared
    only against upper-level specs and linearity bands whose declared
    frequency coverage contains it — a 1 kHz qualification never extends
    into a 30 Hz high-pressure claim.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    caps: dict[MeasChainCapability, CapabilityState] = {}

    active_processing = profile.active_dynamic_processing()
    unknown_processing = any(
        block.state == 'unknown' for block in profile.dynamic_processing
    )
    mechanism = (
        observation.overload_mechanism
        if observation is not None
        else 'unknown'
    )
    overload_confirmed = mechanism in _CONFIRMED_OVERLOAD or (
        observation is not None and observation.stage_reported_overload
    )
    indicator_asserted = (
        observation is not None
        and observation.overload_indicator_state == 'asserted'
    )
    indicator_clear = (
        observation is not None
        and observation.overload_indicator_state == 'clear'
    )
    overload_suspected = (
        not overload_confirmed
        and (
            indicator_asserted
            or (
                observation is not None
                and observation.two_level_check is not None
                and observation.two_level_check.verdict
                in ('overload_suspected', 'dut_or_chain_nonlinearity_present')
            )
        )
    )

    # --- envelope coverage ------------------------------------------------
    edges: tuple[float, ...] = tuple(
        edge
        for edge in (requested_band_low_hz, requested_band_high_hz)
        if edge is not None
    )
    spec_covered = False
    spec_limit_db: float | None = None
    band_covered = False
    band_limit_db: float | None = None
    if edges:
        for edge in edges:
            specs = _evidenced_envelope_at(profile, edge)
            strong = [
                s for s in specs if s.evidence_basis in STRONG_LINEARITY_BASES
            ]
            if strong:
                spec_covered = True
                best = max(s.level_db for s in strong)
                spec_limit_db = (
                    best
                    if spec_limit_db is None
                    else min(spec_limit_db, best)
                )
            bands = profile.linearity_evidence_at(edge)
            strong_bands = [
                b
                for b in bands
                if b.evidence_basis in STRONG_LINEARITY_BASES
            ]
            if strong_bands:
                band_covered = True
                limits = [
                    b.max_linear_level_db
                    for b in strong_bands
                    if b.max_linear_level_db is not None
                ]
                if limits:
                    best = max(limits)
                    band_limit_db = (
                        best
                        if band_limit_db is None
                        else min(band_limit_db, best)
                    )
    envelope_covered = spec_covered or band_covered
    envelope_limit_db = spec_limit_db
    if band_limit_db is not None:
        envelope_limit_db = (
            band_limit_db
            if envelope_limit_db is None
            else min(envelope_limit_db, band_limit_db)
        )

    level_within = None
    if requested_level_db is not None and envelope_limit_db is not None:
        level_within = requested_level_db <= envelope_limit_db

    # --- state derivation ---------------------------------------------------
    if overload_confirmed:
        state = 'overload_observed'
        reasons.append(
            f'confirmed overload mechanism: {mechanism}'
        )
    elif active_processing and requested_class in _HIGH_LEVEL_CLASSES:
        state = 'nonlinear_measurement_ineligible'
        kinds = '/'.join(
            sorted({b.processing_kind for b in active_processing})
        )
        reasons.append(
            f'chain carries enabled/un-disableable dynamic processing '
            f'({kinds}) — never measure loudspeaker compression through a '
            'chain that is itself level-compressing'
        )
    elif overload_suspected:
        state = 'overload_suspected'
        reasons.append('overload suspected (indicator/two-level evidence)')
    elif requested_class in _HIGH_LEVEL_CLASSES and not edges:
        state = 'unqualified_insufficient_evidence'
        reasons.append(
            'high-level/nonlinear claims require a declared frequency band '
            'to check the linearity envelope against'
        )
    elif requested_class in _HIGH_LEVEL_CLASSES and not envelope_covered:
        state = 'unqualified_insufficient_evidence'
        reasons.append(
            'no strong linearity/upper-level evidence covers the '
            'requested band — a sensitivity calibration is not an '
            'overload qualification'
        )
    elif requested_class in _HIGH_LEVEL_CLASSES and level_within is False:
        state = 'overload_suspected'
        reasons.append(
            f'requested level {requested_level_db} exceeds the evidenced '
            f'envelope {envelope_limit_db}'
        )
    elif requested_class in _HIGH_LEVEL_CLASSES and level_within is None:
        state = 'unqualified_insufficient_evidence'
        reasons.append(
            'the evidenced envelope carries no comparable level figure '
            'for the requested band'
        )
    elif (
        observation is not None
        and observation.two_level_check is not None
        and observation.two_level_check.verdict
        == 'chain_linear_within_tested_range'
    ):
        state = 'chain_qualified_within_declared_range'
    elif envelope_covered or (
        observation is not None
        and mechanism == 'no_overload_observed'
        and indicator_clear
    ):
        state = (
            'chain_qualified_within_declared_range'
            if envelope_covered
            else 'chain_qualified_with_limitations'
        )
    else:
        state = 'chain_qualified_with_limitations'
        reasons.append(
            'no overload observed, but the evidenced linearity envelope '
            'does not cover the whole request'
        )

    # --- capability flags ---------------------------------------------------
    if state == 'overload_observed':
        caps.update({
            'absolute_spl_valid': 'invalid',
            'linear_magnitude_valid': 'invalid',
            'phase_valid': 'invalid',
            'high_level_spl_valid': 'invalid',
            'dut_thd_valid': 'invalid',
            'dut_compression_valid': 'invalid',
            'peak_transient_valid': 'invalid',
            'chain_overload_not_excluded': 'valid',
        })
    elif state == 'nonlinear_measurement_ineligible':
        caps.update({
            'absolute_spl_valid': 'limited',
            'linear_magnitude_valid': 'limited',
            'phase_valid': 'limited',
            'high_level_spl_valid': 'invalid',
            'dut_thd_valid': 'invalid',
            'dut_compression_valid': 'invalid',
            'peak_transient_valid': 'invalid',
            'chain_overload_not_excluded': 'valid',
        })
    elif state == 'overload_suspected':
        caps.update({
            'absolute_spl_valid': 'limited',
            'linear_magnitude_valid': 'limited',
            'phase_valid': 'limited',
            'high_level_spl_valid': 'invalid',
            'dut_thd_valid': 'invalid',
            'dut_compression_valid': 'invalid',
            'peak_transient_valid': 'limited',
            'chain_overload_not_excluded': 'valid',
        })
    elif state == 'unqualified_insufficient_evidence':
        high = requested_class in _HIGH_LEVEL_CLASSES
        caps.update({
            'absolute_spl_valid': (
                'valid' if profile.calibration_ref is not None else 'unknown'
            ),
            'linear_magnitude_valid': 'limited',
            'phase_valid': 'unknown',
            'high_level_spl_valid': 'invalid' if high else 'unknown',
            'dut_thd_valid': 'invalid' if high else 'unknown',
            'dut_compression_valid': 'invalid' if high else 'unknown',
            'peak_transient_valid': 'unknown',
            'chain_overload_not_excluded': 'valid',
        })
    else:
        # Qualified (with or without limitations).
        overload_free = (
            observation is not None
            and mechanism == 'no_overload_observed'
            and indicator_clear
        )
        caps['absolute_spl_valid'] = (
            'valid'
            if profile.calibration_ref is not None
            else 'limited'
        )
        caps['linear_magnitude_valid'] = (
            'valid' if not unknown_processing else 'limited'
        )
        caps['phase_valid'] = (
            'valid'
            if not (active_processing or unknown_processing)
            else 'limited'
        )
        if requested_class in _HIGH_LEVEL_CLASSES:
            covered_strong = envelope_covered and level_within is not False
            caps['high_level_spl_valid'] = (
                'valid' if covered_strong else 'limited'
            )
            caps['dut_thd_valid'] = caps['high_level_spl_valid']
            caps['dut_compression_valid'] = caps['high_level_spl_valid']
        else:
            caps['high_level_spl_valid'] = (
                'valid' if level_within else 'limited'
            )
            caps['dut_thd_valid'] = 'limited'
            caps['dut_compression_valid'] = 'limited'
        if requested_class == 'peak_transient':
            peak_seen = (
                observation is not None
                and observation.sample_peak_dbfs is not None
                and observation.peak_semantics == 'peak'
            )
            caps['peak_transient_valid'] = (
                'valid' if peak_seen and overload_free else 'limited'
            )
        else:
            caps['peak_transient_valid'] = (
                'valid' if overload_free else 'limited'
            )
        caps['chain_overload_not_excluded'] = (
            'invalid' if overload_free else 'limited'
        )

    # --- distortion attribution --------------------------------------------
    if state == 'overload_observed':
        attribution: DistortionAttribution = 'chain_overload_confirmed'
    elif state == 'overload_suspected':
        attribution = 'chain_overload_suspected'
    elif state == 'nonlinear_measurement_ineligible':
        attribution = 'chain_distortion_limited'
    elif state == 'unqualified_insufficient_evidence':
        attribution = 'indeterminate'
    elif (
        requested_class == 'dut_nonlinear'
        and caps['dut_thd_valid'] == 'valid'
    ):
        attribution = 'dut_distortion_eligible'
    else:
        attribution = 'chain_distortion_limited'

    payload = dict(
        document_id=document_id,
        chain_ref=measchain_profile_binding(profile),
        observation_ref=(
            measchain_observation_binding(observation)
            if observation is not None
            else None
        ),
        requested_class=requested_class,
        requested_band_low_hz=requested_band_low_hz,
        requested_band_high_hz=requested_band_high_hz,
        requested_level_db=requested_level_db,
        state=state,
        distortion_attribution=attribution,
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_MEASCHAIN_CAPABILITIES
        ),
        limiting_stage_index=(
            observation.overload_stage_index
            if observation is not None
            else None
        ),
        reasons=tuple(reasons),
        evaluation_version=MEASCHAIN_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadMeasChainQualification, payload,
        'qualification_id', 'qualification_sha256', 'mclqual',
    )


__all__ = [
    'ALL_MEASCHAIN_CAPABILITIES',
    'AcquisitionStageKind',
    'CadAcquisitionOverloadObservation',
    'CadAcquisitionStage',
    'CadDynamicProcessingBlock',
    'CadLinearityBand',
    'CadMeasChainLinearityProfile',
    'CadMeasChainQualification',
    'CadTwoLevelCheck',
    'CadUpperLevelSpec',
    'CapabilityState',
    'ChainQualificationState',
    'DistortionAttribution',
    'DynamicProcessingKind',
    'DynamicProcessingState',
    'LevelSemantics',
    'LinearityEvidenceBasis',
    'MEASCHAIN_AUTHORITY_SCHEMA_VERSION',
    'MEASCHAIN_EVALUATION_VERSION',
    'MeasChainCapability',
    'OverloadMechanism',
    'RequestedMeasurementClass',
    'SignalClass',
    'StageOverloadIndicator',
    'STRONG_LINEARITY_BASES',
    'TwoLevelCheckVerdict',
    'UpperLevelThresholdKind',
    'build_measchain_profile',
    'build_overload_observation',
    'evaluate_measchain_qualification',
    'measchain_observation_binding',
    'measchain_profile_binding',
]
