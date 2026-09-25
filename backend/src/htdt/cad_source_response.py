"""Frequency-dependent source response/excitation authority (#542).

A ``SourceFrequencyResponseAuthority`` is a sealed, versioned record bound to
one exact ``EquipmentDefinition`` (id + version + content hash) carrying the
speaker's *output* authority — the frequency-dependent on-axis response or
volume-velocity excitation under an explicit reference condition. It is
deliberately separate from directivity (the directional transfer, bound via
``DirectivityDataset``) and from the scalar ``SensitivityReference`` on the
definition — a single sensitivity number is never silently promoted into a
broadband response, and SPL→volume-velocity conversion happens only through
a versioned conversion authority, never ad-hoc arithmetic.

Multiple datasets per equipment are supported; DSP/playback transforms stay
separate — the response describes the loudspeaker output, not the chain
driving it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDefinition, FrequencyDomain
from .cad_scene import Direction3
from .r120_geometry_compiler import ExactExternalAuthorityRef
from hashlib import sha256

if TYPE_CHECKING:
    from .cad_equipment_repository import CadEquipmentRepository


SourceResponseCapabilityTier = Literal[
    'UNKNOWN',
    'RELATIVE_ON_AXIS_MAGNITUDE',
    'ABSOLUTE_FREE_FIELD_SPL',
    'COMPLEX_RESPONSE',
    'EXACT_VOLUME_VELOCITY',
    'ANALYTIC_RESPONSE',
]

_RESPONSE_PREFIX = 'source-response:'

TIER_LABELS: dict[str, str] = {
    'UNKNOWN': '不明 (応答特性なし)',
    'RELATIVE_ON_AXIS_MAGNITUDE': '相対オン軸振幅 (dB, 基準なし)',
    'ABSOLUTE_FREE_FIELD_SPL': '絶対自由場SPL応答',
    'COMPLEX_RESPONSE': '複素応答 (振幅+位相)',
    'EXACT_VOLUME_VELOCITY': '厳密体積速度 (wave励起用)',
    'ANALYTIC_RESPONSE': '解析モデル応答',
}

InputQuantityKind = Literal['voltage_v_rms', 'power_w', 'dimensionless']
FieldCondition = Literal[
    'free_field', 'half_space_baffle', 'in_room', 'unspecified'
]


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


class SourceResponseCondition(BaseModel):
    """Explicit reference condition the response was measured/claimed under."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    input_quantity: InputQuantityKind
    input_value: float = Field(gt=0.0)
    reference_distance_m: float = Field(gt=0.0)
    field_condition: FieldCondition = 'unspecified'
    mounting_condition: str = ''
    on_axis_direction: Direction3 | None = None
    calibration: str = ''

    @field_validator('input_value', 'reference_distance_m')
    @classmethod
    def finite_value(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('reference condition values must be finite')
        return value


class SourceResponseSample(BaseModel):
    """One frequency point of response evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frequency_hz: float = Field(gt=0)
    magnitude_db_spl: float | None = None
    phase_deg: float | None = None
    volume_velocity_m3_s: float | None = Field(default=None, gt=0.0)

    @field_validator('frequency_hz')
    @classmethod
    def finite_frequency(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('frequency must be finite')
        return value

    @model_validator(mode='after')
    def some_quantity(self) -> 'SourceResponseSample':
        if (
            self.magnitude_db_spl is None
            and self.phase_deg is None
            and self.volume_velocity_m3_s is None
        ):
            raise ValueError('response sample carries no quantity')
        return self


class SourceFrequencyResponseAuthority(BaseModel):
    """Sealed frequency-response authority bound to an exact
    EquipmentDefinition identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    response_id: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    label: str = Field(min_length=1)
    capability_tier: SourceResponseCapabilityTier
    condition: SourceResponseCondition | None = None
    valid_frequency_domain: FrequencyDomain | None = None
    response_samples: tuple[SourceResponseSample, ...] = ()
    provenance: str = Field(min_length=1)
    notes: str = ''
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_response(self) -> 'SourceFrequencyResponseAuthority':
        _require_iso8601(self.created_at_utc, 'response created_at_utc')
        if not self.response_id.startswith(_RESPONSE_PREFIX):
            raise ValueError('source response id must use source-response: prefix')
        sampled = self.capability_tier in (
            'RELATIVE_ON_AXIS_MAGNITUDE',
            'ABSOLUTE_FREE_FIELD_SPL',
            'COMPLEX_RESPONSE',
            'EXACT_VOLUME_VELOCITY',
            'ANALYTIC_RESPONSE',
        )
        if sampled and not self.response_samples:
            raise ValueError(
                f'capability tier {self.capability_tier} requires explicit '
                'response samples — no response is synthesized'
            )
        if sampled and self.condition is None:
            raise ValueError(
                'sampled response evidence requires an explicit reference '
                'condition (input quantity, distance, field condition)'
            )
        if sampled and self.valid_frequency_domain is None:
            raise ValueError(
                'sampled response evidence requires an explicit valid '
                'frequency domain'
            )
        if self.capability_tier == 'COMPLEX_RESPONSE' and any(
            sample.phase_deg is None for sample in self.response_samples
        ):
            raise ValueError(
                'COMPLEX_RESPONSE tier requires phase on every sample'
            )
        if self.capability_tier == 'EXACT_VOLUME_VELOCITY' and any(
            sample.volume_velocity_m3_s is None for sample in self.response_samples
        ):
            raise ValueError(
                'EXACT_VOLUME_VELOCITY tier requires volume velocity on '
                'every sample'
            )
        if self.capability_tier in (
            'RELATIVE_ON_AXIS_MAGNITUDE',
            'ABSOLUTE_FREE_FIELD_SPL',
            'COMPLEX_RESPONSE',
        ) and any(
            sample.magnitude_db_spl is None for sample in self.response_samples
        ):
            raise ValueError(
                f'{self.capability_tier} tier requires magnitude on every sample'
            )
        if self.capability_tier == 'ABSOLUTE_FREE_FIELD_SPL':
            # An absolute level claim must prove the exact field and
            # calibration semantics — an unspecified field condition or an
            # empty calibration note is never absolute evidence.
            if self.condition is not None and (
                self.condition.field_condition != 'free_field'
            ):
                raise ValueError(
                    'ABSOLUTE_FREE_FIELD_SPL requires an explicit free_field '
                    'condition'
                )
            if self.condition is not None and not self.condition.calibration:
                raise ValueError(
                    'ABSOLUTE_FREE_FIELD_SPL requires declared calibration '
                    'evidence'
                )
        if self.capability_tier == 'UNKNOWN' and self.response_samples:
            raise ValueError('UNKNOWN tier cannot carry response samples')
        # The declared frequency domain must honestly bound the evidence:
        # duplicate frequencies are ambiguous and out-of-domain samples are
        # unsupported claims, not evidence.
        frequencies = [
            sample.frequency_hz for sample in self.response_samples
        ]
        if len(frequencies) != len(set(frequencies)):
            raise ValueError(
                'duplicate response sample frequencies are not valid evidence'
            )
        if self.valid_frequency_domain is not None:
            outside = [
                frequency
                for frequency in frequencies
                if not (
                    self.valid_frequency_domain.minimum_hz
                    <= frequency
                    <= self.valid_frequency_domain.maximum_hz
                )
            ]
            if outside:
                raise ValueError(
                    'response samples lie outside the declared valid '
                    f'frequency domain: {outside}'
                )
        if self.semantic_sha256 != _hash(self.identity_payload()):
            raise ValueError('source response semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'response_id': self.response_id,
            'authority_version': self.authority_version,
            'equipment_definition_id': self.equipment_definition_id,
            'equipment_definition_version': self.equipment_definition_version,
            'equipment_definition_sha256': self.equipment_definition_sha256,
            'label': self.label,
            'capability_tier': self.capability_tier,
            'provenance': self.provenance,
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }
        if self.condition is not None:
            payload['condition'] = self.condition.model_dump(mode='json')
        if self.valid_frequency_domain is not None:
            payload['valid_frequency_domain'] = (
                self.valid_frequency_domain.model_dump(mode='json')
            )
        if self.response_samples:
            payload['response_samples'] = [
                sample.model_dump(mode='json')
                for sample in self.response_samples
            ]
        return payload

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.response_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_sha256,
        )


