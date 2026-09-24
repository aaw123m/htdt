from __future__ import annotations

from hashlib import sha256
import json

import pytest

from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
from htdt.cad_directivity_inspection import (
    confirm_directivity_inspection,
    directivity_axis_markers,
    directivity_polar_slice,
    directivity_slice_heatmap,
    inspect_directivity_dataset,
)
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_scene import Offset3, Size3


NOW = '2026-09-24T00:00:00+00:00'


def _asset_payload(*, kind: str = 'magnitude_only') -> dict:
    if kind == 'complex':
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
            'angle_semantics': 'horizontal_vertical',
            'horizontal_wrap': 'none',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
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


def _imported_dataset(*, kind: str = 'magnitude_only'):
    payload = _asset_payload(kind=kind)
    source_bytes = json.dumps(
        payload, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    source_sha256 = sha256(source_bytes).hexdigest()
    provenance = EquipmentDataProvenance(
        evidence_kind=payload['evidence_kind'],
        source_name=payload['source_name'],
        source_version=payload['source_version'],
        source_reference=payload['source_reference'],
        source_sha256=source_sha256,
    )
    domain = DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=min(payload['frequencies_hz']),
            maximum_hz=max(payload['frequencies_hz']),
        ),
        horizontal=AngleDomain(
            minimum_deg=min(payload['horizontal_angles_deg']),
            maximum_deg=max(payload['horizontal_angles_deg']),
        ),
        vertical=AngleDomain(
            minimum_deg=min(payload['vertical_angles_deg']),
            maximum_deg=max(payload['vertical_angles_deg']),
        ),
    )
    interpolation = InterpolationProvenance(
        method=payload['interpolation_method'],
        implementation=payload['interpolation_implementation'],
        implementation_version=payload['interpolation_version'],
        provenance=provenance,
    )
    definition = build_equipment_definition(
        definition_id=f'equipment-{kind}',
        version='1',
        identity_kind='user_defined',
        user_label=f'equipment-{kind}',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier=kind,
            data_format=payload['source_format'],
            provenance=provenance,
            data_asset_sha256=source_sha256,
            valid_domain=domain,
            interpolation=interpolation,
            coherent_phase=(kind == 'complex'),
            phase_reference=payload['phase_reference'],
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(source_bytes, definition)
    return definition, dataset


def test_axis_markers_expose_declared_sign_conventions() -> None:
    _definition, dataset = _imported_dataset()
    markers = directivity_axis_markers(dataset)
    by_name = {item.name: item for item in markers.markers}
    assert markers.azimuth_positive == 'left'
    assert markers.elevation_positive == 'up'
    assert by_name['front'].horizontal_angle_deg == 0.0
    assert by_name['left'].horizontal_angle_deg == 90.0
    assert by_name['right'].horizontal_angle_deg == -90.0
    # Off-grid marker positions are reported as outside the valid domain.
    assert by_name['left'].in_domain is False
    assert by_name['front'].in_domain is True


def test_exact_slice_returns_raw_on_grid_samples() -> None:
    _definition, dataset = _imported_dataset()
    slice_ = directivity_polar_slice(dataset, 'horizontal', 500.0)
    assert slice_.status == 'AVAILABLE'
    assert slice_.interpolated is False
    assert all(item.on_grid for item in slice_.points)
    by_angle = {item.angle_deg: item for item in slice_.points}
    assert by_angle[0.0].magnitude_db == 0.0
    assert by_angle[-30.0].magnitude_db == -6.0
    assert by_angle[-30.0].phase_deg is None
    assert slice_.phase_available is False


def test_off_grid_frequency_requires_explicit_interpolation() -> None:
    _definition, dataset = _imported_dataset()
    exact = directivity_polar_slice(dataset, 'horizontal', 750.0)
    assert exact.status == 'UNKNOWN'
    assert 'not on the dataset grid' in exact.reason

    preview = directivity_polar_slice(
        dataset, 'horizontal', 750.0, allow_interpolation=True
    )
    assert preview.status == 'AVAILABLE'
    assert preview.interpolated is True
    assert preview.interpolation_method == 'linear'
    # Interpolated preview is flagged, never dressed as raw data.
    assert any(not item.on_grid for item in preview.points)


def test_complex_dataset_exposes_phase_and_heatmap() -> None:
    _definition, dataset = _imported_dataset(kind='complex')
    slice_ = directivity_polar_slice(dataset, 'horizontal', 500.0)
    assert slice_.phase_available is True
    assert slice_.points[1].phase_deg == 0.0
    assert slice_.points[0].phase_deg == -20.0

    heatmap = directivity_slice_heatmap(dataset, 'horizontal')
    assert heatmap.status == 'AVAILABLE'
    assert heatmap.frequencies_hz == (500.0, 1000.0)
    assert heatmap.angles_deg == (-30.0, 0.0, 30.0)
    assert heatmap.magnitudes_db[0][1] == 0.0
    assert heatmap.magnitudes_db[1][0] == pytest.approx(-8.0)


def test_inspection_summary_normal_and_advanced_provenance() -> None:
    definition, dataset = _imported_dataset()
    summary = inspect_directivity_dataset(dataset, definition=definition)
    assert summary.dataset_semantic_sha256 == dataset.semantic_sha256
    assert summary.phase_available is False
    assert 'magnitude-only' in summary.phase_statement
    assert summary.provenance['source_format'] == 'custom'
    assert summary.provenance['evidence_kind'] == 'measured'
    assert summary.provenance['valid_frequency_band_hz'] == [500.0, 1000.0]
    assert summary.advanced['grid_sha256'] == dataset.grid_sha256
    assert summary.advanced['interpolation_method'] == 'linear'
    assert summary.acoustic_reference_point_m == (0.0, 0.0, 0.0)
    assert summary.cabinet_envelope_m == (0.2, 0.25, 0.35)


def test_inspection_rejects_unbound_definition() -> None:
    definition, dataset = _imported_dataset()
    _other_definition, other = _imported_dataset(kind='complex')
    with pytest.raises(ValueError, match='exact bound authority'):
        inspect_directivity_dataset(other, definition=definition)


def test_confirmation_is_user_review_provenance_pinned_to_dataset() -> None:
    _definition, dataset = _imported_dataset()
    confirmation = confirm_directivity_inspection(
        dataset,
        front_axis_reviewed=True,
        conventions_reviewed=True,
        confirmed_at_utc=NOW,
        note='front axis visually checked',
    )
    assert confirmation.dataset_semantic_sha256 == dataset.semantic_sha256
    assert confirmation.dataset_id == dataset.dataset_id
    assert confirmation.front_axis_reviewed is True
    assert len(confirmation.confirmation_sha256) == 64
    # Confirmation records review of this exact dataset, not correctness.
    assert confirm_directivity_inspection(
        dataset,
        front_axis_reviewed=False,
        conventions_reviewed=False,
        confirmed_at_utc=NOW,
        confirmation_id='review-failed-1',
    ).confirmation_id == 'review-failed-1'
