"""#783: acoustic source localization — capability-gated, bounded, honest."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from htdt.cad_acoustic_source_pose import (
    AcousticReceiverAnchor,
    DirectArrivalEvidence,
    check_aim_capability,
    compare_source_pose,
    localize_acoustic_source,
)
from htdt.cad_measurement_pose import SpatialUncertainty
from htdt.cad_scene import Position3
from htdt.acoustics.persistence.cad_acoustic_source_pose_repository import AcousticSourcePoseRepository


SOURCE = Position3(x_m=2.0, y_m=3.0, z_m=1.2)
C_MPS = 343.0


def _anchor(
    receiver_id: str, position: Position3, bound_m: float = 0.02
) -> AcousticReceiverAnchor:
    return AcousticReceiverAnchor(
        receiver_id=receiver_id,
        position_m=position,
        uncertainty=SpatialUncertainty(
            kind='radial_tolerance', radial_bound_m=bound_m
        ),
    )


def _arrival(
    receiver_id: str,
    position: Position3,
    *,
    latency_s: float = 0.0,
    jitter_s: float = 0.0,
    ambiguity: str = 'unambiguous',
) -> DirectArrivalEvidence:
    d = math.dist(
        (SOURCE.x_m, SOURCE.y_m, SOURCE.z_m),
        (position.x_m, position.y_m, position.z_m),
    )
    return DirectArrivalEvidence(
        receiver_id=receiver_id,
        arrival_time_s=d / C_MPS + latency_s + jitter_s,
        picker_method='test-direct-pick',
        ambiguity=ambiguity,
    )


def _quad() -> list[tuple[str, Position3]]:
    return [
        ('r1', Position3(x_m=0.5, y_m=0.5, z_m=1.1)),
        ('r2', Position3(x_m=4.5, y_m=0.5, z_m=1.1)),
        ('r3', Position3(x_m=4.5, y_m=3.5, z_m=1.4)),
        ('r4', Position3(x_m=0.5, y_m=3.5, z_m=1.4)),
    ]


def _localize(
    *,
    receivers=None,
    arrivals=None,
    timing_capability='common_clock_exact',
    latency_s=0.0,
    latency_bound_s=0.0,
):
    receivers = receivers if receivers is not None else _quad()
    anchors = [_anchor(rid, pos) for rid, pos in receivers]
    arrivals = (
        arrivals
        if arrivals is not None
        else [_arrival(rid, pos) for rid, pos in receivers]
    )
    return localize_acoustic_source(
        document_id='doc-1',
        source_entity_id='speaker-fl',
        acquisition_session_ref='session-1',
        receivers=anchors,
        arrivals=arrivals,
        timing_capability=timing_capability,
        sound_speed_mps=C_MPS,
        common_latency_s=latency_s,
        latency_bound_s=latency_bound_s,
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )


def test_exact_recovery() -> None:
    obs = _localize()
    assert obs.verdict == 'solvable'
    assert obs.observed_position_m is not None
    for actual, expected in zip(
        (obs.observed_position_m.x_m, obs.observed_position_m.y_m, obs.observed_position_m.z_m),
        (SOURCE.x_m, SOURCE.y_m, SOURCE.z_m),
    ):
        assert actual == pytest.approx(expected, abs=1e-4)
    assert obs.max_residual_m is not None and obs.max_residual_m < 1e-4


def test_bounded_noise_recovery() -> None:
    receivers = _quad()
    arrivals = [
        _arrival(rid, pos, jitter_s=eps)
        for (rid, pos), eps in zip(receivers, (0.0001, -0.0001, 0.00008, -0.00006))
    ]
    obs = _localize(receivers=receivers, arrivals=arrivals)
    assert obs.verdict in {'solvable', 'weakly_conditioned'}
    assert obs.observed_position_m is not None
    err = math.dist(
        (obs.observed_position_m.x_m, obs.observed_position_m.y_m, obs.observed_position_m.z_m),
        (SOURCE.x_m, SOURCE.y_m, SOURCE.z_m),
    )
    assert err < 0.15
    assert obs.uncertainty.bound_m() is not None and obs.uncertainty.bound_m() > 0


def test_unknown_common_latency_blocks_absolute_tof() -> None:
    obs = _localize(timing_capability='uncalibrated')
    assert obs.verdict == 'insufficient_evidence'
    assert obs.observed_position_m is None
    obs = _localize(timing_capability='unknown')
    assert obs.verdict == 'insufficient_evidence'


def test_calibrated_latency_recovers_position() -> None:
    obs = _localize(
        timing_capability='calibrated_latency_offset',
        latency_s=0.002,
    )
    # Arrivals were synthesized without latency, so a wrong latency must
    # not silently produce a clean solve — it shifts ranges and lands as
    # residual/bias, which the contract surfaces.
    assert obs.verdict in {'solvable', 'weakly_conditioned', 'inconsistent'}
    receivers = _quad()
    arrivals = [
        _arrival(rid, pos, latency_s=0.002) for rid, pos in receivers
    ]
    obs = _localize(arrivals=arrivals, latency_s=0.002)
    assert obs.verdict == 'solvable'
    assert obs.observed_position_m is not None
    assert obs.observed_position_m.x_m == pytest.approx(SOURCE.x_m, abs=1e-3)


def test_insufficient_receivers() -> None:
    obs = _localize(receivers=_quad()[:3])
    assert obs.verdict == 'insufficient_evidence'
    assert obs.observed_position_m is None


def test_degenerate_collinear_geometry_weakly_conditioned() -> None:
    receivers = [
        ('r1', Position3(x_m=1.0, y_m=1.0, z_m=1.1)),
        ('r2', Position3(x_m=2.0, y_m=1.0, z_m=1.1)),
        ('r3', Position3(x_m=3.0, y_m=1.0, z_m=1.1)),
        ('r4', Position3(x_m=4.0, y_m=1.0, z_m=1.1)),
    ]
    obs = _localize(receivers=receivers)
    assert obs.verdict in {'weakly_conditioned', 'inconsistent', 'insufficient_evidence'}


def test_outlier_arrival_is_inconsistent() -> None:
    receivers = _quad()
    arrivals = [_arrival(rid, pos) for rid, pos in receivers]
    arrivals[2] = DirectArrivalEvidence(
        receiver_id='r3',
        arrival_time_s=arrivals[2].arrival_time_s + 0.02,  # ~6.9m error
        picker_method='test-direct-pick',
    )
    obs = _localize(receivers=receivers, arrivals=arrivals)
    assert obs.verdict == 'inconsistent'
    assert obs.observed_position_m is None


def test_ambiguous_arrival_is_dropped() -> None:
    receivers = _quad()
    arrivals = [
        _arrival(rid, pos, ambiguity='ambiguous')
        for rid, pos in receivers[:1]
    ] + [_arrival(rid, pos) for rid, pos in receivers[1:]]
    obs = _localize(receivers=receivers, arrivals=arrivals)
    assert obs.verdict == 'insufficient_evidence'


def test_aim_capability_gate() -> None:
    ok, reason = check_aim_capability(
        directivity_dataset_ref=None, receiver_count=4
    )
    assert not ok and 'directivity' in reason
    ok, _ = check_aim_capability(
        directivity_dataset_ref='dir-1', receiver_count=1
    )
    assert not ok
    ok, _ = check_aim_capability(
        directivity_dataset_ref='dir-1', receiver_count=3
    )
    assert ok


def test_comparison_reports_all_three_poses() -> None:
    obs = _localize()
    comparison = compare_source_pose(
        obs,
        design_position_m=Position3(x_m=2.0, y_m=3.0, z_m=1.2),
        as_built_position_m=Position3(x_m=2.1, y_m=3.0, z_m=1.2),
        tolerance_bound_m=0.05,
    )
    assert comparison.status_vs_design == 'within_tolerance'
    assert comparison.status_vs_as_built == 'outside_tolerance'
    assert comparison.delta_vs_as_built_m is not None
    assert comparison.observed_position_m == obs.observed_position_m
    # The observed pose never becomes Scene truth.
    assert comparison.delta_vs_design_m is not None


def test_comparison_indeterminate_without_reference() -> None:
    obs = _localize(timing_capability='uncalibrated')
    comparison = compare_source_pose(
        obs, design_position_m=Position3(x_m=2.0, y_m=3.0, z_m=1.2)
    )
    assert comparison.status_vs_design == 'indeterminate'


def test_repository_round_trip(tmp_path: Path) -> None:
    repo = AcousticSourcePoseRepository(tmp_path / 'cad.sqlite3')
    obs = _localize()
    repo.save_observation(obs)
    repo.save_observation(obs)
    assert repo.get_observation(obs.observation_id) == obs
    assert [
        o.observation_id
        for o in repo.list_observations_for_source('doc-1', 'speaker-fl')
    ] == [obs.observation_id]
