"""Rack / power / thermal infrastructure qualification (issue #587).

A correctly designed audio/video system can still fail in service
because its rack, power and thermal environment cannot sustain the
intended operating state. This module is that qualification authority:
it versions the physical rack population, per-device thermal and
electrical evidence, branch-circuit/PDU/UPS limits, PoE budgets,
scenario-based loads, sustained thermal measurements and a fail-closed
per-dimension verdict.

Standards basis (see docs/reviews/rev56-infra.md): AVIXA
F502.02:2020 (R2023) *Rack Design for AV Systems* and F502.01:2018
(R2023) *Rack Building for Audiovisual Systems* (thermal management,
power system design, earthing/bonding), CEDIA/CTA-RP22 v1.2 §10
(access, weight, ventilation, wiring, ground-loop-free grounding,
spike/blackout/brownout protection) and CEDIA 2026 PoE guidance
(total-power budgeting plus headroom). All standard references pin an
exact revision through the #599 registry; none implies HTDT
certification.

Contract properties:

- heat is evidence, not arithmetic: manufacturer dissipation data and
  heat *derived* from electrical input power are different evidence
  classes (:data:`HeatEvidenceClass`); a nameplate maximum can never be
  silently presented as a real thermal load;
- qualification is scenario-based (:data:`InfraScenarioKind`): a system
  stable at idle is not qualified for sustained high-output playback,
  and a five-minute cold-start check never qualifies a multi-hour
  stress scenario (:data:`RackThermalMeasurement.warm_up_complete`);
- first-order ventilation math (:func:`evaluate_infrastructure`) is
  labelled ``calculated_with_limitations`` — it is never presented as
  CFD or as measured truth (:data:`ThermalCapabilityState`);
- electrical capability stays declared, not invented: circuit limits
  use the *declared* continuous-load policy of the record — HTDT does
  not hard-code electrical-code thresholds, it evaluates against what
  the installer declared;
- surge protection, power conditioning, UPS backup, voltage regulation,
  sequencing and remote control are separate capabilities
  (:data:`ProtectionCapability`) — a marketed 'power conditioner' is
  never equivalent to a UPS;
- PoE per-port and aggregate budgets stay explicit
  (:class:`PoEBudget`) — endpoint support is never claimed from
  Ethernet data compatibility alone;
- cooling noise is a separate axis that composes with #580: a rack that
  meets temperature targets by violating room-noise targets is
  ``not_qualified``, never a hidden average;
- fail-closed: no single dimension upgrades another, an unassessed
  system reads ``insufficient_evidence``, and service actions that
  change device identity/wiring stale the dependent evidence through
  the caller, never silently.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)
from .clock import utc_now_iso as _utc_now


_SHA256 = r'^[0-9a-f]{64}$'

INFRASTRUCTURE_SCHEMA_VERSION = 1

RACK_AUTHORITY_VERSION = 'rev56-infra-rack-1'
RACK_DEVICE_AUTHORITY_VERSION = 'rev56-infra-device-1'
CIRCUIT_AUTHORITY_VERSION = 'rev56-infra-circuit-1'
PROTECTION_AUTHORITY_VERSION = 'rev56-infra-protection-1'
POE_AUTHORITY_VERSION = 'rev56-infra-poe-1'
SCENARIO_AUTHORITY_VERSION = 'rev56-infra-scenario-1'
THERMAL_MEASUREMENT_AUTHORITY_VERSION = 'rev56-infra-thermal-1'
INFRA_QUALIFICATION_AUTHORITY_VERSION = 'rev56-infra-qualification-1'
INFRA_EVALUATION_VERSION = 'rev56-infra-eval-1'

#: 1 W of dissipated electrical power is 3.412 BTU/h of heat to remove —
#: the standard first-order conversion used industry-wide (see
#: docs/reviews/rev56-infra.md); essentially all consumed power becomes
#: heat inside the enclosure.
WATTS_TO_BTU_PER_HOUR = 3.412

#: Sensible-heat airflow relation: CFM = Q(BTU/h) / (1.08 * ΔT(°F)) —
#: first-order constant (air density × specific heat × time
#: conversion); valid only as an engineering estimate, which is exactly
#: what ``calculated_with_limitations`` means.
SENSIBLE_HEAT_CONSTANT = 1.08


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _finite(value: float, label: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{label} must be finite')
    return value


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

RackEnclosureKind = Literal[
    'open_frame',
    'closed_cabinet',
    'wall_cabinet',
    'closet_shelf',
    'furniture_console',
    'other',
    'unknown',
]
"""Enclosure configuration (#587 §1): open frame vs closed cabinetry is
the primary passive-cooling determinant."""

VentPathKind = Literal[
    'front', 'rear', 'side', 'top', 'bottom', 'passive_open',
    'none', 'unknown',
]
"""Airflow path direction (#587 §6). ``passive_open`` is a structurally
open path (no duct), not a measured capacity."""

CoolingDeviceKind = Literal[
    'passive_vent',
    'exhaust_fan',
    'intake_fan',
    'blower',
    'filter_grille',
    'ducted_hvac_return',
    'thermostatic_exhaust',
    'other',
]
"""Cooling device taxonomy (#587 §1/§6)."""

ACTIVE_COOLING_KINDS = frozenset({
    'exhaust_fan', 'intake_fan', 'blower', 'thermostatic_exhaust',
})
"""Kinds that move air — only these can carry a numeric CFM claim."""

FanControlMode = Literal[
    'fixed_speed', 'variable_speed', 'thermostatic', 'unknown'
]

ServiceAccess = Literal[
    'front', 'rear', 'front_and_rear', 'side', 'none_declared', 'unknown'
]
"""Service access class (#587 §13)."""

AirflowManagement = Literal['full', 'partial', 'none', 'unknown']
"""Blanking-panel / airflow-management population (#587 §1)."""

CableEntryZone = Literal['rear', 'bottom', 'top', 'side', 'unknown']

DeviceRole = Literal[
    'amplifier',
    'avr_processor',
    'dsp',
    'source_device',
    'display_projector',
    'network_switch',
    'pdu',
    'ups',
    'media_server',
    'control_processor',
    'other',
    'unknown',
]
"""Installed-device role (#587 §8) — thermal state couples to different
downstream authorities per role (amplifier compression, DSP stability,
projector fan ramp, HDMI/network reliability)."""

HeatEvidenceClass = Literal[
    'manufacturer_thermal_data',
    'derived_from_input_power',
    'unknown',
]
"""Evidence class for a device's heat output (#587 §2): a manufacturer
BTU/dissipation figure and a value *derived* from consumed input power
are different evidence and are never silently interchanged."""

ThermalProtectionState = Literal[
    'declared', 'observed', 'none_declared', 'unknown'
]
"""Whether thermal protection/throttling behaviour is documented or
observed (#587 §2)."""

ContinuousLoadPolicy = Literal[
    'continuous_derated', 'nameplate_only', 'other_declared', 'unknown'
]
"""The *declared* continuous-load policy of a circuit record (#587 §4).
``continuous_derated`` means the installer declared the circuit usable
to 80 % of its breaker rating (the common continuous-load convention);
HTDT evaluates the declared value and never invents a universal code
threshold."""

ProtectionCapability = Literal[
    'surge_protection',
    'power_conditioning',
    'ups_backup',
    'voltage_regulation',
    'sequencing',
    'remote_power_control',
]
"""Separate protection capabilities (#587 §5) — never merged into one
'conditioned power' boolean."""

ProtectionObservedState = Literal[
    'in_service', 'bypassed', 'failed', 'unknown'
]

PoEStandard = Literal[
    'ieee802_3af',     # 15.4 W at the PSE port
    'ieee802_3at',     # 30 W
    'ieee802_3bt_type3',  # 60 W
    'ieee802_3bt_type4',  # 90 W
    'vendor_upoe',     # vendor-specific, declared limit
    'other',
    'unknown',
]
"""PoE generation (#587 §11). Per-port ceilings are the IEEE-defined
PSE-side values for the standard kinds; ``vendor_upoe``/``other`` carry
a declared limit instead."""

_POE_PORT_CEILING_W: dict[str, float] = {
    'ieee802_3af': 15.4,
    'ieee802_3at': 30.0,
    'ieee802_3bt_type3': 60.0,
    'ieee802_3bt_type4': 90.0,
    'vendor_upoe': 90.0,
}

InfraScenarioKind = Literal[
    'standby',
    'idle_normal',
    'movie_typical',
    'reference_capability_stress',
    'lfe_stress',
    'multichannel_stress',
    'gaming_high_frame_rate',
    'projector_warmed',
    'poe_full_load',
    'service_firmware_update',
    'other',
]
"""Operating-scenario taxonomy (#587 §3) — composes with #224 program
material stress profiles via ``program_material_ref``."""

STRESS_SCENARIO_KINDS = frozenset({
    'reference_capability_stress',
    'lfe_stress',
    'multichannel_stress',
    'gaming_high_frame_rate',
    'projector_warmed',
    'poe_full_load',
})
"""Scenario kinds that assert sustained high output — a short cold
observation never qualifies them (#587 §7)."""

ScenarioLoadSource = Literal[
    'measured', 'manufacturer', 'derived', 'assumed', 'unknown'
]
"""Where a per-device scenario load figure came from (#587 §2/§4)."""

TemperaturePoint = Literal[
    'rack_inlet', 'rack_exhaust', 'rack_internal', 'ambient_room',
    'device_exhaust', 'other',
]
"""Measurement point taxonomy (#587 §6/§14)."""

ThermalCapabilityState = Literal[
    'thermally_measured',
    'calculated_with_limitations',
    'manufacturer_guidance_only',
    'over_capacity',
    'insufficient_evidence',
    'not_applicable',
]
"""Per-dimension thermal verdict (#587 §6/§15)."""

ElectricalCapabilityState = Literal[
    'power_measured',
    'calculated_with_limitations',
    'over_capacity',
    'insufficient_evidence',
    'not_applicable',
]

ProtectionState = Literal[
    'ups_verified',
    'protection_declared',
    'ups_insufficient',
    'none_declared',
    'insufficient_evidence',
    'not_applicable',
]

CoolingNoiseState = Literal[
    'noise_verified',
    'noise_target_failed',
    'insufficient_evidence',
    'not_applicable',
]

ServiceabilityState = Literal['serviceable', 'limited', 'unknown']

InfrastructureOverallState = Literal[
    'qualified',
    'qualified_with_limitations',
    'not_qualified',
    'over_capacity',
    'insufficient_evidence',
    'design_only',
]
"""Document-scenario verdict (#587 §15). One dimension passing never
upgrades all dimensions; the overall state is the honest worst-case
summary with the exact failing/limited dimensions in ``limitations``."""


# ---------------------------------------------------------------------------
# JA display labels
# ---------------------------------------------------------------------------

RACK_KIND_LABELS = {
    'open_frame': 'オープンフレーム',
    'closed_cabinet': '密閉キャビネット',
    'wall_cabinet': '壁掛けキャビネット',
    'closet_shelf': 'クローゼット棚',
    'furniture_console': '家具コンソール',
    'other': 'その他',
    'unknown': '不明',
}

SCENARIO_KIND_LABELS = {
    'standby': 'スタンバイ',
    'idle_normal': 'アイドル・通常UI',
    'movie_typical': '通常映画再生',
    'reference_capability_stress': 'リファレンス能力ストレス',
    'lfe_stress': 'LFEストレス',
    'multichannel_stress': 'マルチチャンネルストレス',
    'gaming_high_frame_rate': '高フレームレートゲーミング',
    'projector_warmed': 'プロジェクター暖機',
    'poe_full_load': 'PoE全負荷',
    'service_firmware_update': 'サービス・ファームウェア更新',
    'other': 'その他',
}

THERMAL_STATE_LABELS = {
    'thermally_measured': '熱実測済み',
    'calculated_with_limitations': '一次計算（制限付き）',
    'manufacturer_guidance_only': 'メーカー指針のみ',
    'over_capacity': '容量超過',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象外',
}

ELECTRICAL_STATE_LABELS = {
    'power_measured': '電力実測済み',
    'calculated_with_limitations': '一次計算（制限付き）',
    'over_capacity': '容量超過',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象外',
}

PROTECTION_STATE_LABELS = {
    'ups_verified': 'UPS検証済み',
    'protection_declared': '保護宣言あり',
    'ups_insufficient': 'UPS容量不足',
    'none_declared': '保護宣言なし',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象外',
}

NOISE_STATE_LABELS = {
    'noise_verified': '騒音検証済み',
    'noise_target_failed': '騒音目標未達',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '対象外',
}

INFRA_OVERALL_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'not_qualified': '不適格',
    'over_capacity': '容量超過',
    'insufficient_evidence': '証拠不足',
    'design_only': '設計段階のみ',
}


# ---------------------------------------------------------------------------
# Rack enclosure (#587 §1)
# ---------------------------------------------------------------------------


class CoolingDevice(BaseModel):
    """One declared cooling/airflow device mounted on a rack."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    label: str = Field(min_length=1)
    kind: CoolingDeviceKind
    airflow_cfm: float | None = Field(default=None, gt=0.0)
    """Declared airflow in cubic feet per minute; ``None`` = not
    evidenced — a fan's existence alone is never a capacity figure."""
    control: FanControlMode = 'unknown'
    location: VentPathKind = 'unknown'
    evidence_note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'CoolingDevice':
        if self.airflow_cfm is not None:
            _finite(self.airflow_cfm, 'airflow_cfm')
        if self.airflow_cfm is not None and (
            self.kind not in ACTIVE_COOLING_KINDS
        ):
            raise ValueError(
                'airflow_cfm is only meaningful on active cooling '
                'devices — passive paths carry no numeric CFM claim'
            )
        return self


class RackEnclosure(BaseModel):
    """Exact physical rack/cabinet declaration (#587 §1).

    Identity, capacity, clearance, ventilation paths, cooling devices
    and serviceability are recorded as *declared* evidence — the model
    never assumes all 19-inch racks share depth, airflow or load
    capacity.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-rack-1'
    ] = RACK_AUTHORITY_VERSION
    rack_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    location_ref: AuthorityRef | None = None
    """Optional pin to a room/AcousticRegion authority — remote-rack
    placement composes with #576 isolation instead of being assumed
    free of noise/thermal constraints (#587 §10)."""
    location_note: str | None = None
    rack_unit_capacity: int | None = Field(default=None, ge=1)
    usable_depth_mm: float | None = Field(default=None, gt=0.0)
    weight_capacity_kg: float | None = Field(default=None, gt=0.0)
    enclosure_kind: RackEnclosureKind = 'unknown'
    clearance_front_mm: float | None = Field(default=None, ge=0.0)
    clearance_rear_mm: float | None = Field(default=None, ge=0.0)
    clearance_side_mm: float | None = Field(default=None, ge=0.0)
    service_access: ServiceAccess = 'unknown'
    vent_intake: VentPathKind = 'unknown'
    vent_exhaust: VentPathKind = 'unknown'
    cooling_devices: tuple[CoolingDevice, ...] = ()
    blanking_panels: AirflowManagement = 'unknown'
    cable_entry: CableEntryZone = 'unknown'
    service_notes: str | None = None
    design_ambient_c: float | None = None
    """Declared design ambient temperature at the rack intake — used by
    the first-order calculation; ``None`` = ambient not bounded."""
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    rack_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'rack_id', 'rack_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RackEnclosure':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for field, label in (
            (self.usable_depth_mm, 'usable_depth_mm'),
            (self.weight_capacity_kg, 'weight_capacity_kg'),
            (self.clearance_front_mm, 'clearance_front_mm'),
            (self.clearance_rear_mm, 'clearance_rear_mm'),
            (self.clearance_side_mm, 'clearance_side_mm'),
            (self.design_ambient_c, 'design_ambient_c'),
        ):
            if field is not None:
                _finite(field, label)
        digest = _digest(self.identity_payload())
        if self.rack_sha256 != digest:
            raise ValueError('rack enclosure hash mismatch')
        if self.rack_id != _semantic_id('rack', digest):
            raise ValueError('rack enclosure id mismatch')
        return self


# ---------------------------------------------------------------------------
# Installed device thermal/electrical evidence (#587 §2)
# ---------------------------------------------------------------------------


class InstalledRackDevice(BaseModel):
    """One installed device's thermal/electrical evidence.

    ``subject_ref`` pins the equipment-instance authority when one
    exists; ``circuit_ref`` names the declared branch circuit that
    feeds the device. Every figure carries its evidence class and
    provenance — a nameplate maximum is never a fabricated heat load.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-device-1'
    ] = RACK_DEVICE_AUTHORITY_VERSION
    device_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    rack_ref: AuthorityRef
    subject_ref: AuthorityRef | None = None
    position_label: str | None = None
    """Rack position, e.g. 'RU 12-13' — free text; nothing is inferred
    from it."""
    rack_units: int | None = Field(default=None, ge=1)
    role: DeviceRole = 'unknown'
    circuit_ref: str | None = None
    """Declared branch-circuit identity feeding the device."""
    power_idle_w: float | None = Field(default=None, ge=0.0)
    power_nominal_w: float | None = Field(default=None, ge=0.0)
    power_max_w: float | None = Field(default=None, ge=0.0)
    inrush_w: float | None = Field(default=None, ge=0.0)
    """Startup/inrush draw where known (#587 §4)."""
    heat_dissipation_w: float | None = Field(default=None, ge=0.0)
    """Manufacturer-declared dissipation — only meaningful with
    ``heat_evidence_class == 'manufacturer_thermal_data'``."""
    heat_evidence_class: HeatEvidenceClass = 'unknown'
    ambient_limit_c: float | None = None
    """Maximum allowed intake/ambient temperature."""
    thermal_protection: ThermalProtectionState = 'unknown'
    fan_behavior: Literal[
        'fanless', 'variable_speed', 'fixed_speed', 'unknown'
    ] = 'unknown'
    required_clearance_note: str | None = None
    installation_guidance_ref: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    device_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'device_id', 'device_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'InstalledRackDevice':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.rack_ref.kind != 'rack_enclosure':
            raise ValueError(
                "rack_ref must pin a 'rack_enclosure' authority"
            )
        powers = (
            self.power_idle_w, self.power_nominal_w, self.power_max_w,
            self.inrush_w, self.heat_dissipation_w,
            self.ambient_limit_c,
        )
        for value, label in zip(
            powers,
            ('power_idle_w', 'power_nominal_w', 'power_max_w',
             'inrush_w', 'heat_dissipation_w', 'ambient_limit_c'),
        ):
            if value is not None:
                _finite(value, label)
        if (
            self.power_idle_w is not None
            and self.power_nominal_w is not None
            and self.power_idle_w > self.power_nominal_w
        ):
            raise ValueError('power_idle_w exceeds power_nominal_w')
        if (
            self.power_nominal_w is not None
            and self.power_max_w is not None
            and self.power_nominal_w > self.power_max_w
        ):
            raise ValueError('power_nominal_w exceeds power_max_w')
        if self.heat_evidence_class == 'manufacturer_thermal_data':
            if self.heat_dissipation_w is None:
                raise ValueError(
                    "'manufacturer_thermal_data' requires a declared "
                    'heat_dissipation_w figure'
                )
            if not self.provenance:
                raise ValueError(
                    "'manufacturer_thermal_data' requires provenance — "
                    'a dissipation figure without its source is not '
                    'manufacturer evidence'
                )
        if self.heat_evidence_class == 'derived_from_input_power':
            if not any(
                p is not None
                for p in (
                    self.power_idle_w, self.power_nominal_w,
                    self.power_max_w,
                )
            ):
                raise ValueError(
                    "'derived_from_input_power' requires at least one "
                    'declared input power figure to derive from'
                )
        if self.heat_dissipation_w is not None and not self.provenance:
            raise ValueError(
                'a declared heat_dissipation_w requires provenance — '
                'no free-floating thermal figures'
            )
        digest = _digest(self.identity_payload())
        if self.device_sha256 != digest:
            raise ValueError('installed device hash mismatch')
        if self.device_id != _semantic_id('rdev', digest):
            raise ValueError('installed device id mismatch')
        return self


# ---------------------------------------------------------------------------
# Electrical supply (#587 §4/§5)
# ---------------------------------------------------------------------------


class BranchCircuit(BaseModel):
    """Declared branch circuit / supply feeding rack devices (#587 §4).

    The continuous-load policy is declared per circuit — HTDT evaluates
    the declared capacity and never invents a universal electrical-code
    threshold.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-circuit-1'
    ] = CIRCUIT_AUTHORITY_VERSION
    circuit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    nominal_voltage_v: float | None = Field(default=None, gt=0.0)
    frequency_hz: float | None = Field(default=None, gt=0.0)
    breaker_rating_a: float | None = Field(default=None, gt=0.0)
    continuous_load_policy: ContinuousLoadPolicy = 'unknown'
    policy_note: str | None = None
    pdu_ref: str | None = None
    conditioner_ref: str | None = None
    ups_ref: str | None = None
    """PowerProtectionDevice identity supplying this circuit, when
    declared — the *capability* stays on the device record."""
    measured_voltage_v: float | None = Field(default=None, gt=0.0)
    measured_current_a: float | None = Field(default=None, ge=0.0)
    measured_power_w: float | None = Field(default=None, ge=0.0)
    measured_at_utc: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    circuit_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'circuit_id', 'circuit_sha256'}
        )

    def usable_capacity_w(self) -> float | None:
        """Effective continuous capacity under the declared policy."""
        if self.breaker_rating_a is None or self.nominal_voltage_v is None:
            return None
        apparent = self.breaker_rating_a * self.nominal_voltage_v
        if self.continuous_load_policy == 'continuous_derated':
            return apparent * 0.8
        if self.continuous_load_policy in ('nameplate_only', 'other_declared'):
            return apparent
        return None  # unknown policy -> cannot bound capacity

    @model_validator(mode='after')
    def _check(self) -> 'BranchCircuit':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if self.measured_at_utc is not None:
            _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        digest = _digest(self.identity_payload())
        if self.circuit_sha256 != digest:
            raise ValueError('branch circuit hash mismatch')
        if self.circuit_id != _semantic_id('ckt', digest):
            raise ValueError('branch circuit id mismatch')
        return self


class PowerProtectionDevice(BaseModel):
    """Declared power-protection path device (#587 §5).

    Capabilities are a set, not a label: a marketed 'power conditioner'
    without ``ups_backup`` never substitutes for a UPS, and runtime is
    evidence with its own service history.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-protection-1'
    ] = PROTECTION_AUTHORITY_VERSION
    protection_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    capabilities: tuple[ProtectionCapability, ...]
    va_rating: float | None = Field(default=None, gt=0.0)
    watt_rating: float | None = Field(default=None, gt=0.0)
    runtime_minutes_at_load: float | None = Field(default=None, gt=0.0)
    """Declared/observed runtime at the assigned load."""
    battery_last_test_at_utc: str | None = None
    last_service_at_utc: str | None = None
    observed_state: ProtectionObservedState = 'unknown'
    notes: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    protection_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'protection_id', 'protection_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'PowerProtectionDevice':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for stamp, label in (
            (self.battery_last_test_at_utc, 'battery_last_test_at_utc'),
            (self.last_service_at_utc, 'last_service_at_utc'),
        ):
            if stamp is not None:
                _require_iso8601(stamp, label)
        if not self.capabilities:
            raise ValueError(
                'a protection device must declare at least one '
                'capability — a marketed label alone is not a spec'
            )
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError('duplicate protection capabilities')
        if 'ups_backup' in self.capabilities and (
            self.va_rating is None and self.watt_rating is None
        ):
            raise ValueError(
                "'ups_backup' capability requires a VA or watt rating — "
                'an unrated UPS claim is not a capability'
            )
        digest = _digest(self.identity_payload())
        if self.protection_sha256 != digest:
            raise ValueError('protection device hash mismatch')
        if self.protection_id != _semantic_id('prot', digest):
            raise ValueError('protection device id mismatch')
        return self


class PoEPortAllocation(BaseModel):
    """Per-port PoE power allocation (#587 §11)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    port_label: str = Field(min_length=1)
    endpoint_ref: AuthorityRef | None = None
    allocated_w: float | None = Field(default=None, ge=0.0)
    requested_w: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'PoEPortAllocation':
        for value in (self.allocated_w, self.requested_w):
            if value is not None:
                _finite(value, 'PoE port watts')
        return self


class PoEBudget(BaseModel):
    """Declared PoE power budget for one PSE/switch (#587 §11).

    Aggregate and per-port power stay explicit — endpoint support is
    never claimed from Ethernet data compatibility alone (CEDIA 2026
    guidance: explicit total-power budgeting plus headroom).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-poe-1'
    ] = POE_AUTHORITY_VERSION
    poe_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    pse_ref: AuthorityRef | None = None
    standard: PoEStandard = 'unknown'
    port_power_limit_w: float | None = Field(default=None, gt=0.0)
    aggregate_budget_w: float | None = Field(default=None, gt=0.0)
    ports: tuple[PoEPortAllocation, ...] = ()
    cable_note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    poe_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'poe_id', 'poe_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'PoEBudget':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        ceiling = _POE_PORT_CEILING_W.get(self.standard)
        if (
            ceiling is not None
            and self.port_power_limit_w is not None
            and self.port_power_limit_w > ceiling
        ):
            raise ValueError(
                f'port_power_limit_w {self.port_power_limit_w} exceeds '
                f'the {self.standard} PSE ceiling ({ceiling} W) — a '
                'port cannot supply more than its standard allows'
            )
        labels = [p.port_label for p in self.ports]
        if len(set(labels)) != len(labels):
            raise ValueError('duplicate PoE port labels')
        digest = _digest(self.identity_payload())
        if self.poe_sha256 != digest:
            raise ValueError('PoE budget hash mismatch')
        if self.poe_id != _semantic_id('poe', digest):
            raise ValueError('PoE budget id mismatch')
        return self

    def port_ceiling_w(self) -> float | None:
        """Effective per-port ceiling (declared limit ∩ standard)."""
        standard = _POE_PORT_CEILING_W.get(self.standard)
        if self.port_power_limit_w is None:
            return standard
        if standard is None:
            return self.port_power_limit_w
        return min(standard, self.port_power_limit_w)


# ---------------------------------------------------------------------------
# Operating scenarios (#587 §3/§4)
# ---------------------------------------------------------------------------


class ScenarioDeviceLoad(BaseModel):
    """One device's load inside an operating scenario."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_ref: str = Field(min_length=1)
    """InstalledRackDevice identity."""
    load_w: float | None = Field(default=None, ge=0.0)
    load_source: ScenarioLoadSource = 'unknown'

    @model_validator(mode='after')
    def _check(self) -> 'ScenarioDeviceLoad':
        if self.load_w is not None:
            _finite(self.load_w, 'load_w')
        if self.load_w is None and self.load_source != 'unknown':
            raise ValueError(
                'a None load_w is only honest with '
                "load_source 'unknown'"
            )
        if self.load_w is not None and self.load_source == 'unknown':
            raise ValueError(
                'a numeric load_w requires its source — '
                "'unknown' cannot carry a figure"
            )
        return self


class InfrastructureScenario(BaseModel):
    """Declared operating scenario for thermal/electrical qualification.

    ``required_runtime_minutes`` is the UPS autonomy requirement for
    this scenario when a ride-through claim is made; ``duration``
    semantics stay declared (a stress scenario means sustained load).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-scenario-1'
    ] = SCENARIO_AUTHORITY_VERSION
    scenario_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: InfraScenarioKind
    name: str = Field(min_length=1)
    device_loads: tuple[ScenarioDeviceLoad, ...]
    program_material_ref: str | None = None
    """Optional pin to a #224 stress-profile authority."""
    required_runtime_minutes: float | None = Field(default=None, gt=0.0)
    notes: str | None = None
    declared_at_utc: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'scenario_id', 'scenario_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'InfrastructureScenario':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        if not self.device_loads:
            raise ValueError(
                'a scenario without device loads carries no '
                'thermal/electrical meaning'
            )
        refs = [d.device_ref for d in self.device_loads]
        if len(set(refs)) != len(refs):
            raise ValueError('duplicate device_ref in scenario loads')
        digest = _digest(self.identity_payload())
        if self.scenario_sha256 != digest:
            raise ValueError('scenario hash mismatch')
        if self.scenario_id != _semantic_id('scen', digest):
            raise ValueError('scenario id mismatch')
        return self


# ---------------------------------------------------------------------------
# Thermal measurement evidence (#587 §6/§7/§14)
# ---------------------------------------------------------------------------


class TemperatureReading(BaseModel):
    """One measured temperature point."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    point: TemperaturePoint
    temp_c: float
    instrument_note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'TemperatureReading':
        _finite(self.temp_c, 'temp_c')
        return self


class FanObservation(BaseModel):
    """Observed fan/cooling state during a measurement (#587 §9)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_label: str = Field(min_length=1)
    speed_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    rpm: float | None = Field(default=None, ge=0.0)
    noise_note: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'FanObservation':
        for value in (self.speed_pct, self.rpm):
            if value is not None:
                _finite(value, 'fan observation value')
        return self


class RackThermalMeasurement(BaseModel):
    """Measured rack/power state under a declared scenario (#587 §7).

    ``warm_up_complete`` is the measurer's declaration that temperature
    stabilized; ``still_rising`` records the observed trend. A
    measurement that did not reach steady state is evidence of *not
    qualified*, never of 'close enough'.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-thermal-1'
    ] = THERMAL_MEASUREMENT_AUTHORITY_VERSION
    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    rack_ref: AuthorityRef
    scenario_ref: AuthorityRef | None = None
    readings: tuple[TemperatureReading, ...]
    measured_power_w: float | None = Field(default=None, ge=0.0)
    duration_s: int = Field(gt=0)
    warm_up_complete: bool = False
    still_rising: bool | None = None
    fan_observations: tuple[FanObservation, ...] = ()
    noise_evidence_refs: tuple[AuthorityRef, ...] = ()
    """Pins into the #580 background-noise authority when the cooling
    system's room-noise contribution was measured (#587 §9) — the rack
    record references, never duplicates, that evidence."""
    noise_verdict: Literal['pass', 'fail', 'unmeasured'] | None = None
    instrument_ref: str | None = None
    calibration_note: str | None = None
    measured_at_utc: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'measurement_id', 'measurement_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RackThermalMeasurement':
        _require_iso8601(self.measured_at_utc, 'measured_at_utc')
        if self.rack_ref.kind != 'rack_enclosure':
            raise ValueError(
                "rack_ref must pin a 'rack_enclosure' authority"
            )
        if self.scenario_ref is not None and (
            self.scenario_ref.kind != 'infrastructure_scenario'
        ):
            raise ValueError(
                "scenario_ref must pin an 'infrastructure_scenario' "
                'authority'
            )
        if not self.readings:
            raise ValueError(
                'a thermal measurement requires at least one '
                'temperature reading'
            )
        if self.noise_verdict == 'fail' and not self.noise_evidence_refs:
            raise ValueError(
                "a 'fail' noise verdict requires bound #580 evidence "
                'refs'
            )
        digest = _digest(self.identity_payload())
        if self.measurement_sha256 != digest:
            raise ValueError('thermal measurement hash mismatch')
        if self.measurement_id != _semantic_id('rmeas', digest):
            raise ValueError('thermal measurement id mismatch')
        return self

    def steady_state_ok(self) -> bool:
        """True only when the run reached stabilized temperature."""
        return self.warm_up_complete and self.still_rising is not True

    def max_point_c(self, *points: TemperaturePoint) -> float | None:
        values = [
            r.temp_c for r in self.readings if r.point in points
        ]
        return max(values) if values else None

    def ambient_c(self) -> float | None:
        values = [
            r.temp_c for r in self.readings if r.point == 'ambient_room'
        ]
        return max(values) if values else None


# ---------------------------------------------------------------------------
# Qualification verdict (#587 §15)
# ---------------------------------------------------------------------------


class InfrastructureQualification(BaseModel):
    """Sealed per-scenario infrastructure verdict.

    Produced only by :func:`evaluate_infrastructure`. Dimension states
    are kept separate — no hidden overall rack score (#587 §9/§15).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INFRASTRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'rev56-infra-qualification-1'
    ] = INFRA_QUALIFICATION_AUTHORITY_VERSION
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scenario_ref: AuthorityRef
    evaluation_version: str = Field(min_length=1)
    thermal_state: ThermalCapabilityState
    electrical_state: ElectricalCapabilityState
    protection_state: ProtectionState
    noise_state: CoolingNoiseState
    serviceability_state: ServiceabilityState
    overall_state: InfrastructureOverallState
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'qualification_id', 'qualification_sha256'},
        )

    @model_validator(mode='after')
    def _check(self) -> 'InfrastructureQualification':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        if self.scenario_ref.kind != 'infrastructure_scenario':
            raise ValueError(
                "scenario_ref must pin an 'infrastructure_scenario' "
                'authority'
            )
        if self.overall_state == 'qualified' and (
            self.thermal_state == 'insufficient_evidence'
            or self.electrical_state == 'insufficient_evidence'
        ):
            raise ValueError(
                "a 'qualified' verdict cannot carry an unassessed "
                'thermal or electrical dimension'
            )
        digest = _digest(self.identity_payload())
        if self.qualification_sha256 != digest:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('rqual', digest):
            raise ValueError('qualification id mismatch')
        return self


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_sealed(model: type[BaseModel], sha_field: str,
                  id_field: str, prefix: str,
                  authority_version: str,
                  **kwargs: Any) -> Any:
    probe = model.model_construct(
        **_canon(
            model,
            dict(
                schema_version=INFRASTRUCTURE_SCHEMA_VERSION,
                authority_version=authority_version,
                **{sha_field: '0' * 64, id_field: ''},
                **kwargs,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={sha_field, id_field}),
        **{sha_field: sha, id_field: _semantic_id(prefix, sha)},
    )


def build_rack(**kwargs: Any) -> RackEnclosure:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        RackEnclosure, 'rack_sha256', 'rack_id', 'rack',
        RACK_AUTHORITY_VERSION, **kwargs,
    )


def build_rack_device(**kwargs: Any) -> InstalledRackDevice:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        InstalledRackDevice, 'device_sha256', 'device_id', 'rdev',
        RACK_DEVICE_AUTHORITY_VERSION, **kwargs,
    )


