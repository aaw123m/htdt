"""REV59-ACOUST2 regression tests — fixture/observer scattering
(#743), spectral estimator (#749), evidence supersession (#765)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_evidence_supersession import (
    EvidenceSupersessionRecord,
    ExternalEvidenceSource,
    evaluate_conflict_claim,
)
from htdt.cad_measurement_setup_repository import (
    CadMeasurementSetupRepository,
    MeasurementSetupIntegrityError,
)
from htdt.cad_observer_scattering import (
    FixtureScatteringObservation,
    MeasurementFixtureProfile,
    evaluate_transparency_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_spectral_estimator import (
    SpectralEstimatorProfile,
    SpectralResolutionClaim,
    evaluate_resolution_claim,
)
from htdt.canonical_json import canonical_sha256


DOC = 'doc-acoust2'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _fixture(**kw) -> MeasurementFixtureProfile:
    payload = dict(
        document_id=DOC,
        mic_ref=_ref('microphone', 'mic-1'),
        mount_description='boom stand',
        observer_distance_m=2.0,
    )
    payload.update(kw)
    return MeasurementFixtureProfile.create(**payload)


def _fxo(**kw) -> FixtureScatteringObservation:
    f = _fixture()
    payload = dict(
        document_id=DOC,
        fixture_ref=_ref('fixture', f.profile_id, f.profile_sha256),
        contamination_detected=False,
    )
    payload.update(kw)
    return FixtureScatteringObservation.create(**payload)


def _estimator(**kw) -> SpectralEstimatorProfile:
    payload = dict(
        document_id=DOC,
        estimator_kind='fft',
        window_kind='hann',
        record_length_s=0.5,
        fft_size=32768,
        sample_rate_hz=48000.0,
    )
    payload.update(kw)
    return SpectralEstimatorProfile.create(**payload)


def _src(**kw) -> ExternalEvidenceSource:
    payload = dict(
        document_id=DOC,
        source_tier='primary_vendor_doc',
        publisher='VendorX',
        published_at='2026-01-01',
        scope='four-sub-minimum',
    )
    payload.update(kw)
    return ExternalEvidenceSource.create(**payload)


class TestFixtureScattering:
    def test_sealed_create(self) -> None:
        f = _fixture()
        assert f.profile_id.startswith('fxp-')

    def test_mic_ref_required(self) -> None:
        with pytest.raises(ValidationError):
            _fixture(mic_ref=AuthorityRef(
                kind='microphone', ref_id='x', ref_sha256=None))
        with pytest.raises(ValidationError):
            _fixture(mount_description='')

    def test_observation_requires_fixture_ref(self) -> None:
        with pytest.raises(ValidationError):
            _fxo(fixture_ref=AuthorityRef(
                kind='fixture', ref_id='x', ref_sha256=None))

    def test_calibration_not_transparency(self) -> None:
        verdict, _ = evaluate_transparency_claim(
            None, None, mic_calibrated=True)
        assert verdict == 'calibration_is_not_transparency'

    def test_contamination_detected(self) -> None:
        verdict, _ = evaluate_transparency_claim(
            _fixture(), _fxo(contamination_detected=True))
        assert verdict == 'fixture_contaminating'

    def test_transparent(self) -> None:
        verdict, _ = evaluate_transparency_claim(_fixture(), _fxo())
        assert verdict == 'fixture_transparent'

    def test_no_observation_insufficient(self) -> None:
        verdict, _ = evaluate_transparency_claim(_fixture(), None)
        assert verdict == 'insufficient_evidence'


class TestSpectralEstimator:
    def test_sealed_create(self) -> None:
        e = _estimator()
        assert e.profile_id.startswith('sep-')

    def test_padding_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _estimator(zero_padding_factor=0.5)

    def test_true_vs_displayed_resolution(self) -> None:
        e = _estimator(zero_padding_factor=4.0)
        assert e.displayed_bin_spacing_hz() < e.true_resolution_hz()
        assert e.true_resolution_hz() == pytest.approx(2.0)

    def test_unknown_window_unresolvable(self) -> None:
        verdict, _ = evaluate_resolution_claim(
            _estimator(window_kind='unknown'), 2.0)
        assert verdict == 'unknown_window_unresolvable'

    def test_padding_not_resolution(self) -> None:
        e = _estimator(zero_padding_factor=8.0)
        verdict, _ = evaluate_resolution_claim(e, 0.25)
        assert verdict == 'padded_display_not_resolution'

    def test_claim_within_limit(self) -> None:
        verdict, _ = evaluate_resolution_claim(_estimator(), 2.0)
        assert verdict == 'resolution_declared'

    def test_claim_ref_and_verdict(self) -> None:
        with pytest.raises(ValidationError):
            SpectralResolutionClaim.create(
                document_id=DOC,
                estimator_ref=AuthorityRef(
                    kind='estimator', ref_id='x', ref_sha256=None),
                claimed_resolution_hz=2.0,
                verdict='resolution_declared',
            )


class TestEvidenceSupersession:
    def test_sealed_create(self) -> None:
        s = _src()
        assert s.source_id.startswith('ees-')

    def test_needs_two_sources(self) -> None:
        s1, s2 = _src(), _src()
        with pytest.raises(ValidationError):
            EvidenceSupersessionRecord.create(
                document_id=DOC,
                source_refs=(
                    _ref('source', s1.source_id, s1.source_sha256),),
                resolution='unresolved_conflict',
                rationale='x',
            )
        rec = EvidenceSupersessionRecord.create(
            document_id=DOC,
            source_refs=(
                _ref('source', s1.source_id, s1.source_sha256),
                _ref('source', s2.source_id, s2.source_sha256),
            ),
            resolution='superseded',
            winning_source_ref=_ref(
                'source', s2.source_id, s2.source_sha256),
            rationale='newer same-scope doc',
        )
        assert rec.record_id.startswith('ess-')

    def test_superseded_needs_winner(self) -> None:
        s1, s2 = _src(), _src()
        with pytest.raises(ValidationError):
            EvidenceSupersessionRecord.create(
                document_id=DOC,
                source_refs=(
                    _ref('source', s1.source_id, s1.source_sha256),
                    _ref('source', s2.source_id, s2.source_sha256),
                ),
                resolution='superseded',
                rationale='x',
            )

    def test_different_tiers_scoped(self) -> None:
        a = _src(product_family='fam', product_tier='tier-a')
        b = _src(product_family='fam', product_tier='tier-b')
        verdict, _ = evaluate_conflict_claim((a, b))
        assert verdict == 'scoped_both_valid'

    def test_same_product_different_scope(self) -> None:
        a = _src(product_family='fam', product_tier='t',
                 scope='scope-a')
        b = _src(product_family='fam', product_tier='t',
                 scope='scope-b')
        verdict, _ = evaluate_conflict_claim((a, b))
        assert verdict == 'needs_vendor_clarification'

    def test_same_scope_unresolved(self) -> None:
        a = _src(product_family='fam', product_tier='t',
                 published_at='2026-01-01')
        b = _src(product_family='fam', product_tier='t',
                 published_at='2026-09-09')
        verdict, _ = evaluate_conflict_claim((a, b))
        assert verdict == 'unresolved_conflict'


def _repo(tmp_path: Path) -> CadMeasurementSetupRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadMeasurementSetupRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fxp = _fixture()
    fxo = _fxo()
    sep = _estimator()
    src2 = SpectralResolutionClaim.create(
        document_id=DOC,
        estimator_ref=_ref('estimator', sep.profile_id,
                           sep.profile_sha256),
        claimed_resolution_hz=2.0,
        verdict='resolution_declared',
    )
    s1, s2 = _src(), _src()
    ess = EvidenceSupersessionRecord.create(
        document_id=DOC,
        source_refs=(
            _ref('source', s1.source_id, s1.source_sha256),
            _ref('source', s2.source_id, s2.source_sha256),
        ),
        resolution='superseded',
        winning_source_ref=_ref(
            'source', s2.source_id, s2.source_sha256),
        rationale='newer same-scope doc',
    )
    repo.save_fixture_profile(fxp)
    repo.save_scattering_observation(fxo)
    repo.save_estimator_profile(sep)
    repo.save_resolution_claim(src2)
    repo.save_evidence_source(s1)
    repo.save_supersession_record(ess)
    assert repo.get_fixture_profile(fxp.profile_id) == fxp
    assert repo.get_scattering_observation(
        fxo.observation_id) == fxo
    assert repo.get_estimator_profile(sep.profile_id) == sep
    assert repo.get_resolution_claim(src2.claim_id) == src2
    assert repo.get_evidence_source(s1.source_id) == s1
    assert repo.get_supersession_record(ess.record_id) == ess


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    o = _fxo()
    repo.save_scattering_observation(o)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_fixture_scattering_observations '
            'SET contamination_detected=1 WHERE observation_id=?',
            (o.observation_id,),
        )
        connection.commit()
    with pytest.raises(MeasurementSetupIntegrityError):
        repo.get_scattering_observation(o.observation_id)


def test_acoust2_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_measurement_fixture_profiles',
        'cad_fixture_scattering_observations',
        'cad_spectral_estimator_profiles',
        'cad_spectral_resolution_claims',
        'cad_external_evidence_sources',
        'cad_evidence_supersession_records',
    }
    assert expected <= tables
