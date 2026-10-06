"""REV59-SIGNAL regression tests — #747 audio codec, #753 video
codec, #749 spectral estimator, #670 clock domains, #765 fact
claims, #667 BOM/estimate."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_codec_fidelity import (
    CodecFidelityEvidence,
    evaluate_codec_claim,
)
from htdt.cad_fact_bom import (
    BomEstimate,
    ExternalFactClaim,
    FactConflictResolution,
    evaluate_fact_claim,
)
from htdt.cad_signal_authority_repository import (
    CadSignalAuthorityRepository,
    SignalAuthorityIntegrityError,
)
from htdt.cad_spectral_clock import (
    ClockDomainObservation,
    SpectralEstimatorProfile,
    evaluate_spectral_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadSignalAuthorityRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadSignalAuthorityRepository(SceneRepository(db))


def _cfx(**kw) -> CodecFidelityEvidence:
    payload = dict(
        document_id='doc-1',
        media_kind='audio',
        codec_family='lossless',
        source_master_ref=_ref('master-1'),
    )
    payload.update(kw)
    return CodecFidelityEvidence.create(payload)


def _sep(**kw) -> SpectralEstimatorProfile:
    payload = dict(
        document_id='doc-1',
        record_duration_s=1.0,
        fft_size=65536,
        window_kind='blackman_harris',
        enbw_bins=1.7,
        coherent_gain_corrected=True,
        leakage_bounded=True,
    )
    payload.update(kw)
    return SpectralEstimatorProfile.create(payload)


def _cdo(**kw) -> ClockDomainObservation:
    payload = dict(
        document_id='doc-1',
        domain_kind='aes3',
        lock_state='locked',
        asrc_present=False,
    )
    payload.update(kw)
    return ClockDomainObservation.create(payload)


def _efc(**kw) -> ExternalFactClaim:
    payload = dict(
        document_id='doc-1',
        subject='waveforming config',
        statement='two front subs supported',
        source_ref=_ref('src-1'),
        published_on='2026-09-09',
    )
    payload.update(kw)
    return ExternalFactClaim.create(payload)


def _fcr(**kw) -> FactConflictResolution:
    payload = dict(
        document_id='doc-1',
        winning_ref=_ref('w-1'),
        losing_refs=(_ref('l-1'),),
        resolution_kind='recency',
        rationale='newer KB supersedes',
    )
    payload.update(kw)
    return FactConflictResolution.create(payload)


def _bom(**kw) -> BomEstimate:
    payload = dict(
        document_id='doc-1',
        bom_version='v1',
        line_items=('amp-x1', 'sub-x2'),
        engineering_ref=_ref('eng-1'),
    )
    payload.update(kw)
    return BomEstimate.create(payload)


def test_codec_requires_kind_family():
    with pytest.raises(ValueError):
        _cfx(media_kind='unknown')
    with pytest.raises(ValueError):
        _cfx(codec_family='unknown')
    with pytest.raises(ValueError):
        _cfx(impairments=('bogus',))


def test_codec_ladder():
    assert evaluate_codec_claim(
        None, playback_verified=True)[0] == 'playback_is_not_fidelity'
    assert evaluate_codec_claim(None)[0] == 'transparency_unverified'
    c_imp = _cfx(codec_family='aac', impairments=('transcoding',))
    assert evaluate_codec_claim(c_imp)[0] == 'impairment_detected'
    c_lossy = _cfx(codec_family='eac3', source_master_ref=None)
    assert evaluate_codec_claim(c_lossy)[0] == (
        'transparency_unverified')
    c_nom = _cfx(source_master_ref=None)
    assert evaluate_codec_claim(c_nom)[0] == (
        'transparency_unverified')
    assert evaluate_codec_claim(_cfx())[0] == 'qualified_transparent'


def test_spectral_requires_aperture_window():
    with pytest.raises(ValueError):
        _sep(record_duration_s=None)
    with pytest.raises(ValueError):
        _sep(fft_size=0)
    with pytest.raises(ValueError):
        _sep(window_kind='unknown')


def test_clock_requires_domain_lock():
    with pytest.raises(ValueError):
        _cdo(domain_kind='unknown')
    with pytest.raises(ValueError):
        _cdo(lock_state='unknown')
    with pytest.raises(ValueError):
        _cdo(lock_state='relock_event',
             transition_evidence_ref=None)


def test_spectral_ladder():
    assert evaluate_spectral_claim(
        None, (), plotted_bins_dense=True)[0] == (
        'density_is_not_resolution')
    assert evaluate_spectral_claim(None, ())[0] == (
        'amplitude_uncorrected')
    p_rect = _sep(window_kind='rectangular', leakage_bounded=None)
    assert evaluate_spectral_claim(p_rect, ())[0] == (
        'amplitude_uncorrected')
    p_nocorr = _sep(coherent_gain_corrected=False)
    assert evaluate_spectral_claim(p_nocorr, ())[0] == (
        'amplitude_uncorrected')
    p = _sep()
    c_unlock = _cdo(lock_state='locked')
    c_bad = _cdo(lock_state='unlocked')
    assert evaluate_spectral_claim(p, (c_bad,))[0] == 'lock_unverified'
    c_asrc = _cdo(asrc_present=True)
    assert evaluate_spectral_claim(p, (c_asrc,))[0] == (
        'asrc_undeclared')
    assert evaluate_spectral_claim(p, (c_unlock,))[0] == (
        'qualified_spectral')


def test_fact_requires_source():
    with pytest.raises(ValueError):
        _efc(statement='')
    with pytest.raises(ValueError):
        _efc(source_ref=None)


def test_resolution_requires_both_sides():
    with pytest.raises(ValueError):
        _fcr(winning_ref=None)
    with pytest.raises(ValueError):
        _fcr(losing_refs=())
    with pytest.raises(ValueError):
        _fcr(resolution_kind='version_scope', rationale=None)
    ok = _fcr(resolution_kind='unresolved_conflict',
              rationale=None)
    assert ok.resolution_kind == 'unresolved_conflict'


def test_bom_requires_lines_and_engineering():
    with pytest.raises(ValueError):
        _bom(line_items=())
    with pytest.raises(ValueError):
        _bom(engineering_ref=None)
    with pytest.raises(ValueError):
        _bom(pricing_present=True, supplier_ref=None)
    ok = _bom(pricing_present=True, supplier_ref=_ref('sup-1'))
    assert ok.pricing_present is True


def test_fact_ladder():
    c1 = _efc(statement='two front subs supported')
    c2 = _efc(statement='four subs required')
    assert evaluate_fact_claim((c1, c2), None, None)[0] == (
        'conflict_preserved')
    r_un = _fcr(resolution_kind='unresolved_conflict',
                rationale=None)
    assert evaluate_fact_claim((c1,), r_un, None)[0] == (
        'conflict_preserved')
    r = _fcr()
    b_bad = _bom(pricing_present=True, supplier_ref=_ref('s'))
    assert evaluate_fact_claim((c1,), r, b_bad)[0] == (
        'qualified_facts')
    assert evaluate_fact_claim(None, None, None)[0] == (
        'mutable_row_is_not_fact')
    assert evaluate_fact_claim((c1,), r, _bom())[0] == (
        'qualified_facts')


def test_roundtrip_all_six(tmp_path):
    repo = _repo(tmp_path)
    recs = (_cfx(), _sep(), _cdo(), _efc(), _fcr(), _bom())
    repo.save_codec_evidence(recs[0])
    repo.save_spectral_profile(recs[1])
    repo.save_clock_observation(recs[2])
    repo.save_fact_claim(recs[3])
    repo.save_fact_resolution(recs[4])
    repo.save_bom_estimate(recs[5])
    assert repo.get_codec_evidence(recs[0].evidence_id) == recs[0]
    assert repo.get_spectral_profile(
        recs[1].profile_id) == recs[1]
    assert repo.get_clock_observation(
        recs[2].observation_id) == recs[2]
    assert repo.get_fact_claim(recs[3].claim_id) == recs[3]
    assert repo.get_fact_resolution(
        recs[4].resolution_id) == recs[4]
    assert repo.get_bom_estimate(recs[5].estimate_id) == recs[5]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    p = _sep()
    repo.save_spectral_profile(p)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_fft_spectral_estimator_profiles SET '
            'enbw_bins=? WHERE profile_id=?',
            (99.0, p.profile_id),
        )
    with pytest.raises(SignalAuthorityIntegrityError):
        repo.get_spectral_profile(p.profile_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadSignalAuthorityRepository(SceneRepository(db))
    c = _cfx()
    repo.save_codec_evidence(c)
    assert repo.get_codec_evidence(c.evidence_id) == c