def build_circuit(**kwargs: Any) -> BranchCircuit:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        BranchCircuit, 'circuit_sha256', 'circuit_id', 'ckt',
        CIRCUIT_AUTHORITY_VERSION, **kwargs,
    )


def build_protection_device(**kwargs: Any) -> PowerProtectionDevice:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        PowerProtectionDevice, 'protection_sha256', 'protection_id',
        'prot', PROTECTION_AUTHORITY_VERSION, **kwargs,
    )


def build_poe_budget(**kwargs: Any) -> PoEBudget:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        PoEBudget, 'poe_sha256', 'poe_id', 'poe',
        POE_AUTHORITY_VERSION, **kwargs,
    )


def build_scenario(**kwargs: Any) -> InfrastructureScenario:
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _build_sealed(
        InfrastructureScenario, 'scenario_sha256', 'scenario_id',
        'scen', SCENARIO_AUTHORITY_VERSION, **kwargs,
    )


def build_thermal_measurement(**kwargs: Any) -> RackThermalMeasurement:
    return _build_sealed(
        RackThermalMeasurement, 'measurement_sha256',
        'measurement_id', 'rmeas',
        THERMAL_MEASUREMENT_AUTHORITY_VERSION, **kwargs,
    )


# ---------------------------------------------------------------------------
# Evaluation (#587 §4/§6/§7/§9/§11/§15)
# ---------------------------------------------------------------------------


