"""REV59-ROOMQ regression tests — sound strength G (#761), resonant
treatment (#704), serviceability (#707)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_resonant_treatment import (
    ResonantAbsorberProfile,
    ResonantPerformanceRecord,
    evaluate_resonance_claim,
)
from htdt.cad_room_qualification_repository import (
    CadRoomQualificationRepository,
    RoomQualificationIntegrityError,
)
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_serviceability import (
    ServiceAccessObservation,
    ServiceEnvelopeProfile,
    evaluate_serviceability_claim,
)
from htdt.cad_sound_strength import (
    SoundStrengthObservation,
    SoundStrengthQualification,
    evaluate_g_claim,
)
from htdt.canonical_json import canonical_sha256


DOC = 'doc-roomq'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _gobs(**kw) -> SoundStrengthObservation:
    payload = dict(
        document_id=DOC,
        method='measured_with_reference_source',
        band='500',
        value_db=4.2,
        source_power_ref=_ref('source_power', 'sp-1'),
    )
    payload.update(kw)
    return SoundStrengthObservation.create(**payload)


def _res(**kw) -> ResonantAbsorberProfile:
    payload = dict(
        document_id=DOC,
        absorber_kind='microperforated',
        cavity_depth_m=0.1,
        perforation_ratio=0.01,
        hole_diameter_m=0.0008,
        target_resonance_hz=80.0,
    )
    payload.update(kw)
    return ResonantAbsorberProfile.create(**payload)


def _rpr(**kw) -> ResonantPerformanceRecord:
    r = _res()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('resonant', r.profile_id, r.profile_sha256),
        derivation='measured_impedance_tube',
        peak_absorption_hz=82.0,
    )
    payload.update(kw)
    return ResonantPerformanceRecord.create(**payload)


def _svc(**kw) -> ServiceEnvelopeProfile:
    payload = dict(
        document_id=DOC,
        device_ref=_ref('device', 'proj-1'),
        required_access_items=('filter', 'connectors', 'lamp_module'),
        access_direction='rear',
        service_clearance_m=0.6,
    )
    payload.update(kw)
    return ServiceEnvelopeProfile.create(**payload)


def _svo(**kw) -> ServiceAccessObservation:
    s = _svc()
    payload = dict(
        document_id=DOC,
        envelope_ref=_ref('service', s.profile_id, s.profile_sha256),
        reachable_items=('filter', 'connectors', 'lamp_module'),
        blocked_items=(),
    )
    payload.update(kw)
    return ServiceAccessObservation.create(**payload)


class TestSoundStrength:
    def test_sealed_create(self) -> None:
        g = _gobs()
        assert g.observation_id.startswith('gobs-')

    def test_reference_method_needs_power_ref(self) -> None:
        with pytest.raises(ValidationError):
            _gobs(source_power_ref=None)
        with pytest.raises(ValidationError):
            _gobs(method='derived_from_source_power',
                  source_power_ref=None)

    def test_unknown_method_no_value(self) -> None:
        with pytest.raises(ValidationError):
            _gobs(method='unknown')

    def test_spl_not_g(self) -> None:
        verdict, _ = evaluate_g_claim(None, 'installed_spl')
        assert verdict == 'not_sound_strength'
        verdict, _ = evaluate_g_claim(None, 'normalized_fr')
        assert verdict == 'not_sound_strength'

    def test_relative_only_without_reference(self) -> None:
        g = _gobs(method='computed_from_impulse_response',
                  source_power_ref=None)
        verdict, _ = evaluate_g_claim(g, 'g_value')
        assert verdict == 'g_relative_only'

    def test_g_established(self) -> None:
        verdict, _ = evaluate_g_claim(_gobs(), 'g_value')
        assert verdict == 'g_established'

    def test_qualification_needs_refs(self) -> None:
        with pytest.raises(ValidationError):
            SoundStrengthQualification.create(
                document_id=DOC, observation_refs=(),
                verdict='g_established')


class TestResonantTreatment:
    def test_sealed_create(self) -> None:
        r = _res()
        assert r.profile_id.startswith('res-')

    def test_porous_rejected_here(self) -> None:
        with pytest.raises(ValidationError):
            _res(absorber_kind='porous')

    def test_resonant_needs_physical_params(self) -> None:
        with pytest.raises(ValidationError):
            _res(cavity_depth_m=None, perforation_ratio=None,
                 hole_diameter_m=None)

    def test_declared_only_no_figures(self) -> None:
        with pytest.raises(ValidationError):
            _rpr(derivation='declared_only')

    def test_single_alpha_inadequate(self) -> None:
        verdict, _ = evaluate_resonance_claim(
            _res(), 'single_octave_alpha')
        assert verdict == 'single_coefficient_inadequate'

    def test_porous_model_misapplied(self) -> None:
        verdict, _ = evaluate_resonance_claim(
            _res(), 'porous_equivalent')
        assert verdict == 'porous_model_misapplied'

    def test_model_declared(self) -> None:
        verdict, _ = evaluate_resonance_claim(
            _res(), 'resonant_design')
        assert verdict == 'resonant_model_declared'


class TestServiceability:
    def test_sealed_create(self) -> None:
        s = _svc()
        assert s.profile_id.startswith('svc-')

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _svc(required_access_items=())
        with pytest.raises(ValidationError):
            _svc(required_access_items=('bogus',))

    def test_reach_block_overlap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _svo(reachable_items=('filter', 'connectors'),
                 blocked_items=('filter',))

    def test_fit_not_serviceable(self) -> None:
        verdict, _ = evaluate_serviceability_claim(
            None, None, cad_fit_ok=True)
        assert verdict == 'fit_is_not_serviceable'

    def test_blocked(self) -> None:
        verdict, reason = evaluate_serviceability_claim(
            _svc(),
            _svo(reachable_items=('connectors', 'lamp_module'),
                 blocked_items=('filter',)))
        assert verdict == 'service_blocked'
        assert 'filter' in reason

    def test_unverified_items(self) -> None:
        verdict, _ = evaluate_serviceability_claim(
            _svc(), _svo(reachable_items=('filter',)))
        assert verdict == 'insufficient_evidence'

    def test_verified(self) -> None:
        verdict, _ = evaluate_serviceability_claim(_svc(), _svo())
        assert verdict == 'serviceable_verified'


def _repo(tmp_path: Path) -> CadRoomQualificationRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadRoomQualificationRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    gobs = _gobs()
    g = _gobs()
    gqual = SoundStrengthQualification.create(
        document_id=DOC,
        observation_refs=(
            _ref('g_obs', g.observation_id, g.observation_sha256),),
        verdict='g_established',
    )
    res = _res()
    rpr = _rpr()
    svc = _svc()
    svo = _svo()
    repo.save_g_observation(gobs)
    repo.save_g_qualification(gqual)
    repo.save_resonant_profile(res)
    repo.save_resonant_record(rpr)
    repo.save_service_envelope(svc)
    repo.save_service_observation(svo)
    assert repo.get_g_observation(gobs.observation_id) == gobs
    assert repo.get_g_qualification(gqual.qualification_id) == gqual
    assert repo.get_resonant_profile(res.profile_id) == res
    assert repo.get_resonant_record(rpr.record_id) == rpr
    assert repo.get_service_envelope(svc.profile_id) == svc
    assert repo.get_service_observation(svo.observation_id) == svo


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    g = _gobs()
    repo.save_g_observation(g)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_sound_strength_observations '
            "SET method='declared_only' WHERE observation_id=?",
            (g.observation_id,),
        )
        connection.commit()
    with pytest.raises(RoomQualificationIntegrityError):
        repo.get_g_observation(g.observation_id)


def test_roomq_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
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
        'cad_sound_strength_observations',
        'cad_sound_strength_qualifications',
        'cad_resonant_absorber_profiles',
        'cad_resonant_performance_records',
        'cad_service_envelope_profiles',
        'cad_service_access_observations',
    }
    assert expected <= tables
