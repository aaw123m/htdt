"""Append-only persistence for REV59-DRAWPROF authorities.

Seven tables in one repository — CEB23-B video-design crosswalk
(#741), J-STD-710 drawing symbols (#742), timed-text presentation
(#733):

* ``cad_ht_video_design_profiles`` / ``cad_ceb23_evaluations``
* ``cad_drawing_symbol_profiles`` / ``cad_device_symbol_mappings``
  / ``cad_drawing_export_records``
* ``cad_timed_text_profiles`` / ``cad_caption_render_observations``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_ht_video_profile import (
    CEB23Evaluation,
    HomeTheaterVideoDesignProfile,
)
from .cad_drawing_symbols import (
    ArchitecturalDrawingSymbolProfile,
    DeviceSymbolMapping,
    DrawingExportRecord,
)
from .cad_timed_text import (
    CaptionRenderObservation,
    TimedTextPresentationProfile,
)


class PresentationProfileConflictError(ValueError):
    """A DRAWPROF save violated append-only identity rules."""


class PresentationProfileIntegrityError(ValueError):
    """A stored DRAWPROF row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise PresentationProfileIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise PresentationProfileIntegrityError(
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
            raise PresentationProfileConflictError(
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
            raise PresentationProfileIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise PresentationProfileIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise PresentationProfileIntegrityError(
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
                raise PresentationProfileIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadPresentationProfileRepository:
    """Native storage for the #741/#742/#733 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_ht_video_design_profiles',
                'cad_ceb23_evaluations',
                'cad_drawing_symbol_profiles',
                'cad_device_symbol_mappings',
                'cad_drawing_export_records',
                'cad_timed_text_profiles',
                'cad_caption_render_observations',
            )
        self.video_profiles = _SealedStore(
            self._connect, 'cad_ht_video_design_profiles',
            HomeTheaterVideoDesignProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('edition', 'edition'),
            ),
        )
        self.ceb23_evaluations = _SealedStore(
            self._connect, 'cad_ceb23_evaluations',
            CEB23Evaluation, 'evaluation_id', 'evaluation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
            ),
        )
        self.symbol_profiles = _SealedStore(
            self._connect, 'cad_drawing_symbol_profiles',
            ArchitecturalDrawingSymbolProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('edition', 'edition'),
                ('rights_provenance', 'rights_provenance'),
            ),
        )
        self.symbol_mappings = _SealedStore(
            self._connect, 'cad_device_symbol_mappings',
            DeviceSymbolMapping, 'mapping_id', 'mapping_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('device_kind', 'device_kind'),
            ),
        )
        self.drawing_exports = _SealedStore(
            self._connect, 'cad_drawing_export_records',
            DrawingExportRecord, 'export_id', 'export_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('export_format', 'export_format'),
            ),
        )
        self.text_profiles = _SealedStore(
            self._connect, 'cad_timed_text_profiles',
            TimedTextPresentationProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('profile_kind', 'profile_kind'),
            ),
        )
        self.caption_observations = _SealedStore(
            self._connect, 'cad_caption_render_observations',
            CaptionRenderObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_video_profile(
        self, record: HomeTheaterVideoDesignProfile
    ) -> None:
        self.video_profiles.save(record)

    def get_video_profile(
        self, rid: str
    ) -> HomeTheaterVideoDesignProfile | None:
        return self.video_profiles.get(rid)

    def save_ceb23_evaluation(
        self, record: CEB23Evaluation
    ) -> None:
        self.ceb23_evaluations.save(record)

    def get_ceb23_evaluation(
        self, rid: str
    ) -> CEB23Evaluation | None:
        return self.ceb23_evaluations.get(rid)

    def save_symbol_profile(
        self, record: ArchitecturalDrawingSymbolProfile
    ) -> None:
        self.symbol_profiles.save(record)

    def get_symbol_profile(
        self, rid: str
    ) -> ArchitecturalDrawingSymbolProfile | None:
        return self.symbol_profiles.get(rid)

    def save_symbol_mapping(
        self, record: DeviceSymbolMapping
    ) -> None:
        self.symbol_mappings.save(record)

    def get_symbol_mapping(
        self, rid: str
    ) -> DeviceSymbolMapping | None:
        return self.symbol_mappings.get(rid)

    def save_drawing_export(
        self, record: DrawingExportRecord
    ) -> None:
        self.drawing_exports.save(record)

    def get_drawing_export(
        self, rid: str
    ) -> DrawingExportRecord | None:
        return self.drawing_exports.get(rid)

    def save_text_profile(
        self, record: TimedTextPresentationProfile
    ) -> None:
        self.text_profiles.save(record)

    def get_text_profile(
        self, rid: str
    ) -> TimedTextPresentationProfile | None:
        return self.text_profiles.get(rid)

    def save_caption_observation(
        self, record: CaptionRenderObservation
    ) -> None:
        self.caption_observations.save(record)

    def get_caption_observation(
        self, rid: str
    ) -> CaptionRenderObservation | None:
        return self.caption_observations.get(rid)