def _device_load_w(
    device: InstalledRackDevice, declared: float | None,
) -> float | None:
    """Scenario load for one device, honestly fall back down the
    declared power ladder when the scenario carries no figure."""
    if declared is not None:
        return declared
    for candidate in (
        device.power_max_w, device.power_nominal_w, device.power_idle_w,
    ):
        if candidate is not None:
            return candidate
    return None


def _device_heat_w(
    device: InstalledRackDevice, load_w: float | None,
) -> float | None:
    """Best honest heat figure for a device under the scenario load."""
    if device.heat_evidence_class == 'manufacturer_thermal_data' and (
        device.heat_dissipation_w is not None
    ):
        # Manufacturer dissipation is declared at its stated operating
        # point; when the scenario load is lower than max, scale the
        # dissipation by the load/max ratio rather than pretending a
        # precise operating-point figure.
        if load_w is not None and device.power_max_w:
            return min(
                device.heat_dissipation_w,
                device.heat_dissipation_w * (load_w / device.power_max_w)
                if load_w < device.power_max_w
                else device.heat_dissipation_w,
            )
        return device.heat_dissipation_w
    if load_w is not None:
        return load_w  # derived_from_input_power: all watts become heat
    return None


def _required_cfm(heat_w: float, allowed_rise_c: float) -> float:
    heat_btu_h = heat_w * WATTS_TO_BTU_PER_HOUR
    allowed_rise_f = allowed_rise_c * 9.0 / 5.0
    return heat_btu_h / (SENSIBLE_HEAT_CONSTANT * allowed_rise_f)


