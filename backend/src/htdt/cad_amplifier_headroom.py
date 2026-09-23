from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite, log10, sqrt
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_direct_level import DirectLevelFrequencyBand
from .cad_equipment import (
    EquipmentDataProvenance,
    EquipmentDefinition,
    EquipmentUncertainty,
    FrequencyDomain,
)
from .cad_repository import SceneRevision
from .cad_system_variant import SystemVariant, materialize_system_variant
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveState,
    ObjectiveValidDomain,
    ObjectiveVector,
)


AMPLIFIER_HEADROOM_SCHEMA_VERSION = 1
AMPLIFIER_OUTPUT_AUTHORITY_VERSION = 'o100d-amplifier-output-capability-1'
SPEAKER_LOAD_AUTHORITY_VERSION = 'o100d-speaker-electrical-load-reference-1'
PLAYBACK_CHAIN_AUTHORITY_VERSION = 'o100d-playback-chain-scenario-1'
PLAYBACK_CHAIN_EVALUATION_VERSION = 'o100d-amplifier-electrical-headroom-1'
OBJECTIVE_COMPARISON_MODEL_ID = 'o100d-amplifier-electrical-headroom'

ElectricalQuantity = Literal['voltage_v_rms', 'power_w']
ElectricalUnit = Literal['V RMS', 'W']
LimiterState = Literal['amplifier', 'speaker', 'equal', 'unknown']
LoadSemantics = Literal['exact_resistive_reference', 'nominal_impedance_only']


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


def _unit(quantity: ElectricalQuantity) -> ElectricalUnit:
    return 'V RMS' if quantity == 'voltage_v_rms' else 'W'


class ElectricalValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: ElectricalQuantity
    value: float = Field(gt=0.0)

    @field_validator('value')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='electrical value')


class AmplifierLoadDomain(BaseModel):
    """Load domain for which an amplifier capability was actually evidenced."""

    model_config = ConfigDict(frozen=True)

    semantics: Literal['exact_resistive_reference'] = 'exact_resistive_reference'
    reference_load_ohm: float = Field(gt=0.0)
    minimum_load_ohm: float = Field(gt=0.0)
    maximum_load_ohm: float = Field(gt=0.0)

    @field_validator('reference_load_ohm', 'minimum_load_ohm', 'maximum_load_ohm')
    @classmethod
    def finite_load(cls, value: float) -> float:
        return _finite(value, field_name='load')

    @model_validator(mode='after')
    def valid_domain(self) -> 'AmplifierLoadDomain':
        if self.maximum_load_ohm < self.minimum_load_ohm:
            raise ValueError('amplifier load-domain maximum must be >= minimum')
        if not (
            self.minimum_load_ohm
            <= self.reference_load_ohm
            <= self.maximum_load_ohm
        ):
            raise ValueError('reference load must lie inside amplifier load domain')
        return self

    def contains(self, resistance_ohm: float) -> bool:
        resistance = _finite(resistance_ohm, field_name='resistance_ohm')
        return self.minimum_load_ohm <= resistance <= self.maximum_load_ohm


class AmplifierChannelCountCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    simultaneous_channel_count: int = Field(ge=1)
    shared_supply_evidence: bool = False
    condition_description: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_shared_supply(self) -> 'AmplifierChannelCountCondition':
        if self.simultaneous_channel_count > 1 and not self.shared_supply_evidence:
            raise ValueError(
                'multi-channel capability requires explicit shared-supply evidence'
            )
        return self


