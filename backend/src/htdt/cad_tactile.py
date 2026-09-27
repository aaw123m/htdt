"""Tactile-transducer (bass shaker) authority (#638).

Models tactile actuators as *mechanical* devices coupled to seats/risers —
frozen distinct from acoustic speakers at the type level. A shaker does not
radiate into the room; it injects force into a structure, so:

- acoustic source/solver logic is NOT_APPLICABLE to tactile channels by
  default (reported, not omitted);
- attachment is a first-class binding: an actuator instance is coupled to a
  specific seat/riser/furniture entity at a named attachment point with an
  orientation and install state — N actuators may bind to M targets;
- routing distinguishes channel kinds explicitly (``acoustic`` /
  ``tactile`` / ``amplifier`` legs) so a tactile feed is never drawn as a
  speaker wire;
- :class:`TactileProcessingProfile` owns its own HPF/LPF/gain/delay/
  polarity/limiter fields — they are *not* copied from a subwoofer crossover;
- electrical compatibility is a bounded check (amplifier load vs actuator
  impedance/power), not a full amplifier model;
- commissioning evidence is meter-aware and evaluated per binding:
  accelerometer readings carry axis, unit, sensor ref and calibration ref;
  device readback and user-confirmed evidence stay ``empirical`` — a
  metadata-only record never upgrades a binding to ``measured``;
- rattle findings are recorded as findings — never folded into acoustic
  measurements.

InstalledEquipmentInstance (#569) is referenced by id string only — this
module does not implement that authority.
"""

from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_bass_management import CrossoverSpec, FrequencyBand
from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash




RoutingChannelKind = Literal['acoustic', 'tactile', 'amplifier']
"""Kind of one routing leg — acoustic (speaker feed), tactile (actuator
feed), amplifier (power channel feeding a tactile leg)."""

ActuatorTechClass = Literal[
    'voice_coil',
    'linear_motor',
    'piston',
    'rotary',
    'piezo',
    'other',
    'unknown',
]

AttachmentTargetKind = Literal[
    'seat', 'riser', 'furniture', 'floor', 'platform', 'other'
]

InstallState = Literal['planned', 'mounted', 'removed', 'unknown']

TactileCapabilityState = Literal[
    'not_modeled', 'measured', 'empirical', 'future_model'
]
"""How much the twin can claim about tactile behaviour.

- ``not_modeled``: presence and routing only.
- ``measured``: instrumented evidence exists (accelerometer etc.).
- ``empirical``: user-verified feel/response without instruments.
- ``future_model``: placeholder for a coupled-structure model — any
  quantitative question stays UNKNOWN.
"""


