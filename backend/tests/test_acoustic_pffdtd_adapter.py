from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from htdt.acoustic_benchmark import load_acoustic_benchmark_manifest
from htdt.acoustic_pffdtd_adapter import (
    compile_rigid_fixture_model,
    finite_record_pressure_transfer,
    pffdtd_velocity_potential_to_pressure_trace,
    pffdtd_velocity_potential_to_pressure_transfer,
    recombine_pffdtd_receiver_traces,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'benchmarks' / 'acoustics' / 'r100a_manifest.json'


def _fixture(fixture_id: str = 'wave-rigid-rectangular-modes-v1'):
    manifest = load_acoustic_benchmark_manifest(MANIFEST_PATH)
    return next(
        item
        for item in manifest.fixtures
        if item.fixture_id == fixture_id
    )


def test_rigid_fixture_compiler_preserves_box_authority_and_outward_triangles() -> None:
    fixture = _fixture()
    model = compile_rigid_fixture_model(fixture)
    rigid = model['mats_hash']['_RIGID']

    points = np.asarray(rigid['pts'], dtype=np.float64)
    triangles = np.asarray(rigid['tris'], dtype=np.int64)
    room_centroid = points.mean(axis=0)

    assert points.shape == (8, 3)
    assert triangles.shape == (12, 3)
    assert np.allclose(points.min(axis=0), [0.0, 0.0, 0.0])
    assert np.allclose(points.max(axis=0), [4.0, 5.0, 2.5])
    assert model['sources'][0]['xyz'] == [1.0, 1.0, 1.0]
    assert model['receivers'][0]['xyz'] == [3.0, 3.0, 1.2]

    for tri in triangles:
        vertices = points[tri]
        normal = np.cross(vertices[1] - vertices[0], vertices[2] - vertices[0])
        triangle_centroid = vertices.mean(axis=0)
        assert float(np.dot(normal, triangle_centroid - room_centroid)) > 0.0


def test_rigid_fixture_compiler_preserves_exact_concave_l_prism() -> None:
    fixture = _fixture('wave-concave-l-room-v1')
    model = compile_rigid_fixture_model(fixture)
    rigid = model['mats_hash']['_RIGID']

    points = np.asarray(rigid['pts'], dtype=np.float64)
    triangles = np.asarray(rigid['tris'], dtype=np.int64)

    assert points.shape == (16, 3)
    assert triangles.shape == (28, 3)
    assert model['sources'][0]['xyz'] == [1.0, 1.0, 1.0]
    assert model['receivers'][0]['xyz'] == [5.0, 1.0, 1.0]

    edge_counts: dict[tuple[int, int], int] = {}
    for triangle in triangles:
        for offset in range(3):
            edge = tuple(
                sorted(
                    (
                        int(triangle[offset]),
                        int(triangle[(offset + 1) % 3]),
                    )
                )
            )
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
    assert set(edge_counts.values()) == {2}

    signed_volume = sum(
        float(
            np.dot(
                points[triangle[0]],
                np.cross(points[triangle[1]], points[triangle[2]]),
            )
        )
        / 6.0
        for triangle in triangles
    )
    assert signed_volume == pytest.approx(50.0, abs=1.0e-10)

    for z_m in (0.0, 2.5):
        horizontal = [
            triangle
            for triangle in triangles
            if np.allclose(points[triangle, 2], z_m)
        ]
        assert len(horizontal) == 6
        area_xy = sum(
            abs(
                float(
                    (points[triangle[1], 0] - points[triangle[0], 0])
                    * (points[triangle[2], 1] - points[triangle[0], 1])
                    - (points[triangle[1], 1] - points[triangle[0], 1])
                    * (points[triangle[2], 0] - points[triangle[0], 0])
                )
            )
            * 0.5
            for triangle in horizontal
        )
        assert area_xy == pytest.approx(20.0, abs=1.0e-10)


def test_receiver_recombination_matches_upstream_trilinear_contract() -> None:
    raw = np.arange(24, dtype=np.float64).reshape(8, 3)
    weights = np.asarray([[0.1, 0.1, 0.1, 0.1, 0.15, 0.15, 0.15, 0.15]])
    expected = np.sum(
        (raw * weights.flat[:][:, None]).reshape((*weights.shape, -1)),
        axis=1,
    )

    actual = recombine_pffdtd_receiver_traces(
        raw,
        weights,
        receiver_count=1,
        nt=3,
    )

    assert actual.shape == (1, 3)
    assert np.allclose(actual, expected)


def test_receiver_recombination_rejects_authority_mismatch() -> None:
    with pytest.raises(ValueError, match='mismatch'):
        recombine_pffdtd_receiver_traces(
            np.zeros((7, 3), dtype=np.float64),
            np.zeros((1, 8), dtype=np.float64),
            receiver_count=1,
            nt=3,
        )


def test_velocity_potential_pressure_trace_recovers_linear_primary_field() -> None:
    dt = 0.001
    slope = 2.5
    density = 1.2
    times = np.arange(6, dtype=np.float64) * dt
    potential = 0.3 + slope * times

    pressure = pffdtd_velocity_potential_to_pressure_trace(
        potential,
        time_step_s=dt,
        density_kg_m3=density,
    )

    assert np.allclose(pressure, density * slope)


def test_finite_record_pressure_transfer_matches_direct_dtft_definition() -> None:
    dt = 0.001
    pressure = np.asarray([1.0, 2.0, -0.5, 0.25], dtype=np.float64)
    source = np.asarray([2.0, 0.0, 0.0, 0.0], dtype=np.float64)
    frequencies = np.asarray([100.0, 200.0], dtype=np.float64)
    times = np.arange(pressure.size, dtype=np.float64) * dt
    kernel = np.exp(+2j * np.pi * frequencies[:, None] * times[None, :])
    expected = (dt * (kernel @ pressure)) / (dt * (kernel @ source))

    actual = finite_record_pressure_transfer(
        pressure,
        source,
        time_step_s=dt,
        frequency_hz=frequencies,
    )

    assert np.allclose(actual, expected)


def test_velocity_potential_transfer_wrapper_uses_pressure_record_first() -> None:
    dt = 0.001
    density = 1.2
    times = np.arange(6, dtype=np.float64) * dt
    potential = 0.4 + 3.0 * times
    source = np.asarray([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
    frequencies = np.asarray([100.0, 200.0], dtype=np.float64)
    pressure = pffdtd_velocity_potential_to_pressure_trace(
        potential,
        time_step_s=dt,
        density_kg_m3=density,
    )

    expected = finite_record_pressure_transfer(
        pressure,
        source,
        time_step_s=dt,
        frequency_hz=frequencies,
    )
    actual = pffdtd_velocity_potential_to_pressure_transfer(
        potential,
        source,
        time_step_s=dt,
        frequency_hz=frequencies,
        density_kg_m3=density,
    )

    assert np.allclose(actual, expected)


def test_velocity_potential_pressure_transfer_rejects_zero_source_spectrum() -> None:
    with pytest.raises(ValueError, match='source spectrum is zero'):
        pffdtd_velocity_potential_to_pressure_transfer(
            np.ones(4, dtype=np.float64),
            np.zeros(4, dtype=np.float64),
            time_step_s=0.001,
            frequency_hz=np.asarray([100.0]),
            density_kg_m3=1.2,
        )
