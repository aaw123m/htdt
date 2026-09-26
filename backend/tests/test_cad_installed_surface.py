"""#1033: in-situ installed-surface evidence + promotion."""

from __future__ import annotations

import pytest

from htdt.cad_installed_surface import (
    InstalledSurfaceAcousticMeasurement,
    MeasuredSurfaceQuantity,
    build_installed_surface_measurement,
    promote_installed_measurement,
)
from htdt.cad_scene import Position3

REV_SHA = 'a' * 64


def _impedance_q(**overrides):
    kwargs = {
        'quantity': 'surface_impedance',
        'frequency_hz': (250.0, 500.0, 1000.0),
        'values': (400.0, 350.0, 300.0),
        'unit_label': 'Pa s/m',
        'phase_deg': (10.0, 5.0, 2.0),
    }
    kwargs.update(overrides)
    return MeasuredSurfaceQuantity(**kwargs)


def _measurement(**overrides):
    kwargs = {
        'document_id': 'doc-1',
        'scene_revision_id': 'rev-1',
        'scene_content_hash': REV_SHA,
        'surface_id': 'wall-east',
        'treatment_id': 'panel-T17',
        'patch_location': 'center',
        'patch_extent_m': (0.6, 0.6),
        'patch_center': Position3(x_m=4.0, y_m=2.0, z_m=1.0),
        'method': 'two_microphone_in_situ',
        'method_version': 'tm-1.2',
        'calibration_state': 'calibrated',
        'incidence': 'exact_angle',
        'incidence_angle_deg': 30.0,
        'air_gap_mm': 50.0,
        'backing': 'concrete',
        'quantities': (_impedance_q(),),
        'quality': 'pass',
        'provenance': 'user measurement session 2026-09-20',
        'created_at_utc': '2026-09-20T00:00:00+00:00',
    }
    kwargs.update(overrides)
    return build_installed_surface_measurement(**kwargs)


def test_measurement_identity_pins_scene_patch_and_method():
    m = _measurement()
    assert m.measurement_sha256 != '0' * 64
    same = _measurement(measurement_id=m.measurement_id)
    assert same.measurement_sha256 == m.measurement_sha256
    # Method identity is part of identity — a different version differs.
    other = _measurement(
        measurement_id=m.measurement_id, method_version='tm-2.0'
    )
    assert other.measurement_sha256 != m.measurement_sha256


def test_exact_angle_requires_angle_and_invalid_forbids_quantities():
    with pytest.raises(ValueError, match='incidence_angle_deg'):
        _measurement(incidence='exact_angle', incidence_angle_deg=None)
    with pytest.raises(ValueError, match='invalid'):
        _measurement(quality='invalid', limitations=('x',))


def test_non_pass_quality_requires_explicit_limitations():
    with pytest.raises(ValueError, match='contamination note'):
        _measurement(quality='limited')
    m = _measurement(
        quality='limited', contamination_note='nearby corner reflection'
    )
    assert m.quality == 'limited'


def test_promotion_carries_provenance_method_and_incidence():
    m = _measurement()
    evidence = promote_installed_measurement(
        m,
        m.quantities[0],
        material_id='mat-project-local-t17',
        created_at_utc='2026-09-20T01:00:00+00:00',
    )
    assert evidence.provenance_class == 'user_in_situ_measured'
    assert evidence.material_id == 'mat-project-local-t17'
    assert evidence.method == 'two_microphone_in_situ:tm-1.2'
    assert evidence.source_label == f'in-situ:{m.measurement_id}'
    # exact_angle preserves the measured angle as oblique incidence.
    assert evidence.incidence == 'oblique'
    assert evidence.incidence_angle_deg == 30.0
    # Phase survives for a complex quantity.
    assert evidence.phase_deg == (10.0, 5.0, 2.0)
    # Patch locality is retained as a limitation, not generalized.
    assert any('local patch evidence: center' in s for s in evidence.limitations)


def test_normal_incidence_tube_data_stays_normal():
    m = _measurement(
        method='impedance_tube_import',
        incidence='normal',
        incidence_angle_deg=None,
        quantities=(
            _impedance_q(
                quantity='normal_incidence_absorption_coefficient',
                values=(0.4, 0.7, 0.9),
                unit_label='ratio',
                phase_deg=None,
            ),
        ),
    )
    evidence = promote_installed_measurement(
        m,
        m.quantities[0],
        material_id='mat-1',
        created_at_utc='2026-09-20T01:00:00+00:00',
    )
    assert evidence.quantity == 'normal_incidence_absorption_coefficient'
    assert evidence.incidence == 'normal'
    assert evidence.incidence_angle_deg is None
    assert any('ISO 10534-2' in s for s in evidence.limitations)


def test_diffuse_like_never_promotes_as_random_incidence():
    m = _measurement(
        incidence='field_or_diffuse_like',
        incidence_angle_deg=None,
    )
    evidence = promote_installed_measurement(
        m,
        m.quantities[0],
        material_id='mat-1',
        created_at_utc='2026-09-20T01:00:00+00:00',
    )
    # Honest unknown incidence — no silent upgrade to 'random'.
    assert evidence.incidence == 'unknown'


def test_invalid_and_unpromotable_quantities_fail_closed():
    m = _measurement(quality='limited', contamination_note='finite patch')
    foreign = _impedance_q(values=(1.0, 1.0, 1.0))
    with pytest.raises(ValueError, match='come from the measurement'):
        promote_installed_measurement(
            m, foreign, material_id='mat-1',
            created_at_utc='2026-09-20T01:00:00+00:00',
        )
    oblique_abs = _measurement(
        quantities=(
            _impedance_q(
                quantity='oblique_absorption_coefficient',
                values=(0.5, 0.6, 0.7),
                unit_label='ratio',
                phase_deg=None,
            ),
        ),
    )
    with pytest.raises(ValueError, match='cannot promote'):
        promote_installed_measurement(
            oblique_abs,
            oblique_abs.quantities[0],
            material_id='mat-1',
            created_at_utc='2026-09-20T01:00:00+00:00',
        )
    unknown_q = _measurement(
        quality='unknown', limitations=('unverified import',)
    )
    with pytest.raises(ValueError, match='unknown-quality'):
        promote_installed_measurement(
            unknown_q,
            unknown_q.quantities[0],
            material_id='mat-1',
            created_at_utc='2026-09-20T01:00:00+00:00',
        )


def test_phase_rejected_for_non_complex_quantity():
    with pytest.raises(ValueError, match='phase_deg'):
        _impedance_q(
            quantity='normal_incidence_absorption_coefficient',
            phase_deg=(1.0, 2.0, 3.0),
        )
