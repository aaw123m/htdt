"""Audio-interface transfer / loopback calibration authority (#699,
REV58-MEASELEC).

A valid #611 microphone calibration does not remove the frequency/phase
response of the playback/capture audio-interface path, and a flat-looking
interface *specification* is not a measured calibration of the exact
ports/range/sample-rate state a campaign used. This module makes the
electrical I/O transfer a sealed, fail-closed authority:

- :class:`CadInterfaceIoPath` — the exact output→input path identity:
  device instances, physical/logical ports, channel, gain/pad/range
  settings, sample rate, bit depth, driver/backend, clock topology,
  loopback path kind, cable/adapter, phantom/bias and firmware. Changing a
  material field is a *different* path — corrections never travel across
  paths silently.
- :class:`CadLoopbackObservation` — sealed raw loopback evidence: exact
  #608 stimulus, capture pin, #609 timebase pin, level semantics and the
  #695 linearity/overload verdict that captured it. The raw evidence is
  retained beside — never only inside — a derived correction curve.
- :class:`CadInterfaceTransferCalibration` — the sealed calibration
  artifact: path identity, calibration kind (combined loopback vs
  independently characterized output/input vs digital loopback vs
  provider), which quantities it carries (magnitude / complex+phase /
  absolute gain / latency / noise), the measured and usable frequency
  bands, interpolation + out-of-range policy, phase timing semantics, and
  any de-embedding reference evidence.
- :class:`CadInterfaceCorrectionQualification` — the fail-closed verdict
  binding a calibration to a requested acquisition path: whether the
  correction may be applied, which capability flags hold, and why not.

Honesty rules baked in:

- A combined analog loopback measures DAC+analog+ADC as one product —
  it is never relabelled ``DAC truth`` or ``ADC truth`` without
  de-embedding evidence (independent characterized reference + method).
- Phase correction requires an explicit time reference (#609) and
  declared pure-delay/unwrap semantics; magnitude-only files cannot
  silently grow a phase claim.
- Sample rate participates in applicability — a different rate is
  ``rate_mismatch`` unless a validated equivalence is declared, never a
  silent reuse.
- ``digital_loopback`` verifies routing/format, not the analog
  DAC/ADC transfer — it never satisfies an analog correction request.
- A USB/integrated microphone path cannot be characterized by an analog
  interface loopback at all — the input side is a separate problem.
- Gain/pad/range/input-mode are material path fields: a changed gain
  state does not inherit the old calibration.
- The correction is a derived transform over immutable raw data; the
  calibration record keeps its uncertainty and residual semantics.

Literature / standards basis
----------------------------
- REW soundcard/interface calibration help (roomeqwizard.com
  help/html/soundcard.html, calsoundcard.html, calfiles.html):
  external-loopback calibration files bound to exact inputs/outputs,
  sample-rate specific, subtracted from later measurements, phase
  optional.
- AES17-2020, *AES standard method for digital audio engineering —
  Measurement of digital audio equipment* (current published edition;
  AES17-R review work remains draft/research-only until published).
- De Vito et al., *A new built-in loopback test method for
  Digital-to-Analog Converter frequency response characterization*,
  Measurement 182 (2021) 109712 — loopback measures a chain; DAC/ADC
  separation needs an explicit acquisition-path model.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


IFC_AUTHORITY_SCHEMA_VERSION = 'ifc-loopback-1'
IFC_EVALUATION_VERSION = 'ifc-loopback-eval-1'

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


def _require_positive(value: float, label: str) -> None:
    _require_finite(value, label)
    if value <= 0:
        raise ValueError(f'{label} must be positive')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#699)
# ---------------------------------------------------------------------------

LoopbackPathKind = Literal['analog', 'digital', 'acoustic', 'unknown']

AcquisitionPathClass = Literal[
    'analog_interface',
    'usb_integrated_capture',
    'digital_only',
    'unknown',
]

CalibrationKind = Literal[
    'combined_dac_analog_adc_loopback',
    'output_path_characterized_with_reference_input',
    'input_path_characterized_with_reference_source',
    'digital_loopback',
    'provider_calibration',
    'other_explicit',
]

#: Kinds that legitimately claim a *single-side* (component) transfer —
#: they required an independent reference measurement.
_COMPONENT_KINDS: frozenset[CalibrationKind] = frozenset(
    {
        'output_path_characterized_with_reference_input',
        'input_path_characterized_with_reference_source',
    }
)

CalibrationQuantity = Literal[
    'magnitude_response',
    'complex_response',
    'absolute_gain',
    'relative_normalized_response',
    'latency_delay',
    'noise_distortion',
]

InterpolationMethod = Literal[
    'linear_db',
    'cubic',
    'minimum_phase_derived',
    'provider_defined',
    'none_piecewise_constant',
    'unknown',
]

OutOfRangePolicy = Literal[
    'reject',
    'hold_edge_values',
    'zero_correction',
    'provider_defined',
    'unknown',
]

CorrectionState = Literal[
    'correction_applied',
    'correction_applied_with_limitations',
    'correction_not_required',
    'correction_missing',
    'correction_ineligible',
]

SampleRateApplicability = Literal[
    'exact_rate_match',
    'validated_rate_equivalence',
    'rate_mismatch',
    'unknown',
]

InterfaceCapability = Literal[
    'magnitude_correction_valid',
    'phase_correction_valid',
    'absolute_gain_valid',
    'latency_correction_valid',
    'component_truth_valid',
    'calibration_linearity_evidenced',
]

ALL_INTERFACE_CAPABILITIES: tuple[InterfaceCapability, ...] = (
    'magnitude_correction_valid',
    'phase_correction_valid',
    'absolute_gain_valid',
    'latency_correction_valid',
    'component_truth_valid',
    'calibration_linearity_evidenced',
)

CapabilityState = Literal['valid', 'limited', 'invalid', 'unknown']

LevelSemantics = Literal['peak', 'rms', 'unknown']


# ---------------------------------------------------------------------------
# Embedded evidence blocks
# ---------------------------------------------------------------------------


class CadInterfaceIoPath(BaseModel):
    """The exact output→input path identity an interface calibration is
    bound to.

    Every material field is part of applicability: change one and the
    identity is different. Unset fields stay ``None`` — an honest unknown
    is recorded as unknown, never defaulted.
    """

    model_config = ConfigDict(frozen=True)

    output_device: str | None = None
    output_port: str | None = None
    output_channel: str | None = None
    output_gain_db: float | None = None
    output_range: str | None = None
    input_device: str | None = None
    input_port: str | None = None
    input_channel: str | None = None
    input_gain_db: float | None = None
    input_pad_db: float | None = None
    input_range: str | None = None
    input_mode: str | None = None
    phantom_power: bool | None = None
    sample_rate_hz: float | None = None
    bit_depth: int | None = None
    sample_format: str | None = None
    driver_backend: str | None = None
    driver_mode: str | None = None
    clock_topology_ref: AuthorityRef | None = None
    loopback_path_kind: LoopbackPathKind = 'unknown'
    cable_adapter_identity: str | None = None
    firmware_version: str | None = None
    software_version: str | None = None
    acquisition_class: AcquisitionPathClass = 'unknown'

    @model_validator(mode='after')
    def valid_path(self) -> 'CadInterfaceIoPath':
        for label, value in (
            ('output_gain_db', self.output_gain_db),
            ('input_gain_db', self.input_gain_db),
            ('input_pad_db', self.input_pad_db),
            ('sample_rate_hz', self.sample_rate_hz),
        ):
            if value is not None:
                _require_finite(value, f'io path {label}')
        if self.input_pad_db is not None and self.input_pad_db < 0:
            raise ValueError('io path input_pad_db must be non-negative')
        if self.sample_rate_hz is not None:
            _require_positive(self.sample_rate_hz, 'io path sample_rate_hz')
        if self.bit_depth is not None and self.bit_depth <= 0:
            raise ValueError('io path bit_depth must be positive')
        if self.clock_topology_ref is not None and (
            self.clock_topology_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the #609 clock-topology pin must carry its sha256'
            )
        return self

    def mismatches_against(
        self, other: 'CadInterfaceIoPath'
    ) -> tuple[str, ...]:
        """Material fields that differ between two path identities.

        A field absent on either side is reported as ``unknown_<name>``
        rather than silently matching — unknown identity is a limitation,
        not a pass.
        """
        fields = (
            'output_device', 'output_port', 'output_channel',
            'output_gain_db', 'output_range',
            'input_device', 'input_port', 'input_channel',
            'input_gain_db', 'input_pad_db', 'input_range', 'input_mode',
            'phantom_power', 'sample_rate_hz', 'bit_depth',
            'sample_format', 'driver_backend', 'driver_mode',
            'loopback_path_kind', 'firmware_version',
            'acquisition_class',
        )
        diffs: list[str] = []
        for name in fields:
            mine = getattr(self, name)
            theirs = getattr(other, name)
            if mine is None or theirs is None:
                if mine is not theirs:
                    diffs.append(f'unknown_{name}')
                continue
            if mine != theirs:
                diffs.append(name)
        return tuple(diffs)


class CadFrequencyCoverage(BaseModel):
    """Measured and usable frequency coverage of a calibration curve."""

    model_config = ConfigDict(frozen=True)

    measured_low_hz: float | None = None
    measured_high_hz: float | None = None
    usable_low_hz: float | None = None
    usable_high_hz: float | None = None
    interpolation: InterpolationMethod = 'unknown'
    out_of_range_policy: OutOfRangePolicy = 'unknown'

    @model_validator(mode='after')
    def valid_coverage(self) -> 'CadFrequencyCoverage':
        for label, value in (
            ('measured_low_hz', self.measured_low_hz),
            ('measured_high_hz', self.measured_high_hz),
            ('usable_low_hz', self.usable_low_hz),
            ('usable_high_hz', self.usable_high_hz),
        ):
            if value is not None:
                _require_positive(value, f'coverage {label}')
        if (
            self.measured_low_hz is not None
            and self.measured_high_hz is not None
            and self.measured_high_hz <= self.measured_low_hz
        ):
            raise ValueError('measured band must be ascending')
        if (
            self.usable_low_hz is not None
            and self.usable_high_hz is not None
            and self.usable_high_hz <= self.usable_low_hz
        ):
            raise ValueError('usable band must be ascending')
        # The usable band can never exceed the measured band — holding a
        # correction flat to DC/Nyquist is a policy, not coverage.
        if (
            self.usable_low_hz is not None
            and self.measured_low_hz is not None
            and self.usable_low_hz < self.measured_low_hz
        ):
            raise ValueError(
                'usable band cannot extend below the measured band'
            )
        if (
            self.usable_high_hz is not None
            and self.measured_high_hz is not None
            and self.usable_high_hz > self.measured_high_hz
        ):
            raise ValueError(
                'usable band cannot extend above the measured band'
            )
        return self

    def covers(self, low_hz: float | None, high_hz: float | None) -> bool:
        """Whether the usable band covers the requested band edges."""
        if self.usable_low_hz is None or self.usable_high_hz is None:
            return False
        low = low_hz if low_hz is not None else self.usable_low_hz
        high = high_hz if high_hz is not None else self.usable_high_hz
        return self.usable_low_hz <= low and high <= self.usable_high_hz


class CadDeembeddingEvidence(BaseModel):
    """Independent reference evidence backing a single-side transfer
    claim — a combined loopback alone can never produce one."""

    model_config = ConfigDict(frozen=True)

    reference_instrument: str = Field(min_length=1)
    reference_uncertainty_db: float | None = None
    method: str = Field(min_length=1)
    raw_measurement_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def valid_evidence(self) -> 'CadDeembeddingEvidence':
        if self.reference_uncertainty_db is not None:
            _require_finite(
                self.reference_uncertainty_db, 'reference_uncertainty_db'
            )
            if self.reference_uncertainty_db < 0:
                raise ValueError('reference_uncertainty_db must be >= 0')
        for ref in self.raw_measurement_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'de-embedding raw measurement pins must carry sha256'
                )
        return self


class CadPhaseSemantics(BaseModel):
    """Timing semantics required before a phase correction is usable."""

    model_config = ConfigDict(frozen=True)

    timebase_ref: AuthorityRef
    pure_delay_removed: bool = False
    unwrapping_convention: str | None = None
    frequency_grid_description: str | None = None

    @model_validator(mode='after')
    def valid_phase(self) -> 'CadPhaseSemantics':
        if self.timebase_ref.ref_sha256 is None:
            raise ValueError(
                'phase semantics require the #609 timebase pin sha256 — '
                'a phase curve without a time origin is not a correction'
            )
        return self


class CadImportedCalSource(BaseModel):
    """Provenance of an imported (REW/provider/user) calibration file."""

    model_config = ConfigDict(frozen=True)

    source_tool: str | None = None
    source_version: str | None = None
    raw_file_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    declared_device: str | None = None
    declared_port: str | None = None
    declared_sample_rate_hz: float | None = None
    normalization: str | None = None

    @model_validator(mode='after')
    def valid_source(self) -> 'CadImportedCalSource':
        if self.declared_sample_rate_hz is not None:
            _require_positive(
                self.declared_sample_rate_hz, 'declared_sample_rate_hz'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadLoopbackObservation(BaseModel):
    """Sealed raw loopback-capture evidence.

    Retains the exact stimulus, capture, timebase and linearity verdict
    *before* any correction curve is derived — a normalized ``.cal``-like
    artifact alone is never the evidence.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    io_path: CadInterfaceIoPath
    stimulus_ref: AuthorityRef
    capture_ref: AuthorityRef | None = None
    measchain_qualification_ref: AuthorityRef | None = None
    state_ref: AuthorityRef | None = None
    observed_level_dbfs: float | None = None
    level_semantics: LevelSemantics = 'unknown'
    method_version: str | None = None
    observed_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadLoopbackObservation':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.observed_at_utc is not None:
            _require_iso8601(self.observed_at_utc, 'observed_at_utc')
        if self.observed_level_dbfs is not None:
            _require_finite(
                self.observed_level_dbfs, 'observed_level_dbfs'
            )
        if self.stimulus_ref.ref_sha256 is None:
            raise ValueError(
                'the #608 stimulus pin must carry its sha256'
            )
        for label, ref in (
            ('capture', self.capture_ref),
            ('measchain qualification', self.measchain_qualification_ref),
            ('measurement state', self.state_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('loopback observation hash mismatch')
        if self.observation_id != _semantic_id('ifcobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'io_path': self.io_path.model_dump(mode='json'),
            'stimulus_ref': self.stimulus_ref.model_dump(mode='json'),
            'capture_ref': (
                self.capture_ref.model_dump(mode='json')
                if self.capture_ref is not None
                else None
            ),
            'measchain_qualification_ref': (
                self.measchain_qualification_ref.model_dump(mode='json')
                if self.measchain_qualification_ref is not None
                else None
            ),
            'state_ref': (
                self.state_ref.model_dump(mode='json')
                if self.state_ref is not None
                else None
            ),
            'observed_level_dbfs': self.observed_level_dbfs,
            'level_semantics': self.level_semantics,
            'method_version': self.method_version,
            'observed_at_utc': self.observed_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }


def loopback_observation_binding(
    observation: CadLoopbackObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='loopback_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


class CadInterfaceTransferCalibration(BaseModel):
    """Sealed interface-transfer calibration bound to one exact I/O path.

    ``calibration_kind`` states what was actually measured: a normal
    analog loopback is ``combined_dac_analog_adc_loopback`` — a combined
    path product, never single-side truth. ``quantities`` states what the
    artifact carries; phase is only present when ``phase_semantics``
    pins its time origin.
    """

    model_config = ConfigDict(frozen=True)

    calibration_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    io_path: CadInterfaceIoPath
    calibration_kind: CalibrationKind
    quantities: tuple[CalibrationQuantity, ...]
    frequency_coverage: CadFrequencyCoverage
    phase_semantics: CadPhaseSemantics | None = None
    absolute_gain_semantics: str | None = None
    correction_curve_ref: AuthorityRef | None = None
    calibration_level_dbfs: float | None = None
    level_semantics: LevelSemantics = 'unknown'
    deembedding: CadDeembeddingEvidence | None = None
    observation_refs: tuple[AuthorityRef, ...] = ()
    imported_source: CadImportedCalSource | None = None
    calibration_lifecycle_ref: AuthorityRef | None = None
    validated_rate_equivalence_hz: tuple[float, ...] = ()
    residual_magnitude_error_db: float | None = None
    residual_phase_error_deg: float | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    provenance_json: str = '{}'
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_calibration(self) -> 'CadInterfaceTransferCalibration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if not self.quantities:
            raise ValueError(
                'a calibration must declare at least one quantity'
            )
        if 'complex_response' in self.quantities and (
            self.phase_semantics is None
        ):
            raise ValueError(
                'complex_response requires phase_semantics — a phase '
                'curve without a time origin is not a correction'
            )
        if 'absolute_gain' in self.quantities and (
            self.absolute_gain_semantics is None
        ):
            raise ValueError(
                'absolute_gain requires declared gain-scale semantics — '
                'a 0 dB-normalized file is not absolute calibration'
            )
        if self.calibration_kind in _COMPONENT_KINDS and (
            self.deembedding is None
        ):
            raise ValueError(
                'a single-side (de-embedded) calibration kind requires '
                'independent reference/de-embedding evidence — one '
                'combined loopback cannot solve two unknown transfers'
            )
        if self.calibration_kind == 'digital_loopback' and (
            set(self.quantities)
            & {'magnitude_response', 'complex_response', 'absolute_gain'}
        ):
            raise ValueError(
                'a digital loopback verifies routing/format — it cannot '
                'carry analog magnitude/phase/gain transfer quantities'
            )
        if (
            self.io_path.acquisition_class == 'usb_integrated_capture'
            and self.io_path.loopback_path_kind == 'analog'
        ):
            raise ValueError(
                'an analog loopback cannot characterize a USB-integrated '
                'capture path — the internal ADC/DSP is not in the loop'
            )
        for ref in self.observation_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'loopback observation pins must carry sha256'
                )
        for label, ref in (
            ('correction curve', self.correction_curve_ref),
            ('#611 lifecycle', self.calibration_lifecycle_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'the {label} pin must carry its sha256')
        if self.calibration_level_dbfs is not None:
            _require_finite(
                self.calibration_level_dbfs, 'calibration_level_dbfs'
            )
        for rate in self.validated_rate_equivalence_hz:
            _require_positive(rate, 'validated_rate_equivalence_hz')
        for label, value in (
            ('residual_magnitude_error_db', self.residual_magnitude_error_db),
            ('residual_phase_error_deg', self.residual_phase_error_deg),
        ):
            if value is not None:
                _require_finite(value, label)
                if value < 0:
                    raise ValueError(f'{label} must be non-negative')
        expected = _hash(self.identity_payload())
        if self.calibration_sha256 != expected:
            raise ValueError('interface calibration hash mismatch')
        if self.calibration_id != _semantic_id('ifccal', expected):
            raise ValueError('calibration id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'io_path': self.io_path.model_dump(mode='json'),
            'calibration_kind': self.calibration_kind,
            'quantities': list(self.quantities),
            'frequency_coverage': self.frequency_coverage.model_dump(
                mode='json'
            ),
            'phase_semantics': (
                self.phase_semantics.model_dump(mode='json')
                if self.phase_semantics is not None
                else None
            ),
            'absolute_gain_semantics': self.absolute_gain_semantics,
            'correction_curve_ref': (
                self.correction_curve_ref.model_dump(mode='json')
                if self.correction_curve_ref is not None
                else None
            ),
            'calibration_level_dbfs': self.calibration_level_dbfs,
            'level_semantics': self.level_semantics,
            'deembedding': (
                self.deembedding.model_dump(mode='json')
                if self.deembedding is not None
                else None
            ),
            'observation_refs': [
                r.model_dump(mode='json') for r in self.observation_refs
            ],
            'imported_source': (
                self.imported_source.model_dump(mode='json')
                if self.imported_source is not None
                else None
            ),
            'calibration_lifecycle_ref': (
                self.calibration_lifecycle_ref.model_dump(mode='json')
                if self.calibration_lifecycle_ref is not None
                else None
            ),
            'validated_rate_equivalence_hz': list(
                self.validated_rate_equivalence_hz
            ),
            'residual_magnitude_error_db': self.residual_magnitude_error_db,
            'residual_phase_error_deg': self.residual_phase_error_deg,
            'declared_at_utc': self.declared_at_utc,
            'authority_version': self.authority_version,
            'provenance_json': self.provenance_json,
        }

    def carries(self, quantity: CalibrationQuantity) -> bool:
        return quantity in self.quantities


def interface_calibration_binding(
    calibration: CadInterfaceTransferCalibration,
) -> AuthorityRef:
    return AuthorityRef(
        kind='interface_transfer_calibration',
        ref_id=calibration.calibration_id,
        ref_sha256=calibration.calibration_sha256,
    )


class CadInterfaceCorrectionQualification(BaseModel):
    """Sealed fail-closed applicability verdict: may this calibration
    correct this acquisition path for the requested quantities?"""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    calibration_ref: AuthorityRef | None = None
    measurement_io_path: CadInterfaceIoPath
    requested_quantities: tuple[CalibrationQuantity, ...]
    requested_band_low_hz: float | None = None
    requested_band_high_hz: float | None = None
    state: CorrectionState
    sample_rate_applicability: SampleRateApplicability
    path_mismatches: tuple[str, ...] = ()
    capabilities: tuple[tuple[InterfaceCapability, CapabilityState], ...]
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadInterfaceCorrectionQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.calibration_ref is not None and (
            self.calibration_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the calibration pin must carry its sha256'
            )
        covered = {capability for capability, _ in self.capabilities}
        if covered != set(ALL_INTERFACE_CAPABILITIES):
            raise ValueError(
                'a correction qualification must report every capability'
            )
        for label, value in (
            ('requested_band_low_hz', self.requested_band_low_hz),
            ('requested_band_high_hz', self.requested_band_high_hz),
        ):
            if value is not None:
                _require_positive(value, f'qualification {label}')
        if (
            self.requested_band_low_hz is not None
            and self.requested_band_high_hz is not None
            and self.requested_band_high_hz <= self.requested_band_low_hz
        ):
            raise ValueError('requested band must be ascending')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('ifcqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'calibration_ref': (
                self.calibration_ref.model_dump(mode='json')
                if self.calibration_ref is not None
                else None
            ),
            'measurement_io_path': self.measurement_io_path.model_dump(
                mode='json'
            ),
            'requested_quantities': list(self.requested_quantities),
            'requested_band_low_hz': self.requested_band_low_hz,
            'requested_band_high_hz': self.requested_band_high_hz,
            'state': self.state,
            'sample_rate_applicability': self.sample_rate_applicability,
            'path_mismatches': list(self.path_mismatches),
            'capabilities': [list(item) for item in self.capabilities],
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def capability_state(
        self, capability: InterfaceCapability
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


def build_loopback_observation(
    *,
    document_id: str,
    io_path: CadInterfaceIoPath,
    stimulus_ref: AuthorityRef,
    capture_ref: AuthorityRef | None = None,
    measchain_qualification_ref: AuthorityRef | None = None,
    state_ref: AuthorityRef | None = None,
    observed_level_dbfs: float | None = None,
    level_semantics: LevelSemantics = 'unknown',
    method_version: str | None = None,
    observed_at_utc: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadLoopbackObservation:
    """Seal raw loopback evidence (#699 §3)."""
    payload = dict(
        document_id=document_id,
        io_path=io_path,
        stimulus_ref=stimulus_ref,
        capture_ref=capture_ref,
        measchain_qualification_ref=measchain_qualification_ref,
        state_ref=state_ref,
        observed_level_dbfs=observed_level_dbfs,
        level_semantics=level_semantics,
        method_version=method_version,
        observed_at_utc=observed_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=IFC_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadLoopbackObservation, payload,
        'observation_id', 'observation_sha256', 'ifcobs',
    )


def build_interface_calibration(
    *,
    document_id: str,
    io_path: CadInterfaceIoPath,
    calibration_kind: CalibrationKind,
    quantities: tuple[CalibrationQuantity, ...] | list[CalibrationQuantity],
    frequency_coverage: CadFrequencyCoverage,
    phase_semantics: CadPhaseSemantics | None = None,
    absolute_gain_semantics: str | None = None,
    correction_curve_ref: AuthorityRef | None = None,
    calibration_level_dbfs: float | None = None,
    level_semantics: LevelSemantics = 'unknown',
    deembedding: CadDeembeddingEvidence | None = None,
    observation_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    imported_source: CadImportedCalSource | None = None,
    calibration_lifecycle_ref: AuthorityRef | None = None,
    validated_rate_equivalence_hz: tuple[float, ...]
    | list[float] = (),
    residual_magnitude_error_db: float | None = None,
    residual_phase_error_deg: float | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadInterfaceTransferCalibration:
    """Seal an interface-transfer calibration (#699 §4/§16)."""
    payload = dict(
        document_id=document_id,
        io_path=io_path,
        calibration_kind=calibration_kind,
        quantities=tuple(quantities),
        frequency_coverage=frequency_coverage,
        phase_semantics=phase_semantics,
        absolute_gain_semantics=absolute_gain_semantics,
        correction_curve_ref=correction_curve_ref,
        calibration_level_dbfs=calibration_level_dbfs,
        level_semantics=level_semantics,
        deembedding=deembedding,
        observation_refs=tuple(observation_refs),
        imported_source=imported_source,
        calibration_lifecycle_ref=calibration_lifecycle_ref,
        validated_rate_equivalence_hz=tuple(validated_rate_equivalence_hz),
        residual_magnitude_error_db=residual_magnitude_error_db,
        residual_phase_error_deg=residual_phase_error_deg,
        declared_at_utc=declared_at_utc or _utc_now(),
        authority_version=IFC_AUTHORITY_SCHEMA_VERSION,
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadInterfaceTransferCalibration, payload,
        'calibration_id', 'calibration_sha256', 'ifccal',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def sample_rate_applicability(
    calibration: CadInterfaceTransferCalibration,
    measurement_rate_hz: float | None,
) -> SampleRateApplicability:
    """Whether the calibration's rate covers the measurement's rate.

    Following current REW practice, sample rate is a material identity —
    a different rate is a mismatch unless a validated equivalence was
    declared for exactly that rate.
    """
    cal_rate = calibration.io_path.sample_rate_hz
    if cal_rate is None or measurement_rate_hz is None:
        return 'unknown'
    if cal_rate == measurement_rate_hz:
        return 'exact_rate_match'
    if measurement_rate_hz in calibration.validated_rate_equivalence_hz:
        return 'validated_rate_equivalence'
    return 'rate_mismatch'


def evaluate_interface_correction(
    *,
    document_id: str,
    calibration: CadInterfaceTransferCalibration | None,
    measurement_io_path: CadInterfaceIoPath,
    requested_quantities: tuple[CalibrationQuantity, ...]
    | list[CalibrationQuantity] = ('magnitude_response',),
    requested_band_low_hz: float | None = None,
    requested_band_high_hz: float | None = None,
    requested_component_truth: bool = False,
    evaluated_at_utc: str | None = None,
) -> CadInterfaceCorrectionQualification:
    """Fail-closed verdict for applying an interface calibration.

    Derivation order: missing calibration → correction_missing; a
    requested-nothing call → correction_not_required; then every material
    path mismatch fails closed before any capability is granted.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    requested = tuple(requested_quantities)
    reasons: list[str] = []
    caps: dict[InterfaceCapability, CapabilityState] = {
        capability: 'unknown'
        for capability in ALL_INTERFACE_CAPABILITIES
    }

    rate_state: SampleRateApplicability = 'unknown'
    mismatches: tuple[str, ...] = ()

    if calibration is None:
        state: CorrectionState = 'correction_missing'
        reasons.append('no interface-transfer calibration is bound')
        caps.update({c: 'invalid' for c in caps})
        caps['component_truth_valid'] = 'invalid'
        caps['calibration_linearity_evidenced'] = 'invalid'
    elif not requested and not requested_component_truth:
        state = 'correction_not_required'
        reasons.append('the request declares no correction quantities')
        caps.update({c: 'valid' for c in caps})
        caps['component_truth_valid'] = 'limited'
    else:
        rate_state = sample_rate_applicability(
            calibration, measurement_io_path.sample_rate_hz
        )
        mismatches = calibration.io_path.mismatches_against(
            measurement_io_path
        )
        # Sample rate reports separately from the generic mismatch list —
        # it has its own applicability verdict (REW semantics).
        mismatches = tuple(
            m for m in mismatches if m != 'sample_rate_hz'
        )
        material_mismatch = tuple(
            m for m in mismatches if not m.startswith('unknown_')
        )
        unknown_fields = tuple(
            m[len('unknown_'):]
            for m in mismatches if m.startswith('unknown_')
        )

        if rate_state == 'rate_mismatch':
            state = 'correction_ineligible'
            reasons.append(
                f'sample rate {measurement_io_path.sample_rate_hz} '
                f'differs from the calibration rate '
                f'{calibration.io_path.sample_rate_hz} — cross-rate '
                'reuse needs a validated equivalence'
            )
        elif measurement_io_path.acquisition_class == (
            'usb_integrated_capture'
        ) and calibration.io_path.loopback_path_kind == 'analog':
            state = 'correction_ineligible'
            reasons.append(
                'an analog interface loopback cannot characterize a '
                'USB-integrated capture path'
            )
        elif calibration.calibration_kind == 'digital_loopback' and (
            set(requested)
            & {
                'magnitude_response',
                'complex_response',
                'absolute_gain',
            }
        ):
            state = 'correction_ineligible'
            reasons.append(
                'a digital loopback verifies routing/format — it is not '
                'an analog I/O transfer calibration'
            )
        elif material_mismatch:
            state = 'correction_ineligible'
            reasons.append(
                'material path identity differs: '
                + ', '.join(material_mismatch)
            )
        else:
            missing = [
                q for q in requested if not calibration.carries(q)
            ]
            band_covered = calibration.frequency_coverage.covers(
                requested_band_low_hz, requested_band_high_hz
            )
            phase_requested = (
                'complex_response' in requested
                or 'latency_delay' in requested
            )
            phase_ready = (
                calibration.phase_semantics is not None
            )
            limited = False
            if missing:
                limited = True
                reasons.append(
                    'calibration lacks quantities: ' + ', '.join(missing)
                )
            if not band_covered:
                limited = True
                reasons.append(
                    'requested band exceeds the usable measured band — '
                    'edge values are never silently held to DC/Nyquist'
                )
            if phase_requested and not phase_ready:
                limited = True
                reasons.append(
                    'phase/latency correction requested without a '
                    'declared time origin (#609) — magnitude still '
                    'eligible, phase is not'
                )
            if unknown_fields:
                limited = True
                reasons.append(
                    'unknown path fields: ' + ', '.join(unknown_fields)
                )
            if rate_state == 'unknown':
                limited = True
                reasons.append('sample-rate applicability is unknown')
            if rate_state == 'validated_rate_equivalence':
                limited = True
                reasons.append(
                    'sample-rate coverage rests on declared equivalence '
                    'rather than an exact-rate measurement'
                )
            state = (
                'correction_applied_with_limitations'
                if limited
                else 'correction_applied'
            )

        # --- capability flags ------------------------------------------
        eligible = state in (
            'correction_applied',
            'correction_applied_with_limitations',
        )
        caps['magnitude_correction_valid'] = (
            'valid'
            if eligible and calibration.carries('magnitude_response')
            and calibration.frequency_coverage.covers(
                requested_band_low_hz, requested_band_high_hz
            )
            else (
                'invalid' if state == 'correction_ineligible' else 'limited'
            )
        )
        caps['phase_correction_valid'] = (
            'valid'
            if eligible
            and calibration.carries('complex_response')
            and calibration.phase_semantics is not None
            else 'invalid'
        )
        caps['absolute_gain_valid'] = (
            'valid'
            if eligible
            and calibration.carries('absolute_gain')
            and calibration.absolute_gain_semantics is not None
            else 'invalid'
        )
        caps['latency_correction_valid'] = (
            'valid'
            if eligible
            and calibration.carries('latency_delay')
            and calibration.phase_semantics is not None
            else 'invalid'
        )
        if requested_component_truth:
            caps['component_truth_valid'] = (
                'valid'
                if eligible
                and calibration.calibration_kind in _COMPONENT_KINDS
                and calibration.deembedding is not None
                else 'invalid'
            )
            if caps['component_truth_valid'] != 'valid':
                reasons.append(
                    'a combined loopback is never single-side DAC/ADC '
                    'truth without de-embedding evidence'
                )
        else:
            caps['component_truth_valid'] = 'limited'
        linearity_evidenced = bool(calibration.observation_refs) and (
            calibration.calibration_kind != 'digital_loopback'
        )
        caps['calibration_linearity_evidenced'] = (
            'valid' if linearity_evidenced else 'unknown'
        )
        if not linearity_evidenced and eligible:
            reasons.append(
                'no #695 loopback-capture linearity verdict is pinned — '
                'the correction curve inherits unverified chain state'
            )

    payload = dict(
        document_id=document_id,
        calibration_ref=(
            interface_calibration_binding(calibration)
            if calibration is not None
            else None
        ),
        measurement_io_path=measurement_io_path,
        requested_quantities=requested,
        requested_band_low_hz=requested_band_low_hz,
        requested_band_high_hz=requested_band_high_hz,
        state=state,
        sample_rate_applicability=rate_state,
        path_mismatches=mismatches,
        capabilities=tuple(
            (capability, caps[capability])
            for capability in ALL_INTERFACE_CAPABILITIES
        ),
        reasons=tuple(reasons),
        evaluation_version=IFC_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadInterfaceCorrectionQualification, payload,
        'qualification_id', 'qualification_sha256', 'ifcqual',
    )


__all__ = [
    'ALL_INTERFACE_CAPABILITIES',
    'AcquisitionPathClass',
    'CadDeembeddingEvidence',
    'CadFrequencyCoverage',
    'CadImportedCalSource',
    'CadInterfaceCorrectionQualification',
    'CadInterfaceIoPath',
    'CadInterfaceTransferCalibration',
    'CadLoopbackObservation',
    'CadPhaseSemantics',
    'CalibrationKind',
    'CalibrationQuantity',
    'CapabilityState',
    'CorrectionState',
    'IFC_AUTHORITY_SCHEMA_VERSION',
    'IFC_EVALUATION_VERSION',
    'InterfaceCapability',
    'InterpolationMethod',
    'LevelSemantics',
    'LoopbackPathKind',
    'OutOfRangePolicy',
    'SampleRateApplicability',
    'build_interface_calibration',
    'build_loopback_observation',
    'evaluate_interface_correction',
    'interface_calibration_binding',
    'loopback_observation_binding',
    'sample_rate_applicability',
]
