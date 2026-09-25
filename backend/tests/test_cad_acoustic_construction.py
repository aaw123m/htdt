from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.acoustic_benchmark import SpecificImpedancePoint
from htdt.cad_acoustic_construction import (
    AcousticConstructionDefinition,
    ConstructionLayer,
    build_acoustic_construction,
    compare_modelled_vs_measured,
    derive_material_evidence,
    evidence_as_specific_impedance,
    miki_layer_medium,
    surface_impedance,
)
from htdt.cad_acoustic_material import build_acoustic_material

NOW = '2026-09-24T00:00:00+00:00'


def _construction(**overrides) -> AcousticConstructionDefinition:
    payload = {
        'construction_id': 'acoustic-construction:test-1',
        'label': 'porous 100mm on rigid',
        'layers': (
            ConstructionLayer(
                kind='porous',
                thickness_m=0.1,
                airflow_resistivity_pa_s_m2=10000.0,
            ),
            ConstructionLayer(kind='rigid_backing'),
        ),
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_acoustic_construction(**payload)


def test_construction_requires_rigid_backing() -> None:
    with pytest.raises(ValidationError):
        _construction(
            layers=(
                ConstructionLayer(
                    kind='porous',
                    thickness_m=0.1,
                    airflow_resistivity_pa_s_m2=10000.0,
                ),
            )
        )


def test_porous_requires_explicit_resistivity() -> None:
    with pytest.raises(ValidationError):
        ConstructionLayer(kind='porous', thickness_m=0.1)


def test_miki_medium_reference_point() -> None:
    # f/sigma = 0.05: Zc/rho_c = 1 + 0.07*X^-0.632 - j*0.107*X^-0.632
    zc, k = miki_layer_medium(10000.0, 500.0)
    rho_c = 1.204 * 343.0
    x = 0.05
    expected_re = rho_c * (1.0 + 0.0700 * x**-0.632)
    expected_im = rho_c * (-0.1070 * x**-0.632)
    assert zc.real == pytest.approx(expected_re, rel=1e-9)
    assert zc.imag == pytest.approx(expected_im, rel=1e-9)
    # lossy medium: negative imaginary wavenumber attenuates the wave
    assert k.imag < 0.0


def test_rigid_backed_porous_absorption_reference() -> None:
    # Hand-computed TMM/Miki fixture: 100 mm porous, sigma=10k Pa.s/m^2,
    # f=500 Hz -> Z_in ~ (441 - j221) Pa.s/m, alpha ~ 0.94.
    z = surface_impedance(_construction(), 500.0)
    assert z.real == pytest.approx(441.0, abs=6.0)
    assert z.imag == pytest.approx(-221.0, abs=6.0)
    evidence = derive_material_evidence(
        _construction(), (500.0,), created_at_utc=NOW
    )
    point = evidence.points[0]
    assert point.absorption == pytest.approx(0.936, abs=0.01)
    assert point.validity == 'VALID'


def test_air_gap_increases_low_frequency_absorption() -> None:
    porous_only = _construction()
    with_air = _construction(
        construction_id='acoustic-construction:test-2',
        layers=(
            ConstructionLayer(
                kind='porous',
                thickness_m=0.1,
                airflow_resistivity_pa_s_m2=10000.0,
            ),
            ConstructionLayer(kind='air', thickness_m=0.1),
            ConstructionLayer(kind='rigid_backing'),
        ),
    )
    alpha_no_gap = derive_material_evidence(
        porous_only, (250.0,), created_at_utc=NOW
    ).points[0].absorption
    alpha_gap = derive_material_evidence(
        with_air, (250.0,), created_at_utc=NOW
    ).points[0].absorption
    assert alpha_gap > alpha_no_gap
    # layer order matters: porous-in-front != air-in-front
    assert (
        surface_impedance(porous_only, 250.0)
        != surface_impedance(with_air, 250.0)
    )


def test_validity_gating_fail_closed_and_exploratory() -> None:
    construction = _construction()  # sigma=1e4 -> valid f in [100, 10000]
    with pytest.raises(ValueError, match='outside the Miki'):
        derive_material_evidence(construction, (50.0,), created_at_utc=NOW)
    evidence = derive_material_evidence(
        construction,
        (50.0, 500.0),
        created_at_utc=NOW,
        allow_outside_validity=True,
    )
    by_freq = {p.frequency_hz: p for p in evidence.points}
    assert by_freq[50.0].validity == 'OUTSIDE_MODEL_VALIDITY'
    assert by_freq[500.0].validity == 'VALID'
    assert evidence.valid_frequency_range_hz == (500.0, 500.0)
    assert any('outside' in d for d in evidence.diagnostics)


def test_resistivity_outside_miki_envelope() -> None:
    construction = _construction(
        layers=(
            ConstructionLayer(
                kind='porous',
                thickness_m=0.1,
                airflow_resistivity_pa_s_m2=50.0,
            ),
            ConstructionLayer(kind='rigid_backing'),
        )
    )
    assert construction.parameter_diagnostics()
    with pytest.raises(ValueError, match='outside the Miki'):
        derive_material_evidence(
            construction, (500.0,), created_at_utc=NOW
        )


def test_evidence_binds_construction_and_is_deterministic() -> None:
    first = derive_material_evidence(
        _construction(), (200.0, 500.0), created_at_utc=NOW
    )
    second = derive_material_evidence(
        _construction(), (200.0, 500.0), created_at_utc=NOW
    )
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.construction_sha256 == _construction().semantic_sha256
    assert first.evidence_kind == 'modelled'
    assert any('modelled, not measured' in lim for lim in first.limitations)
    assert any('normal incidence' in lim for lim in first.limitations)


def test_passivity_holds_across_grid() -> None:
    evidence = derive_material_evidence(
        _construction(),
        tuple(f * 100.0 for f in (1, 2, 4, 8, 16, 32, 64, 100)),
        created_at_utc=NOW,
    )
    for point in evidence.points:
        assert point.impedance_re_pa_s_m >= -1e-9
        assert -1.0 <= point.absorption <= 1.0 + 1e-9


def test_specific_impedance_projection_feeds_material_authority() -> None:
    evidence = derive_material_evidence(
        _construction(), (200.0, 500.0, 1000.0), created_at_utc=NOW
    )
    points = evidence_as_specific_impedance(evidence)
    assert len(points) == 3
    material = build_acoustic_material(
        label='modelled porous',
        provenance='modelled:htdt-tmm-1/miki_1990',
        wave_model='specific_impedance_table',
        specific_impedance=points,
        created_at_utc=NOW,
    )
    assert material.wave_model == 'specific_impedance_table'


def test_modelled_vs_measured_residual() -> None:
    evidence = derive_material_evidence(
        _construction(), (500.0,), created_at_utc=NOW
    )
    measured = build_acoustic_material(
        label='measured porous',
        provenance='lab',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=500.0,
                resistance_pa_s_m=400.0,
                reactance_pa_s_m=-200.0,
            ),
        ),
        created_at_utc=NOW,
    )
    residual = compare_modelled_vs_measured(evidence, measured)
    assert residual.common_frequency_range_hz == (500.0, 500.0)
    assert len(residual.impedance_residual) == 1

    rigid = build_acoustic_material(
        label='rigid wall',
        provenance='lab',
        wave_model='rigid',
        created_at_utc=NOW,
    )
    blocked = compare_modelled_vs_measured(evidence, rigid)
    assert blocked.common_frequency_range_hz is None