class AmplifierOutputCapability(BaseModel):
    """Immutable amplifier/output-channel capability with explicit load conditions."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = AMPLIFIER_HEADROOM_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-amplifier-output-capability-1'
    ] = AMPLIFIER_OUTPUT_AUTHORITY_VERSION
    capability_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    identity_kind: Literal['manufacturer', 'user_defined']
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    user_label: str | None = Field(default=None, min_length=1)
    output_id: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    supported_load: AmplifierLoadDomain
    continuous_capability: ElectricalValue | None = None
    peak_capability: ElectricalValue | None = None
    continuous_duration_s: float | None = Field(default=None, gt=0.0)
    peak_duration_s: float | None = Field(default=None, gt=0.0)
    gain_db: float | None = None
    reference_input: ElectricalValue | None = None
    clipping_reference_definition: str = Field(min_length=1)
    valid_frequency_band: FrequencyDomain
    weighting: str = Field(min_length=1)
    channel_count_condition: AmplifierChannelCountCondition
    uncertainty: tuple[EquipmentUncertainty, ...] = ()
    missing_unsupported_fields: tuple[str, ...] = ()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('continuous_duration_s', 'peak_duration_s', 'gain_db')
    @classmethod
    def finite_optional(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='amplifier capability value')

    @model_validator(mode='after')
    def valid_capability(self) -> 'AmplifierOutputCapability':
        if self.identity_kind == 'manufacturer':
            if self.manufacturer is None or self.model is None:
                raise ValueError(
                    'manufacturer amplifier identity requires manufacturer and model'
                )
        elif self.user_label is None:
            raise ValueError('user-defined amplifier identity requires user_label')

        if self.continuous_capability is None and self.peak_capability is None:
            raise ValueError(
                'amplifier output capability requires continuous or peak evidence'
            )
        if (self.continuous_capability is None) != (
            self.continuous_duration_s is None
        ):
            raise ValueError(
                'continuous capability and continuous duration must be supplied together'
            )
        if (self.peak_capability is None) != (self.peak_duration_s is None):
            raise ValueError(
                'peak capability and peak duration must be supplied together'
            )
        if self.gain_db is not None and self.reference_input is None:
            raise ValueError('evidenced amplifier gain requires reference input')
        if len(self.missing_unsupported_fields) != len(
            set(self.missing_unsupported_fields)
        ):
            raise ValueError('missing/unsupported fields must be unique')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('AmplifierOutputCapability semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'capability_id': self.capability_id,
            'version': self.version,
            'identity_kind': self.identity_kind,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'user_label': self.user_label,
            'output_id': self.output_id,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'supported_load': self.supported_load.model_dump(mode='json'),
            'continuous_capability': (
                None
                if self.continuous_capability is None
                else self.continuous_capability.model_dump(mode='json')
            ),
            'peak_capability': (
                None
                if self.peak_capability is None
                else self.peak_capability.model_dump(mode='json')
            ),
            'continuous_duration_s': self.continuous_duration_s,
            'peak_duration_s': self.peak_duration_s,
            'gain_db': self.gain_db,
            'reference_input': (
                None
                if self.reference_input is None
                else self.reference_input.model_dump(mode='json')
            ),
            'clipping_reference_definition': self.clipping_reference_definition,
            'valid_frequency_band': self.valid_frequency_band.model_dump(mode='json'),
            'weighting': self.weighting,
            'channel_count_condition': self.channel_count_condition.model_dump(
                mode='json'
            ),
            'uncertainty': [item.model_dump(mode='json') for item in self.uncertainty],
            'missing_unsupported_fields': list(self.missing_unsupported_fields),
        }


def build_amplifier_output_capability(
    *,
    capability_id: str,
    version: str,
    identity_kind: Literal['manufacturer', 'user_defined'],
    output_id: str,
    provenance: Sequence[EquipmentDataProvenance],
    supported_load: AmplifierLoadDomain,
    clipping_reference_definition: str,
    valid_frequency_band: FrequencyDomain,
    weighting: str,
    channel_count_condition: AmplifierChannelCountCondition,
    manufacturer: str | None = None,
    model: str | None = None,
    user_label: str | None = None,
    continuous_capability: ElectricalValue | None = None,
    peak_capability: ElectricalValue | None = None,
    continuous_duration_s: float | None = None,
    peak_duration_s: float | None = None,
    gain_db: float | None = None,
    reference_input: ElectricalValue | None = None,
    uncertainty: Sequence[EquipmentUncertainty] = (),
    missing_unsupported_fields: Sequence[str] = (),
) -> AmplifierOutputCapability:
    provenance_items = tuple(provenance)
    uncertainty_items = tuple(uncertainty)
    missing_items = tuple(missing_unsupported_fields)
    continuous_duration_value = (
        None if continuous_duration_s is None else float(continuous_duration_s)
    )
    peak_duration_value = (
        None if peak_duration_s is None else float(peak_duration_s)
    )
    gain_value = None if gain_db is None else float(gain_db)
    payload = {
        'schema_version': AMPLIFIER_HEADROOM_SCHEMA_VERSION,
        'authority_version': AMPLIFIER_OUTPUT_AUTHORITY_VERSION,
        'capability_id': capability_id,
        'version': version,
        'identity_kind': identity_kind,
        'manufacturer': manufacturer,
        'model': model,
        'user_label': user_label,
        'output_id': output_id,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'supported_load': supported_load.model_dump(mode='json'),
        'continuous_capability': (
            None
            if continuous_capability is None
            else continuous_capability.model_dump(mode='json')
        ),
        'peak_capability': (
            None
            if peak_capability is None
            else peak_capability.model_dump(mode='json')
        ),
        'continuous_duration_s': continuous_duration_value,
        'peak_duration_s': peak_duration_value,
        'gain_db': gain_value,
        'reference_input': (
            None if reference_input is None else reference_input.model_dump(mode='json')
        ),
        'clipping_reference_definition': clipping_reference_definition,
        'valid_frequency_band': valid_frequency_band.model_dump(mode='json'),
        'weighting': weighting,
        'channel_count_condition': channel_count_condition.model_dump(mode='json'),
        'uncertainty': [item.model_dump(mode='json') for item in uncertainty_items],
        'missing_unsupported_fields': list(missing_items),
    }
    return AmplifierOutputCapability(
        capability_id=capability_id,
        version=version,
        identity_kind=identity_kind,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        output_id=output_id,
        provenance=provenance_items,
        supported_load=supported_load,
        continuous_capability=continuous_capability,
        peak_capability=peak_capability,
        continuous_duration_s=continuous_duration_value,
        peak_duration_s=peak_duration_value,
        gain_db=gain_value,
        reference_input=reference_input,
        clipping_reference_definition=clipping_reference_definition,
        valid_frequency_band=valid_frequency_band,
        weighting=weighting,
        channel_count_condition=channel_count_condition,
        uncertainty=uncertainty_items,
        missing_unsupported_fields=missing_items,
        semantic_sha256=_digest(payload),
    )


class SpeakerElectricalLoadAuthority(BaseModel):
    """Explicit speaker load reference. Nominal impedance is never an exact load model."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = AMPLIFIER_HEADROOM_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-speaker-electrical-load-reference-1'
    ] = SPEAKER_LOAD_AUTHORITY_VERSION
    load_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantics: LoadSemantics
    resistance_ohm: float = Field(gt=0.0)
    valid_frequency_band: FrequencyDomain
    provenance: EquipmentDataProvenance
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('resistance_ohm')
    @classmethod
    def finite_resistance(cls, value: float) -> float:
        return _finite(value, field_name='speaker load resistance')

    @model_validator(mode='after')
    def valid_identity(self) -> 'SpeakerElectricalLoadAuthority':
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('SpeakerElectricalLoadAuthority semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'load_id': self.load_id,
            'version': self.version,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'semantics': self.semantics,
            'resistance_ohm': self.resistance_ohm,
            'valid_frequency_band': self.valid_frequency_band.model_dump(mode='json'),
            'provenance': self.provenance.model_dump(mode='json'),
        }


