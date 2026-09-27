from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRepository, SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .cad_system_variant import (
    EntityLifecycleBinding,
    SystemVariant,
    VariantDiffKind,
)
from .cad_system_variant_repository import (
    CadSystemVariantRepository,
    SystemVariantApplication,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


SYSTEM_VARIANT_AS_BUILT_SCHEMA_VERSION = 1
SYSTEM_VARIANT_AS_BUILT_AUTHORITY_VERSION = 'o100g-system-variant-as-built-1'






class SceneRevisionLineageRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    revision_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


ConformanceState = Literal[
    'exact',
    'deviated',
    'not_installed',
    'unexpected_present',
]


def _entity_sha256(entity) -> str:
    return _digest(entity.model_dump(mode='json'))


class EntityAsBuiltConformance(BaseModel):
    """Per-diff conformance of the exact applied proposal against the
    selected as-built SceneRevision (#958).

    ``exact`` — the diff operation is realized literally: add/replace
    entities are present and content-equal; remove entities are absent.
    ``deviated`` — the entity is installed but its content differs from
    the proposal; the deviation is explicit (``deviation_note``).
    ``not_installed`` and ``unexpected_present`` are derivable evaluation
    states that may never persist inside a completion record: a missing
    proposed entity or a reintroduced removed entity cannot be declared
    as-built, so the record model rejects them outright.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    entity_id: str = Field(min_length=1)
    diff_kind: VariantDiffKind
    state: ConformanceState
    proposed_entity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    actual_entity_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    deviation_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_conformance(self) -> 'EntityAsBuiltConformance':
        if self.state in {'not_installed', 'unexpected_present'}:
            raise ValueError(
                'as-built completion cannot persist a non-conformant '
                'entity state'
            )
        if self.state == 'deviated':
            if self.deviation_note is None or self.actual_entity_sha256 is None:
                raise ValueError(
                    'deviated conformance requires the actual entity and an '
                    'explicit deviation note'
                )
            if self.actual_entity_sha256 == self.proposed_entity_sha256:
                raise ValueError(
                    'deviated conformance requires actual content differing '
                    'from proposed'
                )
        else:
            if self.deviation_note is not None:
                raise ValueError(
                    'only deviated conformance may carry a deviation note'
                )
            if self.diff_kind == 'remove':
                if self.actual_entity_sha256 is not None:
                    raise ValueError(
                        'exact remove conformance cannot carry an actual entity'
                    )
            elif self.actual_entity_sha256 != self.proposed_entity_sha256:
                raise ValueError(
                    'exact conformance requires the actual entity to equal '
                    'the proposed entity'
                )
        return self


class SystemVariantAsBuiltRecord(BaseModel):
    """Explicit installation-completion evidence for one applied proposal.

    The original SystemVariant remains immutable/proposed. This record
    declares that the variant's full add/replace/remove diff is realized
    in one exact applied-or-descendant SceneRevision, with any legitimate
    field deviations recorded explicitly per entity rather than collapsed
    into a generic as_built label (#958).
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

    entity_conformance: tuple[EntityAsBuiltConformance, ...] = Field(
        min_length=1
    )

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

        entity_ids = [item.entity_id for item in self.entity_conformance]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('as-built entity conformance ids must be unique')
        if len(self.notes) != len(set(self.notes)):
            raise ValueError('as-built notes must be unique')

        expected = _digest(self.semantic_payload())
        if self.record_sha256 != expected:
            raise ValueError('SystemVariantAsBuiltRecord semantic hash mismatch')
        if self.record_id != f'system-variant-as-built:{expected}':
            raise ValueError('SystemVariantAsBuiltRecord id mismatch')
        return self

    @property
    def entity_lifecycle(self) -> tuple[EntityLifecycleBinding, ...]:
        """Derived lifecycle view: entities installed as-built.

        Add/replace conformance entries (exact or explicitly deviated)
        materialize as ``as_built`` bindings; removed entities carry no
        lifecycle binding. Measurement evidence is never attached here —
        measured lifecycle is a separate record (#958).
        """
        return tuple(
            EntityLifecycleBinding(
                entity_id=item.entity_id,
                state='as_built',
            )
            for item in self.entity_conformance
            if item.diff_kind != 'remove'
        )

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


def _validate_variant_diff_conformance(
    *,
    variant: SystemVariant,
    target_revision: SceneRevision,
    accepted_deviations: Mapping[str, str],
) -> tuple[EntityAsBuiltConformance, ...]:
    """Evaluate every add/replace/remove diff against the as-built scene.

    Fail-closed semantics: a proposed entity missing from the target or a
    proposal-removed entity still present is not an honest completion and
    raises; content deviations are only admitted via an explicit
    ``accepted_deviations`` note (#958).
    """
    if not variant.diff:
        raise ValueError(
            'as-built promotion requires at least one variant diff operation'
        )
    target_by_id = {
        item.entity_id: item
        for item in target_revision.document.entities
    }
    for binding in variant.equipment_bindings:
        if binding.entity_id not in target_by_id:
            raise ValueError(
                'as-built target is missing equipment-bound entity: '
                f'{binding.entity_id}'
            )
    conformance: list[EntityAsBuiltConformance] = []
    for change in variant.diff:
        entity_id = change.entity_id
        target = target_by_id.get(entity_id)
        actual_sha = (
            _entity_sha256(target) if target is not None else None
        )
        if change.kind == 'remove':
            assert change.before_entity is not None
            if target is not None:
                raise ValueError(
                    'as-built target reintroduces proposal-removed entity: '
                    f'{entity_id}'
                )
            conformance.append(
                EntityAsBuiltConformance(
                    entity_id=entity_id,
                    diff_kind='remove',
                    state='exact',
                    proposed_entity_sha256=_entity_sha256(
                        change.before_entity
                    ),
                )
            )
            continue
        assert change.after_entity is not None
        proposed_sha = _entity_sha256(change.after_entity)
        if target is None:
            raise ValueError(
                f'as-built target is missing proposed entity: {entity_id}'
            )
        if actual_sha == proposed_sha:
            conformance.append(
                EntityAsBuiltConformance(
                    entity_id=entity_id,
                    diff_kind=change.kind,
                    state='exact',
                    proposed_entity_sha256=proposed_sha,
                    actual_entity_sha256=actual_sha,
                )
            )
            continue
        note = accepted_deviations.get(entity_id)
        if not note:
            raise ValueError(
                f'as-built target deviates from proposed entity without an '
                f'accepted-deviation note: {entity_id}'
            )
        conformance.append(
            EntityAsBuiltConformance(
                entity_id=entity_id,
                diff_kind=change.kind,
                state='deviated',
                proposed_entity_sha256=proposed_sha,
                actual_entity_sha256=actual_sha,
                deviation_note=note,
            )
        )
    return tuple(conformance)


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
    accepted_deviations: Mapping[str, str] | None = None,
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
    entity_conformance = _validate_variant_diff_conformance(
        variant=variant,
        target_revision=as_built_revision,
        accepted_deviations=accepted_deviations or {},
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
        'entity_conformance': [
            item.model_dump(mode='json') for item in entity_conformance
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
        entity_conformance=entity_conformance,
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
        return connect_sqlite(self.path)

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
            accepted_deviations={
                item.entity_id: item.deviation_note
                for item in record.entity_conformance
                if item.deviation_note is not None
            },
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
