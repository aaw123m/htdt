from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_coverage import (
    build_coverage_evaluation_scenario,
    evaluate_coverage,
)
from htdt.cad_direct_level import SeatPopulation
from htdt.cad_directivity_import import (
    DIRECTIVITY_ADAPTER_REGISTRY,
    POLAR_TABLE_ADAPTER_ID,
    POLAR_TABLE_ADAPTER_VERSION,
    POLAR_TABLE_SCHEMA,
    import_directivity_asset,
)
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)


NOW = '2026-09-19T13:30:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _grid(
    *,
    spherical: bool = False,
    magnitude_unit: str = 'db',
    complex_data: bool = False,
    horizontal_angles: tuple[float, ...] | None = None,
    vertical_angles: tuple[float, ...] | None = None,
) -> list[tuple[float, float, float, float, float | None]]:
    horizontal = (
        horizontal_angles
        if horizontal_angles is not None
        else ((-90.0, 0.0, 90.0) if spherical else (-30.0, 0.0, 30.0))
    )
    vertical = (
        vertical_angles
        if vertical_angles is not None
        else ((-30.0, 0.0, 30.0) if spherical else (0.0,))
    )
    rows = []
    for frequency_hz in (500.0, 1000.0):
        for h in horizontal:
            for v in vertical:
                on_axis = h == 0.0 and v == 0.0
                magnitude = (
                    (1.0 if on_axis else 0.5)
                    if magnitude_unit == 'linear'
                    else (0.0 if on_axis else -6.0)
                )
                phase = (h + v) / 10.0 if complex_data else None
                rows.append((frequency_hz, h, v, magnitude, phase))
    return rows


def _table_bytes(
    *,
    spherical: bool = False,
    delimiter: str = 'csv',
    magnitude_unit: str = 'db',
    complex_data: bool = False,
    schema: str = POLAR_TABLE_SCHEMA,
    horizontal_wrap: str | None = None,
    rows: list[tuple[float, float, float, float, float | None]] | None = None,
) -> bytes:
    angle_semantics = (
        'spherical_azimuth_elevation' if spherical else 'horizontal_vertical'
    )
    wrap = (
        horizontal_wrap
        if horizontal_wrap is not None
        else ('signed_180' if spherical else 'none')
    )
    separator = ',' if delimiter == 'csv' else '\t'
    capability = 'complex' if complex_data else 'magnitude_only'
    metadata = [
        f'# schema={schema}',
        f'# delimiter={delimiter}',
        '# dataset_id=polar-fixture',
        '# dataset_version=1',
        f'# capability={capability}',
        f'# angle_semantics={angle_semantics}',
        f'# horizontal_wrap={wrap}',
        '# reference_axis=equipment_acoustic_reference_axis',
        '# azimuth_positive=left',
        '# elevation_positive=up',
        '# frequency_unit=Hz',
        '# angle_unit=degree',
        f'# magnitude_unit={magnitude_unit}',
        '# normalization_reference=on_axis_per_frequency',
        '# interpolation_method=linear',
        '# interpolation_implementation=htdt-grid-linear',
        '# interpolation_version=1',
        '# evidence_kind=user_defined',
        '# source_name=Issue 168 polar table fixture',
        '# source_version=1',
        '# source_reference=focused-test',
    ]
    if complex_data:
        metadata.append('# phase_reference=acoustic_reference_point/source-t0')

    angle_columns = (
        ('azimuth_deg', 'elevation_deg')
        if spherical
        else ('horizontal_angle_deg', 'vertical_angle_deg')
    )
    columns = [
        'frequency_hz',
        angle_columns[0],
        angle_columns[1],
        'magnitude',
        'magnitude_unit',
    ]
    if complex_data:
        columns.append('phase_deg')

    data_rows = rows or _grid(
        spherical=spherical,
        magnitude_unit=magnitude_unit,
        complex_data=complex_data,
    )
    body = [separator.join(columns)]
    for frequency_hz, h, v, magnitude, phase in data_rows:
        values = [
            str(frequency_hz),
            str(h),
            str(v),
            str(magnitude),
            magnitude_unit,
        ]
        if complex_data:
            assert phase is not None
            values.append(str(phase))
        body.append(separator.join(values))
    return ('\n'.join([*metadata, *body]) + '\n').encode('utf-8')


