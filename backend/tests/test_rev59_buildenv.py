"""REV59-BUILDENV regression tests — product safety certification
(#751), occupied-room IAQ (#740), material VOC emissions (#750)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_building_environment_repository import (
    BuildingEnvironmentIntegrityError,
    CadBuildingEnvironmentRepository,
)
from htdt.cad_iaq_occupancy import (
    OccupiedIaqObservation,
    OccupiedIaqQualification,
    evaluate_iaq_claim,
)
from htdt.cad_product_safety import (
    ProductSafetyProfile,
    evaluate_safety_claim,
)
from htdt.cad_voc_evidence import (
    VocEmissionProfile,
    evaluate_emission_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-buildenv'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _safety(**kw) -> ProductSafetyProfile:
    payload = dict(
        document_id=DOC,
        product_ref=_ref('device', 'dev-1'),
        listing_kind='nrtl_listed',
        standard='UL62368-1',
        certificate_ref=_ref('certificate', 'cert-1'),
        market='US',
    )
    payload.update(kw)
    return ProductSafetyProfile.create(**payload)


def _iaq(**kw) -> OccupiedIaqObservation:
    payload = dict(
        document_id=DOC,
        room_id='room-1',
        occupied=True,
        co2_ppm=950.0,
        temperature_c=22.5,
    )
    payload.update(kw)
    return OccupiedIaqObservation.create(**payload)


def _iaqq(**kw) -> OccupiedIaqQualification:
    o = _iaq()
    payload = dict(
        document_id=DOC,
        room_id='room-1',
        observation_refs=(
            _ref('iaq_obs', o.observation_id, o.observation_sha256),),
        verdict='occupied_conditions_qualified',
        limits_profile='ASHRAE62.1',
    )
    payload.update(kw)
    return OccupiedIaqQualification.create(**payload)


def _voc(**kw) -> VocEmissionProfile:
    payload = dict(
        document_id=DOC,
        material_ref=_ref('material', 'mat-1'),
        emission_scheme='CDPH_SM_v1.2',
        certificate_ref=_ref('certificate', 'voc-1'),
    )
    payload.update(kw)
    return VocEmissionProfile.create(**payload)


class TestProductSafety:
    def test_sealed_create(self) -> None:
        p = _safety()
        assert p.profile_id.startswith('psf-')

    def test_listing_needs_standard_and_cert(self) -> None:
        with pytest.raises(ValidationError):
            _safety(standard=None)
        with pytest.raises(ValidationError):
            _safety(certificate_ref=None)
        with pytest.raises(ValidationError):
            _safety(certificate_ref=AuthorityRef(
                kind='certificate', ref_id='x', ref_sha256=None))

    def test_unlisted_rejects_cert(self) -> None:
        with pytest.raises(ValidationError):
            _safety(listing_kind='unlisted')

    def test_compatibility_not_certification(self) -> None:
        verdict, _ = evaluate_safety_claim(
            None, 'US', engineering_compatible=True)
        assert verdict == 'compatibility_is_not_certification'

    def test_market_mismatch(self) -> None:
        verdict, _ = evaluate_safety_claim(_safety(), 'JP')
        assert verdict == 'certified_other_market'

    def test_declared_only(self) -> None:
        verdict, _ = evaluate_safety_claim(
            _safety(listing_kind='declared_conformity'), 'US')
        assert verdict == 'declared_only'

    def test_certified_for_market(self) -> None:
        verdict, _ = evaluate_safety_claim(_safety(), 'US')
        assert verdict == 'certified_for_market'


class TestOccupiedIaq:
    def test_sealed_create(self) -> None:
        o = _iaq()
        assert o.observation_id.startswith('iaq-')

    def test_no_figure_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _iaq(co2_ppm=None, temperature_c=None)

    def test_airflow_not_iaq(self) -> None:
        verdict, _ = evaluate_iaq_claim(None, airflow_target_met=True)
        assert verdict == 'airflow_is_not_occupied_iaq'

    def test_unoccupied_observation_insufficient(self) -> None:
        verdict, _ = evaluate_iaq_claim(_iaq(occupied=False))
        assert verdict == 'insufficient_evidence'

    def test_qualification_needs_refs_and_limits(self) -> None:
        with pytest.raises(ValidationError):
            _iaqq(observation_refs=())
        with pytest.raises(ValidationError):
            _iaqq(limits_profile=None)


class TestVocEvidence:
    def test_sealed_create(self) -> None:
        v = _voc()
        assert v.profile_id.startswith('voc-')

    def test_scheme_needs_evidence(self) -> None:
        with pytest.raises(ValidationError):
            _voc(certificate_ref=None)

    def test_no_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _voc(emission_scheme=None, certificate_ref=None)

    def test_room_iaq_not_proven_by_cert(self) -> None:
        verdict, _ = evaluate_emission_claim(_voc(), 'room_iaq')
        assert verdict == 'room_iaq_not_proven_by_cert'

    def test_product_documented(self) -> None:
        verdict, _ = evaluate_emission_claim(_voc(), 'product_emission')
        assert verdict == 'product_emission_documented'

    def test_no_profile_undocumented(self) -> None:
        verdict, _ = evaluate_emission_claim(None, 'product_emission')
        assert verdict == 'product_emission_undocumented'


def _repo(tmp_path: Path) -> CadBuildingEnvironmentRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadBuildingEnvironmentRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    psf = _safety()
    iaq = _iaq()
    iaqq = _iaqq()
    voc = _voc()
    repo.save_safety_profile(psf)
    repo.save_iaq_observation(iaq)
    repo.save_iaq_qualification(iaqq)
    repo.save_voc_profile(voc)
    assert repo.get_safety_profile(psf.profile_id) == psf
    assert repo.get_iaq_observation(iaq.observation_id) == iaq
    assert repo.get_iaq_qualification(iaqq.qualification_id) == iaqq
    assert repo.get_voc_profile(voc.profile_id) == voc


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    o = _iaq()
    repo.save_iaq_observation(o)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_occupied_iaq_observations '
            'SET occupied=0 WHERE observation_id=?',
            (o.observation_id,),
        )
        connection.commit()
    with pytest.raises(BuildingEnvironmentIntegrityError):
        repo.get_iaq_observation(o.observation_id)


def test_buildenv_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
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
        'cad_product_safety_profiles',
        'cad_occupied_iaq_observations',
        'cad_occupied_iaq_qualifications',
        'cad_voc_emission_profiles',
    }
    assert expected <= tables
