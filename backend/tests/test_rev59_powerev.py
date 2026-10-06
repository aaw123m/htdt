"""REV59-POWEREV regression tests — power sequencing (#736),
AC power quality (#738), EMC product evidence (#752)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_emc_evidence import (
    EmcProductProfile,
    EmcSymptomRecord,
    evaluate_emc_claim,
)
from htdt.cad_power_evidence_repository import (
    CadPowerEvidenceRepository,
    PowerEvidenceIntegrityError,
)
from htdt.cad_power_quality import (
    PowerQualityMeasurement,
    PowerQualityQualification,
    evaluate_supply_claim,
)
from htdt.cad_power_sequencing import (
    PowerSequenceEvent,
    PowerSequencingProfile,
    UpsTransitionRecord,
    evaluate_sequence_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-powerev'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _profile(**kw) -> PowerSequencingProfile:
    payload = dict(
        document_id=DOC,
        direction='power_on',
        stages=('network', 'source', 'processor', 'amplifier'),
        amplifier_device_ids=('amp-1',),
        always_on_device_ids=('net-1', 'ctl-1'),
        inrush_staging_declared=True,
    )
    payload.update(kw)
    return PowerSequencingProfile.create(**payload)


def _event(**kw) -> PowerSequenceEvent:
    p = _profile()
    payload = dict(
        document_id=DOC,
        profile_ref=_ref('sequencing', p.profile_id, p.profile_sha256),
        observed_stage_order=('network', 'source', 'processor',
                              'amplifier'),
        amplifier_on_after_processing=True,
        control_survived_outage=True,
    )
    payload.update(kw)
    return PowerSequenceEvent.create(**payload)


def _ups(**kw) -> UpsTransitionRecord:
    payload = dict(
        document_id=DOC,
        ups_device_id='ups-1',
        transfer_observed=True,
        recovery_sequence_ref=_ref('sequencing', 'ps-1'),
        protected_devices_survived=('net-1',),
    )
    payload.update(kw)
    return UpsTransitionRecord.create(**payload)


def _pqm(**kw) -> PowerQualityMeasurement:
    payload = dict(
        document_id=DOC,
        circuit_id='ckt-1',
        rms_voltage_v=119.8,
        frequency_hz=60.0,
        thd_percent=2.1,
    )
    payload.update(kw)
    return PowerQualityMeasurement.create(**payload)


def _pqq(**kw) -> PowerQualityQualification:
    m = _pqm()
    payload = dict(
        document_id=DOC,
        circuit_id='ckt-1',
        measurement_ref=_ref(
            'pq_measure', m.measurement_id, m.measurement_sha256),
        verdict='supply_qualified',
        limits_profile='EN50160',
    )
    payload.update(kw)
    return PowerQualityQualification.create(**payload)


def _emc(**kw) -> EmcProductProfile:
    payload = dict(
        document_id=DOC,
        product_ref=_ref('device', 'dev-1'),
        emissions_standard='CISPR32',
        emissions_test_ref=_ref('test', 'em-1'),
        immunity_standard='CISPR35',
        immunity_test_ref=_ref('test', 'im-1'),
    )
    payload.update(kw)
    return EmcProductProfile.create(**payload)


def _symptom(**kw) -> EmcSymptomRecord:
    payload = dict(
        document_id=DOC,
        product_ref=_ref('device', 'dev-1'),
        symptom='hum on input',
    )
    payload.update(kw)
    return EmcSymptomRecord.create(**payload)


class TestPowerSequencing:
    def test_sealed_create(self) -> None:
        p = _profile()
        assert p.profile_id.startswith('psq-')

    def test_empty_stages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _profile(stages=())

    def test_power_off_needs_amps(self) -> None:
        with pytest.raises(ValidationError):
            _profile(direction='power_off', amplifier_device_ids=())

    def test_event_requires_profile_ref(self) -> None:
        with pytest.raises(ValidationError):
            _event(profile_ref=AuthorityRef(
                kind='sequencing', ref_id='x', ref_sha256=None))

    def test_no_evidence_unverified(self) -> None:
        verdict, _ = evaluate_sequence_claim(None, None)
        assert verdict == 'unverified_sequencing'
        verdict, _ = evaluate_sequence_claim(_profile(), None)
        assert verdict == 'unverified_sequencing'

    def test_order_mismatch(self) -> None:
        ev = _event(observed_stage_order=('amplifier', 'processor'))
        verdict, reason = evaluate_sequence_claim(_profile(), ev)
        assert verdict == 'unverified_sequencing'
        assert reason == 'observed_order_mismatch'

    def test_amp_violation_on(self) -> None:
        ev = _event(amplifier_on_after_processing=False)
        verdict, _ = evaluate_sequence_claim(_profile(), ev)
        assert verdict == 'amplifier_order_violation'

    def test_always_on_violation(self) -> None:
        ev = _event(control_survived_outage=False)
        verdict, _ = evaluate_sequence_claim(_profile(), ev)
        assert verdict == 'always_on_violation'

    def test_verified(self) -> None:
        verdict, _ = evaluate_sequence_claim(_profile(), _event())
        assert verdict == 'sequence_verified'

    def test_ups_transfer_needs_recovery_ref(self) -> None:
        with pytest.raises(ValidationError):
            _ups(recovery_sequence_ref=None)


class TestPowerQuality:
    def test_sealed_create(self) -> None:
        m = _pqm()
        assert m.measurement_id.startswith('pqm-')

    def test_no_figure_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _pqm(rms_voltage_v=None, frequency_hz=None)

    def test_unknown_event_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _pqm(events=({'kind': 'flicker'},))

    def test_capacity_not_stability(self) -> None:
        verdict, reason = evaluate_supply_claim(None)
        assert verdict == 'unmeasured_supply'
        assert reason == 'capacity_is_not_stability'

    def test_interruption_degrades(self) -> None:
        m = _pqm(events=({'kind': 'short_interruption'},))
        verdict, _ = evaluate_supply_claim(m)
        assert verdict == 'supply_degraded'

    def test_qualified_needs_limits_profile(self) -> None:
        with pytest.raises(ValidationError):
            _pqq(limits_profile=None)
        with pytest.raises(ValidationError):
            _pqq(verdict='bogus')


class TestEmcEvidence:
    def test_sealed_create(self) -> None:
        p = _emc()
        assert p.profile_id.startswith('emc-')

    def test_standard_needs_test_ref(self) -> None:
        with pytest.raises(ValidationError):
            _emc(emissions_test_ref=None)
        with pytest.raises(ValidationError):
            _emc(immunity_test_ref=None)

    def test_no_test_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _emc(emissions_test_ref=None, immunity_test_ref=None,
                 emissions_standard=None, immunity_standard=None)

    def test_safety_not_emc(self) -> None:
        verdict, _ = evaluate_emc_claim(None, has_safety_approval=True)
        assert verdict == 'safety_is_not_emc'

    def test_symptom_not_nonconformance(self) -> None:
        verdict, _ = evaluate_emc_claim(None, symptom=_symptom())
        assert verdict == 'symptom_not_nonconformance'

    def test_partial_evidence(self) -> None:
        p = _emc(immunity_standard=None, immunity_test_ref=None)
        verdict, reason = evaluate_emc_claim(p)
        assert verdict == 'emc_partial_evidence'
        assert reason == 'missing_immunity'

    def test_complete(self) -> None:
        verdict, _ = evaluate_emc_claim(_emc())
        assert verdict == 'emc_evidence_complete'


def _repo(tmp_path: Path) -> CadPowerEvidenceRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadPowerEvidenceRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    psq = _profile()
    psev = _event()
    upst = _ups()
    pqm = _pqm()
    pqq = _pqq()
    emc = _emc()
    emcs = _symptom()

    repo.save_sequencing_profile(psq)
    repo.save_sequence_event(psev)
    repo.save_ups_transition(upst)
    repo.save_pq_measurement(pqm)
    repo.save_pq_qualification(pqq)
    repo.save_emc_profile(emc)
    repo.save_emc_symptom(emcs)

    assert repo.get_sequencing_profile(psq.profile_id) == psq
    assert repo.get_sequence_event(psev.event_id) == psev
    assert repo.get_ups_transition(upst.record_id) == upst
    assert repo.get_pq_measurement(pqm.measurement_id) == pqm
    assert repo.get_pq_qualification(pqq.qualification_id) == pqq
    assert repo.get_emc_profile(emc.profile_id) == emc
    assert repo.get_emc_symptom(emcs.record_id) == emcs


def test_repository_idempotent_resave(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    p = _profile()
    repo.save_sequencing_profile(p)
    repo.save_sequencing_profile(p)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    p = _profile()
    repo.save_sequencing_profile(p)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_power_sequencing_profiles '
            "SET direction='power_off' WHERE profile_id=?",
            (p.profile_id,),
        )
        connection.commit()
    with pytest.raises(PowerEvidenceIntegrityError):
        repo.get_sequencing_profile(p.profile_id)


def test_powerev_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
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
        'cad_power_sequencing_profiles',
        'cad_power_sequence_events',
        'cad_ups_transition_records',
        'cad_power_quality_measurements',
        'cad_power_quality_qualifications',
        'cad_emc_product_profiles',
        'cad_emc_symptom_records',
    }
    assert expected <= tables
