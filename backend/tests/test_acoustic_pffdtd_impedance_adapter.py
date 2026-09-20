from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from htdt.acoustic_benchmark import (
    AcousticBenchmarkFixture,
    AcousticMaterial,
    SpecificImpedancePoint,
    load_acoustic_benchmark_manifest,
)
from htdt.acoustic_pffdtd_impedance_adapter import (
    PFFDTD_IMPEDANCE_MAPPING_ID,
    PFFDTD_IMPEDANCE_MAPPING_VERSION,
    compile_frequency_independent_resistive_impedance_boundary,
    compile_impedance_fixture_boundary,
    compile_impedance_fixture_model,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'


def _fixture():
    manifest = load_acoustic_benchmark_manifest(MANIFEST_PATH)
    return next(
        item
        for item in manifest.fixtures
        if item.fixture_id == 'wave-normal-incidence-impedance-v1'
    )


def test_impedance_fixture_maps_exact_physical_authority_to_pffdtd_def() -> None:
    fixture = _fixture()

    boundary = compile_impedance_fixture_boundary(fixture)
    model = compile_impedance_fixture_model(fixture)

    assert boundary['material_id'] == 'z-2z0'
    assert boundary['frequencies_hz'] == (100.0, 200.0, 300.0)
    assert boundary['density_kg_m3'] == pytest.approx(1.2)
    assert boundary['sound_speed_m_s'] == pytest.approx(343.0)
    assert boundary['characteristic_impedance_pa_s_m'] == pytest.approx(411.6)
    assert boundary['physical_resistance_pa_s_m'] == pytest.approx(823.2)
    assert boundary['physical_reactance_pa_s_m'] == 0.0
    assert boundary['normalized_impedance'] == pytest.approx(2.0)
    assert boundary['normalized_admittance'] == pytest.approx(0.5)
    assert np.allclose(boundary['def_coefficients'], [[0.0, 2.0, 0.0]])

    assert set(model['mats_hash']) == {'_RIGID', 'z-2z0'}
    assert len(model['mats_hash']['_RIGID']['tris']) == 10
    assert len(model['mats_hash']['z-2z0']['tris']) == 2
    assert model['sources'][0]['xyz'] == [4.0, 2.0, 1.0]
    assert model['receivers'][0]['xyz'] == [3.0, 2.0, 1.0]


def test_impedance_fixture_refuses_reactive_table_without_fitting_authority() -> None:
    fixture = _fixture()
    payload = fixture.model_dump(mode='python')
    impedance = next(
        item for item in payload['materials'] if item['material_id'] == 'z-2z0'
    )
    impedance['specific_impedance'][1]['reactance_pa_s_m'] = 25.0
    reactive = AcousticBenchmarkFixture.model_validate(payload)

    with pytest.raises(ValueError, match='refuses reactive impedance authority'):
        compile_impedance_fixture_boundary(reactive)


def test_impedance_fixture_refuses_frequency_varying_table_without_fitting_authority() -> None:
    fixture = _fixture()
    payload = fixture.model_dump(mode='python')
    impedance = next(
        item for item in payload['materials'] if item['material_id'] == 'z-2z0'
    )
    impedance['specific_impedance'][1]['resistance_pa_s_m'] = 900.0
    varying = AcousticBenchmarkFixture.model_validate(payload)

    with pytest.raises(ValueError, match='refuses frequency-varying resistance'):
        compile_impedance_fixture_boundary(varying)



def test_reusable_r100b_mapping_preserves_exact_mapping_authority() -> None:
    material = AcousticMaterial(
        material_id='explicit-z',
        provenance='focused R130B exact impedance fixture',
        version='7',
        wave_model='specific_impedance_table',
        specific_impedance=(
            SpecificImpedancePoint(
                frequency_hz=40.0,
                resistance_pa_s_m=823.2,
                reactance_pa_s_m=0.0,
            ),
            SpecificImpedancePoint(
                frequency_hz=80.0,
                resistance_pa_s_m=823.2,
                reactance_pa_s_m=0.0,
            ),
        ),
    )

    mapped = compile_frequency_independent_resistive_impedance_boundary(
        material=material,
        frequencies_hz=(40.0, 80.0),
        density_kg_m3=1.2,
        sound_speed_m_s=343.0,
    )

    assert mapped['mapping_id'] == PFFDTD_IMPEDANCE_MAPPING_ID
    assert mapped['mapping_version'] == PFFDTD_IMPEDANCE_MAPPING_VERSION
    assert mapped['physical_resistance_pa_s_m'] == pytest.approx(823.2)
    assert mapped['characteristic_impedance_pa_s_m'] == pytest.approx(411.6)
    assert mapped['normalized_impedance'] == pytest.approx(2.0)
    assert np.allclose(
        mapped['def_coefficients'],
        np.asarray([[0.0, 2.0, 0.0]], dtype=np.float64),
        rtol=0.0,
        atol=1.0e-15,
    )


def test_reusable_r100b_mapping_never_converts_unsupported_material_to_impedance() -> None:
    material = AcousticMaterial(
        material_id='not-impedance',
        provenance='negative scalar/statistical fixture',
        version='1',
        wave_model='unsupported',
    )

    with pytest.raises(ValueError, match='refuses unsupported wave material'):
        compile_frequency_independent_resistive_impedance_boundary(
            material=material,
            frequencies_hz=(40.0, 80.0),
            density_kg_m3=1.2,
            sound_speed_m_s=343.0,
        )