def _domain(
    *,
    spherical: bool = False,
    horizontal: tuple[float, float] | None = None,
    vertical: tuple[float, float] | None = None,
    maximum_hz: float = 1000.0,
) -> DirectivityDomain:
    if horizontal is None:
        horizontal = (-90.0, 90.0) if spherical else (-30.0, 30.0)
    if vertical is None:
        vertical = (-30.0, 30.0) if spherical else (0.0, 0.0)
    return DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=maximum_hz),
        horizontal=AngleDomain(
            minimum_deg=horizontal[0],
            maximum_deg=horizontal[1],
        ),
        vertical=AngleDomain(
            minimum_deg=vertical[0],
            maximum_deg=vertical[1],
        ),
    )


def _definition(
    raw: bytes,
    *,
    spherical: bool = False,
    complex_data: bool = False,
    domain: DirectivityDomain | None = None,
    source_sha_override: str | None = None,
):
    source_sha = source_sha_override or sha256(raw).hexdigest()
    provenance = EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='Issue 168 equipment fixture',
        source_version='1',
        source_reference='focused-test',
        source_sha256=source_sha,
    )
    interpolation = InterpolationProvenance(
        method='linear',
        implementation='htdt-grid-linear',
        implementation_version='1',
        provenance=provenance,
    )
    return build_equipment_definition(
        definition_id='polar-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='Polar speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='complex' if complex_data else 'magnitude_only',
            data_format='polar_table',
            provenance=provenance,
            data_asset_sha256=source_sha,
            valid_domain=domain or _domain(spherical=spherical),
            interpolation=interpolation,
            coherent_phase=complex_data,
            phase_reference=(
                'acoustic_reference_point/source-t0'
                if complex_data
                else None
            ),
        ),
    )


def _import(raw: bytes, definition):
    return import_directivity_asset(
        raw_source_bytes=raw,
        explicit_source_format='polar_table',
        declared_schema=POLAR_TABLE_SCHEMA,
        equipment_definition=definition,
        adapter_id=POLAR_TABLE_ADAPTER_ID,
        adapter_version=POLAR_TABLE_ADAPTER_VERSION,
    )


def test_valid_magnitude_only_polar_table_imports_exact_raw_hash() -> None:
    raw = _table_bytes()
    definition = _definition(raw)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'IMPORTED'
    assert result.dataset is not None
    assert result.dataset.source_asset_sha256 == sha256(raw).hexdigest()
    assert result.dataset.source_format == 'polar_table'
    assert result.dataset.kind == 'magnitude_only'
    assert result.diagnostic.sample_count == 6


def test_valid_spherical_tsv_import_is_explicit_not_inferred() -> None:
    raw = _table_bytes(spherical=True, delimiter='tsv')
    definition = _definition(raw, spherical=True)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'IMPORTED'
    assert result.dataset is not None
    assert (
        result.dataset.coordinate_convention.angle_semantics
        == 'spherical_azimuth_elevation'
    )
    assert result.dataset.coordinate_convention.horizontal_wrap == 'signed_180'
    assert result.diagnostic.sample_count == 18


def test_linear_magnitude_conversion_uses_pressure_db20() -> None:
    raw = _table_bytes(magnitude_unit='linear')
    definition = _definition(raw)
    result = _import(raw, definition)

    assert result.dataset is not None
    off_axis = next(
        sample
        for sample in result.dataset.samples
        if sample.frequency_hz == 500.0
        and sample.horizontal_angle_deg == -30.0
    )
    assert off_axis.magnitude_db == pytest.approx(-6.020599913, abs=1e-9)


