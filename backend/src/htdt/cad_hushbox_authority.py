"""Projector hush-box / enclosure co-design authority (#624, REV57-PROJ).

A projector hush box is a coupled acoustic + thermal + airflow +
optical + serviceability system. Reducing audible projector noise is
not a valid improvement when the enclosure raises projector
temperature, recirculates exhaust, escalates the internal fan, or
degrades the optical path. This module qualifies those axes together:

- :class:`CadProjectorInstallConstraints` — the exact manufacturer
  installation/ventilation envelope for one projector identity
  (temperature/humidity/altitude/orientation limits, clearances,
  intake/exhaust locations, enclosure restrictions) bound to its source
  document. Unknown fields stay UNKNOWN; nothing is copied across
  models.
- :class:`CadProjectorEnclosurePlan` — the sealed enclosure design /
  as-built: internal geometry, projector pose and clearances, explicit
  air path (source → intake → projector → exhaust → destination) with
  declared recirculation, fans/filters/lining, optical port and access.
- :class:`CadEnclosureOperatingObservation` — one declared sustained
  thermal scenario (warmed multi-hour state, not a cold five-minute
  test).
- :class:`CadEnclosureAcousticObservation` — before/after listener SPL
  bound to a comparable projector operating state.
- :class:`CadEnclosureQualification` + :func:`evaluate_enclosure` — the
  fail-closed joint verdict: thermal, acoustic, optical and
  serviceability axes stay independent; there is no single hush-box
  score.

Honesty rules baked in:

- Free-air fan CFM never claims installed airflow through a resistive
  lined plenum.
- Attenuation claimed across different projector thermal/power states
  (high-power before, low-power after) is marked ``not_comparable``.
- Internal temperature rise that escalates the projector's own fans is
  reported — the fan-control paradox cannot hide inside a lower
  enclosure reading.
- An uncharacterized optical port limits the verdict; glazing is not
  ``optically transparent`` without measured transmission/flare
  evidence.
- Fire/electrical/building approval stays an external professional
  authority — HTDT never certifies it and never suggests defeating
  thermal protection or interlocks.

Literature basis
----------------
- Sony VPL-XW7000ES installation/ventilation support guidance —
  product-specific clearance and ventilation requirements tied to
  reliability (product-specific evidence, not a generic hush-box rule).
- JVC DLA-NZ900 product data — rear-intake/front-exhaust layout,
  5–35 °C operating temperature, 20–80 % humidity, ~440 W input,
  fan-noise state, installation-angle/altitude constraints.
- CEDIA showcase practice (Skyline Cinema, Stingray) — ventilated
  hush boxes / remote ventilated enclosures with added intake/exhaust
  fans and temperature monitoring where room HVAC was insufficient.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


HUSHBOX_SCHEMA_VERSION = 'proj-hushbox-1'
HUSHBOX_EVALUATION_VERSION = 'proj-hushbox-eval-1'

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
# Taxonomies (#624)
# ---------------------------------------------------------------------------

AirEndpoint = Literal['room', 'remote', 'ducted', 'unknown']

RecirculationState = Literal['none', 'possible', 'declared', 'unknown']

AirflowBasis = Literal[
    'installed_measured',
    'free_air_rating',
    'estimated',
    'unknown',
]
"""Free-air ratings never silently equal installed airflow."""

FanRole = Literal['intake', 'exhaust', 'internal', 'unknown']

EnclosureScenario = Literal[
    'standby',
    'sdr_low_power',
    'hdr_high_output',
    'long_movie_warmed',
    'high_ambient',
    'high_altitude',
    'filter_loaded',
    'service_stress',
]

ProjectorFanState = Literal['normal', 'escalated', 'max', 'unknown']

ProtectionEvent = Literal[
    'none', 'warning', 'throttling', 'shutdown', 'unknown',
]

AcousticComparability = Literal[
    'same_state', 'different_state', 'unknown',
]

OpticalPortImpact = Literal[
    'none_observed', 'observed', 'uncharacterized', 'unknown',
]

ContaminationState = Literal['clean', 'contaminated', 'unknown']

ThermalState = Literal[
    'within_documented_environment',
    'thermally_measured_stable',
    'thermally_limited',
    'ventilation_requirement_unknown',
    'manufacturer_constraint_violated',
    'over_temperature_event',
    'insufficient_evidence',
    'not_evaluated',
]

EnclosureAcousticState = Literal[
    'net_reduction_documented',
    'net_reduction_limited',
    'fan_escalation_negates',
    'not_comparable',
    'insufficient_evidence',
    'not_evaluated',
]

EnclosureOpticalState = Literal[
    'no_optical_port',
    'port_within_limits',
    'port_degrades_image',
    'port_uncharacterized',
    'not_evaluated',
]

ServiceabilityState = Literal[
    'service_access_documented',
    'service_access_limited',
    'service_access_blocked',
    'unknown',
]

EnclosureVerdict = Literal[
    'qualified',
    'qualified_with_limitations',
    'failed',
    'insufficient_evidence',
]


# ---------------------------------------------------------------------------
# Manufacturer installation constraints
# ---------------------------------------------------------------------------


class CadProjectorInstallConstraints(BaseModel):
    """Manufacturer-documented installation/ventilation envelope for one
    exact projector identity.

    Every limit is optional-but-pinned: fields not documented stay None
    (UNKNOWN). ``source_document``+``source_revision`` (and optional
    ``source_sha256``) pin the document the constraints were extracted
    from, so rules are never copied from a different model or family.
    """

    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    projector_ref: AuthorityRef | None = None
    manufacturer: str | None = None
    model: str | None = None
    hardware_revision: str | None = None
    firmware: str | None = None
    light_source_mode: str | None = None
    power_draw_w: float | None = None
    operating_temp_min_c: float | None = None
    operating_temp_max_c: float | None = None
    humidity_min_pct: float | None = None
    humidity_max_pct: float | None = None
    altitude_limit_m: float | None = None
    orientation_restrictions: str | None = None
    clearance_front_m: float | None = None
    clearance_rear_m: float | None = None
    clearance_left_m: float | None = None
    clearance_right_m: float | None = None
    clearance_top_m: float | None = None
    clearance_bottom_m: float | None = None
    intake_locations: str | None = None
    exhaust_locations: str | None = None
    service_access_requirements: str | None = None
    enclosure_restrictions: str | None = None
    source_document: str = Field(min_length=1)
    source_revision: str | None = None
    source_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    constraint_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_constraints(self) -> 'CadProjectorInstallConstraints':
        _require_iso8601(
            self.declared_at_utc, 'constraints declared_at_utc'
        )
        if self.projector_ref is not None and (
            self.projector_ref.ref_sha256 is None
        ):
            raise ValueError('projector_ref must pin its sha256')
        numeric_fields = (
            ('power_draw_w', self.power_draw_w),
            ('operating_temp_min_c', self.operating_temp_min_c),
            ('operating_temp_max_c', self.operating_temp_max_c),
            ('humidity_min_pct', self.humidity_min_pct),
            ('humidity_max_pct', self.humidity_max_pct),
            ('altitude_limit_m', self.altitude_limit_m),
            ('clearance_front_m', self.clearance_front_m),
            ('clearance_rear_m', self.clearance_rear_m),
            ('clearance_left_m', self.clearance_left_m),
            ('clearance_right_m', self.clearance_right_m),
            ('clearance_top_m', self.clearance_top_m),
            ('clearance_bottom_m', self.clearance_bottom_m),
        )
        for label, value in numeric_fields:
            if value is not None:
                _require_finite(value, f'constraint {label}')
        for label, value in (
            ('power_draw_w', self.power_draw_w),
            ('altitude_limit_m', self.altitude_limit_m),
            ('clearance_front_m', self.clearance_front_m),
            ('clearance_rear_m', self.clearance_rear_m),
            ('clearance_left_m', self.clearance_left_m),
            ('clearance_right_m', self.clearance_right_m),
            ('clearance_top_m', self.clearance_top_m),
            ('clearance_bottom_m', self.clearance_bottom_m),
        ):
            if value is not None and value < 0:
                raise ValueError(f'constraint {label} must be non-negative')
        if (
            self.operating_temp_min_c is not None
            and self.operating_temp_max_c is not None
            and self.operating_temp_min_c > self.operating_temp_max_c
        ):
            raise ValueError('operating temperature range is inverted')
        if (
            self.humidity_min_pct is not None
            and self.humidity_max_pct is not None
            and self.humidity_min_pct > self.humidity_max_pct
        ):
            raise ValueError('humidity range is inverted')
        expected = _hash(self.identity_payload())
        if self.constraint_sha256 != expected:
            raise ValueError('install constraints hash mismatch')
        if self.constraint_id != _semantic_id('pjcons', expected):
            raise ValueError('constraint id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'projector_ref': (
                self.projector_ref.model_dump(mode='json')
                if self.projector_ref is not None else None
            ),
            'manufacturer': self.manufacturer,
            'model': self.model,
            'hardware_revision': self.hardware_revision,
            'firmware': self.firmware,
            'light_source_mode': self.light_source_mode,
            'power_draw_w': self.power_draw_w,
            'operating_temp_min_c': self.operating_temp_min_c,
            'operating_temp_max_c': self.operating_temp_max_c,
            'humidity_min_pct': self.humidity_min_pct,
            'humidity_max_pct': self.humidity_max_pct,
            'altitude_limit_m': self.altitude_limit_m,
            'orientation_restrictions': self.orientation_restrictions,
            'clearance_front_m': self.clearance_front_m,
            'clearance_rear_m': self.clearance_rear_m,
            'clearance_left_m': self.clearance_left_m,
            'clearance_right_m': self.clearance_right_m,
            'clearance_top_m': self.clearance_top_m,
            'clearance_bottom_m': self.clearance_bottom_m,
            'intake_locations': self.intake_locations,
            'exhaust_locations': self.exhaust_locations,
            'service_access_requirements': self.service_access_requirements,
            'enclosure_restrictions': self.enclosure_restrictions,
            'source_document': self.source_document,
            'source_revision': self.source_revision,
            'source_sha256': self.source_sha256,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def constraint_binding(
    constraints: CadProjectorInstallConstraints,
) -> AuthorityRef:
    return AuthorityRef(
        kind='projector_install_constraints',
        ref_id=constraints.constraint_id,
        ref_sha256=constraints.constraint_sha256,
    )


# ---------------------------------------------------------------------------
# Enclosure plan
# ---------------------------------------------------------------------------


class CadEnclosureFan(BaseModel):
    """One added fan/blower inside the enclosure air path.

    ``airflow_basis`` keeps free-air ratings, installed measurements and
    estimates distinct — a catalogue CFM is not installed airflow
    through a resistive lined duct.
    """

    model_config = ConfigDict(frozen=True)

    fan_id: str = Field(min_length=1)
    product: str | None = None
    role: FanRole = 'unknown'
    control_strategy: str | None = None
    operating_point: str | None = None
    airflow_basis: AirflowBasis = 'unknown'
    airflow_cfm: float | None = None
    noise_evidence: str | None = None
    failure_state: str | None = None
    filter_dependent: bool | None = None

    @model_validator(mode='after')
    def valid_fan(self) -> 'CadEnclosureFan':
        if self.airflow_cfm is not None:
            _require_finite(self.airflow_cfm, 'fan airflow_cfm')
            if self.airflow_cfm < 0:
                raise ValueError('fan airflow must be non-negative')
            if self.airflow_basis in ('unknown',):
                raise ValueError(
                    'a declared airflow requires its basis — '
                    'free-air rating and installed measurement differ'
                )
        return self


class CadEnclosureFilter(BaseModel):
    """One filter in the enclosure air path."""

    model_config = ConfigDict(frozen=True)

    filter_id: str = Field(min_length=1)
    product_class: str | None = None
    installed_area: str | None = None
    service_state: Literal['clean', 'loaded', 'overdue', 'unknown'] = 'unknown'
    last_service_utc: str | None = None

    @model_validator(mode='after')
    def valid_filter(self) -> 'CadEnclosureFilter':
        if self.last_service_utc is not None:
            _require_iso8601(
                self.last_service_utc, 'filter last_service_utc'
            )
        return self


class CadOpticalPort(BaseModel):
    """The enclosure's optical window / projection port.

    Ordinary glazing is not ``optically transparent``: measured
    transmission loss, chromaticity shift, ghosting/flare, contrast and
    focus impacts are recorded as evidence, or the port stays
    ``uncharacterized``.
    """

    model_config = ConfigDict(frozen=True)

    material: str | None = None
    thickness_mm: float | None = None
    coating: str | None = None
    angle_deg: float | None = None
    clear_aperture_mm: float | None = None
    lens_distance_m: float | None = None
    contamination: ContaminationState = 'unknown'
    transmission_loss_pct: float | None = None
    chromaticity_shift: float | None = None
    ghosting_flare: OpticalPortImpact = 'unknown'
    contrast_impact: OpticalPortImpact = 'unknown'
    focus_impact: OpticalPortImpact = 'unknown'

    @model_validator(mode='after')
    def valid_port(self) -> 'CadOpticalPort':
        for label, value in (
            ('thickness_mm', self.thickness_mm),
            ('angle_deg', self.angle_deg),
            ('clear_aperture_mm', self.clear_aperture_mm),
            ('lens_distance_m', self.lens_distance_m),
            ('transmission_loss_pct', self.transmission_loss_pct),
            ('chromaticity_shift', self.chromaticity_shift),
        ):
            if value is not None:
                _require_finite(value, f'optical port {label}')
        for label, value in (
            ('thickness_mm', self.thickness_mm),
            ('clear_aperture_mm', self.clear_aperture_mm),
            ('lens_distance_m', self.lens_distance_m),
        ):
            if value is not None and value < 0:
                raise ValueError(f'optical port {label} must be non-negative')
        return self

    def characterized(self) -> bool:
        """True when the port carries any measured optical evidence."""
        return any(
            value is not None
            for value in (
                self.transmission_loss_pct,
                self.chromaticity_shift,
            )
        ) or 'unknown' not in (
            self.ghosting_flare, self.contrast_impact, self.focus_impact
        )


class CadEnclosureAirPath(BaseModel):
    """The explicit air path — never a bare CFM figure.

    ``recirculation`` records whether the declared layout can feed hot
    exhaust back to the intake; ``declared`` means the geometry as
    designed recirculates, ``possible`` means the path is not proven
    separated.
    """

    model_config = ConfigDict(frozen=True)

    intake_source: AirEndpoint = 'unknown'
    exhaust_destination: AirEndpoint = 'unknown'
    recirculation: RecirculationState = 'unknown'
    path_description: str | None = None


class CadProjectorEnclosurePlan(BaseModel):
    """Sealed enclosure design / as-built geometry.

    A visually similar cabinet is not the same airflow/acoustic system —
    the plan binds exact geometry, air path, fans, filters, lining,
    optical port and access provisions.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    constraint_ref: AuthorityRef | None = None
    projector_ref: AuthorityRef | None = None
    enclosure_ref: AuthorityRef | None = None
    remote_projection: bool = False
    internal_volume_m3: float | None = None
    projector_pose: str | None = None
    clearances_json: str = '{}'
    air_path: CadEnclosureAirPath | None = None
    fans: tuple[CadEnclosureFan, ...] = ()
    filters: tuple[CadEnclosureFilter, ...] = ()
    optical_port: CadOpticalPort | None = None
    lining_treatment: str | None = None
    access_panels: str | None = None
    mount_assembly: str | None = None
    structural_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_plan(self) -> 'CadProjectorEnclosurePlan':
        _require_iso8601(self.declared_at_utc, 'enclosure declared_at_utc')
        for ref, label in (
            (self.constraint_ref, 'constraint_ref'),
            (self.projector_ref, 'projector_ref'),
            (self.enclosure_ref, 'enclosure_ref'),
            (self.structural_ref, 'structural_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.internal_volume_m3 is not None:
            _require_finite(
                self.internal_volume_m3, 'enclosure internal_volume_m3'
            )
            if self.internal_volume_m3 <= 0:
                raise ValueError('enclosure volume must be positive')
        fan_ids = [fan.fan_id for fan in self.fans]
        if len(fan_ids) != len(set(fan_ids)):
            raise ValueError('fan ids must be unique')
        filter_ids = [f.filter_id for f in self.filters]
        if len(filter_ids) != len(set(filter_ids)):
            raise ValueError('filter ids must be unique')
        if self.remote_projection and self.optical_port is None:
            raise ValueError(
                'remote projection requires a declared optical port — '
                'the projection window is part of the design'
            )
        expected = _hash(self.identity_payload())
        if self.plan_sha256 != expected:
            raise ValueError('enclosure plan hash mismatch')
        if self.plan_id != _semantic_id('hushplan', expected):
            raise ValueError('plan id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'constraint_ref': (
                self.constraint_ref.model_dump(mode='json')
                if self.constraint_ref is not None else None
            ),
            'projector_ref': (
                self.projector_ref.model_dump(mode='json')
                if self.projector_ref is not None else None
            ),
            'enclosure_ref': (
                self.enclosure_ref.model_dump(mode='json')
                if self.enclosure_ref is not None else None
            ),
            'remote_projection': self.remote_projection,
            'internal_volume_m3': self.internal_volume_m3,
            'projector_pose': self.projector_pose,
            'clearances_json': self.clearances_json,
            'air_path': (
                self.air_path.model_dump(mode='json')
                if self.air_path is not None else None
            ),
            'fans': [f.model_dump(mode='json') for f in self.fans],
            'filters': [
                f.model_dump(mode='json') for f in self.filters
            ],
            'optical_port': (
                self.optical_port.model_dump(mode='json')
                if self.optical_port is not None else None
            ),
            'lining_treatment': self.lining_treatment,
            'access_panels': self.access_panels,
            'mount_assembly': self.mount_assembly,
            'structural_ref': (
                self.structural_ref.model_dump(mode='json')
                if self.structural_ref is not None else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def enclosure_plan_binding(
    plan: CadProjectorEnclosurePlan,
) -> AuthorityRef:
    return AuthorityRef(
        kind='projector_enclosure_plan',
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


# ---------------------------------------------------------------------------
# Operating / acoustic observations
# ---------------------------------------------------------------------------


class CadEnclosureOperatingObservation(BaseModel):
    """One declared sustained thermal scenario.

    A cold five-minute test is not multi-hour thermal evidence —
    ``duration_s`` and ``time_to_stability_s`` keep the run honest.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    scenario: EnclosureScenario
    duration_s: float
    time_to_stability_s: float | None = None
    inlet_temp_c: float | None = None
    outlet_temp_c: float | None = None
    ambient_temp_c: float | None = None
    projector_temp_c: float | None = None
    projector_fan_state: ProjectorFanState = 'unknown'
    protection_event: ProtectionEvent = 'none'
    fan_states: str | None = None
    filter_state: str | None = None
    measured_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadEnclosureOperatingObservation':
        _require_iso8601(
            self.measured_at_utc, 'observation measured_at_utc'
        )
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('observations must pin the plan sha256')
        _require_finite(self.duration_s, 'observation duration_s')
        if self.duration_s <= 0:
            raise ValueError('duration must be positive')
        for label, value in (
            ('time_to_stability_s', self.time_to_stability_s),
            ('inlet_temp_c', self.inlet_temp_c),
            ('outlet_temp_c', self.outlet_temp_c),
            ('ambient_temp_c', self.ambient_temp_c),
            ('projector_temp_c', self.projector_temp_c),
        ):
            if value is not None:
                _require_finite(value, f'observation {label}')
        if (
            self.time_to_stability_s is not None
            and self.time_to_stability_s < 0
        ):
            raise ValueError('time_to_stability must be non-negative')
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('operating observation hash mismatch')
        if self.observation_id != _semantic_id('hushobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'scenario': self.scenario,
            'duration_s': self.duration_s,
            'time_to_stability_s': self.time_to_stability_s,
            'inlet_temp_c': self.inlet_temp_c,
            'outlet_temp_c': self.outlet_temp_c,
            'ambient_temp_c': self.ambient_temp_c,
            'projector_temp_c': self.projector_temp_c,
            'projector_fan_state': self.projector_fan_state,
            'protection_event': self.protection_event,
            'fan_states': self.fan_states,
            'filter_state': self.filter_state,
            'measured_at_utc': self.measured_at_utc,
            'provenance_json': self.provenance_json,
        }


class CadEnclosureAcousticObservation(BaseModel):
    """Before/after listener SPL for one comparable projector state.

    ``comparability`` is declared: attenuation measured across different
    projector thermal/power states (high-power before, low-power after)
    is ``different_state`` and can never claim net reduction.
    """

    model_config = ConfigDict(frozen=True)

    acoustic_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    projector_state_description: str | None = None
    comparability: AcousticComparability = 'unknown'
    pre_spl_db: float | None = None
    post_spl_db: float | None = None
    listener_position: str | None = None
    band_detail_json: str = '{}'
    added_fan_noise_db: float | None = None
    structure_borne_flags: tuple[str, ...] = ()
    measured_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    acoustic_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_acoustic(self) -> 'CadEnclosureAcousticObservation':
        _require_iso8601(
            self.measured_at_utc, 'acoustic measured_at_utc'
        )
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('acoustic observations pin the plan sha256')
        for label, value in (
            ('pre_spl_db', self.pre_spl_db),
            ('post_spl_db', self.post_spl_db),
            ('added_fan_noise_db', self.added_fan_noise_db),
        ):
            if value is not None:
                _require_finite(value, f'acoustic {label}')
        if (self.pre_spl_db is None) != (self.post_spl_db is None):
            raise ValueError(
                'before/after SPL must be a pair — a single reading '
                'cannot claim attenuation'
            )
        expected = _hash(self.identity_payload())
        if self.acoustic_sha256 != expected:
            raise ValueError('acoustic observation hash mismatch')
        if self.acoustic_id != _semantic_id('hushac', expected):
            raise ValueError('acoustic id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'projector_state_description': (
                self.projector_state_description
            ),
            'comparability': self.comparability,
            'pre_spl_db': self.pre_spl_db,
            'post_spl_db': self.post_spl_db,
            'listener_position': self.listener_position,
            'band_detail_json': self.band_detail_json,
            'added_fan_noise_db': self.added_fan_noise_db,
            'structure_borne_flags': list(self.structure_borne_flags),
            'measured_at_utc': self.measured_at_utc,
            'provenance_json': self.provenance_json,
        }


# ---------------------------------------------------------------------------
# Qualification verdict
# ---------------------------------------------------------------------------


class CadEnclosureQualification(BaseModel):
    """Sealed joint verdict — thermal + acoustic + optical + service.

    The axes stay independent: there is no single hush-box score, and a
    pass on one axis never upgrades another.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    constraint_ref: AuthorityRef | None = None
    observation_refs: tuple[AuthorityRef, ...] = ()
    acoustic_ref: AuthorityRef | None = None
    thermal_state: ThermalState
    acoustic_state: EnclosureAcousticState
    optical_state: EnclosureOpticalState
    serviceability_state: ServiceabilityState
    verdict: EnclosureVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadEnclosureQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.plan_ref.ref_sha256 is None:
            raise ValueError('qualifications must pin the plan sha256')
        for ref, label in (
            (self.constraint_ref, 'constraint_ref'),
            (self.acoustic_ref, 'acoustic_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for ref in self.observation_refs:
            if ref.ref_sha256 is None:
                raise ValueError('observation refs must pin their sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification hash mismatch')
        if self.qualification_id != _semantic_id('hushqual', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'plan_ref': self.plan_ref.model_dump(mode='json'),
            'constraint_ref': (
                self.constraint_ref.model_dump(mode='json')
                if self.constraint_ref is not None else None
            ),
            'observation_refs': [
                r.model_dump(mode='json') for r in self.observation_refs
            ],
            'acoustic_ref': (
                self.acoustic_ref.model_dump(mode='json')
                if self.acoustic_ref is not None else None
            ),
            'thermal_state': self.thermal_state,
            'acoustic_state': self.acoustic_state,
            'optical_state': self.optical_state,
            'serviceability_state': self.serviceability_state,
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def enclosure_qualification_binding(
    qualification: CadEnclosureQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='enclosure_qualification',
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


def build_install_constraints(**kwargs: Any) -> CadProjectorInstallConstraints:
    """Seal manufacturer installation constraints for one projector."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadProjectorInstallConstraints, dict(kwargs),
        'constraint_id', 'constraint_sha256', 'pjcons',
    )


def build_enclosure_plan(
    *,
    document_id: str,
    constraint_ref: AuthorityRef | CadProjectorInstallConstraints | None = None,
    projector_ref: AuthorityRef | None = None,
    enclosure_ref: AuthorityRef | None = None,
    remote_projection: bool = False,
    internal_volume_m3: float | None = None,
    projector_pose: str | None = None,
    clearances_json: str = '{}',
    air_path: CadEnclosureAirPath | None = None,
    fans: tuple[CadEnclosureFan, ...] = (),
    filters: tuple[CadEnclosureFilter, ...] = (),
    optical_port: CadOpticalPort | None = None,
    lining_treatment: str | None = None,
    access_panels: str | None = None,
    mount_assembly: str | None = None,
    structural_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadProjectorEnclosurePlan:
    """Seal an enclosure plan / as-built."""
    if isinstance(constraint_ref, CadProjectorInstallConstraints):
        constraint_ref = constraint_binding(constraint_ref)
    payload = dict(
        document_id=document_id,
        constraint_ref=constraint_ref,
        projector_ref=projector_ref,
        enclosure_ref=enclosure_ref,
        remote_projection=remote_projection,
        internal_volume_m3=internal_volume_m3,
        projector_pose=projector_pose,
        clearances_json=clearances_json,
        air_path=air_path,
        fans=fans,
        filters=filters,
        optical_port=optical_port,
        lining_treatment=lining_treatment,
        access_panels=access_panels,
        mount_assembly=mount_assembly,
        structural_ref=structural_ref,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadProjectorEnclosurePlan, payload,
        'plan_id', 'plan_sha256', 'hushplan',
    )


def build_operating_observation(
    *,
    document_id: str,
    plan: CadProjectorEnclosurePlan | AuthorityRef,
    scenario: EnclosureScenario,
    duration_s: float,
    time_to_stability_s: float | None = None,
    inlet_temp_c: float | None = None,
    outlet_temp_c: float | None = None,
    ambient_temp_c: float | None = None,
    projector_temp_c: float | None = None,
    projector_fan_state: ProjectorFanState = 'unknown',
    protection_event: ProtectionEvent = 'none',
    fan_states: str | None = None,
    filter_state: str | None = None,
    measured_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadEnclosureOperatingObservation:
    """Seal one sustained thermal observation."""
    plan_ref = (
        enclosure_plan_binding(plan)
        if isinstance(plan, CadProjectorEnclosurePlan)
        else plan
    )
    payload = dict(
        document_id=document_id,
        plan_ref=plan_ref,
        scenario=scenario,
        duration_s=duration_s,
        time_to_stability_s=time_to_stability_s,
        inlet_temp_c=inlet_temp_c,
        outlet_temp_c=outlet_temp_c,
        ambient_temp_c=ambient_temp_c,
        projector_temp_c=projector_temp_c,
        projector_fan_state=projector_fan_state,
        protection_event=protection_event,
        fan_states=fan_states,
        filter_state=filter_state,
        measured_at_utc=measured_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadEnclosureOperatingObservation, payload,
        'observation_id', 'observation_sha256', 'hushobs',
    )


def build_acoustic_observation(
    *,
    document_id: str,
    plan: CadProjectorEnclosurePlan | AuthorityRef,
    projector_state_description: str | None = None,
    comparability: AcousticComparability = 'unknown',
    pre_spl_db: float | None = None,
    post_spl_db: float | None = None,
    listener_position: str | None = None,
    band_detail_json: str = '{}',
    added_fan_noise_db: float | None = None,
    structure_borne_flags: tuple[str, ...] = (),
    measured_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadEnclosureAcousticObservation:
    """Seal one before/after acoustic observation."""
    plan_ref = (
        enclosure_plan_binding(plan)
        if isinstance(plan, CadProjectorEnclosurePlan)
        else plan
    )
    payload = dict(
        document_id=document_id,
        plan_ref=plan_ref,
        projector_state_description=projector_state_description,
        comparability=comparability,
        pre_spl_db=pre_spl_db,
        post_spl_db=post_spl_db,
        listener_position=listener_position,
        band_detail_json=band_detail_json,
        added_fan_noise_db=added_fan_noise_db,
        structure_borne_flags=structure_borne_flags,
        measured_at_utc=measured_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadEnclosureAcousticObservation, payload,
        'acoustic_id', 'acoustic_sha256', 'hushac',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_OBSERVATION_REF_KIND = 'enclosure_operating_observation'
_ACOUSTIC_REF_KIND = 'enclosure_acoustic_observation'


def _observation_ref(
    observation: CadEnclosureOperatingObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind=_OBSERVATION_REF_KIND,
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


def _acoustic_ref(
    observation: CadEnclosureAcousticObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind=_ACOUSTIC_REF_KIND,
        ref_id=observation.acoustic_id,
        ref_sha256=observation.acoustic_sha256,
    )


def evaluate_enclosure(
    *,
    document_id: str,
    plan: CadProjectorEnclosurePlan,
    constraints: CadProjectorInstallConstraints | None = None,
    observations: tuple[CadEnclosureOperatingObservation, ...] = (),
    acoustic: CadEnclosureAcousticObservation | None = None,
    evaluated_at_utc: str | None = None,
) -> CadEnclosureQualification:
    """Fail-closed joint thermal/acoustic/optical/service verdict.

    Manufacturer-documented limits are the only thresholds — no
    universal enclosure delta-T, CFM or clearance is invented. A
    ``qualified`` verdict requires a documented environment, a sustained
    stable scenario, a comparable acoustic reduction and a
    characterized (or absent) optical port.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    for obs in observations:
        if obs.plan_ref.ref_id != plan.plan_id or (
            obs.plan_ref.ref_sha256 != plan.plan_sha256
        ):
            raise ValueError(
                'operating observation binds a different plan'
            )
    if acoustic is not None and (
        acoustic.plan_ref.ref_id != plan.plan_id
        or acoustic.plan_ref.ref_sha256 != plan.plan_sha256
    ):
        raise ValueError('acoustic observation binds a different plan')
    if constraints is not None and plan.constraint_ref is not None and (
        plan.constraint_ref.ref_id != constraints.constraint_id
        or plan.constraint_ref.ref_sha256 != constraints.constraint_sha256
    ):
        raise ValueError(
            'the supplied constraints do not match the plan binding'
        )

    # ---------------- thermal -----------------------------------------
    thermal_state: ThermalState
    if constraints is None:
        thermal_state = 'ventilation_requirement_unknown'
        reasons.append(
            'no manufacturer installation/ventilation constraints bound — '
            'the operating envelope cannot be checked'
        )
    elif not observations:
        thermal_state = 'insufficient_evidence'
        reasons.append(
            'no sustained operating observation — a cold short test '
            'is not thermal evidence'
        )
    else:
        protection_event_seen = False
        violated = False
        stable_seen = False
        fan_escalated = False
        for obs in observations:
            if obs.protection_event in ('throttling', 'shutdown'):
                protection_event_seen = True
                reasons.append(
                    f'scenario {obs.scenario}: projector protection '
                    f'event {obs.protection_event} — candidate rejected'
                )
                continue
            if (
                constraints.operating_temp_max_c is not None
                and obs.inlet_temp_c is not None
                and obs.inlet_temp_c > constraints.operating_temp_max_c
            ):
                violated = True
                reasons.append(
                    f'scenario {obs.scenario}: enclosure inlet '
                    f'{obs.inlet_temp_c} °C exceeds documented operating '
                    f'maximum {constraints.operating_temp_max_c} °C'
                )
                continue
            if obs.time_to_stability_s is not None:
                stable_seen = True
            if obs.projector_fan_state in ('escalated', 'max'):
                fan_escalated = True
                reasons.append(
                    f'scenario {obs.scenario}: projector internal fan '
                    'escalated — enclosure insertion raised thermal load'
                )
        if protection_event_seen:
            thermal_state = 'over_temperature_event'
        elif violated:
            thermal_state = 'manufacturer_constraint_violated'
        elif fan_escalated:
            thermal_state = 'thermally_limited'
        elif (
            plan.air_path is not None
            and plan.air_path.recirculation in ('declared', 'possible')
        ):
            thermal_state = 'thermally_limited'
            reasons.append(
                'enclosure air path does not prove exhaust/intake '
                'separation — recirculation risk limits the verdict'
            )
        elif stable_seen:
            thermal_state = 'thermally_measured_stable'
        else:
            thermal_state = 'within_documented_environment'

    # ---------------- acoustic ----------------------------------------
    acoustic_state: EnclosureAcousticState
    if acoustic is None:
        acoustic_state = 'not_evaluated'
    elif acoustic.comparability != 'same_state':
        acoustic_state = 'not_comparable'
        reasons.append(
            'acoustic before/after not measured at the same projector '
            'state — attenuation cannot be claimed across differing '
            'thermal/power states'
        )
    elif acoustic.pre_spl_db is None or acoustic.post_spl_db is None:
        acoustic_state = 'insufficient_evidence'
    else:
        if any(
            obs.projector_fan_state in ('escalated', 'max')
            for obs in observations
        ):
            acoustic_state = 'fan_escalation_negates'
            reasons.append(
                'enclosure attenuated direct noise but the projector '
                'fan escalated — net listener noise improvement is not '
                'established'
            )
        elif acoustic.post_spl_db < acoustic.pre_spl_db:
            acoustic_state = 'net_reduction_documented'
        elif acoustic.post_spl_db <= acoustic.pre_spl_db:
            acoustic_state = 'net_reduction_limited'
        else:
            acoustic_state = 'fan_escalation_negates'
            reasons.append(
                'post-enclosure listener SPL is not below the baseline '
                '— the enclosure added noise or load'
            )
        if acoustic.structure_borne_flags:
            reasons.append(
                'structure-borne flags present: '
                + ', '.join(acoustic.structure_borne_flags)
            )
            if acoustic_state == 'net_reduction_documented':
                acoustic_state = 'net_reduction_limited'

    # ---------------- optical port ------------------------------------
    optical_state: EnclosureOpticalState
    if plan.optical_port is None:
        optical_state = 'no_optical_port'
    else:
        port = plan.optical_port
        if (
            port.ghosting_flare == 'observed'
            or port.contrast_impact == 'observed'
            or port.focus_impact == 'observed'
            or port.contamination == 'contaminated'
        ):
            optical_state = 'port_degrades_image'
            reasons.append(
                'optical port has observed degradation (flare/contrast/'
                'focus/contamination) — it limits #619/#622 qualification'
            )
        elif not port.characterized():
            optical_state = 'port_uncharacterized'
            reasons.append(
                'optical port lacks measured transmission/chromaticity/'
                'flare evidence — ordinary glazing is not transparent '
                'by default'
            )
        else:
            optical_state = 'port_within_limits'

    # ---------------- serviceability ----------------------------------
    serviceability_state: ServiceabilityState
    if plan.access_panels is None and plan.mount_assembly is None:
        serviceability_state = 'unknown'
        reasons.append(
            'no access/service evidence declared — filter, lens and '
            'lamp service paths are unverified'
        )
    elif plan.access_panels is None:
        serviceability_state = 'service_access_limited'
        reasons.append(
            'mount declared but no access panels — routine service may '
            'require disturbing calibrated projector geometry'
        )
    else:
        serviceability_state = 'service_access_documented'

    # ---------------- joint verdict -----------------------------------
    if thermal_state in (
        'over_temperature_event',
        'manufacturer_constraint_violated',
    ) or acoustic_state == 'fan_escalation_negates':
        verdict: EnclosureVerdict = 'failed'
    elif thermal_state in ('ventilation_requirement_unknown',
                           'insufficient_evidence'):
        verdict = 'insufficient_evidence'
    elif (
        thermal_state in ('within_documented_environment',
                          'thermally_measured_stable')
        and acoustic_state in ('net_reduction_documented',
                               'not_evaluated')
        and optical_state in ('no_optical_port', 'port_within_limits')
        and serviceability_state in ('service_access_documented',)
    ):
        verdict = 'qualified'
    elif thermal_state in ('within_documented_environment',
                           'thermally_measured_stable',
                           'thermally_limited'):
        verdict = 'qualified_with_limitations'
    else:
        verdict = 'insufficient_evidence'

    payload = dict(
        document_id=document_id,
        plan_ref=enclosure_plan_binding(plan),
        constraint_ref=(
            constraint_binding(constraints)
            if constraints is not None else None
        ),
        observation_refs=tuple(
            _observation_ref(obs) for obs in observations
        ),
        acoustic_ref=(
            _acoustic_ref(acoustic) if acoustic is not None else None
        ),
        thermal_state=thermal_state,
        acoustic_state=acoustic_state,
        optical_state=optical_state,
        serviceability_state=serviceability_state,
        verdict=verdict,
        reasons=tuple(reasons),
        evaluation_version=HUSHBOX_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadEnclosureQualification, payload,
        'qualification_id', 'qualification_sha256', 'hushqual',
    )


__all__ = [
    'HUSHBOX_EVALUATION_VERSION',
    'HUSHBOX_SCHEMA_VERSION',
    'AcousticComparability',
    'AirEndpoint',
    'AirflowBasis',
    'CadEnclosureAcousticObservation',
    'CadEnclosureAirPath',
    'CadEnclosureFan',
    'CadEnclosureFilter',
    'CadEnclosureOperatingObservation',
    'CadEnclosureQualification',
    'CadOpticalPort',
    'CadProjectorEnclosurePlan',
    'CadProjectorInstallConstraints',
    'ContaminationState',
    'EnclosureAcousticState',
    'EnclosureOpticalState',
    'EnclosureScenario',
    'EnclosureVerdict',
    'FanRole',
    'OpticalPortImpact',
    'ProtectionEvent',
    'ProjectorFanState',
    'RecirculationState',
    'ServiceabilityState',
    'ThermalState',
    'build_acoustic_observation',
    'build_enclosure_plan',
    'build_install_constraints',
    'build_operating_observation',
    'constraint_binding',
    'enclosure_plan_binding',
    'enclosure_qualification_binding',
    'evaluate_enclosure',
]
