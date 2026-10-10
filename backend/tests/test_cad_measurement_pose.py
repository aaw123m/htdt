"""#732: planned target vs. observed capsule pose — separate authorities."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_measurement_pose_observation_repository import (
    MeasurementPoseObservationRepository,
)
from htdt.cad_measurement_pose import (
    SpatialUncertainty,
    UNKNOWN_UNCERTAINTY,
    build_pose_observation,
    evaluate_pose_delta,
)
from htdt.cad_scene import Position3


def _observed(
    *,
    method: str = 'tape_or_laser_from_datum',
    position: Position3 | None = None,
    uncertainty: SpatialUncertainty | None = None,
    **kwargs,
):
    return build_pose_observation(
        document_id='doc-1',
        method=method,
        observed_position_m=position,
        uncertainty=(
            uncertainty
            if uncertainty is not None
            else SpatialUncertainty(kind='radial_tolerance', radial_bound_m=0.05)
        ),
        **kwargs,
    )


def test_attestation_cannot_claim_zero_error() -> None:
    with pytest.raises(ValueError, match='zero-error|positive bound'):
        _observed(
            method='user_attested_within_tolerance',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            uncertainty=SpatialUncertainty(
                kind='radial_tolerance', radial_bound_m=0.0
            ),
        )


def test_attestation_requires_bounded_uncertainty() -> None:
    with pytest.raises(ValueError, match='attestation-within-tolerance'):
        _observed(
            method='user_attested_within_tolerance',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            uncertainty=SpatialUncertainty(
                kind='covariance', covariance_m2=(0.001,) * 9
            ),
        )


def test_attestation_with_bound_is_valid() -> None:
    obs = _observed(
        method='user_attested_within_tolerance',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        uncertainty=SpatialUncertainty(
            kind='radial_tolerance', radial_bound_m=0.10
        ),
    )
    assert obs.uncertainty.bound_m() == pytest.approx(0.10)


def test_unknown_method_carries_no_position() -> None:
    with pytest.raises(ValueError, match='cannot carry a position'):
        _observed(
            method='unknown',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            uncertainty=UNKNOWN_UNCERTAINTY,
        )


def test_absent_position_requires_unknown_uncertainty() -> None:
    with pytest.raises(ValueError, match='unknown uncertainty'):
        _observed(
            method='manual_numeric_measurement',
            position=None,
            uncertainty=SpatialUncertainty(
                kind='radial_tolerance', radial_bound_m=0.02
            ),
        )


def test_unknown_observation_is_valid_evidence() -> None:
    obs = _observed(method='unknown', position=None, uncertainty=UNKNOWN_UNCERTAINTY)
    assert obs.observed_position_m is None
    assert obs.uncertainty.kind == 'unknown'


def test_covariance_only_for_exact_methods() -> None:
    with pytest.raises(ValueError, match='covariance requires'):
        _observed(
            method='capture_derived',
            position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            uncertainty=SpatialUncertainty(
                kind='covariance', covariance_m2=(0.001,) * 9
            ),
        )


def test_semantic_hash_and_authority_ref() -> None:
    obs = _observed(
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )
    ref = obs.authority_ref()
    assert ref.semantic_hash_sha256 == obs.semantic_sha256
    with pytest.raises(ValueError, match='hash mismatch'):
        obs.__class__.model_validate(
            {
                **obs.model_dump(mode='python'),
                'limitations': ('x',),
            }
        )


def test_delta_within_tolerance() -> None:
    obs = _observed(
        position=Position3(x_m=1.02, y_m=1.0, z_m=1.0),
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )
    delta = evaluate_pose_delta(
        obs,
        planned_position_m=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        tolerance_bound_m=0.05,
    )
    assert delta.classification == 'within_tolerance'
    assert delta.delta_norm_m == pytest.approx(0.02)


def test_delta_outside_tolerance() -> None:
    obs = _observed(
        position=Position3(x_m=1.2, y_m=1.0, z_m=1.0),
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )
    delta = evaluate_pose_delta(
        obs,
        planned_position_m=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        tolerance_bound_m=0.05,
    )
    assert delta.classification == 'outside_tolerance'


def test_delta_unknown_when_evidence_missing() -> None:
    obs = _observed(method='unknown', uncertainty=UNKNOWN_UNCERTAINTY)
    delta = evaluate_pose_delta(
        obs,
        planned_position_m=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        tolerance_bound_m=0.05,
    )
    assert delta.classification == 'unknown'
    assert delta.delta_m is None


def test_delta_unknown_without_tolerance() -> None:
    obs = _observed(
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )
    delta = evaluate_pose_delta(
        obs,
        planned_position_m=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
    )
    assert delta.classification == 'unknown'


def test_repository_round_trip_and_immutability(tmp_path: Path) -> None:
    repo = MeasurementPoseObservationRepository(tmp_path / 'cad.sqlite3')
    obs = _observed(
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        measurement_ref='meas-1',
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )
    repo.save_observation(obs)
    # Idempotent re-save of identical content.
    repo.save_observation(obs)
    loaded = repo.get_observation(obs.observation_id)
    assert loaded == obs
    assert [
        o.observation_id
        for o in repo.list_observations_for_measurement('meas-1')
    ] == [obs.observation_id]

    tampered = obs.model_copy(update={'detail': 'x'})
    with pytest.raises(ValueError):
        repo.save_observation(
            obs.model_copy(update={'semantic_sha256': tampered.semantic_sha256})
            if False else _tampered(obs)
        )


def _tampered(obs):
    # Build a structurally valid but different record under the same id.
    from htdt.cad_measurement_pose import MeasurementPoseObservation
    import json
    from hashlib import sha256

    payload = obs.model_dump(mode='json')
    payload['limitations'] = ['tampered']
    identity = obs.identity_payload()
    identity['limitations'] = ['tampered']
    payload['semantic_sha256'] = sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()
    return MeasurementPoseObservation.model_validate(payload)


def test_binding_pose_states(tmp_path: Path) -> None:
    repo = MeasurementPoseObservationRepository(tmp_path / 'cad.sqlite3')
    obs = _observed(
        method='user_attested_within_tolerance',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        uncertainty=SpatialUncertainty(
            kind='radial_tolerance', radial_bound_m=0.10
        ),
        measurement_ref='meas-9',
        observed_at_utc='2026-09-24T00:00:00+00:00',
    )
    repo.save_observation(obs)
    delta = evaluate_pose_delta(
        obs,
        planned_position_m=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        tolerance_bound_m=0.10,
    )
    repo.save_delta(delta)
    assert repo.get_delta(delta.delta_id) == delta

    from htdt.cad_measurement_pose import MeasurementPoseBinding

    binding = MeasurementPoseBinding(
        measurement_ref='meas-9', observation=obs, delta=delta
    )
    assert binding.pose_state == 'confirmed_within_tolerance'
    assert MeasurementPoseBinding(measurement_ref='m').pose_state == 'not_recorded'
