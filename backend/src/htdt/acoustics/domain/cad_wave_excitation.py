
import json
from math import cos, isfinite, radians, sin
from typing import Annotated, Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_equipment import (
    EquipmentDataProvenance,
    EquipmentEvidenceKind,
    FrequencyDomain,
    InterpolationProvenance,
)
from ...cad_r110_source import R110CompiledSourceModel
from ...cad_source_response import (
    SourceFrequencyResponseAuthority,
    source_response_wave_excitation_eligible,
)
from ...r120_geometry_compiler import ExactExternalAuthorityRef
from ...canonical_json import canonical_json as _canonical, canonical_sha256 as _digest

WAVE_EXCITATION_AUTHORITY_VERSION = 'r110-wave-excitation-2'
WAVE_SOURCE_BINDING_AUTHORITY_VERSION = 'r110-wave-source-binding-1'
WAVE_EXCITATION_EVIDENCE_AUTHORITY_VERSION = 'wave-excitation-evidence-1'
WAVE_EXCITATION_EVIDENCE_ID_PREFIX = 'wave-excitation-evidence:'

WAVE_EXCITATION_TABLE_SCHEMA = 'htdt.wave-excitation-volume-velocity-table.v1'
WAVE_EXCITATION_TABLE_CONVERTER_ID = 'htdt.wave-excitation-volume-velocity-table'
WAVE_EXCITATION_TABLE_CONVERTER_VERSION = '1'
WAVE_EXCITATION_CONSTANT_MODEL_ID = 'htdt.wave-excitation-constant-volume-velocity'
WAVE_EXCITATION_CONSTANT_MODEL_VERSION = '1'
WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_ID = (
    'htdt.wave-excitation-source-response-volume-velocity'
)
WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_VERSION = '1'
_SOURCE_RESPONSE_PARAMETER_KEYS = frozenset({'imaginary_sign'})

WaveExcitationModel = Literal[
    'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
]
WaveExcitationPhasorConvention = Literal['exp(-i*omega*t)']

# Evidence kinds whose claims are externally sourced: the retained source
# bytes are a managed content-addressed asset and the recorded converter plus
# conversion/calibration parameters must replay to the exact samples.
EXTERNAL_WAVE_EXCITATION_EVIDENCE_KINDS = frozenset(
    {'measured', 'manufacturer', 'inferred'}
)

