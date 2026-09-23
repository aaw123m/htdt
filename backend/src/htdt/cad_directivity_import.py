from __future__ import annotations

import csv
from hashlib import sha256
import io
from math import isfinite, log10
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_directivity import (
    NORMALIZED_JSON_ADAPTER_ID,
    NORMALIZED_JSON_ADAPTER_VERSION,
    NORMALIZED_JSON_DIRECTIVITY_ADAPTER,
    NORMALIZED_JSON_PARSER_ID,
    NORMALIZED_JSON_PARSER_VERSION,
    DirectivityCoordinateConvention,
    DirectivityDataset,
    DirectivityNormalization,
    DirectivitySample,
    build_directivity_dataset,
    validate_directivity_dataset_binding,
)
from .cad_equipment import (
    DirectivityDataFormat,
    DirectivityDomain,
    EquipmentDataProvenance,
    EquipmentDefinition,
    EquipmentEvidenceKind,
    InterpolationMethod,
    InterpolationProvenance,
)


DIRECTIVITY_IMPORT_REGISTRY_ID = 'htdt.directivity-import-adapters'
DIRECTIVITY_IMPORT_REGISTRY_VERSION = '1'
DIRECTIVITY_IMPORT_AUTHORITY_VERSION = 'issue168-directivity-import-1'

POLAR_TABLE_SCHEMA = 'htdt.polar-table.v1'
POLAR_TABLE_ADAPTER_ID = 'htdt.polar-table-adapter'
POLAR_TABLE_ADAPTER_VERSION = '1'
POLAR_TABLE_PARSER_ID = 'htdt.polar-table-parser'
POLAR_TABLE_PARSER_VERSION = '1'

ImportCapability = Literal['magnitude_only', 'complex']
ImportState = Literal['IMPORTED', 'UNSUPPORTED', 'REJECTED']
AdapterSupportState = Literal['SUPPORTED', 'DEFERRED']


