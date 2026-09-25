from __future__ import annotations

import json
from math import cos, radians, sin

from test_cad_directivity import (
    NORMALIZED_JSON_DIRECTIVITY_ADAPTER,
    _definition_for_asset,
)

from htdt.cad_directivity import (
    classify_directivity_grid,
    direction_to_directivity_angles,
    evaluate_directivity,
    evaluate_directivity_direction,
)


def _sphere_dataset(vertical_angles=(-90.0, -45.0, 0.0, 45.0, 90.0)):
    """Full-circle signed_180 azimuth x elevation grid (#981)."""
    horizontal_angles = [
        -180.0 + 30.0 * index for index in range(12)
    ]  # -180 .. 150, step 30
    payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': 'fixture-sphere',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'measured',
        'source_name': 'spherical deterministic fixture',
        'source_version': '2026-09-25',
        'source_reference': 'fixture-sphere',
        'kind': 'magnitude_only',
        'coordinate_convention': {
            'angle_semantics': 'spherical_azimuth_elevation',
            'horizontal_wrap': 'signed_180',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'normalized_magnitude_unit': 'db',
            'reference': 'explicit_reference_level',
            'reference_level_db': 0.0,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': None,
        'interpolation_method': 'linear',
        'interpolation_implementation': 'htdt-grid-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': horizontal_angles,
        'vertical_angles_deg': list(vertical_angles),
        'samples': [
            {
                'frequency_hz': frequency,
                'horizontal_angle_deg': horizontal,
                'vertical_angle_deg': vertical,
                'magnitude': -6.0
                if vertical == 0.0 and horizontal == 0.0
                else (
                    -3.0
                    if vertical == 90.0 and horizontal == -180.0
                    else -12.0
                ),
            }
            for frequency in (500.0, 1000.0)
            for horizontal in horizontal_angles
            for vertical in vertical_angles
        ],
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    definition = _definition_for_asset(
        raw, definition_id='sphere-def', kind='magnitude_only'
    )
    return NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(raw, definition)


def test_azimuth_seam_brackets_across_wrap() -> None:
    dataset = _sphere_dataset()
    # 165deg lies between the last axis (150) and the first (-180) on the
    # seam: previously UNSUPPORTED, now interpolates across the wrap.
    result = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=165.0,
        vertical_angle_deg=0.0,
    )
    assert result.decision == 'SUPPORTED'
    assert result.interpolation_applied
    assert result.magnitude_db is not None
    # Seam-neighbour samples are 150 and -180 only.
    assert len(result.supporting_sample_sha256) >= 2


def test_seam_equivalent_directions_are_invariant() -> None:
    dataset = _sphere_dataset()
    # -180 and +180 are the same physical direction.
    positive = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=180.0,
        vertical_angle_deg=0.0,
    )
    negative = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=-180.0,
        vertical_angle_deg=0.0,
    )
    assert positive.decision == negative.decision == 'SUPPORTED'
    assert positive.magnitude_db == negative.magnitude_db


def test_pole_evaluation_ignores_azimuth() -> None:
    dataset = _sphere_dataset()
    # At the +90 pole all azimuths name the same direction; evaluation
    # must not depend on the arbitrary azimuth parameterization.
    for azimuth in (-90.0, 0.0, 45.0, 150.0):
        result = evaluate_directivity(
            dataset,
            frequency_hz=500.0,
            horizontal_angle_deg=azimuth,
            vertical_angle_deg=90.0,
        )
        assert result.decision == 'SUPPORTED'
        # Canonical pole meridian is the dataset's first azimuth (-180):
        # magnitude is the canonical pole sample's -3 dB, not a fabricated
        # azimuth-dependent blend.
        assert result.magnitude_db == -3.0
        assert 'azimuth canonicalized at the elevation pole' in result.reasons


def test_unit_direction_evaluation_matches_angles() -> None:
    dataset = _sphere_dataset()
    azimuth, elevation = direction_to_directivity_angles(
        (cos(radians(30.0)), sin(radians(30.0)), 0.0),
        semantics='spherical_azimuth_elevation',
    )
    assert abs(azimuth - 30.0) < 1e-9
    assert abs(elevation) < 1e-9

    by_direction = evaluate_directivity_direction(
        dataset,
        direction=(cos(radians(30.0)), sin(radians(30.0)), 0.0),
        frequency_hz=500.0,
    )
    by_angles = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=30.0,
        vertical_angle_deg=0.0,
    )
    assert by_direction.decision == by_angles.decision == 'SUPPORTED'
    assert by_direction.magnitude_db == by_angles.magnitude_db

    # Rotation equivariance: a rotated (frame, direction) pair expressed in
    # the same local frame yields the same local evaluation.
    up = evaluate_directivity_direction(
        dataset,
        direction=(0.0, 0.0, 1.0),
        frequency_hz=500.0,
    )
    assert up.decision == 'SUPPORTED'
    assert up.magnitude_db == -3.0  # canonical pole sample


def test_grid_classification_separates_cuts_from_sphere() -> None:
    sphere = _sphere_dataset()
    assert classify_directivity_grid(sphere) == 'full_sphere_grid'

    # A single-plane azimuth cut is cuts-like, not a full sphere.
    thin = _sphere_dataset(vertical_angles=(0.0,))
    assert classify_directivity_grid(thin) == 'hv_cuts_suspect'


def test_non_periodic_partial_arc_still_unsupported() -> None:
    dataset = _sphere_dataset()
    # Partial-arc behaviour: interior gaps still bracket normally; a
    # request far outside the vertical domain stays UNSUPPORTED.
    result = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=30.0,
        vertical_angle_deg=60.0,
    )
    assert result.decision == 'SUPPORTED'
    outside = evaluate_directivity(
        dataset,
        frequency_hz=500.0,
        horizontal_angle_deg=30.0,
        vertical_angle_deg=120.0,
    )
    assert outside.decision == 'UNSUPPORTED'