def _require_canonical_json(value: Mapping[str, Any], *, field_name: str) -> None:
    try:
        _canonical(dict(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field_name} must be canonical JSON') from exc

class ComplexVolumeVelocitySample(BaseModel):
    """Absolute complex acoustic volume velocity in SI units."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    real_m3_s: float
    imag_m3_s: float

    @model_validator(mode='after')
    def finite_sample(self) -> 'ComplexVolumeVelocitySample':
        if not all(
            isfinite(float(value))
            for value in (
                self.frequency_hz,
                self.real_m3_s,
                self.imag_m3_s,
            )
        ):
            raise ValueError('wave-excitation sample values must be finite')
        return self

class WaveExcitationEvidenceSubject(BaseModel):
    """Exact normalized excitation values one evidence authority supports.

    The subject pins the equipment definition the evidence was produced for and
    the exact complex volume-velocity samples the evidence stands behind, so
    evidence retained for one definition or sample set can never silently
    authorize another.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    samples: tuple[ComplexVolumeVelocitySample, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def ordered_subject(self) -> 'WaveExcitationEvidenceSubject':
        frequencies = [float(item.frequency_hz) for item in self.samples]
        if (
            frequencies != sorted(frequencies)
            or len(frequencies) != len(set(frequencies))
        ):
            raise ValueError(
                'wave-excitation evidence subject frequencies must be unique '
                'and sorted'
            )
        return self

class WaveExcitationSourceAssetDerivation(BaseModel):
    """Derivation from exact retained source bytes through a pinned converter.

    ``source_asset_sha256`` names a managed content-addressed asset (the shared
    measurement-assets store and native backup contract); the recorded
    ``converter_id``/``converter_version`` plus the retained
    conversion/calibration parameters must replay the subject samples exactly.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['source_asset'] = 'source_asset'
    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    converter_id: str = Field(min_length=1)
    converter_version: str = Field(min_length=1)
    conversion_parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def canonical_parameters(self) -> 'WaveExcitationSourceAssetDerivation':
        _require_canonical_json(
            self.conversion_parameters,
            field_name='wave-excitation conversion parameters',
        )
        return self

class WaveExcitationManualDerivation(BaseModel):
    """Explicit immutable manual evidence record for user-entered data.

    The retained authority — author, time, the provenance source reference and
    the exact subject values — is itself the evidence; a naked provenance hash
    is never accepted as authoritative.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['manual'] = 'manual'
    author: str = Field(min_length=1)
    authored_at_utc: str = Field(min_length=1)

class WaveExcitationSourceResponseDerivation(BaseModel):
    """Derivation from an exact persisted ``SourceFrequencyResponseAuthority``.

    Pins the response authority's exact id + version + content hash and the
    converter/parameters that derive complex volume-velocity samples from it.
    An edited or replaced response authority never matches the recorded
    triple, so downstream excitation identities go stale exactly.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['source_response'] = 'source_response'
    source_response_id: str = Field(min_length=1)
    source_response_version: str = Field(min_length=1)
    source_response_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    converter_id: str = Field(min_length=1)
    converter_version: str = Field(min_length=1)
    conversion_parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def canonical_parameters(self) -> 'WaveExcitationSourceResponseDerivation':
        _require_canonical_json(
            self.conversion_parameters,
            field_name='wave-excitation source-response conversion parameters',
        )
        return self

class WaveExcitationAnalyticDerivation(BaseModel):
    """Analytic/generated derivation: exact model identity plus parameters.

    Regenerating the recorded model version with the retained parameters must
    reproduce the subject samples exactly.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['analytic_model'] = 'analytic_model'
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    model_parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def canonical_parameters(self) -> 'WaveExcitationAnalyticDerivation':
        _require_canonical_json(
            self.model_parameters,
            field_name='wave-excitation analytic model parameters',
        )
        return self

WaveExcitationDerivation = Annotated[
    WaveExcitationSourceAssetDerivation
    | WaveExcitationManualDerivation
    | WaveExcitationAnalyticDerivation
    | WaveExcitationSourceResponseDerivation,
    Field(discriminator='kind'),
]

_DERIVATION_EVIDENCE_KINDS: dict[str, frozenset[str]] = {
    'source_asset': EXTERNAL_WAVE_EXCITATION_EVIDENCE_KINDS,
    'manual': frozenset({'user_defined'}),
    'analytic_model': frozenset({'analytic'}),
    # The response authority is itself a sealed evidence record; any evidence
    # kind the response stands behind may feed the derivation chain.
    'source_response': frozenset(
        {
            'measured',
            'manufacturer',
            'inferred',
            'analytic',
            'user_defined',
        }
    ),
}

def _evidence_source_sha256(
    derivation: WaveExcitationDerivation,
    subject: WaveExcitationEvidenceSubject,
) -> str:
    """The exact source material a provenance source hash must commit to.

    For source-asset derivations that is the managed asset digest itself; for
    analytic derivations it is the canonical model specification; for manual
    evidence it is the canonical manual statement (author/time plus the exact
    retained values). A fabricated hash therefore cannot satisfy the evidence
    authority validator.
    """
    if isinstance(derivation, WaveExcitationSourceAssetDerivation):
        return derivation.source_asset_sha256
    if isinstance(derivation, WaveExcitationSourceResponseDerivation):
        return _digest(
            {
                'derivation': 'source_response',
                'source_response_id': derivation.source_response_id,
                'source_response_version': derivation.source_response_version,
                'source_response_sha256': derivation.source_response_sha256,
                'converter_id': derivation.converter_id,
                'converter_version': derivation.converter_version,
                'conversion_parameters': derivation.conversion_parameters,
            }
        )
    if isinstance(derivation, WaveExcitationAnalyticDerivation):
        return _digest(
            {
                'derivation': 'analytic_model',
                'model_id': derivation.model_id,
                'model_version': derivation.model_version,
                'model_parameters': derivation.model_parameters,
            }
        )
    return _digest(
        {
            'derivation': 'manual',
            'author': derivation.author,
            'authored_at_utc': derivation.authored_at_utc,
            'subject': subject.model_dump(mode='json'),
        }
    )

class WaveExcitationEvidenceAuthority(BaseModel):
    """Immutable content-addressed evidence behind one provenance claim.

    - measured/manufacturer/inferred claims retain the exact source asset and
      the converter/parameters that derive the complex volume-velocity samples;
    - analytic claims retain the exact model id/version/parameters that
      regenerate the samples;
    - user_defined claims are explicit manual evidence records carrying author,
      time and the exact values — never a bare source hash.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'wave-excitation-evidence-1'
    ] = WAVE_EXCITATION_EVIDENCE_AUTHORITY_VERSION
    evidence_id: str = Field(pattern=r'^wave-excitation-evidence:[0-9a-f]{64}$')
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    provenance: EquipmentDataProvenance
    derivation: WaveExcitationDerivation
    subject: WaveExcitationEvidenceSubject

    @model_validator(mode='after')
    def valid_evidence(self) -> 'WaveExcitationEvidenceAuthority':
        allowed_kinds = _DERIVATION_EVIDENCE_KINDS[self.derivation.kind]
        if self.provenance.evidence_kind not in allowed_kinds:
            raise ValueError(
                f"wave-excitation {self.derivation.kind} evidence requires "
                f'evidence_kind in {sorted(allowed_kinds)}'
            )
        if (
            self.provenance.source_sha256
            != _evidence_source_sha256(self.derivation, self.subject)
        ):
            raise ValueError(
                'wave-excitation evidence provenance hash does not commit to '
                'the retained source material'
            )
        expected = _digest(self.identity_payload())
        if self.evidence_sha256 != expected:
            raise ValueError(
                'WaveExcitationEvidenceAuthority semantic hash mismatch'
            )
        if self.evidence_id != f'{WAVE_EXCITATION_EVIDENCE_ID_PREFIX}{expected}':
            raise ValueError('WaveExcitationEvidenceAuthority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evidence_id', 'evidence_sha256'},
        )

    def subject_sha256(self) -> str:
        return _digest(self.subject.model_dump(mode='json'))

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.evidence_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.evidence_sha256,
        )

def _finite_json_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{field_name} must be a JSON number')
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number

def _reject_json_constant(value: str) -> Any:
    raise ValueError(f'non-finite JSON number is not allowed: {value}')

def _sorted_unique_samples(
    samples: Sequence[ComplexVolumeVelocitySample],
) -> tuple[ComplexVolumeVelocitySample, ...]:
    ordered = sorted(samples, key=lambda item: float(item.frequency_hz))
    frequencies = [float(item.frequency_hz) for item in ordered]
    if len(frequencies) != len(set(frequencies)):
        raise ValueError('wave-excitation derivation produced duplicate frequencies')
    return tuple(ordered)

class WaveExcitationSourceConverter(Protocol):
    """Versioned source-asset → complex volume-velocity converter contract."""

    converter_id: str
    converter_version: str

    def convert(
        self,
        source_bytes: bytes,
        parameters: Mapping[str, Any],
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        ...

_TABLE_FREQUENCY_UNIT_SCALE = {'Hz': 1.0, 'kHz': 1000.0}
_TABLE_VALUE_UNIT_SCALE = {'m3_s': 1.0, 'cm3_s': 1e-6, 'mm3_s': 1e-9}
_TABLE_VALUE_FORMS = {'rectangular', 'polar_deg'}
_TABLE_IMAGINARY_SIGNS = {'as_recorded', 'conjugate'}
_TABLE_PARAMETER_KEYS = frozenset(
    {
        'frequency_unit',
        'value_unit',
        'value_form',
        'calibration_scale',
        'imaginary_sign',
    }
)
_TABLE_REQUIRED_PARAMETERS = frozenset(
    {'frequency_unit', 'value_unit', 'value_form'}
)

class VolumeVelocityTableConverter:
    """Canonical ``htdt.wave-excitation-volume-velocity-table.v1`` converter.

    The retained source is a strict UTF-8 JSON document
    ``{"schema": ..., "rows": [...]}`` carrying raw numeric rows. Every semantic
    needed to derive absolute complex volume velocity lives in the recorded
    conversion parameters — frequency unit, value unit, value form
    (rectangular real/imag or polar magnitude/phase_deg), an optional
    calibration scale and an optional imaginary-sign flip — so replaying the
    pinned converter over the exact bytes reproduces the persisted samples.
    """

    converter_id = WAVE_EXCITATION_TABLE_CONVERTER_ID
    converter_version = WAVE_EXCITATION_TABLE_CONVERTER_VERSION

    def convert(
        self,
        source_bytes: bytes,
        parameters: Mapping[str, Any],
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        unknown = sorted(set(parameters) - _TABLE_PARAMETER_KEYS)
        if unknown:
            raise ValueError(
                f'unknown wave-excitation conversion parameters: {unknown}'
            )
        missing = sorted(_TABLE_REQUIRED_PARAMETERS - set(parameters))
        if missing:
            raise ValueError(
                f'missing wave-excitation conversion parameters: {missing}'
            )

        frequency_unit = parameters['frequency_unit']
        if frequency_unit not in _TABLE_FREQUENCY_UNIT_SCALE:
            raise ValueError('wave-excitation frequency_unit must be Hz or kHz')
        value_unit = parameters['value_unit']
        if value_unit not in _TABLE_VALUE_UNIT_SCALE:
            raise ValueError(
                'wave-excitation value_unit must be m3_s, cm3_s or mm3_s'
            )
        value_form = parameters['value_form']
        if value_form not in _TABLE_VALUE_FORMS:
            raise ValueError(
                'wave-excitation value_form must be rectangular or polar_deg'
            )
        imaginary_sign = parameters.get('imaginary_sign', 'as_recorded')
        if imaginary_sign not in _TABLE_IMAGINARY_SIGNS:
            raise ValueError(
                'wave-excitation imaginary_sign must be as_recorded or conjugate'
            )
        calibration = _finite_json_number(
            parameters.get('calibration_scale', 1.0),
            field_name='calibration_scale',
        )
        if calibration <= 0.0:
            raise ValueError('wave-excitation calibration_scale must be positive')

        try:
            # utf-8-sig: tolerate a leading BOM on file ingress; anything
            # else non-UTF-8 still fails closed.
            text = source_bytes.decode('utf-8-sig', errors='strict')
        except UnicodeDecodeError as exc:
            raise ValueError(
                'wave-excitation source table must be strict UTF-8'
            ) from exc
        try:
            document = json.loads(text, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            raise ValueError(
                'wave-excitation source table must be a JSON document'
            ) from exc
        if not isinstance(document, dict) or set(document) != {'schema', 'rows'}:
            raise ValueError(
                'wave-excitation source table must be an object with exactly '
                'schema and rows'
            )
        if document['schema'] != WAVE_EXCITATION_TABLE_SCHEMA:
            raise ValueError('unsupported wave-excitation source table schema')
        rows = document['rows']
        if not isinstance(rows, list) or not rows:
            raise ValueError(
                'wave-excitation source table requires at least one row'
            )

        frequency_scale = _TABLE_FREQUENCY_UNIT_SCALE[frequency_unit]
        linear_scale = _TABLE_VALUE_UNIT_SCALE[value_unit] * calibration
        expected_keys = (
            {'frequency', 'real', 'imag'}
            if value_form == 'rectangular'
            else {'frequency', 'magnitude', 'phase_deg'}
        )
        samples: list[ComplexVolumeVelocitySample] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected_keys:
                raise ValueError(
                    'wave-excitation source table row fields must exactly '
                    'match the declared value_form'
                )
            frequency_hz = (
                _finite_json_number(row['frequency'], field_name='frequency')
                * frequency_scale
            )
            if value_form == 'rectangular':
                real = _finite_json_number(row['real'], field_name='real')
                imag = _finite_json_number(row['imag'], field_name='imag')
            else:
                magnitude = _finite_json_number(
                    row['magnitude'], field_name='magnitude'
                )
                phase_deg = _finite_json_number(
                    row['phase_deg'], field_name='phase_deg'
                )
                real = magnitude * cos(radians(phase_deg))
                imag = magnitude * sin(radians(phase_deg))
            real *= linear_scale
            imag *= linear_scale
            if imaginary_sign == 'conjugate':
                imag = -imag
            samples.append(
                ComplexVolumeVelocitySample(
                    frequency_hz=frequency_hz,
                    real_m3_s=real,
                    imag_m3_s=imag,
                )
            )
        return _sorted_unique_samples(samples)

class SourceResponseVolumeVelocityConverter:
    """Replayable ``SourceFrequencyResponseAuthority`` → complex Q converter.

    The retained source is the response authority's canonical JSON payload;
    conversion emits one ``ComplexVolumeVelocitySample`` per response sample
    from the paired magnitude/phase of the declared ``exp(-i*omega*t)``
    phasor. An ineligible response (wrong tier, missing phase/phasor or
    reference semantics) fails closed instead of being silently promoted.
    """

    converter_id = WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_ID
    converter_version = WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_VERSION

    def convert(
        self,
        source_bytes: bytes,
        parameters: Mapping[str, Any],
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        unknown = sorted(set(parameters) - _SOURCE_RESPONSE_PARAMETER_KEYS)
        if unknown:
            raise ValueError(
                f'unknown source-response conversion parameters: {unknown}'
            )
        imaginary_sign = parameters.get('imaginary_sign', 'as_recorded')
        if imaginary_sign not in _TABLE_IMAGINARY_SIGNS:
            raise ValueError(
                'source-response imaginary_sign must be as_recorded or '
                'conjugate'
            )
        try:
            # utf-8-sig: tolerate a leading BOM on file ingress; anything
            # else non-UTF-8 still fails closed.
            text = source_bytes.decode('utf-8-sig', errors='strict')
        except UnicodeDecodeError as exc:
            raise ValueError(
                'source-response wave-excitation payload must be strict UTF-8'
            ) from exc
        response = SourceFrequencyResponseAuthority.model_validate_json(text)
        ineligible = source_response_wave_excitation_eligible(response)
        if ineligible is not None:
            raise ValueError(
                f'source response is not eligible for wave excitation: '
                f'{ineligible}'
            )
        samples: list[ComplexVolumeVelocitySample] = []
        for item in response.response_samples:
            assert item.volume_velocity_m3_s is not None
            assert item.volume_velocity_phase_deg is not None
            magnitude = float(item.volume_velocity_m3_s)
            phase_rad = radians(float(item.volume_velocity_phase_deg))
            real = magnitude * cos(phase_rad)
            imag = magnitude * sin(phase_rad)
            if imaginary_sign == 'conjugate':
                imag = -imag
            samples.append(
                ComplexVolumeVelocitySample(
                    frequency_hz=float(item.frequency_hz),
                    real_m3_s=real,
                    imag_m3_s=imag,
                )
            )
        return _sorted_unique_samples(samples)

class WaveExcitationAnalyticModel(Protocol):
    """Versioned analytic excitation generator contract."""

    model_id: str
    model_version: str

    def generate(
        self,
        parameters: Mapping[str, Any],
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        ...

class ConstantVolumeVelocityModel:
    """Exact constant complex volume-velocity spectrum generator."""

    model_id = WAVE_EXCITATION_CONSTANT_MODEL_ID
    model_version = WAVE_EXCITATION_CONSTANT_MODEL_VERSION

    def generate(
        self,
        parameters: Mapping[str, Any],
    ) -> tuple[ComplexVolumeVelocitySample, ...]:
        if set(parameters) != {'frequencies_hz', 'real_m3_s', 'imag_m3_s'}:
            raise ValueError(
                'constant volume-velocity model requires exactly '
                'frequencies_hz, real_m3_s and imag_m3_s'
            )
        real = _finite_json_number(parameters['real_m3_s'], field_name='real_m3_s')
        imag = _finite_json_number(parameters['imag_m3_s'], field_name='imag_m3_s')
        frequencies = parameters['frequencies_hz']
        if not isinstance(frequencies, (list, tuple)) or not frequencies:
            raise ValueError('frequencies_hz must be a non-empty array')
        samples = [
            ComplexVolumeVelocitySample(
                frequency_hz=_finite_json_number(
                    frequency, field_name='frequencies_hz'
                ),
                real_m3_s=real,
                imag_m3_s=imag,
            )
            for frequency in frequencies
        ]
        return _sorted_unique_samples(samples)

_SOURCE_CONVERTERS: dict[tuple[str, str], WaveExcitationSourceConverter] = {
    (
        WAVE_EXCITATION_TABLE_CONVERTER_ID,
        WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
    ): VolumeVelocityTableConverter(),
    (
        WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_ID,
        WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_VERSION,
    ): SourceResponseVolumeVelocityConverter(),
}
_ANALYTIC_MODELS: dict[tuple[str, str], WaveExcitationAnalyticModel] = {
    (
        WAVE_EXCITATION_CONSTANT_MODEL_ID,
        WAVE_EXCITATION_CONSTANT_MODEL_VERSION,
    ): ConstantVolumeVelocityModel(),
}

def replay_wave_excitation_source_derivation(
    *,
    source_bytes: bytes,
    converter_id: str,
    converter_version: str,
    conversion_parameters: Mapping[str, Any],
) -> tuple[ComplexVolumeVelocitySample, ...]:
    """Re-run a recorded converter version against exact retained source bytes.

    The pinned registry must still resolve the exact
    ``(converter_id, converter_version)`` implementation that produced a
    persisted excitation, so removed or never-registered versions fail closed
    instead of silently approximating the derivation.
    """
    converter = _SOURCE_CONVERTERS.get((converter_id, converter_version))
    if converter is None:
        raise ValueError(
            'no registered wave-excitation source converter for '
            f'{converter_id}@{converter_version}'
        )
    return converter.convert(source_bytes, conversion_parameters)

def replay_wave_excitation_analytic_derivation(
    *,
    model_id: str,
    model_version: str,
    model_parameters: Mapping[str, Any],
) -> tuple[ComplexVolumeVelocitySample, ...]:
    """Regenerate exact samples from a recorded analytic model version."""
    model = _ANALYTIC_MODELS.get((model_id, model_version))
    if model is None:
        raise ValueError(
            'no registered wave-excitation analytic model for '
            f'{model_id}@{model_version}'
        )
    return model.generate(model_parameters)

class AcousticWaveExcitationAuthority(BaseModel):
    """Explicit acoustic source-strength authority.

    This is not inferred from electrical sensitivity. It represents an exact
    equivalent-monopole volume-velocity spectrum at the equipment acoustic
    reference point. Every provenance claim is paired positionally with a typed
    ``source_evidence`` ref to a retained ``WaveExcitationEvidenceAuthority`` —
    a bare provenance hash is never authoritative evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r110-wave-excitation-2'
    ] = WAVE_EXCITATION_AUTHORITY_VERSION
    excitation_id: str = Field(pattern=r'^acoustic-wave-excitation:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    excitation_model: WaveExcitationModel = (
        'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
    )
    quantity: Literal['complex_volume_velocity_m3_s'] = 'complex_volume_velocity_m3_s'
    phasor_convention: WaveExcitationPhasorConvention = 'exp(-i*omega*t)'
    reference_semantics: Literal[
        'equipment_acoustic_reference_point'
    ] = 'equipment_acoustic_reference_point'

    samples: tuple[ComplexVolumeVelocitySample, ...] = Field(min_length=1)
    valid_frequency_domain: FrequencyDomain
    interpolation: InterpolationProvenance
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    source_evidence: tuple[ExactExternalAuthorityRef, ...] = Field(min_length=1)
    approximation_note: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_authority(self) -> 'AcousticWaveExcitationAuthority':
        frequencies = [float(item.frequency_hz) for item in self.samples]
        if frequencies != sorted(frequencies) or len(frequencies) != len(set(frequencies)):
            raise ValueError(
                'wave-excitation frequencies must be unique and sorted'
            )
        if (
            float(self.valid_frequency_domain.minimum_hz) != frequencies[0]
            or float(self.valid_frequency_domain.maximum_hz) != frequencies[-1]
        ):
            raise ValueError(
                'wave-excitation valid frequency domain must equal sample bounds'
            )
        if len(self.source_evidence) != len(self.provenance):
            raise ValueError(
                'wave-excitation source evidence must pair with every '
                'provenance claim'
            )
        if self.interpolation.provenance not in self.provenance:
            raise ValueError(
                'wave-excitation interpolation provenance must resolve to '
                'an excitation provenance item'
            )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('AcousticWaveExcitationAuthority semantic hash mismatch')
        if self.excitation_id != f'acoustic-wave-excitation:{expected}':
            raise ValueError('AcousticWaveExcitationAuthority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'excitation_id', 'semantic_sha256'},
        )

class WaveSourceExcitationBinding(BaseModel):
    """Exact composition of one persisted R110 source and one excitation authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'r110-wave-source-binding-1'
    ] = WAVE_SOURCE_BINDING_AUTHORITY_VERSION
    binding_id: str = Field(pattern=r'^wave-source-excitation-binding:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    r110_compiled_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    excitation_id: str = Field(pattern=r'^acoustic-wave-excitation:[0-9a-f]{64}$')
    excitation_authority_version: str = Field(min_length=1)
    excitation_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    valid_frequency_domain: FrequencyDomain
    source_model: WaveExcitationModel

    @model_validator(mode='after')
    def validate_binding(self) -> 'WaveSourceExcitationBinding':
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('WaveSourceExcitationBinding semantic hash mismatch')
        if self.binding_id != f'wave-source-excitation-binding:{expected}':
            raise ValueError('WaveSourceExcitationBinding id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'binding_id', 'semantic_sha256'},
        )

def build_wave_excitation_evidence_authority(
    *,
    evidence_kind: EquipmentEvidenceKind,
    source_name: str,
    source_version: str,
    source_reference: str,
    derivation: WaveExcitationDerivation,
    subject: WaveExcitationEvidenceSubject,
) -> WaveExcitationEvidenceAuthority:
    """Build an evidence authority whose provenance hash commits to its source.

    ``source_sha256`` is computed from the derivation's exact source material —
    the managed asset digest, the analytic model specification or the manual
    statement — so a caller can never label an arbitrary hash as evidence.
    """
    provenance = EquipmentDataProvenance(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version=source_version,
        source_reference=source_reference,
        source_sha256=_evidence_source_sha256(derivation, subject),
    )
    core = {
        'authority_version': WAVE_EXCITATION_EVIDENCE_AUTHORITY_VERSION,
        'provenance': provenance.model_dump(mode='json'),
        'derivation': derivation.model_dump(mode='json'),
        'subject': subject.model_dump(mode='json'),
    }
    digest = _digest(core)
    return WaveExcitationEvidenceAuthority(
        evidence_id=f'{WAVE_EXCITATION_EVIDENCE_ID_PREFIX}{digest}',
        evidence_sha256=digest,
        provenance=provenance,
        derivation=derivation,
        subject=subject,
    )

def build_acoustic_wave_excitation_authority(
    *,
    definition_id: str,
    definition_version: str,
    definition_sha256: str,
    samples: Sequence[ComplexVolumeVelocitySample],
    interpolation: InterpolationProvenance,
    provenance: Sequence[EquipmentDataProvenance],
    evidence: Sequence[WaveExcitationEvidenceAuthority],
    approximation_note: str,
) -> AcousticWaveExcitationAuthority:
    sample_tuple = tuple(samples)
    if len(sample_tuple) < 2:
        raise ValueError(
            'wave-excitation authority requires at least two frequency samples'
        )
    frequencies = [float(item.frequency_hz) for item in sample_tuple]
    if frequencies != sorted(frequencies) or len(frequencies) != len(set(frequencies)):
        raise ValueError('wave-excitation frequencies must be unique and sorted')
    provenance_tuple = tuple(provenance)
    evidence_tuple = tuple(evidence)
    if len(evidence_tuple) != len(provenance_tuple):
        raise ValueError(
            'wave-excitation source evidence must pair with every provenance '
            'claim'
        )
    for claim, item in zip(provenance_tuple, evidence_tuple, strict=True):
        if item.provenance != claim:
            raise ValueError(
                'wave-excitation evidence authority does not match its paired '
                'provenance claim'
            )
        if (
            item.subject.definition_id != definition_id
            or item.subject.definition_version != definition_version
            or item.subject.definition_sha256 != definition_sha256
        ):
            raise ValueError(
                'wave-excitation evidence subject does not match the '
                'EquipmentDefinition'
            )
        if tuple(item.subject.samples) != sample_tuple:
            raise ValueError(
                'wave-excitation evidence subject does not support the samples'
            )
    evidence_refs = tuple(item.as_external_ref() for item in evidence_tuple)
    domain = FrequencyDomain(
        minimum_hz=frequencies[0],
        maximum_hz=frequencies[-1],
    )
    core = {
        'authority_version': WAVE_EXCITATION_AUTHORITY_VERSION,
        'definition_id': definition_id,
        'definition_version': definition_version,
        'definition_sha256': definition_sha256,
        'excitation_model': (
            'equivalent_monopole_volume_velocity_at_equipment_acoustic_reference'
        ),
        'quantity': 'complex_volume_velocity_m3_s',
        'phasor_convention': 'exp(-i*omega*t)',
        'reference_semantics': 'equipment_acoustic_reference_point',
        'samples': [item.model_dump(mode='json') for item in sample_tuple],
        'valid_frequency_domain': domain.model_dump(mode='json'),
        'interpolation': interpolation.model_dump(mode='json'),
        'provenance': [item.model_dump(mode='json') for item in provenance_tuple],
        'source_evidence': [
            item.model_dump(mode='json') for item in evidence_refs
        ],
        'approximation_note': approximation_note,
    }
    digest = _digest(core)
    return AcousticWaveExcitationAuthority(
        excitation_id=f'acoustic-wave-excitation:{digest}',
        semantic_sha256=digest,
        definition_id=definition_id,
        definition_version=definition_version,
        definition_sha256=definition_sha256,
        samples=sample_tuple,
        valid_frequency_domain=domain,
        interpolation=interpolation,
        provenance=provenance_tuple,
        source_evidence=evidence_refs,
        approximation_note=approximation_note,
    )

def derive_wave_excitation_evidence_from_source_response(
    *,
    response: SourceFrequencyResponseAuthority,
    evidence_kind: EquipmentEvidenceKind,
    source_name: str,
    source_version: str,
    source_reference: str,
    conversion_parameters: Mapping[str, Any] | None = None,
) -> WaveExcitationEvidenceAuthority:
    """Derive excitation evidence from an exact source-response authority.

    The source response must satisfy the wave-excitation eligibility contract
    (EXACT_VOLUME_VELOCITY tier, complete complex volume-velocity samples,
    declared phasor/reference semantics, explicit condition and frequency
    domain); the derived subject samples are produced by replaying the pinned
    converter so the evidence authority's derivation replays exactly.
    """
    parameters = dict(conversion_parameters or {})
    ineligible = source_response_wave_excitation_eligible(response)
    if ineligible is not None:
        raise ValueError(
            'source response is not eligible for wave excitation: '
            f'{ineligible}'
        )
    samples = replay_wave_excitation_source_derivation(
        source_bytes=response.model_dump_json().encode('utf-8'),
        converter_id=WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_ID,
        converter_version=WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_VERSION,
        conversion_parameters=parameters,
    )
    derivation = WaveExcitationSourceResponseDerivation(
        source_response_id=response.response_id,
        source_response_version=response.authority_version,
        source_response_sha256=response.semantic_sha256,
        converter_id=WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_ID,
        converter_version=WAVE_EXCITATION_SOURCE_RESPONSE_CONVERTER_VERSION,
        conversion_parameters=parameters,
    )
    subject = WaveExcitationEvidenceSubject(
        definition_id=response.equipment_definition_id,
        definition_version=response.equipment_definition_version,
        definition_sha256=response.equipment_definition_sha256,
        samples=samples,
    )
    return build_wave_excitation_evidence_authority(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version=source_version,
        source_reference=source_reference,
        derivation=derivation,
        subject=subject,
    )

def bind_wave_excitation_to_r110_source(
    *,
    source: R110CompiledSourceModel,
    excitation: AcousticWaveExcitationAuthority,
) -> WaveSourceExcitationBinding:
    source = R110CompiledSourceModel.model_validate(
        source.model_dump(mode='python')
    )
    excitation = AcousticWaveExcitationAuthority.model_validate(
        excitation.model_dump(mode='python')
    )
    if (
        excitation.definition_id != source.equipment_definition_id
        or excitation.definition_version != source.equipment_definition_version
        or excitation.definition_sha256 != source.equipment_definition_sha256
    ):
        raise ValueError(
            'wave-excitation EquipmentDefinition does not match exact R110 source'
        )
    core = {
        'authority_version': WAVE_SOURCE_BINDING_AUTHORITY_VERSION,
        'r110_compiled_source_sha256': source.semantic_sha256,
        'source_entity_id': source.source_entity_id,
        'equipment_definition_id': source.equipment_definition_id,
        'equipment_definition_version': source.equipment_definition_version,
        'equipment_definition_sha256': source.equipment_definition_sha256,
        'excitation_id': excitation.excitation_id,
        'excitation_authority_version': excitation.authority_version,
        'excitation_semantic_sha256': excitation.semantic_sha256,
        'valid_frequency_domain': excitation.valid_frequency_domain.model_dump(
            mode='json'
        ),
        'source_model': excitation.excitation_model,
    }
    digest = _digest(core)
    return WaveSourceExcitationBinding(
        binding_id=f'wave-source-excitation-binding:{digest}',
        semantic_sha256=digest,
        r110_compiled_source_sha256=source.semantic_sha256,
        source_entity_id=source.source_entity_id,
        equipment_definition_id=source.equipment_definition_id,
        equipment_definition_version=source.equipment_definition_version,
        equipment_definition_sha256=source.equipment_definition_sha256,
        excitation_id=excitation.excitation_id,
        excitation_authority_version=excitation.authority_version,
        excitation_semantic_sha256=excitation.semantic_sha256,
        valid_frequency_domain=excitation.valid_frequency_domain,
        source_model=excitation.excitation_model,
    )

class WaveExcitationSourceAssetMetadata(BaseModel):
    """Preserved import-context metadata for one excitation source asset.

    Byte identity lives in the shared managed asset registry
    (``cad_measurement_assets`` row plus the digest-named file); this record
    preserves the original filename/media/schema context separately so evidence
    metadata never alters the content address.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    filename: str | None = Field(default=None, min_length=1)
    media_type: str | None = Field(default=None, min_length=1)
    declared_schema: str | None = Field(default=None, min_length=1)
    size_bytes: int = Field(ge=0)
    recorded_at_utc: str = Field(min_length=1)

