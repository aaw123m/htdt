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
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import FrequencyDomain
from .cad_video_geometry import AngleRange
from .r120_geometry_compiler import ExactExternalAuthorityRef
from hashlib import sha256

if TYPE_CHECKING:
    from .cad_repository import SceneRepository


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
    #: Project the screen binding was authored under. ``None`` declares an
    #: unscoped/reusable product definition; a project-local transfer always
    #: pins the exact document so a colliding local entity id in another
    #: project cannot satisfy the binding by string match alone.
    document_id: str | None = Field(default=None, min_length=1)
    label: str = Field(min_length=1)
    capability_tier: TransferCapabilityTier
    transfer_samples: tuple[TransferSample, ...] = ()
    valid_frequency_domain: FrequencyDomain | None = None
    valid_incidence_angle_deg: AngleRange | None = None
    #: Exact measured dataset/source-artifact authority a MEASURED_DATASET
    #: claim resolves to — a free-text provenance label is not measured
    #: evidence authority.
    measured_dataset_ref: ExactExternalAuthorityRef | None = None
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
        if self.capability_tier == 'MEASURED_DATASET' and (
            self.measured_dataset_ref is None
        ):
            raise ValueError(
                'MEASURED_DATASET tier requires an exact measured dataset '
                'authority reference — a free-text provenance label is not '
                'measured evidence; use a lower capability tier instead'
            )
        if self.transfer_samples and self.valid_frequency_domain is None:
            raise ValueError(
                'sampled transfer evidence requires an explicit valid '
                'frequency domain'
            )
        if self.capability_tier in (
            'FREQUENCY_AND_ANGLE',
            'COMPLEX',
            'MEASURED_DATASET',
        ) and self.valid_incidence_angle_deg is None:
            raise ValueError(
                f'capability tier {self.capability_tier} requires an explicit '
                'valid incidence-angle domain'
            )
        # Evidence can never claim more than the declared valid domains: every
        # sample must sit inside the declared frequency domain, and every
        # declared incidence angle inside the declared angle range.
        if self.valid_frequency_domain is not None:
            outside = [
                sample.frequency_hz
                for sample in self.transfer_samples
                if not (
                    self.valid_frequency_domain.minimum_hz
                    <= sample.frequency_hz
                    <= self.valid_frequency_domain.maximum_hz
                )
            ]
            if outside:
                raise ValueError(
                    'transfer samples lie outside the declared valid '
                    f'frequency domain: {outside}'
                )
        if self.valid_incidence_angle_deg is not None:
            outside_angles = [
                sample.incidence_angle_deg
                for sample in self.transfer_samples
                if sample.incidence_angle_deg is not None
                and not (
                    self.valid_incidence_angle_deg.minimum_deg
                    <= sample.incidence_angle_deg
                    <= self.valid_incidence_angle_deg.maximum_deg
                )
            ]
            if outside_angles:
                raise ValueError(
                    'transfer samples lie outside the declared valid '
                    f'incidence-angle domain: {outside_angles}'
                )
        sample_points = [
            (sample.frequency_hz, sample.incidence_angle_deg)
            for sample in self.transfer_samples
        ]
        if len(sample_points) != len(set(sample_points)):
            raise ValueError(
                'duplicate frequency/incidence-angle samples are not valid '
                'evidence'
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
        if self.measured_dataset_ref is not None:
            payload['measured_dataset_ref'] = (
                self.measured_dataset_ref.model_dump(mode='json')
            )
        if self.document_id is not None:
            payload['document_id'] = self.document_id
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
    measured_dataset_ref: ExactExternalAuthorityRef | None = None,
    measurement_condition: str = '',
    notes: str = '',
    authority_version: str = '1',
    transfer_id: str | None = None,
    document_id: str | None = None,
    created_at_utc: str | None = None,
) -> AcousticScreenTransferAuthority:
    """Assemble a sealed screen-transfer authority."""

    payload: dict[str, Any] = {
        'transfer_id': transfer_id or f'{_TRANSFER_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'screen_entity_id': screen_entity_id,
        'document_id': document_id,
        'label': label,
        'capability_tier': capability_tier,
        'transfer_samples': transfer_samples,
        'valid_frequency_domain': valid_frequency_domain,
        'valid_incidence_angle_deg': valid_incidence_angle_deg,
        'measured_dataset_ref': measured_dataset_ref,
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

    def __init__(
        self,
        path: Path | str,
        scene_repository: 'SceneRepository | None' = None,
    ) -> None:
        self.path = Path(path)
        self.scene_repository = scene_repository
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
        """Persist an immutable transfer authority: same id + byte-identical
        payload is an idempotent no-op; a different payload under an existing
        id is a collision — a revised authority needs a new identity."""
        payload_json = transfer.model_dump_json()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_screen_transfers'
                ' WHERE transfer_id=?',
                (transfer.transfer_id,),
            ).fetchone()
            if row is not None:
                if row['payload_json'] == payload_json:
                    return
                raise ValueError(
                    f'screen transfer id collision with different payload: '
                    f'{transfer.transfer_id}'
                )
            connection.execute(
                'INSERT INTO cad_screen_transfers'
                '(transfer_id, screen_entity_id, payload_json) VALUES(?,?,?)',
                (
                    transfer.transfer_id,
                    transfer.screen_entity_id,
                    payload_json,
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
        """Record the transfer choice for one screen in one document.

        The transfer must be persisted with an identical semantic hash, and
        a project-bound transfer may only serve the document it was authored
        for. When a ``SceneRepository`` is wired the document must exist and
        the named screen entity must be present with kind ``screen``.
        """
        persisted = self.get_transfer(transfer.transfer_id)
        if persisted is None:
            raise ValueError(
                f'selected screen transfer is not persisted: '
                f'{transfer.transfer_id}'
            )
        if persisted.semantic_sha256 != transfer.semantic_sha256:
            raise ValueError(
                f'selected screen transfer hash does not match the persisted '
                f'authority: {transfer.transfer_id}'
            )
        if transfer.document_id is not None and transfer.document_id != (
            document_id
        ):
            raise ValueError(
                f'screen transfer {transfer.transfer_id} was authored for '
                f'document {transfer.document_id}, not {document_id}'
            )
        if self.scene_repository is not None:
            revision = self.scene_repository.current_head(document_id)
            if revision is None:
                raise ValueError(f'document does not exist: {document_id}')
            screen = next(
                (
                    entity
                    for entity in revision.document.entities
                    if entity.entity_id == transfer.screen_entity_id
                ),
                None,
            )
            if screen is None:
                raise ValueError(
                    f'screen entity {transfer.screen_entity_id} does not '
                    f'exist in document {document_id}'
                )
            if screen.kind != 'screen':
                raise ValueError(
                    f'entity {transfer.screen_entity_id} in document '
                    f'{document_id} is not a screen (kind={screen.kind})'
                )
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

    def selections_for_document(
        self,
        document_id: str,
    ) -> dict[str, AcousticScreenTransferAuthority]:
        """Every verified transfer selection recorded for this document."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT screen_entity_id, transfer_id, transfer_sha256'
                ' FROM cad_screen_transfer_selections WHERE document_id=?'
                ' ORDER BY screen_entity_id ASC',
                (document_id,),
            ).fetchall()
        result: dict[str, AcousticScreenTransferAuthority] = {}
        for row in rows:
            transfer = self.get_transfer(row['transfer_id'])
            if (
                transfer is None
                or transfer.semantic_sha256 != row['transfer_sha256']
            ):
                continue
            result[row['screen_entity_id']] = transfer
        return result

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
