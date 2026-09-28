"""Personal / assistive listening playback authority (#1048).

Headphones, earbuds, hearing devices, Auracast and TV private-listening
routes are *separate outputs* from room loudspeaker playback — never
represented as fake Scene speakers, and room acoustic prediction never
applies to them silently.

- :class:`PersonalAudioEndpoint` — endpoint class and capability snapshot;
  capability-based (e.g. Auracast requires LE Audio / Public Broadcast
  Profile support, not a generic Bluetooth version label).
- :class:`PersonalListeningRoute` — the immutable route authority: source
  output → transport family → endpoint, with the exact selected
  program/mix bound where known.
- :class:`RoomPersonalCoexistence` — whether room speakers and the
  personal endpoint are active together; each side keeps its own sync
  measurements (no one generic ``lip_sync_ms`` serves every route).
- :func:`evaluate_personal_route` — commissioning checks; a personal
  route claiming room-speaker prediction inheritance is flagged, never
  silently allowed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




PersonalTransportFamily = Literal[
    'wired_headphone',
    'bluetooth_classic',
    'le_audio_unicast',
    'auracast_broadcast',
    'vendor_wireless',
    'tv_private_listening',
    'opaque_external',
    'unknown',
]

EndpointKind = Literal[
    'headphone',
    'earbud',
    'hearing_device',
    'assistive_receiver',
    'personal_receiver_unknown',
]

CoexistenceState = Literal[
    'personal_only',
    'room_only',
    'both_active',
    'both_muted_unknown',
    'unknown',
]


class PersonalAudioEndpoint(BaseModel):
    """A personal receiver endpoint — capability-based, never a room
    speaker."""

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(min_length=1)
    endpoint_kind: EndpointKind = 'personal_receiver_unknown'
    model_label: str | None = None
    supports_le_audio: bool | None = None
    supports_auracast_pbp: bool | None = None
    latency_reporting: bool | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()


class PersonalListeningRoute(BaseModel):
    """Immutable route authority for one source output → personal
    endpoint path under one exact program/mix selection."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['personal-listening-route-1'] = (
        'personal-listening-route-1'
    )
    route_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_condition_id: str | None = None
    source_output_ref: str | None = None
    transmitter_equipment_id: str | None = None
    transport_family: PersonalTransportFamily = 'unknown'
    endpoint_id: str | None = None
    selected_program_id: str | None = None
    codec_id: str | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    channel_semantics: str | None = None
    auracast_broadcast_id: str | None = None
    auracast_encrypted: bool | None = None
    coexistence: CoexistenceState = 'unknown'
    room_speaker_muted: bool | None = None
    claims_room_acoustic_prediction: bool = False
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    route_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'route_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'PersonalListeningRoute':
        if self.transport_family == 'auracast_broadcast':
            if self.auracast_broadcast_id is None:
                raise ValueError(
                    'an Auracast route requires a broadcast identity'
                )
        if self.route_sha256 != _hash(self.semantic_payload()):
            raise ValueError('personal listening route hash mismatch')
        return self


def build_personal_listening_route(
    *,
    route_id: str,
    version: str,
    source_condition_id: str | None = None,
    source_output_ref: str | None = None,
    transmitter_equipment_id: str | None = None,
    transport_family: PersonalTransportFamily = 'unknown',
    endpoint_id: str | None = None,
    selected_program_id: str | None = None,
    codec_id: str | None = None,
    sample_rate_hz: int | None = None,
    channel_semantics: str | None = None,
    auracast_broadcast_id: str | None = None,
    auracast_encrypted: bool | None = None,
    coexistence: CoexistenceState = 'unknown',
    room_speaker_muted: bool | None = None,
    claims_room_acoustic_prediction: bool = False,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> PersonalListeningRoute:
    probe = PersonalListeningRoute.model_construct(**canonicalize_payload(PersonalListeningRoute, dict(
        route_id=route_id,
        version=version,
        source_condition_id=source_condition_id,
        source_output_ref=source_output_ref,
        transmitter_equipment_id=transmitter_equipment_id,
        transport_family=transport_family,
        endpoint_id=endpoint_id,
        selected_program_id=selected_program_id,
        codec_id=codec_id,
        sample_rate_hz=sample_rate_hz,
        channel_semantics=channel_semantics,
        auracast_broadcast_id=auracast_broadcast_id,
        auracast_encrypted=auracast_encrypted,
        coexistence=coexistence,
        room_speaker_muted=room_speaker_muted,
        claims_room_acoustic_prediction=claims_room_acoustic_prediction,
        provenance=tuple(provenance),
        route_sha256='',
    )))
    return PersonalListeningRoute(
        **probe.model_dump(mode='python', exclude={'route_sha256'}),
        route_sha256=_hash(probe.semantic_payload()),
    )


class PersonalRouteCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


def evaluate_personal_route(
    *,
    route: PersonalListeningRoute,
    endpoint: PersonalAudioEndpoint | None,
) -> tuple[PersonalRouteCheckResult, ...]:
    """Commissioning checks for one personal route."""

    checks: list[PersonalRouteCheckResult] = []

    if route.transport_family == 'auracast_broadcast':
        capable = (
            endpoint is not None and endpoint.supports_auracast_pbp is True
        )
        checks.append(
            PersonalRouteCheckResult(
                check='auracast_capability',
                status='PASS' if capable else 'UNKNOWN',
                reason=(
                    'endpoint declares Public Broadcast Profile support'
                    if capable
                    else 'Auracast capability requires explicit LE Audio/'
                    'PBP support — a generic Bluetooth version does not '
                    'imply it'
                ),
            )
        )

    if route.endpoint_id is None:
        checks.append(
            PersonalRouteCheckResult(
                check='endpoint_bound',
                status='UNKNOWN',
                reason='no personal endpoint bound to this route',
            )
        )
    elif endpoint is None or endpoint.endpoint_id != route.endpoint_id:
        checks.append(
            PersonalRouteCheckResult(
                check='endpoint_bound',
                status='FAIL',
                reason='route references an endpoint that is not supplied',
            )
        )
    else:
        checks.append(
            PersonalRouteCheckResult(
                check='endpoint_bound',
                status='PASS',
                reason='route bound to a recorded personal endpoint',
            )
        )

    checks.append(
        PersonalRouteCheckResult(
            check='room_prediction_boundary',
            status='FAIL' if route.claims_room_acoustic_prediction else 'PASS',
            reason=(
                'route claims room-loudspeaker acoustic prediction — '
                'personal endpoints do not inherit room acoustics'
                if route.claims_room_acoustic_prediction
                else 'personal playback stays outside room loudspeaker '
                'prediction'
            ),
        )
    )

    checks.append(
        PersonalRouteCheckResult(
            check='coexistence_explicit',
            status='PASS' if route.coexistence != 'unknown' else 'UNKNOWN',
            reason=(
                f'room/personal coexistence recorded: {route.coexistence}'
                if route.coexistence != 'unknown'
                else 'room-speaker coexistence state not recorded'
            ),
        )
    )

    checks.append(
        PersonalRouteCheckResult(
            check='program_identity',
            status='PASS' if route.selected_program_id else 'UNKNOWN',
            reason=(
                f'selected program/mix bound: {route.selected_program_id}'
                if route.selected_program_id
                else 'no exact program/mix bound to this route'
            ),
        )
    )

    return tuple(checks)
