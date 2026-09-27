"""Persistence for O100C cost records and variant cost evaluations (#514)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .cad_equipment_instance import InstalledEquipmentInstance
from .cad_equipment_instance_repository import (
    CadInstalledEquipmentRepository,
)
from .cad_installation_cost import (
    CostRecord,
    VariantCostEvaluation,
    evaluate_variant_installation_cost,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables, connect_sqlite
from .cad_system_variant_repository import CadSystemVariantRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadInstallationCostRepository:
    """Append-only store with replay validation on every read and write.

    A persisted evaluation is only authoritative when it reproduces exactly
    from the pinned authorities it declares: the SceneRevision it priced, the
    SystemVariant (itself replayed by its own repository), and the exact cost
    records listed under ``cost_record_sha256s``. Caller-authored totals,
    budget state, or record bindings that do not replay are rejected on save
    and on every authoritative read (#994).
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()
        # Shares this database; its get_variant() replays variant authority.
        self.variant_repository = CadSystemVariantRepository(scene_repository)

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_cost_records',
                'cad_cost_evaluations',
            )

    def _validate_record_equipment_binding(
        self,
        connection: sqlite3.Connection,
        record: CostRecord,
    ) -> None:
        """A bound record must pin a persisted exact EquipmentDefinition.

        Persisted definition rows are the authority the binding's
        (definition_id, version, semantic_sha256) triple must match exactly;
        unbound records and records bound to unknown or mismatched
        definitions fail closed (#994).
        """
        if record.equipment_definition_sha256 is None:
            return
        row = connection.execute(
            """
            SELECT definition_id, version
            FROM cad_equipment_definitions
            WHERE semantic_sha256=?
            """,
            (record.equipment_definition_sha256,),
        ).fetchone()
        if row is None:
            raise ValueError(
                'cost record references an unpersisted equipment definition'
            )
        if (
            row['definition_id'] != record.equipment_definition_id
            or row['version'] != record.equipment_definition_version
        ):
            raise ValueError(
                'cost record equipment definition identity mismatch'
            )

    def _validated_record(
        self,
        row: sqlite3.Row,
        connection: sqlite3.Connection,
    ) -> CostRecord:
        """Deserialize one persisted record row and verify its identity."""
        record = CostRecord.model_validate_json(row['payload_json'])
        if (
            row['record_id'] != record.record_id
            or row['record_sha256'] != record.record_sha256
            or row['category'] != record.category
        ):
            raise ValueError(
                'persisted cost record row disagrees with its payload'
            )
        self._validate_record_equipment_binding(connection, record)
        return record

    def save_record(
        self,
        record: CostRecord,
        *,
        document_id: str,
    ) -> CostRecord:
        record = CostRecord.model_validate(record.model_dump(mode='python'))
        with closing(self._connect()) as connection, connection:
            self._validate_record_equipment_binding(connection, record)
            existing = connection.execute(
                'SELECT * FROM cad_cost_records WHERE record_id=?',
                (record.record_id,),
            ).fetchone()
            if existing is not None:
                persisted = self._validated_record(existing, connection)
                if persisted != record:
                    raise ValueError('cost record id has different semantics')
                if existing['document_id'] != document_id:
                    raise ValueError(
                        'cost record id persisted under a different document'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_cost_records(
                    record_id, record_sha256, document_id, category,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    document_id,
                    record.category,
                    record.model_dump_json(),
                    _utc_now(),
                ),
            )
        return record

    def get_record(self, record_id: str) -> CostRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_cost_records WHERE record_id=?',
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            return self._validated_record(row, connection)

    def list_records(self, document_id: str) -> tuple[CostRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_cost_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
            return tuple(
                self._validated_record(row, connection) for row in rows
            )

    def _resolve_and_replay(
        self,
        connection: sqlite3.Connection,
        evaluation: VariantCostEvaluation,
    ) -> VariantCostEvaluation:
        """Resolve pinned authorities and replay the canonical evaluator.

        The declared ``cost_record_sha256s`` set is the canonical input: every
        pinned record must exist in this document, bind to a persisted exact
        EquipmentDefinition when it declares one, and — together with the
        resolved SceneRevision, SystemVariant and embedded scenario —
        reproduce the persisted evaluation bit-for-bit (#994).
        """
        revision = self.scene_repository.get(evaluation.scene_revision_id)
        if revision is None:
            raise ValueError(
                'cost evaluation SceneRevision does not exist'
            )
        if (
            revision.document_id != evaluation.document_id
            or revision.content_hash != evaluation.scene_content_hash
        ):
            raise ValueError('cost evaluation SceneRevision authority mismatch')

        variant = self.variant_repository.get_variant(evaluation.variant_id)
        if variant is None:
            raise ValueError('cost evaluation SystemVariant does not exist')
        if (
            variant.document_id != evaluation.document_id
            or variant.variant_sha256 != evaluation.variant_sha256
            or variant.baseline_revision_id != revision.revision_id
            or variant.baseline_content_hash != revision.content_hash
        ):
            raise ValueError('cost evaluation SystemVariant authority mismatch')
        if evaluation.scenario.document_id != evaluation.document_id:
            raise ValueError('cost evaluation scenario document mismatch')

        records: list[CostRecord] = []
        for record_sha256 in evaluation.cost_record_sha256s:
            row = connection.execute(
                """
                SELECT * FROM cad_cost_records
                WHERE record_sha256=? AND document_id=?
                """,
                (record_sha256, evaluation.document_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    'cost evaluation references an unresolved cost record'
                )
            records.append(self._validated_record(row, connection))

        # Resolve the pinned baseline installed-equipment inventory the
        # evaluation consumed (#1058). ``None`` marks a legacy evaluation
        # persisted before baseline resolution existed; replay feeds the
        # unresolved scope so the persisted line set reproduces exactly.
        baseline_equipment: tuple[InstalledEquipmentInstance, ...] | None = (
            None
        )
        if evaluation.baseline_equipment_sha256s is not None:
            instance_repository = CadInstalledEquipmentRepository(
                self.scene_repository
            )
            available = {
                instance.semantic_sha256: instance
                for instance in instance_repository.list_instances(
                    evaluation.document_id,
                    include_removed=True,
                )
            }
            resolved = []
            for instance_sha256 in evaluation.baseline_equipment_sha256s:
                instance = available.get(instance_sha256)
                if instance is None:
                    raise ValueError(
                        'cost evaluation references an unresolved baseline '
                        'equipment instance'
                    )
                resolved.append(instance)
            baseline_equipment = tuple(resolved)

        replayed = evaluate_variant_installation_cost(
            revision=revision,
            variant=variant,
            scenario=evaluation.scenario,
            cost_records=tuple(records),
            baseline_equipment=baseline_equipment,
        )
        if replayed != evaluation:
            raise ValueError(
                'persisted cost evaluation does not reproduce from its '
                'canonical inputs'
            )
        return evaluation

    def _validated_evaluation(
        self,
        row: sqlite3.Row,
    ) -> VariantCostEvaluation:
        """Deserialize one persisted evaluation row and replay its inputs."""
        evaluation = VariantCostEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            row['evaluation_id'] != evaluation.evaluation_id
            or row['evaluation_sha256'] != evaluation.evaluation_sha256
            or row['document_id'] != evaluation.document_id
            or row['variant_id'] != evaluation.variant_id
        ):
            raise ValueError(
                'persisted cost evaluation row disagrees with its payload'
            )
        with closing(self._connect()) as connection:
            self._resolve_and_replay(connection, evaluation)
        return evaluation

    def save_evaluation(
        self,
        evaluation: VariantCostEvaluation,
    ) -> VariantCostEvaluation:
        evaluation = VariantCostEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        # Replay before opening the write transaction: resolution reads on
        # sibling connections and must not run under BEGIN IMMEDIATE.
        with closing(self._connect()) as connection:
            self._resolve_and_replay(connection, evaluation)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT * FROM cad_cost_evaluations '
                'WHERE evaluation_id=?',
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = self._validated_evaluation(existing)
                if persisted != evaluation:
                    raise ValueError(
                        'cost evaluation id has different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_cost_evaluations(
                    evaluation_id, evaluation_sha256, document_id, variant_id,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.variant_id,
                    evaluation.model_dump_json(),
                    _utc_now(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> VariantCostEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_cost_evaluations '
                'WHERE evaluation_id=?',
                (evaluation_id,),
            ).fetchone()
        return None if row is None else self._validated_evaluation(row)

    def list_evaluations(
        self,
        document_id: str,
    ) -> tuple[VariantCostEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_cost_evaluations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._validated_evaluation(row) for row in rows)