def _evaluate_thermal(
    scenario: InfrastructureScenario,
    devices: Sequence[InstalledRackDevice],
    racks: Sequence[RackEnclosure],
    measurements: Sequence[RackThermalMeasurement],
    reasons: list[str],
    limitations: list[str],
) -> ThermalCapabilityState:
    loads: dict[str, float | None] = {}
    unknown_devices: list[str] = []
    for entry in scenario.device_loads:
        device = next(
            (d for d in devices if d.device_id == entry.device_ref), None
        )
        if device is None:
            unknown_devices.append(entry.device_ref)
            continue
        loads[device.device_id] = _device_load_w(device, entry.load_w)
        if loads[device.device_id] is None:
            unknown_devices.append(device.device_id)
    if unknown_devices:
        limitations.append(
            'load unknown for devices: ' + ', '.join(unknown_devices)
        )
    device_by_id = {d.device_id: d for d in devices}
    total_heat_w = 0.0
    heat_unbounded = False
    for device_id, load in loads.items():
        heat = _device_heat_w(device_by_id[device_id], load)
        if heat is None:
            heat_unbounded = True
        else:
            total_heat_w += heat
    used_racks = {
        d.rack_ref.ref_id
        for d in devices
        if d.device_id in loads
    }
    scenario_measurements = [
        m for m in measurements
        if m.scenario_ref is not None
        and m.scenario_ref.ref_id == scenario.scenario_id
        and m.rack_ref.ref_id in used_racks
    ]
    ambient_limits = [
        d.ambient_limit_c
        for d in devices
        if d.device_id in loads and d.ambient_limit_c is not None
    ]
    if scenario_measurements:
        steady = [m for m in scenario_measurements if m.steady_state_ok()]
        if steady:
            worst = max(
                m.max_point_c(
                    'rack_internal', 'device_exhaust', 'rack_exhaust',
                    'rack_inlet',
                ) or -1e9
                for m in steady
            )
            if ambient_limits and worst > min(ambient_limits):
                reasons.append(
                    'measured sustained temperature '
                    f'{worst:.1f} C exceeds device limit '
                    f'{min(ambient_limits):.1f} C'
                )
                return 'over_capacity'
            reasons.append(
                'sustained steady-state measurement within device '
                'ambient limits'
            )
            return 'thermally_measured'
        reasons.append(
            'thermal measurement exists but never reached steady '
            'state — a cold/short check cannot qualify a sustained '
            'scenario'
        )
        return 'insufficient_evidence'
    active_cfm = 0.0
    any_active_declared = False
    passive_only = True
    rack_ambients: list[float] = []
    for rack in racks:
        if rack.rack_id not in used_racks:
            continue
        if rack.design_ambient_c is not None:
            rack_ambients.append(rack.design_ambient_c)
        for dev in rack.cooling_devices:
            if dev.kind in ACTIVE_COOLING_KINDS:
                passive_only = False
                any_active_declared = True
                if dev.airflow_cfm is not None:
                    active_cfm += dev.airflow_cfm
        if rack.enclosure_kind == 'open_frame':
            passive_only = True
    if heat_unbounded:
        limitations.append(
            'heat load cannot be bounded for all devices — no '
            'manufacturer dissipation and no input power to derive '
            'from'
        )
        return 'insufficient_evidence'
    if not ambient_limits:
        limitations.append(
            'no device ambient-temperature limit declared — thermal '
            'margin cannot be bounded'
        )
        return 'insufficient_evidence'
    ambient_c = max(rack_ambients) if rack_ambients else None
    if ambient_c is None:
        limitations.append(
            'no design ambient temperature declared — first-order '
            'ventilation estimate cannot bound the rise'
        )
        if any_active_declared or passive_only:
            return 'manufacturer_guidance_only'
        return 'insufficient_evidence'
    allowed_rise_c = min(ambient_limits) - ambient_c
    if allowed_rise_c <= 0:
        reasons.append(
            f'declared ambient {ambient_c:.1f} C already exceeds the '
            f'lowest device limit {min(ambient_limits):.1f} C'
        )
        return 'over_capacity'
    if any_active_declared and active_cfm <= 0:
        limitations.append(
            'active cooling declared without airflow evidence — no '
            'numeric capacity can be claimed'
        )
        return 'manufacturer_guidance_only'
    if any_active_declared:
        required = _required_cfm(total_heat_w, allowed_rise_c)
        if active_cfm < required:
            reasons.append(
                f'first-order airflow {active_cfm:.0f} CFM below '
                f'required {required:.0f} CFM for '
                f'{total_heat_w:.0f} W at {allowed_rise_c:.1f} C rise'
            )
            return 'over_capacity'
        reasons.append(
            'first-order heat balance within declared active airflow '
            'and device ambient limits'
        )
        return 'calculated_with_limitations'
    if passive_only:
        limitations.append(
            'passive ventilation only — no measured margin can be '
            'claimed from a first-order balance'
        )
        return 'manufacturer_guidance_only'
    return 'insufficient_evidence'