class TactileActuatorDefinition(BaseModel):
    """Manufacturer/model-level actuator facts."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['tactile-actuator-1'] = 'tactile-actuator-1'
    definition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    manufacturer: str | None = None
    model: str | None = None
    tech_class: ActuatorTechClass = 'unknown'
    envelope_size: tuple[float, float, float] | None = None
    mounting_constraints: str | None = None
    impedance_ohm: float | None = Field(default=None, gt=0.0)
    power_handling_w: float | None = Field(default=None, gt=0.0)
    operating_band: FrequencyBand | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    definition_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'definition_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'TactileActuatorDefinition':
        if (self.manufacturer is None) != (self.model is None):
            raise ValueError(
                'manufacturer and model must be supplied together'
            )
        if self.definition_sha256 != _hash(self.semantic_payload()):
            raise ValueError('tactile actuator semantic hash mismatch')
        return self


def build_tactile_actuator_definition(
    *,
    definition_id: str,
    version: str,
    tech_class: ActuatorTechClass = 'unknown',
    manufacturer: str | None = None,
    model: str | None = None,
    envelope_size: tuple[float, float, float] | None = None,
    mounting_constraints: str | None = None,
    impedance_ohm: float | None = None,
    power_handling_w: float | None = None,
    operating_band: FrequencyBand | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> TactileActuatorDefinition:
    probe = TactileActuatorDefinition.model_construct(
        definition_id=definition_id,
        version=version,
        manufacturer=manufacturer,
        model=model,
        tech_class=tech_class,
        envelope_size=envelope_size,
        mounting_constraints=mounting_constraints,
        impedance_ohm=impedance_ohm,
        power_handling_w=power_handling_w,
        operating_band=operating_band,
        provenance=tuple(provenance),
        definition_sha256='',
    )
    return TactileActuatorDefinition(
        **probe.model_dump(mode='python', exclude={'definition_sha256'}),
        definition_sha256=_hash(probe.semantic_payload()),
    )


class TactileAttachmentBinding(BaseModel):
    """Couples one installed actuator instance to one structural target."""

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(min_length=1)
    actuator_instance_id: str = Field(min_length=1)
    actuator_definition_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    target_entity_id: str = Field(min_length=1)
    target_kind: AttachmentTargetKind = 'seat'
    attachment_point: str | None = None
    orientation: str | None = None
    install_state: InstallState = 'unknown'
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    binding_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'binding_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'TactileAttachmentBinding':
        if self.binding_sha256 != _hash(self.semantic_payload()):
            raise ValueError('tactile attachment semantic hash mismatch')
        return self


def build_tactile_attachment_binding(
    *,
    binding_id: str,
    actuator_instance_id: str,
    target_entity_id: str,
    target_kind: AttachmentTargetKind = 'seat',
    actuator_definition_sha256: str | None = None,
    attachment_point: str | None = None,
    orientation: str | None = None,
    install_state: InstallState = 'unknown',
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> TactileAttachmentBinding:
    probe = TactileAttachmentBinding.model_construct(
        binding_id=binding_id,
        actuator_instance_id=actuator_instance_id,
        actuator_definition_sha256=actuator_definition_sha256,
        target_entity_id=target_entity_id,
        target_kind=target_kind,
        attachment_point=attachment_point,
        orientation=orientation,
        install_state=install_state,
        provenance=tuple(provenance),
        binding_sha256='',
    )
    return TactileAttachmentBinding(
        **probe.model_dump(mode='python', exclude={'binding_sha256'}),
        binding_sha256=_hash(probe.semantic_payload()),
    )


class TactileProcessingProfile(BaseModel):
    """Signal processing on one tactile bus — its own authority, never a
    copy of subwoofer bass-management filters."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['tactile-processing-1'] = (
        'tactile-processing-1'
    )
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_bus: str = Field(min_length=1)
    amplifier_channel_ref: str | None = None
    high_pass: CrossoverSpec | None = None
    low_pass: CrossoverSpec | None = None
    gain_db: float | None = None
    delay_ms: float | None = Field(default=None, ge=0.0)
    polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'
    limiter_policy: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    profile_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'TactileProcessingProfile':
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError('tactile processing semantic hash mismatch')
        return self


