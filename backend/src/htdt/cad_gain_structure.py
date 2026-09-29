"""Line-level gain structure authority (#646).

Models the signal-level chain *before* power/acoustic headroom: processor
pre-out, external DSP, level converters and amplifier inputs. Each stage is an
immutable evidenced capability (max input/output levels, fixed or ranged gain,
port topology, analog/digital domain); a scenario binds ordered stages to a
document with a requested head level and a required output level; the
evaluation reports per-stage headroom and the chain's limiting interface.

Unit conventions are explicit: analog levels are V RMS (compared in dBV), and
digital levels are dBFS. Domain boundaries are only crossed by stages whose
``input_domain`` differs from ``output_domain`` and that carry an evidenced
``full_scale_v_rms`` (the analog level equivalent to 0 dBFS); anything else
fails closed rather than converting implicitly.
"""

from __future__ import annotations

from math import isfinite, log10
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_amplifier_headroom import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


GAIN_STRUCTURE_SCHEMA_VERSION = 1
LINE_LEVEL_STAGE_AUTHORITY_VERSION = 'line-level-stage-capability-1'
GAIN_STRUCTURE_SCENARIO_VERSION = 'gain-structure-scenario-1'
GAIN_STRUCTURE_EVALUATION_VERSION = 'gain-structure-evaluation-1'

LevelDomain = Literal['analog_v_rms', 'digital_dbfs']
LevelUnit = Literal['V RMS', 'dBFS']
PortTopology = Literal['unbalanced', 'balanced', 'internal', 'unknown']
StageKind = Literal[
    'source_output',
    'processor_pre_out',
    'level_converter',
    'dsp_input',
    'dsp_output',
    'amplifier_input',
    'amplifier_output',
    'integrated',
]
StageResultState = Literal['available', 'clipped', 'unknown']
GainStructureState = Literal['available', 'partial', 'unsupported']

DBU_REFERENCE_V_RMS = 0.7745966692414834  # sqrt(0.6) — 0 dBu = 0.775 V RMS






def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def vrms_to_dbv(value_v_rms: float) -> float:
    """Analog V RMS -> dBV (0 dBV = 1.0 V RMS)."""
    value = _finite(value_v_rms, field_name='V RMS level')
    if value <= 0.0:
        raise ValueError('V RMS level must be > 0')
    return 20.0 * log10(value)


def dbv_to_vrms(value_dbv: float) -> float:
    try:
        return 10.0 ** (_finite(value_dbv, field_name='dBV level') / 20.0)
    except OverflowError as exc:
        raise ValueError('dBV level out of representable range') from exc


def dbu_to_vrms(value_dbu: float) -> float:
    """dBu -> V RMS at the standard 0.775 V reference."""
    try:
        return DBU_REFERENCE_V_RMS * (
            10.0 ** (_finite(value_dbu, field_name='dBu level') / 20.0)
        )
    except OverflowError as exc:
        raise ValueError('dBu level out of representable range') from exc


def dbfs_to_dbv(value_dbfs: float, full_scale_v_rms: float) -> float:
    """dBFS -> dBV through an evidenced 0 dBFS = full_scale_v_rms mapping."""
    dbfs = _finite(value_dbfs, field_name='dBFS level')
    if dbfs > 0.0:
        raise ValueError('dBFS level cannot exceed 0 dBFS')
    return vrms_to_dbv(
        _finite(full_scale_v_rms, field_name='full-scale V RMS')
    ) + dbfs


def dbv_to_dbfs(value_dbv: float, full_scale_v_rms: float) -> float:
    return _finite(value_dbv, field_name='dBV level') - vrms_to_dbv(
        _finite(full_scale_v_rms, field_name='full-scale V RMS')
    )


class LineLevelValue(BaseModel):
    """One explicit line-level quantity; the domain names the convention."""

    model_config = ConfigDict(frozen=True)

    domain: LevelDomain
    value: float

    @field_validator('value')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='line-level value')

    @model_validator(mode='after')
    def valid_value(self) -> 'LineLevelValue':
        if self.domain == 'analog_v_rms' and self.value <= 0.0:
            raise ValueError('analog line level must be > 0 V RMS')
        if self.domain == 'digital_dbfs' and self.value > 0.0:
            raise ValueError('digital line level cannot exceed 0 dBFS')
        return self

    def to_db(self, *, full_scale_v_rms: float | None = None) -> float:
        """Level in the domain's native dB convention (dBV or dBFS)."""
        if self.domain == 'analog_v_rms':
            return vrms_to_dbv(self.value)
        return self.value

    @property
    def db_unit(self) -> LevelUnit:
        return 'V RMS' if self.domain == 'analog_v_rms' else 'dBFS'