def build_source_response(
    *,
    equipment_definition: EquipmentDefinition,
    label: str,
    capability_tier: SourceResponseCapabilityTier,
    provenance: str,
    condition: SourceResponseCondition | None = None,
    valid_frequency_domain: FrequencyDomain | None = None,
    response_samples: tuple[SourceResponseSample, ...] = (),
    notes: str = '',
    authority_version: str = '1',
    response_id: str | None = None,
    created_at_utc: str | None = None,
) -> SourceFrequencyResponseAuthority:
    """Assemble a sealed source-response authority bound to the exact
    equipment definition."""

    payload: dict[str, Any] = {
        'response_id': response_id or f'{_RESPONSE_PREFIX}{uuid4()}',
        'authority_version': authority_version,
        'equipment_definition_id': equipment_definition.definition_id,
        'equipment_definition_version': equipment_definition.version,
        'equipment_definition_sha256': equipment_definition.semantic_sha256,
        'label': label,
        'capability_tier': capability_tier,
        'condition': condition,
        'valid_frequency_domain': valid_frequency_domain,
        'response_samples': response_samples,
        'provenance': provenance,
        'notes': notes,
        'created_at_utc': created_at_utc or _utc_now(),
    }
    provisional = SourceFrequencyResponseAuthority.model_construct(
        **payload,
        semantic_sha256='0' * 64,
    )
    return SourceFrequencyResponseAuthority.model_validate(
        {
            **payload,
            'semantic_sha256': _hash(provisional.identity_payload()),
        }
    )


