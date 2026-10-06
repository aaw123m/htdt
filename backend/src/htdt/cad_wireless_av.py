"""Wireless AV transport qualification authority (issue #717).

Wireless speakers, subwoofers, headphones and AV endpoints can have
correct logical routing while still failing on RF link quality,
interference, channel changes, packet loss/concealment, transport
latency, inter-speaker synchronization or reconnect behavior.
Provider claims (WiSA HT/E latency/sync, Bluetooth LE Audio profiles)
are provider-specific evidence — the installed system must be
field-qualified independently.

Basis: Bluetooth LE Audio over LE Isochronous Channels with Generic
Audio Framework; adopted BAP 1.0.2 (audio distribution) and CAP 1.0.1
(coordinated multi-device procedures); WiSA HT / WiSA E provider
claims for transport latency, inter-speaker synchronization and
RF/interference handling (provider evidence, not field truth).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


_TRANSPORT_KINDS = (
    'wisa_ht', 'wisa_e', 'bluetooth_le_audio', 'bluetooth_classic',
    'wifi', 'proprietary_rf', 'other', 'unknown',
)
_ENDPOINT_ROLES = (
    'speaker', 'subwoofer', 'headphones', 'transmitter', 'receiver',
    'source', 'other',
)

WirelessVerdict = Literal[
    'qualified',
    'routing_is_not_transport',
    'unmeasured_link',
    'dropout_evidence',
    'sync_not_demonstrated',
    'provider_claim_only',
]


class WirelessAVLink(BaseModel):
    """Declaration of one wireless AV transport link (#717).

    A link is declared transport + endpoints + codec/topology. The
    'provider_claimed' spec values are kept honest — they are claims,
    not field measurements.
    """

    model_config = ConfigDict(frozen=True)

    link_id: str
    link_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    transport_kind: Literal[
        'wisa_ht', 'wisa_e', 'bluetooth_le_audio', 'bluetooth_classic',
        'wifi', 'proprietary_rf', 'other', 'unknown',
    ]
    endpoint_roles: tuple[str, ...]
    codec_descriptor: str | None = None
    sample_rate_hz: int | None = None
    channel_topology: str | None = None
    claimed_latency_ms: float | None = None
    claimed_inter_speaker_sync_ms: float | None = None
    provider_claim_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('transport_kind') not in _TRANSPORT_KINDS:
                raise ValueError('unknown transport kind')
            roles = data.get('endpoint_roles') or ()
            if not roles:
                raise ValueError('a wireless link requires endpoints')
            unknown = [r for r in roles if r not in _ENDPOINT_ROLES]
            if unknown:
                raise ValueError(f'unknown endpoint roles: {unknown}')
            has_claim = (
                data.get('claimed_latency_ms') is not None
                or data.get('claimed_inter_speaker_sync_ms') is not None
            )
            if has_claim and data.get('provider_claim_ref') is None:
                raise ValueError(
                    'provider claims require a pinned provider claim '
                    'reference'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'link_id', 'link_sha256'}
        )

    @classmethod
    def create(cls, payload: dict[str, Any]) -> 'WirelessAVLink':
        return _seal(cls, payload, 'link_id', 'link_sha256', 'wav')


class WirelessTransportObservation(BaseModel):
    """Field-measured wireless transport evidence (#717).

    Binds the measured RF/link behaviour of a declared link over an
    observation window — latency, packet loss/concealment, dropouts,
    reconnects, channel changes. Only pinned observations qualify the
    link in place.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    link_ref: AuthorityRef
    measured_latency_ms: float | None = None
    packet_loss_fraction: float | None = None
    concealment_events: int = 0
    dropout_events: int = 0
    reconnect_events: int = 0
    channel_change_events: int = 0
    rf_environment_descriptor: str | None = None
    observation_window_s: float | None = None
    figure_ref: AuthorityRef | None = None
    data_ref: AuthorityRef | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('link_ref') is None:
                raise ValueError(
                    'a wireless observation requires a pinned link_ref'
                )
            if (
                data.get('figure_ref') is None
                and data.get('data_ref') is None
            ):
                raise ValueError(
                    'an observation without a pinned figure or data '
                    'is not evidence'
                )
            for key in (
                'dropout_events', 'reconnect_events',
                'channel_change_events', 'concealment_events',
            ):
                if data.get(key, 0) < 0:
                    raise ValueError(f'{key} must be non-negative')
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'},
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'WirelessTransportObservation':
        return _seal(
            cls, payload, 'observation_id', 'observation_sha256', 'wto'
        )


class WirelessSynchronizationEvidence(BaseModel):
    """Measured inter-speaker synchronization for wireless endpoints
    (#717). Binds measured offsets — declared topology is not sync
    evidence.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    link_ref: AuthorityRef
    observation_ref: AuthorityRef | None = None
    max_inter_speaker_offset_ms: float | None = None
    clock_reference_descriptor: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _validate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('link_ref') is None:
                raise ValueError('sync evidence requires a pinned link')
            if data.get('max_inter_speaker_offset_ms') is None:
                raise ValueError(
                    'sync evidence must bind a measured maximum offset'
                )
        return data

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(
        cls, payload: dict[str, Any]
    ) -> 'WirelessSynchronizationEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'wsync'
        )


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is None or ref.ref_sha256 is None:
            raise ValueError('authority references must be sha-pinned')


def evaluate_wireless_claim(
    link: WirelessAVLink | None,
    observation: WirelessTransportObservation | None,
    sync: WirelessSynchronizationEvidence | None = None,
    *,
    routing_verified: bool = False,
) -> tuple[WirelessVerdict, str]:
    """Judge a wireless transport claim (#717).

    - routing verified but no transport evidence →
      'routing_is_not_transport'
    - provider claims only → 'provider_claim_only'
    - unmeasured link → 'unmeasured_link'
    - dropouts/reconnects observed → 'dropout_evidence'
    - multi-endpoint claim without sync evidence →
      'sync_not_demonstrated'
    - otherwise → 'qualified'
    """
    if link is None:
        return 'unmeasured_link', 'no wireless link declared'
    if observation is None:
        if routing_verified:
            return (
                'routing_is_not_transport',
                'logical routing correct is not wireless transport '
                'evidence',
            )
        if link.provider_claim_ref is not None:
            return (
                'provider_claim_only',
                'provider spec claims are not field qualification',
            )
        return 'unmeasured_link', 'link never field-observed'
    if (
        observation.dropout_events > 0
        or observation.reconnect_events > 0
    ):
        return (
            'dropout_evidence',
            f'dropouts={observation.dropout_events} '
            f'reconnects={observation.reconnect_events} observed in '
            'field window',
        )
    audio_endpoints = {
        r for r in link.endpoint_roles
        if r in ('speaker', 'subwoofer', 'headphones')
    }
    if len(audio_endpoints) > 1:
        if sync is None or sync.max_inter_speaker_offset_ms is None:
            return (
                'sync_not_demonstrated',
                'multi-endpoint wireless claim needs measured '
                'inter-speaker sync',
            )
    return (
        'qualified',
        'link field-observed without dropouts and sync demonstrated '
        'where required',
    )


WIRELESS_LABELS: dict[str, str] = {
    'qualified': '無線伝送は現地検証済み',
    'routing_is_not_transport': '論理ルーティングは伝送証拠ではない',
    'unmeasured_link': '無線リンク未測定',
    'dropout_evidence': 'ドロップアウト/再接続を観測',
    'sync_not_demonstrated': 'スピーカー間同期が未実証',
    'provider_claim_only': 'プロバイダ主張のみ（現地未検証）',
}
