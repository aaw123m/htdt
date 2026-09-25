from __future__ import annotations

import numpy as np
import pytest

from htdt.cad_spatial_reproduction import (
    load_sofa_dataset_profile,
    normalize_spherical_position,
)

NOW = '2026-09-24T00:00:00+00:00'


def _write_sofa(path, *, convention: str = 'SimpleFreeFieldHRIR') -> None:
    import h5py

    with h5py.File(path, 'w') as handle:
        handle.attrs['Conventions'] = np.bytes_('SOFA')
        handle.attrs['SOFAConventions'] = np.bytes_(convention)
        handle.attrs['DataType'] = np.bytes_('FIR')
        handle.attrs['RoomType'] = np.bytes_('free field')
        handle.attrs['GLOBAL:DatabaseName'] = np.bytes_('FIXTURE-HRTF')
        handle.attrs['GLOBAL:Title'] = np.bytes_('fixture dataset')
        handle.attrs['GLOBAL:Organization'] = np.bytes_('HTDT test')
        handle.attrs['GLOBAL:ListenerShortName'] = np.bytes_('generic')
        # 3 measurements: front, left (+90), behind-right (250 -> -110)
        source = np.array(
            [
                [0.0, 0.0, 1.5],
                [90.0, 0.0, 1.5],
                [250.0, 10.0, 2.0],
            ],
            dtype=np.float64,
        )
        handle.create_dataset('SourcePosition', data=source)
        handle.create_dataset(
            'Data.SamplingRate', data=np.array([48000.0])
        )
        # Data.IR: M=3 measurements, R=2 receivers, N=4 taps
        handle.create_dataset(
            'Data.IR', data=np.zeros((3, 2, 4), dtype=np.float64)
        )


def test_normalize_spherical_position() -> None:
    az, el, d = normalize_spherical_position(
        azimuth_deg=250.0, elevation_deg=10.0, distance_m=2.0
    )
    assert az == pytest.approx(-110.0)
    assert el == pytest.approx(10.0)
    assert d == pytest.approx(2.0)
    # elevation clamps to +-90
    _, el2, _ = normalize_spherical_position(
        azimuth_deg=0.0, elevation_deg=95.0, distance_m=1.0
    )
    assert el2 == 90.0
    with pytest.raises(ValueError):
        normalize_spherical_position(
            azimuth_deg=0.0, elevation_deg=0.0, distance_m=0.0
        )


def test_load_sofa_profile_normalizes_and_binds(tmp_path) -> None:
    sofa = tmp_path / 'fixture.sofa'
    _write_sofa(sofa)
    profile = load_sofa_dataset_profile(
        sofa,
        profile_id='spatial-profile:fixture-1',
        profile_version='1',
        personalization_scope='generic',
        license_kind='cc0_public',
        created_at_utc=NOW,
    )
    assert profile.convention == 'SimpleFreeFieldHRIR'
    assert profile.measurement_count == 3
    assert profile.receiver_count == 2
    assert profile.emitter_count == 4
    assert profile.sample_rate_hz == pytest.approx(48000.0)
    points = {p.measurement_index: p for p in profile.source_points}
    assert points[0].azimuth_deg == pytest.approx(0.0)
    assert points[1].azimuth_deg == pytest.approx(90.0)
    assert points[2].azimuth_deg == pytest.approx(-110.0)
    assert points[2].elevation_deg == pytest.approx(10.0)
    assert profile.dataset_name == 'FIXTURE-HRTF'
    assert profile.global_organization == 'HTDT test'
    assert profile.personalization_scope == 'generic'
    assert profile.license_kind == 'cc0_public'


def test_brir_convention_rejected_no_double_room(tmp_path) -> None:
    sofa = tmp_path / 'brir.sofa'
    _write_sofa(sofa, convention='SingleRoomDRIR')
    with pytest.raises(ValueError, match='SimpleFreeFieldHRIR'):
        load_sofa_dataset_profile(
            sofa,
            profile_id='spatial-profile:brir',
            profile_version='1',
            personalization_scope='generic',
            license_kind='unknown',
            created_at_utc=NOW,
        )


def test_individualized_scope_is_explicit(tmp_path) -> None:
    sofa = tmp_path / 'own.sofa'
    _write_sofa(sofa)
    profile = load_sofa_dataset_profile(
        sofa,
        profile_id='spatial-profile:me',
        profile_version='1',
        personalization_scope='individualized',
        license_kind='individualized_private',
        license_note='measured on my own head, do not redistribute',
        created_at_utc=NOW,
    )
    assert profile.personalization_scope == 'individualized'
    assert profile.license_kind == 'individualized_private'
    assert 'do not redistribute' in profile.license_note


def test_profile_deterministic(tmp_path) -> None:
    sofa = tmp_path / 'fixture.sofa'
    _write_sofa(sofa)
    first = load_sofa_dataset_profile(
        sofa,
        profile_id='spatial-profile:fixture-1',
        profile_version='1',
        personalization_scope='generic',
        license_kind='cc0_public',
        created_at_utc=NOW,
    )
    second = load_sofa_dataset_profile(
        sofa,
        profile_id='spatial-profile:fixture-1',
        profile_version='1',
        personalization_scope='generic',
        license_kind='cc0_public',
        created_at_utc=NOW,
    )
    assert first.semantic_sha256 == second.semantic_sha256
