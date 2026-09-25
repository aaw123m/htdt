from __future__ import annotations

from hashlib import sha256
import math

import pytest

from htdt.cad_equipment import FrequencyDomain
from htdt.cad_scene import Position3
from htdt.cad_spatial_field import (
    FieldPlaneRequest,
    FieldProbeSetRequest,
    RegularGridAxis,
    build_rectangular_mode_field,
    build_spatial_field_request,
    build_spatial_field_result,
    extract_field_slice,
    field_difference,
    probe_field,
)


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


DOMAIN = FrequencyDomain(minimum_hz=20.0, maximum_hz=200.0)


def _request(**overrides):
    kwargs = dict(
        acoustic_scene_snapshot_id='snapshot-1',
        acoustic_scene_snapshot_sha256=_hash('snapshot'),
        source_scenario_id='scenario-1',
        source_scenario_sha256=_hash('scenario'),
        solver_result_id='result-1',
        solver_result_sha256=_hash('result'),
        provider_id='provider-1',
        provider_version='1',
        quantity='pressure_magnitude_pa',
        frequency_hz=100.0,
        volume=None,
        plane=FieldPlaneRequest(axis_plane='xy', coordinate_m=1.0),
    )
    kwargs.update(overrides)
    return build_spatial_field_request(**kwargs)


def _axes() -> tuple[RegularGridAxis, RegularGridAxis, RegularGridAxis]:
    return (
        RegularGridAxis(name='x_m', origin_m=0.0, spacing_m=1.0, count=4),
        RegularGridAxis(name='y_m', origin_m=0.0, spacing_m=1.0, count=3),
        RegularGridAxis(name='z_m', origin_m=0.0, spacing_m=1.0, count=2),
    )


def _field(**overrides) -> object:
    request = _request()
    kwargs = dict(
        request=request,
        axes=_axes(),
        representation='complex_pressure',
        pressure_real=tuple(1.0 for _ in range(24)),
        pressure_imag=tuple(0.0 for _ in range(24)),
        absolute_pressure_reference=True,
        phasor_convention='exp(+i*omega*t)',
        valid_frequency_domain=DOMAIN,
    )
    kwargs.update(overrides)
    return build_spatial_field_result(**kwargs)


def test_request_requires_exactly_one_kind() -> None:
    request = _request()
    assert request.request_id.startswith('spatial-field-request:')
    with pytest.raises(ValueError, match='exactly one'):
        _request(
            plane=FieldPlaneRequest(axis_plane='xy', coordinate_m=1.0),
            probe_set=FieldProbeSetRequest(
                points=(Position3(x_m=0.0, y_m=0.0, z_m=0.0),)
            ),
        )


def test_result_binds_frequency_to_valid_domain() -> None:
    with pytest.raises(ValueError, match='outside the valid domain'):
        _field(valid_frequency_domain=FrequencyDomain(
            minimum_hz=110.0, maximum_hz=200.0
        ))


def test_magnitude_only_field_cannot_show_phase_or_complex() -> None:
    field = _field(
        representation='magnitude_only',
        pressure_real=None,
        pressure_imag=None,
        pressure_magnitude_pa=tuple(1.0 for _ in range(24)),
        phasor_convention=None,
    )
    supported, _ = field.supports_quantity('phase_deg')
    assert supported is False
    supported, _ = field.supports_quantity('pressure_magnitude_pa')
    assert supported is True
    with pytest.raises(ValueError, match='complex pressure authority'):
        extract_field_slice(
            field,
            FieldPlaneRequest(axis_plane='xy', coordinate_m=0.0),
            'phase_deg',
        )


def test_spl_requires_absolute_reference() -> None:
    field = _field(absolute_pressure_reference=False)
    supported, reason = field.supports_quantity('spl_db')
    assert supported is False
    assert 'absolute pressure reference' in reason


def test_exact_slice_is_grid_locked_and_masks_phase() -> None:
    field = build_rectangular_mode_field(
        request=_request(),
        room_size_m=(4.0, 3.0, 2.0),
        mode_indices=(1, 0, 0),
        axes=_axes(),
        amplitude_pa=1.0,
        valid_frequency_domain=DOMAIN,
    )
    slice_view = extract_field_slice(
        field,
        FieldPlaneRequest(axis_plane='xy', coordinate_m=0.0),
        'pressure_magnitude_pa',
    )
    assert slice_view.sample_state == 'exact'
    assert slice_view.row_axis == 'x_m'
    assert slice_view.column_axis == 'y_m'
    assert len(slice_view.rows) == 4  # x axis
    assert len(slice_view.rows[0]) == 3  # y axis
    # mode n_x=1 over Lx=4: p = cos(pi x / 4)
    assert slice_view.rows[0][0] == pytest.approx(1.0)
    assert slice_view.rows[2][0] == pytest.approx(0.0, abs=1e-12)

    phase_view = extract_field_slice(
        field,
        FieldPlaneRequest(axis_plane='xy', coordinate_m=0.0),
        'phase_deg',
        phase_mask_min_magnitude_pa=0.01,
    )
    # nodal positions (x index 2) masked, others unmasked
    assert (2, 0) in phase_view.masked_positions
    assert (0, 0) not in phase_view.masked_positions

    with pytest.raises(ValueError, match='does not coincide'):
        extract_field_slice(
            field,
            FieldPlaneRequest(axis_plane='xy', coordinate_m=0.4),
            'pressure_magnitude_pa',
        )


