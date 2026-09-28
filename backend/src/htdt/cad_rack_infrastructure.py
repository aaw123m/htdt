"""Equipment rack / electrical-load / thermal planning authority (#562).

HTDT plans physical equipment geometry, playback-chain headroom (#477),
cable runs (#538) and installation output, but had no first-class record for
the rack infrastructure questions an installer actually asks: will the
devices fit, what is the documented load, what heat is expected, which
outlet feeds what. This module is that bounded planning authority — it is
inventory + arithmetic + documented device requirements, never electrical
or HVAC engineering.

Contract properties:

- a :class:`RackDefinition` is manufacturer/authority evidence: envelope,
  optional RU capacity, usable depth, declared clearances and cabinet
  ventilation state; the IEC 60297 family can be cited as interface
  provenance but never infers depth, load capacity or cooling;
- :class:`EquipmentPowerProfile` keeps input power (per named operating
  state, each value pinned as manufacturer-declared / user-measured /
  explicitly-derived) strictly separate from amplifier acoustic output
  capability and from heat dissipation;
- :func:`evaluate_rack_fit` reports per-device PASS/FAIL/UNKNOWN checks for
  RU occupancy, depth and declared clearances only — it never claims
  airflow adequacy from free space; the layout must be bound to the exact
  rack being evaluated, RU overlap marks every participant (never just the
  later placement), and a clearance axis whose rack-side value is
  undeclared stays ``UNKNOWN`` rather than being promoted to ``PASS``;
- ``RackDefinition.service_clearance_m`` is recorded documentation only:
  no device-level service requirement exists yet, so it never contributes
  to ``clearance_fit``;
- :func:`summarize_load` aggregates only data compatible with the scenario
  state and keeps unknown device contributions explicit — a missing device
  is never 0 W;
- circuit/PDU assignments are abstract endpoints with arithmetic summaries;
  no breaker sizing or code-compliance verdict is produced; the assignment
  graph itself is validated — an assignment to an unknown endpoint or
  device, a duplicate endpoint id, or a device feeding multiple endpoints
  is a topology error, distinct from a valid device whose power datum is
  simply missing;
- :func:`summarize_heat` sums only documented heat dissipation and keeps
  unknown devices visible; a watts→heat conversion is allowed only under an
  explicit recorded assumption and stays labelled derived.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest, canonicalize_payload


RACK_SCHEMA_VERSION = 1
RACK_AUTHORITY_VERSION = 'rack-infrastructure-1'


#: Named operating states on a device power profile. ``rated_max`` is the
#: manufacturer's maximum/rated input — not a continuous playback load, and
#: never silently reused as one.
PowerState = Literal[
    'standby_off',
    'standby_passthrough',
    'networked_standby',
    'idle',
    'typical',
    'rated_max',
    'custom',
]

#: How a power/heat value was obtained. Marketing amplifier *output* wattage
#: is never an input-power or heat datum.
PowerValueKind = Literal['manufacturer_declared', 'user_measured', 'derived']

FitStatus = Literal['PASS', 'FAIL', 'UNKNOWN']






def _finite(value: object, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise ValueError(f'{field_name} must be a finite number')
    return float(value)


class RackDefinition(BaseModel):
    """One rack/cabinet as documented — not a structural certification."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = RACK_SCHEMA_VERSION
    rack_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    width_m: float = Field(gt=0.0)
    height_m: float = Field(gt=0.0)
    #: Usable internal depth — the actual constraint, not the nominal
    #: 19-inch interface family width.
    usable_depth_m: float = Field(gt=0.0)
    ru_capacity: int | None = Field(default=None, gt=0)
    front_clearance_m: float | None = Field(default=None, ge=0.0)
    rear_clearance_m: float | None = Field(default=None, ge=0.0)
    service_clearance_m: float | None = Field(default=None, ge=0.0)
    cabinet_state: Literal['open', 'closed', 'unknown'] = 'unknown'
    #: Optional mechanical-interface citation (for example an IEC 60297
    #: family reference); interface compatibility alone proves nothing about
    #: depth, load or cooling.
    mechanical_standard_ref: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_rack(self) -> 'RackDefinition':
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('RackDefinition semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'rack_id': self.rack_id,
            'name': self.name,
            'width_m': self.width_m,
            'height_m': self.height_m,
            'usable_depth_m': self.usable_depth_m,
            'ru_capacity': self.ru_capacity,
            'front_clearance_m': self.front_clearance_m,
            'rear_clearance_m': self.rear_clearance_m,
            'service_clearance_m': self.service_clearance_m,
            'cabinet_state': self.cabinet_state,
            'mechanical_standard_ref': self.mechanical_standard_ref,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'note': self.note,
        }