def build_speaker_electrical_load_authority(
    *,
    load_id: str,
    version: str,
    equipment_definition: EquipmentDefinition,
    semantics: LoadSemantics,
    resistance_ohm: float,
    valid_frequency_band: FrequencyDomain,
    provenance: EquipmentDataProvenance,
) -> SpeakerElectricalLoadAuthority:
    payload = {
        'schema_version': AMPLIFIER_HEADROOM_SCHEMA_VERSION,
        'authority_version': SPEAKER_LOAD_AUTHORITY_VERSION,
        'load_id': load_id,
        'version': version,
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'semantics': semantics,
        'resistance_ohm': float(resistance_ohm),
        'valid_frequency_band': valid_frequency_band.model_dump(mode='json'),
        'provenance': provenance.model_dump(mode='json'),
    }
    return SpeakerElectricalLoadAuthority(
        load_id=load_id,
        version=version,
        equipment_definition_id=equipment_definition.definition_id,
        equipment_definition_version=equipment_definition.version,
        equipment_definition_sha256=equipment_definition.semantic_sha256,
        semantics=semantics,
        resistance_ohm=resistance_ohm,
        valid_frequency_band=valid_frequency_band,
        provenance=provenance,
        semantic_sha256=_digest(payload),
    )


class AuthorityRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class PlaybackRouting(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_entity_id: str = Field(min_length=1)
    channel_role_id: str = Field(min_length=1)
    amplifier_output_id: str = Field(min_length=1)


class SimultaneousChannelCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    output_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_outputs(self) -> 'SimultaneousChannelCondition':
        if len(self.output_ids) != len(set(self.output_ids)):
            raise ValueError('simultaneous output IDs must be unique')
        return self

    @property
    def channel_count(self) -> int:
        return len(self.output_ids)


class PlaybackChainScenario(BaseModel):
    """Exact playback-chain scenario; identity and comparison semantics are separate."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = AMPLIFIER_HEADROOM_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-playback-chain-scenario-1'
    ] = PLAYBACK_CHAIN_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_equipment: AuthorityRef
    amplifier_capability: AuthorityRef
    speaker_load: AuthorityRef | None = None
    speaker_load_semantics: LoadSemantics | None = None
    speaker_load_resistance_ohm: float | None = Field(default=None, gt=0.0)
    routing: PlaybackRouting
    requested_input: ElectricalValue
    requested_output_quantity: ElectricalQuantity
    requested_continuous_output_value: float = Field(gt=0.0)
    requested_peak_output_value: float = Field(gt=0.0)
    continuous_duration_s: float = Field(gt=0.0)
    peak_duration_s: float = Field(gt=0.0)
    frequency_band: DirectLevelFrequencyBand
    weighting: str = Field(min_length=1)
    target_spl_db_spl: float
    target_reference_condition: str = Field(min_length=1)
    acoustic_target_distance_m: float = Field(gt=0.0)
    target_mode: Literal['continuous', 'peak']
    simultaneous_channel_condition: SimultaneousChannelCondition
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    comparison_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator(
        'requested_continuous_output_value',
        'requested_peak_output_value',
        'continuous_duration_s',
        'peak_duration_s',
        'target_spl_db_spl',
        'acoustic_target_distance_m',
    )
    @classmethod
    def finite_scenario_value(cls, value: float) -> float:
        return _finite(value, field_name='playback-chain scenario value')

    @model_validator(mode='after')
    def valid_identity(self) -> 'PlaybackChainScenario':
        load_parts = (
            self.speaker_load is not None,
            self.speaker_load_semantics is not None,
            self.speaker_load_resistance_ohm is not None,
        )
        if any(load_parts) and not all(load_parts):
            raise ValueError(
                'speaker load ref/semantics/resistance must be supplied together'
            )
        if (
            self.routing.amplifier_output_id
            not in self.simultaneous_channel_condition.output_ids
        ):
            raise ValueError(
                'routed amplifier output must be in simultaneous channel condition'
            )
        digest = _digest(self.semantic_payload())
        if self.scenario_sha256 != digest:
            raise ValueError('PlaybackChainScenario semantic hash mismatch')
        if self.scenario_id != _semantic_id('playback-chain', digest):
            raise ValueError('PlaybackChainScenario ID does not match semantic hash')
        if self.comparison_sha256 != _digest(self.comparison_payload()):
            raise ValueError('PlaybackChainScenario comparison hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'variant_id': self.variant_id,
            'variant_sha256': self.variant_sha256,
            'source_equipment': self.source_equipment.model_dump(mode='json'),
            'amplifier_capability': self.amplifier_capability.model_dump(mode='json'),
            'speaker_load': (
                None
                if self.speaker_load is None
                else self.speaker_load.model_dump(mode='json')
            ),
            'speaker_load_semantics': self.speaker_load_semantics,
            'speaker_load_resistance_ohm': self.speaker_load_resistance_ohm,
            'routing': self.routing.model_dump(mode='json'),
            'requested_input': self.requested_input.model_dump(mode='json'),
            'requested_output_quantity': self.requested_output_quantity,
            'requested_continuous_output_value': (
                self.requested_continuous_output_value
            ),
            'requested_peak_output_value': self.requested_peak_output_value,
            'continuous_duration_s': self.continuous_duration_s,
            'peak_duration_s': self.peak_duration_s,
            'frequency_band': self.frequency_band.model_dump(mode='json'),
            'weighting': self.weighting,
            'target_spl_db_spl': self.target_spl_db_spl,
            'target_reference_condition': self.target_reference_condition,
            'acoustic_target_distance_m': self.acoustic_target_distance_m,
            'target_mode': self.target_mode,
            'simultaneous_channel_condition': (
                self.simultaneous_channel_condition.model_dump(mode='json')
            ),
        }

    def comparison_payload(self) -> dict[str, Any]:
        return {
            'comparison_contract': 'o100d-amplifier-electrical-headroom-comparison-1',
            'channel_role_id': self.routing.channel_role_id,
            'speaker_load_semantics': self.speaker_load_semantics,
            'speaker_load_resistance_ohm': self.speaker_load_resistance_ohm,
            'requested_input': self.requested_input.model_dump(mode='json'),
            'requested_output_quantity': self.requested_output_quantity,
            'requested_continuous_output_value': (
                self.requested_continuous_output_value
            ),
            'requested_peak_output_value': self.requested_peak_output_value,
            'continuous_duration_s': self.continuous_duration_s,
            'peak_duration_s': self.peak_duration_s,
            'frequency_band': self.frequency_band.model_dump(mode='json'),
            'weighting': self.weighting,
            'target_spl_db_spl': self.target_spl_db_spl,
            'target_reference_condition': self.target_reference_condition,
            'acoustic_target_distance_m': self.acoustic_target_distance_m,
            'target_mode': self.target_mode,
            'simultaneous_channel_count': (
                self.simultaneous_channel_condition.channel_count
            ),
        }


def build_playback_chain_scenario(
    *,
    revision: SceneRevision,
    variant: SystemVariant,
    source_equipment: EquipmentDefinition,
    amplifier_capability: AmplifierOutputCapability,
    speaker_load: SpeakerElectricalLoadAuthority | None,
    routing: PlaybackRouting,
    requested_input: ElectricalValue,
    requested_output_quantity: ElectricalQuantity,
    requested_continuous_output_value: float,
    requested_peak_output_value: float,
    continuous_duration_s: float,
    peak_duration_s: float,
    frequency_band: DirectLevelFrequencyBand,
    weighting: str,
    target_spl_db_spl: float,
    target_reference_condition: str,
    acoustic_target_distance_m: float,
    target_mode: Literal['continuous', 'peak'],
    simultaneous_channel_condition: SimultaneousChannelCondition,
) -> PlaybackChainScenario:
    source_ref = AuthorityRef(
        authority_id=source_equipment.definition_id,
        version=source_equipment.version,
        semantic_sha256=source_equipment.semantic_sha256,
    )
    amplifier_ref = AuthorityRef(
        authority_id=amplifier_capability.capability_id,
        version=amplifier_capability.version,
        semantic_sha256=amplifier_capability.semantic_sha256,
    )
    load_ref = (
        None
        if speaker_load is None
        else AuthorityRef(
            authority_id=speaker_load.load_id,
            version=speaker_load.version,
            semantic_sha256=speaker_load.semantic_sha256,
        )
    )
    prototype = {
        'schema_version': AMPLIFIER_HEADROOM_SCHEMA_VERSION,
        'authority_version': PLAYBACK_CHAIN_AUTHORITY_VERSION,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'source_equipment': source_ref.model_dump(mode='json'),
        'amplifier_capability': amplifier_ref.model_dump(mode='json'),
        'speaker_load': (
            None if load_ref is None else load_ref.model_dump(mode='json')
        ),
        'speaker_load_semantics': (
            None if speaker_load is None else speaker_load.semantics
        ),
        'speaker_load_resistance_ohm': (
            None if speaker_load is None else float(speaker_load.resistance_ohm)
        ),
        'routing': routing.model_dump(mode='json'),
        'requested_input': requested_input.model_dump(mode='json'),
        'requested_output_quantity': requested_output_quantity,
        'requested_continuous_output_value': float(
            requested_continuous_output_value
        ),
        'requested_peak_output_value': float(requested_peak_output_value),
        'continuous_duration_s': float(continuous_duration_s),
        'peak_duration_s': float(peak_duration_s),
        'frequency_band': frequency_band.model_dump(mode='json'),
        'weighting': weighting,
        'target_spl_db_spl': float(target_spl_db_spl),
        'target_reference_condition': target_reference_condition,
        'acoustic_target_distance_m': float(acoustic_target_distance_m),
        'target_mode': target_mode,
        'simultaneous_channel_condition': (
            simultaneous_channel_condition.model_dump(mode='json')
        ),
    }
    comparison = {
        'comparison_contract': 'o100d-amplifier-electrical-headroom-comparison-1',
        'channel_role_id': routing.channel_role_id,
        'speaker_load_semantics': (
            None if speaker_load is None else speaker_load.semantics
        ),
        'speaker_load_resistance_ohm': (
            None if speaker_load is None else float(speaker_load.resistance_ohm)
        ),
        'requested_input': requested_input.model_dump(mode='json'),
        'requested_output_quantity': requested_output_quantity,
        'requested_continuous_output_value': float(
            requested_continuous_output_value
        ),
        'requested_peak_output_value': float(requested_peak_output_value),
        'continuous_duration_s': float(continuous_duration_s),
        'peak_duration_s': float(peak_duration_s),
        'frequency_band': frequency_band.model_dump(mode='json'),
        'weighting': weighting,
        'target_spl_db_spl': float(target_spl_db_spl),
        'target_reference_condition': target_reference_condition,
        'acoustic_target_distance_m': float(acoustic_target_distance_m),
        'target_mode': target_mode,
        'simultaneous_channel_count': simultaneous_channel_condition.channel_count,
    }
    digest = _digest(prototype)
    return PlaybackChainScenario(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        source_equipment=source_ref,
        amplifier_capability=amplifier_ref,
        speaker_load=load_ref,
        speaker_load_semantics=(
            None if speaker_load is None else speaker_load.semantics
        ),
        speaker_load_resistance_ohm=(
            None if speaker_load is None else speaker_load.resistance_ohm
        ),
        routing=routing,
        requested_input=requested_input,
        requested_output_quantity=requested_output_quantity,
        requested_continuous_output_value=requested_continuous_output_value,
        requested_peak_output_value=requested_peak_output_value,
        continuous_duration_s=continuous_duration_s,
        peak_duration_s=peak_duration_s,
        frequency_band=frequency_band,
        weighting=weighting,
        target_spl_db_spl=target_spl_db_spl,
        target_reference_condition=target_reference_condition,
        acoustic_target_distance_m=acoustic_target_distance_m,
        target_mode=target_mode,
        simultaneous_channel_condition=simultaneous_channel_condition,
        scenario_id=_semantic_id('playback-chain', digest),
        scenario_sha256=digest,
        comparison_sha256=_digest(comparison),
    )


class PlaybackChainScalarResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: ObjectiveState
    value: float | None
    unit: Literal['V RMS', 'W', 'dB', 'dB SPL']
    reason: str | None = None

    @model_validator(mode='after')
    def valid_state(self) -> 'PlaybackChainScalarResult':
        if self.state == 'available':
            if self.value is None or not isfinite(float(self.value)):
                raise ValueError('available playback-chain result requires finite value')
            if self.reason is not None:
                raise ValueError('available playback-chain result must not carry reason')
        else:
            if self.value is not None:
                raise ValueError('unavailable playback-chain result must not carry value')
            if self.reason is None:
                raise ValueError('unavailable playback-chain result requires reason')
        return self


class PlaybackChainEvaluation(BaseModel):
    """Immutable electrical headroom evidence, separate from speaker acoustic authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = AMPLIFIER_HEADROOM_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-amplifier-electrical-headroom-1'
    ] = PLAYBACK_CHAIN_EVALUATION_VERSION
    scenario: PlaybackChainScenario
    continuous_electrical_margin: PlaybackChainScalarResult
    peak_electrical_margin: PlaybackChainScalarResult
    continuous_amplifier_spl_ceiling: PlaybackChainScalarResult
    peak_amplifier_spl_ceiling: PlaybackChainScalarResult
    continuous_speaker_spl_ceiling: PlaybackChainScalarResult
    peak_speaker_spl_ceiling: PlaybackChainScalarResult
    amplifier_constrained_target_margin: PlaybackChainScalarResult
    continuous_limiter: LimiterState
    peak_limiter: LimiterState
    support_reasons: tuple[str, ...]
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'PlaybackChainEvaluation':
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('PlaybackChainEvaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('amp-headroom', digest):
            raise ValueError('PlaybackChainEvaluation ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'scenario': self.scenario.model_dump(mode='json'),
            'continuous_electrical_margin': (
                self.continuous_electrical_margin.model_dump(mode='json')
            ),
            'peak_electrical_margin': self.peak_electrical_margin.model_dump(
                mode='json'
            ),
            'continuous_amplifier_spl_ceiling': (
                self.continuous_amplifier_spl_ceiling.model_dump(mode='json')
            ),
            'peak_amplifier_spl_ceiling': (
                self.peak_amplifier_spl_ceiling.model_dump(mode='json')
            ),
            'continuous_speaker_spl_ceiling': (
                self.continuous_speaker_spl_ceiling.model_dump(mode='json')
            ),
            'peak_speaker_spl_ceiling': (
                self.peak_speaker_spl_ceiling.model_dump(mode='json')
            ),
            'amplifier_constrained_target_margin': (
                self.amplifier_constrained_target_margin.model_dump(mode='json')
            ),
            'continuous_limiter': self.continuous_limiter,
            'peak_limiter': self.peak_limiter,
            'support_reasons': list(self.support_reasons),
        }


def _available(
    value: float,
    unit: Literal['V RMS', 'W', 'dB', 'dB SPL'],
) -> PlaybackChainScalarResult:
    return PlaybackChainScalarResult(
        state='available',
        value=float(value),
        unit=unit,
    )


def _missing(
    reason: str,
    unit: Literal['V RMS', 'W', 'dB', 'dB SPL'],
) -> PlaybackChainScalarResult:
    return PlaybackChainScalarResult(
        state='missing',
        value=None,
        unit=unit,
        reason=reason,
    )


def _unsupported(
    reason: str,
    unit: Literal['V RMS', 'W', 'dB', 'dB SPL'],
) -> PlaybackChainScalarResult:
    return PlaybackChainScalarResult(
        state='unsupported',
        value=None,
        unit=unit,
        reason=reason,
    )


def _band_contains(domain: FrequencyDomain, band: DirectLevelFrequencyBand) -> bool:
    return (
        band.low_hz >= domain.minimum_hz
        and band.high_hz <= domain.maximum_hz
    )


def _convert_electrical(
    value: float,
    from_quantity: ElectricalQuantity,
    to_quantity: ElectricalQuantity,
    load: SpeakerElectricalLoadAuthority | None,
) -> float | None:
    if from_quantity == to_quantity:
        return float(value)
    if load is None or load.semantics != 'exact_resistive_reference':
        return None
    resistance = float(load.resistance_ohm)
    if from_quantity == 'voltage_v_rms' and to_quantity == 'power_w':
        return float(value) ** 2 / resistance
    if from_quantity == 'power_w' and to_quantity == 'voltage_v_rms':
        return sqrt(float(value) * resistance)
    return None


def _load_support_reason(
    amplifier: AmplifierOutputCapability,
    load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
) -> str | None:
    if load is None:
        return 'speaker electrical load authority is missing'
    if load.semantics != 'exact_resistive_reference':
        return (
            'speaker load is nominal-only; nominal impedance does not establish '
            'actual amplifier load capability'
        )
    if not _band_contains(load.valid_frequency_band, scenario.frequency_band):
        return 'requested frequency band is outside speaker load reference domain'
    if not amplifier.supported_load.contains(load.resistance_ohm):
        return 'speaker reference load is outside amplifier evidenced load domain'
    return None


def _common_support_reason(
    amplifier: AmplifierOutputCapability,
    load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
) -> str | None:
    load_reason = _load_support_reason(amplifier, load, scenario)
    if load_reason is not None:
        return load_reason
    if not _band_contains(amplifier.valid_frequency_band, scenario.frequency_band):
        return 'requested frequency band is outside amplifier capability domain'
    if amplifier.weighting != scenario.weighting:
        return 'playback weighting differs from amplifier capability authority'
    if (
        amplifier.channel_count_condition.simultaneous_channel_count
        != scenario.simultaneous_channel_condition.channel_count
    ):
        if (
            scenario.simultaneous_channel_condition.channel_count > 1
            and amplifier.channel_count_condition.simultaneous_channel_count == 1
        ):
            return (
                'single-channel amplifier capability cannot be promoted to '
                'simultaneous multi-channel performance'
            )
        return 'simultaneous channel-count condition differs from capability authority'
    return None


def _electrical_margin(
    amplifier: AmplifierOutputCapability,
    load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
    *,
    kind: Literal['continuous', 'peak'],
) -> PlaybackChainScalarResult:
    unit = _unit(scenario.requested_output_quantity)
    common_reason = _common_support_reason(amplifier, load, scenario)
    if common_reason is not None:
        return _unsupported(common_reason, unit)

    if kind == 'continuous':
        capability = amplifier.continuous_capability
        capability_duration = amplifier.continuous_duration_s
        requested_duration = scenario.continuous_duration_s
        requested_value = scenario.requested_continuous_output_value
    else:
        capability = amplifier.peak_capability
        capability_duration = amplifier.peak_duration_s
        requested_duration = scenario.peak_duration_s
        requested_value = scenario.requested_peak_output_value

    if capability is None:
        return _missing(f'{kind} amplifier capability is not evidenced', unit)
    if capability_duration is None:
        return _missing(f'{kind} amplifier duration is not evidenced', unit)
    if abs(float(capability_duration) - float(requested_duration)) > 1e-9:
        return _unsupported(
            f'{kind} duration differs from amplifier capability authority',
            unit,
        )
    converted = _convert_electrical(
        capability.value,
        capability.quantity,
        scenario.requested_output_quantity,
        load,
    )
    if converted is None:
        return _unsupported(
            'voltage/power comparison requires exact resistive load semantics',
            unit,
        )
    return _available(float(converted) - float(requested_value), unit)


def _amplifier_output_for_mode(
    amplifier: AmplifierOutputCapability,
    load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
    *,
    kind: Literal['continuous', 'peak'],
) -> tuple[float, ElectricalQuantity] | None:
    margin = _electrical_margin(
        amplifier,
        load,
        scenario,
        kind=kind,
    )
    if margin.state != 'available':
        return None
    capability = (
        amplifier.continuous_capability
        if kind == 'continuous'
        else amplifier.peak_capability
    )
    assert capability is not None
    return float(capability.value), capability.quantity


def _amplifier_spl_ceiling(
    equipment: EquipmentDefinition,
    amplifier: AmplifierOutputCapability,
    load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
    *,
    kind: Literal['continuous', 'peak'],
) -> PlaybackChainScalarResult:
    electrical_margin = _electrical_margin(
        amplifier,
        load,
        scenario,
        kind=kind,
    )
    if electrical_margin.state != 'available':
        reason = electrical_margin.reason or 'electrical capability unavailable'
        if electrical_margin.state == 'missing':
            return _missing(reason, 'dB SPL')
        return _unsupported(reason, 'dB SPL')

    sensitivity = equipment.sensitivity
    if sensitivity is None:
        return _missing(
            'speaker sensitivity/reference level is not evidenced',
            'dB SPL',
        )
    if sensitivity.valid_frequency_domain is None:
        return _missing(
            'speaker sensitivity has no valid frequency domain',
            'dB SPL',
        )
    if not _band_contains(
        sensitivity.valid_frequency_domain,
        scenario.frequency_band,
    ):
        return _unsupported(
            'requested frequency band is outside speaker sensitivity domain',
            'dB SPL',
        )
    if sensitivity.weighting is None:
        if scenario.weighting != 'unweighted':
            return _unsupported(
                'speaker sensitivity has no weighting authority for requested weighting',
                'dB SPL',
            )
    elif sensitivity.weighting != scenario.weighting:
        return _unsupported(
            'speaker sensitivity weighting differs from playback scenario',
            'dB SPL',
        )

    mode_output = _amplifier_output_for_mode(
        amplifier,
        load,
        scenario,
        kind=kind,
    )
    assert mode_output is not None
    amplifier_value, amplifier_quantity = mode_output
    normalized_input = _convert_electrical(
        amplifier_value,
        amplifier_quantity,
        sensitivity.input_quantity,
        load,
    )
    if normalized_input is None:
        return _unsupported(
            'amplifier capability cannot be converted to speaker sensitivity reference',
            'dB SPL',
        )
    ratio = float(normalized_input) / float(sensitivity.input_value)
    input_delta_db = (
        20.0 * log10(ratio)
        if sensitivity.input_quantity == 'voltage_v_rms'
        else 10.0 * log10(ratio)
    )
    distance_delta_db = 20.0 * log10(
        float(sensitivity.distance_m)
        / float(scenario.acoustic_target_distance_m)
    )
    return _available(
        float(sensitivity.level_db_spl) + input_delta_db + distance_delta_db,
        'dB SPL',
    )


def _speaker_spl_ceiling(
    equipment: EquipmentDefinition,
    scenario: PlaybackChainScenario,
    *,
    kind: Literal['continuous', 'peak'],
) -> PlaybackChainScalarResult:
    capability = equipment.spl_capability
    if capability is None:
        return _missing('speaker acoustic SPL capability is not evidenced', 'dB SPL')
    if kind == 'continuous':
        level = capability.continuous_db_spl
        duration = capability.continuous_duration_s
        requested_duration = scenario.continuous_duration_s
    else:
        level = capability.peak_db_spl
        duration = capability.peak_duration_s
        requested_duration = scenario.peak_duration_s
    if level is None:
        return _missing(f'speaker {kind} SPL capability is not evidenced', 'dB SPL')
    if capability.valid_frequency_domain is None:
        return _missing(
            f'speaker {kind} SPL capability has no valid frequency domain',
            'dB SPL',
        )
    if not _band_contains(capability.valid_frequency_domain, scenario.frequency_band):
        return _unsupported(
            f'requested frequency band is outside speaker {kind} SPL capability domain',
            'dB SPL',
        )
    if scenario.weighting != 'unweighted':
        return _unsupported(
            'speaker SPL capability has no weighting provenance for requested weighting',
            'dB SPL',
        )
    if duration is None:
        return _missing(
            f'speaker {kind} SPL capability has no duration authority',
            'dB SPL',
        )
    if abs(float(duration) - float(requested_duration)) > 1e-9:
        return _unsupported(
            f'{kind} duration differs from speaker SPL capability authority',
            'dB SPL',
        )
    distance_delta_db = 20.0 * log10(
        float(capability.reference_distance_m)
        / float(scenario.acoustic_target_distance_m)
    )
    return _available(float(level) + distance_delta_db, 'dB SPL')


def _limiter(
    amplifier_ceiling: PlaybackChainScalarResult,
    speaker_ceiling: PlaybackChainScalarResult,
) -> LimiterState:
    if (
        amplifier_ceiling.state != 'available'
        or speaker_ceiling.state != 'available'
        or amplifier_ceiling.value is None
        or speaker_ceiling.value is None
    ):
        return 'unknown'
    delta = float(amplifier_ceiling.value) - float(speaker_ceiling.value)
    if abs(delta) <= 1e-9:
        return 'equal'
    return 'amplifier' if delta < 0.0 else 'speaker'


def _target_margin(
    ceiling: PlaybackChainScalarResult,
    target_spl_db_spl: float,
) -> PlaybackChainScalarResult:
    if ceiling.state == 'available':
        assert ceiling.value is not None
        return _available(float(ceiling.value) - float(target_spl_db_spl), 'dB')
    reason = ceiling.reason or 'amplifier-constrained acoustic ceiling unavailable'
    if ceiling.state == 'missing':
        return _missing(reason, 'dB')
    return _unsupported(reason, 'dB')


def _validate_bindings(
    *,
    revision: SceneRevision,
    variant: SystemVariant,
    equipment: EquipmentDefinition,
    amplifier: AmplifierOutputCapability,
    load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
) -> None:
    if (
        variant.document_id != revision.document_id
        or variant.baseline_revision_id != revision.revision_id
        or variant.baseline_content_hash != revision.content_hash
    ):
        raise ValueError('playback-chain SystemVariant/SceneRevision authority mismatch')
    if (
        scenario.document_id != revision.document_id
        or scenario.scene_revision_id != revision.revision_id
        or scenario.scene_content_hash != revision.content_hash
        or scenario.variant_id != variant.variant_id
        or scenario.variant_sha256 != variant.variant_sha256
    ):
        raise ValueError('PlaybackChainScenario exact Scene/SystemVariant binding mismatch')

    equipment_ref = scenario.source_equipment
    if (
        equipment_ref.authority_id != equipment.definition_id
        or equipment_ref.version != equipment.version
        or equipment_ref.semantic_sha256 != equipment.semantic_sha256
    ):
        raise ValueError('PlaybackChainScenario source EquipmentDefinition mismatch')
    amplifier_ref = scenario.amplifier_capability
    if (
        amplifier_ref.authority_id != amplifier.capability_id
        or amplifier_ref.version != amplifier.version
        or amplifier_ref.semantic_sha256 != amplifier.semantic_sha256
    ):
        raise ValueError('PlaybackChainScenario amplifier capability mismatch')
    load_ref = scenario.speaker_load
    if load is None:
        if (
            load_ref is not None
            or scenario.speaker_load_semantics is not None
            or scenario.speaker_load_resistance_ohm is not None
        ):
            raise ValueError('PlaybackChainScenario expects a speaker load authority')
    else:
        if load_ref is None:
            raise ValueError('PlaybackChainScenario omits supplied speaker load authority')
        if (
            load_ref.authority_id != load.load_id
            or load_ref.version != load.version
            or load_ref.semantic_sha256 != load.semantic_sha256
        ):
            raise ValueError('PlaybackChainScenario speaker load authority mismatch')
        if (
            scenario.speaker_load_semantics != load.semantics
            or scenario.speaker_load_resistance_ohm is None
            or abs(
                float(scenario.speaker_load_resistance_ohm)
                - float(load.resistance_ohm)
            ) > 1e-12
        ):
            raise ValueError('PlaybackChainScenario speaker load condition mismatch')
        if (
            load.equipment_definition_id != equipment.definition_id
            or load.equipment_definition_version != equipment.version
            or load.equipment_definition_sha256 != equipment.semantic_sha256
        ):
            raise ValueError(
                'speaker load authority is not bound to source EquipmentDefinition'
            )

    bindings = [
        item
        for item in variant.equipment_bindings
        if item.entity_id == scenario.routing.source_entity_id
    ]
    if len(bindings) != 1:
        raise ValueError(
            'playback-chain source requires exactly one equipment binding'
        )
    binding = bindings[0]
    if (
        binding.equipment_definition_id != equipment.definition_id
        or binding.equipment_definition_version != equipment.version
        or binding.equipment_definition_sha256 != equipment.semantic_sha256
    ):
        raise ValueError('playback-chain source equipment binding mismatch')

    scene = materialize_system_variant(revision, variant)
    try:
        source = scene.entity(scenario.routing.source_entity_id)
    except KeyError as exc:
        raise ValueError('playback-chain source entity is missing') from exc
    if source.kind != 'speaker':
        raise ValueError('playback-chain source entity must be a speaker')
    if source.speaker_role != scenario.routing.channel_role_id:
        raise ValueError('playback-chain routing role does not match source speaker role')
    if scenario.routing.amplifier_output_id != amplifier.output_id:
        raise ValueError('playback-chain routed output does not match amplifier capability')


def evaluate_playback_chain(
    *,
    revision: SceneRevision,
    variant: SystemVariant,
    equipment_definition: EquipmentDefinition,
    amplifier_capability: AmplifierOutputCapability,
    speaker_load: SpeakerElectricalLoadAuthority | None,
    scenario: PlaybackChainScenario,
) -> PlaybackChainEvaluation:
    """Evaluate explicit amplifier electrical headroom without inventing speaker load data."""

    _validate_bindings(
        revision=revision,
        variant=variant,
        equipment=equipment_definition,
        amplifier=amplifier_capability,
        load=speaker_load,
        scenario=scenario,
    )

    continuous_margin = _electrical_margin(
        amplifier_capability,
        speaker_load,
        scenario,
        kind='continuous',
    )
    peak_margin = _electrical_margin(
        amplifier_capability,
        speaker_load,
        scenario,
        kind='peak',
    )
    continuous_amp_ceiling = _amplifier_spl_ceiling(
        equipment_definition,
        amplifier_capability,
        speaker_load,
        scenario,
        kind='continuous',
    )
    peak_amp_ceiling = _amplifier_spl_ceiling(
        equipment_definition,
        amplifier_capability,
        speaker_load,
        scenario,
        kind='peak',
    )
    continuous_speaker_ceiling = _speaker_spl_ceiling(
        equipment_definition,
        scenario,
        kind='continuous',
    )
    peak_speaker_ceiling = _speaker_spl_ceiling(
        equipment_definition,
        scenario,
        kind='peak',
    )
    target_ceiling = (
        continuous_amp_ceiling
        if scenario.target_mode == 'continuous'
        else peak_amp_ceiling
    )
    target_margin = _target_margin(target_ceiling, scenario.target_spl_db_spl)

    support_reasons = tuple(
        dict.fromkeys(
            item.reason
            for item in (
                continuous_margin,
                peak_margin,
                continuous_amp_ceiling,
                peak_amp_ceiling,
                continuous_speaker_ceiling,
                peak_speaker_ceiling,
                target_margin,
            )
            if item.reason is not None
        )
    )
    payload = {
        'schema_version': AMPLIFIER_HEADROOM_SCHEMA_VERSION,
        'authority_version': PLAYBACK_CHAIN_EVALUATION_VERSION,
        'scenario': scenario.model_dump(mode='json'),
        'continuous_electrical_margin': continuous_margin.model_dump(mode='json'),
        'peak_electrical_margin': peak_margin.model_dump(mode='json'),
        'continuous_amplifier_spl_ceiling': continuous_amp_ceiling.model_dump(
            mode='json'
        ),
        'peak_amplifier_spl_ceiling': peak_amp_ceiling.model_dump(mode='json'),
        'continuous_speaker_spl_ceiling': continuous_speaker_ceiling.model_dump(
            mode='json'
        ),
        'peak_speaker_spl_ceiling': peak_speaker_ceiling.model_dump(mode='json'),
        'amplifier_constrained_target_margin': target_margin.model_dump(mode='json'),
        'continuous_limiter': _limiter(
            continuous_amp_ceiling,
            continuous_speaker_ceiling,
        ),
        'peak_limiter': _limiter(
            peak_amp_ceiling,
            peak_speaker_ceiling,
        ),
        'support_reasons': list(support_reasons),
    }
    digest = _digest(payload)
    return PlaybackChainEvaluation(
        scenario=scenario,
        continuous_electrical_margin=continuous_margin,
        peak_electrical_margin=peak_margin,
        continuous_amplifier_spl_ceiling=continuous_amp_ceiling,
        peak_amplifier_spl_ceiling=peak_amp_ceiling,
        continuous_speaker_spl_ceiling=continuous_speaker_ceiling,
        peak_speaker_spl_ceiling=peak_speaker_ceiling,
        amplifier_constrained_target_margin=target_margin,
        continuous_limiter=payload['continuous_limiter'],
        peak_limiter=payload['peak_limiter'],
        support_reasons=support_reasons,
        evaluation_id=_semantic_id('amp-headroom', digest),
        evaluation_sha256=digest,
    )


def amplifier_headroom_objective_definitions(
    scenario: PlaybackChainScenario,
) -> tuple[ObjectiveDefinition, ...]:
    unit = _unit(scenario.requested_output_quantity)
    comparison_version = scenario.comparison_sha256
    return (
        ObjectiveDefinition(
            objective_id='o100d.electrical_headroom.continuous',
            quantity='amplifier_electrical_continuous_headroom_margin',
            unit=unit,
            direction='maximize',
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
            comparison_model_id=OBJECTIVE_COMPARISON_MODEL_ID,
            comparison_model_version=comparison_version,
        ),
        ObjectiveDefinition(
            objective_id='o100d.electrical_headroom.peak',
            quantity='amplifier_electrical_peak_headroom_margin',
            unit=unit,
            direction='maximize',
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
            comparison_model_id=OBJECTIVE_COMPARISON_MODEL_ID,
            comparison_model_version=comparison_version,
        ),
        ObjectiveDefinition(
            objective_id='o100d.amplifier_constrained_target_margin',
            quantity='amplifier_constrained_direct_target_spl_margin',
            unit='dB',
            direction='maximize',
            valid_domain=ObjectiveValidDomain(kind='finite_real'),
            comparison_model_id=OBJECTIVE_COMPARISON_MODEL_ID,
            comparison_model_version=comparison_version,
        ),
    )


def amplifier_headroom_objective_vector(
    evaluation: PlaybackChainEvaluation,
) -> ObjectiveVector:
    definitions = amplifier_headroom_objective_definitions(evaluation.scenario)
    results = (
        evaluation.continuous_electrical_margin,
        evaluation.peak_electrical_margin,
        evaluation.amplifier_constrained_target_margin,
    )
    metrics = tuple(
        ObjectiveMetric(
            objective_id=definition.objective_id,
            value=result.value,
            unit=definition.unit,
            direction=definition.direction,
            state=result.state,
            definition=definition,
        )
        for definition, result in zip(definitions, results, strict=True)
    )
    return ObjectiveVector(
        candidate_id=evaluation.scenario.variant_id,
        metrics=metrics,
    )
