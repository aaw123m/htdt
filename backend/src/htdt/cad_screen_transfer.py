"""Acoustic screen-transfer authority — the R-Series thin-transmission
surface contract (#541).

An ``AcousticScreenTransferAuthority`` is a sealed, versioned record that
carries the measured/claimed acoustic transfer behaviour of one screen
entity — separate from the video-geometry identity (screen pixels stay a
display concern). The capability tier says *what kind of evidence* the
authority holds, from ``UNKNOWN`` up to a measured dataset with
frequency-and-angle-dependent coefficients; propagation models apply only
what the tier supports, and an R150 wave lane reports UNSUPPORTED until an
exact thin-interface model exists — an "AT screen" claim is never treated
as acoustic truth and ``acoustically_transparent=False`` from legacy v1
bindings is never reinterpreted as zero transmission.

The authority binds geometrically to the screen plane via
``screen_entity_id``; multiple speakers share the same authority — transfer
is a property of the screen, not baked into any EquipmentDefinition.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import FrequencyDomain
from .cad_video_geometry import AngleRange
from .r120_geometry_compiler import ExactExternalAuthorityRef
from hashlib import sha256


TransferCapabilityTier = Literal[
    'UNKNOWN',
    'AT_CLAIM',
    'MAGNITUDE_NORMAL_INCIDENCE',
    'FREQUENCY_AND_ANGLE',
    'COMPLEX',
    'TRANSMISSION_AND_REFLECTION',
    'MEASURED_DATASET',
]

_TRANSFER_PREFIX = 'screen-transfer:'

TIER_LABELS: dict[str, str] = {
    'UNKNOWN': '不明 (transfer特性なし)',
    'AT_CLAIM': 'AT主張 (測定データなしの透過フラグ)',
    'MAGNITUDE_NORMAL_INCIDENCE': '法線入射のみの振幅特性',
    'FREQUENCY_AND_ANGLE': '周波数+入射角依存の振幅特性',
    'COMPLEX': '複素透過 (位相あり)',
    'TRANSMISSION_AND_REFLECTION': '透過+反射特性',
    'MEASURED_DATASET': '実測データセット',
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _require_iso8601(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{name} must be timezone-aware')


class TransferSample(BaseModel):
    """One measured/claimed transmission coefficient point."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0)
    incidence_angle_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    magnitude: float | None = Field(default=None, ge=0.0, le=1.0)
    phase_deg: float | None = None
    reflection_magnitude: float | None = Field(default=None, ge=0.0, le=1.0)


