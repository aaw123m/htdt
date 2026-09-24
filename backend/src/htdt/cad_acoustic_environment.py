"""Versioned acoustic-environment authority for prediction and measurement (#479).

A bare sound-speed number is not acoustic truth: ``AcousticEnvironmentProfile``
pins ``sound_speed_m_s``, ``temperature_c``, their per-value provenance kinds
and a free-text provenance note into one sealed authority. Profiles are shared
library records (``authority_id`` is content-derived); a per-document selection
row makes "which environment was the prediction run under" explicit and lets a
profile change stale dependent predictions.

Value+source integrity mirrors ``SnapshotEnvironmentAuthorityRef``: an unknown
speed has no fabricated 343 m/s, a declared value always carries its source
kind, and derived field-level source refs keep provenance exact without
inventing physics.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import SnapshotEnvironmentAuthorityRef
from .cad_schema import ensure_native_schema
from .r120_geometry_compiler import ExactExternalAuthorityRef


ACOUSTIC_ENVIRONMENT_SCHEMA_VERSION = 1
ACOUSTIC_ENVIRONMENT_AUTHORITY_VERSION = '1'

EnvironmentSourceKind = Literal[
    'nominal_assumption',
    'derived_from_temperature',
    'manual_measured',
    'unknown',
]
EnvironmentCompatibility = Literal['same', 'different', 'unknown']

# Documented standard assumption used by the shared default profile: the 20 °C
# nominal value is labelled an assumption, never measured truth.
NOMINAL_SOUND_SPEED_M_S = 343.0
NOMINAL_TEMPERATURE_C = 20.0


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class AcousticEnvironmentProfile(BaseModel):
    """Sealed environment authority; ids and hashes are content-derived."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ACOUSTIC_ENVIRONMENT_SCHEMA_VERSION
    authority_version: Literal['1'] = ACOUSTIC_ENVIRONMENT_AUTHORITY_VERSION
    authority_id: str = Field(pattern=r'^acoustic-environment:[0-9a-f]{64}$')
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    label: str = Field(min_length=1)
    sound_speed_m_s: float | None = Field(default=None, gt=0.0)
    temperature_c: float | None = None
    sound_speed_source_kind: EnvironmentSourceKind
    temperature_source_kind: EnvironmentSourceKind | None = None
    provenance: str = ''
    created_at_utc: str = Field(min_length=1)
    notes: str = ''

    @model_validator(mode='after')
    def valid_profile(self) -> 'AcousticEnvironmentProfile':
        for name, value in (
            ('sound_speed_m_s', self.sound_speed_m_s),
            ('temperature_c', self.temperature_c),
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{name} must be finite')
        if self.sound_speed_source_kind == 'unknown':
            if self.sound_speed_m_s is not None:
                raise ValueError('unknown sound-speed source cannot carry a value')
        elif self.sound_speed_m_s is None:
            raise ValueError('non-unknown sound speed requires a source kind and value')
        if self.sound_speed_source_kind == 'derived_from_temperature':
            if self.temperature_c is None:
                raise ValueError('temperature-derived sound speed requires temperature_c')
            if self.temperature_source_kind is None:
                raise ValueError('temperature-derived sound speed requires temperature provenance')
        if self.temperature_c is None and self.temperature_source_kind is not None:
            raise ValueError('temperature source kind requires a temperature value')
        if self.temperature_c is not None and self.temperature_source_kind is None:
            raise ValueError('a temperature value requires a source kind')
        if self.temperature_source_kind == 'unknown' and self.temperature_c is not None:
            raise ValueError('unknown temperature source cannot carry a value')
        try:
            parsed = datetime.fromisoformat(self.created_at_utc)
        except ValueError as exc:
            raise ValueError('created_at_utc must be ISO-8601') from exc
        if parsed.tzinfo is None:
            raise ValueError('created_at_utc must be timezone-aware')
        if self.semantic_hash_sha256 != _hash(self.identity_payload()):
            raise ValueError('acoustic environment profile hash mismatch')
        if self.authority_id != f'acoustic-environment:{self.semantic_hash_sha256}':
            raise ValueError('acoustic environment profile id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'label': self.label,
            'sound_speed_m_s': self.sound_speed_m_s,
            'temperature_c': self.temperature_c,
            'sound_speed_source_kind': self.sound_speed_source_kind,
            'temperature_source_kind': self.temperature_source_kind,
            'provenance': self.provenance,
            'created_at_utc': self.created_at_utc,
            'notes': self.notes,
        }

    def authority_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.authority_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.semantic_hash_sha256,
        )


