"""Operating power & thermal telemetry authority (#1049 / PWT10).

The rack power profile (#562) records what a device is *planned* to draw
in each named state — manufacturer declarations and derived figures. This
module records what the installed system *actually did*: measured power,
energy and temperature observations plus power events, kept strictly
separate from planning data.

Fail-closed contract:

- an :class:`OperatingPowerObservation` binds an exact device/branch, an
  exact observed operating state (``disconnected`` through
  ``vendor_defined``), the instrument that saw it, and the measurement
  interval — a measured value is never merged into the planned profile;
- device-reported values (e.g. a UPS's own runtime estimate) stay
  ``instrument_source='device_reported'`` — they are not recalculated or
  promoted to metered readings;
- a :class:`ThermalObservation` binds an exact sensor and *location
  semantics* (ambient/intake/exhaust/internal surface/etc.) — a bare
  "temperature" number is uninterpretable without knowing where;
- :class:`PowerEventObservation` records outage/reboot/undervoltage/UPS
  transfer/PDU trip events with observed evidence — never a diagnosis;
- raw series stay raw: a telemetry *session* is an immutable bundle of
  observations; derived summaries are separate records that reference
  the session by hash;
- local-only by default — no credentials, tokens, or remote endpoints in
  any bundle.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


POWER_TELEMETRY_AUTHORITY_VERSION = 'power-telemetry-1'

#: Observed operating state — finer-grained than the planned profile's
#: named states, and may include vendor-specific names via
#: ``vendor_defined`` + ``state_label``.
ObservedOperatingState = Literal[
    'disconnected',
    'soft_off',
    'standby',
    'networked_standby',
    'idle',
    'active',
    'playback',
    'high_load',
    'vendor_defined',
    'unknown',
]

#: How the observation was obtained.
TelemetryInstrumentSource = Literal[
    'inline_meter',
    'pdu_reported',
    'ups_reported',
    'device_reported',
    'manual_reading',
    'derived',
    'unknown',
]

#: Where a thermal sensor's reading physically applies.
ThermalSensorLocation = Literal[
    'room_ambient',
    'rack_intake',
    'rack_exhaust',
    'device_intake',
    'device_exhaust',
    'device_internal',
    'surface',
    'custom',
    'unknown',
]

#: Observed power-system events — evidence records, not diagnoses.
PowerEventKind = Literal[
    'outage',
    'reboot',
    'undervoltage',
    'overvoltage',
    'ups_transfer',
    'pdu_trip',
    'vendor_defined',
    'unknown',
]






def _finite(value: object, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class OperatingPowerObservation(BaseModel):
    """One measured power datum on an installed device or PDU branch.

    ``interval_s`` is the sample/integration window; ``energy_wh`` is a
    cumulative meter reading when the instrument reports one. Values
    beyond what the instrument measured stay ``None`` — never derived in
    place.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    #: The equipment entity or PDU branch being observed.
    subject_id: str = Field(min_length=1)
    subject_kind: Literal['equipment', 'pdu_branch', 'ups', 'custom'] = (
        'equipment'
    )
    observed_state: ObservedOperatingState = 'unknown'
    state_label: str | None = Field(default=None, min_length=1)
    instrument_id: str = Field(min_length=1)
    instrument_source: TelemetryInstrumentSource = 'unknown'
    observed_at: str = Field(min_length=1)
    interval_s: float | None = Field(default=None, gt=0.0)
    voltage_v: float | None = Field(default=None, ge=0.0)
    current_a: float | None = Field(default=None, ge=0.0)
    real_power_w: float | None = Field(default=None, ge=0.0)
    apparent_power_va: float | None = Field(default=None, ge=0.0)
    power_factor: float | None = Field(default=None, ge=0.0, le=1.0)
    energy_wh: float | None = Field(default=None, ge=0.0)
    #: Battery evidence when the instrument (e.g. a UPS via NUT) reports
    #: it — absent instruments keep ``None``.
    battery_charge_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    battery_runtime_s: float | None = Field(default=None, ge=0.0)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_observation(self) -> 'OperatingPowerObservation':
        for name in (
            'interval_s', 'voltage_v', 'current_a', 'real_power_w',
            'apparent_power_va', 'power_factor', 'energy_wh',
            'battery_charge_pct', 'battery_runtime_s',
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(value, field_name=name)
        if self.observed_state == 'vendor_defined' and not self.state_label:
            raise ValueError(
                'vendor_defined observed state requires a state_label'
            )
        if (
            self.power_factor is not None
            and self.real_power_w is not None
            and self.apparent_power_va is not None
            and self.apparent_power_va > 0.0
        ):
            implied = self.real_power_w / self.apparent_power_va
            if abs(implied - self.power_factor) > 0.02:
                raise ValueError(
                    'power_factor inconsistent with W/VA — record the '
                    'instrument reading, not a patched value'
                )
        return self


class ThermalObservation(BaseModel):
    """One measured temperature with exact sensor/location semantics."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sensor_id: str = Field(min_length=1)
    location: ThermalSensorLocation = 'unknown'
    location_label: str | None = Field(default=None, min_length=1)
    subject_id: str | None = Field(default=None, min_length=1)
    observed_at: str = Field(min_length=1)
    temperature_c: float
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_thermal(self) -> 'ThermalObservation':
        _finite(self.temperature_c, field_name='temperature_c')
        if self.temperature_c < -60.0 or self.temperature_c > 150.0:
            raise ValueError('temperature_c outside a plausible range')
        if self.location == 'custom' and not self.location_label:
            raise ValueError('custom thermal location requires a label')
        return self


class PowerEventObservation(BaseModel):
    """One observed power-system event — evidence, not a diagnosis."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    kind: PowerEventKind = 'unknown'
    event_label: str | None = Field(default=None, min_length=1)
    observed_at: str = Field(min_length=1)
    detail: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_event(self) -> 'PowerEventObservation':
        if self.kind == 'vendor_defined' and not self.event_label:
            raise ValueError(
                'vendor_defined power event requires an event_label'
            )
        return self


class PowerThermalTelemetrySession(BaseModel):
    """An immutable bundle of raw observations from one capture session.

    The session's ``semantic_sha256`` is what derived summaries reference —
    raw series are never rewritten in place.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'power-telemetry-1'
    ] = POWER_TELEMETRY_AUTHORITY_VERSION
    session_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    power_observations: tuple[OperatingPowerObservation, ...] = ()
    thermal_observations: tuple[ThermalObservation, ...] = ()
    power_events: tuple[PowerEventObservation, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_session(self) -> 'PowerThermalTelemetrySession':
        session_ids = (
            [o.session_id for o in self.power_observations]
            + [o.session_id for o in self.thermal_observations]
            + [e.session_id for e in self.power_events]
        )
        foreign = [sid for sid in session_ids if sid != self.session_id]
        if foreign:
            raise ValueError(
                'observation session_ids must match the bundle session'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('telemetry session semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'session_id': self.session_id,
            'version': self.version,
            'power_observations': [
                o.model_dump(mode='json')
                for o in self.power_observations
            ],
            'thermal_observations': [
                o.model_dump(mode='json')
                for o in self.thermal_observations
            ],
            'power_events': [
                e.model_dump(mode='json') for e in self.power_events
            ],
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


def build_power_thermal_telemetry_session(
    *,
    session_id: str | None = None,
    version: str = '1',
    power_observations: tuple[OperatingPowerObservation, ...] = (),
    thermal_observations: tuple[ThermalObservation, ...] = (),
    power_events: tuple[PowerEventObservation, ...] = (),
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> PowerThermalTelemetrySession:
    payload: dict[str, Any] = {
        'authority_version': POWER_TELEMETRY_AUTHORITY_VERSION,
        'session_id': session_id or str(uuid4()),
        'version': version,
        'power_observations': power_observations,
        'thermal_observations': thermal_observations,
        'power_events': power_events,
        'provenance': provenance,
    }
    provisional = PowerThermalTelemetrySession.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return PowerThermalTelemetrySession(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class TelemetryCheck(BaseModel):
    """One readiness check on a telemetry session."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str


class TelemetryReadinessReport(BaseModel):
    """Readiness report for a telemetry session — per-aspect checks so a
    session with observed-but-incomplete data stays UNKNOWN, never
    silently upgraded to a planning-grade record."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1)
    session_id: str
    session_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[TelemetryCheck, ...]
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_report(self) -> 'TelemetryReadinessReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('telemetry report hash mismatch')
        expected = 'ptr-' + self.report_sha256[:24]
        if self.report_id != expected:
            raise ValueError('telemetry report id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'authority_version': POWER_TELEMETRY_AUTHORITY_VERSION,
            'session_id': self.session_id,
            'session_sha256': self.session_sha256,
            'checks': [
                check.model_dump(mode='json') for check in self.checks
            ],
        }


def evaluate_telemetry_readiness(
    *, session: PowerThermalTelemetrySession
) -> TelemetryReadinessReport:
    """Report the readiness of a captured telemetry session.

    - ``observations_present`` — the bundle carries at least one
      observation;
    - ``states_recorded`` — every power observation carries a declared
      observed state (``unknown`` flags the check UNKNOWN);
    - ``instruments_recorded`` — every observation names an instrument
      and a source kind; device-reported values stay device-reported;
    - ``thermal_locations_bound`` — every thermal observation carries a
      declared location;
    - ``distinct_from_planning`` — the session is evidence, not a planned
      profile: reported only as PASS informational (observed data never
      merges into #562 planning records).
    """

    checks: list[TelemetryCheck] = []
    total = (
        len(session.power_observations)
        + len(session.thermal_observations)
        + len(session.power_events)
    )
    checks.append(
        TelemetryCheck(
            check='observations_present',
            status='PASS' if total else 'UNKNOWN',
            reason=(
                f'{total} observations recorded'
                if total
                else 'empty telemetry session'
            ),
        )
    )

    unknown_states = [
        o.observation_id
        for o in session.power_observations
        if o.observed_state == 'unknown'
    ]
    checks.append(
        TelemetryCheck(
            check='states_recorded',
            status='PASS' if not unknown_states else 'UNKNOWN',
            reason=(
                'all power observations carry a declared state'
                if not unknown_states
                else 'observations with unknown state: '
                + ', '.join(unknown_states)
            ),
        )
    )

    unknown_instruments = [
        o.observation_id
        for o in session.power_observations
        if o.instrument_source == 'unknown'
    ]
    checks.append(
        TelemetryCheck(
            check='instruments_recorded',
            status='PASS' if not unknown_instruments else 'UNKNOWN',
            reason=(
                'all power observations name an instrument source'
                if not unknown_instruments
                else 'observations without instrument source: '
                + ', '.join(unknown_instruments)
            ),
        )
    )

    unknown_locations = [
        o.observation_id
        for o in session.thermal_observations
        if o.location == 'unknown'
    ]
    checks.append(
        TelemetryCheck(
            check='thermal_locations_bound',
            status='PASS' if not unknown_locations else 'UNKNOWN',
            reason=(
                'all thermal observations bind a location'
                if not unknown_locations
                else 'thermal observations without location: '
                + ', '.join(unknown_locations)
            ),
        )
    )

    checks.append(
        TelemetryCheck(
            check='distinct_from_planning',
            status='PASS',
            reason='observed telemetry is kept separate from the '
            'planned power profile — never merged, never promoted',
        )
    )

    probe = TelemetryReadinessReport.model_construct(
        report_id='',
        session_id=session.session_id,
        session_sha256=session.semantic_sha256,
        checks=tuple(checks),
        report_sha256='',
    )
    digest = _hash(probe.identity_payload())
    return TelemetryReadinessReport(
        **probe.model_dump(
            mode='python',
            exclude={'report_sha256', 'report_id'},
        ),
        report_id='ptr-' + digest[:24],
        report_sha256=digest,
    )


__all__ = [
    'ObservedOperatingState',
    'OperatingPowerObservation',
    'POWER_TELEMETRY_AUTHORITY_VERSION',
    'PowerEventKind',
    'PowerEventObservation',
    'PowerThermalTelemetrySession',
    'TelemetryCheck',
    'TelemetryInstrumentSource',
    'TelemetryReadinessReport',
    'ThermalObservation',
    'ThermalSensorLocation',
    'build_power_thermal_telemetry_session',
    'evaluate_telemetry_readiness',
]