class AcousticScreenTransferAuthority(BaseModel):
    """Sealed transfer-surface authority bound to one screen entity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    transfer_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    screen_entity_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    capability_tier: TransferCapabilityTier
    transfer_samples: tuple[TransferSample, ...] = ()
    valid_frequency_domain: FrequencyDomain | None = None
    valid_incidence_angle_deg: AngleRange | None = None
    measurement_condition: str = ''
    provenance: str = Field(min_length=1)
    notes: str = ''
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_transfer(self) -> 'AcousticScreenTransferAuthority':
        _require_iso8601(self.created_at_utc, 'transfer created_at_utc')
        if not self.transfer_id.startswith(_TRANSFER_PREFIX):
            raise ValueError('screen transfer id must use screen-transfer: prefix')
        if self.capability_tier in (
            'MAGNITUDE_NORMAL_INCIDENCE',
            'FREQUENCY_AND_ANGLE',
            'COMPLEX',
            'TRANSMISSION_AND_REFLECTION',
            'MEASURED_DATASET',
        ) and not self.transfer_samples:
            raise ValueError(
                f'capability tier {self.capability_tier} requires '
                'explicit transfer samples — no coefficients may be invented'
            )
        if self.capability_tier in ('UNKNOWN', 'AT_CLAIM') and self.transfer_samples:
            raise ValueError(
                f'capability tier {self.capability_tier} cannot carry '
                'transfer samples — raise the tier to match the evidence'
            )
        if self.capability_tier == 'COMPLEX' and any(
            sample.phase_deg is None for sample in self.transfer_samples
        ):
            raise ValueError('COMPLEX tier requires phase on every sample')
        if self.capability_tier == 'TRANSMISSION_AND_REFLECTION' and any(
            sample.reflection_magnitude is None for sample in self.transfer_samples
        ):
            raise ValueError(
                'TRANSMISSION_AND_REFLECTION tier requires reflection on every sample'
            )
        if self.capability_tier in ('FREQUENCY_AND_ANGLE', 'COMPLEX') and any(
            sample.incidence_angle_deg is None for sample in self.transfer_samples
        ):
            raise ValueError(
                f'capability tier {self.capability_tier} requires an incidence '
                'angle on every sample'
            )
        if self.transfer_samples and self.valid_frequency_domain is None:
            raise ValueError(
                'sampled transfer evidence requires an explicit valid '
                'frequency domain'
            )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('screen transfer semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'transfer_id': self.transfer_id,
            'authority_version': self.authority_version,
            'screen_entity_id': self.screen_entity_id,
            'label': self.label,
            'capability_tier': self.capability_tier,
            'provenance': self.provenance,
            'measurement_condition': self.measurement_condition,
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }
        # Optional evidence fields join the identity only when present —
        # additive convention, same as the other sealed authorities.
        if self.transfer_samples:
            payload['transfer_samples'] = [
                sample.model_dump(mode='json') for sample in self.transfer_samples
            ]
        if self.valid_frequency_domain is not None:
            payload['valid_frequency_domain'] = (
                self.valid_frequency_domain.model_dump(mode='json')
            )
        if self.valid_incidence_angle_deg is not None:
            payload['valid_incidence_angle_deg'] = (
                self.valid_incidence_angle_deg.model_dump(mode='json')
            )
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.transfer_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_screen_transfer(
    *,
    screen_entity_id: str,
    label: str,
    capability_tier: TransferCapabilityTier,
    provenance: str,
    transfer_samples: tuple[TransferSample, ...] = (),
    valid_frequency_domain: FrequencyDomain | None = None,
    valid_incidence_angle_deg: AngleRange | None = None,
    measurement_condition: str = '',
    notes: str = '',
    authority_version: str = '1',
    transfer_id: str | None = None,
    created_at_utc: str | None = None,
) -> AcousticScreenTransferAuthority:
    """Assemble a sealed screen-transfer authority."""

    payload: dict[str, Any] = {
        'transfer_id': transfer_id or f'{_TRANSFER_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'screen_entity_id': screen_entity_id,
        'label': label,
        'capability_tier': capability_tier,
        'transfer_samples': transfer_samples,
        'valid_frequency_domain': valid_frequency_domain,
        'valid_incidence_angle_deg': valid_incidence_angle_deg,
        'measurement_condition': measurement_condition,
        'provenance': provenance,
        'notes': notes,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = AcousticScreenTransferAuthority.model_construct(
        **payload,
        semantic_sha256='0' * 64,
    )
    return AcousticScreenTransferAuthority.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def transfer_capability_label(tier: TransferCapabilityTier) -> str:
    return TIER_LABELS.get(tier, tier)


class CadScreenTransferRepository:
    """SQLite persistence for screen-transfer authorities and per-screen
    selection (the geometric binding of the authority to the screen plane
    lives on ``ScreenGeometryBinding.screen_transfer_ref``; this table is
    the product-level current choice per document)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_screen_transfers (
                    transfer_id TEXT PRIMARY KEY,
                    screen_entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_screen_transfer_selections (
                    document_id TEXT NOT NULL,
                    screen_entity_id TEXT NOT NULL,
                    transfer_id TEXT NOT NULL,
                    transfer_sha256 TEXT NOT NULL,
                    PRIMARY KEY (document_id, screen_entity_id)
                )
                """
            )

    def save_transfer(
        self,
        transfer: AcousticScreenTransferAuthority,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_screen_transfers'
                '(transfer_id, screen_entity_id, payload_json) VALUES(?,?,?)'
                ' ON CONFLICT(transfer_id) DO UPDATE SET'
                ' screen_entity_id=excluded.screen_entity_id,'
                ' payload_json=excluded.payload_json',
                (
                    transfer.transfer_id,
                    transfer.screen_entity_id,
                    transfer.model_dump_json(),
                ),
            )

    def get_transfer(
        self,
        transfer_id: str,
    ) -> AcousticScreenTransferAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_screen_transfers WHERE transfer_id=?',
                (transfer_id,),
            ).fetchone()
        if row is None:
            return None
        return AcousticScreenTransferAuthority.model_validate_json(
            row['payload_json']
        )

    def list_transfers_for_screen(
        self,
        screen_entity_id: str,
    ) -> tuple[AcousticScreenTransferAuthority, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_screen_transfers'
                ' WHERE screen_entity_id=? ORDER BY transfer_id ASC',
                (screen_entity_id,),
            ).fetchall()
        return tuple(
            AcousticScreenTransferAuthority.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def select_transfer(
        self,
        document_id: str,
        transfer: AcousticScreenTransferAuthority,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_screen_transfer_selections'
                '(document_id, screen_entity_id, transfer_id, transfer_sha256)'
                ' VALUES(?,?,?,?)'
                ' ON CONFLICT(document_id, screen_entity_id) DO UPDATE SET'
                ' transfer_id=excluded.transfer_id,'
                ' transfer_sha256=excluded.transfer_sha256',
                (
                    document_id,
                    transfer.screen_entity_id,
                    transfer.transfer_id,
                    transfer.semantic_sha256,
                ),
            )

    def clear_selection(self, document_id: str, screen_entity_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_screen_transfer_selections'
                ' WHERE document_id=? AND screen_entity_id=?',
                (document_id, screen_entity_id),
            )

    def selected_transfer(
        self,
        document_id: str,
        screen_entity_id: str,
    ) -> AcousticScreenTransferAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT transfer_id, transfer_sha256'
                ' FROM cad_screen_transfer_selections'
                ' WHERE document_id=? AND screen_entity_id=?',
                (document_id, screen_entity_id),
            ).fetchone()
        if row is None:
            return None
        transfer = self.get_transfer(row['transfer_id'])
        if transfer is None:
            return None
        if transfer.semantic_sha256 != row['transfer_sha256']:
            raise ValueError(
                f'selected screen transfer {row["transfer_id"]} hash '
                'mismatch — refusing to resolve a different authority than '
                'was selected'
            )
        return transfer


__all__ = [
    'AcousticScreenTransferAuthority',
    'CadScreenTransferRepository',
    'TransferCapabilityTier',
    'TransferSample',
    'build_screen_transfer',
    'transfer_capability_label',
]
