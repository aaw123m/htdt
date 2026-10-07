"""Issue #791 — operational energy / power-mode evidence authority.

Covers mode/quantity/state separation, the CTA-6043 ↔ IEC 63474:2023
adoption pin (never silently upgraded to IEC 63474:2026), IEC 62087
stimulus requirements, scenario composition without hidden averages,
schedule-explicit derivations and repository round-trip + tamper
detection.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_operational_energy import (
    DevicePowerModeObservation,
    EnergyUseDerivation,
    NetworkedStandbyEvidence,
    OperationalEnergyIntegrityError,
    OperationalEnergyScenario,
    ScenarioComponent,
    evaluate_networked_standby_claim,
    evaluate_observation_currency,
)
from htdt.cad_operational_energy_repository import (
    CadOperationalEnergyRepository,
    OperationalEnergyConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.measurement_evidence_display import (
    energy_mode_label,
    energy_observation_line,
)


def _ref(kind: str, rid: str = 'x') -> AuthorityRef:
    return AuthorityRef(
        kind=kind, ref_id=rid, ref_sha256='a' * 64)


def _obs(document_id: str = 'doc-1', **kw) -> DevicePowerModeObservation:
    payload = dict(
        document_id=document_id,
        device_ref=_ref('equipment', 'avr-1'),
        mode='networked_standby',
        evidence_class='htdt_field_measured',
        firmware_version='1.4.2',
        config_preset='network-control-on',
        network_interfaces_enabled=('ethernet',),
        wake_capabilities=('wake_on_lan',),
        power_management_setting='deep_standby',
        real_power_w=1.8,
        observed_at_utc='2026-10-01T00:00:00Z',
    )
    payload.update(kw)
    return DevicePowerModeObservation.create(**payload)


def _nse(document_id: str = 'doc-1', **kw) -> NetworkedStandbyEvidence:
    payload = dict(
        document_id=document_id,
        device_ref=_ref('equipment', 'avr-1'),
        standard_profile='iec_63474_2026',
        standard_reference='IEC 63474@2026',
        network_interfaces=('ethernet',),
        connection_state='connected',
        network_available=True,
        wake_capability='wake_on_lan',
        power_management_configuration='deep_standby',
        low_power_reached=True,
        measurement_duration_s=3600.0,
        stability_rule='iec_63474_2026_default',
        result_w=1.8,
        evidence_class='standardized_lab_measured',
        observed_at_utc='2026-10-01T00:00:00Z',
    )
    payload.update(kw)
    return NetworkedStandbyEvidence.create(**payload)


def _scenario(
    document_id: str = 'doc-1',
    obs: DevicePowerModeObservation | None = None,
    **kw,
) -> OperationalEnergyScenario:
    obs = obs if obs is not None else _obs(document_id)
    payload = dict(
        document_id=document_id,
        scenario_kind='overnight_networked_standby',
        label='overnight',
        components=(
            ScenarioComponent(
                observation_ref=AuthorityRef(
                    kind='device_power_mode_observation',
                    ref_id=obs.observation_id,
                    ref_sha256=obs.observation_sha256),
                device_ref_id='avr-1',
                mode=obs.mode),
        ),
    )
    payload.update(kw)
    return OperationalEnergyScenario.create(**payload)


def _derivation(
    scenario: OperationalEnergyScenario, **kw,
) -> EnergyUseDerivation:
    payload = dict(
        document_id=scenario.document_id,
        scenario_ref=AuthorityRef(
            kind='operational_energy_scenario',
            ref_id=scenario.scenario_id,
            ref_sha256=scenario.scenario_sha256),
        derivation_kind='derived_schedule',
        derivation_version='1.0',
        mode_schedule=(
            {'mode': 'networked_standby', 'hours_per_year': 6000.0},
            {'mode': 'active_playback', 'hours_per_year': 500.0},
        ),
        annual_kwh=350.0,
        schedule_source='owner stated usage',
    )
    payload.update(kw)
    return EnergyUseDerivation.create(**payload)


def _repo(tmp_path):
    db = tmp_path / 'scene.sqlite3'
    ensure_native_schema(db)
    return CadOperationalEnergyRepository(SceneRepository(db))


# ------------------------------------------------------------------ observations


def test_observation_requires_pinned_device() -> None:
    with pytest.raises(ValueError):
        _obs(device_ref=AuthorityRef(
            kind='equipment', ref_id='avr-1', ref_sha256=None))


def test_observation_quantities_must_be_non_negative() -> None:
    for field in ('real_power_w', 'energy_wh', 'voltage_v',
                  'dwell_time_s'):
        with pytest.raises(ValueError):
            _obs(**{field: -1.0})


def test_observation_power_factor_bounded() -> None:
    with pytest.raises(ValueError):
        _obs(power_factor=1.2)


def test_energy_requires_interval() -> None:
    with pytest.raises(ValueError):
        _obs(energy_wh=42.0)


def test_standard_profile_requires_pinned_edition() -> None:
    with pytest.raises(ValueError):
        _obs(standard_profile='iec_62301_2026')
    with pytest.raises(ValueError):
        _obs(standard_profile='iec_62301_2026',
             standard_reference='IEC 62301')


def test_iec_62087_active_claim_requires_stimulus() -> None:
    with pytest.raises(ValueError):
        _obs(mode='active_playback',
             standard_profile='iec_62087_3_2023',
             standard_reference='IEC 62087-3@2023')
    # Non-active modes under the same profile need no stimulus.
    _obs(mode='idle_ready',
         standard_profile='iec_62087_3_2023',
         standard_reference='IEC 62087-3@2023')


# ------------------------------------------------------------------ standby evidence


def test_cta_6043_never_adopts_iec_2026() -> None:
    with pytest.raises(ValueError):
        _nse(standard_profile='ansi_cta_6043',
             standard_reference='ANSI/CTA-6043@2026',
             adopted_base_profile='iec_63474_2026')
    ok = _nse(standard_profile='ansi_cta_6043',
              standard_reference='ANSI/CTA-6043@2026',
              adopted_base_profile='iec_63474_2023_withdrawn')
    assert ok.standard_profile == 'ansi_cta_6043'


def test_iec_2026_direct_result_has_no_adopted_base() -> None:
    with pytest.raises(ValueError):
        _nse(adopted_base_profile='iec_63474_2023_withdrawn')


# ------------------------------------------------------------------ evaluators


def test_currency_unknown_without_state_epoch() -> None:
    assert evaluate_observation_currency(_obs(), _ref('state')) \
        == 'unknown'
    obs = _obs(device_state_ref=_ref('device_state', 's1'))
    assert evaluate_observation_currency(obs, None) == 'unknown'


def test_currency_stale_on_state_change() -> None:
    obs = _obs(device_state_ref=_ref('device_state', 's1'))
    assert evaluate_observation_currency(
        obs, _ref('device_state', 's1')) == 'current'
    assert evaluate_observation_currency(
        obs, _ref('device_state', 's2')) == 'stale_after_state_change'


def test_standby_claim_fail_closed() -> None:
    assert evaluate_networked_standby_claim('idle_ready', _nse()) \
        == 'not_networked_standby'
    assert evaluate_networked_standby_claim(
        'networked_standby', None) == 'networked_standby_unverified'
    assert evaluate_networked_standby_claim(
        'networked_standby',
        _nse(standard_profile='unknown',
             standard_reference=None)) == 'networked_standby_unverified'
    assert evaluate_networked_standby_claim(
        'networked_standby',
        _nse(low_power_reached=None)) == 'networked_standby_unverified'
    assert evaluate_networked_standby_claim(
        'networked_standby', _nse()) == 'networked_standby_verified'


# ------------------------------------------------------------------ scenarios


def test_scenario_requires_components_with_pinned_refs() -> None:
    with pytest.raises(ValueError):
        _scenario(components=())
    with pytest.raises(ValueError):
        ScenarioComponent(
            observation_ref=AuthorityRef(
                kind='device_power_mode_observation',
                ref_id='o', ref_sha256=None),
            device_ref_id='avr-1',
            mode='networked_standby')
    with pytest.raises(ValueError):
        ScenarioComponent(
            observation_ref=_ref('device_power_mode_observation'),
            device_ref_id='',
            mode='networked_standby')


# ------------------------------------------------------------------ derivations


def test_derivation_requires_explicit_schedule() -> None:
    with pytest.raises(ValueError):
        _derivation(_scenario(), mode_schedule=())


def test_derivation_tariff_all_or_nothing() -> None:
    for missing in ('tariff_currency', 'tariff_rate',
                    'tariff_unit', 'tariff_source'):
        kw = dict(
            tariff_currency='USD', tariff_rate=0.2,
            tariff_unit='kwh', tariff_source='utility bill 2026-09')
        kw[missing] = None
        with pytest.raises(ValueError):
            _derivation(_scenario(), **kw)
    ok = _derivation(
        _scenario(),
        tariff_currency='USD', tariff_rate=0.2,
        tariff_unit='kwh', tariff_source='utility bill 2026-09')
    assert ok.tariff_rate == 0.2


def test_derivation_schedule_entries_need_mode_and_hours() -> None:
    with pytest.raises(ValueError):
        _derivation(_scenario(), mode_schedule=({'mode': 'idle_ready'},))
    with pytest.raises(ValueError):
        _derivation(_scenario(), mode_schedule=(
            {'mode': 'idle_ready', 'hours_per_year': -1.0},))


# ------------------------------------------------------------------ repository


def test_repository_round_trip_all_records(tmp_path) -> None:
    repo = _repo(tmp_path)
    obs = _obs()
    nse = _nse()
    scenario = _scenario(obs=obs)
    derivation = _derivation(scenario)
    repo.power_mode_observations.save(obs)
    repo.networked_standby_evidence.save(nse)
    repo.scenarios.save(scenario)
    repo.derivations.save(derivation)
    assert repo.get_observation(obs.observation_id) == obs
    assert repo.get_standby_evidence(nse.evidence_id) == nse
    assert repo.get_scenario(scenario.scenario_id) == scenario
    assert repo.get_derivation(derivation.derivation_id) == derivation


def test_repository_append_only_conflict(tmp_path) -> None:
    repo = _repo(tmp_path)
    obs = _obs()
    repo.power_mode_observations.save(obs)
    tampered = _obs(real_power_w=99.0)
    object.__setattr__(tampered, 'observation_id', obs.observation_id)
    object.__setattr__(
        tampered, 'observation_sha256', obs.observation_sha256)
    with pytest.raises(
            (OperationalEnergyConflictError,
             OperationalEnergyIntegrityError)):
        repo.power_mode_observations.save(tampered)
    repo.power_mode_observations.save(obs)


def test_repository_detects_column_tampering(tmp_path) -> None:
    repo = _repo(tmp_path)
    obs = _obs()
    repo.power_mode_observations.save(obs)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_device_power_mode_observations '
            'SET mode=? WHERE observation_id=?',
            ('active', obs.observation_id))
    with pytest.raises(OperationalEnergyIntegrityError):
        repo.get_observation(obs.observation_id)


def test_repository_list_filters_by_document(tmp_path) -> None:
    repo = _repo(tmp_path)
    a = _obs(document_id='doc-a')
    b = _obs(document_id='doc-b')
    repo.power_mode_observations.save(a)
    repo.power_mode_observations.save(b)
    assert repo.power_mode_observations.list('doc-a') == (a,)
    assert repo.power_mode_observations.list() == (a, b)


# ------------------------------------------------------------------ labels


def test_ja_label_coverage() -> None:
    assert energy_observation_line('stale_after_state_change') \
        == '電源モード証拠: 状態変更により陳腐化'
    assert energy_mode_label('networked_standby') \
        == 'ネットワークスタンバイ'
    assert energy_mode_label('__none__') == '__none__'