def response_capability_label(tier: SourceResponseCapabilityTier) -> str:
    return TIER_LABELS.get(tier, tier)


class CadSourceResponseRepository:
    """SQLite persistence for source-response authorities and the per-
    (document, equipment) selection consumed by R110 compilation.

    Pass the project's ``CadEquipmentRepository`` so ``save_response``
    re-resolves the bound ``EquipmentDefinition`` instead of trusting
    caller-declared id/version/hash strings.
    """

    def __init__(
        self,
        path: Path | str,
        equipment_repository: 'CadEquipmentRepository | None' = None,
    ) -> None:
        self.path = Path(path)
        self.equipment_repository = equipment_repository
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_source_responses (
                    response_id TEXT PRIMARY KEY,
                    equipment_definition_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_source_response_selections (
                    document_id TEXT NOT NULL,
                    equipment_definition_id TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    PRIMARY KEY (document_id, equipment_definition_id)
                )
                """
            )

    def save_response(
        self,
        response: SourceFrequencyResponseAuthority,
    ) -> None:
        """Persist an immutable source-response authority: same id +
        byte-identical payload is an idempotent no-op; a different payload
        under an existing id is a collision — a revised authority needs a
        new identity. With an equipment repository wired, the declared
        EquipmentDefinition id/version/hash must re-resolve exactly."""
        if self.equipment_repository is not None:
            definition = self.equipment_repository.get_definition(
                response.equipment_definition_id,
                response.equipment_definition_version,
            )
            if definition is None:
                raise ValueError(
                    'source response binds an EquipmentDefinition that does '
                    f'not resolve: {response.equipment_definition_id} '
                    f'version {response.equipment_definition_version}'
                )
            if definition.semantic_sha256 != response.equipment_definition_sha256:
                raise ValueError(
                    'source response EquipmentDefinition hash does not match '
                    'the persisted definition authority'
                )
        payload_json = response.model_dump_json()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_source_responses '
                'WHERE response_id=?',
                (response.response_id,),
            ).fetchone()
            if row is not None:
                if row['payload_json'] == payload_json:
                    return
                raise ValueError(
                    f'source response id collision with different payload: '
                    f'{response.response_id}'
                )
            connection.execute(
                'INSERT INTO cad_source_responses'
                '(response_id, equipment_definition_id, payload_json)'
                ' VALUES(?,?,?)',
                (
                    response.response_id,
                    response.equipment_definition_id,
                    payload_json,
                ),
            )

    def get_response(
        self,
        response_id: str,
    ) -> SourceFrequencyResponseAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_source_responses WHERE response_id=?',
                (response_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceFrequencyResponseAuthority.model_validate_json(
            row['payload_json']
        )

    def get_response_by_sha256(
        self,
        semantic_sha256: str,
    ) -> SourceFrequencyResponseAuthority | None:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_source_responses'
            ).fetchall()
        for row in rows:
            response = SourceFrequencyResponseAuthority.model_validate_json(
                row['payload_json']
            )
            if response.semantic_sha256 == semantic_sha256:
                return response
        return None

    def list_responses_for_equipment(
        self,
        equipment_definition_id: str,
    ) -> tuple[SourceFrequencyResponseAuthority, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_source_responses'
                ' WHERE equipment_definition_id=? ORDER BY response_id ASC',
                (equipment_definition_id,),
            ).fetchall()
        return tuple(
            SourceFrequencyResponseAuthority.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def select_response(
        self,
        document_id: str,
        response: SourceFrequencyResponseAuthority,
    ) -> None:
        """Record the response choice for one equipment in one document.

        The response must be persisted with an identical semantic hash —
        selection never trusts an unpersisted or mutated authority.
        """
        persisted = self.get_response(response.response_id)
        if persisted is None:
            raise ValueError(
                f'selected source response is not persisted: '
                f'{response.response_id}'
            )
        if persisted.semantic_sha256 != response.semantic_sha256:
            raise ValueError(
                f'selected source response hash does not match the persisted '
                f'authority: {response.response_id}'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_source_response_selections'
                '(document_id, equipment_definition_id, response_id,'
                ' response_sha256) VALUES(?,?,?,?)'
                ' ON CONFLICT(document_id, equipment_definition_id)'
                ' DO UPDATE SET response_id=excluded.response_id,'
                ' response_sha256=excluded.response_sha256',
                (
                    document_id,
                    response.equipment_definition_id,
                    response.response_id,
                    response.semantic_sha256,
                ),
            )

    def clear_selection(
        self,
        document_id: str,
        equipment_definition_id: str,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_source_response_selections'
                ' WHERE document_id=? AND equipment_definition_id=?',
                (document_id, equipment_definition_id),
            )

    def selected_response(
        self,
        document_id: str,
        equipment_definition_id: str,
    ) -> SourceFrequencyResponseAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT response_id, response_sha256'
                ' FROM cad_source_response_selections'
                ' WHERE document_id=? AND equipment_definition_id=?',
                (document_id, equipment_definition_id),
            ).fetchone()
        if row is None:
            return None
        response = self.get_response(row['response_id'])
        if response is None:
            return None
        if response.semantic_sha256 != row['response_sha256']:
            raise ValueError(
                f'selected source response {row["response_id"]} hash '
                'mismatch — refusing to resolve a different authority'
            )
        return response


__all__ = [
    'CadSourceResponseRepository',
    'FieldCondition',
    'InputQuantityKind',
    'SourceFrequencyResponseAuthority',
    'SourceResponseCapabilityTier',
    'SourceResponseCondition',
    'SourceResponseSample',
    'build_source_response',
    'response_capability_label',
]
