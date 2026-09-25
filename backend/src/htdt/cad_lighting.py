"""Theater lighting-scene authority (#640).

Models fixtures, zones, named scenes and ambient-light evidence for a theater
room — explicitly *not* an architectural lighting CAD. There is no photometric
simulation: dimmer levels are percentages of device output and are NEVER
converted into lux; ambient lux only ever comes from measured evidence.

- :class:`LightingFixture` — one controllable device (downlight, LED strip,
  bias bar, …). Fixtures may optionally bind to a scene entity id for 3-D
  placement; a control-only fixture with no modelled body is valid.
- :class:`LightingZone` — an explicit list of member fixtures with a role
  (``general``, ``bias``, ``accent``, ``path``, ``other``). A bias zone names
  the display/screen entity it serves — the twin records the typed role and
  level, but claims no reference-standard compliance without a measured
  profile.
- :class:`LightingScene` — a user-named authority (``scene_id`` +
  ``version`` + hash) mapping fixture/zone refs to states. Scene names and
  purposes are user vocabulary — the module hard-codes no scene list.
- Setpoint stages are separate fields on
  :class:`LightingCommissioningRecord`: ``desired`` lives in the scene,
  ``commanded`` is what the controller was told, ``read_back`` is what the
  bus reported, ``measured`` is what was observed in the room. They are never
  merged.
- :class:`LightingAmbientObservation` — a lux reading bound to an exact
  ``LightingScene`` (id+version+sha triple), a location/plane label, the
  instrument and timestamp, and optionally the room operating state id.
- :func:`evaluate_lighting_scene` — commissioning checks: every scene ref
  resolves, states are well-formed, bias zones have a target surface, ambient
  evidence is bound to the exact scene hash. Photometric questions report
  UNKNOWN — not a number synthesised from dimmer levels.

Controller/protocol adapters are out of scope; no credentials live in these
authorities — a fixture's ``control_ref`` is an opaque string the adapter
layer interprets.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status


def _hash(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


LightingFixtureType = Literal[
    'downlight',
    'sconce',
    'led_strip',
    'bias_bar',
    'rope_light',
    'panel',
    'lamp',
    'other',
]

LightingZoneRole = Literal['general', 'bias', 'accent', 'path', 'other']
"""Typed zone role. ``bias`` is the only role with semantic attachment: a
bias zone must declare which display/screen entity it illuminates behind."""

LightingCapability = Literal[
    'state_only', 'measured_at_screen', 'future_photometric'
]
"""What the twin can claim about this zone's light.

- ``state_only``: level/CCT commands only — no illuminance claims.
- ``measured_at_screen``: an ambient/screen measurement exists that
  characterises the effect.
- ``future_photometric``: placeholder for real photometric modeling; any
  photometric question stays UNKNOWN until then.
