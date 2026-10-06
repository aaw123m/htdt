"""REV59-LISTENEXP regression tests — #696 listening experiments,
#726 assistive listening, #727 dynamic binaural."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_assistive_listening import (
    ALSQualification,
    AssistiveListeningPath,
    ReceiverCompatibilityEvidence,
    evaluate_als_claim,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_binaural_dynamic import (
    BinauralQualification,
    DynamicBinauralSession,
    PoseTrackingEvidence,
    evaluate_binaural_claim,
)
from htdt.cad_listening_evidence_repository import (
    CadListeningEvidenceRepository,
    ListeningEvidenceIntegrityError,
)
from htdt.cad_listening_experiment import (
    ListeningExperimentPlan,
    ListenerQualification,
    SubjectiveInferenceRecord,
    evaluate_experiment_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadListeningEvidenceRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadListeningEvidenceRepository(SceneRepository(db))


def _plan(**kw) -> ListeningExperimentPlan:
    payload = dict(
        document_id='doc-1',
        method_kind='bs1116_3',
        impairment_regime='small_impairment',
        listener_count=12,
        trials_per_listener=16,
        randomization_plan_ref=_ref('rand-1'),
    )
    payload.update(kw)
    return ListeningExperimentPlan.create(payload)


def _lqual(**kw) -> ListenerQualification:
    payload = dict(
        document_id='doc-1',
        screening_ref=_ref('scr-1'),
        training_completed=True,
        test_retest_ref=_ref('tr-1'),
        panel_size=12,
    )
    payload.update(kw)
    return ListenerQualification.create(payload)


def _sinf(**kw) -> SubjectiveInferenceRecord:
    payload = dict(
        document_id='doc-1',
        plan_ref=_ref('p-1'),
        effect_estimate=0.4,
        confidence_interval=(0.1, 0.7),
        verdict='significant',
    )
    payload.update(kw)
    return SubjectiveInferenceRecord.create(payload)


def _als(**kw) -> AssistiveListeningPath:
    payload = dict(document_id='doc-1', technology='induction_loop')
    payload.update(kw)
    return AssistiveListeningPath.create(payload)


def _alsq(**kw) -> ALSQualification:
    payload = dict(
        document_id='doc-1',
        path_ref=_ref('als-1'),
        field_strength_ref=_ref('fs-1'),
        snr_ref=_ref('snr-1'),
    )
    payload.update(kw)
    return ALSQualification.create(payload)


def _rcomp(**kw) -> ReceiverCompatibilityEvidence:
    payload = dict(
        document_id='doc-1',
        path_ref=_ref('als-1'),
        receiver_kind='telecoil',
        compatible=True,
    )
    payload.update(kw)
    return ReceiverCompatibilityEvidence.create(payload)


def _dbin(**kw) -> DynamicBinauralSession:
    payload = dict(
        document_id='doc-1',
        hrtf_class='individual',
        hrtf_ref=_ref('hrtf-1'),
        update_rate_hz=60.0,
    )
    payload.update(kw)
    return DynamicBinauralSession.create(payload)


def _ptrk(**kw) -> PoseTrackingEvidence:
    payload = dict(
        document_id='doc-1',
        session_ref=_ref('s-1'),
        motion_to_audio_latency_ms=18.0,
        latency_measurement_ref=_ref('lat-1'),
    )
    payload.update(kw)
    return PoseTrackingEvidence.create(payload)


def _bqual(**kw) -> BinauralQualification:
    payload = dict(
        document_id='doc-1',
        session_ref=_ref('s-1'),
        angular_sampling_ref=_ref('ang-1'),
        verdict='qualified',
    )
    payload.update(kw)
    return BinauralQualification.create(payload)


# -- validation --------------------------------------------------------

def test_plan_requires_method_and_regime_sanity():
    with pytest.raises(ValueError):
        _plan(method_kind='unknown')
    with pytest.raises(ValueError, match='intermediate'):
        _plan(
            method_kind='mushra_bs1534_3',
            impairment_regime='small_impairment',
        )


def test_lqual_qualified_flag():
    assert _lqual().qualified
    assert not _lqual(screening_ref=None).qualified
    assert not _lqual(training_completed=False).qualified
    assert not _lqual(test_retest_ref=None).qualified


def test_sinf_requires_plan_and_interval():
    with pytest.raises(ValueError):
        _sinf(plan_ref=None)
    with pytest.raises(ValueError, match='confidence'):
        _sinf(confidence_interval=None)


def test_als_requires_technology():
    with pytest.raises(ValueError):
        _als(technology='unknown')


def test_alsq_requires_path():
    with pytest.raises(ValueError):
        _alsq(path_ref=None)


def test_rcomp_requires_path_and_kind():
    with pytest.raises(ValueError):
        _rcomp(path_ref=None)
    with pytest.raises(ValueError, match='receiver kind'):
        _rcomp(receiver_kind='')


def test_dbin_requires_hrtf_class():
    with pytest.raises(ValueError):
        _dbin(hrtf_class='unknown')
    with pytest.raises(ValueError):
        _dbin(update_rate_hz=0.0)


def test_ptrk_declared_latency_needs_measurement():
    with pytest.raises(ValueError, match='measurement'):
        _ptrk(latency_measurement_ref=None)
    ok = _ptrk(motion_to_audio_latency_ms=None,
               latency_measurement_ref=None)
    assert ok.motion_to_audio_latency_ms is None


def test_bqual_requires_session():
    with pytest.raises(ValueError):
        _bqual(session_ref=None)


# -- evaluators --------------------------------------------------------

def test_experiment_claim_ladder():
    p, q, i = _plan(), _lqual(), _sinf()
    assert evaluate_experiment_claim(None, q, i)[0] == (
        'unverified_result'
    )
    p_nr = _plan(randomization_plan_ref=None)
    assert evaluate_experiment_claim(p_nr, q, i)[0] == (
        'unrandomized_trials'
    )
    assert evaluate_experiment_claim(p, None, i)[0] == (
        'unqualified_listeners'
    )
    p_small = _plan(listener_count=2, trials_per_listener=4)
    assert evaluate_experiment_claim(p_small, q, i)[0] == (
        'insufficient_trials'
    )
    assert evaluate_experiment_claim(p, q, None)[0] == (
        'unverified_result'
    )
    assert evaluate_experiment_claim(p, q, i)[0] == (
        'qualified_experiment'
    )


def test_als_claim_ladder():
    a, q, r = _als(), _alsq(), _rcomp()
    assert evaluate_als_claim(None, q, r, speaker_qualified=True)[0] == (
        'speaker_perf_is_not_als'
    )
    assert evaluate_als_claim(a, q, r)[0] == 'routing_unverified'
    assert evaluate_als_claim(
        a, None, r, routing_verified=True
    )[0] == 'path_declared_unmeasured'
    r_bad = _rcomp(compatible=False)
    assert evaluate_als_claim(
        a, q, r_bad, routing_verified=True
    )[0] == 'receiver_incompatible'
    assert evaluate_als_claim(
        a, q, r, routing_verified=True
    )[0] == 'qualified_path'


def test_binaural_claim_ladder():
    s, p, q = _dbin(), _ptrk(), _bqual()
    assert evaluate_binaural_claim(None, p, q)[0] == (
        'pose_frame_unqualified'
    )
    assert evaluate_binaural_claim(
        s, None, q, static_render_ref=_ref('st-1')
    )[0] == 'static_is_not_dynamic'
    p_no_lat = _ptrk(motion_to_audio_latency_ms=None,
                     latency_measurement_ref=None)
    assert evaluate_binaural_claim(s, p_no_lat, q)[0] == (
        'tracker_latency_unmeasured'
    )
    assert evaluate_binaural_claim(s, p, None)[0] == (
        'angular_sampling_unqualified'
    )
    q_no_ang = _bqual(angular_sampling_ref=None)
    assert evaluate_binaural_claim(s, p, q_no_ang)[0] == (
        'angular_sampling_unqualified'
    )
    assert evaluate_binaural_claim(s, p, q)[0] == 'qualified_dynamic'


# -- repository --------------------------------------------------------

def test_roundtrip_all_nine(tmp_path):
    repo = _repo(tmp_path)
    recs = (_plan(), _lqual(), _sinf(), _als(), _alsq(), _rcomp(),
            _dbin(), _ptrk(), _bqual())
    repo.save_listening_plan(recs[0])
    repo.save_listener_qualification(recs[1])
    repo.save_inference_record(recs[2])
    repo.save_als_path(recs[3])
    repo.save_als_qualification(recs[4])
    repo.save_receiver_evidence(recs[5])
    repo.save_binaural_session(recs[6])
    repo.save_pose_evidence(recs[7])
    repo.save_binaural_qualification(recs[8])
    assert repo.get_listening_plan(recs[0].plan_id) == recs[0]
    assert repo.get_listener_qualification(
        recs[1].qualification_id) == recs[1]
    assert repo.get_inference_record(recs[2].record_id) == recs[2]
    assert repo.get_als_path(recs[3].path_id) == recs[3]
    assert repo.get_als_qualification(
        recs[4].qualification_id) == recs[4]
    assert repo.get_receiver_evidence(recs[5].evidence_id) == recs[5]
    assert repo.get_binaural_session(recs[6].session_id) == recs[6]
    assert repo.get_pose_evidence(recs[7].evidence_id) == recs[7]
    assert repo.get_binaural_qualification(
        recs[8].qualification_id) == recs[8]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    a = _als()
    repo.save_als_path(a)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_assistive_listening_paths SET technology=? '
            'WHERE path_id=?', ('fm_receiver', a.path_id)
        )
    with pytest.raises(ListeningEvidenceIntegrityError):
        repo.get_als_path(a.path_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadListeningEvidenceRepository(SceneRepository(db))
    a = _als()
    repo.save_als_path(a)
    assert repo.get_als_path(a.path_id) == a
