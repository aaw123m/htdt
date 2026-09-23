from __future__ import annotations

from bisect import bisect_left
from hashlib import sha256
from itertools import product
import json
from math import atan2, cos, isfinite, log, log10, pi, radians, sin
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import (
    DirectivityDataFormat,
    DirectivityDomain,
    EquipmentDataProvenance,
    EquipmentDefinition,
    EquipmentEvidenceKind,
    FrequencyDomain,
    AngleDomain,
    InterpolationMethod,
    InterpolationProvenance,
)


DIRECTIVITY_DATASET_SCHEMA_VERSION = 1
DIRECTIVITY_DATASET_AUTHORITY_VERSION = 'o100c-directivity-dataset-1'
DIRECTIVITY_EVALUATION_AUTHORITY_VERSION = 'o100c-directivity-evaluation-1'
DIRECTIVITY_DB_CONVERSION_VERSION = 'pressure-amplitude-db20-v1'
NORMALIZED_JSON_FORMAT = 'htdt_normalized_directivity_json_v1'
NORMALIZED_JSON_PARSER_ID = 'htdt.normalized-directivity-json'
NORMALIZED_JSON_PARSER_VERSION = '1'
NORMALIZED_JSON_ADAPTER_ID = 'htdt.normalized-directivity-adapter'
NORMALIZED_JSON_ADAPTER_VERSION = '1'

DirectivityDatasetKind = Literal['magnitude_only', 'complex']
DirectivityAngleSemantics = Literal[
    'horizontal_vertical',
    'spherical_azimuth_elevation',
]
DirectivityAngleWrap = Literal['none', 'signed_180']
DirectivityReferenceAxis = Literal[
    'equipment_acoustic_reference_axis',
]
DirectivityNormalizationReference = Literal[
    'on_axis_per_frequency',
    'explicit_reference_level',
]
DirectivityMagnitudeUnit = Literal['db', 'linear']
DirectivityEvaluationRequest = Literal['magnitude', 'complex']
DirectivityEvaluationDecision = Literal['SUPPORTED', 'UNSUPPORTED']


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def _validate_axis(
    values: Sequence[float],
    *,
    field_name: str,
    positive: bool = False,
    minimum_length: int = 1,
) -> tuple[float, ...]:
    result = tuple(
        _finite(value, field_name=field_name)
        for value in values
    )
    if len(result) < minimum_length:
        raise ValueError(f'{field_name} requires at least {minimum_length} values')
    if positive and any(value <= 0.0 for value in result):
        raise ValueError(f'{field_name} values must be positive')
    if tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise ValueError(f'{field_name} values must be strictly increasing and unique')
    return result


