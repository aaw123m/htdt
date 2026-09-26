"""#1068 — FLAIR geometry-acoustics bridge tests."""

import pytest

from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_flair_bridge import (
    FLAIR_ADMISSION,
    FLAIR_BRIDGE_CASE,
    FLAIR_MEASUREMENT_COUNT,
    advance_geometry_stage,
    build_flair_geometry_artifact,
    flair_measurement_id,
    flair_split_counts,
    flair_split_role,
    solver_may_consume,
)


def _prov():
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='test',
        source_version='1',
        source_reference='test',
        source_sha256='0' * 64,
    )


def _artifact(stage='observed_point_cloud', **kw):
    return build_flair_geometry_artifact(
        artifact_id='a1',
        stage=stage,
        artifact_sha256='a' * 64,
        provenance=_prov(),
        **kw,
    )


class TestAdmission:
    def test_payload_pinned(self):
        f = FLAIR_ADMISSION.files[0]
        assert f.file_name == 'data_FLAIR.mat'
        assert f.md5 == '41e06a449ff39d271e32b3b82ab29341'
        assert f.size_bytes == 115821339
        assert FLAIR_ADMISSION.license_id == 'cc-by-4.0'


class TestGeometryLadder:
    def test_solver_only_consumes_solver_ready(self):
        observed = _artifact()
        assert solver_may_consume(observed) is False
        candidate = advance_geometry_stage(
            observed, new_stage='reconstruction_candidate'
        )
        assert solver_may_consume(candidate) is False
        ready = advance_geometry_stage(
            candidate, new_stage='solver_ready'
        )
        assert solver_may_consume(ready) is True

    def test_cannot_skip_stages(self):
        with pytest.raises(ValueError):
            advance_geometry_stage(
                _artifact(), new_stage='solver_ready'
            )

    def test_observed_has_no_parent(self):
        with pytest.raises(ValueError):
            _artifact(derived_from_artifact_id='x',
                      derived_from_stage='observed_point_cloud')

    def test_derived_requires_parent_one_rung_down(self):
        with pytest.raises(ValueError):
            _artifact(
                stage='solver_ready',
                derived_from_artifact_id='x',
                derived_from_stage='observed_point_cloud',
            )


class TestSplit:
    def test_roles_are_preregistered_and_scoped(self):
        roles = {flair_split_role(flair_measurement_id(i))
                 for i in range(FLAIR_MEASUREMENT_COUNT)}
        assert roles == {'calibration', 'holdout'}

    def test_split_is_deterministic(self):
        a = flair_split_role('flair/rir-007')
        b = flair_split_role('flair/rir-007')
        assert a == b

    def test_counts_cover_all_measurements(self):
        counts = flair_split_counts()
        assert sum(counts.values()) == FLAIR_MEASUREMENT_COUNT
        assert counts['holdout'] > 0

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            flair_measurement_id(FLAIR_MEASUREMENT_COUNT)
        with pytest.raises(ValueError):
            flair_split_role('other/rir-0')


class TestCase:
    def test_materials_policy_unknown(self):
        assert FLAIR_BRIDGE_CASE.material_policy == (
            'unknown_unless_measured'
        )
        ladder = FLAIR_BRIDGE_CASE.geometry_authority_ladder
        assert ladder[0] == 'observed_point_cloud'
        assert ladder[-1] == 'solver_ready'