def build_rack_definition(**kwargs: Any) -> RackDefinition:
    provisional = RackDefinition.model_construct(**canonicalize_payload(RackDefinition, dict(
        **kwargs, semantic_sha256='0' * 64
    )))
    return RackDefinition(
        **kwargs,
        semantic_sha256=_digest(provisional.semantic_payload()),
    )


class PowerValue(BaseModel):
    """One input-power datum for one operating state, with exact provenance."""

    model_config = ConfigDict(frozen=True)

    state: PowerState
    power_w: float = Field(ge=0.0)
    kind: PowerValueKind
    #: Required when ``kind == 'derived'``: the bounded assumption under
    #: which the number was obtained (for example a measured-input
    #: conversion policy). Declared/measured values may record the document
    #: or meter context here.
    derivation: str | None = Field(default=None, min_length=1)
    state_label: str | None = Field(default=None, min_length=1)
    provenance: EquipmentDataProvenance

    @model_validator(mode='after')
    def valid_value(self) -> 'PowerValue':
        if self.kind == 'derived' and self.derivation is None:
            raise ValueError('derived power values require the derivation record')
        if self.state == 'custom' and not self.state_label:
            raise ValueError('custom power state requires a state_label')
        return self


class EquipmentPowerProfile(BaseModel):
    """Input-power/heat record for one device — never acoustic capability."""

    model_config = ConfigDict(frozen=True)

    device_id: str = Field(min_length=1)
    #: Optional catalog identity — stays a ref, never copies the definition.
    equipment_definition_id: str | None = Field(default=None, min_length=1)
    power_values: tuple[PowerValue, ...] = ()
    heat_dissipation_w: float | None = Field(default=None, ge=0.0)
    heat_provenance: EquipmentDataProvenance | None = None
    ru_height: int | None = Field(default=None, gt=0)
    chassis_depth_m: float | None = Field(default=None, gt=0.0)
    requires_front_clearance_m: float | None = Field(default=None, ge=0.0)
    requires_rear_clearance_m: float | None = Field(default=None, ge=0.0)
    power_connector: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_profile(self) -> 'EquipmentPowerProfile':
        if (self.heat_dissipation_w is None) != (self.heat_provenance is None):
            raise ValueError(
                'heat dissipation value and provenance must be supplied together'
            )
        states = [
            (item.state, item.state_label) for item in self.power_values
        ]
        if len(states) != len(set(states)):
            raise ValueError('duplicate power state entries')
        return self

    def power_for(self, state: PowerState, state_label: str | None = None) -> PowerValue | None:
        return next(
            (
                item
                for item in self.power_values
                if item.state == state and item.state_label == state_label
            ),
            None,
        )


class RackPlacement(BaseModel):
    """Where one device sits — a declared position, not a fit claim."""

    model_config = ConfigDict(frozen=True)

    device_id: str = Field(min_length=1)
    #: Bottom RU (1-based) when the rack uses unit rails.
    ru_position: int | None = Field(default=None, gt=0)
    shelf_label: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_placement(self) -> 'RackPlacement':
        if self.ru_position is None and self.shelf_label is None:
            raise ValueError('placement requires a ru_position or a shelf_label')
        return self


class RackLayout(BaseModel):
    model_config = ConfigDict(frozen=True)

    rack_id: str = Field(min_length=1)
    placements: tuple[RackPlacement, ...] = ()

    @model_validator(mode='after')
    def valid_layout(self) -> 'RackLayout':
        ids = [item.device_id for item in self.placements]
        if len(ids) != len(set(ids)):
            raise ValueError('rack layout device ids must be unique')
        return self


class DeviceFitResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    device_id: str = Field(min_length=1)
    ru_occupancy: FitStatus
    depth_fit: FitStatus
    clearance_fit: FitStatus
    conflicts: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def evaluate_rack_fit(
    layout: RackLayout,
    rack: RackDefinition,
    devices: Sequence[EquipmentPowerProfile],
) -> tuple[DeviceFitResult, ...]:
    """Bounded fit checks: RU occupancy, depth, declared clearances.

    The layout must be bound to the exact rack being evaluated — evaluating
    a Rack-A layout against Rack B is an authority-binding error, not an
    unknown-dimension case. Unknown dimensions stay UNKNOWN — nothing is
    inferred from the nominal rack family. RU overlap is physical and
    symmetric: every device whose declared span collides fails, independent
    of placement order. A declared device clearance requirement whose
    rack-side value is undeclared stays UNKNOWN — it is never promoted to
    PASS. No airflow-adequacy claim is produced.
    """

    if layout.rack_id != rack.rack_id:
        raise ValueError(
            'rack layout is bound to a different rack definition: '
            f'{layout.rack_id!r} != {rack.rack_id!r}'
        )

    by_id = {item.device_id: item for item in devices}

    # Resolve every declared RU span before evaluating so occupancy is
    # order-independent; all devices sharing an RU participate in the
    # conflict, not just the later placement.
    spans: dict[str, range] = {}
    for placement in layout.placements:
        device = by_id.get(placement.device_id)
        if (
            placement.ru_position is None
            or device is None
            or device.ru_height is None
        ):
            continue
        spans[placement.device_id] = range(
            placement.ru_position,
            placement.ru_position + device.ru_height,
        )
    occupancy: dict[int, set[str]] = {}
    for device_id, span in spans.items():
        for ru in span:
            occupancy.setdefault(ru, set()).add(device_id)

    results: list[DeviceFitResult] = []
    for placement in layout.placements:
        device = by_id.get(placement.device_id)
        reasons: list[str] = []
        conflicts: list[str] = []

        ru_status: FitStatus = 'UNKNOWN'
        if placement.ru_position is not None:
            span = spans.get(placement.device_id)
            if span is None:
                reasons.append('device RU height is not declared')
            else:
                clashes = sorted({
                    other
                    for ru in span
                    for other in occupancy.get(ru, set())
                    if other != placement.device_id
                })
                exceeds_capacity = (
                    rack.ru_capacity is not None
                    and span.stop - 1 > rack.ru_capacity
                )
                if exceeds_capacity:
                    ru_status = 'FAIL'
                    reasons.append('device exceeds declared rack RU capacity')
                if clashes:
                    ru_status = 'FAIL'
                    conflicts.extend(clashes)
                    reasons.append('device RU span overlaps another placement')
                if ru_status != 'FAIL':
                    if rack.ru_capacity is None:
                        reasons.append('rack RU capacity is not declared')
                    else:
                        ru_status = 'PASS'

        depth_status: FitStatus = 'UNKNOWN'
        if device is None or device.chassis_depth_m is None:
            reasons.append('device chassis depth is not declared')
        elif device.chassis_depth_m <= rack.usable_depth_m:
            depth_status = 'PASS'
        else:
            depth_status = 'FAIL'
            reasons.append('device depth exceeds rack usable depth')

        # Per-axis clearance semantics: a declared device requirement
        # evaluates PASS/FAIL/UNKNOWN against the rack's available value;
        # any FAIL dominates, then any UNKNOWN — a missing rack-side value
        # is never promoted to PASS.
        clearance_checks = (
            ('front', device.requires_front_clearance_m if device else None,
             rack.front_clearance_m),
            ('rear', device.requires_rear_clearance_m if device else None,
             rack.rear_clearance_m),
        )
        axis_statuses: list[FitStatus] = []
        for label, required, available in clearance_checks:
            if required is None:
                continue
            if available is None:
                axis_statuses.append('UNKNOWN')
                reasons.append(f'rack {label} clearance is not declared')
            elif required > available:
                axis_statuses.append('FAIL')
                reasons.append(
                    f'device {label} clearance requirement exceeds the rack'
                )
            else:
                axis_statuses.append('PASS')
        if 'FAIL' in axis_statuses:
            clearance_status: FitStatus = 'FAIL'
        elif 'UNKNOWN' in axis_statuses:
            clearance_status = 'UNKNOWN'
        elif axis_statuses:
            clearance_status = 'PASS'
        else:
            clearance_status = 'UNKNOWN'

        results.append(DeviceFitResult(
            device_id=placement.device_id,
            ru_occupancy=ru_status,
            depth_fit=depth_status,
            clearance_fit=clearance_status,
            conflicts=tuple(conflicts),
            reasons=tuple(reasons),
        ))
    return tuple(results)


