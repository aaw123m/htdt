"""REV59-INFRA2 regression tests — #736 power sequencing, #738 AC
quality, #740 occupied IAQ, #750 material emissions, #751 product
safety, #752 EMC."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_facility_repository import (
    CadFacilityRepository,
    FacilityIntegrityError,
)
from htdt.cad_indoor_environment import (
    IndoorAirObservation,
    MaterialEmissionEvidence,
    evaluate_indoor_claim,
)
from htdt.cad_power_sequence import (
    PowerQualityObservation,
    PowerSequenceEvidence,
    PowerSequencePlan,
    evaluate_power_claim,
)
from htdt.cad_product_compliance import (
    EMCComplianceEvidence,
    ProductSafetyEvidence,
    evaluate_compliance_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadFacilityRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadFacilityRepository(SceneRepository(db))


def _plan(**kw) -> PowerSequencePlan:
    payload = dict(
        document_id='doc-1',
        steps=('processor', 'switch', 'amplifier'),
        amplifier_step='amplifier',
        amplifier_last_on_first_off=True,
        inrush_staged=True,
        always_on_devices=('control',),
        edid_control_dependencies=('sink-first',),
    )
    payload.update(kw)
    return PowerSequencePlan.create(payload)


def _psev(**kw) -> PowerSequenceEvidence:
    payload = dict(
        document_id='doc-1',
        plan_ref=_ref('p-1'),
        outcome='clean_sequence',
        observation_ref=_ref('obs-1'),
    )
    payload.update(kw)
    return PowerSequenceEvidence.create(payload)


def _pqo(**kw) -> PowerQualityObservation:
    payload = dict(
        document_id='doc-1',
        instrument_class='iec_61000_4_30_class_a',
        observed_events=('voltage_dip',),
        rms_voltage_v=100.2,
        frequency_hz=50.01,
    )
    payload.update(kw)
    return PowerQualityObservation.create(payload)


def _iao(**kw) -> IndoorAirObservation:
    payload = dict(
        document_id='doc-1',
        sensor_class='ndir_co2',
        co2_ppm_peak=980.0,
        temperature_c=23.5,
        session_duration_min=150,
        capture_ref=_ref('cap-1'),
    )
    payload.update(kw)
    return IndoorAirObservation.create(payload)


def _mem(**kw) -> MaterialEmissionEvidence:
    payload = dict(
        document_id='doc-1',
        emission_class='voc_chamber',
        tested_product_descriptor='panel-A build-up v2',
        report_ref=_ref('rpt-1'),
    )
    payload.update(kw)
    return MaterialEmissionEvidence.create(payload)


def _pse(**kw) -> ProductSafetyEvidence:
    payload = dict(
        document_id='doc-1',
        safety_standard='iec_62368_1',
        standard_edition='ed3',
        tested_variant='AMP-1200 / PSU-X',
        certificate_ref=_ref('cert-1'),
        market_scope='JP/EU',
    )
    payload.update(kw)
    return ProductSafetyEvidence.create(payload)


def _emc(**kw) -> EMCComplianceEvidence:
    payload = dict(
        document_id='doc-1',
        profile_kind='cispr_32_emissions',
        equipment_class='class_b',
        tested_configuration='AMP-1200 rack config',
        report_ref=_ref('emc-1'),
    )
    payload.update(kw)
    return EMCComplianceEvidence.create(payload)


def test_plan_requires_steps_and_amplifier_rule():
    with pytest.raises(ValueError, match='steps'):
        _plan(steps=())
    with pytest.raises(ValueError, match='last-on'):
        _plan(amplifier_last_on_first_off=False)


def test_sequence_evidence_requires_plan_and_observation():
    with pytest.raises(ValueError):
        _psev(plan_ref=None)
    with pytest.raises(ValueError, match='observation'):
        _psev(observation_ref=None)


def test_quality_observation_requires_class_and_events():
    with pytest.raises(ValueError):
        _pqo(instrument_class='unknown')
    with pytest.raises(ValueError, match='event'):
        _pqo(observed_events=())
    with pytest.raises(ValueError, match='capture'):
        _pqo(correlated_symptom='amp reboot')


def test_indoor_observation_requires_sensor_and_capture():
    with pytest.raises(ValueError):
        _iao(sensor_class='unknown')
    with pytest.raises(ValueError):
        _iao(capture_ref=None)


def test_emission_requires_descriptor_and_report():
    with pytest.raises(ValueError):
        _mem(emission_class='unknown')
    with pytest.raises(ValueError, match='tested'):
        _mem(tested_product_descriptor='')
    with pytest.raises(ValueError, match='report'):
        _mem(report_ref=None)
    m = _mem(emission_class='manufacturer_program', report_ref=None)
    assert m.emission_class == 'manufacturer_program'


def test_safety_requires_certificate_and_variant():
    with pytest.raises(ValueError):
        _pse(safety_standard='unknown')
    with pytest.raises(ValueError):
        _pse(certificate_ref=None)
    with pytest.raises(ValueError):
        _pse(tested_variant='')


def test_emc_requires_class_and_report():
    with pytest.raises(ValueError):
        _emc(profile_kind='unknown')
    with pytest.raises(ValueError, match='Class'):
        _emc(equipment_class=None)
    with pytest.raises(ValueError):
        _emc(report_ref=None)


def test_power_claim_ladder():
    p, e, q = _plan(), _psev(), _pqo()
    assert evaluate_power_claim(
        None, None, None, capacity_qualified=True
    )[0] == 'capacity_is_not_sequence'
    assert evaluate_power_claim(None, None, None)[0] == (
        'order_unverified'
    )
    e_bad = _psev(outcome='timeout')
    assert evaluate_power_claim(p, e_bad, q)[0] == 'order_unverified'
    assert evaluate_power_claim(p, e, None)[0] == 'quality_unobserved'
    assert evaluate_power_claim(p, e, q)[0] == 'qualified_sequence'


def test_indoor_claim_ladder():
    o, m = _iao(), _mem()
    assert evaluate_indoor_claim(
        None, None, ventilation_design_qualified=True
    )[0] == 'design_is_not_occupied_outcome'
    assert evaluate_indoor_claim(None, None)[0] == (
        'observation_unqualified'
    )
    m_prog = _mem(emission_class='manufacturer_program',
                  report_ref=None)
    assert evaluate_indoor_claim(o, (m_prog,))[0] == (
        'certificate_is_not_room_iaq'
    )
    assert evaluate_indoor_claim(o, None)[0] == 'emission_unbounded'
    assert evaluate_indoor_claim(o, (m,))[0] == 'qualified_indoor'


def test_compliance_claim_ladder():
    s, e = _pse(), _emc()
    assert evaluate_compliance_claim(
        None, None, performance_qualified=True
    )[0] == 'performance_is_not_safety'
    assert evaluate_compliance_claim(None, None)[0] == (
        'safety_unverified'
    )
    s_jp = _pse(market_scope='US-only')
    assert evaluate_compliance_claim(
        s_jp, e, project_market='JP'
    )[0] == 'scope_mismatch'
    assert evaluate_compliance_claim(s, None)[0] == 'emc_unverified'
    assert evaluate_compliance_claim(
        s, e, field_symptom_observed=True
    )[0] == 'field_symptom_is_not_emc'
    assert evaluate_compliance_claim(s, e)[0] == 'qualified_compliance'


def test_roundtrip_all_seven(tmp_path):
    repo = _repo(tmp_path)
    recs = (_plan(), _psev(), _pqo(), _iao(), _mem(), _pse(), _emc())
    repo.save_power_plan(recs[0])
    repo.save_power_evidence(recs[1])
    repo.save_quality_observation(recs[2])
    repo.save_indoor_observation(recs[3])
    repo.save_emission_evidence(recs[4])
    repo.save_safety_evidence(recs[5])
    repo.save_emc_evidence(recs[6])
    assert repo.get_power_plan(recs[0].plan_id) == recs[0]
    assert repo.get_power_evidence(recs[1].evidence_id) == recs[1]
    assert repo.get_quality_observation(
        recs[2].observation_id) == recs[2]
    assert repo.get_indoor_observation(
        recs[3].observation_id) == recs[3]
    assert repo.get_emission_evidence(recs[4].evidence_id) == recs[4]
    assert repo.get_safety_evidence(recs[5].evidence_id) == recs[5]
    assert repo.get_emc_evidence(recs[6].evidence_id) == recs[6]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    s = _pse()
    repo.save_safety_evidence(s)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_product_safety_evidence SET '
            'safety_standard=? WHERE evidence_id=?',
            ('ul_listed', s.evidence_id),
        )
    with pytest.raises(FacilityIntegrityError):
        repo.get_safety_evidence(s.evidence_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadFacilityRepository(SceneRepository(db))
    p = _plan()
    repo.save_power_plan(p)
    assert repo.get_power_plan(p.plan_id) == p