def _evaluate_electrical(
    scenario: InfrastructureScenario,
    devices: Sequence[InstalledRackDevice],
    circuits: Sequence[BranchCircuit],
    reasons: list[str],
    limitations: list[str],
) -> ElectricalCapabilityState:
    device_by_id = {d.device_id: d for d in devices}
    per_circuit: dict[str, float] = {}
    unassigned: list[str] = []
    measured_any = False
    for entry in scenario.device_loads:
        device = device_by_id.get(entry.device_ref)
        if device is None:
            continue
        load = _device_load_w(device, entry.load_w)
        if load is None:
            continue
        if entry.load_source == 'measured':
            measured_any = True
        if device.circuit_ref is None:
            unassigned.append(device.device_id)
            continue
        per_circuit[device.circuit_ref] = (
            per_circuit.get(device.circuit_ref, 0.0) + load
        )
    if unassigned:
        limitations.append(
            'devices without a declared circuit: '
            + ', '.join(sorted(unassigned))
        )
    if not per_circuit:
        return 'insufficient_evidence'
    saw_bounded = False
    saw_unbounded = False
    for circuit_ref, load_w in per_circuit.items():
        circuit = next(
            (c for c in circuits if c.circuit_id == circuit_ref), None
        )
        if circuit is None:
            saw_unbounded = True
            limitations.append(
                f'circuit {circuit_ref} not declared — capacity unknown'
            )
            continue
        usable = circuit.usable_capacity_w()
        if usable is None:
            saw_unbounded = True
            limitations.append(
                f'circuit {circuit_ref} lacks a bounded capacity '
                '(ratings/policy undeclared)'
            )
            continue
        saw_bounded = True
        if load_w > usable:
            reasons.append(
                f'scenario load {load_w:.0f} W exceeds usable '
                f'capacity {usable:.0f} W on circuit {circuit_ref}'
            )
            return 'over_capacity'
    if saw_bounded and not saw_unbounded:
        if measured_any or any(
            c.measured_power_w is not None for c in circuits
        ):
            reasons.append(
                'circuit loads bounded by declared policy and supported '
                'by measured figures'
            )
            return 'power_measured'
        reasons.append(
            'circuit loads within declared usable capacity'
        )
        return 'calculated_with_limitations'
    if saw_bounded:
        reasons.append(
            'some circuits bounded, others lack declared capacity'
        )
        return 'calculated_with_limitations'
    return 'insufficient_evidence'