class ElectricalScenario(BaseModel):
    """One explicit use scenario — standby, playback, documented maximum."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str = Field(min_length=1)
    state: PowerState
    state_label: str | None = Field(default=None, min_length=1)
    device_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_scenario(self) -> 'ElectricalScenario':
        if self.state == 'custom' and not self.state_label:
            raise ValueError('custom scenario requires a state_label')
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError('scenario device ids must be unique')
        return self


class LoadSummary(BaseModel):
    """Arithmetic load summary for one scenario — no compliance verdict."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str = Field(min_length=1)
    state: PowerState
    known_w: float = Field(ge=0.0)
    known_device_ids: tuple[str, ...] = ()
    #: Devices with no compatible datum — never silently counted as 0 W.
    unknown_device_ids: tuple[str, ...] = ()


def summarize_load(
    scenario: ElectricalScenario,
    devices: Sequence[EquipmentPowerProfile],
) -> LoadSummary:
    by_id = {item.device_id: item for item in devices}
    known_w = 0.0
    known: list[str] = []
    unknown: list[str] = []
    for device_id in scenario.device_ids:
        profile = by_id.get(device_id)
        value = (
            None
            if profile is None
            else profile.power_for(scenario.state, scenario.state_label)
        )
        if value is None:
            unknown.append(device_id)
            continue
        known_w += value.power_w
        known.append(device_id)
    return LoadSummary(
        scenario_id=scenario.scenario_id,
        state=scenario.state,
        known_w=known_w,
        known_device_ids=tuple(known),
        unknown_device_ids=tuple(unknown),
    )


class CircuitEndpoint(BaseModel):
    """Abstract circuit/outlet/PDU/UPS branch — topology metadata only."""

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(min_length=1)
    kind: Literal['circuit', 'outlet', 'pdu_branch', 'ups_branch', 'other']
    label: str = Field(min_length=1)
    #: A user-declared rating for arithmetic context; HTDT never derives or
    #: validates a code-compliant capacity from it.
    declared_rating_w: float | None = Field(default=None, gt=0.0)


class CircuitAssignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    device_id: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)


class EndpointLoadSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(min_length=1)
    known_w: float = Field(ge=0.0)
    known_device_ids: tuple[str, ...] = ()
    unknown_device_ids: tuple[str, ...] = ()
    declared_rating_w: float | None = None
    exceeds_declared_rating: bool | None = None


