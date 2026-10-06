"""REV59-AUDIOMET regression tests — #774 receiver reference,
#743 fixture scattering, #773 echo diagnostics, #678 DRR."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_metrology_repository import (
    AcousticMetrologyIntegrityError,
    CadAcousticMetrologyRepository,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_drr_authority import (
    DRRMeasurement,
    DRRMethodProfile,
    evaluate_drr_claim,
)
from htdt.cad_echo_diagnostic import (
    DiscreteReflectionEvent,
    EchoDiagnostic,
    evaluate_echo_diagnostic_claim,
)
from htdt.cad_fixture_scattering import (
    FixtureScatteringEvidence,
    MeasurementFixture,
    evaluate_fixture_claim,
)
from htdt.cad_receiver_reference import (
    MicrophoneCapsulePose,
    ReceiverReferencePoint,
    evaluate_receiver_origin_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadAcousticMetrologyRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadAcousticMetrologyRepository(SceneRepository(db))


def _rref(**kw) -> ReceiverReferencePoint:
    payload = dict(
        document_id='doc-1',
        point_kind='acoustic_centre',
        offset_uncertainty_m=0.005,
        calibration_ref=_ref('cal-1'),
    )
    payload.update(kw)
    return ReceiverReferencePoint.create(payload)


def _mcp(**kw) -> MicrophoneCapsulePose:
    payload = dict(
        document_id='doc-1',
        reference_ref=_ref('r-1'),
        position_m=(1.0, 2.0, 1.2),
    )
    payload.update(kw)
    return MicrophoneCapsulePose.create(payload)


def _mfx(**kw) -> MeasurementFixture:
    payload = dict(
        document_id='doc-1',
        fixture_kind='boom_arm',
        distance_to_capsule_m=0.15,
    )
    payload.update(kw)
    return MeasurementFixture.create(payload)


def _fsx(**kw) -> FixtureScatteringEvidence:
    payload = dict(
        document_id='doc-1',
        fixture_refs=(_ref('f-1'),),
        bound_kind='measured',
        magnitude_bound_db=0.5,
        evidence_ref=_ref('ev-1'),
    )
    payload.update(kw)
    return FixtureScatteringEvidence.create(payload)


def _dre(**kw) -> DiscreteReflectionEvent:
    payload = dict(
        document_id='doc-1',
        delay_ms=85.0,
        relative_level_db=-6.0,
        periodicity='periodic_train',
        repetition_interval_ms=28.0,
        etc_ref=_ref('etc-1'),
    )
    payload.update(kw)
    return DiscreteReflectionEvent.create(payload)


def _edia(**kw) -> EchoDiagnostic:
    payload = dict(
        document_id='doc-1',
        signal_class='speech',
        event_refs=(_ref('e-1'),),
        criterion_value=0.9,
        threshold_ref=_ref('th-1'),
        verdict='disturbing_echo_likely',
    )
    payload.update(kw)
    return EchoDiagnostic.create(payload)


def _drrm(**kw) -> DRRMethodProfile:
    payload = dict(
        document_id='doc-1',
        direct_window_ms=5.0,
        receiver_kind='omni',
    )
    payload.update(kw)
    return DRRMethodProfile.create(payload)


def _drrv(**kw) -> DRRMeasurement:
    payload = dict(
        document_id='doc-1',
        method_ref=_ref('m-1'),
        drr_db=6.5,
        measurement_ref=_ref('meas-1'),
    )
    payload.update(kw)
    return DRRMeasurement.create(payload)


# -- validation --------------------------------------------------------

def test_receiver_requires_point_kind():
    with pytest.raises(ValueError):
        _rref(point_kind='unknown')
    with pytest.raises(ValueError, match='offset'):
        _rref(point_kind='diaphragm_center',
              acoustic_centre_offset_m=None,
              offset_uncertainty_m=None)


def test_capsule_pose_requires_reference():
    with pytest.raises(ValueError):
        _mcp(reference_ref=None)


def test_scattering_measured_needs_evidence():
    with pytest.raises(ValueError, match='evidence'):
        _fsx(evidence_ref=None)
    with pytest.raises(ValueError, match='declared_absent'):
        _fsx(bound_kind='declared_absent', fixture_refs=(_ref('f-1'),))


def test_event_requires_etc_and_interval():
    with pytest.raises(ValueError):
        _dre(etc_ref=None)
    with pytest.raises(ValueError, match='repetition'):
        _dre(repetition_interval_ms=None)


def test_diagnostic_requires_signal_class_and_threshold():
    with pytest.raises(ValueError):
        _edia(signal_class='unknown')
    with pytest.raises(ValueError, match='criterion'):
        _edia(threshold_ref=None)


def test_drr_method_requires_window_and_receiver():
    with pytest.raises(ValueError):
        _drrm(receiver_kind='unknown')
    with pytest.raises(ValueError, match='window'):
        _drrm(direct_window_ms=0.0)


def test_drr_measurement_requires_method_and_evidence():
    with pytest.raises(ValueError):
        _drrv(method_ref=None)
    with pytest.raises(ValueError):
        _drrv(measurement_ref=None)


# -- evaluators --------------------------------------------------------

def test_receiver_claim_ladder():
    r, p = _rref(), _mcp()
    assert evaluate_receiver_origin_claim(None, p)[0] == (
        'reference_point_unqualified'
    )
    r_grid = _rref(point_kind='protection_grid',
                   acoustic_centre_offset_m=(0, 0, 0.003))
    assert evaluate_receiver_origin_claim(
        r_grid, p, precision_claim='phase'
    )[0] in ('uncertainty_budget_required',
             'acoustic_centre_conflation')
    r_nounc = _rref(offset_uncertainty_m=None)
    assert evaluate_receiver_origin_claim(
        r_nounc, p, precision_claim='phase'
    )[0] == 'acoustic_centre_conflation'
    assert evaluate_receiver_origin_claim(r, p)[0] == (
        'reference_qualified'
    )


def test_fixture_claim_ladder():
    f, s = _mfx(), _fsx()
    assert evaluate_fixture_claim(None, None, mic_calibrated=True)[0] == (
        'calibrated_mic_is_not_setup'
    )
    assert evaluate_fixture_claim(None, None)[0] == (
        'fixture_unregistered'
    )
    assert evaluate_fixture_claim((f,), None)[0] == (
        'scattering_unbounded'
    )
    s_est = _fsx(bound_kind='estimated', magnitude_bound_db=None,
                 evidence_ref=None)
    assert evaluate_fixture_claim((f,), s_est)[0] == (
        'scattering_unbounded'
    )
    assert evaluate_fixture_claim((f,), s)[0] == 'fixture_qualified'


def test_echo_claim_ladder():
    e = _edia()
    assert evaluate_echo_diagnostic_claim(
        None, broadband_metrics_clean=True
    )[0] == 'broadband_metrics_are_not_echo_evidence'
    assert evaluate_echo_diagnostic_claim(None)[0] == (
        'diagnostic_inconclusive'
    )
    e_inc = _edia(verdict='inconclusive')
    assert evaluate_echo_diagnostic_claim(e_inc)[0] == (
        'diagnostic_inconclusive'
    )
    assert evaluate_echo_diagnostic_claim(e)[0] == 'echo_flagged'
    e_none = _edia(verdict='no_disturbing_echo')
    assert evaluate_echo_diagnostic_claim(e_none)[0] == (
        'no_echo_detected'
    )


def test_drr_claim_ladder():
    m, v = _drrm(), _drrv()
    assert evaluate_drr_claim(m, None)[0] == 'unbounded_scalar'
    assert evaluate_drr_claim(None, v)[0] == 'method_unqualified'
    m_bin = _drrm(receiver_kind='binaural')
    assert evaluate_drr_claim(m, v, compared_method=m_bin)[0] == (
        'cross_method_comparison'
    )
    assert evaluate_drr_claim(m, v)[0] == 'qualified_drr'


# -- repository --------------------------------------------------------

def test_roundtrip_all_eight(tmp_path):
    repo = _repo(tmp_path)
    recs = (_rref(), _mcp(), _mfx(), _fsx(), _dre(), _edia(),
            _drrm(), _drrv())
    repo.save_receiver_reference(recs[0])
    repo.save_capsule_pose(recs[1])
    repo.save_fixture(recs[2])
    repo.save_scattering_evidence(recs[3])
    repo.save_reflection_event(recs[4])
    repo.save_echo_diagnostic(recs[5])
    repo.save_drr_method(recs[6])
    repo.save_drr_measurement(recs[7])
    assert repo.get_receiver_reference(recs[0].reference_id) == recs[0]
    assert repo.get_capsule_pose(recs[1].pose_id) == recs[1]
    assert repo.get_fixture(recs[2].fixture_id) == recs[2]
    assert repo.get_scattering_evidence(recs[3].evidence_id) == recs[3]
    assert repo.get_reflection_event(recs[4].event_id) == recs[4]
    assert repo.get_echo_diagnostic(recs[5].diagnostic_id) == recs[5]
    assert repo.get_drr_method(recs[6].profile_id) == recs[6]
    assert repo.get_drr_measurement(recs[7].measurement_id) == recs[7]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    e = _edia()
    repo.save_echo_diagnostic(e)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_echo_diagnostics SET verdict=? '
            'WHERE diagnostic_id=?', ('no_disturbing_echo',
                                      e.diagnostic_id)
        )
    with pytest.raises(AcousticMetrologyIntegrityError):
        repo.get_echo_diagnostic(e.diagnostic_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadAcousticMetrologyRepository(SceneRepository(db))
    r = _rref()
    repo.save_receiver_reference(r)
    assert repo.get_receiver_reference(r.reference_id) == r