def build_tactile_processing_profile(
    *,
    profile_id: str,
    version: str,
    source_bus: str,
    amplifier_channel_ref: str | None = None,
    high_pass: CrossoverSpec | None = None,
    low_pass: CrossoverSpec | None = None,
    gain_db: float | None = None,
    delay_ms: float | None = None,
    polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown',
    limiter_policy: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> TactileProcessingProfile:
    probe = TactileProcessingProfile.model_construct(
        profile_id=profile_id,
        version=version,
        source_bus=source_bus,
        amplifier_channel_ref=amplifier_channel_ref,
        high_pass=high_pass,
        low_pass=low_pass,
        gain_db=gain_db,
        delay_ms=delay_ms,
        polarity=polarity,
        limiter_policy=limiter_policy,
        provenance=tuple(provenance),
        profile_sha256='',
    )
    return TactileProcessingProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


TactileEvidenceMethod = Literal[
    'accelerometer', 'device_readback', 'user_confirmed', 'other'
]


class TactileMeasurement(BaseModel):
    """Commissioning evidence for one bound actuator/target pair."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    method: TactileEvidenceMethod
    measured_at_utc: str = Field(min_length=1)
    axis: str | None = None
    value: float | None = None
    unit: str | None = None
    sensor_ref: str | None = None
    calibration_ref: str | None = None
    note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'TactileMeasurement':
        if self.method == 'accelerometer':
            if self.axis is None or self.unit is None:
                raise ValueError(
                    'accelerometer evidence requires axis and unit'
                )
        if self.value is not None and self.unit is None:
            raise ValueError('a measured value requires its unit')
        return self


class RattleFinding(BaseModel):
    """A recorded rattle/resonance finding — kept out of acoustic data."""

    model_config = ConfigDict(frozen=True)

    finding_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    observed_at_utc: str = Field(min_length=1)
    description: str | None = None
    severity: Literal['low', 'medium', 'high', 'unknown'] = 'unknown'
    related_binding_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class TactileElectricalCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


def check_tactile_electrical(
    *,
    actuator: TactileActuatorDefinition,
    amplifier_min_load_ohm: float | None,
    amplifier_max_power_w: float | None,
) -> tuple[TactileElectricalCheck, ...]:
    """Bounded electrical-compat check: load compatibility and power
    headroom — nothing more is modelled."""

    checks: list[TactileElectricalCheck] = []
    if actuator.impedance_ohm is None or amplifier_min_load_ohm is None:
        checks.append(
            TactileElectricalCheck(
                check='impedance_compatibility',
                status='UNKNOWN',
                reason='actuator impedance or amplifier minimum load '
                'not recorded',
            )
        )
    else:
        ok = actuator.impedance_ohm >= amplifier_min_load_ohm
        checks.append(
            TactileElectricalCheck(
                check='impedance_compatibility',
                status='PASS' if ok else 'FAIL',
                reason=(
                    f'actuator {actuator.impedance_ohm}Ω vs amplifier '
                    f'minimum {amplifier_min_load_ohm}Ω'
                ),
            )
        )
    if actuator.power_handling_w is None or amplifier_max_power_w is None:
        checks.append(
            TactileElectricalCheck(
                check='power_headroom',
                status='UNKNOWN',
                reason='actuator power handling or amplifier output '
                'not recorded',
            )
        )
    else:
        ok = amplifier_max_power_w <= actuator.power_handling_w
        checks.append(
            TactileElectricalCheck(
                check='power_headroom',
                status='PASS' if ok else 'FAIL',
                reason=(
                    f'amplifier {amplifier_max_power_w}W vs actuator '
                    f'handling {actuator.power_handling_w}W'
                ),
            )
        )
    return tuple(checks)


class TactileSystemEvaluation(BaseModel):
    """Commissioning evaluation of the tactile subsystem."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    checks: tuple[TactileElectricalCheck, ...]
    acoustic_solver_status: Literal['NOT_APPLICABLE'] = 'NOT_APPLICABLE'
    acoustic_solver_reason: str = (
        'tactile actuators couple mechanically; the room acoustic solver '
        'does not apply by default'
    )
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'TactileSystemEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('tactile system evaluation hash mismatch')
        if self.evaluation_id != 'tse-' + digest[:24]:
            raise ValueError('tactile system evaluation id mismatch')
        return self


def evaluate_tactile_system(
    *,
    bindings: tuple[TactileAttachmentBinding, ...],
    processing: tuple[TactileProcessingProfile, ...] = (),
    measurements: tuple[TactileMeasurement, ...] = (),
    entity_kinds: dict[str, str] | None = None,
    known_source_bus_ids: tuple[str, ...] = (),
    known_amplifier_channel_ids: tuple[str, ...] = (),
) -> TactileSystemEvaluation:
    """Binding/processing coherence checks (fail-closed).

    - ``attachment_target``: PASS only when the target Scene entity resolves
      and its kind matches the declared attachable kind; an unresolved or
      stale target id is UNKNOWN (self-declared ``target_kind`` is
      provenance, not proof), a resolved kind mismatch is FAIL.
    - ``processing_source_resolves``: a processing profile's ``source_bus``
      (and ``amplifier_channel_ref`` when set) must resolve against the
      supplied canonical bus/channel ids — a non-empty string alone is
      UNKNOWN, a resolvable id is PASS, a resolver-supplied miss is FAIL.
    - ``measurement_binding_resolves``: every measurement must name an
      attachment binding that was actually supplied.
    - ``measurement_coverage``: per-binding coverage — ``measured`` needs a
      bound accelerometer record with a finite value, axis, unit and
      instrument authority (``sensor_ref``); other evidence kinds count as
      ``empirical``; none is ``not_modeled``.
    - ``capability_state``: whole-subsystem ``measured`` only when every
      supplied binding carries qualifying accelerometer evidence — one
      record never upgrades unmeasured attachments.
    - the acoustic solver reports NOT_APPLICABLE by construction.
    """

    checks: list[TactileElectricalCheck] = []
    entity_kinds = entity_kinds or {}
    attachable = {'seat', 'riser', 'furniture', 'floor', 'platform'}
    binding_ids = {binding.binding_id for binding in bindings}
    bus_ids = set(known_source_bus_ids)
    amp_ids = set(known_amplifier_channel_ids)
    measured_count = 0

    for binding in bindings:
        declared = binding.target_kind
        actual = entity_kinds.get(binding.target_entity_id)
        if actual is None:
            checks.append(
                TactileElectricalCheck(
                    check='attachment_target',
                    status='UNKNOWN',
                    reason=f'{binding.binding_id}: target entity '
                    f"'{binding.target_entity_id}' unresolved; declared "
                    f"kind '{declared}' is unverified",
                )
            )
        else:
            ok = actual in attachable and actual == declared
            checks.append(
                TactileElectricalCheck(
                    check='attachment_target',
                    status='PASS' if ok else 'FAIL',
                    reason=f'{binding.binding_id}: target scene kind '
                    f"'{actual}' vs declared '{declared}'",
                )
            )

        bound = [
            m for m in measurements if m.binding_id == binding.binding_id
        ]
        qualified = [
            m
            for m in bound
            if m.method == 'accelerometer'
            and m.value is not None
            and isfinite(float(m.value))
            and m.axis is not None
            and m.unit is not None
            and m.sensor_ref is not None
        ]
        if qualified:
            coverage: TactileCapabilityState = 'measured'
            measured_count += 1
            coverage_status: EvaluationStatus = 'PASS'
            calibrated = all(
                m.calibration_ref is not None for m in qualified
            )
            coverage_reason = (
                'qualifying accelerometer evidence bound'
                + ('' if calibrated else ' (uncalibrated)')
            )
        elif bound:
            coverage = 'empirical'
            coverage_status = 'UNKNOWN'
            coverage_reason = (
                'only non-instrument or metadata-only evidence bound'
            )
        else:
            coverage = 'not_modeled'
            coverage_status = 'UNKNOWN'
            coverage_reason = 'no measurement evidence bound'
        checks.append(
            TactileElectricalCheck(
                check='measurement_coverage',
                status=coverage_status,
                reason=f'{binding.binding_id}: {coverage} — '
                f'{coverage_reason}',
            )
        )

    for measurement in measurements:
        resolved = measurement.binding_id in binding_ids
        checks.append(
            TactileElectricalCheck(
                check='measurement_binding_resolves',
                status='PASS' if resolved else 'FAIL',
                reason=(
                    f"{measurement.measurement_id}: bound to "
                    f"'{measurement.binding_id}'"
                    if resolved
                    else f'{measurement.measurement_id}: binding '
                    f"'{measurement.binding_id}' not among supplied "
                    'bindings'
                ),
            )
        )

    for profile in processing:
        states: list[str] = []
        if not bus_ids:
            states.append('unverifiable')
        else:
            states.append(
                'resolved' if profile.source_bus in bus_ids else 'missing'
            )
        if profile.amplifier_channel_ref is not None:
            if not amp_ids:
                states.append('unverifiable')
            else:
                states.append(
                    'resolved'
                    if profile.amplifier_channel_ref in amp_ids
                    else 'missing'
                )
        if 'missing' in states:
            status = 'FAIL'
        elif 'unverifiable' in states:
            status = 'UNKNOWN'
        else:
            status = 'PASS'
        checks.append(
            TactileElectricalCheck(
                check='processing_source_resolves',
                status=status,
                reason=f'{profile.profile_id}: source bus '
                f"'{profile.source_bus}'"
                + (
                    f", amplifier channel '{profile.amplifier_channel_ref}'"
                    if profile.amplifier_channel_ref is not None
                    else ''
                )
                + f' — {"/".join(states)}',
            )
        )

    if bindings and measured_count == len(bindings):
        capability: TactileCapabilityState = 'measured'
        capability_status: EvaluationStatus = 'PASS'
        capability_reason = (
            f'capability state: measured ({measured_count} of '
            f'{len(bindings)} bindings carry qualifying accelerometer '
            'evidence)'
        )
    elif any(
        m.binding_id in binding_ids for m in measurements
    ):
        capability = 'empirical'
        capability_status = 'UNKNOWN'
        capability_reason = (
            'capability state: empirical (measured coverage '
            f'{measured_count} of {len(bindings)} bindings; '
            'non-instrument or unbound records do not upgrade coverage)'
        )
    else:
        capability = 'not_modeled'
        capability_status = 'UNKNOWN'
        capability_reason = 'capability state: not_modeled'
    checks.append(
        TactileElectricalCheck(
            check='capability_state',
            status=capability_status,
            reason=capability_reason,
        )
    )

    probe = TactileSystemEvaluation.model_construct(
        evaluation_id='',
        checks=tuple(checks),
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return TactileSystemEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='tse-' + digest[:24],
        evaluation_sha256=digest,
    )


def tactile_system_status(
    evaluation: TactileSystemEvaluation,
) -> EvaluationStatus:
    return _combine_status(tuple(c.status for c in evaluation.checks))
