from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_calibration import (
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.cad_target_profile import (
    CalibrationPlanTargetBinding,
    TargetProfileTolerance,
    build_plan_target_binding,
    build_target_profile,
)
from htdt.cad_target_profile_repository import (
    CadTargetProfileRepository,
    TargetProfileConflictError,
)

NOW = '2026-09-24T00:00:00+00:00'


def _scene(document_id: str = 'doc-1') -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    return scene_repository, revision, CadTargetProfileRepository(
        scene_repository
    )


def _curve() -> CadTargetCurve:
    return CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=20000.0, level_db=-3.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='reference_frequency', reference_frequency_hz=1000.0
        ),
    )


def _profile(revision, **overrides):
    payload = {
        'document_id': revision.document_id,
        'name': 'House Curve',
        'kind': 'in_room',
        'source': 'authored',
        'curve': _curve(),
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_target_profile(**payload)


def test_profile_is_versioned_and_hash_bound(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    profile = _profile(
        revision,
        normalization=CadTargetNormalizationCondition(
            method='reference_frequency', reference_frequency_hz=1000.0
        ),
        tolerances=(
            TargetProfileTolerance(from_hz=20.0, to_hz=200.0, tolerance_db=3.0),
        ),
        applies_to=('room:doc-1', 'speaker:fl'),
    )
    repository.save_profile(profile)
    stored = repository.get_profile(profile.profile_id, profile.version)
    assert stored == profile
    assert stored.curve.points[0].frequency_hz == 20.0
    assert repository.get_profile_by_hash(profile.semantic_sha256) == profile


def test_new_version_appends_old_version_is_never_rewritten(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    v1 = _profile(revision, profile_id='house', version='1')
    repository.save_profile(v1)
    v2 = _profile(revision, profile_id='house', version='2', name='House v2')
    repository.save_profile(v2)

    with pytest.raises(TargetProfileConflictError):
        repository.save_profile(v1)
    versions = repository.list_profile_versions('house')
    assert [item.version for item in versions] == ['1', '2']
    # The earlier payload survived untouched under its own hash.
    assert repository.get_profile('house', '1') == v1


class _StubPlan:
    def __init__(self, plan_semantic_sha256: str, document_id: str) -> None:
        self.plan_semantic_sha256 = plan_semantic_sha256
        self.document_id = document_id


class _StubCalibrationRepository:
    """Duck-typed CadCalibrationRepository stand-in for binding checks."""

    def __init__(self, plan=None) -> None:
        self._plan = plan

    def get_plan(self, plan_id: str):
        return self._plan


def test_binding_records_exact_plan_and_profile_hashes(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    profile = _profile(revision)
    repository.save_profile(profile)
    plan_sha = 'b' * 64
    repository.calibration_repository = _StubCalibrationRepository(
        _StubPlan(plan_sha, revision.document_id)
    )
    binding = build_plan_target_binding(
        document_id=revision.document_id,
        plan_id='plan-1',
        plan_semantic_sha256=plan_sha,
        profile=profile,
        bound_at_utc=NOW,
    )
    assert binding.profile_semantic_sha256 == profile.semantic_sha256
    repository.save_binding(binding)
    assert repository.get_binding(binding.binding_id) == binding
    assert repository.list_bindings_for_plan('plan-1') == (binding,)


def test_binding_fails_closed_on_missing_or_mismatched_authority(
    tmp_path: Path,
) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    profile = _profile(revision)
    repository.save_profile(profile)
    binding = build_plan_target_binding(
        document_id=revision.document_id,
        plan_id='plan-1',
        plan_semantic_sha256='b' * 64,
        profile=profile,
        bound_at_utc=NOW,
    )
    # No calibration repository at all — cannot prove the plan exists.
    with pytest.raises(ValueError, match='CadCalibrationRepository'):
        repository.save_binding(binding)
    # Repository present but the plan is absent.
    repository.calibration_repository = _StubCalibrationRepository()
    with pytest.raises(ValueError, match='persisted calibration plan'):
        repository.save_binding(binding)
    # Plan exists but the binding's pinned hash is for another payload.
    repository.calibration_repository = _StubCalibrationRepository(
        _StubPlan('c' * 64, revision.document_id)
    )
    with pytest.raises(ValueError, match='different plan payload'):
        repository.save_binding(binding)


def test_profile_semantic_hash_is_self_verified() -> None:
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        profile = _profile_document()
        CadTargetCurveProfile_model = type(profile)
        payload = profile.model_dump(mode='json')
        payload['name'] = 'tampered'
        CadTargetCurveProfile_model.model_validate(payload)


def _profile_document():
    return build_target_profile(
        document_id='doc-1',
        name='House Curve',
        kind='custom',
        source='authored',
        curve=_curve(),
        created_at_utc=NOW,
    )