def _field_source_ref(
    profile: AcousticEnvironmentProfile,
    field_name: str,
    value: float,
) -> ExactExternalAuthorityRef:
    """Field-level provenance ref inside one exact profile (not new physics)."""

    payload = {
        'profile_authority_id': profile.authority_id,
        'profile_semantic_hash_sha256': profile.semantic_hash_sha256,
        'field': field_name,
        'value': value,
    }
    digest = _hash(payload)
    return ExactExternalAuthorityRef(
        authority_id=f'acoustic-environment-field:{digest}',
        authority_version='1',
        semantic_hash_sha256=digest,
    )


def build_acoustic_environment_profile(
    *,
    label: str,
    sound_speed_source_kind: EnvironmentSourceKind,
    sound_speed_m_s: float | None = None,
    temperature_c: float | None = None,
    temperature_source_kind: EnvironmentSourceKind | None = None,
    provenance: str = '',
    notes: str = '',
    created_at_utc: str | None = None,
) -> AcousticEnvironmentProfile:
    """Assemble one sealed environment profile with a content-derived id."""

    payload = {
        'label': label,
        'sound_speed_m_s': sound_speed_m_s,
        'temperature_c': temperature_c,
        'sound_speed_source_kind': sound_speed_source_kind,
        'temperature_source_kind': temperature_source_kind,
        'provenance': provenance,
        'created_at_utc': created_at_utc or datetime.now(timezone.utc).isoformat(),
        'notes': notes,
    }
    digest = _hash(
        {
            'schema_version': ACOUSTIC_ENVIRONMENT_SCHEMA_VERSION,
            'authority_version': ACOUSTIC_ENVIRONMENT_AUTHORITY_VERSION,
            **payload,
        }
    )
    return AcousticEnvironmentProfile(
        authority_id=f'acoustic-environment:{digest}',
        semantic_hash_sha256=digest,
        **payload,
    )


def nominal_environment_profile() -> AcousticEnvironmentProfile:
    """Shared 343 m/s / 20 °C default — deterministic and labelled assumption."""

    return build_acoustic_environment_profile(
        label='標準仮定 (343 m/s, 20 °C)',
        sound_speed_source_kind='nominal_assumption',
        sound_speed_m_s=NOMINAL_SOUND_SPEED_M_S,
        temperature_c=NOMINAL_TEMPERATURE_C,
        temperature_source_kind='nominal_assumption',
        provenance='standard-assumption',
        created_at_utc='1970-01-01T00:00:00+00:00',
    )


def sound_speed_from_temperature_c(temperature_c: float) -> float:
    """Documented derivation c = 331.3 + 0.606·T (m/s); never applied silently."""

    return 331.3 + 0.606 * temperature_c


def snapshot_environment_ref(
    profile: AcousticEnvironmentProfile,
) -> SnapshotEnvironmentAuthorityRef:
    """Project a profile into the snapshot authority ref (value+source pairs)."""

    sound_speed_source = (
        None
        if profile.sound_speed_m_s is None
        else _field_source_ref(profile, 'sound_speed_m_s', profile.sound_speed_m_s)
    )
    temperature_source = (
        None
        if profile.temperature_c is None
        else _field_source_ref(profile, 'temperature_c', profile.temperature_c)
    )
    return SnapshotEnvironmentAuthorityRef(
        authority=profile.authority_ref(),
        sound_speed_m_s=profile.sound_speed_m_s,
        sound_speed_source_authority=sound_speed_source,
        temperature_c=profile.temperature_c,
        temperature_source_authority=temperature_source,
    )


