"""Equipment acoustic self-noise authority tests (#1011)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_equipment_self_noise import (
    EquipmentAcousticNoiseProfile,
    EquipmentNoiseBand,
    build_equipment_noise_profile,
    combine_noise_sources_energy,
    evaluate_equipment_noise_readiness,
    predict_free_field_listener_level,
)


def _provenance(digit: str = '2', kind='measured'):
    return (
        EquipmentDataProvenance(
            evidence_kind=kind,
            source_name='measurement rig',
            source_version='1',
            source_reference='lab session 7',
            source_sha256=digit * 64,
        ),
    )


def test_manufacturer_overall_is_not_transformed():
    profile = build_equipment_noise_profile(
        noise_profile_id='np-1',
        equipment_id='proj-1',
        quantity='manufacturer_declared_overall',
        operating_state='typical_load',
        weighting='A',
        overall_level_db=24.0,
        provenance=_provenance(kind='manufacturer'),
    )
    assert profile.quantity == 'manufacturer_declared_overall'
    report = evaluate_equipment_noise_readiness(profile=profile)
    checks = {c.check: c.status for c in report.checks}
    assert checks['quantity_declared'] == 'PASS'
    assert checks['manufacturer_figure_contained'] == 'UNKNOWN'


def test_pressure_quantity_requires_distance():
    with pytest.raises(ValidationError, match='reference distance'):
        build_equipment_noise_profile(
            equipment_id='pc-1',
            quantity='sound_pressure_at_reference_point',
            operating_state='idle',
            overall_level_db=30.0,
            provenance=_provenance(),
        )


def test_octave_band_quantity_requires_bands():
    with pytest.raises(ValidationError, match='band level'):
        build_equipment_noise_profile(
            equipment_id='avr-1',
            quantity='octave_band_sound_power',
            operating_state='idle',
            provenance=_provenance(),
        )


def test_measured_installed_source_is_ready():
    profile = build_equipment_noise_profile(
        equipment_id='proj-1',
        quantity='octave_band_sound_pressure',
        operating_state='idle',
        weighting='A',
        reference_distance_m=1.0,
        radiation_model='omni_approximation',
        bands=(
            EquipmentNoiseBand(band_center_hz=250.0, band_level_db=28.0),
            EquipmentNoiseBand(band_center_hz=500.0, band_level_db=30.0),
            EquipmentNoiseBand(band_center_hz=1000.0, band_level_db=27.0),
        ),
        provenance=_provenance(),
    )
    report = evaluate_equipment_noise_readiness(profile=profile)
    checks = {c.check: c.status for c in report.checks}
    assert checks['operating_state_bound'] == 'PASS'
    assert checks['level_recorded'] == 'PASS'
    # omni approximation is labelled, never claimed as measured
    assert checks['directivity_labelled'] == 'UNKNOWN'


def test_unknown_state_and_quantity_stay_unknown():
    profile = build_equipment_noise_profile(
        equipment_id='avr-1',
        provenance=_provenance(),
    )
    report = evaluate_equipment_noise_readiness(profile=profile)
    checks = {c.check: c.status for c in report.checks}
    assert checks['operating_state_bound'] == 'UNKNOWN'
    assert checks['quantity_declared'] == 'UNKNOWN'
    assert checks['level_recorded'] == 'UNKNOWN'


def test_sources_combine_by_energy_not_sum():
    combined = combine_noise_sources_energy((24.0, 24.0))
    assert combined is not None
    assert 26.9 < combined < 27.1  # +3 dB, not 48
    assert combine_noise_sources_energy(()) is None
    # different levels
    combined = combine_noise_sources_energy((30.0, 20.0))
    assert combined is not None
    assert 30.0 < combined < 31.0


def test_free_field_listener_bound():
    level = predict_free_field_listener_level(
        source_level_db=30.0,
        reference_distance_m=1.0,
        listener_distance_m=2.0,
    )
    assert 23.9 < level < 24.1  # -6 dB per doubling
    with pytest.raises(ValueError, match='positive'):
        predict_free_field_listener_level(
            source_level_db=30.0,
            reference_distance_m=0.0,
            listener_distance_m=1.0,
        )


def test_profile_hash_integrity():
    profile = build_equipment_noise_profile(
        equipment_id='pc-1',
        quantity='sound_power_level',
        operating_state='high_load',
        overall_level_db=35.0,
        provenance=_provenance(),
    )
    payload = profile.model_dump(mode='python')
    payload['overall_level_db'] = 40.0
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        EquipmentAcousticNoiseProfile(**payload)