"""

SetpointStage = Literal['desired', 'commanded', 'read_back', 'measured']

LightingEvaluationDimension = Literal[
    'scene_definition_integrity',
    'device_state_conformance',
    'ambient_measurement_evidence',
    'photometric_model_capability',
]
"""Independent verdict axes. ``photometric_model_capability`` is an
intentionally unsupported capability — it is reported, never folded into
the device-state commissioning verdict."""


class LightingFixture(BaseModel):
    model_config = ConfigDict(frozen=True)

    fixture_id: str = Field(min_length=1)
    label: str | None = None
    fixture_type: LightingFixtureType = 'other'
    entity_id: str | None = None
    control_ref: str | None = None
    controllable_level: bool = True
    cct_min_k: int | None = Field(default=None, ge=0)
    cct_max_k: int | None = Field(default=None, ge=0)
    color_capable: bool = False
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'LightingFixture':
        if (
            self.cct_min_k is not None
            and self.cct_max_k is not None
            and self.cct_min_k > self.cct_max_k
        ):
            raise ValueError('cct_min_k must not exceed cct_max_k')
        return self


class LightingZone(BaseModel):
    model_config = ConfigDict(frozen=True)

    zone_id: str = Field(min_length=1)
    label: str | None = None
    role: LightingZoneRole = 'general'
    member_fixture_ids: tuple[str, ...] = ()
    bias_target_entity_id: str | None = None
    capability: LightingCapability = 'state_only'
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'LightingZone':
        if self.role == 'bias' and self.bias_target_entity_id is None:
            raise ValueError(
                'bias zones must name the display/screen entity they serve'
            )
        if self.role != 'bias' and self.bias_target_entity_id is not None:
            raise ValueError(
                'bias_target_entity_id is only valid on bias zones'
            )
        ids = list(self.member_fixture_ids)
        if len(set(ids)) != len(ids):
            raise ValueError('zone membership must not duplicate fixtures')
        return self


class ChannelState(BaseModel):
    """A state assignment inside a scene: ref + level (+optional CCT/color).

    Level is a plain 0–100 device percentage — it is never a photometric
    quantity.
    """

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal['fixture', 'zone']
    ref_id: str = Field(min_length=1)
    level_percent: float = Field(ge=0.0, le=100.0)
    cct_k: int | None = Field(default=None, ge=0)
    color_rgb: tuple[int, int, int] | None = None


class LightingScene(BaseModel):
    """A user-named lighting scene — durable, versioned, self-hashed."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['lighting-scene-1'] = 'lighting-scene-1'
    scene_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    label: str
    purpose: str | None = None
    states: tuple[ChannelState, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    scene_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'scene_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'LightingScene':
        keys = [(s.ref_kind, s.ref_id) for s in self.states]
        if len(set(keys)) != len(keys):
            raise ValueError(
                'a scene must not assign the same fixture/zone twice'
            )
        if self.scene_sha256 != _hash(self.semantic_payload()):
            raise ValueError('lighting scene semantic hash mismatch')
        return self


def build_lighting_scene(
    *,
    scene_id: str,
    version: str,
    label: str,
    states: tuple[ChannelState, ...] = (),
    purpose: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> LightingScene:
    probe = LightingScene.model_construct(
        scene_id=scene_id,
        version=version,
        label=label,
        purpose=purpose,
        states=tuple(states),
        provenance=tuple(provenance),
        scene_sha256='',
    )
    return LightingScene(
        **probe.model_dump(mode='python', exclude={'scene_sha256'}),
        scene_sha256=_hash(probe.semantic_payload()),
    )


class StageLevel(BaseModel):
    """A level observed at one setpoint stage."""

    model_config = ConfigDict(frozen=True)

    stage: SetpointStage
    level_percent: float = Field(ge=0.0, le=100.0)
    cct_k: int | None = Field(default=None, ge=0)
    observed_at_utc: str | None = None
    source: str | None = None


class LightingCommissioningRecord(BaseModel):
    """Per-ref setpoint lifecycle for one executed scene.

    Desired state comes from the scene; commanded/read-back/measured come
    from controller interaction. All four are stored side by side — never
    collapsed into one "actual" value.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    scene_version: str = Field(min_length=1)
    scene_sha256: str = Field(min_length=16)
    ref_kind: Literal['fixture', 'zone']
    ref_id: str = Field(min_length=1)
    desired: StageLevel | None = None
    commanded: StageLevel | None = None
    read_back: StageLevel | None = None
    measured: StageLevel | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'LightingCommissioningRecord':
        for slot in ('desired', 'commanded', 'read_back', 'measured'):
            level = getattr(self, slot)
            if level is not None and level.stage != slot:
                raise ValueError(
                    f'{slot} slot carries a StageLevel with '
                    f'stage={level.stage!r}; stage must match the slot'
                )
        return self


class LightingTolerancePolicy(BaseModel):
    """Versioned tolerance policy for setpoint comparisons.

    Evaluating commissioning records requires an explicit policy — the
    evaluator never falls back to a hidden dimmer tolerance. Fields are
    ``None`` when that quantity is not comparable under this policy (a
    comparison that needs an unconfigured dimension stays UNKNOWN).
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    level_percent_tolerance: float = Field(gt=0.0, le=100.0)
    cct_k_tolerance: int | None = Field(default=None, ge=0)


class LightingAmbientObservation(BaseModel):
    """A measured lux reading bound to an exact scene identity."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    observation_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    scene_version: str = Field(min_length=1)
    scene_sha256: str = Field(min_length=16)
    location_label: str = Field(min_length=1)
    plane: str | None = None
    measured_lux: float = Field(ge=0.0)
    instrument: str | None = None
    measured_at_utc: str = Field(min_length=1)
    room_operating_state_id: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class LightingCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    dimension: LightingEvaluationDimension
    reason: str | None = None


class LightingDimensionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    dimension: LightingEvaluationDimension
    status: EvaluationStatus


class LightingSceneEvaluation(BaseModel):
    """Commissioning evaluation of a scene against the lighting inventory."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    scene: LightingScene
    checks: tuple[LightingCheckResult, ...]
    dimensions: tuple[LightingDimensionResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'LightingSceneEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('lighting scene evaluation hash mismatch')
        if self.evaluation_id != 'lse-' + digest[:24]:
            raise ValueError('lighting scene evaluation id mismatch')
        return self


def _record_desired_level(
    record: LightingCommissioningRecord,
    scene_states: dict[tuple[str, str], ChannelState],
) -> tuple[float | None, int | None]:
    """The intended level/CCT for this record's ref: the record's own
    ``desired`` snapshot wins; otherwise the scene's assignment."""
    if record.desired is not None:
        return record.desired.level_percent, record.desired.cct_k
    state = scene_states.get((record.ref_kind, record.ref_id))
    if state is None:
        return None, None
    return state.level_percent, state.cct_k


def _evaluate_record_conformance(
    record: LightingCommissioningRecord,
    scene: LightingScene,
    scene_states: dict[tuple[str, str], ChannelState],
    known_refs: set[tuple[str, str]],
    policy: LightingTolerancePolicy,
) -> LightingCheckResult:
    """Device-state conformance for one record.

    Commanded state is evidence a request was sent — never proof of
    applied state. Only ``read_back`` (or a scoped ``measured``
    observation where no read-back exists) can verify.
    """
    name = f'device_state_conformance:{record.ref_kind}:{record.ref_id}'

    if (
        record.scene_id != scene.scene_id
        or record.scene_version != scene.version
        or record.scene_sha256 != scene.scene_sha256
    ):
        return LightingCheckResult(
            check=name,
            status='FAIL',
            dimension='device_state_conformance',
            reason='record is bound to a different scene identity',
        )
    if (record.ref_kind, record.ref_id) not in known_refs:
        return LightingCheckResult(
            check=name,
            status='FAIL',
            dimension='device_state_conformance',
            reason='record ref does not resolve in the lighting inventory',
        )

    desired_level, desired_cct = _record_desired_level(record, scene_states)
    if desired_level is None:
        return LightingCheckResult(
            check=name,
            status='UNKNOWN',
            dimension='device_state_conformance',
            reason='no desired state recorded for this ref',
        )

    observed = record.read_back if record.read_back is not None else record.measured
    if observed is None:
        if record.commanded is not None:
            detail = 'commanded but no read-back — application unverified'
        else:
            detail = 'desired only — never executed'
        return LightingCheckResult(
            check=name,
            status='UNKNOWN',
            dimension='device_state_conformance',
            reason=detail,
        )

    mismatches: list[str] = []
    unknowns: list[str] = []
    delta = abs(observed.level_percent - desired_level)
    if delta > policy.level_percent_tolerance:
        mismatches.append(
            f'level desired {desired_level}% vs {observed.stage} '
            f'{observed.level_percent}% (tolerance '
            f'{policy.level_percent_tolerance}%)'
        )
    if observed.cct_k is not None or desired_cct is not None:
        if (
            observed.cct_k is None
            or desired_cct is None
            or policy.cct_k_tolerance is None
        ):
            unknowns.append('cct comparison not possible under this policy')
        elif abs(observed.cct_k - desired_cct) > policy.cct_k_tolerance:
            mismatches.append(
                f'cct desired {desired_cct}K vs {observed.stage} '
                f'{observed.cct_k}K (tolerance {policy.cct_k_tolerance}K)'
            )

    if mismatches:
        return LightingCheckResult(
            check=name,
            status='FAIL',
            dimension='device_state_conformance',
            reason='; '.join(mismatches),
        )
    if unknowns:
        return LightingCheckResult(
            check=name,
            status='UNKNOWN',
            dimension='device_state_conformance',
            reason='; '.join(unknowns),
        )
    return LightingCheckResult(
        check=name,
        status='PASS',
        dimension='device_state_conformance',
        reason=f'{observed.stage} matches desired within '
        f'policy {policy.policy_id} v{policy.policy_version}',
    )


def evaluate_lighting_scene(
    *,
    scene: LightingScene,
    fixtures: tuple[LightingFixture, ...],
    zones: tuple[LightingZone, ...],
    ambient_observation: LightingAmbientObservation | None = None,
    commissioning_records: tuple[LightingCommissioningRecord, ...] = (),
    tolerance_policy: LightingTolerancePolicy | None = None,
) -> LightingSceneEvaluation:
    """Per-check evaluation — no hidden overall score.

    Each check names its dimension; ``dimensions`` folds statuses per axis
    so an intentionally unsupported capability (photometric modeling)
    never contaminates the device-state commissioning verdict.
    """

    if commissioning_records and tolerance_policy is None:
        raise ValueError(
            'commissioning records require an explicit tolerance_policy'
        )

    fixture_ids = {f.fixture_id for f in fixtures}
    zone_ids = {z.zone_id for z in zones}
    zone_by_id = {z.zone_id: z for z in zones}

    unresolved = [
        f'{s.ref_kind}:{s.ref_id}'
        for s in scene.states
        if (s.ref_id not in fixture_ids if s.ref_kind == 'fixture'
            else s.ref_id not in zone_ids)
    ]
    checks: list[LightingCheckResult] = [
        LightingCheckResult(
            check='references_resolve',
            status='FAIL' if unresolved else 'PASS',
            dimension='scene_definition_integrity',
            reason=(
                'unresolved refs: ' + ', '.join(unresolved)
                if unresolved
                else 'all scene refs resolve to fixtures/zones'
            ),
        )
    ]

    bias_zones = [
        zone_by_id[s.ref_id]
        for s in scene.states
        if s.ref_kind == 'zone'
        and s.ref_id in zone_by_id
        and zone_by_id[s.ref_id].role == 'bias'
    ]
    if bias_zones:
        missing_target = [
            z.zone_id for z in bias_zones
            if z.bias_target_entity_id is None
        ]
        checks.append(
            LightingCheckResult(
                check='bias_target_bound',
                status='FAIL' if missing_target else 'PASS',
                dimension='scene_definition_integrity',
                reason=(
                    'bias zones missing a target surface: '
                    + ', '.join(missing_target)
                    if missing_target
                    else 'bias zones declare their display/screen target'
                ),
            )
        )
    else:
        checks.append(
            LightingCheckResult(
                check='bias_target_bound',
                status='NOT_APPLICABLE',
                dimension='scene_definition_integrity',
                reason='scene touches no bias zone',
            )
        )

    if ambient_observation is None:
        checks.append(
            LightingCheckResult(
                check='ambient_evidence',
                status='UNKNOWN',
                dimension='ambient_measurement_evidence',
                reason='no measured lux observation bound to this scene',
            )
        )
    elif (
        ambient_observation.scene_id != scene.scene_id
        or ambient_observation.scene_version != scene.version
        or ambient_observation.scene_sha256 != scene.scene_sha256
    ):
        checks.append(
            LightingCheckResult(
                check='ambient_evidence',
                status='FAIL',
                dimension='ambient_measurement_evidence',
                reason='ambient observation is bound to a different '
                'scene identity',
            )
        )
    else:
        checks.append(
            LightingCheckResult(
                check='ambient_evidence',
                status='PASS',
                dimension='ambient_measurement_evidence',
                reason=f'measured {ambient_observation.measured_lux} lux at '
                f'{ambient_observation.location_label}',
            )
        )

    # Device-state conformance: each record is evaluated independently
    # against the exact scene identity and the versioned tolerance policy.
    scene_states = {(s.ref_kind, s.ref_id): s for s in scene.states}
    known_refs = {
        *(('fixture', f.fixture_id) for f in fixtures),
        *(('zone', z.zone_id) for z in zones),
    }
    for record in commissioning_records:
        checks.append(
            _evaluate_record_conformance(
                record, scene, scene_states, known_refs, tolerance_policy
            )
        )

    # The twin does no photometric modeling: any illuminance question from
    # state alone is UNKNOWN — reported on its own dimension, never folded
    # into the device-state verdict.
    checks.append(
        LightingCheckResult(
            check='photometric_modeling',
            status='UNKNOWN',
            dimension='photometric_model_capability',
            reason='dimmer levels are never converted to lux; only '
            'measured evidence carries illuminance',
        )
    )

    dimension_order = (
        'scene_definition_integrity',
        'device_state_conformance',
        'ambient_measurement_evidence',
        'photometric_model_capability',
    )
    dimensions = tuple(
        LightingDimensionResult(
            dimension=dimension,
            status=_combine_status(
                tuple(
                    c.status for c in checks if c.dimension == dimension
                )
            ),
        )
        for dimension in dimension_order
        if any(c.dimension == dimension for c in checks)
    )

    probe = LightingSceneEvaluation.model_construct(
        evaluation_id='',
        scene=scene,
        checks=tuple(checks),
        dimensions=dimensions,
        evaluation_sha256='',
    )
    digest = _hash(probe.semantic_payload())
    return LightingSceneEvaluation(
        **probe.model_dump(
            mode='python',
            exclude={'evaluation_sha256', 'evaluation_id'},
        ),
        evaluation_id='lse-' + digest[:24],
        evaluation_sha256=digest,
    )


def lighting_scene_status(
    evaluation: LightingSceneEvaluation,
    *,
    dimension: LightingEvaluationDimension | None = None,
) -> EvaluationStatus:
    """Fold check statuses — the checks list is the authoritative record.

    With ``dimension`` set, only that dimension's verdict is returned, so a
    caller can ask for the device-state commissioning verdict without the
    intentionally-unsupported photometric capability contaminating it.
    """
    statuses = tuple(
        c.status
        for c in evaluation.checks
        if dimension is None or c.dimension == dimension
    )
    return _combine_status(statuses)
