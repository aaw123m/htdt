"""Append-only persistence for REV59-AUDIO2 authorities.

Six tables — panning continuity (#652), subwoofer localization
(#669), group-delay audibility (#657), headphone coupling (#702),
structure-borne path (#653), spatial remapping (#664).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_panning_continuity import (
    PanningContinuityEvidence,
    SubwooferLocalizationProfile,
)
from .cad_perceptual_chain import (
    GroupDelayAudibility,
    HeadphoneCouplingEvidence,
)
from .cad_structureborne_remap import (
    SpatialRemappingEvidence,
    StructurebornePath,
)


class AudioAuthorityConflictError(ValueError):
    """An AUDIO2 save violated append-only identity rules."""


class AudioAuthorityIntegrityError(ValueError):
    """A stored AUDIO2 row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise AudioAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise AudioAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise AudioAuthorityConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise AudioAuthorityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise AudioAuthorityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise AudioAuthorityIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise AudioAuthorityIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadAudioPerceptionRepository:
    """Native storage for the #652/#669/#657/#702/#653/#664
    authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_panning_continuity_evidence',
                'cad_subwoofer_localization_profiles',
                'cad_groupdelay_audibility',
                'cad_headphone_coupling_evidence',
                'cad_structureborne_paths',
                'cad_spatial_remapping_evidence',
            )

        self.continuity_evidence = _SealedStore(
            self._connect, 'cad_panning_continuity_evidence',
            PanningContinuityEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('stimulus_kind', 'stimulus_kind'),
            ),
        )

        self.localization_profiles = _SealedStore(
            self._connect, 'cad_subwoofer_localization_profiles',
            SubwooferLocalizationProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('stimulus_kind', 'stimulus_kind'),
            ),
        )

        self.groupdelay_verdicts = _SealedStore(
            self._connect, 'cad_groupdelay_audibility',
            GroupDelayAudibility, 'verdict_id', 'verdict_sha256',
            (
                ('document_id', '__document_id__'),
                ('stimulus_kind', 'stimulus_kind'),
                ('peak_delay_ms', 'peak_delay_ms'),
                ('frequency_hz', 'frequency_hz'),
            ),
        )

        self.coupling_evidence = _SealedStore(
            self._connect, 'cad_headphone_coupling_evidence',
            HeadphoneCouplingEvidence, 'coupling_id', 'coupling_sha256',
            (
                ('document_id', '__document_id__'),
                ('compensation_kind', 'compensation_kind'),
                ('fit_state', 'fit_state'),
            ),
        )

        self.structureborne_paths = _SealedStore(
            self._connect, 'cad_structureborne_paths',
            StructurebornePath, 'path_id', 'path_sha256',
            (
                ('document_id', '__document_id__'),
                ('source_kind', 'source_kind'),
                ('mount_kind', 'mount_kind'),
            ),
        )

        self.remap_evidence = _SealedStore(
            self._connect, 'cad_spatial_remapping_evidence',
            SpatialRemappingEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('remap_mode', 'remap_mode'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_continuity_evidence(self, record: PanningContinuityEvidence) -> None:
        self.continuity_evidence.save(record)

    def get_continuity_evidence(self, rid: str) -> PanningContinuityEvidence | None:
        return self.continuity_evidence.get(rid)

    def save_localization_profile(self, record: SubwooferLocalizationProfile) -> None:
        self.localization_profiles.save(record)

    def get_localization_profile(self, rid: str) -> SubwooferLocalizationProfile | None:
        return self.localization_profiles.get(rid)

    def save_groupdelay_verdict(self, record: GroupDelayAudibility) -> None:
        self.groupdelay_verdicts.save(record)

    def get_groupdelay_verdict(self, rid: str) -> GroupDelayAudibility | None:
        return self.groupdelay_verdicts.get(rid)

    def save_coupling_evidence(self, record: HeadphoneCouplingEvidence) -> None:
        self.coupling_evidence.save(record)

    def get_coupling_evidence(self, rid: str) -> HeadphoneCouplingEvidence | None:
        return self.coupling_evidence.get(rid)

    def save_structureborne_path(self, record: StructurebornePath) -> None:
        self.structureborne_paths.save(record)

    def get_structureborne_path(self, rid: str) -> StructurebornePath | None:
        return self.structureborne_paths.get(rid)

    def save_remap_evidence(self, record: SpatialRemappingEvidence) -> None:
        self.remap_evidence.save(record)

    def get_remap_evidence(self, rid: str) -> SpatialRemappingEvidence | None:
        return self.remap_evidence.get(rid)