def summarize_endpoint_loads(
    assignments: Sequence[CircuitAssignment],
    endpoints: Sequence[CircuitEndpoint],
    devices: Sequence[EquipmentPowerProfile],
    *,
    state: PowerState = 'rated_max',
    state_label: str | None = None,
) -> tuple[EndpointLoadSummary, ...]:
    """Per-endpoint arithmetic summary of the declared rating state.

    The assignment graph is validated before arithmetic: an assignment to
    an unknown endpoint or device, a duplicate endpoint id, or a device
    assigned to multiple endpoints (multi-feed is not modeled) is a
    topology error — never silently dropped or double-counted. A valid
    assignment whose device lacks a matching power datum remains an
    ``unknown_device_ids`` evidence gap, distinct from invalid topology.

    ``exceeds_declared_rating`` is plain arithmetic against a user-declared
    number — it is not a code-compliance verdict.
    """

    endpoint_ids = [item.endpoint_id for item in endpoints]
    if len(endpoint_ids) != len(set(endpoint_ids)):
        raise ValueError('circuit endpoint ids must be unique')
    known_endpoints = set(endpoint_ids)
    known_devices = {item.device_id for item in devices}

    by_id = {item.device_id: item for item in devices}
    by_endpoint: dict[str, list[str]] = {}
    assigned_devices: set[str] = set()
    for assignment in assignments:
        if assignment.endpoint_id not in known_endpoints:
            raise ValueError(
                'circuit assignment references unknown endpoint '
                f'{assignment.endpoint_id!r}'
            )
        if assignment.device_id not in known_devices:
            raise ValueError(
                'circuit assignment references unknown device '
                f'{assignment.device_id!r}'
            )
        if assignment.device_id in assigned_devices:
            raise ValueError(
                f'device {assignment.device_id!r} is assigned to multiple '
                'endpoints'
            )
        assigned_devices.add(assignment.device_id)
        by_endpoint.setdefault(assignment.endpoint_id, []).append(
            assignment.device_id
        )
    summaries: list[EndpointLoadSummary] = []
    for endpoint in endpoints:
        known_w = 0.0
        known: list[str] = []
        unknown: list[str] = []
        for device_id in by_endpoint.get(endpoint.endpoint_id, []):
            profile = by_id.get(device_id)
            value = (
                None
                if profile is None
                else profile.power_for(state, state_label)
            )
            if value is None:
                unknown.append(device_id)
            else:
                known_w += value.power_w
                known.append(device_id)
        exceeds = (
            None
            if endpoint.declared_rating_w is None or unknown
            else known_w > endpoint.declared_rating_w
        )
        summaries.append(EndpointLoadSummary(
            endpoint_id=endpoint.endpoint_id,
            known_w=known_w,
            known_device_ids=tuple(known),
            unknown_device_ids=tuple(unknown),
            declared_rating_w=endpoint.declared_rating_w,
            exceeds_declared_rating=exceeds,
        ))
    return tuple(summaries)


class HeatSummary(BaseModel):
    """Summed *documented* heat — provenance retained, unknowns explicit."""

    model_config = ConfigDict(frozen=True)

    documented_heat_w: float = Field(ge=0.0)
    documented_device_ids: tuple[str, ...] = ()
    undocumented_device_ids: tuple[str, ...] = ()
    #: Optional watts→heat conversion, only when an explicit assumption was
    #: recorded; labelled derived, never an HVAC sizing result.
    derived_heat_w: float | None = Field(default=None, ge=0.0)
    derivation_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_summary(self) -> 'HeatSummary':
        if (self.derived_heat_w is None) != (self.derivation_note is None):
            raise ValueError(
                'derived heat and its assumption note must be supplied together'
            )
        return self


def summarize_heat(
    devices: Sequence[EquipmentPowerProfile],
    *,
    derived_from_state: PowerState | None = None,
    derivation_note: str | None = None,
) -> HeatSummary:
    documented_w = 0.0
    documented: list[str] = []
    undocumented: list[str] = []
    derived_w = 0.0
    derived_any = False
    for device in devices:
        if device.heat_dissipation_w is None:
            undocumented.append(device.device_id)
        else:
            documented_w += device.heat_dissipation_w
            documented.append(device.device_id)
        if derived_from_state is not None:
            value = device.power_for(derived_from_state)
            if value is not None:
                derived_w += value.power_w
                derived_any = True
    return HeatSummary(
        documented_heat_w=documented_w,
        documented_device_ids=tuple(documented),
        undocumented_device_ids=tuple(undocumented),
        derived_heat_w=(
            derived_w if derived_from_state is not None and derived_any else None
        ),
        derivation_note=derivation_note if derived_any else None,
    )


__all__ = [
    'RACK_AUTHORITY_VERSION',
    'RACK_SCHEMA_VERSION',
    'CircuitAssignment',
    'CircuitEndpoint',
    'DeviceFitResult',
    'ElectricalScenario',
    'EndpointLoadSummary',
    'EquipmentPowerProfile',
    'FitStatus',
    'HeatSummary',
    'LoadSummary',
    'PowerState',
    'PowerValue',
    'PowerValueKind',
    'RackDefinition',
    'RackLayout',
    'RackPlacement',
    'build_rack_definition',
    'evaluate_rack_fit',
    'summarize_endpoint_loads',
    'summarize_heat',
    'summarize_load',
]