def test_probe_reports_exact_vs_snapped() -> None:
    field = _field()
    exact = probe_field(
        field, Position3(x_m=1.0, y_m=1.0, z_m=0.0), 'pressure_magnitude_pa'
    )
    assert exact.sample_state == 'exact'
    # An off-grid read in exact_samples mode is the nearest node — never
    # silently labelled 'interpolated' (#951).
    snapped = probe_field(
        field,
        Position3(x_m=1.02, y_m=1.0, z_m=0.0),
        'pressure_magnitude_pa',
    )
    assert snapped.sample_state == 'nearest_sample'
    assert snapped.nearest_sample_index == exact.nearest_sample_index
    assert snapped.sampled_position.x_m == pytest.approx(1.0)
    assert snapped.requested_position.x_m == pytest.approx(1.02)
    assert snapped.distance_m == pytest.approx(0.02)


def test_probe_trilinear_interpolation() -> None:
    # mode n_x=1 over Lx=4: p = cos(pi x / 4) — between x=1 and x=2 the true
    # trilinear blend differs from the nearest node value.
    field = build_rectangular_mode_field(
        request=_request(),
        room_size_m=(4.0, 3.0, 2.0),
        mode_indices=(1, 0, 0),
        axes=_axes(),
        amplitude_pa=1.0,
        valid_frequency_domain=DOMAIN,
    )
    probed = probe_field(
        field,
        Position3(x_m=1.5, y_m=0.0, z_m=0.0),
        'pressure_magnitude_pa',
        interpolation='trilinear',
    )
    assert probed.sample_state == 'interpolated'
    assert probed.sampled_position.x_m == pytest.approx(1.5)
    nearest = probe_field(
        field,
        Position3(x_m=1.5, y_m=0.0, z_m=0.0),
        'pressure_magnitude_pa',
    )
    assert nearest.sample_state == 'nearest_sample'
    assert probed.value != pytest.approx(nearest.value)
    cos1, cos2 = math.cos(math.pi / 4), math.cos(math.pi / 2)
    assert probed.value == pytest.approx(abs(0.5 * (cos1 + cos2)))
    # Phase is derived from the interpolated complex parts.
    phase = probe_field(
        field,
        Position3(x_m=1.5, y_m=0.0, z_m=0.0),
        'phase_deg',
        interpolation='trilinear',
    )
    assert phase.value == pytest.approx(0.0, abs=1e-9)


def test_probe_fails_closed_outside_domain() -> None:
    field = _field()
    with pytest.raises(ValueError, match='outside the sampled field domain'):
        probe_field(
            field,
            Position3(x_m=9.0, y_m=0.0, z_m=0.0),
            'pressure_magnitude_pa',
            interpolation='trilinear',
        )
    with pytest.raises(ValueError, match='outside the sampled field domain'):
        probe_field(
            field,
            Position3(x_m=9.0, y_m=0.0, z_m=0.0),
            'pressure_magnitude_pa',
        )


def test_energy_density_is_not_a_field_quantity() -> None:
    # #952: a dimensionless pressure proxy must never be labeled J/m3.
    field = _field()
    with pytest.raises(ValueError, match='unsupported quantity'):
        extract_field_slice(
            field,
            FieldPlaneRequest(axis_plane='xy', coordinate_m=0.0),
            'energy_density',
        )
    with pytest.raises(ValueError, match='unsupported quantity'):
        probe_field(
            field,
            Position3(x_m=0.0, y_m=0.0, z_m=0.0),
            'energy_density',
        )


def test_complex_pressure_is_not_a_scalar_view() -> None:
    field = _field()
    with pytest.raises(ValueError, match='two-component'):
        extract_field_slice(
            field,
            FieldPlaneRequest(axis_plane='xy', coordinate_m=0.0),
            'complex_pressure',
        )
    with pytest.raises(ValueError, match='two-component'):
        probe_field(
            field,
            Position3(x_m=0.0, y_m=0.0, z_m=0.0),
            'complex_pressure',
        )


def test_field_difference_is_fail_closed() -> None:
    left = _field()
    right = _field(
        pressure_imag=tuple(0.5 for _ in range(24)),
    )
    deltas = field_difference(left, right, difference_semantics='db_delta')
    assert len(deltas) == 24
    for value in deltas:
        assert math.isfinite(value)

    identical = field_difference(left, left, difference_semantics='db_delta')
    assert all(value == 0.0 for value in identical)

    mismatched_axes = _field(
        axes=(
            RegularGridAxis(name='x_m', origin_m=0.0, spacing_m=0.5, count=4),
            RegularGridAxis(name='y_m', origin_m=0.0, spacing_m=1.0, count=3),
            RegularGridAxis(name='z_m', origin_m=0.0, spacing_m=1.0, count=2),
        ),
        pressure_real=tuple(1.0 for _ in range(24)),
        pressure_imag=tuple(0.0 for _ in range(24)),
    )
    with pytest.raises(ValueError, match='incompatible'):
        field_difference(left, mismatched_axes, difference_semantics='db_delta')