def environment_compatibility(
    prediction_environment: ExactExternalAuthorityRef | None,
    measurement_environment: ExactExternalAuthorityRef | None,
) -> EnvironmentCompatibility:
    """Same/different/unknown comparison for predicted-vs-measured contexts."""

    if prediction_environment is None or measurement_environment is None:
        return 'unknown'
    if (
        prediction_environment.authority_id == measurement_environment.authority_id
        and prediction_environment.semantic_hash_sha256
        == measurement_environment.semantic_hash_sha256
    ):
        return 'same'
    return 'different'


class CadAcousticEnvironmentRepository:
    """Persist environment profiles and per-document profile selections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS cad_environment_profiles (
                    authority_id TEXT PRIMARY KEY,
                    semantic_hash_sha256 TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                '''
            )
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS cad_environment_selections (
                    document_id TEXT PRIMARY KEY,
                    authority_id TEXT NOT NULL,
                    semantic_hash_sha256 TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                '''
            )

    @staticmethod
    def _payload(profile: AcousticEnvironmentProfile) -> str:
        return json.dumps(
            profile.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )

    def save_profile(self, profile: AcousticEnvironmentProfile) -> None:
        payload = self._payload(profile)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_environment_profiles WHERE authority_id=?',
                (profile.authority_id,),
            ).fetchone()
            if existing is not None:
                if str(existing['payload_json']) != payload:
                    raise ValueError(
                        'acoustic environment profile id collision with different payload'
                    )
                return
            connection.execute(
                '''
                INSERT INTO cad_environment_profiles(
                    authority_id, semantic_hash_sha256, label, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    profile.authority_id,
                    profile.semantic_hash_sha256,
                    profile.label,
                    profile.created_at_utc,
                    payload,
                ),
            )

    def get_profile(self, authority_id: str) -> AcousticEnvironmentProfile | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_environment_profiles WHERE authority_id=?',
                (authority_id,),
            ).fetchone()
        if row is None:
            return None
        return AcousticEnvironmentProfile.model_validate(json.loads(str(row['payload_json'])))

    def list_profiles(self) -> tuple[AcousticEnvironmentProfile, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_environment_profiles '
                'ORDER BY created_at_utc ASC, authority_id ASC'
            ).fetchall()
        return tuple(
            AcousticEnvironmentProfile.model_validate(json.loads(str(row['payload_json'])))
            for row in rows
        )

    def ensure_default_profile(self) -> AcousticEnvironmentProfile:
        """Persist the shared nominal-assumption profile once and return it."""

        profile = nominal_environment_profile()
        self.save_profile(profile)
        return profile

    def select_profile(
        self,
        document_id: str,
        profile: AcousticEnvironmentProfile,
    ) -> None:
        if not document_id:
            raise ValueError('document_id must not be empty')
        if self.get_profile(profile.authority_id) is None:
            raise ValueError('selected environment profile is not persisted')
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO cad_environment_selections(
                    document_id, authority_id, semantic_hash_sha256, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    authority_id=excluded.authority_id,
                    semantic_hash_sha256=excluded.semantic_hash_sha256,
                    updated_at_utc=excluded.updated_at_utc
                ''',
                (
                    document_id,
                    profile.authority_id,
                    profile.semantic_hash_sha256,
                    updated_at,
                ),
            )

    def clear_selection(self, document_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM cad_environment_selections WHERE document_id=?',
                (document_id,),
            )

    def selected_profile(
        self,
        document_id: str,
    ) -> AcousticEnvironmentProfile | None:
        """The document's selected profile; fails closed on tampered rows."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT authority_id, semantic_hash_sha256 FROM cad_environment_selections '
                'WHERE document_id=?',
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        profile = self.get_profile(str(row['authority_id']))
        if profile is None or profile.semantic_hash_sha256 != str(row['semantic_hash_sha256']):
            raise ValueError('selected environment profile does not match stored identity')
        return profile