def _evaluate_protection(
    scenario: InfrastructureScenario,
    devices: Sequence[InstalledRackDevice],
    circuits: Sequence[BranchCircuit],
    protections: Sequence[PowerProtectionDevice],
    reasons: list[str],
    limitations: list[str],
) -> ProtectionState:
    ups_refs = {
        c.ups_ref for c in circuits if c.ups_ref is not None
    }
    if not ups_refs:
        if any(p.capabilities for p in protections):
            return 'protection_declared'
        if not protections:
            return 'none_declared'
        return 'protection_declared'
    device_by_id = {d.device_id: d for d in devices}
    result: ProtectionState | None = None
    for ups_ref in sorted(ups_refs):
        device = next(
            (p for p in protections if p.protection_id == ups_ref), None
        )
        if device is None:
            limitations.append(
                f'UPS device {ups_ref} referenced but not declared'
            )
            result = result or 'insufficient_evidence'
            continue
        if 'ups_backup' not in device.capabilities:
            limitations.append(
                f'device {ups_ref} is referenced as UPS but declares '
                'no ups_backup capability — a power conditioner is '
                'not a UPS'
            )
            result = result or 'insufficient_evidence'
            continue
        if device.observed_state in ('bypassed', 'failed'):
            reasons.append(
                f'UPS {ups_ref} observed state is '
                f'{device.observed_state}'
            )
            return 'ups_insufficient'
        load_w = 0.0
        load_known = True
        for circuit in circuits:
            if circuit.ups_ref != ups_ref:
                continue
            for entry in scenario.device_loads:
                dev = device_by_id.get(entry.device_ref)
                if dev is None or dev.circuit_ref != circuit.circuit_id:
                    continue
                load = _device_load_w(dev, entry.load_w)
                if load is None:
                    load_known = False
                else:
                    load_w += load
        if device.watt_rating is not None:
            if load_known and load_w > device.watt_rating:
                reasons.append(
                    f'UPS {ups_ref} load {load_w:.0f} W exceeds '
                    f'watt rating {device.watt_rating:.0f} W'
                )
                return 'ups_insufficient'
        elif device.va_rating is not None:
            limitations.append(
                f'UPS {ups_ref} rated in VA only — real-power margin '
                'cannot be verified'
            )
            result = result or 'insufficient_evidence'
            continue
        if (
            scenario.required_runtime_minutes is not None
            and device.runtime_minutes_at_load is not None
            and device.runtime_minutes_at_load
            < scenario.required_runtime_minutes
        ):
            reasons.append(
                f'UPS {ups_ref} runtime '
                f'{device.runtime_minutes_at_load:.0f} min below '
                f'required {scenario.required_runtime_minutes:.0f} min'
            )
            return 'ups_insufficient'
        if not load_known:
            limitations.append(
                'UPS load cannot be fully bounded — some device '
                'loads unknown'
            )
            result = result or 'insufficient_evidence'
            continue
        result = 'ups_verified'
    return result or 'insufficient_evidence'


