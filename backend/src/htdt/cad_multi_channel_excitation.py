"""Multi-channel excitation scenario authority (#492).

A snapshot may contain several sources, but "source exists" must never mean
"source is driven". This module defines the immutable product-level scenario
that pins exactly which logical channels / physical source entities
participate in one playback condition, their per-source drive state
(gain/delay/polarity/filter authority) and the bass-management routing that
redirects band-limited energy between channels and radiators.

Coherent system playback is produced only by exact complex linear
superposition over transfers that share phasor convention, frequency grid,
source normalization and the timing authority the scenario requires.
Magnitude-only per-source results can never be silently summed into a
coherent system response.
"""

from __future__ import annotations

import cmath
from math import isfinite, log10, pi
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_bass_management import BassManagementProfile
from .cad_equipment import FrequencyDomain
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


MULTI_CHANNEL_EXCITATION_SCHEMA_VERSION = 1
MULTI_CHANNEL_EXCITATION_AUTHORITY_VERSION = 'mc-excitation-scenario-1'
COHERENT_COMPOSITION_AUTHORITY_VERSION = 'mc-coherent-composition-1'

PHASOR_CONVENTION = 'exp(+i*omega*t)'






def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f'{field_name} must be finite')
    return number


class ExcitationDriveState(BaseModel):
    """Per-source drive state for one participant.

    ``None`` fields are UNKNOWN authority: a missing gain/delay/polarity is
    never silently defaulted to unity at composition time; the scenario
    contract requires explicit values for active participants.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    gain_db: float = 0.0
    delay_s: float = 0.0
    polarity: Literal[1, -1] = 1
    filter_authority_ref: ExactExternalAuthorityRef | None = None
    source_excitation_binding_id: str | None = Field(default=None, min_length=1)
    source_excitation_binding_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @field_validator('gain_db', 'delay_s')
    @classmethod
    def finite(cls, value: float) -> float:
        return _finite(value, field_name='drive state')

    @model_validator(mode='after')
    def validate_state(self) -> 'ExcitationDriveState':
        if self.delay_s < 0.0:
            raise ValueError('drive delay must be non-negative')
        if (self.source_excitation_binding_id is None) != (
            self.source_excitation_binding_sha256 is None
        ):
            raise ValueError(
                'source excitation binding id/hash must be supplied together'
            )
        return self


class ExcitationChannelParticipant(BaseModel):
    """One logical channel bound to one exact physical source entity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    logical_channel_id: str = Field(min_length=1)
    channel_role_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    source_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    drive: ExcitationDriveState