class LineLevelStageCapability(BaseModel):
    """Immutable per-stage line-level capability with explicit conventions."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = GAIN_STRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'line-level-stage-capability-1'
    ] = LINE_LEVEL_STAGE_AUTHORITY_VERSION
    stage_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    identity_kind: Literal['manufacturer', 'user_defined']
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    user_label: str | None = Field(default=None, min_length=1)
    port_id: str = Field(min_length=1)
    stage_kind: StageKind
    topology: PortTopology = 'unknown'
    input_domain: LevelDomain
    output_domain: LevelDomain
    maximum_input_level: LineLevelValue | None = None
    maximum_output_level: LineLevelValue | None = None
    nominal_output_level: LineLevelValue | None = None
    gain_db: float | None = None
    gain_db_minimum: float | None = None
    gain_db_maximum: float | None = None
    full_scale_v_rms: float | None = Field(default=None, gt=0.0)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    missing_unsupported_fields: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('gain_db', 'gain_db_minimum', 'gain_db_maximum')
    @classmethod
    def finite_gain(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='stage gain')

    @model_validator(mode='after')
    def valid_stage(self) -> 'LineLevelStageCapability':
        if self.identity_kind == 'manufacturer':
            if self.manufacturer is None or self.model is None:
                raise ValueError(
                    'manufacturer stage identity requires manufacturer and model'
                )
        elif self.user_label is None:
            raise ValueError('user-defined stage identity requires user_label')

        if self.input_domain != self.output_domain:
            if self.stage_kind != 'level_converter':
                raise ValueError(
                    'only level_converter stages may cross analog/digital '
                    'domains'
                )
            if self.full_scale_v_rms is None:
                raise ValueError(
                    'domain-crossing stage requires an evidenced '
                    'full_scale_v_rms mapping'
                )
        elif self.full_scale_v_rms is not None:
            raise ValueError(
                'full_scale_v_rms only applies to domain-crossing stages'
            )

        for level, name in (
            (self.maximum_input_level, 'maximum_input_level'),
            (self.maximum_output_level, 'maximum_output_level'),
            (self.nominal_output_level, 'nominal_output_level'),
        ):
            if level is None:
                continue
            expected = self.input_domain if name == 'maximum_input_level' else (
                self.output_domain
            )
            if level.domain != expected:
                raise ValueError(
                    f'{name} domain must match the stage {expected} domain'
                )

        ranged = self.gain_db_minimum is not None or (
            self.gain_db_maximum is not None
        )
        if self.gain_db is not None and ranged:
            raise ValueError('fixed gain and ranged gain are mutually exclusive')
        if ranged and (
            self.gain_db_minimum is None or self.gain_db_maximum is None
        ):
            raise ValueError('ranged gain requires minimum and maximum')
        if (
            self.gain_db_minimum is not None
            and self.gain_db_maximum is not None
            and self.gain_db_maximum < self.gain_db_minimum
        ):
            raise ValueError('gain range maximum must be >= minimum')
        if self.gain_db is None and not ranged:
            raise ValueError(
                'stage requires fixed gain or an evidenced gain range'
            )
        if len(self.missing_unsupported_fields) != len(
            set(self.missing_unsupported_fields)
        ):
            raise ValueError('missing/unsupported fields must be unique')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('LineLevelStageCapability semantic hash mismatch')
        return self

    @property
    def crosses_domains(self) -> bool:
        return self.input_domain != self.output_domain

    def semantic_payload(self) -> dict[str, Any]:
        def level_payload(level: LineLevelValue | None) -> dict[str, Any] | None:
            return None if level is None else level.model_dump(mode='json')

        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'stage_id': self.stage_id,
            'version': self.version,
            'identity_kind': self.identity_kind,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'user_label': self.user_label,
            'port_id': self.port_id,
            'stage_kind': self.stage_kind,
            'topology': self.topology,
            'input_domain': self.input_domain,
            'output_domain': self.output_domain,
            'maximum_input_level': level_payload(self.maximum_input_level),
            'maximum_output_level': level_payload(self.maximum_output_level),
            'nominal_output_level': level_payload(self.nominal_output_level),
            'gain_db': self.gain_db,
            'gain_db_minimum': self.gain_db_minimum,
            'gain_db_maximum': self.gain_db_maximum,
            'full_scale_v_rms': self.full_scale_v_rms,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'missing_unsupported_fields': list(self.missing_unsupported_fields),
        }


def build_line_level_stage_capability(
    *,
    stage_id: str,
    version: str,
    identity_kind: Literal['manufacturer', 'user_defined'],
    port_id: str,
    stage_kind: StageKind,
    input_domain: LevelDomain,
    output_domain: LevelDomain,
    provenance: Sequence[EquipmentDataProvenance],
    topology: PortTopology = 'unknown',
    manufacturer: str | None = None,
    model: str | None = None,
    user_label: str | None = None,
    maximum_input_level: LineLevelValue | None = None,
    maximum_output_level: LineLevelValue | None = None,
    nominal_output_level: LineLevelValue | None = None,
    gain_db: float | None = None,
    gain_db_minimum: float | None = None,
    gain_db_maximum: float | None = None,
    full_scale_v_rms: float | None = None,
    missing_unsupported_fields: Sequence[str] = (),
) -> LineLevelStageCapability:
    provenance_items = tuple(provenance)
    missing_items = tuple(missing_unsupported_fields)
    payload = {
        'schema_version': GAIN_STRUCTURE_SCHEMA_VERSION,
        'authority_version': LINE_LEVEL_STAGE_AUTHORITY_VERSION,
        'stage_id': stage_id,
        'version': version,
        'identity_kind': identity_kind,
        'manufacturer': manufacturer,
        'model': model,
        'user_label': user_label,
        'port_id': port_id,
        'stage_kind': stage_kind,
        'topology': topology,
        'input_domain': input_domain,
        'output_domain': output_domain,
        'maximum_input_level': (
            None
            if maximum_input_level is None
            else maximum_input_level.model_dump(mode='json')
        ),
        'maximum_output_level': (
            None
            if maximum_output_level is None
            else maximum_output_level.model_dump(mode='json')
        ),
        'nominal_output_level': (
            None
            if nominal_output_level is None
            else nominal_output_level.model_dump(mode='json')
        ),
        'gain_db': gain_db,
        'gain_db_minimum': gain_db_minimum,
        'gain_db_maximum': gain_db_maximum,
        'full_scale_v_rms': full_scale_v_rms,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'missing_unsupported_fields': list(missing_items),
    }
    return LineLevelStageCapability(
        stage_id=stage_id,
        version=version,
        identity_kind=identity_kind,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        port_id=port_id,
        stage_kind=stage_kind,
        topology=topology,
        input_domain=input_domain,
        output_domain=output_domain,
        maximum_input_level=maximum_input_level,
        maximum_output_level=maximum_output_level,
        nominal_output_level=nominal_output_level,
        gain_db=gain_db,
        gain_db_minimum=gain_db_minimum,
        gain_db_maximum=gain_db_maximum,
        full_scale_v_rms=full_scale_v_rms,
        provenance=provenance_items,
        missing_unsupported_fields=missing_items,
        semantic_sha256=_digest(payload),
    )


class GainStructureOperatingState(BaseModel):
    """Mutable-state snapshot (volume/trims) bound into one scenario."""

    model_config = ConfigDict(frozen=True)

    master_volume_db: float | None = None
    channel_trim_db: float | None = None
    stage_gain_db: tuple[tuple[str, float], ...] = ()

    @field_validator('master_volume_db', 'channel_trim_db')
    @classmethod
    def finite_trim(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='operating state trim')

    @model_validator(mode='after')
    def valid_state(self) -> 'GainStructureOperatingState':
        stage_ids = [stage_id for stage_id, _db in self.stage_gain_db]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError('stage gain overrides must be unique per stage')
        for _stage_id, value in self.stage_gain_db:
            _finite(value, field_name='stage gain override')
        return self

    def gain_override(self, stage_id: str) -> float | None:
        for bound_id, value in self.stage_gain_db:
            if bound_id == stage_id:
                return value
        return None


class GainStructureScenario(BaseModel):
    """Ordered stage binding plus operating state, hashed as one authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = GAIN_STRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'gain-structure-scenario-1'
    ] = GAIN_STRUCTURE_SCENARIO_VERSION
    document_id: str = Field(min_length=1)
    stages: tuple[AuthorityRef, ...] = Field(min_length=1)
    head_input: LineLevelValue
    required_output: LineLevelValue
    operating_state: GainStructureOperatingState | None = None
    playback_chain_scenario: AuthorityRef | None = None
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_scenario(self) -> 'GainStructureScenario':
        stage_ids = [ref.authority_id for ref in self.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError('gain-structure stages must be unique')
        if self.scenario_sha256 != _digest(self.semantic_payload()):
            raise ValueError('GainStructureScenario semantic hash mismatch')
        if self.scenario_id != _semantic_id(
            'gain-structure', self.scenario_sha256
        ):
            raise ValueError(
                'GainStructureScenario ID does not match semantic hash'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'stages': [ref.model_dump(mode='json') for ref in self.stages],
            'head_input': self.head_input.model_dump(mode='json'),
            'required_output': self.required_output.model_dump(mode='json'),
            'operating_state': (
                None
                if self.operating_state is None
                else self.operating_state.model_dump(mode='json')
            ),
            'playback_chain_scenario': (
                None
                if self.playback_chain_scenario is None
                else self.playback_chain_scenario.model_dump(mode='json')
            ),
        }


def build_gain_structure_scenario(
    *,
    document_id: str,
    stages: Sequence[LineLevelStageCapability],
    head_input: LineLevelValue,
    required_output: LineLevelValue,
    operating_state: GainStructureOperatingState | None = None,
    playback_chain_scenario: AuthorityRef | None = None,
) -> GainStructureScenario:
    stage_refs = tuple(
        AuthorityRef(
            authority_id=stage.stage_id,
            version=stage.version,
            semantic_sha256=stage.semantic_sha256,
        )
        for stage in stages
    )
    payload = {
        'schema_version': GAIN_STRUCTURE_SCHEMA_VERSION,
        'authority_version': GAIN_STRUCTURE_SCENARIO_VERSION,
        'document_id': document_id,
        'stages': [ref.model_dump(mode='json') for ref in stage_refs],
        'head_input': head_input.model_dump(mode='json'),
        'required_output': required_output.model_dump(mode='json'),
        'operating_state': (
            None
            if operating_state is None
            else operating_state.model_dump(mode='json')
        ),
        'playback_chain_scenario': (
            None
            if playback_chain_scenario is None
            else playback_chain_scenario.model_dump(mode='json')
        ),
    }
    digest = _digest(payload)
    return GainStructureScenario(
        document_id=document_id,
        stages=stage_refs,
        head_input=head_input,
        required_output=required_output,
        operating_state=operating_state,
        playback_chain_scenario=playback_chain_scenario,
        scenario_id=_semantic_id('gain-structure', digest),
        scenario_sha256=digest,
    )


class GainStructureStageResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: AuthorityRef
    input_level_db: float | None = None
    output_level_db: float | None = None
    applied_gain_db: float | None = None
    input_headroom_db: float | None = None
    output_headroom_db: float | None = None
    state: StageResultState
    reason: str | None = None

    @field_validator(
        'input_level_db',
        'output_level_db',
        'applied_gain_db',
        'input_headroom_db',
        'output_headroom_db',
    )
    @classmethod
    def finite_result(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='gain-structure stage result')

    @model_validator(mode='after')
    def valid_result(self) -> 'GainStructureStageResult':
        if self.state == 'available' and self.reason is not None:
            raise ValueError('available stage result must not carry reason')
        if self.state != 'available' and self.reason is None:
            raise ValueError('non-available stage result requires reason')
        if self.state == 'available' and (
            self.input_level_db is None or self.output_level_db is None
        ):
            raise ValueError(
                'available stage result requires resolved input/output levels'
            )
        return self


class GainStructureEvaluation(BaseModel):
    """Immutable per-stage headroom evidence for one scenario."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = GAIN_STRUCTURE_SCHEMA_VERSION
    authority_version: Literal[
        'gain-structure-evaluation-1'
    ] = GAIN_STRUCTURE_EVALUATION_VERSION
    scenario: GainStructureScenario
    stage_results: tuple[GainStructureStageResult, ...]
    delivered_output_level_db: float | None = None
    required_output_level_db: float | None = None
    output_margin_db: float | None = None
    minimum_headroom_db: float | None = None
    limiting_stage_id: str | None = None
    state: GainStructureState
    support_reasons: tuple[str, ...]
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'GainStructureEvaluation':
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('GainStructureEvaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('gain-structure-eval', digest):
            raise ValueError(
                'GainStructureEvaluation ID does not match semantic hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'scenario': self.scenario.model_dump(mode='json'),
            'stage_results': [
                result.model_dump(mode='json') for result in self.stage_results
            ],
            'delivered_output_level_db': self.delivered_output_level_db,
            'required_output_level_db': self.required_output_level_db,
            'output_margin_db': self.output_margin_db,
            'minimum_headroom_db': self.minimum_headroom_db,
            'limiting_stage_id': self.limiting_stage_id,
            'state': self.state,
            'support_reasons': list(self.support_reasons),
        }


def _applied_gain(
    stage: LineLevelStageCapability,
    scenario: GainStructureScenario,
) -> tuple[float | None, str | None]:
    if scenario.operating_state is not None:
        override = scenario.operating_state.gain_override(stage.stage_id)
        if override is not None:
            if (
                stage.gain_db_minimum is not None
                and stage.gain_db_maximum is not None
                and not (
                    stage.gain_db_minimum <= override <= stage.gain_db_maximum
                )
            ):
                raise ValueError(
                    f'operating gain for stage {stage.stage_id} is outside '
                    'its evidenced gain range'
                )
            if stage.gain_db is not None and abs(override - stage.gain_db) > 1e-9:
                raise ValueError(
                    f'operating gain for stage {stage.stage_id} contradicts '
                    'its evidenced fixed gain'
                )
            return override, None
    if stage.gain_db is not None:
        return stage.gain_db, None
    return None, 'stage gain is ranged; no operating-state gain was supplied'


def evaluate_gain_structure(
    *,
    scenario: GainStructureScenario,
    stages: Sequence[LineLevelStageCapability],
) -> GainStructureEvaluation:
    """Propagate the head level through ordered stages and report headroom.

    Levels are compared in each domain's native dB convention (dBV / dBFS);
    domain boundaries only cross at level_converter stages through their
    evidenced full_scale_v_rms mapping. A stage whose gain or input level
    cannot be resolved reports 'unknown' and downstream stages stay unknown —
    nothing is extrapolated past unevidenced evidence.
    """
    stage_list = list(stages)
    if len(stage_list) != len(scenario.stages):
        raise ValueError('resolved stage count must match the scenario')
    for ref, stage in zip(scenario.stages, stage_list):
        if (
            stage.stage_id != ref.authority_id
            or stage.version != ref.version
            or stage.semantic_sha256 != ref.semantic_sha256
        ):
            raise ValueError(
                'resolved stage does not match the scenario binding exactly'
            )

    reasons: list[str] = []
    results: list[GainStructureStageResult] = []
    level_db: float | None = scenario.head_input.to_db()
    current_domain: LevelDomain = scenario.head_input.domain
    if current_domain != stage_list[0].input_domain:
        raise ValueError(
            'head input domain does not match the first stage input domain'
        )

    minimum_headroom: float | None = None
    limiting_stage_id: str | None = None
    state: GainStructureState = 'available'
    unknown_tail = False

    def track(headroom: float | None, stage: LineLevelStageCapability) -> None:
        nonlocal minimum_headroom, limiting_stage_id
        if headroom is None:
            return
        if minimum_headroom is None or headroom < minimum_headroom:
            minimum_headroom = headroom
            limiting_stage_id = stage.stage_id

    for ref, stage in zip(scenario.stages, stage_list):
        if unknown_tail:
            results.append(
                GainStructureStageResult(
                    stage=ref,
                    state='unknown',
                    reason='upstream stage level is unresolved',
                )
            )
            continue

        if current_domain != stage.input_domain:
            raise ValueError(
                f'stage {stage.stage_id} input domain does not match the '
                'upstream output domain'
            )

        assert level_db is not None
        gain, gain_reason = _applied_gain(stage, scenario)
        input_headroom: float | None = None
        if stage.maximum_input_level is not None:
            input_headroom = (
                stage.maximum_input_level.to_db(
                    full_scale_v_rms=stage.full_scale_v_rms
                )
                - level_db
            )
            track(input_headroom, stage)
            if input_headroom < 0:
                state = 'unsupported'
                reasons.append(
                    f'input level exceeds the evidenced maximum input of '
                    f'stage {stage.stage_id}'
                )
        if gain is None:
            unknown_tail = True
            results.append(
                GainStructureStageResult(
                    stage=ref,
                    input_level_db=level_db,
                    input_headroom_db=input_headroom,
                    state='unknown',
                    reason=gain_reason,
                )
            )
            reasons.append(
                f'stage {stage.stage_id}: {gain_reason}'
            )
            if state == 'available':
                state = 'partial'
            continue

        output_domain = stage.output_domain
        if stage.crosses_domains:
            assert stage.full_scale_v_rms is not None
            if stage.input_domain == 'digital_dbfs':
                # 0 dBFS = full_scale_v_rms; gain applies across the boundary
                output_db = (
                    vrms_to_dbv(stage.full_scale_v_rms) + level_db + gain
                )
            else:
                output_db = level_db + gain - vrms_to_dbv(
                    stage.full_scale_v_rms
                )
        else:
            output_db = level_db + gain

        output_headroom: float | None = None
        stage_state: StageResultState = 'available'
        stage_reason: str | None = None
        if stage.maximum_output_level is not None:
            if stage.maximum_output_level.domain != output_domain:
                raise ValueError(
                    f'stage {stage.stage_id} maximum output level domain '
                    'does not match its output domain'
                )
            output_headroom = (
                stage.maximum_output_level.to_db(
                    full_scale_v_rms=stage.full_scale_v_rms
                )
                - output_db
            )
            track(output_headroom, stage)
            if output_headroom < 0:
                stage_state = 'clipped'
                stage_reason = (
                    'output level exceeds the evidenced maximum output'
                )
                state = 'unsupported'
                reasons.append(
                    f'output of stage {stage.stage_id} exceeds its evidenced '
                    'maximum'
                )
            if input_headroom is not None and input_headroom < 0:
                stage_state = 'clipped'
                stage_reason = (
                    'input level exceeds the evidenced maximum input'
                )
        results.append(
            GainStructureStageResult(
                stage=ref,
                input_level_db=level_db,
                output_level_db=output_db,
                applied_gain_db=gain,
                input_headroom_db=input_headroom,
                output_headroom_db=output_headroom,
                state=stage_state,
                reason=stage_reason,
            )
        )
        level_db = output_db
        current_domain = output_domain

    delivered_db: float | None = level_db if not unknown_tail else None
    required_db: float | None = None
    output_margin: float | None = None
    if delivered_db is not None:
        last_domain = current_domain
        if scenario.required_output.domain != last_domain:
            raise ValueError(
                'required output domain does not match the last stage output '
                'domain'
            )
        required_db = scenario.required_output.to_db()
        output_margin = delivered_db - required_db
        if output_margin < 0:
            state = 'unsupported'
            reasons.append(
                'delivered output level is below the required output level'
            )
    elif scenario.required_output is not None:
        required_db = scenario.required_output.to_db()

    payload = {
        'schema_version': GAIN_STRUCTURE_SCHEMA_VERSION,
        'authority_version': GAIN_STRUCTURE_EVALUATION_VERSION,
        'scenario': scenario.model_dump(mode='json'),
        'stage_results': [result.model_dump(mode='json') for result in results],
        'delivered_output_level_db': delivered_db,
        'required_output_level_db': required_db,
        'output_margin_db': output_margin,
        'minimum_headroom_db': minimum_headroom,
        'limiting_stage_id': limiting_stage_id,
        'state': state,
        'support_reasons': list(reasons),
    }
    digest = _digest(payload)
    return GainStructureEvaluation(
        scenario=scenario,
        stage_results=tuple(results),
        delivered_output_level_db=delivered_db,
        required_output_level_db=required_db,
        output_margin_db=output_margin,
        minimum_headroom_db=minimum_headroom,
        limiting_stage_id=limiting_stage_id,
        state=state,
        support_reasons=tuple(reasons),
        evaluation_id=_semantic_id('gain-structure-eval', digest),
        evaluation_sha256=digest,
    )


__all__ = [
    'DBU_REFERENCE_V_RMS',
    'GAIN_STRUCTURE_EVALUATION_VERSION',
    'GAIN_STRUCTURE_SCHEMA_VERSION',
    'GAIN_STRUCTURE_SCENARIO_VERSION',
    'LINE_LEVEL_STAGE_AUTHORITY_VERSION',
    'GainStructureEvaluation',
    'GainStructureOperatingState',
    'GainStructureScenario',
    'GainStructureStageResult',
    'GainStructureState',
    'LevelDomain',
    'LineLevelStageCapability',
    'LineLevelValue',
    'PortTopology',
    'StageKind',
    'StageResultState',
    'build_gain_structure_scenario',
    'build_line_level_stage_capability',
    'dbfs_to_dbv',
    'dbu_to_vrms',
    'dbv_to_dbfs',
    'dbv_to_vrms',
    'evaluate_gain_structure',
    'vrms_to_dbv',
]