def _evaluate_noise(
    racks: Sequence[RackEnclosure],
    devices: Sequence[InstalledRackDevice],
    measurements: Sequence[RackThermalMeasurement],
    reasons: list[str],
) -> CoolingNoiseState:
    used_racks = {d.rack_ref.ref_id for d in devices}
    has_active = any(
        dev.kind in ACTIVE_COOLING_KINDS
        for r in racks
        if r.rack_id in used_racks
        for dev in r.cooling_devices
    )
    if not has_active:
        return 'not_applicable'
    verdicts = [m.noise_verdict for m in measurements if m.noise_verdict]
    if any(v == 'fail' for v in verdicts):
        reasons.append(
            'cooling noise violates the declared room-noise target — '
            'a rack is not qualified by temperature alone (#587 §9)'
        )
        return 'noise_target_failed'
    if any(v == 'pass' for v in verdicts):
        return 'noise_verified'
    return 'insufficient_evidence'


def _evaluate_serviceability(
    racks: Sequence[RackEnclosure],
    devices: Sequence[InstalledRackDevice],
) -> ServiceabilityState:
    used = [r for r in racks if r.rack_id in {d.rack_ref.ref_id for d in devices}]
    if not used:
        return 'unknown'
    states = []
    for rack in used:
        access_ok = rack.service_access in ('front', 'rear', 'front_and_rear')
        clearances = (
            rack.clearance_front_mm, rack.clearance_rear_mm,
        )
        clearance_ok = any(c is not None and c > 0 for c in clearances)
        if access_ok and clearance_ok:
            states.append('serviceable')
        elif access_ok or clearance_ok or rack.service_access == 'side':
            states.append('limited')
        else:
            states.append('unknown')
    if all(s == 'serviceable' for s in states):
        return 'serviceable'
    if all(s == 'unknown' for s in states):
        return 'unknown'
    return 'limited'