class DirectivityAdapterDescriptor(BaseModel):
    """Versioned registry record; this is metadata, not a heuristic detector."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    accepted_source_format: DirectivityDataFormat
    accepted_schema: str = Field(min_length=1)
    parser_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    supported_capabilities: tuple[ImportCapability, ...]
    coordinate_semantics: tuple[
        Literal['horizontal_vertical', 'spherical_azimuth_elevation'], ...
    ]
    unit_semantics: tuple[Literal['db', 'linear'], ...]
    strict_validation: Literal[True] = True
    deterministic_normalized_output: Literal[True] = True
    parser_semantics: str = Field(min_length=1)
    support_state: AdapterSupportState
    deferred_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_support_state(self) -> 'DirectivityAdapterDescriptor':
        if self.support_state == 'SUPPORTED':
            if self.deferred_reason is not None:
                raise ValueError('supported adapter must not carry deferred_reason')
            if not self.supported_capabilities:
                raise ValueError('supported adapter must declare capabilities')
        elif self.deferred_reason is None:
            raise ValueError('deferred adapter requires explicit deferred_reason')
        return self


class DirectivityAdapterRegistryAuthority(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    registry_id: Literal[
        'htdt.directivity-import-adapters'
    ] = DIRECTIVITY_IMPORT_REGISTRY_ID
    registry_version: Literal['1'] = DIRECTIVITY_IMPORT_REGISTRY_VERSION
    adapters: tuple[DirectivityAdapterDescriptor, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_adapters(self) -> 'DirectivityAdapterRegistryAuthority':
        identities = [
            (
                item.adapter_id,
                item.adapter_version,
                item.accepted_source_format,
                item.accepted_schema,
            )
            for item in self.adapters
        ]
        if len(identities) != len(set(identities)):
            raise ValueError('directivity adapter registry identities must be unique')
        return self

    def resolve_supported(
        self,
        *,
        source_format: DirectivityDataFormat,
        declared_schema: str,
        adapter_id: str,
        adapter_version: str,
    ) -> DirectivityAdapterDescriptor | None:
        matches = [
            item
            for item in self.adapters
            if item.support_state == 'SUPPORTED'
            and item.accepted_source_format == source_format
            and item.accepted_schema == declared_schema
            and item.adapter_id == adapter_id
            and item.adapter_version == adapter_version
        ]
        if len(matches) > 1:
            raise ValueError('adapter registry contains an ambiguous supported adapter')
        return None if not matches else matches[0]

    def deferred_for_format(
        self,
        source_format: DirectivityDataFormat,
    ) -> DirectivityAdapterDescriptor | None:
        matches = [
            item
            for item in self.adapters
            if item.support_state == 'DEFERRED'
            and item.accepted_source_format == source_format
        ]
        if len(matches) > 1:
            raise ValueError('adapter registry contains ambiguous deferred format entries')
        return None if not matches else matches[0]


DIRECTIVITY_ADAPTER_REGISTRY = DirectivityAdapterRegistryAuthority(
    adapters=(
        DirectivityAdapterDescriptor(
            adapter_id=NORMALIZED_JSON_ADAPTER_ID,
            adapter_version=NORMALIZED_JSON_ADAPTER_VERSION,
            accepted_source_format='custom',
            accepted_schema='htdt.normalized-directivity.v1',
            parser_id=NORMALIZED_JSON_PARSER_ID,
            parser_version=NORMALIZED_JSON_PARSER_VERSION,
            supported_capabilities=('magnitude_only', 'complex'),
            coordinate_semantics=(
                'horizontal_vertical',
                'spherical_azimuth_elevation',
            ),
            unit_semantics=('db', 'linear'),
            parser_semantics=(
                'Exact existing HTDT normalized-directivity JSON v1; no native-format '
                'detection or schema guessing.'
            ),
            support_state='SUPPORTED',
        ),
        DirectivityAdapterDescriptor(
            adapter_id=POLAR_TABLE_ADAPTER_ID,
            adapter_version=POLAR_TABLE_ADAPTER_VERSION,
            accepted_source_format='polar_table',
            accepted_schema=POLAR_TABLE_SCHEMA,
            parser_id=POLAR_TABLE_PARSER_ID,
            parser_version=POLAR_TABLE_PARSER_VERSION,
            supported_capabilities=('magnitude_only', 'complex'),
            coordinate_semantics=(
                'horizontal_vertical',
                'spherical_azimuth_elevation',
            ),
            unit_semantics=('db', 'linear'),
            parser_semantics=(
                'UTF-8 metadata-header CSV/TSV with exact versioned columns, '
                'explicit coordinate/unit/normalization/phase semantics, and a '
                'complete rectangular grid.'
            ),
            support_state='SUPPORTED',
        ),
        DirectivityAdapterDescriptor(
            adapter_id='htdt.clf-native-deferred',
            adapter_version='1',
            accepted_source_format='clf',
            accepted_schema='native-clf-deferred',
            parser_id='unsupported',
            parser_version='0',
            supported_capabilities=(),
            coordinate_semantics=(),
            unit_semantics=(),
            parser_semantics='No guessed CLF field/version decoding is authorized.',
            support_state='DEFERRED',
            deferred_reason=(
                'Native CLF parsing is deferred until an exact public/official format '
                'specification and version semantics are implemented without heuristics.'
            ),
        ),
        DirectivityAdapterDescriptor(
            adapter_id='htdt.cf2-native-deferred',
            adapter_version='1',
            accepted_source_format='cf2',
            accepted_schema='native-cf2-deferred',
            parser_id='unsupported',
            parser_version='0',
            supported_capabilities=(),
            coordinate_semantics=(),
            unit_semantics=(),
            parser_semantics='No guessed CF2 field/version decoding is authorized.',
            support_state='DEFERRED',
            deferred_reason=(
                'Native CF2 parsing is deferred until exact format/version semantics '
                'can be implemented without inference.'
            ),
        ),
        DirectivityAdapterDescriptor(
            adapter_id='htdt.sofa-aes69-deferred',
            adapter_version='1',
            accepted_source_format='sofa_aes69',
            accepted_schema='sofa-aes69-deferred',
            parser_id='unsupported',
            parser_version='0',
            supported_capabilities=(),
            coordinate_semantics=(),
            unit_semantics=(),
            parser_semantics='SOFA/AES69 is outside this slice; no HDF5 dependency is added.',
            support_state='DEFERRED',
            deferred_reason=(
                'SOFA/AES69 remains a future adapter and does not add an HDF5/SOFA '
                'production dependency in this slice.'
            ),
        ),
        DirectivityAdapterDescriptor(
            adapter_id='htdt.cta2034-summary-deferred',
            adapter_version='1',
            accepted_source_format='cta2034_summary',
            accepted_schema='cta2034-summary-deferred',
            parser_id='unsupported',
            parser_version='0',
            supported_capabilities=(),
            coordinate_semantics=(),
            unit_semantics=(),
            parser_semantics=(
                'Summary curves are not silently promoted to an arbitrary-angle '
                'DirectivityDataset.'
            ),
            support_state='DEFERRED',
            deferred_reason=(
                'CTA-2034/spinorama summary data requires a separate polar_summary '
                'authority and is not promoted to a full spherical field.'
            ),
        ),
    )
)


class DirectivityImportDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'issue168-directivity-import-1'
    ] = DIRECTIVITY_IMPORT_AUTHORITY_VERSION
    import_state: ImportState
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_format: DirectivityDataFormat
    declared_schema: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    parser_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    domain: DirectivityDomain | None = None
    capability: ImportCapability | None = None
    warnings: tuple[str, ...] = ()
    rejection_reason: str | None = Field(default=None, min_length=1)
    normalized_dataset_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @model_validator(mode='after')
    def valid_state(self) -> 'DirectivityImportDiagnostic':
        if self.import_state == 'IMPORTED':
            if self.rejection_reason is not None:
                raise ValueError('imported diagnostic must not carry rejection_reason')
            if (
                self.sample_count <= 0
                or self.domain is None
                or self.capability is None
                or self.normalized_dataset_sha256 is None
            ):
                raise ValueError('imported diagnostic requires normalized dataset details')
        else:
            if self.rejection_reason is None:
                raise ValueError('non-imported diagnostic requires rejection_reason')
            if self.normalized_dataset_sha256 is not None:
                raise ValueError('failed import must not claim a normalized dataset hash')
        return self


class DirectivityImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    dataset: DirectivityDataset | None = None
    diagnostic: DirectivityImportDiagnostic

    @model_validator(mode='after')
    def valid_result(self) -> 'DirectivityImportResult':
        if self.diagnostic.import_state == 'IMPORTED':
            if self.dataset is None:
                raise ValueError('successful directivity import requires a dataset')
            if self.dataset.semantic_sha256 != self.diagnostic.normalized_dataset_sha256:
                raise ValueError('import diagnostic dataset hash mismatch')
        elif self.dataset is not None:
            raise ValueError('failed directivity import must not carry a dataset')
        return self


class DirectivityAssetAdapter(Protocol):
    descriptor: DirectivityAdapterDescriptor

    def parse(
        self,
        source_bytes: bytes,
        definition: EquipmentDefinition,
    ) -> DirectivityDataset:
        ...


class NormalizedJsonAssetAdapter:
    descriptor = DIRECTIVITY_ADAPTER_REGISTRY.resolve_supported(
        source_format='custom',
        declared_schema='htdt.normalized-directivity.v1',
        adapter_id=NORMALIZED_JSON_ADAPTER_ID,
        adapter_version=NORMALIZED_JSON_ADAPTER_VERSION,
    )
    if descriptor is None:  # pragma: no cover - registry construction invariant
        raise RuntimeError('normalized JSON adapter descriptor is missing')

    def parse(
        self,
        source_bytes: bytes,
        definition: EquipmentDefinition,
    ) -> DirectivityDataset:
        return NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(source_bytes, definition)


def _required_metadata() -> set[str]:
    return {
        'schema',
        'delimiter',
        'dataset_id',
        'dataset_version',
        'capability',
        'angle_semantics',
        'horizontal_wrap',
        'reference_axis',
        'azimuth_positive',
        'elevation_positive',
        'frequency_unit',
        'angle_unit',
        'magnitude_unit',
        'normalization_reference',
        'interpolation_method',
        'interpolation_implementation',
        'interpolation_version',
        'evidence_kind',
        'source_name',
        'source_version',
        'source_reference',
    }


_ALLOWED_OPTIONAL_METADATA = {'reference_level_db', 'phase_reference'}


def _parse_metadata_and_rows(source_bytes: bytes) -> tuple[dict[str, str], list[str]]:
    try:
        text = source_bytes.decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        raise ValueError('polar_table source must be strict UTF-8') from exc
    lines = text.splitlines()
    if not lines or not lines[0].startswith('# '):
        raise ValueError('polar_table must start with explicit metadata headers')

    metadata: dict[str, str] = {}
    row_start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith('# '):
            item = line[2:]
            if '=' not in item:
                raise ValueError('polar_table metadata line must use "# key=value"')
            key, value = item.split('=', 1)
            if not key or not value or key.strip() != key or value.strip() != value:
                raise ValueError('polar_table metadata keys/values must be exact and non-empty')
            if key in metadata:
                raise ValueError(f'duplicate polar_table metadata field: {key}')
            metadata[key] = value
            continue
        row_start = index
        break
    if row_start is None:
        raise ValueError('polar_table has no tabular header/data')

    allowed = _required_metadata() | _ALLOWED_OPTIONAL_METADATA
    unknown = sorted(set(metadata) - allowed)
    missing = sorted(_required_metadata() - set(metadata))
    if unknown:
        raise ValueError(f'unknown polar_table metadata fields: {unknown}')
    if missing:
        raise ValueError(f'missing polar_table metadata fields: {missing}')
    return metadata, lines[row_start:]


def _finite_number(value: str, *, field_name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f'{field_name} must be numeric') from exc
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def _validate_angles(
    *,
    angle_semantics: str,
    horizontal_wrap: str,
    horizontal_angle_deg: float,
    vertical_angle_deg: float,
) -> None:
    if vertical_angle_deg < -90.0 or vertical_angle_deg > 90.0:
        raise ValueError('vertical/elevation angle must be within [-90, 90] degrees')
    if horizontal_angle_deg < -180.0 or horizontal_angle_deg > 180.0:
        raise ValueError('horizontal/azimuth angle must be within [-180, 180] degrees')
    if angle_semantics == 'horizontal_vertical':
        if horizontal_wrap != 'none':
            raise ValueError('horizontal_vertical polar_table requires horizontal_wrap=none')
    elif angle_semantics == 'spherical_azimuth_elevation':
        if horizontal_wrap not in {'none', 'signed_180'}:
            raise ValueError('spherical polar_table has invalid horizontal_wrap')
        if horizontal_wrap == 'signed_180' and horizontal_angle_deg >= 180.0:
            raise ValueError('signed_180 azimuth must use [-180, 180) coordinates')
    else:
        raise ValueError('unsupported polar_table angle_semantics')


class PolarTableDirectivityAdapter:
    descriptor = DIRECTIVITY_ADAPTER_REGISTRY.resolve_supported(
        source_format='polar_table',
        declared_schema=POLAR_TABLE_SCHEMA,
        adapter_id=POLAR_TABLE_ADAPTER_ID,
        adapter_version=POLAR_TABLE_ADAPTER_VERSION,
    )
    if descriptor is None:  # pragma: no cover - registry construction invariant
        raise RuntimeError('polar_table adapter descriptor is missing')

    def parse(
        self,
        source_bytes: bytes,
        definition: EquipmentDefinition,
    ) -> DirectivityDataset:
        source_sha = sha256(source_bytes).hexdigest()
        metadata, table_lines = _parse_metadata_and_rows(source_bytes)

        if metadata['schema'] != POLAR_TABLE_SCHEMA:
            raise ValueError('unsupported polar_table schema/version')
        if metadata['delimiter'] == 'csv':
            delimiter = ','
        elif metadata['delimiter'] == 'tsv':
            delimiter = '\t'
        else:
            raise ValueError('polar_table delimiter must be explicitly csv or tsv')
        if metadata['frequency_unit'] != 'Hz':
            raise ValueError('polar_table frequency_unit must be exactly Hz')
        if metadata['angle_unit'] != 'degree':
            raise ValueError('polar_table angle_unit must be exactly degree')
        if metadata['reference_axis'] != 'equipment_acoustic_reference_axis':
            raise ValueError('polar_table reference_axis is unsupported')
        if metadata['azimuth_positive'] != 'left':
            raise ValueError('polar_table azimuth_positive must be left')
        if metadata['elevation_positive'] != 'up':
            raise ValueError('polar_table elevation_positive must be up')

        capability = metadata['capability']
        if capability not in {'magnitude_only', 'complex'}:
            raise ValueError('polar_table capability must be magnitude_only or complex')
        magnitude_unit = metadata['magnitude_unit']
        if magnitude_unit not in {'db', 'linear'}:
            raise ValueError('polar_table magnitude_unit must be db or linear')

        normalization_reference = metadata['normalization_reference']
        if normalization_reference == 'on_axis_per_frequency':
            if 'reference_level_db' in metadata:
                raise ValueError(
                    'on_axis_per_frequency must not declare reference_level_db'
                )
            normalization = DirectivityNormalization(
                source_magnitude_unit=magnitude_unit,
                reference='on_axis_per_frequency',
            )
        elif normalization_reference == 'explicit_reference_level':
            if 'reference_level_db' not in metadata:
                raise ValueError(
                    'explicit_reference_level requires reference_level_db'
                )
            normalization = DirectivityNormalization(
                source_magnitude_unit=magnitude_unit,
                reference='explicit_reference_level',
                reference_level_db=_finite_number(
                    metadata['reference_level_db'],
                    field_name='reference_level_db',
                ),
            )
        else:
            raise ValueError('unsupported polar_table normalization_reference')

        phase_reference = metadata.get('phase_reference')
        if capability == 'complex':
            if phase_reference is None:
                raise ValueError('complex polar_table requires explicit phase_reference')
        elif phase_reference is not None:
            raise ValueError('magnitude-only polar_table must not declare phase_reference')

        coordinate = DirectivityCoordinateConvention(
            angle_semantics=metadata['angle_semantics'],
            horizontal_wrap=metadata['horizontal_wrap'],
            reference_axis=metadata['reference_axis'],
            azimuth_positive=metadata['azimuth_positive'],
            elevation_positive=metadata['elevation_positive'],
            angle_unit=metadata['angle_unit'],
        )

        interpolation_method = metadata['interpolation_method']
        if interpolation_method not in {
            'none',
            'nearest',
            'linear',
            'log_frequency_linear_angle',
        }:
            raise ValueError('polar_table interpolation method is unsupported')

        evidence_kind: EquipmentEvidenceKind = metadata['evidence_kind']  # type: ignore[assignment]
        provenance = EquipmentDataProvenance(
            evidence_kind=evidence_kind,
            source_name=metadata['source_name'],
            source_version=metadata['source_version'],
            source_reference=metadata['source_reference'],
            source_sha256=source_sha,
        )
        interpolation = InterpolationProvenance(
            method=interpolation_method,  # type: ignore[arg-type]
            implementation=metadata['interpolation_implementation'],
            implementation_version=metadata['interpolation_version'],
            provenance=provenance,
        )

        if metadata['angle_semantics'] == 'horizontal_vertical':
            angle_columns = ('horizontal_angle_deg', 'vertical_angle_deg')
        else:
            angle_columns = ('azimuth_deg', 'elevation_deg')
        columns = (
            'frequency_hz',
            angle_columns[0],
            angle_columns[1],
            'magnitude',
            'magnitude_unit',
        )
        if capability == 'complex':
            columns = (*columns, 'phase_deg')

        reader = csv.reader(
            io.StringIO('\n'.join(table_lines)),
            delimiter=delimiter,
            strict=True,
        )
        try:
            header = tuple(next(reader))
        except StopIteration as exc:
            raise ValueError('polar_table is missing the tabular header') from exc
        if header != columns:
            raise ValueError(
                f'polar_table columns must exactly equal {columns}; got {header}'
            )

        samples: list[DirectivitySample] = []
        sample_keys: set[tuple[float, float, float]] = set()
        frequencies: set[float] = set()
        horizontal_angles: set[float] = set()
        vertical_angles: set[float] = set()
        for row_number, row in enumerate(reader, start=2):
            if not row or all(value == '' for value in row):
                raise ValueError(f'polar_table row {row_number} is blank')
            if len(row) != len(columns):
                raise ValueError(
                    f'polar_table row {row_number} has {len(row)} fields; '
                    f'expected {len(columns)}'
                )
            values = dict(zip(columns, row, strict=True))
            frequency_hz = _finite_number(
                values['frequency_hz'],
                field_name='frequency_hz',
            )
            if frequency_hz <= 0.0:
                raise ValueError('frequency_hz must be positive')
            horizontal_angle_deg = _finite_number(
                values[angle_columns[0]],
                field_name=angle_columns[0],
            )
            vertical_angle_deg = _finite_number(
                values[angle_columns[1]],
                field_name=angle_columns[1],
            )
            _validate_angles(
                angle_semantics=metadata['angle_semantics'],
                horizontal_wrap=metadata['horizontal_wrap'],
                horizontal_angle_deg=horizontal_angle_deg,
                vertical_angle_deg=vertical_angle_deg,
            )
            if values['magnitude_unit'] != magnitude_unit:
                raise ValueError(
                    'row magnitude_unit must exactly match declared magnitude_unit'
                )
            magnitude = _finite_number(values['magnitude'], field_name='magnitude')
            if magnitude_unit == 'linear':
                if magnitude <= 0.0:
                    raise ValueError('linear magnitude must be > 0')
                magnitude_db = 20.0 * log10(magnitude)
            else:
                magnitude_db = magnitude

            phase_deg: float | None = None
            if capability == 'complex':
                phase_deg = _finite_number(
                    values['phase_deg'],
                    field_name='phase_deg',
                )

            key = (frequency_hz, horizontal_angle_deg, vertical_angle_deg)
            if key in sample_keys:
                raise ValueError('duplicate polar_table sample coordinates')
            sample_keys.add(key)
            frequencies.add(frequency_hz)
            horizontal_angles.add(horizontal_angle_deg)
            vertical_angles.add(vertical_angle_deg)
            samples.append(
                DirectivitySample(
                    frequency_hz=frequency_hz,
                    horizontal_angle_deg=horizontal_angle_deg,
                    vertical_angle_deg=vertical_angle_deg,
                    magnitude_db=magnitude_db,
                    phase_deg=phase_deg,
                )
            )

        if not samples:
            raise ValueError('polar_table requires at least one sample')
        if -180.0 in horizontal_angles and 180.0 in horizontal_angles:
            raise ValueError(
                'polar_table contains ambiguous -180/+180 duplicate directions'
            )

        frequency_grid = tuple(sorted(frequencies))
        horizontal_grid = tuple(sorted(horizontal_angles))
        vertical_grid = tuple(sorted(vertical_angles))
        expected_count = (
            len(frequency_grid) * len(horizontal_grid) * len(vertical_grid)
        )
        if len(samples) != expected_count:
            raise ValueError(
                'polar_table grid is incomplete; every frequency/angle combination '
                'must be present exactly once'
            )

        samples.sort(
            key=lambda item: (
                item.frequency_hz,
                item.horizontal_angle_deg,
                item.vertical_angle_deg,
            )
        )
        return build_directivity_dataset(
            dataset_id=metadata['dataset_id'],
            version=metadata['dataset_version'],
            definition=definition,
            source_asset_sha256=source_sha,
            source_format='polar_table',
            parser_id=POLAR_TABLE_PARSER_ID,
            parser_version=POLAR_TABLE_PARSER_VERSION,
            adapter_id=POLAR_TABLE_ADAPTER_ID,
            adapter_version=POLAR_TABLE_ADAPTER_VERSION,
            evidence_kind=evidence_kind,
            source_provenance=provenance,
            kind=capability,  # type: ignore[arg-type]
            coordinate_convention=coordinate,
            normalization=normalization,
            frequencies_hz=frequency_grid,
            horizontal_angles_deg=horizontal_grid,
            vertical_angles_deg=vertical_grid,
            samples=tuple(samples),
            interpolation=interpolation,
            phase_reference=phase_reference,
        )


NORMALIZED_JSON_ASSET_ADAPTER = NormalizedJsonAssetAdapter()
POLAR_TABLE_DIRECTIVITY_ADAPTER = PolarTableDirectivityAdapter()

_ACTIVE_ADAPTERS: dict[tuple[str, str], DirectivityAssetAdapter] = {
    (
        NORMALIZED_JSON_ASSET_ADAPTER.descriptor.adapter_id,
        NORMALIZED_JSON_ASSET_ADAPTER.descriptor.adapter_version,
    ): NORMALIZED_JSON_ASSET_ADAPTER,
    (
        POLAR_TABLE_DIRECTIVITY_ADAPTER.descriptor.adapter_id,
        POLAR_TABLE_DIRECTIVITY_ADAPTER.descriptor.adapter_version,
    ): POLAR_TABLE_DIRECTIVITY_ADAPTER,
}


def replay_directivity_import(
    *,
    source_bytes: bytes,
    equipment_definition: EquipmentDefinition,
    adapter_id: str,
    adapter_version: str,
) -> DirectivityDataset:
    """Re-run a recorded adapter version against exact retained source bytes.

    This is the audit/migration replay path: the pinned registry must still
    resolve the exact ``(adapter_id, adapter_version)`` implementation that
    produced a persisted dataset, so removed or never-registered versions
    fail closed instead of silently approximating the transformation. The
    replayed output is re-validated against the EquipmentDefinition binding,
    so replay can never widen authority.
    """

    adapter = _ACTIVE_ADAPTERS.get((adapter_id, adapter_version))
    if adapter is None:
        raise ValueError(
            'no supported directivity adapter implementation is registered '
            f'for {adapter_id}@{adapter_version}'
        )
    dataset = adapter.parse(source_bytes, equipment_definition)
    if (
        dataset.adapter_id != adapter_id
        or dataset.adapter_version != adapter_version
    ):
        raise ValueError('replayed directivity dataset adapter identity mismatch')
    validate_directivity_dataset_binding(dataset, equipment_definition)
    return dataset


def _failed_result(
    *,
    state: Literal['UNSUPPORTED', 'REJECTED'],
    source_sha256: str,
    source_format: DirectivityDataFormat,
    declared_schema: str,
    adapter_id: str,
    adapter_version: str,
    parser_id: str,
    parser_version: str,
    reason: str,
) -> DirectivityImportResult:
    return DirectivityImportResult(
        diagnostic=DirectivityImportDiagnostic(
            import_state=state,
            source_sha256=source_sha256,
            source_format=source_format,
            declared_schema=declared_schema,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            parser_id=parser_id,
            parser_version=parser_version,
            sample_count=0,
            rejection_reason=reason,
        )
    )


def import_directivity_asset(
    *,
    raw_source_bytes: bytes,
    explicit_source_format: DirectivityDataFormat,
    declared_schema: str,
    equipment_definition: EquipmentDefinition,
    adapter_id: str,
    adapter_version: str,
    registry: DirectivityAdapterRegistryAuthority = DIRECTIVITY_ADAPTER_REGISTRY,
) -> DirectivityImportResult:
    """Bounded import service.

    The raw bytes are hashed before decoding/parsing. Format/schema/adapter are
    all explicit inputs; filenames/extensions and table columns are never used
    to select a parser.
    """

    source_sha = sha256(raw_source_bytes).hexdigest()
    descriptor = registry.resolve_supported(
        source_format=explicit_source_format,
        declared_schema=declared_schema,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )
    if descriptor is None:
        deferred = registry.deferred_for_format(explicit_source_format)
        if deferred is not None:
            return _failed_result(
                state='UNSUPPORTED',
                source_sha256=source_sha,
                source_format=explicit_source_format,
                declared_schema=declared_schema,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                parser_id=deferred.parser_id,
                parser_version=deferred.parser_version,
                reason=deferred.deferred_reason
                or 'directivity source format is explicitly deferred',
            )
        return _failed_result(
            state='UNSUPPORTED',
            source_sha256=source_sha,
            source_format=explicit_source_format,
            declared_schema=declared_schema,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            parser_id='unresolved',
            parser_version='0',
            reason=(
                'no exact registered adapter matches the explicit '
                'source-format/schema/adapter tuple'
            ),
        )

    adapter = _ACTIVE_ADAPTERS.get(
        (descriptor.adapter_id, descriptor.adapter_version)
    )
    if adapter is None:
        return _failed_result(
            state='UNSUPPORTED',
            source_sha256=source_sha,
            source_format=explicit_source_format,
            declared_schema=declared_schema,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            parser_id=descriptor.parser_id,
            parser_version=descriptor.parser_version,
            reason='registered adapter implementation is unavailable',
        )

    try:
        dataset = adapter.parse(raw_source_bytes, equipment_definition)
        if dataset.source_asset_sha256 != source_sha:
            raise ValueError('adapter normalized dataset source hash does not match raw bytes')
        validate_directivity_dataset_binding(dataset, equipment_definition)
    except ValueError as exc:
        return _failed_result(
            state='REJECTED',
            source_sha256=source_sha,
            source_format=explicit_source_format,
            declared_schema=declared_schema,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            parser_id=descriptor.parser_id,
            parser_version=descriptor.parser_version,
            reason=str(exc),
        )

    return DirectivityImportResult(
        dataset=dataset,
        diagnostic=DirectivityImportDiagnostic(
            import_state='IMPORTED',
            source_sha256=source_sha,
            source_format=explicit_source_format,
            declared_schema=declared_schema,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            parser_id=descriptor.parser_id,
            parser_version=descriptor.parser_version,
            sample_count=len(dataset.samples),
            domain=dataset.valid_domain,
            capability=dataset.kind,
            normalized_dataset_sha256=dataset.semantic_sha256,
        ),
    )