def test_duplicate_row_is_rejected() -> None:
    raw = _table_bytes()
    lines = raw.decode().splitlines()
    raw = ('\n'.join([*lines, lines[-1]]) + '\n').encode()
    definition = _definition(raw)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert 'duplicate' in result.diagnostic.rejection_reason.lower()


def test_missing_grid_cell_is_rejected() -> None:
    raw = _table_bytes()
    lines = raw.decode().splitlines()
    raw = ('\n'.join(lines[:-1]) + '\n').encode()
    definition = _definition(raw)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert 'incomplete' in result.diagnostic.rejection_reason.lower()


def test_malformed_declared_schema_inside_asset_is_rejected() -> None:
    raw = _table_bytes(schema='htdt.polar-table.v2')
    definition = _definition(raw)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert 'schema' in result.diagnostic.rejection_reason.lower()


def test_equipment_domain_mismatch_is_rejected() -> None:
    raw = _table_bytes()
    definition = _definition(raw, domain=_domain(maximum_hz=2000.0))
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert 'domain' in result.diagnostic.rejection_reason.lower()


def test_exact_source_sha_mismatch_is_rejected_without_normalizing_bytes() -> None:
    raw = _table_bytes()
    definition = _definition(raw, source_sha_override='f' * 64)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert result.diagnostic.source_sha256 == sha256(raw).hexdigest()
    assert 'source asset hash' in result.diagnostic.rejection_reason.lower()


def test_phase_column_in_magnitude_only_table_is_rejected() -> None:
    raw = _table_bytes()
    lines = raw.decode().splitlines()
    header_index = next(
        index for index, line in enumerate(lines) if not line.startswith('# ')
    )
    lines[header_index] += ',phase_deg'
    for index in range(header_index + 1, len(lines)):
        lines[index] += ',0.0'
    raw = ('\n'.join(lines) + '\n').encode()
    definition = _definition(raw)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert 'columns' in result.diagnostic.rejection_reason.lower()


def test_complex_schema_requires_and_preserves_exact_phase_reference() -> None:
    raw = _table_bytes(complex_data=True)
    definition = _definition(raw, complex_data=True)
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'IMPORTED'
    assert result.dataset is not None
    assert result.dataset.kind == 'complex'
    assert (
        result.dataset.phase_reference
        == 'acoustic_reference_point/source-t0'
    )
    assert all(sample.phase_deg is not None for sample in result.dataset.samples)


def test_plus_minus_180_ambiguity_is_rejected() -> None:
    rows = _grid(
        spherical=True,
        horizontal_angles=(-180.0, 0.0, 180.0),
        vertical_angles=(0.0,),
    )
    raw = _table_bytes(
        spherical=True,
        horizontal_wrap='none',
        rows=rows,
    )
    definition = _definition(
        raw,
        spherical=True,
        domain=_domain(
            spherical=True,
            horizontal=(-180.0, 180.0),
            vertical=(0.0, 0.0),
        ),
    )
    result = _import(raw, definition)

    assert result.diagnostic.import_state == 'REJECTED'
    assert 'ambiguous -180/+180' in result.diagnostic.rejection_reason


def test_import_twice_produces_same_dataset_hash() -> None:
    raw = _table_bytes()
    definition = _definition(raw)
    first = _import(raw, definition)
    second = _import(raw, definition)

    assert first.dataset is not None and second.dataset is not None
    assert first.dataset.semantic_sha256 == second.dataset.semantic_sha256
    assert first.dataset == second.dataset