def evaluate_infrastructure(
    *,
    scenario: InfrastructureScenario,
    racks: Sequence[RackEnclosure],
    devices: Sequence[InstalledRackDevice],
    circuits: Sequence[BranchCircuit],
    protections: Sequence[PowerProtectionDevice] = (),
    measurements: Sequence[RackThermalMeasurement] = (),
    evaluated_at_utc: str,
) -> InfrastructureQualification:
    """Fail-closed per-scenario infrastructure verdict (#587 §15).

    Dimension states are evaluated independently; the overall state
    is the honest aggregate — never a hidden weighted score.
    """
    reasons: list[str] = []
    limitations: list[str] = []
    in_devices = [
        d for d in devices
        if any(e.device_ref == d.device_id for e in scenario.device_loads)
    ]
    thermal = _evaluate_thermal(
        scenario, in_devices, racks, measurements, reasons, limitations,
    )
    electrical = _evaluate_electrical(
        scenario, in_devices, circuits, reasons, limitations,
    )
    protection = _evaluate_protection(
        scenario, in_devices, circuits, protections, reasons, limitations,
    )
    noise = _evaluate_noise(
        racks, in_devices,
        [
            m for m in measurements
            if m.scenario_ref is not None
            and m.scenario_ref.ref_id == scenario.scenario_id
        ],
        reasons,
    )
    serviceability = _evaluate_serviceability(racks, in_devices)

    if thermal == 'over_capacity' or electrical == 'over_capacity':
        overall = 'over_capacity'
    elif noise == 'noise_target_failed' or protection == 'ups_insufficient':
        overall = 'not_qualified'
    elif (
        thermal == 'insufficient_evidence'
        and electrical == 'insufficient_evidence'
    ):
        overall = 'insufficient_evidence'
    elif thermal in ('insufficient_evidence', 'manufacturer_guidance_only') and (
        electrical == 'insufficient_evidence'
    ):
        overall = 'design_only'
    elif (
        thermal in ('thermally_measured', 'calculated_with_limitations')
        and electrical in ('power_measured', 'calculated_with_limitations')
        and noise in ('noise_verified', 'not_applicable')
        and protection
        in ('ups_verified', 'protection_declared', 'none_declared')
    ):
        overall = 'qualified'
    else:
        overall = 'qualified_with_limitations'
        if thermal == 'manufacturer_guidance_only':
            limitations.append(
                'thermal margin rests on guidance/passive airflow only — '
                'no measurement or first-order capacity claim'
            )
        if noise == 'insufficient_evidence':
            limitations.append(
                'active cooling exists but room-noise contribution '
                'was never measured'
            )
        if protection == 'insufficient_evidence':
            limitations.append(
                'UPS capability could not be verified against the '
                'assigned load'
            )

    probe = InfrastructureQualification.model_construct(
        **_canon(
            InfrastructureQualification,
            dict(
                schema_version=INFRASTRUCTURE_SCHEMA_VERSION,
                authority_version=INFRA_QUALIFICATION_AUTHORITY_VERSION,
                qualification_id='',
                document_id=scenario.document_id,
                scenario_ref=AuthorityRef(
                    kind='infrastructure_scenario',
                    ref_id=scenario.scenario_id,
                    ref_sha256=scenario.scenario_sha256,
                ),
                evaluation_version=INFRA_EVALUATION_VERSION,
                thermal_state=thermal,
                electrical_state=electrical,
                protection_state=protection,
                noise_state=noise,
                serviceability_state=serviceability,
                overall_state=overall,
                reasons=tuple(reasons),
                limitations=tuple(limitations),
                evidence_refs=tuple(
                    m.measurement_id
                    for m in measurements
                    if m.scenario_ref is not None
                    and m.scenario_ref.ref_id == scenario.scenario_id
                ),
                evaluated_at_utc=evaluated_at_utc,
                qualification_sha256='0' * 64,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return InfrastructureQualification(
        **probe.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'},
        ),
        qualification_id=_semantic_id('rqual', sha),
        qualification_sha256=sha,
    )
