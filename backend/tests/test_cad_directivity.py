from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_directivity import (
    NORMALIZED_JSON_DIRECTIVITY_ADAPTER,
    DirectivityCoordinateConvention,
    DirectivityDataset,
    DirectivityNormalization,
    evaluate_directivity,
    validate_directivity_dataset_binding,
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
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Size3


def _asset_payload(
    *,
    kind: str = 'magnitude_only',
    source_magnitude_unit: str = 'db',
    angle_semantics: str = 'horizontal_vertical',
    horizontal_wrap: str = 'none',
) -> dict:
    if kind == 'complex':
        if source_magnitude_unit == 'linear':
            values = {
                (500.0, -30.0): (0.5, -20.0),
                (500.0, 0.0): (1.0, 0.0),
                (500.0, 30.0): (0.5, 20.0),
                (1000.0, -30.0): (0.4, -30.0),
                (1000.0, 0.0): (1.0, 0.0),
                (1000.0, 30.0): (0.4, 30.0),
            }
        else:
            values = {
                (500.0, -30.0): (-6.0, -20.0),
                (500.0, 0.0): (0.0, 0.0),
                (500.0, 30.0): (-6.0, 20.0),
                (1000.0, -30.0): (-8.0, -30.0),
                (1000.0, 0.0): (0.0, 0.0),
                (1000.0, 30.0): (-8.0, 30.0),
            }
        samples = [
            {
                'frequency_hz': frequency_hz,
                'horizontal_angle_deg': horizontal_angle_deg,
                'vertical_angle_deg': 0.0,
                'magnitude': magnitude,
                'phase_deg': phase_deg,
            }
            for (frequency_hz, horizontal_angle_deg), (
                magnitude,
                phase_deg,
            ) in sorted(values.items())
        ]
        phase_reference = 'acoustic_reference_point / source t0'
    else:
        values = {
            (500.0, -30.0): -6.0,
            (500.0, 0.0): 0.0,
            (500.0, 30.0): -6.0,
            (1000.0, -30.0): -8.0,
            (1000.0, 0.0): 0.0,
            (1000.0, 30.0): -8.0,
        }
        samples = [
            {
                'frequency_hz': frequency_hz,
                'horizontal_angle_deg': horizontal_angle_deg,
                'vertical_angle_deg': 0.0,
                'magnitude': magnitude,
            }
            for (frequency_hz, horizontal_angle_deg), magnitude
            in sorted(values.items())
        ]
        phase_reference = None

    return {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': f'fixture-{kind}',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'measured',
        'source_name': f'{kind} deterministic fixture',
        'source_version': '2026-09-19',
        'source_reference': 'fixture-grid',
        'kind': kind,
        'coordinate_convention': {
            'angle_semantics': angle_semantics,
            'horizontal_wrap': horizontal_wrap,
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': source_magnitude_unit,
            'normalized_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
            'reference_level_db': None,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': phase_reference,
        'interpolation_method': 'linear',
        'interpolation_implementation': 'htdt-grid-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-30.0, 0.0, 30.0],
        'vertical_angles_deg': [0.0],
        'samples': samples,
    }


def _asset_bytes(**kwargs) -> bytes:
    return json.dumps(
        _asset_payload(**kwargs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _definition_for_asset(
    source_bytes: bytes,
    *,
    definition_id: str,
    kind: str,
    user_label: str | None = None,
    separate_capability_provenance: bool = False,
):
    source = json.loads(source_bytes)
    source_sha256 = sha256(source_bytes).hexdigest()
    asset_provenance = EquipmentDataProvenance(
        evidence_kind=source['evidence_kind'],
        source_name=source['source_name'],
        source_version=source['source_version'],
        source_reference=source['source_reference'],
        source_sha256=source_sha256,
    )
    capability_provenance = (
        EquipmentDataProvenance(
            evidence_kind=source['evidence_kind'],
            source_name=f"{source['source_name']} metadata",
            source_version=source['source_version'],
            source_reference=f"{source['source_reference']}-metadata",
            source_sha256='9' * 64,
        )
        if separate_capability_provenance
        else asset_provenance
    )
    domain = DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=min(source['frequencies_hz']),
            maximum_hz=max(source['frequencies_hz']),
        ),
        horizontal=AngleDomain(
            minimum_deg=min(source['horizontal_angles_deg']),
            maximum_deg=max(source['horizontal_angles_deg']),
        ),
        vertical=AngleDomain(
            minimum_deg=min(source['vertical_angles_deg']),
            maximum_deg=max(source['vertical_angles_deg']),
        ),
    )
    interpolation = InterpolationProvenance(
        method=source['interpolation_method'],
        implementation=source['interpolation_implementation'],
        implementation_version=source['interpolation_version'],
        provenance=capability_provenance,
    )
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=user_label or definition_id,
        provenance=(capability_provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier=kind,
            data_format=source['source_format'],
            provenance=capability_provenance,
            data_asset_sha256=source_sha256,
            valid_domain=domain,
            interpolation=interpolation,
            coherent_phase=(kind == 'complex'),
            phase_reference=source['phase_reference'],
        ),
    )


def _imported_dataset(
    *,
    kind: str = 'magnitude_only',
    source_magnitude_unit: str = 'db',
):
    source_bytes = _asset_bytes(
        kind=kind,
        source_magnitude_unit=source_magnitude_unit,
    )
    definition = _definition_for_asset(
        source_bytes,
        definition_id=f'equipment-{kind}',
        kind=kind,
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    return source_bytes, definition, dataset


def test_magnitude_and_complex_datasets_import_with_exact_source_provenance() -> None:
    magnitude_bytes, magnitude_definition, magnitude = _imported_dataset()
    complex_bytes, complex_definition, complex_dataset = _imported_dataset(
        kind='complex',
        source_magnitude_unit='linear',
    )

    assert magnitude.kind == 'magnitude_only'
    assert magnitude.source_asset_sha256 == sha256(magnitude_bytes).hexdigest()
    assert magnitude.equipment_definition_sha256 == (
        magnitude_definition.semantic_sha256
    )
    assert magnitude.phase_reference is None

    assert complex_dataset.kind == 'complex'
    assert complex_dataset.source_asset_sha256 == sha256(complex_bytes).hexdigest()
    assert complex_dataset.equipment_definition_sha256 == (
        complex_definition.semantic_sha256
    )
    assert complex_dataset.phase_reference == (
        'acoustic_reference_point / source t0'
    )
    off_axis = next(
        sample
        for sample in complex_dataset.samples
        if sample.frequency_hz == 500.0
        and sample.horizontal_angle_deg == 30.0
    )
    assert off_axis.magnitude_db == pytest.approx(-6.020599913279624)


def test_dataset_binding_allows_separate_equipment_provenance_document() -> None:
    source_bytes = _asset_bytes()
    definition = _definition_for_asset(
        source_bytes,
        definition_id='separate-provenance',
        kind='magnitude_only',
        separate_capability_provenance=True,
    )

    assert definition.directivity.provenance.source_sha256 != sha256(
        source_bytes
    ).hexdigest()
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    assert dataset.source_asset_sha256 == sha256(source_bytes).hexdigest()
    assert dataset.equipment_definition_sha256 == definition.semantic_sha256


def test_on_grid_interpolation_and_domain_fail_closed() -> None:
    _source, _definition, dataset = _imported_dataset()

    exact = evaluate_directivity(
        dataset,
        frequency_hz=1000.0,
        horizontal_angle_deg=30.0,
        vertical_angle_deg=0.0,
    )
    assert exact.decision == 'SUPPORTED'
    assert exact.interpolation_applied is False
    assert exact.magnitude_db == -8.0

    interpolated = evaluate_directivity(
        dataset,
        frequency_hz=750.0,
        horizontal_angle_deg=15.0,
        vertical_angle_deg=0.0,
    )
    assert interpolated.decision == 'SUPPORTED'
    assert interpolated.interpolation_applied is True
    assert interpolated.magnitude_db == pytest.approx(-3.5)

    frequency_outside = evaluate_directivity(
        dataset,
        frequency_hz=1500.0,
        horizontal_angle_deg=0.0,
        vertical_angle_deg=0.0,
    )
    assert frequency_outside.decision == 'UNSUPPORTED'
    assert frequency_outside.magnitude_db is None

    angle_outside = evaluate_directivity(
        dataset,
        frequency_hz=750.0,
        horizontal_angle_deg=45.0,
        vertical_angle_deg=0.0,
    )
    assert angle_outside.decision == 'UNSUPPORTED'
    assert angle_outside.magnitude_db is None

    complex_request = evaluate_directivity(
        dataset,
        frequency_hz=750.0,
        horizontal_angle_deg=15.0,
        vertical_angle_deg=0.0,
        request='complex',
    )
    assert complex_request.decision == 'UNSUPPORTED'
    assert complex_request.complex_real is None


def test_complex_evaluation_preserves_phase_capability() -> None:
    _source, _definition, dataset = _imported_dataset(
        kind='complex',
        source_magnitude_unit='linear',
    )
    result = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=30.0,
        vertical_angle_deg=0.0,
        request='complex',
    )
    assert result.decision == 'SUPPORTED'
    assert result.phase_deg == 20.0
    assert result.magnitude_linear == pytest.approx(0.5)
    assert result.complex_real is not None
    assert result.complex_imag is not None


def test_save_reopen_preserves_dataset_and_evaluation_identity(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported_dataset()
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    equipment_repository = CadEquipmentRepository(scene_repository)
    equipment_repository.save_definition(definition)
    repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename='fixture-dataset.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )

    before = evaluate_directivity(
        dataset,
        frequency_hz=750.0,
        horizontal_angle_deg=15.0,
        vertical_angle_deg=0.0,
    )

    reopened_equipment = CadEquipmentRepository(scene_repository)
    reopened_repository = CadDirectivityRepository(
        scene_repository,
        reopened_equipment,
    )
    reopened = reopened_repository.get_dataset(
        dataset.dataset_id,
        dataset.version,
    )
    assert reopened == dataset
    assert reopened is not None
    after = evaluate_directivity(
        reopened,
        frequency_hz=750.0,
        horizontal_angle_deg=15.0,
        vertical_angle_deg=0.0,
    )
    assert after == before
    assert after.semantic_sha256 == before.semantic_sha256


def test_equipment_definition_hash_mismatch_is_rejected() -> None:
    source_bytes, definition, dataset = _imported_dataset()
    other_definition = _definition_for_asset(
        source_bytes,
        definition_id=definition.definition_id,
        kind='magnitude_only',
        user_label='semantically different equipment definition',
    )
    assert other_definition.semantic_sha256 != definition.semantic_sha256

    with pytest.raises(
        ValueError,
        match='EquipmentDefinition hash mismatch',
    ):
        validate_directivity_dataset_binding(dataset, other_definition)


def test_malformed_or_ambiguous_source_data_fails_closed() -> None:
    incomplete = _asset_payload()
    incomplete['samples'] = incomplete['samples'][:-1]
    incomplete_bytes = json.dumps(
        incomplete,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    incomplete_definition = _definition_for_asset(
        incomplete_bytes,
        definition_id='incomplete-grid',
        kind='magnitude_only',
    )
    with pytest.raises(
        ValidationError,
        match='grid is incomplete',
    ):
        NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
            incomplete_bytes,
            incomplete_definition,
        )

    ambiguous = _asset_payload(
        angle_semantics='spherical_azimuth_elevation',
        horizontal_wrap='signed_180',
    )
    ambiguous['horizontal_angles_deg'] = [-180.0, 0.0, 180.0]
    ambiguous['samples'] = [
        {
            'frequency_hz': frequency_hz,
            'horizontal_angle_deg': horizontal_angle_deg,
            'vertical_angle_deg': 0.0,
            'magnitude': 0.0 if horizontal_angle_deg == 0.0 else -6.0,
        }
        for frequency_hz in (500.0, 1000.0)
        for horizontal_angle_deg in (-180.0, 0.0, 180.0)
    ]
    ambiguous_bytes = json.dumps(
        ambiguous,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    ambiguous_definition = _definition_for_asset(
        ambiguous_bytes,
        definition_id='ambiguous-wrap',
        kind='magnitude_only',
    )
    with pytest.raises(
        ValidationError,
        match='signed_180 azimuth grid',
    ):
        NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
            ambiguous_bytes,
            ambiguous_definition,
        )


def test_direct_models_reject_fabricated_complex_phase() -> None:
    convention = DirectivityCoordinateConvention(
        angle_semantics='horizontal_vertical',
        horizontal_wrap='none',
    )
    normalization = DirectivityNormalization(
        source_magnitude_unit='db',
        reference='on_axis_per_frequency',
    )
    assert convention.reference_axis == 'equipment_acoustic_reference_axis'
    assert normalization.conversion_version == 'pressure-amplitude-db20-v1'

    with pytest.raises(ValidationError):
        DirectivityDataset.model_validate(
            {
                'dataset_id': 'invalid',
                'version': '1',
            }
        )