class DirectivityCoordinateConvention(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    angle_semantics: DirectivityAngleSemantics
    horizontal_wrap: DirectivityAngleWrap
    reference_axis: DirectivityReferenceAxis = 'equipment_acoustic_reference_axis'
    azimuth_positive: Literal['left'] = 'left'
    elevation_positive: Literal['up'] = 'up'
    angle_unit: Literal['degree'] = 'degree'

    @model_validator(mode='after')
    def valid_wrap(self) -> 'DirectivityCoordinateConvention':
        if (
            self.angle_semantics == 'horizontal_vertical'
            and self.horizontal_wrap != 'none'
        ):
            raise ValueError(
                'horizontal/vertical convention does not permit azimuth wrap'
            )
        return self


class DirectivityNormalization(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_magnitude_unit: DirectivityMagnitudeUnit
    normalized_magnitude_unit: Literal['db'] = 'db'
    reference: DirectivityNormalizationReference
    reference_level_db: float | None = None
    conversion_version: Literal[
        'pressure-amplitude-db20-v1'
    ] = DIRECTIVITY_DB_CONVERSION_VERSION

    @field_validator('reference_level_db')
    @classmethod
    def finite_reference(
        cls,
        value: float | None,
    ) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='reference_level_db')

    @model_validator(mode='after')
    def valid_reference(self) -> 'DirectivityNormalization':
        if self.reference == 'on_axis_per_frequency':
            if self.reference_level_db is not None:
                raise ValueError(
                    'on-axis normalization must not carry an explicit reference level'
                )
        elif self.reference_level_db is None:
            raise ValueError(
                'explicit-reference normalization requires reference_level_db'
            )
        return self


class DirectivitySample(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    horizontal_angle_deg: float
    vertical_angle_deg: float
    magnitude_db: float
    phase_deg: float | None = None

    @field_validator(
        'frequency_hz',
        'horizontal_angle_deg',
        'vertical_angle_deg',
        'magnitude_db',
        'phase_deg',
    )
    @classmethod
    def finite_values(
        cls,
        value: float | None,
    ) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='directivity sample value')


class DirectivityDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = DIRECTIVITY_DATASET_SCHEMA_VERSION
    authority_version: Literal[
        'o100c-directivity-dataset-1'
    ] = DIRECTIVITY_DATASET_AUTHORITY_VERSION

    dataset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    source_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_format: DirectivityDataFormat
    container_format: Literal[
        'htdt_normalized_directivity_json_v1'
    ] = NORMALIZED_JSON_FORMAT
    parser_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    evidence_kind: EquipmentEvidenceKind
    source_provenance: EquipmentDataProvenance

    kind: DirectivityDatasetKind
    coordinate_convention: DirectivityCoordinateConvention
    normalization: DirectivityNormalization
    phase_reference: str | None = Field(default=None, min_length=1)

    frequencies_hz: tuple[float, ...] = Field(min_length=2)
    horizontal_angles_deg: tuple[float, ...] = Field(min_length=1)
    vertical_angles_deg: tuple[float, ...] = Field(min_length=1)
    samples: tuple[DirectivitySample, ...] = Field(min_length=1)

    valid_domain: DirectivityDomain
    interpolation: InterpolationProvenance

    grid_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('frequencies_hz')
    @classmethod
    def valid_frequency_grid(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        return _validate_axis(
            values,
            field_name='frequency grid',
            positive=True,
            minimum_length=2,
        )

    @field_validator('horizontal_angles_deg')
    @classmethod
    def valid_horizontal_grid(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        return _validate_axis(
            values,
            field_name='horizontal angle grid',
        )

    @field_validator('vertical_angles_deg')
    @classmethod
    def valid_vertical_grid(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        return _validate_axis(
            values,
            field_name='vertical angle grid',
        )

    @model_validator(mode='after')
    def valid_dataset(self) -> 'DirectivityDataset':
        if self.source_provenance.evidence_kind != self.evidence_kind:
            raise ValueError('dataset evidence kind does not match source provenance')
        if self.source_provenance.source_sha256 != self.source_asset_sha256:
            raise ValueError('dataset source asset hash does not match source provenance')

        if self.kind == 'complex':
            if self.phase_reference is None:
                raise ValueError('complex dataset requires explicit phase reference')
            if any(sample.phase_deg is None for sample in self.samples):
                raise ValueError('complex dataset requires phase for every sample')
        else:
            if self.phase_reference is not None:
                raise ValueError(
                    'magnitude-only dataset must not claim a phase reference'
                )
            if any(sample.phase_deg is not None for sample in self.samples):
                raise ValueError(
                    'magnitude-only dataset must not contain phase samples'
                )

        if self.coordinate_convention.horizontal_wrap == 'signed_180':
            if any(
                angle < -180.0 or angle >= 180.0
                for angle in self.horizontal_angles_deg
            ):
                raise ValueError(
                    'signed_180 azimuth grid must use [-180, 180) coordinates'
                )

        expected_domain = DirectivityDomain(
            frequency=FrequencyDomain(
                minimum_hz=self.frequencies_hz[0],
                maximum_hz=self.frequencies_hz[-1],
            ),
            horizontal=AngleDomain(
                minimum_deg=self.horizontal_angles_deg[0],
                maximum_deg=self.horizontal_angles_deg[-1],
            ),
            vertical=AngleDomain(
                minimum_deg=self.vertical_angles_deg[0],
                maximum_deg=self.vertical_angles_deg[-1],
            ),
        )
        if self.valid_domain != expected_domain:
            raise ValueError('dataset valid domain must exactly match its grid extent')

        sample_map: dict[tuple[float, float, float], DirectivitySample] = {}
        for sample in self.samples:
            key = (
                sample.frequency_hz,
                sample.horizontal_angle_deg,
                sample.vertical_angle_deg,
            )
            if key in sample_map:
                raise ValueError('directivity dataset contains duplicate sample coordinates')
            sample_map[key] = sample

        expected_keys = set(
            product(
                self.frequencies_hz,
                self.horizontal_angles_deg,
                self.vertical_angles_deg,
            )
        )
        if set(sample_map) != expected_keys:
            raise ValueError(
                'directivity dataset grid is incomplete or contains off-grid samples'
            )

        if self.normalization.reference == 'on_axis_per_frequency':
            if (
                0.0 not in self.horizontal_angles_deg
                or 0.0 not in self.vertical_angles_deg
            ):
                raise ValueError(
                    'on-axis normalization requires horizontal=0 and vertical=0 samples'
                )
            for frequency_hz in self.frequencies_hz:
                on_axis = sample_map[(frequency_hz, 0.0, 0.0)]
                if abs(on_axis.magnitude_db) > 1e-9:
                    raise ValueError(
                        'on-axis normalized dataset must be 0 dB on axis at each frequency'
                    )

        expected_grid_hash = _digest(
            {
                'frequencies_hz': list(self.frequencies_hz),
                'horizontal_angles_deg': list(self.horizontal_angles_deg),
                'vertical_angles_deg': list(self.vertical_angles_deg),
                'coordinate_convention': self.coordinate_convention.model_dump(
                    mode='json'
                ),
            }
        )
        if self.grid_sha256 != expected_grid_hash:
            raise ValueError('directivity dataset grid hash mismatch')

        expected_sample_hash = _digest(
            [sample.model_dump(mode='json') for sample in self.samples]
        )
        if self.sample_sha256 != expected_sample_hash:
            raise ValueError('directivity dataset sample hash mismatch')

        payload = self.model_dump(mode='json')
        semantic_sha256 = payload.pop('semantic_sha256')
        if semantic_sha256 != _digest(payload):
            raise ValueError('DirectivityDataset semantic hash mismatch')
        return self


def build_directivity_dataset(
    *,
    dataset_id: str,
    version: str,
    definition: EquipmentDefinition,
    source_asset_sha256: str,
    source_format: DirectivityDataFormat,
    parser_id: str,
    parser_version: str,
    adapter_id: str,
    adapter_version: str,
    evidence_kind: EquipmentEvidenceKind,
    source_provenance: EquipmentDataProvenance,
    kind: DirectivityDatasetKind,
    coordinate_convention: DirectivityCoordinateConvention,
    normalization: DirectivityNormalization,
    frequencies_hz: Sequence[float],
    horizontal_angles_deg: Sequence[float],
    vertical_angles_deg: Sequence[float],
    samples: Sequence[DirectivitySample],
    interpolation: InterpolationProvenance,
    phase_reference: str | None = None,
) -> DirectivityDataset:
    frequencies = _validate_axis(
        frequencies_hz,
        field_name='frequency grid',
        positive=True,
        minimum_length=2,
    )
    horizontal = _validate_axis(
        horizontal_angles_deg,
        field_name='horizontal angle grid',
    )
    vertical = _validate_axis(
        vertical_angles_deg,
        field_name='vertical angle grid',
    )
    sample_items = tuple(samples)

    valid_domain = DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=frequencies[0],
            maximum_hz=frequencies[-1],
        ),
        horizontal=AngleDomain(
            minimum_deg=horizontal[0],
            maximum_deg=horizontal[-1],
        ),
        vertical=AngleDomain(
            minimum_deg=vertical[0],
            maximum_deg=vertical[-1],
        ),
    )
    grid_sha256 = _digest(
        {
            'frequencies_hz': list(frequencies),
            'horizontal_angles_deg': list(horizontal),
            'vertical_angles_deg': list(vertical),
            'coordinate_convention': coordinate_convention.model_dump(mode='json'),
        }
    )
    sample_sha256 = _digest(
        [sample.model_dump(mode='json') for sample in sample_items]
    )
    payload: dict[str, Any] = {
        'schema_version': DIRECTIVITY_DATASET_SCHEMA_VERSION,
        'authority_version': DIRECTIVITY_DATASET_AUTHORITY_VERSION,
        'dataset_id': dataset_id,
        'version': version,
        'equipment_definition_id': definition.definition_id,
        'equipment_definition_version': definition.version,
        'equipment_definition_sha256': definition.semantic_sha256,
        'source_asset_sha256': source_asset_sha256,
        'source_format': source_format,
        'container_format': NORMALIZED_JSON_FORMAT,
        'parser_id': parser_id,
        'parser_version': parser_version,
        'adapter_id': adapter_id,
        'adapter_version': adapter_version,
        'evidence_kind': evidence_kind,
        'source_provenance': source_provenance.model_dump(mode='json'),
        'kind': kind,
        'coordinate_convention': coordinate_convention.model_dump(mode='json'),
        'normalization': normalization.model_dump(mode='json'),
        'phase_reference': phase_reference,
        'frequencies_hz': list(frequencies),
        'horizontal_angles_deg': list(horizontal),
        'vertical_angles_deg': list(vertical),
        'samples': [sample.model_dump(mode='json') for sample in sample_items],
        'valid_domain': valid_domain.model_dump(mode='json'),
        'interpolation': interpolation.model_dump(mode='json'),
        'grid_sha256': grid_sha256,
        'sample_sha256': sample_sha256,
    }
    dataset = DirectivityDataset(
        **payload,
        semantic_sha256=_digest(payload),
    )
    validate_directivity_dataset_binding(dataset, definition)
    return dataset


def validate_directivity_dataset_binding(
    dataset: DirectivityDataset,
    definition: EquipmentDefinition,
) -> None:
    if (
        dataset.equipment_definition_id != definition.definition_id
        or dataset.equipment_definition_version != definition.version
        or dataset.equipment_definition_sha256 != definition.semantic_sha256
    ):
        raise ValueError('DirectivityDataset EquipmentDefinition hash mismatch')

    capability = definition.directivity
    if capability.tier != dataset.kind:
        raise ValueError(
            'DirectivityDataset kind does not match EquipmentDefinition directivity tier'
        )
    if capability.data_asset_sha256 != dataset.source_asset_sha256:
        raise ValueError(
            'DirectivityDataset source asset hash does not match EquipmentDefinition'
        )
    if capability.data_format != dataset.source_format:
        raise ValueError(
            'DirectivityDataset source format does not match EquipmentDefinition'
        )
    if capability.valid_domain != dataset.valid_domain:
        raise ValueError(
            'DirectivityDataset domain does not match EquipmentDefinition directivity domain'
        )
    if capability.interpolation is None:
        raise ValueError(
            'EquipmentDefinition directivity interpolation provenance is missing'
        )
    if (
        capability.interpolation.method != dataset.interpolation.method
        or capability.interpolation.implementation
        != dataset.interpolation.implementation
        or capability.interpolation.implementation_version
        != dataset.interpolation.implementation_version
    ):
        raise ValueError(
            'DirectivityDataset interpolation authority does not match EquipmentDefinition'
        )
    if dataset.kind == 'complex':
        if (
            not capability.coherent_phase
            or capability.phase_reference != dataset.phase_reference
        ):
            raise ValueError(
                'complex DirectivityDataset phase reference does not match EquipmentDefinition'
            )


class DirectivitySourceSampleV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0.0)
    horizontal_angle_deg: float
    vertical_angle_deg: float
    magnitude: float
    phase_deg: float | None = None

    @field_validator(
        'frequency_hz',
        'horizontal_angle_deg',
        'vertical_angle_deg',
        'magnitude',
        'phase_deg',
    )
    @classmethod
    def finite_values(
        cls,
        value: float | None,
    ) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='source directivity sample value')


