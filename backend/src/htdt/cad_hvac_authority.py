"""Theater HVAC acoustic/airflow co-design authority (#616, REV57-INST).

A theater HVAC path is a coupled airflow + acoustic + isolation system.
Qualifying only the acoustic side produces quiet rooms that cannot
ventilate or cool equipment; qualifying only airflow hides regenerated
noise, tonal fan content and duct-borne flanking. This module qualifies
the axes together:

- :class:`CadVentilationScenario` — the required operating state:
  occupancy, load contributions, required/target airflow, supply/return
  state, fan mode, zone/control state, temperature/RH targets. HTDT
  consumes required airflow values — it is not a mechanical code-design
  package.
- :class:`CadHvacPath` — the explicit path graph: source (fan / AHU /
  FCU / ERV) -> segments -> damper -> silencer -> terminal -> room,
  including adjacent-room cross-talk/flanking legs.
- :class:`CadHvacComponentEvidence` — per-component evidence bound to
  an exact operating condition: sound power / insertion loss /
  regenerated flow noise / pressure loss stay *separate* quantities,
  each with its own method and evidence class. ISO 7235 laboratory
  silencer data never reads as ISO 11820 in-situ installed truth.
- :class:`CadHvacFieldObservation` — commissioning evidence in a
  normal warmed/occupied operating state: terminal flow, room noise,
  fan/balancing state, tonal/regenerated flags, rattle routing to
  #589.
- :class:`CadHvacQualification` + :func:`evaluate_hvac_path` — the
  fail-closed joint verdict: airflow eligibility, acoustic
  contribution, silencer evidence class, flanking and tonality stay
  independent. A quiet candidate that fails required airflow is
  ineligible — never a consolation pass.

Honesty rules baked in:

- One broadband ``duct loss dB`` scalar is not a quantitative path —
  attenuation evidence keeps frequency-band detail or stays declared.
- Manufacturer terminal NC/NR application tables keep their derivation
  class — they never become raw measured room spectra.
- A VFD/fan-speed change is a new operating state; evidence does not
  scale proportionally without an explicit model domain.
- Mechanically excited grille/duct rattle is routed to #589 — airflow
  noise models never absorb a buzz finding.
- Breakout / break-in / structure-borne legs may be declared
  ``unsupported`` — they stay explicit instead of silently omitted.

Literature basis
----------------
- CEDIA/CTA-RP22 v1.2 section 8.4 — oversized ducts for low noise,
  sound attenuators against fan/adjacent-room transmission, tonal
  fan/motor noise, audible diffuser/grille rattle prevention.
- ISO 5135:2020 — air-terminal-device sound power under exact test
  method/operating condition (not a generic ``quiet diffuser`` label).
- ISO 7235:2003 — laboratory silencer insertion loss, flow/regenerated
  noise and pressure loss as separate quantities.
- ISO 11820:1996 — in-situ silencer measurements; results cannot simply
  be compared as equivalent to ISO 7235 laboratory data.
- ASHRAE Handbook sound/vibration chapters — source-path-receiver
  framing; handbook tables stay external copyrighted references.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


HVAC_SCHEMA_VERSION = 'hvac-codesign-1'
HVAC_EVALUATION_VERSION = 'hvac-codesign-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#616)
# ---------------------------------------------------------------------------

PathKind = Literal['supply', 'return', 'exhaust', 'cross_talk', 'other']

PathNodeKind = Literal[
    'source_fan',
    'ahu',
    'fcu',
    'erv',
    'duct_segment',
    'lined_duct',
    'bend',
    'branch',
    'damper',
    'silencer',
    'terminal',
    'grille',
    'diffuser',
    'room_boundary',
    'adjacent_room_boundary',
    'other',
    'unknown',
]
"""One node in the explicit HVAC path graph."""

PathLegKind = Literal[
    'duct_borne_internal',
    'breakout_duct_to_room',
    'break_in_room_to_duct',
    'structure_borne_mechanical',
    'cross_talk_duct',
    'unsupported',
    'unknown',
]
"""Propagation-leg classes. ``unsupported`` stays explicit — an
unmodelled path is never silently omitted from the graph."""

AirflowEvidenceClass = Literal[
    'design',
    'manufacturer_declared',
    'calculated',
    'field_measured',
]
"""DESIGN / MANUFACTURER_DECLARED / CALCULATED / FIELD_MEASURED stay
separate — a nominal diffuser size is not proof of actual flow."""

AcousticMethod = Literal[
    'iso_5135_terminal',
    'iso_7235_laboratory',
    'iso_11820_in_situ',
    'manufacturer_application_table',
    'engineering_model',
    'field_measurement',
    'other',
    'unknown',
]
"""Evidence method + revision. ISO 7235 laboratory insertion loss and
ISO 11820 in-situ results are different evidence classes — never
conflated."""

ComponentKind = Literal[
    'fan_source',
    'duct_element',
    'silencer',
    'terminal_device',
    'other',
]

OperatingStateKind = Literal[
    'hvac_off',
    'low_speed',
    'normal_occupied',
    'warmed_projector_load',
    'maximum_load',
    'balancing_test',
    'other',
    'unknown',
]
"""The operating state a piece of evidence was captured under. A
silent HVAC-off measurement never qualifies normal-operation noise."""

FlankingRole = Literal[
    'none',
    'suspected_path',
    'confirmed_path',
    'isolated_from_targets',
    'unknown',
]

RattleRouting = Literal[
    'none_observed',
    'rattle_suspected_route_to_589',
    'airborne_noise_only',
    'unknown',
]

BalancingState = Literal[
    'as_designed',
    'field_balanced',
    'rebalanced_since_qualification',
    'unknown',
]

AirflowEligibility = Literal[
    'eligible',
    'under_ventilated',
    'airflow_requirement_unbound',
    'insufficient_evidence',
    'not_evaluated',
]

PathAcousticState = Literal[
    'contributions_documented',
    'contributions_limited',
    'lab_evidence_not_installed_truth',
    'tonal_content_unresolved',
    'insufficient_evidence',
    'not_evaluated',
]

HvacFlankingState = Literal[
    'no_flanking_declared',
    'flanking_path_open',
    'flanking_path_mitigated',
    'flanking_unverified',
    'not_evaluated',
]

HvacVerdict = Literal[
    'qualified',
    'qualified_with_limitations',
    'ineligible_airflow',
    'failed',
    'insufficient_evidence',
]


# ---------------------------------------------------------------------------
# Ventilation scenario
# ---------------------------------------------------------------------------


class CadVentilationScenario(BaseModel):
    """The exact operating scenario a path is qualified against.

    HTDT consumes required airflow / load values from mechanical
    design or project requirements — it never invents code-compliance
    numbers. Undocumented fields stay None (UNKNOWN).
    """

    model_config = ConfigDict(frozen=True)

    scenario_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str | None = None
    occupancy: int | None = None
    sensible_load_w: float | None = None
    latent_load_w: float | None = None
    equipment_heat_w: float | None = None
    projector_heat_w: float | None = None
    required_supply_flow_lps: float | None = None
    required_return_flow_lps: float | None = None
    target_supply_flow_lps: float | None = None
    fan_mode: str | None = None
    fan_speed_state: str | None = None
    zone_control_state: str | None = None
    room_temp_target_c: float | None = None
    room_rh_target_pct: float | None = None
    outside_conditions: str | None = None
    operating_state: OperatingStateKind = 'unknown'
    requirement_source: str | None = None
    requirement_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    scenario_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_scenario(self) -> 'CadVentilationScenario':
        _require_iso8601(
            self.declared_at_utc, 'scenario declared_at_utc'
        )
        if self.requirement_ref is not None and (
            self.requirement_ref.ref_sha256 is None
        ):
            raise ValueError('requirement_ref must pin its sha256')
        if self.occupancy is not None and self.occupancy < 0:
            raise ValueError('occupancy must be non-negative')
        for label, value in (
            ('sensible_load_w', self.sensible_load_w),
            ('latent_load_w', self.latent_load_w),
            ('equipment_heat_w', self.equipment_heat_w),
            ('projector_heat_w', self.projector_heat_w),
            ('required_supply_flow_lps', self.required_supply_flow_lps),
            ('required_return_flow_lps', self.required_return_flow_lps),
            ('target_supply_flow_lps', self.target_supply_flow_lps),
            ('room_temp_target_c', self.room_temp_target_c),
            ('room_rh_target_pct', self.room_rh_target_pct),
        ):
            if value is not None:
                _require_finite(value, f'scenario {label}')
        if (
            self.required_supply_flow_lps is not None
            and self.required_supply_flow_lps < 0
        ):
            raise ValueError('required supply flow must be non-negative')
        expected = _hash(self.identity_payload())
        if self.scenario_sha256 != expected:
            raise ValueError('ventilation scenario hash mismatch')
        if self.scenario_id != _semantic_id('hvacscn', expected):
            raise ValueError('scenario id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'label': self.label,
            'occupancy': self.occupancy,
            'sensible_load_w': self.sensible_load_w,
            'latent_load_w': self.latent_load_w,
            'equipment_heat_w': self.equipment_heat_w,
            'projector_heat_w': self.projector_heat_w,
            'required_supply_flow_lps': self.required_supply_flow_lps,
            'required_return_flow_lps': self.required_return_flow_lps,
            'target_supply_flow_lps': self.target_supply_flow_lps,
            'fan_mode': self.fan_mode,
            'fan_speed_state': self.fan_speed_state,
            'zone_control_state': self.zone_control_state,
            'room_temp_target_c': self.room_temp_target_c,
            'room_rh_target_pct': self.room_rh_target_pct,
            'outside_conditions': self.outside_conditions,
            'operating_state': self.operating_state,
            'requirement_source': self.requirement_source,
            'requirement_ref': (
                self.requirement_ref.model_dump(mode='json')
                if self.requirement_ref is not None else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def required_flow_known(self) -> bool:
        return self.required_supply_flow_lps is not None


def ventilation_scenario_binding(
    scenario: CadVentilationScenario,
) -> AuthorityRef:
    return AuthorityRef(
        kind='hvac_ventilation_scenario',
        ref_id=scenario.scenario_id,
        ref_sha256=scenario.scenario_sha256,
    )


# ---------------------------------------------------------------------------
# HVAC path declaration
# ---------------------------------------------------------------------------


class CadHvacPathNode(BaseModel):
    """One node on the path graph."""

    model_config = ConfigDict(frozen=True)

    node_id: str = Field(min_length=1)
    kind: PathNodeKind = 'unknown'
    label: str | None = None
    room_ref: str | None = None
    geometry_ref: AuthorityRef | None = None
    component_evidence_id: str | None = None


class CadHvacPathLeg(BaseModel):
    """One propagation leg between two nodes.

    ``leg_kind`` separates duct-borne internal propagation from
    breakout / break-in / structure-borne / cross-talk legs — an
    unmodelled mechanism stays ``unsupported``, never omitted.
    """

    model_config = ConfigDict(frozen=True)

    leg_id: str = Field(min_length=1)
    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)
    leg_kind: PathLegKind = 'unknown'
    model_ref: str | None = None
    attenuation_band_db_json: str | None = None
    broadband_loss_db: float | None = None

    @model_validator(mode='after')
    def valid_leg(self) -> 'CadHvacPathLeg':
        if self.broadband_loss_db is not None:
            _require_finite(
                self.broadband_loss_db, 'leg broadband_loss_db'
            )
        if (
            self.broadband_loss_db is not None
            and self.attenuation_band_db_json is None
            and self.leg_kind not in ('unsupported', 'unknown')
        ):
            raise ValueError(
                'a single broadband duct-loss scalar is not a '
                'quantitative path — supply band detail or mark the '
                'leg unsupported'
            )
        return self


class CadHvacPath(BaseModel):
    """The explicit supply/return/cross-talk path under qualification.

    Nodes and legs form the graph; ``serves_room`` binds the acoustic
    region the path terminates into; ``flanking_role`` records whether
    the same duct network is also an open cross-talk route to adjacent
    rooms (#576 composition).
    """

    model_config = ConfigDict(frozen=True)

    path_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str | None = None
    path_kind: PathKind
    serves_room: str | None = None
    nodes: tuple[CadHvacPathNode, ...] = ()
    legs: tuple[CadHvacPathLeg, ...] = ()
    flanking_role: FlankingRole = 'unknown'
    flanking_target_rooms: tuple[str, ...] = ()
    isolation_scenario_ref: AuthorityRef | None = None
    scenario_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    path_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_path(self) -> 'CadHvacPath':
        _require_iso8601(self.declared_at_utc, 'path declared_at_utc')
        for ref, label in (
            (self.isolation_scenario_ref, 'isolation_scenario_ref'),
            (self.scenario_ref, 'scenario_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError('path node ids must be unique')
        known = set(node_ids)
        leg_ids = [leg.leg_id for leg in self.legs]
        if len(leg_ids) != len(set(leg_ids)):
            raise ValueError('path leg ids must be unique')
        for leg in self.legs:
            if leg.from_node not in known or leg.to_node not in known:
                raise ValueError(
                    f'leg {leg.leg_id} references an undeclared node'
                )
        if self.flanking_role in ('suspected_path', 'confirmed_path') and (
            not self.flanking_target_rooms
        ):
            raise ValueError(
                'a flanking path must name its target rooms'
            )
        expected = _hash(self.identity_payload())
        if self.path_sha256 != expected:
            raise ValueError('HVAC path hash mismatch')
        if self.path_id != _semantic_id('hvacpath', expected):
            raise ValueError('path id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'label': self.label,
            'path_kind': self.path_kind,
            'serves_room': self.serves_room,
            'nodes': [n.model_dump(mode='json') for n in self.nodes],
            'legs': [l.model_dump(mode='json') for l in self.legs],
            'flanking_role': self.flanking_role,
            'flanking_target_rooms': list(self.flanking_target_rooms),
            'isolation_scenario_ref': (
                self.isolation_scenario_ref.model_dump(mode='json')
                if self.isolation_scenario_ref is not None else None
            ),
            'scenario_ref': (
                self.scenario_ref.model_dump(mode='json')
                if self.scenario_ref is not None else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def hvac_path_binding(path: CadHvacPath) -> AuthorityRef:
    return AuthorityRef(
        kind='hvac_path',
        ref_id=path.path_id,
        ref_sha256=path.path_sha256,
    )


# ---------------------------------------------------------------------------
# Component evidence
# ---------------------------------------------------------------------------


class CadHvacComponentEvidence(BaseModel):
    """Acoustic/airflow evidence for one component under one exact
    operating condition.

    Sound power, insertion loss, regenerated flow noise and pressure
    loss are *separate* quantities — a silencer is never reduced to one
    number, and each quantity carries its own method and evidence
    class. ``method`` pins ISO 5135 terminal tests, ISO 7235 laboratory
    silencer data, ISO 11820 in-situ measurements, manufacturer
    application tables, engineering models or field measurements apart.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    component_kind: ComponentKind
    product_model: str | None = None
    method: AcousticMethod = 'unknown'
    method_revision: str | None = None
    airflow_evidence_class: AirflowEvidenceClass = 'design'
    flow_rate_lps: float | None = None
    velocity_mps: float | None = None
    static_pressure_pa: float | None = None
    fan_operating_point: str | None = None
    sound_power_band_db_json: str | None = None
    insertion_loss_band_db_json: str | None = None
    regenerated_noise_band_db_json: str | None = None
    pressure_loss_pa: float | None = None
    tonal_flags: tuple[str, ...] = ()
    test_condition: str | None = None
    source_document: str | None = None
    source_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    instrument_ref: AuthorityRef | None = None
    uncertainty_db: float | None = None
    measured_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_evidence(self) -> 'CadHvacComponentEvidence':
        _require_iso8601(
            self.declared_at_utc, 'evidence declared_at_utc'
        )
        if self.measured_at_utc is not None:
            _require_iso8601(
                self.measured_at_utc, 'evidence measured_at_utc'
            )
        for ref, label in ((self.instrument_ref, 'instrument_ref'),):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for label, value in (
            ('flow_rate_lps', self.flow_rate_lps),
            ('velocity_mps', self.velocity_mps),
            ('static_pressure_pa', self.static_pressure_pa),
            ('pressure_loss_pa', self.pressure_loss_pa),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'evidence {label}')
        if self.component_kind == 'silencer' and (
            self.insertion_loss_band_db_json is not None
            and self.regenerated_noise_band_db_json is None
            and self.pressure_loss_pa is None
        ):
            raise ValueError(
                'a silencer is never reduced to insertion loss alone — '
                'regenerated noise and pressure loss stay separate '
                'quantities (declare them or leave the evidence partial)'
            )
        if (
            self.flow_rate_lps is not None
            and self.airflow_evidence_class == 'design'
            and self.method in (
                'iso_11820_in_situ',
                'field_measurement',
            )
        ):
            raise ValueError(
                'in-situ / field-measured flow requires '
                'field_measured evidence class'
            )
        expected = _hash(self.identity_payload())
        if self.evidence_sha256 != expected:
            raise ValueError('HVAC component evidence hash mismatch')
        if self.evidence_id != _semantic_id('hvaccomp', expected):
            raise ValueError('evidence id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'component_kind': self.component_kind,
            'product_model': self.product_model,
            'method': self.method,
            'method_revision': self.method_revision,
            'airflow_evidence_class': self.airflow_evidence_class,
            'flow_rate_lps': self.flow_rate_lps,
            'velocity_mps': self.velocity_mps,
            'static_pressure_pa': self.static_pressure_pa,
            'fan_operating_point': self.fan_operating_point,
            'sound_power_band_db_json': self.sound_power_band_db_json,
            'insertion_loss_band_db_json': (
                self.insertion_loss_band_db_json
            ),
            'regenerated_noise_band_db_json': (
                self.regenerated_noise_band_db_json
            ),
            'pressure_loss_pa': self.pressure_loss_pa,
            'tonal_flags': list(self.tonal_flags),
            'test_condition': self.test_condition,
            'source_document': self.source_document,
            'source_sha256': self.source_sha256,
            'instrument_ref': (
                self.instrument_ref.model_dump(mode='json')
                if self.instrument_ref is not None else None
            ),
            'uncertainty_db': self.uncertainty_db,
            'measured_at_utc': self.measured_at_utc,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def hvac_component_binding(
    evidence: CadHvacComponentEvidence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='hvac_component_evidence',
        ref_id=evidence.evidence_id,
        ref_sha256=evidence.evidence_sha256,
    )


# ---------------------------------------------------------------------------
# Field observation / commissioning record
# ---------------------------------------------------------------------------


class CadHvacFieldObservation(BaseModel):
    """One commissioning observation in a real operating state.

    ``operating_state`` is part of identity: an ``hvac_off`` reading can
    never qualify normal-operation background noise. Rattle findings are
    routed to #589 via ``rattle_routing`` — they never fold into this
    authority's airborne-noise evidence.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    path_ref: AuthorityRef
    scenario_ref: AuthorityRef | None = None
    operating_state: OperatingStateKind = 'unknown'
    measured_supply_flow_lps: float | None = None
    measured_terminal_flow_lps: float | None = None
    fan_state: str | None = None
    room_noise_db: float | None = None
    room_noise_weighting: str | None = None
    room_noise_spectrum_json: str | None = None
    tonal_observed: bool | None = None
    rattle_routing: RattleRouting = 'unknown'
    balancing_state: BalancingState = 'unknown'
    instrument_ref: AuthorityRef | None = None
    measurement_position: str | None = None
    uncertainty_db: float | None = None
    measured_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadHvacFieldObservation':
        _require_iso8601(
            self.measured_at_utc, 'observation measured_at_utc'
        )
        if self.path_ref.ref_sha256 is None:
            raise ValueError('observations must pin the path sha256')
        for ref, label in (
            (self.scenario_ref, 'scenario_ref'),
            (self.instrument_ref, 'instrument_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for label, value in (
            ('measured_supply_flow_lps', self.measured_supply_flow_lps),
            ('measured_terminal_flow_lps', self.measured_terminal_flow_lps),
            ('room_noise_db', self.room_noise_db),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'observation {label}')
        if self.room_noise_db is not None and (
            self.room_noise_weighting is None
        ):
            raise ValueError(
                'a room-noise level requires its weighting — a bare '
                'dB figure is not a reproducible measurement'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('field observation hash mismatch')
        if self.observation_id != _semantic_id('hvacobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'path_ref': self.path_ref.model_dump(mode='json'),
            'scenario_ref': (
                self.scenario_ref.model_dump(mode='json')
                if self.scenario_ref is not None else None
            ),
            'operating_state': self.operating_state,
            'measured_supply_flow_lps': self.measured_supply_flow_lps,
            'measured_terminal_flow_lps': (
                self.measured_terminal_flow_lps
            ),
            'fan_state': self.fan_state,
            'room_noise_db': self.room_noise_db,
            'room_noise_weighting': self.room_noise_weighting,
            'room_noise_spectrum_json': self.room_noise_spectrum_json,
            'tonal_observed': self.tonal_observed,
            'rattle_routing': self.rattle_routing,
            'balancing_state': self.balancing_state,
            'instrument_ref': (
                self.instrument_ref.model_dump(mode='json')
                if self.instrument_ref is not None else None
            ),
            'measurement_position': self.measurement_position,
            'uncertainty_db': self.uncertainty_db,
            'measured_at_utc': self.measured_at_utc,
            'provenance_json': self.provenance_json,
        }


def hvac_observation_binding(
    observation: CadHvacFieldObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='hvac_field_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification verdict
# ---------------------------------------------------------------------------


class CadHvacQualification(BaseModel):
    """Sealed joint verdict — airflow + acoustic + flanking + tonality.

    The axes stay independent: there is no single HVAC score, and a
    pass on noise never upgrades an under-ventilated path.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    path_ref: AuthorityRef
    scenario_ref: AuthorityRef | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    observation_refs: tuple[AuthorityRef, ...] = ()
    airflow_eligibility: AirflowEligibility
    acoustic_state: PathAcousticState
    flanking_state: HvacFlankingState
    tonal_state: Literal[
        'no_tonal_flags', 'tonal_flags_present', 'not_evaluated'
    ]
    verdict: HvacVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadHvacQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.path_ref.ref_sha256 is None:
            raise ValueError('qualifications must pin the path sha256')
        for ref, label in (
            (self.scenario_ref, 'scenario_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for ref in self.evidence_refs:
            if ref.ref_sha256 is None:
                raise ValueError('evidence refs must pin their sha256')
        for ref in self.observation_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'observation refs must pin their sha256'
                )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('HVAC qualification hash mismatch')
        if self.qualification_id != _semantic_id('hvacqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'path_ref': self.path_ref.model_dump(mode='json'),
            'scenario_ref': (
                self.scenario_ref.model_dump(mode='json')
                if self.scenario_ref is not None else None
            ),
            'evidence_refs': [
                r.model_dump(mode='json') for r in self.evidence_refs
            ],
            'observation_refs': [
                r.model_dump(mode='json') for r in self.observation_refs
            ],
            'airflow_eligibility': self.airflow_eligibility,
            'acoustic_state': self.acoustic_state,
            'flanking_state': self.flanking_state,
            'tonal_state': self.tonal_state,
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def hvac_qualification_binding(
    qualification: CadHvacQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='hvac_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_ventilation_scenario(**kwargs: Any) -> CadVentilationScenario:
    """Seal one required ventilation/thermal scenario."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadVentilationScenario, dict(kwargs),
        'scenario_id', 'scenario_sha256', 'hvacscn',
    )


def build_hvac_path(
    *,
    document_id: str,
    path_kind: PathKind,
    label: str | None = None,
    serves_room: str | None = None,
    nodes: tuple[CadHvacPathNode, ...] = (),
    legs: tuple[CadHvacPathLeg, ...] = (),
    flanking_role: FlankingRole = 'unknown',
    flanking_target_rooms: tuple[str, ...] = (),
    isolation_scenario_ref: AuthorityRef | None = None,
    scenario: CadVentilationScenario | AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadHvacPath:
    """Seal one explicit HVAC acoustic/airflow path."""
    scenario_ref = (
        ventilation_scenario_binding(scenario)
        if isinstance(scenario, CadVentilationScenario)
        else scenario
    )
    payload = dict(
        document_id=document_id,
        label=label,
        path_kind=path_kind,
        serves_room=serves_room,
        nodes=nodes,
        legs=legs,
        flanking_role=flanking_role,
        flanking_target_rooms=flanking_target_rooms,
        isolation_scenario_ref=isolation_scenario_ref,
        scenario_ref=scenario_ref,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadHvacPath, payload,
        'path_id', 'path_sha256', 'hvacpath',
    )


def build_component_evidence(**kwargs: Any) -> CadHvacComponentEvidence:
    """Seal one component's acoustic/airflow evidence."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadHvacComponentEvidence, dict(kwargs),
        'evidence_id', 'evidence_sha256', 'hvaccomp',
    )


def build_field_observation(
    *,
    document_id: str,
    path: CadHvacPath | AuthorityRef,
    scenario: CadVentilationScenario | AuthorityRef | None = None,
    operating_state: OperatingStateKind = 'unknown',
    measured_supply_flow_lps: float | None = None,
    measured_terminal_flow_lps: float | None = None,
    fan_state: str | None = None,
    room_noise_db: float | None = None,
    room_noise_weighting: str | None = None,
    room_noise_spectrum_json: str | None = None,
    tonal_observed: bool | None = None,
    rattle_routing: RattleRouting = 'unknown',
    balancing_state: BalancingState = 'unknown',
    instrument_ref: AuthorityRef | None = None,
    measurement_position: str | None = None,
    uncertainty_db: float | None = None,
    measured_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadHvacFieldObservation:
    """Seal one commissioning observation."""
    path_ref = (
        hvac_path_binding(path)
        if isinstance(path, CadHvacPath)
        else path
    )
    scenario_ref = (
        ventilation_scenario_binding(scenario)
        if isinstance(scenario, CadVentilationScenario)
        else scenario
    )
    payload = dict(
        document_id=document_id,
        path_ref=path_ref,
        scenario_ref=scenario_ref,
        operating_state=operating_state,
        measured_supply_flow_lps=measured_supply_flow_lps,
        measured_terminal_flow_lps=measured_terminal_flow_lps,
        fan_state=fan_state,
        room_noise_db=room_noise_db,
        room_noise_weighting=room_noise_weighting,
        room_noise_spectrum_json=room_noise_spectrum_json,
        tonal_observed=tonal_observed,
        rattle_routing=rattle_routing,
        balancing_state=balancing_state,
        instrument_ref=instrument_ref,
        measurement_position=measurement_position,
        uncertainty_db=uncertainty_db,
        measured_at_utc=measured_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadHvacFieldObservation, payload,
        'observation_id', 'observation_sha256', 'hvacobs',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_hvac_path(
    *,
    document_id: str,
    path: CadHvacPath,
    scenario: CadVentilationScenario | None = None,
    evidence: tuple[CadHvacComponentEvidence, ...] = (),
    observations: tuple[CadHvacFieldObservation, ...] = (),
    evaluated_at_utc: str | None = None,
) -> CadHvacQualification:
    """Fail-closed joint HVAC verdict.

    Airflow eligibility, acoustic contribution, flanking and tonal
    content stay independent axes — a quieter path that cannot deliver
    required airflow is ``ineligible_airflow``, never ``qualified``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    if scenario is not None and path.scenario_ref is not None and (
        path.scenario_ref.ref_id != scenario.scenario_id
        or path.scenario_ref.ref_sha256 != scenario.scenario_sha256
    ):
        raise ValueError(
            'the supplied scenario does not match the path binding'
        )
    for obs in observations:
        if obs.path_ref.ref_id != path.path_id or (
            obs.path_ref.ref_sha256 != path.path_sha256
        ):
            raise ValueError(
                'field observation binds a different path'
            )

    component_ids = {
        node.component_evidence_id
        for node in path.nodes
        if node.component_evidence_id is not None
    }
    bound_evidence = tuple(
        e for e in evidence if e.evidence_id in component_ids
    )
    loose_evidence = tuple(
        e for e in evidence if e.evidence_id not in component_ids
    )
    if loose_evidence:
        reasons.append(
            'evidence not bound to a path node is ignored: '
            + ', '.join(e.evidence_id for e in loose_evidence)
        )

    # ---------------- airflow eligibility ----------------------------
    airflow: AirflowEligibility
    if scenario is None or not scenario.required_flow_known():
        airflow = 'airflow_requirement_unbound'
        reasons.append(
            'no required-airflow scenario bound — eligibility cannot '
            'be claimed (HTDT does not invent ventilation requirements)'
        )
    else:
        required = scenario.required_supply_flow_lps
        measured_flows = [
            obs.measured_supply_flow_lps
            for obs in observations
            if obs.measured_supply_flow_lps is not None
        ]
        declared_flows = [
            e.flow_rate_lps
            for e in bound_evidence
            if e.flow_rate_lps is not None
        ]
        if measured_flows:
            delivered = min(measured_flows)
            basis = 'field-measured'
        elif declared_flows:
            delivered = min(declared_flows)
            basis = 'declared'
        else:
            delivered = None
            basis = None
        if delivered is None:
            airflow = 'insufficient_evidence'
            reasons.append(
                'no airflow evidence bound — required flow cannot be '
                'shown delivered'
            )
        elif delivered < required:
            airflow = 'under_ventilated'
            reasons.append(
                f'{basis} flow {delivered} l/s is below the required '
                f'{required} l/s — a quieter path that cannot '
                'ventilate is ineligible'
            )
        else:
            airflow = 'eligible'
            if basis == 'declared':
                reasons.append(
                    'airflow eligibility rests on declared/calculated '
                    'flow — field balancing can stale it'
                )

    # ---------------- acoustic contribution --------------------------
    acoustic: PathAcousticState
    tonal = 'not_evaluated'
    tonal_flags = [
        flag
        for e in bound_evidence
        for flag in e.tonal_flags
    ]
    tonal_observed = any(
        obs.tonal_observed for obs in observations
        if obs.tonal_observed is not None
    )
    if any(
        e.method == 'iso_11820_in_situ'
        for e in bound_evidence
    ) or any(
        obs.room_noise_spectrum_json is not None
        for obs in observations
    ):
        acoustic = 'contributions_documented'
    elif bound_evidence or observations:
        acoustic = 'contributions_limited'
        if any(
            e.method == 'iso_7235_laboratory'
            for e in bound_evidence
        ) and not any(
            e.method == 'iso_11820_in_situ'
            for e in bound_evidence
        ):
            acoustic = 'lab_evidence_not_installed_truth'
            reasons.append(
                'silencer/path evidence is ISO 7235 laboratory data '
                'only — it is design evidence, not installed '
                'performance (ISO 11820 in-situ differs in sound/'
                'flow field and mounting)'
            )
    else:
        acoustic = 'insufficient_evidence'
        reasons.append(
            'no acoustic evidence bound to the path'
        )
    if tonal_flags or tonal_observed:
        tonal = 'tonal_flags_present'
        reasons.append(
            'tonal/regenerated-noise flags present: '
            + ', '.join(dict.fromkeys(tonal_flags or ('field tonal',)))
        )
        if acoustic == 'contributions_documented':
            acoustic = 'tonal_content_unresolved'
    elif bound_evidence or observations:
        tonal = 'no_tonal_flags'

    # ---------------- flanking / cross-talk --------------------------
    flanking: HvacFlankingState
    if path.flanking_role == 'confirmed_path':
        flanking = 'flanking_path_open'
        reasons.append(
            'the duct network is a confirmed cross-talk route to '
            'adjacent rooms — wall isolation alone cannot qualify '
            'room-to-room isolation (#576 composition)'
        )
    elif path.flanking_role == 'suspected_path':
        flanking = 'flanking_unverified'
        reasons.append(
            'a shared-duct flanking route is suspected but unverified'
        )
    elif path.flanking_role == 'isolated_from_targets':
        flanking = 'flanking_path_mitigated'
    elif path.flanking_role == 'none':
        flanking = 'no_flanking_declared'
    else:
        flanking = 'not_evaluated'

    # ---------------- rattle routing ---------------------------------
    for obs in observations:
        if obs.rattle_routing == 'rattle_suspected_route_to_589':
            reasons.append(
                'mechanically excited grille/duct rattle suspected — '
                'routed to #589, never absorbed into airflow-noise '
                'evidence'
            )

    # ---------------- joint verdict ----------------------------------
    if airflow == 'under_ventilated':
        verdict: HvacVerdict = 'ineligible_airflow'
    elif flanking == 'flanking_path_open':
        verdict = 'failed'
        reasons.append(
            'open duct cross-talk defeats the isolation claim'
        )
    elif airflow in ('airflow_requirement_unbound',
                     'insufficient_evidence'):
        verdict = 'insufficient_evidence'
    elif acoustic == 'insufficient_evidence':
        verdict = 'insufficient_evidence'
    elif (
        airflow == 'eligible'
        and acoustic in ('contributions_documented',
                         'contributions_limited')
        and tonal == 'no_tonal_flags'
        and flanking in ('no_flanking_declared',
                         'flanking_path_mitigated')
    ):
        if acoustic == 'contributions_documented':
            verdict = 'qualified'
        else:
            verdict = 'qualified_with_limitations'
    elif airflow == 'eligible':
        verdict = 'qualified_with_limitations'
    else:
        verdict = 'insufficient_evidence'

    payload = dict(
        document_id=document_id,
        path_ref=hvac_path_binding(path),
        scenario_ref=(
            ventilation_scenario_binding(scenario)
            if scenario is not None else None
        ),
        evidence_refs=tuple(
            hvac_component_binding(e) for e in bound_evidence
        ),
        observation_refs=tuple(
            hvac_observation_binding(o) for o in observations
        ),
        airflow_eligibility=airflow,
        acoustic_state=acoustic,
        flanking_state=flanking,
        tonal_state=tonal,
        verdict=verdict,
        reasons=tuple(reasons),
        evaluation_version=HVAC_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadHvacQualification, payload,
        'qualification_id', 'qualification_sha256', 'hvacqual',
    )


__all__ = [
    'HVAC_EVALUATION_VERSION',
    'HVAC_SCHEMA_VERSION',
    'AcousticMethod',
    'AirflowEligibility',
    'AirflowEvidenceClass',
    'BalancingState',
    'CadHvacComponentEvidence',
    'CadHvacFieldObservation',
    'CadHvacPath',
    'CadHvacPathLeg',
    'CadHvacPathNode',
    'CadHvacQualification',
    'CadVentilationScenario',
    'ComponentKind',
    'FlankingRole',
    'HvacFlankingState',
    'HvacVerdict',
    'OperatingStateKind',
    'PathAcousticState',
    'PathKind',
    'PathLegKind',
    'PathNodeKind',
    'RattleRouting',
    'build_component_evidence',
    'build_field_observation',
    'build_hvac_path',
    'build_ventilation_scenario',
    'evaluate_hvac_path',
    'hvac_component_binding',
    'hvac_observation_binding',
    'hvac_path_binding',
    'hvac_qualification_binding',
    'ventilation_scenario_binding',
]