class BassManagementRoute(BaseModel):
    """Explicit band-limited redirection of one channel to one radiator."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    route_id: str = Field(min_length=1)
    from_logical_channel_id: str = Field(min_length=1)
    to_source_entity_id: str = Field(min_length=1)
    band: FrequencyDomain
    method_authority_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_route(self) -> 'BassManagementRoute':
        if self.from_logical_channel_id == self.to_source_entity_id:
            raise ValueError('bass-management route endpoints must be distinct')
        return self


ScenarioPreset = Literal[
    'custom',
    'single_channel',
    'stereo_lr',
    'subwoofers_only',
    'mains_plus_subs_bass_managed',
    'all_active_channels',
]

CombinationSemantics = Literal[
    'independent_transfer_set',
    'coherent_system_sum',
]


class MultiChannelExcitationScenario(BaseModel):
    """Immutable playback/excitation scenario authority.

    ``combination_semantics`` separates a set of independent per-source
    transfer problems from a coherent system playback sum. Scenario
    membership is explicit: presence in the snapshot is not participation.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_CHANNEL_EXCITATION_SCHEMA_VERSION
    authority_version: Literal[
        'mc-excitation-scenario-1'
    ] = MULTI_CHANNEL_EXCITATION_AUTHORITY_VERSION
    scenario_id: str = Field(pattern=r'^mc-excitation-scenario:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    participants: tuple[ExcitationChannelParticipant, ...] = Field(min_length=1)
    bass_management_routes: tuple[BassManagementRoute, ...] = ()
    combination_semantics: CombinationSemantics = 'coherent_system_sum'
    timing_authority_requirement: Literal[
        'required_absolute_or_relative', 'not_required'
    ] = 'required_absolute_or_relative'
    preset: ScenarioPreset = 'custom'

    @model_validator(mode='after')
    def validate_scenario(self) -> 'MultiChannelExcitationScenario':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('system variant id/hash must be supplied together')
        logical = [item.logical_channel_id for item in self.participants]
        if len(logical) != len(set(logical)):
            raise ValueError('scenario logical channel ids must be unique')
        sources = [item.source_entity_id for item in self.participants]
        if len(sources) != len(set(sources)):
            raise ValueError(
                'one physical source entity cannot participate twice; '
                'encode a second logical role on a distinct entity'
            )
        participant_channels = set(logical)
        participant_sources = set(sources)
        for route in self.bass_management_routes:
            if route.from_logical_channel_id not in participant_channels:
                raise ValueError(
                    'bass-management route originates outside the scenario'
                )
            if route.to_source_entity_id not in participant_sources:
                raise ValueError(
                    'bass-management route targets a source outside the scenario'
                )
        route_ids = [item.route_id for item in self.bass_management_routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError('bass-management route ids must be unique')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('multi-channel excitation scenario hash mismatch')
        if self.scenario_id != f'mc-excitation-scenario:{expected}':
            raise ValueError('multi-channel excitation scenario id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'scenario_id', 'semantic_sha256'},
        )


def build_multi_channel_excitation_scenario(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    participants: tuple[ExcitationChannelParticipant, ...],
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    bass_management_routes: tuple[BassManagementRoute, ...] = (),
    combination_semantics: CombinationSemantics = 'coherent_system_sum',
    timing_authority_requirement: Literal[
        'required_absolute_or_relative', 'not_required'
    ] = 'required_absolute_or_relative',
    preset: ScenarioPreset = 'custom',
) -> MultiChannelExcitationScenario:
    payload = {
        'schema_version': MULTI_CHANNEL_EXCITATION_SCHEMA_VERSION,
        'authority_version': MULTI_CHANNEL_EXCITATION_AUTHORITY_VERSION,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'participants': [
            item.model_dump(mode='json') for item in participants
        ],
        'bass_management_routes': [
            item.model_dump(mode='json') for item in bass_management_routes
        ],
        'combination_semantics': combination_semantics,
        'timing_authority_requirement': timing_authority_requirement,
        'preset': preset,
    }
    digest = _digest(payload)
    return MultiChannelExcitationScenario(
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        system_variant_id=system_variant_id,
        system_variant_sha256=system_variant_sha256,
        participants=participants,
        bass_management_routes=bass_management_routes,
        combination_semantics=combination_semantics,
        timing_authority_requirement=timing_authority_requirement,
        preset=preset,
        scenario_id=f'mc-excitation-scenario:{digest}',
        semantic_sha256=digest,
    )


class ScenarioSourceTransfer(BaseModel):
    """Per-source transfer input to coherent composition.

    ``pressure_real``/``pressure_imag`` present ⇒ complex authority; when
    only magnitude exists, ``pressure_magnitude_pa`` carries it and the
    composer must fail closed rather than synthesize phase.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_entity_id: str = Field(min_length=1)
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    pressure_real: tuple[float, ...] | None = None
    pressure_imag: tuple[float, ...] | None = None
    pressure_magnitude_pa: tuple[float, ...] | None = None
    phasor_convention: str | None = None
    source_normalization_id: str = Field(min_length=1)
    timing_authority: Literal[
        'absolute_propagation_time', 'relative_delay', 'unavailable'
    ]
    result_authority_ref: ExactExternalAuthorityRef | None = None

    @field_validator('frequency_hz')
    @classmethod
    def valid_axis(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if tuple(value) != tuple(sorted(set(value))):
            raise ValueError('transfer frequency axis must be sorted and unique')
        if any(not isfinite(float(v)) or float(v) <= 0.0 for v in value):
            raise ValueError('transfer frequencies must be finite and positive')
        return value

    @model_validator(mode='after')
    def validate_transfer(self) -> 'ScenarioSourceTransfer':
        count = len(self.frequency_hz)
        complex_parts = (self.pressure_real is None) + (self.pressure_imag is None)
        if complex_parts == 1:
            raise ValueError('complex transfer requires both real and imaginary parts')
        if complex_parts == 0:
            if len(self.pressure_real) != count or len(self.pressure_imag) != count:
                raise ValueError('complex transfer arrays must match the frequency axis')
            if self.phasor_convention is None:
                raise ValueError('complex transfer requires a phasor convention')
            if self.pressure_magnitude_pa is not None:
                raise ValueError('one transfer cannot carry both complex and magnitude-only data')
        else:
            if self.pressure_magnitude_pa is None or len(self.pressure_magnitude_pa) != count:
                raise ValueError('magnitude-only transfer requires the magnitude axis data')
            if any(float(v) < 0.0 or not isfinite(float(v)) for v in self.pressure_magnitude_pa):
                raise ValueError('magnitude-only transfer values must be finite non-negative')
        return self

    @property
    def is_complex(self) -> bool:
        return self.pressure_real is not None


class ResolvedPlaybackTransfer(BaseModel):
    """A resolved complex transfer for an external playback-chain authority.

    ``ExcitationDriveState.filter_authority_ref`` and
    ``BassManagementRoute.method_authority_ref`` are opaque authority
    identities — the scenario stores which authority applies, never the
    curve itself. Composition resolves each ref against this type: the
    resolved transfer must carry exact complex data on the scenario
    frequency grid under the same phasor convention.

    ``residual_*`` is only meaningful for crossover/routing methods: it is
    the complementary transfer the originating channel keeps after its
    redirected band leaves (e.g. the high-pass a crossover applies to the
    small main). A plain per-source drive filter never carries one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority: ExactExternalAuthorityRef
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    transfer_real: tuple[float, ...]
    transfer_imag: tuple[float, ...]
    phasor_convention: str = Field(min_length=1)
    residual_real: tuple[float, ...] | None = None
    residual_imag: tuple[float, ...] | None = None

    @field_validator('frequency_hz')
    @classmethod
    def valid_axis(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if tuple(value) != tuple(sorted(set(value))):
            raise ValueError('transfer frequency axis must be sorted and unique')
        if any(not isfinite(float(v)) or float(v) <= 0.0 for v in value):
            raise ValueError('transfer frequencies must be finite and positive')
        return value

    @model_validator(mode='after')
    def validate_transfer(self) -> 'ResolvedPlaybackTransfer':
        count = len(self.frequency_hz)
        if len(self.transfer_real) != count or len(self.transfer_imag) != count:
            raise ValueError('transfer arrays must match the frequency axis')
        for value in (*self.transfer_real, *self.transfer_imag):
            if not isfinite(float(value)):
                raise ValueError('transfer values must be finite')
        if (self.residual_real is None) != (self.residual_imag is None):
            raise ValueError(
                'residual transfer requires both real and imaginary parts'
            )
        if self.residual_real is not None and (
            len(self.residual_real) != count
            or len(self.residual_imag) != count
        ):
            raise ValueError(
                'residual transfer arrays must match the frequency axis'
            )
        if self.residual_real is not None:
            for value in (*self.residual_real, *self.residual_imag):
                if not isfinite(float(value)):
                    raise ValueError('residual transfer values must be finite')
        return self

    @property
    def has_residual(self) -> bool:
        return self.residual_real is not None


class CoherentSystemResponse(BaseModel):
    """The composed system transfer; a distinct authority from any cell."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_CHANNEL_EXCITATION_SCHEMA_VERSION
    authority_version: Literal[
        'mc-coherent-composition-1'
    ] = COHERENT_COMPOSITION_AUTHORITY_VERSION
    response_id: str = Field(pattern=r'^mc-coherent-response:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    scenario_id: str = Field(pattern=r'^mc-excitation-scenario:[0-9a-f]{64}$')
    scenario_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    state: Literal['READY', 'UNSUPPORTED']
    unsupported_reasons: tuple[str, ...] = ()
    frequency_hz: tuple[float, ...] | None = None
    pressure_real: tuple[float, ...] | None = None
    pressure_imag: tuple[float, ...] | None = None
    magnitude_pa: tuple[float, ...] | None = None
    magnitude_db_spl: tuple[float, ...] | None = None
    phase_deg: tuple[float, ...] | None = None
    phasor_convention: str | None = None
    #: Exact playback-chain authorities (drive filters, crossover methods)
    #: whose resolved transfers shaped this response — the numerical result
    #: depends on the same semantics the scenario identity pins.
    applied_authority_refs: tuple[ExactExternalAuthorityRef, ...] = ()
    pressure_reference_pa: float = 20.0e-6

    @model_validator(mode='after')
    def validate_response(self) -> 'CoherentSystemResponse':
        if self.state == 'READY':
            if self.unsupported_reasons:
                raise ValueError('READY response cannot carry unsupported reasons')
            for field_name in (
                'frequency_hz',
                'pressure_real',
                'pressure_imag',
                'magnitude_pa',
                'magnitude_db_spl',
                'phase_deg',
            ):
                if getattr(self, field_name) is None:
                    raise ValueError(f'READY response requires {field_name}')
            if self.phasor_convention is None:
                raise ValueError('READY response requires a phasor convention')
            count = len(self.frequency_hz)
            for field_name in (
                'pressure_real',
                'pressure_imag',
                'magnitude_pa',
                'magnitude_db_spl',
                'phase_deg',
            ):
                if len(getattr(self, field_name)) != count:
                    raise ValueError(f'{field_name} must match the frequency axis')
        else:
            if not self.unsupported_reasons:
                raise ValueError('UNSUPPORTED response requires reasons')
            for field_name in (
                'frequency_hz',
                'pressure_real',
                'pressure_imag',
                'magnitude_pa',
                'magnitude_db_spl',
                'phase_deg',
            ):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f'UNSUPPORTED response cannot carry {field_name}'
                    )
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('coherent response semantic hash mismatch')
        if self.response_id != f'mc-coherent-response:{expected}':
            raise ValueError('coherent response id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'response_id', 'semantic_sha256'},
        )


def _unsupported_response(
    scenario: MultiChannelExcitationScenario,
    reasons: list[str],
) -> CoherentSystemResponse:
    payload = {
        'schema_version': MULTI_CHANNEL_EXCITATION_SCHEMA_VERSION,
        'authority_version': COHERENT_COMPOSITION_AUTHORITY_VERSION,
        'scenario_id': scenario.scenario_id,
        'scenario_semantic_sha256': scenario.semantic_sha256,
        'state': 'UNSUPPORTED',
        'unsupported_reasons': sorted(set(reasons)),
        'frequency_hz': None,
        'pressure_real': None,
        'pressure_imag': None,
        'magnitude_pa': None,
        'magnitude_db_spl': None,
        'phase_deg': None,
        'phasor_convention': None,
        'applied_authority_refs': [],
        'pressure_reference_pa': 20.0e-6,
    }
    digest = _digest(payload)
    return CoherentSystemResponse(
        scenario_id=scenario.scenario_id,
        scenario_semantic_sha256=scenario.semantic_sha256,
        state='UNSUPPORTED',
        unsupported_reasons=tuple(sorted(set(reasons))),
        applied_authority_refs=(),
        response_id=f'mc-coherent-response:{digest}',
        semantic_sha256=digest,
    )


def compose_coherent_system_response(
    scenario: MultiChannelExcitationScenario,
    transfers: tuple[ScenarioSourceTransfer, ...],
    *,
    resolved_transfers: tuple[ResolvedPlaybackTransfer, ...] = (),
) -> CoherentSystemResponse:
    """Compose one coherent system transfer via exact linear superposition.

    ``ScenarioSourceTransfer`` stays the room/source transfer of the
    physical source; playback-chain drive/filter/routing transfers are
    applied as separate per-source multipliers before summation so room
    results stay reusable across scenarios.

    Every ``filter_authority_ref`` and every bass-management
    ``method_authority_ref`` declared by the scenario must resolve to an
    exact complex transfer on the scenario grid — unresolved or
    incomplete routing/filter authority fails closed to UNSUPPORTED
    instead of being silently ignored.
    """
    reasons: list[str] = []
    if scenario.combination_semantics != 'coherent_system_sum':
        reasons.append(
            'scenario declares independent_transfer_set; coherent composition '
            'is not part of its contract'
        )

    by_source: dict[str, ScenarioSourceTransfer] = {}
    for transfer in transfers:
        if transfer.source_entity_id in by_source:
            reasons.append(
                f'duplicate transfer for source {transfer.source_entity_id}'
            )
            continue
        by_source[transfer.source_entity_id] = transfer
    extra = set(by_source) - {p.source_entity_id for p in scenario.participants}
    if extra:
        reasons.append(
            'transfers were provided for sources outside the scenario: '
            + ','.join(sorted(extra))
        )

    active: list[tuple[ExcitationChannelParticipant, ScenarioSourceTransfer]] = []
    for participant in scenario.participants:
        transfer = by_source.get(participant.source_entity_id)
        if transfer is None:
            reasons.append(
                f'no transfer supplied for active source {participant.source_entity_id}'
            )
            continue
        if not transfer.is_complex:
            reasons.append(
                f'source {participant.source_entity_id} has magnitude-only '
                'authority; it cannot enter a coherent sum'
            )
            continue
        active.append((participant, transfer))

    if active:
        grid = active[0][1].frequency_hz
        convention = active[0][1].phasor_convention
        normalization = active[0][1].source_normalization_id
        for participant, transfer in active[1:]:
            if transfer.frequency_hz != grid:
                reasons.append(
                    f'source {participant.source_entity_id} frequency grid differs '
                    'from the scenario reference grid'
                )
            if transfer.phasor_convention != convention:
                reasons.append(
                    f'source {participant.source_entity_id} phasor convention differs'
                )
            if transfer.source_normalization_id != normalization:
                reasons.append(
                    f'source {participant.source_entity_id} source normalization differs'
                )

    if scenario.timing_authority_requirement == 'required_absolute_or_relative':
        for participant, transfer in active:
            if participant.drive.delay_s != 0.0 and transfer.timing_authority == 'unavailable':
                reasons.append(
                    f'source {participant.source_entity_id} applies a delay without '
                    'any timing authority'
                )
            elif transfer.timing_authority == 'unavailable':
                reasons.append(
                    f'source {participant.source_entity_id} lacks timing authority '
                    'required by the scenario'
                )

    resolved: dict[str, ResolvedPlaybackTransfer] = {}
    for item in resolved_transfers:
        resolved[item.authority.semantic_hash_sha256] = item

    def _resolve(ref: ExactExternalAuthorityRef) -> ResolvedPlaybackTransfer | None:
        item = resolved.get(ref.semantic_hash_sha256)
        if (
            item is None
            or item.authority.authority_id != ref.authority_id
            or item.authority.authority_version != ref.authority_version
        ):
            return None
        return item

    grid = active[0][1].frequency_hz if active else ()
    convention = active[0][1].phasor_convention if active else None

    # Resolve per-source drive filters before composition; an unresolvable
    # ref is a fail-closed reason, never an ignored field.
    participant_filters: dict[str, ResolvedPlaybackTransfer] = {}
    for participant in scenario.participants:
        ref = participant.drive.filter_authority_ref
        if ref is None:
            continue
        transfer = _resolve(ref)
        if transfer is None:
            reasons.append(
                f'filter authority {ref.authority_id} for source '
                f'{participant.source_entity_id} could not be resolved to '
                'an exact complex transfer'
            )
            continue
        if active:
            if transfer.frequency_hz != grid:
                reasons.append(
                    f'filter authority {ref.authority_id} frequency grid '
                    'differs from the scenario reference grid'
                )
                continue
            if transfer.phasor_convention != convention:
                reasons.append(
                    f'filter authority {ref.authority_id} phasor convention '
                    'differs from the scenario'
                )
                continue
        participant_filters[participant.logical_channel_id] = transfer

    # Resolve each bass-management route's method authority: the redirected
    # band needs an exact crossover transfer L(f) into the target and the
    # complementary residual H(f) the from-channel keeps.
    route_methods: dict[str, ResolvedPlaybackTransfer] = {}
    for route in scenario.bass_management_routes:
        transfer = _resolve(route.method_authority_ref)
        if transfer is None:
            reasons.append(
                f'bass-management route {route.route_id} method authority '
                f'{route.method_authority_ref.authority_id} could not be '
                'resolved to an exact complex crossover transfer'
            )
            continue
        if not transfer.has_residual:
            reasons.append(
                f'bass-management route {route.route_id} method authority '
                'declares no complementary residual transfer for the '
                'originating channel'
            )
            continue
        if active:
            if transfer.frequency_hz != grid:
                reasons.append(
                    f'bass-management route {route.route_id} method '
                    'frequency grid differs from the scenario reference grid'
                )
                continue
            if transfer.phasor_convention != convention:
                reasons.append(
                    f'bass-management route {route.route_id} method phasor '
                    'convention differs from the scenario'
                )
                continue
        route_methods[route.route_id] = transfer

    if reasons:
        return _unsupported_response(scenario, reasons)

    grid = active[0][1].frequency_hz
    convention = active[0][1].phasor_convention
    participant_by_channel = {
        p.logical_channel_id: p for p in scenario.participants
    }
    # Effective per-source drive multiplier: own drive state, own resolved
    # filter, then the residual of every route that redirects bands away.
    multipliers: dict[str, list[complex]] = {}
    applied_refs: list[ExactExternalAuthorityRef] = []
    for participant in scenario.participants:
        gain = 10.0 ** (participant.drive.gain_db / 20.0)
        polarity = float(participant.drive.polarity)
        delay = participant.drive.delay_s
        base = [
            polarity
            * gain
            * cmath.exp(-1j * 2.0 * pi * delay * frequency)
            for frequency in grid
        ]
        filt = participant_filters.get(participant.logical_channel_id)
        if filt is not None:
            base = [
                b * complex(re, im)
                for b, re, im in zip(
                    base, filt.transfer_real, filt.transfer_imag
                )
            ]
            applied_refs.append(filt.authority)
        multipliers[participant.source_entity_id] = base

    for route in scenario.bass_management_routes:
        method = route_methods[route.route_id]
        origin = participant_by_channel[route.from_logical_channel_id]
        origin_mult = multipliers[origin.source_entity_id]
        method_real = method.transfer_real
        method_imag = method.transfer_imag
        residual_real = method.residual_real
        residual_imag = method.residual_imag
        # The originating source keeps only the residual band.
        multipliers[origin.source_entity_id] = [
            value * complex(r, i)
            for value, r, i in zip(origin_mult, residual_real, residual_imag)
        ]
        # The redirected band adds to the target source's effective drive.
        target_mult = multipliers[route.to_source_entity_id]
        multipliers[route.to_source_entity_id] = [
            value + origin * complex(l_r, l_i)
            for value, origin, l_r, l_i in zip(
                target_mult, origin_mult, method_real, method_imag
            )
        ]
        applied_refs.append(route.method_authority_ref)

    summed = [0j] * len(grid)
    for participant, transfer in active:
        multiplier = multipliers[participant.source_entity_id]
        for index in range(len(grid)):
            phasor = complex(
                transfer.pressure_real[index], transfer.pressure_imag[index]
            )
            summed[index] += phasor * multiplier[index]

    magnitude_pa = tuple(abs(value) for value in summed)
    # JSON cannot carry -inf; a true pressure null floors at -400 dB SPL.
    magnitude_db_spl = tuple(
        20.0 * log10(value / 20.0e-6) if value > 0.0 else -400.0
        for value in magnitude_pa
    )
    phase_deg = tuple(
        cmath.phase(value) * 180.0 / pi for value in summed
    )
    payload = {
        'schema_version': MULTI_CHANNEL_EXCITATION_SCHEMA_VERSION,
        'authority_version': COHERENT_COMPOSITION_AUTHORITY_VERSION,
        'scenario_id': scenario.scenario_id,
        'scenario_semantic_sha256': scenario.semantic_sha256,
        'state': 'READY',
        'unsupported_reasons': [],
        'frequency_hz': list(grid),
        'pressure_real': [value.real for value in summed],
        'pressure_imag': [value.imag for value in summed],
        'magnitude_pa': list(magnitude_pa),
        'magnitude_db_spl': list(magnitude_db_spl),
        'phase_deg': list(phase_deg),
        'phasor_convention': convention,
        'applied_authority_refs': [
            ref.model_dump(mode='json') for ref in applied_refs
        ],
        'pressure_reference_pa': 20.0e-6,
    }
    digest = _digest(payload)
    return CoherentSystemResponse(
        scenario_id=scenario.scenario_id,
        scenario_semantic_sha256=scenario.semantic_sha256,
        state='READY',
        frequency_hz=grid,
        pressure_real=tuple(value.real for value in summed),
        pressure_imag=tuple(value.imag for value in summed),
        magnitude_pa=magnitude_pa,
        magnitude_db_spl=tuple(payload['magnitude_db_spl']),
        phase_deg=phase_deg,
        phasor_convention=convention,
        applied_authority_refs=tuple(applied_refs),
        response_id=f'mc-coherent-response:{digest}',
        semantic_sha256=digest,
    )


def bass_management_routes_for_scenario(
    scenario: MultiChannelExcitationScenario,
    profile: BassManagementProfile,
    *,
    method_authority_ref: ExactExternalAuthorityRef,
    destination_source_ids: dict[str, str],
) -> tuple[BassManagementRoute, ...]:
    """Compile a canonical #633 ``BassManagementProfile`` into scenario
    routes, so the profile remains the single routing truth.

    Every ``high_pass`` main-channel rule with recorded redirected
    destinations becomes one ``BassManagementRoute`` per
    (participant, destination source). ``destination_source_ids`` binds
    profile destination ids to scenario ``source_entity_id`` values; a
    missing binding or a rule without a recorded ``redirected_low_band``
    raises ``ValueError`` rather than silently approximating the routing.
    """
    routes: list[BassManagementRoute] = []
    channel_by_role = {p.channel_role_id: p for p in scenario.participants}
    for rule in profile.main_rules:
        if rule.handling != 'high_pass':
            continue
        participant = channel_by_role.get(rule.logical_role_id)
        if participant is None:
            continue
        if rule.redirected_low_band is None:
            raise ValueError(
                f'bass-management rule for role {rule.logical_role_id} '
                'records no redirected_low_band; refusing to invent the '
                'routed band'
            )
        for destination in rule.redirected_destinations:
            target_source = destination_source_ids.get(destination)
            if target_source is None:
                raise ValueError(
                    f'bass destination {destination} is not bound to a '
                    'scenario source entity'
                )
            routes.append(
                BassManagementRoute(
                    route_id=f'{rule.logical_role_id}->{destination}',
                    from_logical_channel_id=participant.logical_channel_id,
                    to_source_entity_id=target_source,
                    band=FrequencyDomain(
                        minimum_hz=max(rule.redirected_low_band.low_hz, 1e-3),
                        maximum_hz=rule.redirected_low_band.high_hz,
                    ),
                    method_authority_ref=method_authority_ref,
                )
            )
    return tuple(routes)