class NormalizedDirectivityJsonV1(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra='forbid',
        populate_by_name=True,
    )

    schema_id: Literal[
        'htdt.normalized-directivity.v1'
    ] = Field(
        default='htdt.normalized-directivity.v1',
        alias='schema',
    )
    dataset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_format: DirectivityDataFormat
    evidence_kind: EquipmentEvidenceKind
    source_name: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    kind: DirectivityDatasetKind
    coordinate_convention: DirectivityCoordinateConvention
    normalization: DirectivityNormalization
    phase_reference: str | None = Field(default=None, min_length=1)
    interpolation_method: InterpolationMethod
    interpolation_implementation: str = Field(min_length=1)
    interpolation_version: str = Field(min_length=1)
    frequencies_hz: tuple[float, ...] = Field(min_length=2)
    horizontal_angles_deg: tuple[float, ...] = Field(min_length=1)
    vertical_angles_deg: tuple[float, ...] = Field(min_length=1)
    samples: tuple[DirectivitySourceSampleV1, ...] = Field(min_length=1)

    @field_validator('frequencies_hz')
    @classmethod
    def valid_frequency_grid(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        return _validate_axis(
            values,
            field_name='source frequency grid',
            positive=True,
            minimum_length=2,
        )

    @field_validator('horizontal_angles_deg')
    @classmethod
    def valid_horizontal_grid(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        return _validate_axis(
            values,
            field_name='source horizontal angle grid',
        )

    @field_validator('vertical_angles_deg')
    @classmethod
    def valid_vertical_grid(
        cls,
        values: tuple[float, ...],
    ) -> tuple[float, ...]:
        return _validate_axis(
            values,
            field_name='source vertical angle grid',
        )

    @model_validator(mode='after')
    def valid_phase_shape(self) -> 'NormalizedDirectivityJsonV1':
        if self.kind == 'complex':
            if self.phase_reference is None:
                raise ValueError(
                    'complex normalized source requires explicit phase reference'
                )
            if any(sample.phase_deg is None for sample in self.samples):
                raise ValueError(
                    'complex normalized source requires phase for every sample'
                )
        else:
            if self.phase_reference is not None:
                raise ValueError(
                    'magnitude-only normalized source must not declare phase reference'
                )
            if any(sample.phase_deg is not None for sample in self.samples):
                raise ValueError(
                    'magnitude-only normalized source must not contain phase'
                )
        return self


class DirectivityImportAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def parse(
        self,
        source_bytes: bytes,
        definition: EquipmentDefinition,
    ) -> DirectivityDataset:
        ...


class NormalizedJsonDirectivityAdapter:
    adapter_id = NORMALIZED_JSON_ADAPTER_ID
    adapter_version = NORMALIZED_JSON_ADAPTER_VERSION

    def parse(
        self,
        source_bytes: bytes,
        definition: EquipmentDefinition,
    ) -> DirectivityDataset:
        source_asset_sha256 = sha256(source_bytes).hexdigest()
        try:
            source_text = source_bytes.decode('utf-8', errors='strict')
            raw = json.loads(source_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('malformed normalized directivity JSON source') from exc

        source = NormalizedDirectivityJsonV1.model_validate(raw)
        if source.source_format != 'custom':
            raise ValueError(
                'normalized JSON adapter accepts only custom normalized source format; '
                'native CLF/CF2/SOFA/AES69 require dedicated adapters'
            )
        if source.interpolation_method not in {
            'none',
            'nearest',
            'linear',
            'log_frequency_linear_angle',
        }:
            raise ValueError(
                'normalized directivity adapter does not implement requested interpolation'
            )

        provenance = EquipmentDataProvenance(
            evidence_kind=source.evidence_kind,
            source_name=source.source_name,
            source_version=source.source_version,
            source_reference=source.source_reference,
            source_sha256=source_asset_sha256,
        )
        interpolation = InterpolationProvenance(
            method=source.interpolation_method,
            implementation=source.interpolation_implementation,
            implementation_version=source.interpolation_version,
            provenance=provenance,
        )

        samples: list[DirectivitySample] = []
        for sample in source.samples:
            if source.normalization.source_magnitude_unit == 'db':
                magnitude_db = sample.magnitude
            else:
                if sample.magnitude <= 0.0:
                    raise ValueError(
                        'linear directivity magnitude must be positive for dB conversion'
                    )
                magnitude_db = 20.0 * log10(sample.magnitude)
            samples.append(
                DirectivitySample(
                    frequency_hz=sample.frequency_hz,
                    horizontal_angle_deg=sample.horizontal_angle_deg,
                    vertical_angle_deg=sample.vertical_angle_deg,
                    magnitude_db=magnitude_db,
                    phase_deg=sample.phase_deg,
                )
            )

        return build_directivity_dataset(
            dataset_id=source.dataset_id,
            version=source.version,
            definition=definition,
            source_asset_sha256=source_asset_sha256,
            source_format=source.source_format,
            parser_id=NORMALIZED_JSON_PARSER_ID,
            parser_version=NORMALIZED_JSON_PARSER_VERSION,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            evidence_kind=source.evidence_kind,
            source_provenance=provenance,
            kind=source.kind,
            coordinate_convention=source.coordinate_convention,
            normalization=source.normalization,
            frequencies_hz=source.frequencies_hz,
            horizontal_angles_deg=source.horizontal_angles_deg,
            vertical_angles_deg=source.vertical_angles_deg,
            samples=samples,
            interpolation=interpolation,
            phase_reference=source.phase_reference,
        )


NORMALIZED_JSON_DIRECTIVITY_ADAPTER = NormalizedJsonDirectivityAdapter()


class DirectivityEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'o100c-directivity-evaluation-1'
    ] = DIRECTIVITY_EVALUATION_AUTHORITY_VERSION
    decision: DirectivityEvaluationDecision
    request: DirectivityEvaluationRequest

    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    dataset_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    requested_frequency_hz: float
    requested_horizontal_angle_deg: float
    requested_vertical_angle_deg: float
    evaluated_horizontal_angle_deg: float

    interpolation_applied: bool
    interpolation_method: InterpolationMethod
    interpolation_implementation: str = Field(min_length=1)
    interpolation_version: str = Field(min_length=1)
    conversion_version: Literal[
        'pressure-amplitude-db20-v1'
    ] = DIRECTIVITY_DB_CONVERSION_VERSION

    magnitude_db: float | None = None
    magnitude_linear: float | None = None
    phase_deg: float | None = None
    complex_real: float | None = None
    complex_imag: float | None = None
    supporting_sample_sha256: tuple[str, ...] = ()
    reasons: tuple[str, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator(
        'requested_frequency_hz',
        'requested_horizontal_angle_deg',
        'requested_vertical_angle_deg',
        'evaluated_horizontal_angle_deg',
        'magnitude_db',
        'magnitude_linear',
        'phase_deg',
        'complex_real',
        'complex_imag',
    )
    @classmethod
    def finite_values(
        cls,
        value: float | None,
    ) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='directivity evaluation value')

    @model_validator(mode='after')
    def valid_result(self) -> 'DirectivityEvaluationResult':
        if self.decision == 'SUPPORTED':
            if self.magnitude_db is None or self.magnitude_linear is None:
                raise ValueError(
                    'supported directivity evaluation requires magnitude result'
                )
            if not self.supporting_sample_sha256:
                raise ValueError(
                    'supported directivity evaluation requires supporting samples'
                )
            if self.request == 'complex':
                if (
                    self.phase_deg is None
                    or self.complex_real is None
                    or self.complex_imag is None
                ):
                    raise ValueError(
                        'supported complex evaluation requires complex result values'
                    )
        else:
            if any(
                value is not None
                for value in (
                    self.magnitude_db,
                    self.magnitude_linear,
                    self.phase_deg,
                    self.complex_real,
                    self.complex_imag,
                )
            ):
                raise ValueError(
                    'unsupported directivity evaluation must not carry fabricated values'
                )

        payload = self.model_dump(mode='json')
        semantic_sha256 = payload.pop('semantic_sha256')
        if semantic_sha256 != _digest(payload):
            raise ValueError('DirectivityEvaluationResult semantic hash mismatch')
        return self


def _wrap_horizontal(
    dataset: DirectivityDataset,
    angle_deg: float,
) -> float:
    angle = _finite(angle_deg, field_name='horizontal_angle_deg')
    if dataset.coordinate_convention.horizontal_wrap == 'none':
        return angle
    wrapped = ((angle + 180.0) % 360.0) - 180.0
    return 0.0 if wrapped == 0.0 else wrapped


def _sample_map(
    dataset: DirectivityDataset,
) -> dict[tuple[float, float, float], DirectivitySample]:
    return {
        (
            sample.frequency_hz,
            sample.horizontal_angle_deg,
            sample.vertical_angle_deg,
        ): sample
        for sample in dataset.samples
    }


def _sample_hash(sample: DirectivitySample) -> str:
    return _digest(sample.model_dump(mode='json'))


def _bracket(
    axis: tuple[float, ...],
    value: float,
    *,
    log_axis: bool = False,
) -> tuple[tuple[float, float], ...]:
    index = bisect_left(axis, value)
    if index < len(axis) and axis[index] == value:
        return ((value, 1.0),)
    if index == 0 or index == len(axis):
        raise ValueError('requested value is outside interpolation axis')
    low = axis[index - 1]
    high = axis[index]
    if log_axis:
        weight_high = (log(value) - log(low)) / (log(high) - log(low))
    else:
        weight_high = (value - low) / (high - low)
    return (
        (low, 1.0 - weight_high),
        (high, weight_high),
    )


def _nearest(axis: tuple[float, ...], value: float) -> float:
    return min(axis, key=lambda candidate: (abs(candidate - value), candidate))


def _make_evaluation_result(
    *,
    dataset: DirectivityDataset,
    decision: DirectivityEvaluationDecision,
    request: DirectivityEvaluationRequest,
    requested_frequency_hz: float,
    requested_horizontal_angle_deg: float,
    requested_vertical_angle_deg: float,
    evaluated_horizontal_angle_deg: float,
    interpolation_applied: bool,
    magnitude_db: float | None,
    magnitude_linear: float | None,
    phase_deg: float | None,
    complex_real: float | None,
    complex_imag: float | None,
    supporting_sample_sha256: Sequence[str],
    reasons: Sequence[str],
) -> DirectivityEvaluationResult:
    payload: dict[str, Any] = {
        'authority_version': DIRECTIVITY_EVALUATION_AUTHORITY_VERSION,
        'decision': decision,
        'request': request,
        'dataset_id': dataset.dataset_id,
        'dataset_version': dataset.version,
        'dataset_semantic_sha256': dataset.semantic_sha256,
        'equipment_definition_sha256': dataset.equipment_definition_sha256,
        'requested_frequency_hz': requested_frequency_hz,
        'requested_horizontal_angle_deg': requested_horizontal_angle_deg,
        'requested_vertical_angle_deg': requested_vertical_angle_deg,
        'evaluated_horizontal_angle_deg': evaluated_horizontal_angle_deg,
        'interpolation_applied': interpolation_applied,
        'interpolation_method': dataset.interpolation.method,
        'interpolation_implementation': dataset.interpolation.implementation,
        'interpolation_version': dataset.interpolation.implementation_version,
        'conversion_version': DIRECTIVITY_DB_CONVERSION_VERSION,
        'magnitude_db': magnitude_db,
        'magnitude_linear': magnitude_linear,
        'phase_deg': phase_deg,
        'complex_real': complex_real,
        'complex_imag': complex_imag,
        'supporting_sample_sha256': list(supporting_sample_sha256),
        'reasons': list(reasons),
    }
    return DirectivityEvaluationResult(
        **payload,
        semantic_sha256=_digest(payload),
    )


def evaluate_directivity(
    dataset: DirectivityDataset,
    *,
    frequency_hz: float,
    horizontal_angle_deg: float,
    vertical_angle_deg: float,
    request: DirectivityEvaluationRequest = 'magnitude',
) -> DirectivityEvaluationResult:
    frequency = _finite(frequency_hz, field_name='frequency_hz')
    requested_horizontal = _finite(
        horizontal_angle_deg,
        field_name='horizontal_angle_deg',
    )
    vertical = _finite(vertical_angle_deg, field_name='vertical_angle_deg')
    horizontal = _wrap_horizontal(dataset, requested_horizontal)

    def unsupported(reason: str) -> DirectivityEvaluationResult:
        return _make_evaluation_result(
            dataset=dataset,
            decision='UNSUPPORTED',
            request=request,
            requested_frequency_hz=frequency,
            requested_horizontal_angle_deg=requested_horizontal,
            requested_vertical_angle_deg=vertical,
            evaluated_horizontal_angle_deg=horizontal,
            interpolation_applied=False,
            magnitude_db=None,
            magnitude_linear=None,
            phase_deg=None,
            complex_real=None,
            complex_imag=None,
            supporting_sample_sha256=(),
            reasons=(reason,),
        )

    if request == 'complex' and dataset.kind != 'complex':
        return unsupported(
            'magnitude-only dataset does not provide coherent complex directivity'
        )

    if not dataset.valid_domain.frequency.contains(frequency):
        return unsupported('requested frequency is outside dataset domain')
    if not dataset.valid_domain.horizontal.contains(horizontal):
        return unsupported('requested horizontal/azimuth angle is outside dataset domain')
    if not dataset.valid_domain.vertical.contains(vertical):
        return unsupported('requested vertical/elevation angle is outside dataset domain')

    samples = _sample_map(dataset)
    exact_key = (frequency, horizontal, vertical)
    exact = samples.get(exact_key)
    if exact is not None:
        magnitude_linear = 10.0 ** (exact.magnitude_db / 20.0)
        phase_deg: float | None = None
        complex_real: float | None = None
        complex_imag: float | None = None
        if request == 'complex':
            assert exact.phase_deg is not None
            phase_deg = exact.phase_deg
            angle_rad = radians(phase_deg)
            complex_real = magnitude_linear * cos(angle_rad)
            complex_imag = magnitude_linear * sin(angle_rad)
        return _make_evaluation_result(
            dataset=dataset,
            decision='SUPPORTED',
            request=request,
            requested_frequency_hz=frequency,
            requested_horizontal_angle_deg=requested_horizontal,
            requested_vertical_angle_deg=vertical,
            evaluated_horizontal_angle_deg=horizontal,
            interpolation_applied=False,
            magnitude_db=exact.magnitude_db,
            magnitude_linear=magnitude_linear,
            phase_deg=phase_deg,
            complex_real=complex_real,
            complex_imag=complex_imag,
            supporting_sample_sha256=(_sample_hash(exact),),
            reasons=('exact on-grid directivity sample',),
        )

    method = dataset.interpolation.method
    if method == 'none':
        return unsupported(
            'requested point is off-grid and dataset forbids interpolation'
        )
    if method not in {
        'nearest',
        'linear',
        'log_frequency_linear_angle',
    }:
        return unsupported(
            f'interpolation method {method!r} is not implemented by this evaluator'
        )

    if method == 'nearest':
        support_key = (
            _nearest(dataset.frequencies_hz, frequency),
            _nearest(dataset.horizontal_angles_deg, horizontal),
            _nearest(dataset.vertical_angles_deg, vertical),
        )
        sample = samples.get(support_key)
        if sample is None:
            return unsupported(
                'required nearest sample is missing from dataset'
            )
        weighted_samples = ((sample, 1.0),)
    else:
        frequency_support = _bracket(
            dataset.frequencies_hz,
            frequency,
            log_axis=(method == 'log_frequency_linear_angle'),
        )
        horizontal_support = _bracket(
            dataset.horizontal_angles_deg,
            horizontal,
        )
        vertical_support = _bracket(
            dataset.vertical_angles_deg,
            vertical,
        )
        weighted: list[tuple[DirectivitySample, float]] = []
        for (
            (sample_frequency, frequency_weight),
            (sample_horizontal, horizontal_weight),
            (sample_vertical, vertical_weight),
        ) in product(
            frequency_support,
            horizontal_support,
            vertical_support,
        ):
            sample = samples.get(
                (
                    sample_frequency,
                    sample_horizontal,
                    sample_vertical,
                )
            )
            if sample is None:
                return unsupported(
                    'required interpolation sample is missing from dataset'
                )
            weighted.append(
                (
                    sample,
                    frequency_weight
                    * horizontal_weight
                    * vertical_weight,
                )
            )
        weighted_samples = tuple(weighted)

    supporting_hashes = tuple(
        _sample_hash(sample)
        for sample, _weight in weighted_samples
    )

    if dataset.kind == 'magnitude_only':
        magnitude_db = sum(
            weight * sample.magnitude_db
            for sample, weight in weighted_samples
        )
        magnitude_linear = 10.0 ** (magnitude_db / 20.0)
        return _make_evaluation_result(
            dataset=dataset,
            decision='SUPPORTED',
            request=request,
            requested_frequency_hz=frequency,
            requested_horizontal_angle_deg=requested_horizontal,
            requested_vertical_angle_deg=vertical,
            evaluated_horizontal_angle_deg=horizontal,
            interpolation_applied=True,
            magnitude_db=magnitude_db,
            magnitude_linear=magnitude_linear,
            phase_deg=None,
            complex_real=None,
            complex_imag=None,
            supporting_sample_sha256=supporting_hashes,
            reasons=(
                'interpolated inside declared dataset domain without extrapolation',
            ),
        )

    complex_value = 0j
    for sample, weight in weighted_samples:
        if sample.phase_deg is None:
            return unsupported(
                'required complex interpolation sample lacks phase'
            )
        magnitude = 10.0 ** (sample.magnitude_db / 20.0)
        angle_rad = radians(sample.phase_deg)
        complex_value += weight * complex(
            magnitude * cos(angle_rad),
            magnitude * sin(angle_rad),
        )

    magnitude_linear = abs(complex_value)
    if magnitude_linear <= 0.0:
        return unsupported(
            'complex interpolation produced zero magnitude with no finite dB representation'
        )
    magnitude_db = 20.0 * log10(magnitude_linear)
    phase_deg = atan2(complex_value.imag, complex_value.real) * 180.0 / pi

    return _make_evaluation_result(
        dataset=dataset,
        decision='SUPPORTED',
        request=request,
        requested_frequency_hz=frequency,
        requested_horizontal_angle_deg=requested_horizontal,
        requested_vertical_angle_deg=vertical,
        evaluated_horizontal_angle_deg=horizontal,
        interpolation_applied=True,
        magnitude_db=magnitude_db,
        magnitude_linear=magnitude_linear,
        phase_deg=phase_deg if request == 'complex' else None,
        complex_real=complex_value.real if request == 'complex' else None,
        complex_imag=complex_value.imag if request == 'complex' else None,
        supporting_sample_sha256=supporting_hashes,
        reasons=(
            'interpolated inside declared dataset domain without extrapolation',
        ),
    )
