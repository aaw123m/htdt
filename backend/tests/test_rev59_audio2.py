"""REV59-AUDIO2 regression tests — #652 panning, #669 subwoofer
localization, #657 group delay, #702 headphone coupling,
#653 structure-borne, #664 remapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_audio_perception_repository import (
    AudioAuthorityIntegrityError,
    CadAudioPerceptionRepository,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_panning_continuity import (
    PanningContinuityEvidence,
    SubwooferLocalizationProfile,
    evaluate_panning_claim,
)
from htdt.cad_perceptual_chain import (
    GroupDelayAudibility,
    HeadphoneCouplingEvidence,
    evaluate_perceptual_claim,
)
from htdt.cad_structureborne_remap import (
    SpatialRemappingEvidence,
    StructurebornePath,
    evaluate_structureborne_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadAudioPerceptionRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadAudioPerceptionRepository(SceneRepository(db))


def _pce(**kw) -> PanningContinuityEvidence:
    payload = dict(
        document_id='doc-1',
        channel_pair=('L', 'C'),
        timbre_delta_db=0.4,
        stimulus_kind='pan_sweep',
        measurement_ref=_ref('meas-1'),
    )
    payload.update(kw)
    return PanningContinuityEvidence.create(payload)


def _slp(**kw) -> SubwooferLocalizationProfile:
    payload = dict(
        document_id='doc-1',
        crossover_hz=80.0,
        separation_deg=45.0,
        sub_count=2,
        distortion_leakage_bounded=True,
        delay_alignment_ref=_ref('del-1'),
        stimulus_kind='program',
    )
    payload.update(kw)
    return SubwooferLocalizationProfile.create(payload)


def _gda(**kw) -> GroupDelayAudibility:
    payload = dict(
        document_id='doc-1',
        peak_delay_ms=12.0,
        frequency_hz=60.0,
        stimulus_kind='music',
        audibility_threshold_ref=_ref('thr-1'),
        measured_delay_ref=_ref('meas-2'),
    )
    payload.update(kw)
    return GroupDelayAudibility.create(payload)


def _hpc(**kw) -> HeadphoneCouplingEvidence:
    payload = dict(
        document_id='doc-1',
        headphone_model='HD800S',
        compensation_kind='individual_hptf',
        fit_state='verified_placement',
        listener_individualized=True,
        compensation_ref=_ref('cmp-1'),
    )
    payload.update(kw)
    return HeadphoneCouplingEvidence.create(payload)


def _sbp(**kw) -> StructurebornePath:
    payload = dict(
        document_id='doc-1',
        source_kind='subwoofer',
        mount_kind='isolated',
        receiving_room='bedroom',
    )
    payload.update(kw)
    return StructurebornePath.create(payload)


def _srm(**kw) -> SpatialRemappingEvidence:
    payload = dict(
        document_id='doc-1',
        remap_mode='3d_remapping',
        actual_positions_ref=_ref('pos-1'),
        reference_layout_ref=_ref('ref-1'),
        verification_ref=_ref('ver-1'),
    )
    payload.update(kw)
    return SpatialRemappingEvidence.create(payload)


def test_continuity_requires_pair_and_measurement():
    with pytest.raises(ValueError, match='pair'):
        _pce(channel_pair=('L', 'L'))
    with pytest.raises(ValueError):
        _pce(stimulus_kind='unknown')
    with pytest.raises(ValueError):
        _pce(measurement_ref=None)


def test_localization_requires_context():
    with pytest.raises(ValueError):
        _slp(crossover_hz=None)
    with pytest.raises(ValueError):
        _slp(stimulus_kind='unknown')


def test_groupdelay_requires_peak_freq_stimulus():
    with pytest.raises(ValueError):
        _gda(peak_delay_ms=None)
    with pytest.raises(ValueError):
        _gda(frequency_hz=None)
    with pytest.raises(ValueError):
        _gda(stimulus_kind='unknown')
    with pytest.raises(ValueError):
        _gda(measured_delay_ref=None)


def test_coupling_requires_state_and_ref():
    with pytest.raises(ValueError):
        _hpc(compensation_kind='unknown')
    with pytest.raises(ValueError):
        _hpc(fit_state='unknown')
    with pytest.raises(ValueError):
        _hpc(headphone_model='')
    with pytest.raises(ValueError):
        _hpc(compensation_kind='diffuse_field', compensation_ref=None)


def test_structureborne_requires_source_and_mount():
    with pytest.raises(ValueError):
        _sbp(source_kind='unknown')
    with pytest.raises(ValueError):
        _sbp(mount_kind='unknown')


def test_remap_requires_positions_and_mode():
    with pytest.raises(ValueError):
        _srm(remap_mode='unknown')
    with pytest.raises(ValueError, match='positions'):
        _srm(remap_mode='3d_remapping', actual_positions_ref=None)


def test_panning_claim_ladder():
    c, l = _pce(), _slp()
    assert evaluate_panning_claim(
        c, l, rule_threshold_hz=80.0
    )[0] == 'fixed_rule_misapplied'
    l_nol = _slp(distortion_leakage_bounded=False)
    assert evaluate_panning_claim(c, l_nol)[0] == (
        'localization_context_unqualified')
    l_nodel = _slp(delay_alignment_ref=None)
    assert evaluate_panning_claim(c, l_nodel)[0] == (
        'localization_context_unqualified')
    assert evaluate_panning_claim(
        None, l, per_channel_calibrated=True)[0] == (
        'per_channel_is_not_interchannel')
    assert evaluate_panning_claim(c, l)[0] == 'qualified_continuity'


def test_perceptual_claim_ladder():
    g, h = _gda(), _hpc()
    assert evaluate_perceptual_claim(
        None, h, correction_reduced_delay=True)[0] == (
        'lower_is_not_better')
    g_nothr = _gda(audibility_threshold_ref=None)
    assert evaluate_perceptual_claim(g_nothr, h)[0] == (
        'peak_is_not_audibility')
    assert evaluate_perceptual_claim(
        g, None, brir_rendered=True)[0] == 'brir_is_not_eardrum'
    h_nom = _hpc(fit_state='nominal_placement')
    assert evaluate_perceptual_claim(g, h_nom)[0] == (
        'coupling_unqualified')
    h_unc = _hpc(compensation_kind='uncompensated',
                 compensation_ref=None)
    assert evaluate_perceptual_claim(g, h_unc)[0] == (
        'coupling_unqualified')
    assert evaluate_perceptual_claim(g, h)[0] == (
        'qualified_perceptual')


def test_structureborne_claim_ladder():
    p, r = _sbp(), _srm()
    assert evaluate_structureborne_claim(
        None, r, airborne_isolation_qualified=True)[0] == (
        'airborne_is_not_structureborne')
    p_rig = _sbp(mount_kind='rigid_coupled')
    assert evaluate_structureborne_claim(p_rig, r)[0] == (
        'airborne_is_not_structureborne')
    r_nov = _srm(verification_ref=None)
    assert evaluate_structureborne_claim(p, r_nov)[0] == (
        'remap_unverified')
    r_none = _srm(remap_mode='none_declared',
                  actual_positions_ref=None)
    assert evaluate_structureborne_claim(p, r_none)[0] == (
        'measured_positions_required')
    assert evaluate_structureborne_claim(p, r)[0] == (
        'qualified_path')


def test_roundtrip_all_six(tmp_path):
    repo = _repo(tmp_path)
    recs = (_pce(), _slp(), _gda(), _hpc(), _sbp(), _srm())
    repo.save_continuity_evidence(recs[0])
    repo.save_localization_profile(recs[1])
    repo.save_groupdelay_verdict(recs[2])
    repo.save_coupling_evidence(recs[3])
    repo.save_structureborne_path(recs[4])
    repo.save_remap_evidence(recs[5])
    assert repo.get_continuity_evidence(
        recs[0].evidence_id) == recs[0]
    assert repo.get_localization_profile(
        recs[1].profile_id) == recs[1]
    assert repo.get_groupdelay_verdict(
        recs[2].verdict_id) == recs[2]
    assert repo.get_coupling_evidence(
        recs[3].coupling_id) == recs[3]
    assert repo.get_structureborne_path(
        recs[4].path_id) == recs[4]
    assert repo.get_remap_evidence(recs[5].evidence_id) == recs[5]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    g = _gda()
    repo.save_groupdelay_verdict(g)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_groupdelay_audibility SET '
            'peak_delay_ms=? WHERE verdict_id=?',
            (999.0, g.verdict_id),
        )
    with pytest.raises(AudioAuthorityIntegrityError):
        repo.get_groupdelay_verdict(g.verdict_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadAudioPerceptionRepository(SceneRepository(db))
    c = _pce()
    repo.save_continuity_evidence(c)
    assert repo.get_continuity_evidence(c.evidence_id) == c