def test_imported_dataset_save_and_reopen_reuses_existing_repository(
    tmp_path: Path,
) -> None:
    raw = _table_bytes()
    definition = _definition(raw)
    result = _import(raw, definition)
    assert result.dataset is not None

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    equipment_repository = CadEquipmentRepository(scene_repository)
    _save_equipment(equipment_repository, definition)
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    directivity_repository.save_dataset(
        result.dataset,
        source_bytes=raw,
        source_filename='polar-fixture.csv',
        media_type='text/csv',
        declared_schema=POLAR_TABLE_SCHEMA,
    )

    reopened_scene = SceneRepository(scene_repository.path)
    reopened_equipment = CadEquipmentRepository(reopened_scene)
    reopened_repository = CadDirectivityRepository(
        reopened_scene,
        reopened_equipment,
    )
    reopened = reopened_repository.get_dataset(
        result.dataset.dataset_id,
        result.dataset.version,
    )
    assert reopened == result.dataset
    assert (
        reopened_repository.read_source_asset(
            result.dataset.source_asset_sha256
        )
        == raw
    )
    metadata = reopened_repository.get_source_metadata(
        result.dataset.source_asset_sha256
    )
    assert metadata is not None
    assert metadata.filename == 'polar-fixture.csv'
    assert metadata.media_type == 'text/csv'
    assert metadata.declared_schema == POLAR_TABLE_SCHEMA


def _scene_and_variant(tmp_path: Path, definition):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    speaker = SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )
    seat = SceneEntity(
        entity_id='seat-1',
        kind='seat',
        name='Seat 1',
        position=Position3(x_m=1.0, y_m=3.0, z_m=1.0),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )
    revision = scene_repository.save(
        SceneDocument(
            document_id='issue168-import-integration',
            schema_version=2,
            room=RoomPrism(width_m=5.0, depth_m=5.0, height_m=2.5),
            entities=(speaker, seat),
        ),
        parent_revision_id=None,
    ).revision
    variant = build_system_variant(
        baseline=revision,
        name='Imported directivity variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    return revision, variant


def test_imported_dataset_compiles_directly_through_r110(tmp_path: Path) -> None:
    raw = _table_bytes()
    definition = _definition(raw)
    result = _import(raw, definition)
    assert result.dataset is not None
    revision, variant = _scene_and_variant(tmp_path, definition)

    compiled = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=definition,
        directivity_dataset=result.dataset,
    )

    assert compiled.directivity_dataset_sha256 == result.dataset.semantic_sha256
    assert compiled.capability('magnitude_directivity').decision == 'SUPPORTED'


def test_imported_dataset_evaluates_directly_through_coverage(
    tmp_path: Path,
) -> None:
    raw = _table_bytes()
    definition = _definition(raw)
    result = _import(raw, definition)
    assert result.dataset is not None
    revision, variant = _scene_and_variant(tmp_path, definition)

    scenario = build_coverage_evaluation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        receiver_population=SeatPopulation(
            population_id='seat-population',
            seat_entity_ids=('seat-1',),
        ),
        directivity_dataset=result.dataset,
        equipment_definition=definition,
        evaluation_frequencies_hz=(500.0, 1000.0),
        frequency_aggregation_semantics='worst_over_requested_frequencies',
        coverage_threshold_db=-6.0,
    )
    evaluation = evaluate_coverage(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        directivity_dataset=result.dataset,
        scenario=scenario,
    )

    assert evaluation.aggregates.useful_coverage_fraction.state == 'available'
    assert evaluation.aggregates.useful_coverage_fraction.value == 1.0


@pytest.mark.parametrize('source_format', ('clf', 'cf2'))
def test_native_clf_and_cf2_remain_explicitly_deferred(source_format: str) -> None:
    descriptor = DIRECTIVITY_ADAPTER_REGISTRY.deferred_for_format(source_format)
    assert descriptor is not None
    assert descriptor.support_state == 'DEFERRED'
    assert descriptor.supported_capabilities == ()

    raw = f'not parsed as {source_format}'.encode('ascii')
    definition = _definition(_table_bytes())
    result = import_directivity_asset(
        raw_source_bytes=raw,
        explicit_source_format=source_format,
        declared_schema=f'unknown-{source_format}-version',
        equipment_definition=definition,
        adapter_id=f'any-{source_format}-parser',
        adapter_version='1',
    )

    assert result.dataset is None
    assert result.diagnostic.import_state == 'UNSUPPORTED'
    assert result.diagnostic.source_sha256 == sha256(raw).hexdigest()
    assert 'deferred' in result.diagnostic.rejection_reason.lower()
