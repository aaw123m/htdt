from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository, SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .cad_system_variant import EntityLifecycleBinding, SystemVariant
from .cad_system_variant_repository import (
    CadSystemVariantRepository,
    SystemVariantApplication,
)


SYSTEM_VARIANT_AS_BUILT_SCHEMA_VERSION = 1
SYSTEM_VARIANT_AS_BUILT_AUTHORITY_VERSION = 'o100g-system-variant-as-built-1'


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


class SceneRevisionLineageRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    revision_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class SystemVariantAsBuiltRecord(BaseModel):
    """Explicit installation-completion evidence for one applied proposal.

    The original SystemVariant remains immutable/proposed. This record declares
    that all entities proposed by that variant are physically as-built in one
    exact applied-or-descendant SceneRevision.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = SYSTEM_VARIANT_AS_BUILT_SCHEMA_VERSION
    authority_version: Literal[
        'o100g-system-variant-as-built-1'
    ] = SYSTEM_VARIANT_AS_BUILT_AUTHORITY_VERSION

    record_id: str = Field(pattern=r'^system-variant-as-built:[0-9a-f]{64}$')
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    application_id: str = Field(min_length=1)
    application_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)

    applied_revision_id: str = Field(min_length=1)
    applied_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    as_built_revision_id: str = Field(min_length=1)
    as_built_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    revision_lineage: tuple[SceneRevisionLineageRef, ...] = Field(min_length=1)

    entity_lifecycle: tuple[EntityLifecycleBinding, ...] = Field(min_length=1)

    confirmation_kind: Literal[
        'manual_installation_completion'
    ] = 'manual_installation_completion'
    confirmed_by: str = Field(min_length=1)
    confirmed_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantAsBuiltRecord':
        if self.revision_lineage[0].revision_id != self.applied_revision_id:
            raise ValueError('as-built lineage must start at applied revision')
        if (
            self.revision_lineage[0].content_hash
            != self.applied_content_hash
        ):
            raise ValueError('as-built applied revision hash mismatch')
        if self.revision_lineage[-1].revision_id != self.as_built_revision_id:
            raise ValueError('as-built lineage must end at target revision')
        if (
            self.revision_lineage[-1].content_hash
            != self.as_built_content_hash
        ):
            raise ValueError('as-built target revision hash mismatch')
        ids = [item.revision_id for item in self.revision_lineage]
        if len(ids) != len(set(ids)):
            raise ValueError('as-built revision lineage must not repeat revisions')

        entity_ids = [item.entity_id for item in self.entity_lifecycle]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('as-built entity lifecycle ids must be unique')
        if any(item.state != 'as_built' for item in self.entity_lifecycle):
            raise ValueError(
                'SystemVariantAsBuiltRecord permits as_built lifecycle only'
            )
        if any(item.measurement_ids for item in self.entity_lifecycle):
            raise ValueError(
                'as-built lifecycle cannot carry measurement evidence'
            )
        if len(self.notes) != len(set(self.notes)):
            raise ValueError('as-built notes must be unique')

        expected = _digest(self.semantic_payload())
        if self.record_sha256 != expected:
            raise ValueError('SystemVariantAsBuiltRecord semantic hash mismatch')
        if self.record_id != f'system-variant-as-built:{expected}':
            raise ValueError('SystemVariantAsBuiltRecord id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'record_id', 'record_sha256'},
        )


def _revision_lineage(
    *,
    scene_repository: SceneRepository,
    application: SystemVariantApplication,
    target_revision: SceneRevision,
) -> tuple[SceneRevisionLineageRef, ...]:
    if target_revision.document_id != application.document_id:
        raise ValueError('as-built target belongs to another document')

    reverse: list[SceneRevision] = []
    seen: set[str] = set()
    current = target_revision
    while True:
        if current.revision_id in seen:
            raise ValueError('SceneRevision ancestry contains a cycle')
        seen.add(current.revision_id)
        reverse.append(current)
        if current.revision_id == application.applied_revision_id:
            if current.content_hash != application.applied_content_hash:
                raise ValueError(
                    'SystemVariantApplication applied revision hash mismatch'
                )
            break
        parent_id = current.parent_revision_id
        if parent_id is None:
            raise ValueError(
                'as-built target is not a descendant of applied SystemVariant'
            )
        parent = scene_repository.get(parent_id)
        if parent is None:
            raise ValueError(
                'as-built SceneRevision ancestry references missing parent'
            )
        if parent.document_id != application.document_id:
            raise ValueError('as-built SceneRevision ancestry crosses document')
        current = parent

    return tuple(
        SceneRevisionLineageRef(
            revision_id=item.revision_id,
            content_hash=item.content_hash,
        )
        for item in reversed(reverse)
    )


def _validate_proposed_entities_present(
    *,
    variant: SystemVariant,
    target_revision: SceneRevision,
) -> tuple[EntityLifecycleBinding, ...]:
    target_by_id = {
        item.entity_id: item
        for item in target_revision.document.entities
    }
    bindings: list[EntityLifecycleBinding] = []
    for proposed in variant.proposed_entities:
        entity_id = proposed.entity.entity_id
        target = target_by_id.get(entity_id)
        if target is None:
            raise ValueError(
                f'as-built target is missing proposed entity: {entity_id}'
            )
        if target.kind != proposed.entity.kind:
            raise ValueError(
                f'as-built proposed entity kind changed: {entity_id}'
            )
        if (
            target.kind == 'speaker'
            and target.speaker_role != proposed.entity.speaker_role
        ):
            raise ValueError(
                f'as-built proposed speaker role changed: {entity_id}'
            )
        bindings.append(
            EntityLifecycleBinding(
                entity_id=entity_id,
                state='as_built',
            )
        )
    if not bindings:
        raise ValueError('as-built promotion requires proposed physical entities')
    return tuple(bindings)


def build_system_variant_as_built_record(
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
    application: SystemVariantApplication,
    variant: SystemVariant,
    as_built_revision: SceneRevision,
    confirmed_by: str,
    confirmed_at_utc: str,
    notes: Sequence[str] = (),
) -> SystemVariantAsBuiltRecord:
    persisted_application = variant_repository.get_application(
        application.application_id
    )
    if persisted_application != application:
        raise ValueError(
            'as-built promotion requires exact persisted SystemVariantApplication'
        )
    persisted_variant = variant_repository.get_variant(variant.variant_id)
    if persisted_variant != variant:
        raise ValueError(
            'as-built promotion requires exact persisted SystemVariant'
        )
    if (
        application.variant_id != variant.variant_id
        or application.variant_sha256 != variant.variant_sha256
        or application.document_id != variant.document_id
    ):
        raise ValueError('as-built application/SystemVariant identity mismatch')
    if (
        as_built_revision.document_id != variant.document_id
    ):
        raise ValueError('as-built target/SystemVariant document mismatch')

    nearest = variant_repository.proposal_lineage_for_revision(
        as_built_revision.revision_id
    )
    if nearest != (application, variant):
        raise ValueError(
            'as-built target is not governed by the exact SystemVariantApplication'
        )

    lineage = _revision_lineage(
        scene_repository=scene_repository,
        application=application,
        target_revision=as_built_revision,
    )
    entity_lifecycle = _validate_proposed_entities_present(
        variant=variant,
        target_revision=as_built_revision,
    )
    note_tuple = tuple(notes)
    core = {
        'schema_version': SYSTEM_VARIANT_AS_BUILT_SCHEMA_VERSION,
        'authority_version': SYSTEM_VARIANT_AS_BUILT_AUTHORITY_VERSION,
        'application_id': application.application_id,
        'application_sha256': application.application_sha256,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'document_id': variant.document_id,
        'applied_revision_id': application.applied_revision_id,
        'applied_content_hash': application.applied_content_hash,
        'as_built_revision_id': as_built_revision.revision_id,
        'as_built_content_hash': as_built_revision.content_hash,
        'revision_lineage': [
            item.model_dump(mode='json') for item in lineage
        ],
        'entity_lifecycle': [
            item.model_dump(mode='json') for item in entity_lifecycle
        ],
        'confirmation_kind': 'manual_installation_completion',
        'confirmed_by': confirmed_by,
        'confirmed_at_utc': confirmed_at_utc,
        'notes': list(note_tuple),
    }
    digest = _digest(core)
    return SystemVariantAsBuiltRecord(
        record_id=f'system-variant-as-built:{digest}',
        record_sha256=digest,
        application_id=application.application_id,
        application_sha256=application.application_sha256,
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        document_id=variant.document_id,
        applied_revision_id=application.applied_revision_id,
        applied_content_hash=application.applied_content_hash,
        as_built_revision_id=as_built_revision.revision_id,
        as_built_content_hash=as_built_revision.content_hash,
        revision_lineage=lineage,
        entity_lifecycle=entity_lifecycle,
        confirmed_by=confirmed_by,
        confirmed_at_utc=confirmed_at_utc,
        notes=note_tuple,
    )


class CadSystemVariantLifecycleRepository:
    """Append-only O100G physical lifecycle evidence."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.path = Path(scene_repository.path)
        if Path(variant_repository.path) != self.path:
            raise ValueError(
                'SystemVariant lifecycle and variant repositories must share '
                'one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_system_variant_as_built')

    def _validate(
        self,
        record: SystemVariantAsBuiltRecord,
    ) -> SystemVariantAsBuiltRecord:
        record = SystemVariantAsBuiltRecord.model_validate(
            record.model_dump(mode='python')
        )
        application = self.variant_repository.get_application(
            record.application_id
        )
        if application is None:
            raise ValueError(
                'as-built record references missing SystemVariantApplication'
            )
        if application.application_sha256 != record.application_sha256:
            raise ValueError('as-built application hash mismatch')
        variant = self.variant_repository.get_variant(record.variant_id)
        if variant is None:
            raise ValueError('as-built record references missing SystemVariant')
        if variant.variant_sha256 != record.variant_sha256:
            raise ValueError('as-built SystemVariant hash mismatch')
        target = self.scene_repository.get(record.as_built_revision_id)
        if target is None:
            raise ValueError('as-built target SceneRevision disappeared')
        if target.content_hash != record.as_built_content_hash:
            raise ValueError('as-built target SceneRevision hash mismatch')

        rebuilt = build_system_variant_as_built_record(
            scene_repository=self.scene_repository,
            variant_repository=self.variant_repository,
            application=application,
            variant=variant,
            as_built_revision=target,
            confirmed_by=record.confirmed_by,
            confirmed_at_utc=record.confirmed_at_utc,
            notes=record.notes,
        )
        if rebuilt != record:
            raise ValueError(
                'as-built record does not reproduce from exact authorities'
            )
        return record

    def save(
        self,
        record: SystemVariantAsBuiltRecord,
    ) -> SystemVariantAsBuiltRecord:
        record = self._validate(record)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_system_variant_as_built
                WHERE application_id=?
                """,
                (record.application_id,),
            ).fetchone()
            if existing is not None:
                persisted = SystemVariantAsBuiltRecord.model_validate_json(
                    existing['payload_json']
                )
                if persisted != record:
                    raise ValueError(
                        'SystemVariantApplication already has different '
                        'as-built completion evidence'
                    )
                return self._validate(persisted)
            connection.execute(
                """
                INSERT INTO cad_system_variant_as_built(
                    record_id,
                    record_sha256,
                    application_id,
                    variant_id,
                    as_built_revision_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.application_id,
                    record.variant_id,
                    record.as_built_revision_id,
                    record.model_dump_json(),
                    record.confirmed_at_utc,
                ),
            )
        return record

    def get(
        self,
        record_id: str,
    ) -> SystemVariantAsBuiltRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_system_variant_as_built
                WHERE record_id=?
                """,
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            SystemVariantAsBuiltRecord.model_validate_json(
                row['payload_json']
            )
        )

    def for_application(
        self,
        application_id: str,
    ) -> SystemVariantAsBuiltRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_system_variant_as_built
                WHERE application_id=?
                """,
                (application_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            SystemVariantAsBuiltRecord.model_validate_json(
                row['payload_json']
            )
        )
