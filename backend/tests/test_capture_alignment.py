"""Transform-class contract for CaptureWorldToSceneAuthority (issue #347)."""

from __future__ import annotations

import math
import uuid

import pytest
from pydantic import ValidationError

from htdt.capture_semantic_promotion import (
    CaptureAlignmentError,
    CaptureAlignmentScalePolicy,
    CaptureWorldToSceneAuthority,
    inspect_capture_alignment,
    make_capture_world_to_scene_authority,
)
from htdt.semantic_geometry import SemanticCoordinateTransform


def _matrix(
    a: tuple[tuple[float, float, float], ...] | None = None,
    t: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> SemanticCoordinateTransform:
    a = a or ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    return SemanticCoordinateTransform(
        matrix_source_to_scene_m=(
            (*a[0], t[0]),
            (*a[1], t[1]),
            (*a[2], t[2]),
            (0.0, 0.0, 0.0, 1.0),
        ),
        provenance='explicit_user_authority',
        reason='alignment test fixture',
    )


ROTATE_90_Z = ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
UNIFORM_CM = ((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 0.01))
NON_UNIFORM = ((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
SHEAR = ((1.0, 0.5, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
REFLECT_X = ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
ROT_UNIFORM = (
    (0.0, -0.5, 0.0),
    (0.5, 0.0, 0.0),
    (0.0, 0.0, 0.5),
)


def _space() -> str:
    return str(uuid.uuid4())


class TestClassification:
    def test_identity_is_rigid(self):
        inspection = inspect_capture_alignment(_matrix())
        assert inspection.transform_class == 'rigid'
        assert inspection.determinant == pytest.approx(1.0)
        assert inspection.uniform_scale_m_per_capture_m is None
        assert not inspection.requires_affine_override

    def test_rotation_translation_is_rigid(self):
        inspection = inspect_capture_alignment(
            _matrix(ROTATE_90_Z, t=(1.5, -2.0, 0.25))
        )
        assert inspection.transform_class == 'rigid'

    def test_uniform_scale_is_similarity(self):
        inspection = inspect_capture_alignment(_matrix(UNIFORM_CM))
        assert inspection.transform_class == 'similarity'
        assert inspection.uniform_scale_m_per_capture_m == pytest.approx(0.01)
        assert inspection.requires_explicit_scale_provenance

    def test_rotated_uniform_scale_is_similarity(self):
        inspection = inspect_capture_alignment(_matrix(ROT_UNIFORM))
        assert inspection.transform_class == 'similarity'
        assert inspection.uniform_scale_m_per_capture_m == pytest.approx(0.5)

    def test_non_uniform_scale_is_affine(self):
        inspection = inspect_capture_alignment(_matrix(NON_UNIFORM))
        assert inspection.transform_class == 'affine'
        assert inspection.requires_affine_override
        assert inspection.allowed_methods == ('explicit_affine_override',)

    def test_shear_is_affine(self):
        inspection = inspect_capture_alignment(_matrix(SHEAR))
        assert inspection.transform_class == 'affine'

    def test_reflection_rejected_even_for_inspection(self):
        with pytest.raises(CaptureAlignmentError, match='handedness'):
            inspect_capture_alignment(_matrix(REFLECT_X))


class TestAuthorityContract:
    def test_default_method_binds_rigid(self):
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=_space(), transform=_matrix(ROTATE_90_Z)
        )
        assert authority.transform_class == 'rigid'
        assert authority.alignment_method == 'rigid_registration'
        assert authority.uniform_scale_m_per_capture_m is None

    def test_identity_method_requires_identity(self):
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=_space(),
            transform=_matrix(),
            alignment_method='identity',
        )
        assert authority.transform_class == 'rigid'
        with pytest.raises(CaptureAlignmentError, match='identity'):
            make_capture_world_to_scene_authority(
                coordinate_space_id=_space(),
                transform=_matrix(ROTATE_90_Z),
                alignment_method='identity',
            )

    def test_uniform_scale_rejected_by_default(self):
        with pytest.raises(CaptureAlignmentError, match='similarity'):
            make_capture_world_to_scene_authority(
                coordinate_space_id=_space(), transform=_matrix(UNIFORM_CM)
            )

    def test_uniform_scale_explicit_method_records_provenance(self):
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=_space(),
            transform=_matrix(UNIFORM_CM),
            alignment_method='uniform_scale_alignment',
        )
        assert authority.transform_class == 'similarity'
        assert authority.uniform_scale_m_per_capture_m == pytest.approx(0.01)

    def test_uniform_scale_outside_policy_bounds_rejected(self):
        policy = CaptureAlignmentScalePolicy(
            min_uniform_scale=0.005, max_uniform_scale=0.02
        )
        make_capture_world_to_scene_authority(
            coordinate_space_id=_space(),
            transform=_matrix(UNIFORM_CM),
            alignment_method='uniform_scale_alignment',
            scale_policy=policy,
        )
        with pytest.raises(CaptureAlignmentError, match='outside'):
            make_capture_world_to_scene_authority(
                coordinate_space_id=_space(),
                transform=_matrix(UNIFORM_CM),
                alignment_method='uniform_scale_alignment',
                scale_policy=CaptureAlignmentScalePolicy(
                    min_uniform_scale=0.02, max_uniform_scale=0.5
                ),
            )

    def test_scale_factor_must_exceed_unit_conversion_range(self):
        huge = ((2000.0, 0.0, 0.0), (0.0, 2000.0, 0.0), (0.0, 0.0, 2000.0))
        with pytest.raises(CaptureAlignmentError, match='outside'):
            make_capture_world_to_scene_authority(
                coordinate_space_id=_space(),
                transform=_matrix(huge),
                alignment_method='uniform_scale_alignment',
            )

    def test_shear_rejected_for_normal_methods(self):
        for method in ('identity', 'rigid_registration', 'uniform_scale_alignment'):
            with pytest.raises(CaptureAlignmentError):
                make_capture_world_to_scene_authority(
                    coordinate_space_id=_space(),
                    transform=_matrix(SHEAR),
                    alignment_method=method,  # type: ignore[arg-type]
                )

    def test_non_uniform_scale_rejected_for_normal_methods(self):
        for method in ('identity', 'rigid_registration', 'uniform_scale_alignment'):
            with pytest.raises(CaptureAlignmentError):
                make_capture_world_to_scene_authority(
                    coordinate_space_id=_space(),
                    transform=_matrix(NON_UNIFORM),
                    alignment_method=method,  # type: ignore[arg-type]
                )

    def test_affine_override_admits_shear(self):
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=_space(),
            transform=_matrix(SHEAR),
            alignment_method='explicit_affine_override',
        )
        assert authority.transform_class == 'affine'
        assert authority.alignment_method == 'explicit_affine_override'

    def test_reflection_rejected_for_every_method(self):
        for method in (
            'identity',
            'rigid_registration',
            'uniform_scale_alignment',
            'explicit_affine_override',
        ):
            with pytest.raises(CaptureAlignmentError, match='handedness'):
                make_capture_world_to_scene_authority(
                    coordinate_space_id=_space(),
                    transform=_matrix(REFLECT_X),
                    alignment_method=method,  # type: ignore[arg-type]
                )

    def test_method_is_part_of_authority_identity(self):
        space = _space()
        override = make_capture_world_to_scene_authority(
            coordinate_space_id=space,
            transform=_matrix(SHEAR),
            alignment_method='explicit_affine_override',
        )
        assert 'capture-world-to-scene:' in override.authority_id
        # Same matrix under a different declared method would hash differently,
        # so an authority id can never be replayed across methods.
        rigid = make_capture_world_to_scene_authority(
            coordinate_space_id=space,
            transform=_matrix(ROTATE_90_Z),
            alignment_method='rigid_registration',
        )
        assert override.authority_id != rigid.authority_id

    def test_direct_construction_cannot_lie_about_class(self):
        matrix = _matrix(NON_UNIFORM)
        space = _space()
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=space,
            transform=matrix,
            alignment_method='explicit_affine_override',
        )
        payload = authority.model_dump(mode='json')
        payload['transform_class'] = 'rigid'
        with pytest.raises(ValidationError, match='transform_class mismatch'):
            CaptureWorldToSceneAuthority.model_validate(payload)

    def test_similarity_method_requires_recorded_scale(self):
        space = _space()
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=space,
            transform=_matrix(UNIFORM_CM),
            alignment_method='uniform_scale_alignment',
        )
        payload = authority.model_dump(mode='json')
        payload['uniform_scale_m_per_capture_m'] = None
        with pytest.raises(ValidationError):
            CaptureWorldToSceneAuthority.model_validate(payload)

    def test_rigid_authority_cannot_record_scale(self):
        space = _space()
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=space, transform=_matrix()
        )
        payload = authority.model_dump(mode='json')
        payload['uniform_scale_m_per_capture_m'] = 2.0
        with pytest.raises(ValidationError):
            CaptureWorldToSceneAuthority.model_validate(payload)

    def test_round_trip_serialization(self):
        authority = make_capture_world_to_scene_authority(
            coordinate_space_id=_space(),
            transform=_matrix(ROT_UNIFORM),
            alignment_method='uniform_scale_alignment',
        )
        restored = CaptureWorldToSceneAuthority.model_validate(
            authority.model_dump(mode='json')
        )
        assert restored == authority


class TestScalePolicy:
    def test_bounds_must_be_ordered(self):
        with pytest.raises(ValidationError):
            CaptureAlignmentScalePolicy(min_uniform_scale=2.0, max_uniform_scale=1.0)

    def test_bounds_must_be_positive(self):
        with pytest.raises(ValidationError):
            CaptureAlignmentScalePolicy(min_uniform_scale=0.0)
