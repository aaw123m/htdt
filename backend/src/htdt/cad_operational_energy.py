"""Operational energy / power-mode evidence authority (#791).

Adequate circuit capacity and acceptable rack temperature do not say how
much power the commissioned system consumes in active, idle, standby,
networked-standby and off-like modes, nor whether firmware/network
settings let power management reach the intended low-power state. This
module owns that evidence: per-mode power observations bound to exact
device/firmware/network state, networked-standby profile evidence,
system-level operating scenarios and reproducible annualized-energy
derivations.

Layers stay separate and fail closed:

- mode identity is explicit (active/idle/standby/networked-standby/
  off-like/...); provider labels like ``Eco`` or ``Quick Start`` are
  recorded as labels, never normalized into mode semantics without
  evidence;
- every observation binds the exact device instance, firmware, config
  preset, network/power-management and workload state — a firmware or
  quick-start change is a different applicability identity;
- electrical quantities stay distinct (W, Wh/kWh over an interval, VA,
  PF, transition energy, dwell time); nothing collapses into one
  ``power_watts``;
- standards stay pinned by edition: IEC 63474:2026 (networked standby)
  vs IEC 62301:2026 (other non-active modes) vs ANSI/CTA-6043 (US
  adoption of IEC 63474:2023 — explicitly NOT the 2026 edition) vs
  IEC 62087-2/-3 product/profile work; a CTA result is never silently
  relabeled as current IEC;
- evidence classes stay distinct (standardized-lab / independent-lab /
  manufacturer-declared / HTDT-field / telemetry / PDU-meter /
  derived); a manufacturer standby spec is design evidence, not proof
  the installed state reaches it;
- system scenarios keep per-device mode evidence; an always-on
  endpoint is never hidden inside a system average;
- annualized energy is DERIVED from an explicit mode-hours schedule
  (or a pinned long-term meter campaign) — never a hidden usage
  assumption; tariff/cost inputs stay external and versioned.

Boundaries this module does not own: rack/thermal qualification (#587
consumes measured power but does not get nameplate-as-normal-load),
power sequencing/outage recovery (#736 — transitions' safety vs their
energy), power quality (#738), lifecycle/carbon factors (external
profiles only — no ``green`` rating from low standby), and AV
performance requirements (an energy-saving mode never silently
overrides a required image/sound target).

Basis: issue #791 scope; IEC 63474:2026 (Ed.2, networked standby,
replaces 63474:2023); IEC 62301:2026 (standby/other non-active);
ANSI/CTA-6043 (2026-04, US adoption of IEC 63474:2023);
IEC 62087-2:2023 (signals/media incl. HDR/UHD); IEC 62087-3:2023
(television-set power, product-class specific).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


def _require_profile_pin(reference: str) -> None:
    if '@' not in reference:
        raise ValueError(
            'standard/profile references must pin exact editions '
            '(standard_id@edition)')


class OperationalEnergyIntegrityError(ValueError):
    """A sealed operational-energy record failed integrity checks."""


PowerMode = Literal[
    'active',
    'active_playback',
    'idle_ready',
    'standby_non_networked',
    'networked_standby',
    'off_mode',
    'sleep',
    'update_maintenance',
    'boot_startup',
    'shutdown',
    'device_specific_other',
    'unknown',
]

EnergyEvidenceClass = Literal[
    'standardized_lab_measured',
    'independent_lab',
    'manufacturer_declared',
    'htdt_field_measured',
    'device_telemetry',
    'smart_pdu_meter_observed',
    'derived',
    'unknown',
]

EnergyStandardProfile = Literal[
    'iec_63474_2026',
    'iec_63474_2023_withdrawn',
    'ansi_cta_6043',
    'iec_62301_2026',
    'iec_62087_2_2023',
    'iec_62087_3_2023',
    'other_product_standard',
    'project_defined',
    'unknown',
]

ScenarioKind = Literal[
    'away_all_off',
    'ready_for_remote_control',
    'movie_standby',
    'movie_active_sdr',
    'movie_active_hdr',
    'gaming_active',
    'reference_audio_stress',
    'overnight_networked_standby',
    'maintenance_update',
    'custom',
]

DerivationKind = Literal[
    'derived_schedule',
    'long_term_meter',
]

ObservationCurrency = Literal[
    'current',
    'stale_after_state_change',
    'unknown',
]

NetworkedStandbyClaim = Literal[
    'networked_standby_verified',
    'networked_standby_unverified',
    'not_networked_standby',
]

class ScenarioComponent(BaseModel):
    """One device contribution inside a system scenario — the per-device
    mode and its pinned observation ref stay visible so an always-on
    endpoint is never hidden inside a system average."""

    model_config = ConfigDict(frozen=True)

    observation_ref: AuthorityRef
    device_ref_id: str
    mode: PowerMode
    real_power_w: float | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ScenarioComponent':
        _require_refs(self.observation_ref)
        if not self.device_ref_id:
            raise ValueError('device_ref_id is required')
        if self.real_power_w is not None and self.real_power_w < 0.0:
            raise ValueError('real_power_w must be >= 0')
        return self


ENERGY_LABELS: dict[str, str] = {
    'active': 'アクティブ',
    'active_playback': 'アクティブ再生',
    'idle_ready': 'アイドル/レディ',
    'standby_non_networked': 'スタンバイ（非ネットワーク）',
    'networked_standby': 'ネットワークスタンバイ',
    'off_mode': 'オフモード',
    'sleep': 'スリープ',
    'update_maintenance': 'アップデート/保守',
    'boot_startup': '起動中',
    'shutdown': 'シャットダウン',
    'device_specific_other': '機器固有モード',
    'current': '最新',
    'stale_after_state_change': '状態変更により陳腐化',
    'networked_standby_verified': 'ネットワークスタンバイ検証済み',
    'networked_standby_unverified': 'ネットワークスタンバイ未検証',
    'not_networked_standby': 'ネットワークスタンバイ非該当',
    'derived_schedule': 'スケジュール由来（導出）',
    'long_term_meter': '長期メータ実測',
    'unknown': '不明',
}


class DevicePowerModeObservation(BaseModel):
    """One power-mode observation for one exact device state
    (dpmo- prefix).

    Mode, electrical quantities and the producing state are bound
    together — a standby watt number detached from firmware/network/
    power-management state is not evidence. ``device_state_ref``
    optionally pins the #592/#595 device-state epoch so a later firmware
    or config change can stale exactly the affected observations.
    Standards-based measurements must pin the profile edition
    (``standard_reference = 'IEC 63474@2026'`` form); an IEC 62087
    product-profile claim additionally requires the stimulus/media ref
    (#608) that produced the active reading.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef
    device_state_ref: AuthorityRef | None = None
    mode: PowerMode
    provider_mode_label: str | None = None
    evidence_class: EnergyEvidenceClass
    standard_profile: EnergyStandardProfile = 'unknown'
    standard_reference: str | None = None
    firmware_version: str | None = None
    config_preset: str | None = None
    network_interfaces_enabled: tuple[str, ...] = ()
    wake_capabilities: tuple[str, ...] = ()
    power_management_setting: str | None = None
    display_mode: str | None = None
    amplifier_state: str | None = None
    wireless_transport_state: str | None = None
    real_power_w: float | None = None
    energy_wh: float | None = None
    energy_interval_s: float | None = None
    apparent_power_va: float | None = None
    power_factor: float | None = None
    current_a: float | None = None
    voltage_v: float | None = None
    transition_energy_wh: float | None = None
    dwell_time_s: float | None = None
    stimulus_ref: AuthorityRef | None = None
    workload_state: str | None = None
    observed_at_utc: str
    duration_s: float | None = None
    source_provenance: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'DevicePowerModeObservation':
        _require_refs(self.device_ref)
        for ref in (self.device_state_ref, self.stimulus_ref):
            if ref is not None:
                _require_refs(ref)
        if not self.observed_at_utc:
            raise ValueError('observed_at_utc is required')
        if self.standard_profile not in (
                'project_defined', 'unknown') \
                and not self.standard_reference:
            raise ValueError(
                'standard_reference is required for a declared '
                'standard profile')
        if self.standard_reference:
            _require_profile_pin(self.standard_reference)
        if self.standard_profile in (
                'iec_62087_2_2023', 'iec_62087_3_2023') \
                and self.stimulus_ref is None \
                and self.mode in ('active', 'active_playback'):
            raise ValueError(
                'an IEC 62087 active-power claim requires the exact '
                'stimulus/media reference — `playing video` is not a '
                'measurand')
        for name in (
                'real_power_w', 'energy_wh', 'energy_interval_s',
                'apparent_power_va', 'current_a', 'voltage_v',
                'transition_energy_wh', 'dwell_time_s', 'duration_s'):
            value = getattr(self, name)
            if value is not None and value < 0.0:
                raise ValueError(f'{name} must be >= 0')
        if self.power_factor is not None \
                and not 0.0 <= self.power_factor <= 1.0:
            raise ValueError('power_factor must be within [0, 1]')
        if self.energy_wh is not None \
                and self.energy_interval_s is None:
            raise ValueError(
                'energy_wh requires energy_interval_s — energy '
                'without an interval is not a quantity')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'DevicePowerModeObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'dpmo')


class NetworkedStandbyEvidence(BaseModel):
    """Networked-standby profile evidence for one device (nse- prefix).

    Keeps the IEC 63474 / CTA-6043 semantics exact: interfaces,
    connection state, network availability, wake/control capability,
    power-management configuration, disable support/test and the
    duration/stability rule all stay separate fields. The CTA-6043 ↔
    IEC 63474:2023 adoption is recorded on the record itself so a US
    adoption is never silently promoted to the newer IEC edition.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    device_ref: AuthorityRef
    standard_profile: Literal[
        'iec_63474_2026', 'iec_63474_2023_withdrawn',
        'ansi_cta_6043', 'other_product_standard',
        'project_defined', 'unknown',
    ]
    standard_reference: str | None = None
    adopted_base_profile: EnergyStandardProfile | None = None
    network_interfaces: tuple[str, ...] = ()
    connection_state: str | None = None
    network_available: bool | None = None
    wake_capability: str | None = None
    power_management_configuration: str | None = None
    disable_supported: bool | None = None
    disable_tested: bool | None = None
    low_power_reached: bool | None = None
    measurement_duration_s: float | None = None
    stability_rule: str | None = None
    result_w: float | None = None
    evidence_class: EnergyEvidenceClass
    observed_at_utc: str
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'NetworkedStandbyEvidence':
        _require_refs(self.device_ref)
        if not self.observed_at_utc:
            raise ValueError('observed_at_utc is required')
        if self.standard_profile not in (
                'project_defined', 'unknown') \
                and not self.standard_reference:
            raise ValueError(
                'standard_reference is required for a declared '
                'standard profile')
        if self.standard_reference:
            _require_profile_pin(self.standard_reference)
        if self.standard_profile == 'ansi_cta_6043' \
                and self.adopted_base_profile \
                not in (None, 'iec_63474_2023_withdrawn'):
            raise ValueError(
                'ANSI/CTA-6043 adopts IEC 63474:2023 — it can never be '
                'recorded as adopting iec_63474_2026')
        if self.standard_profile == 'iec_63474_2026' \
                and self.adopted_base_profile is not None:
            raise ValueError(
                'a direct IEC 63474:2026 result has no adopted base '
                'profile')
        if self.result_w is not None and self.result_w < 0.0:
            raise ValueError('result_w must be >= 0')
        if self.measurement_duration_s is not None \
                and self.measurement_duration_s <= 0.0:
            raise ValueError('measurement_duration_s must be > 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'NetworkedStandbyEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'nse')


class OperationalEnergyScenario(BaseModel):
    """One system-level operating scenario composed of per-device mode
    observations (oes- prefix).

    ``components`` keeps each device's contribution visible — a
    scenario is never a bare average that could hide an always-on
    endpoint. The optional thermal link states explicitly that rack
    heat (#587) is derived from measured power under the recorder's
    approximation, never from nameplate maximums.
    """

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    scenario_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    scenario_kind: ScenarioKind
    label: str
    components: tuple[ScenarioComponent, ...]
    excluded_loads: tuple[str, ...] = ()
    thermal_note: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'OperationalEnergyScenario':
        if not self.components:
            raise ValueError(
                'at least one device component is required — a system '
                'scenario is never a bare average')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'scenario_id', 'scenario_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'OperationalEnergyScenario':
        return _seal(
            cls, payload, 'scenario_id', 'scenario_sha256', 'oes')


class EnergyUseDerivation(BaseModel):
    """An annualized-energy derivation for one scenario (eud- prefix).

    ``mode_schedule`` is the explicit hours/year-by-mode contract the
    derivation runs on; ``annual_kwh`` is a DERIVED result (or a pinned
    long-term meter campaign), never a hidden assumption. Cost is
    optional and always carries its own source/currency/unit/time — no
    regional tariff is baked into HTDT.
    """

    model_config = ConfigDict(frozen=True)

    derivation_id: str
    derivation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    scenario_ref: AuthorityRef
    derivation_kind: DerivationKind = 'derived_schedule'
    derivation_version: str
    mode_schedule: tuple[dict[str, Any], ...]
    annual_kwh: float
    schedule_source: str
    occupancy_assumption: str | None = None
    uncertainty_kwh_low: float | None = None
    uncertainty_kwh_high: float | None = None
    tariff_currency: str | None = None
    tariff_rate: float | None = None
    tariff_unit: str | None = None
    tariff_source: str | None = None
    tariff_valid_from: str | None = None
    excluded_loads: tuple[str, ...] = ()
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'EnergyUseDerivation':
        _require_refs(self.scenario_ref)
        if self.derivation_kind == 'derived_schedule' \
                and not self.mode_schedule:
            raise ValueError(
                'a derived_schedule derivation requires an explicit '
                'mode schedule — no hidden usage assumptions')
        for entry in self.mode_schedule:
            if 'mode' not in entry or 'hours_per_year' not in entry:
                raise ValueError(
                    'every schedule entry needs mode + hours_per_year')
            if entry['hours_per_year'] < 0.0:
                raise ValueError('hours_per_year must be >= 0')
        if self.annual_kwh < 0.0:
            raise ValueError('annual_kwh must be >= 0')
        if not self.derivation_version:
            raise ValueError('derivation_version is required')
        if not self.schedule_source:
            raise ValueError('schedule_source is required')
        tariff_fields = (
            self.tariff_currency, self.tariff_rate,
            self.tariff_unit, self.tariff_source)
        if any(f is not None for f in tariff_fields) \
                and not all(f is not None for f in tariff_fields):
            raise ValueError(
                'tariff requires currency + rate + unit + source '
                'together — partial tariffs are rejected')
        if self.tariff_rate is not None and self.tariff_rate < 0.0:
            raise ValueError('tariff_rate must be >= 0')
        for name in ('uncertainty_kwh_low', 'uncertainty_kwh_high'):
            value = getattr(self, name)
            if value is not None and value < 0.0:
                raise ValueError(f'{name} must be >= 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'derivation_id', 'derivation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'EnergyUseDerivation':
        return _seal(
            cls, payload, 'derivation_id', 'derivation_sha256', 'eud')


def evaluate_observation_currency(
    observation: DevicePowerModeObservation,
    current_device_state: AuthorityRef | None,
) -> ObservationCurrency:
    """Whether a power-mode observation still reflects the device's
    current state epoch.

    Fail-closed: an observation with no pinned device-state epoch can
    only ever be ``unknown`` — it cannot be proven stale but equally
    cannot be proven current. A pinned epoch that no longer matches the
    current state is ``stale_after_state_change`` (firmware/config
    regressions like ENR30 invalidate exactly this evidence).
    """
    if observation.device_state_ref is None \
            or current_device_state is None:
        return 'unknown'
    if observation.device_state_ref.ref_id \
            != current_device_state.ref_id \
            or observation.device_state_ref.ref_sha256 \
            != current_device_state.ref_sha256:
        return 'stale_after_state_change'
    return 'current'


def evaluate_networked_standby_claim(
    observation_mode: PowerMode,
    evidence: NetworkedStandbyEvidence | None,
) -> NetworkedStandbyClaim:
    """Whether a device may carry the `networked_standby` mode.

    An idle device with a cable attached is not networked standby: the
    claim needs IEC 63474/CTA-class profile evidence showing
    low_power_reached under a measured duration/stability rule.
    """
    if observation_mode != 'networked_standby':
        return 'not_networked_standby'
    if evidence is None:
        return 'networked_standby_unverified'
    if evidence.standard_profile == 'unknown' \
            or evidence.low_power_reached is not True \
            or evidence.measurement_duration_s is None \
            or evidence.result_w is None:
        return 'networked_standby_unverified'
    return 'networked_standby_verified'
