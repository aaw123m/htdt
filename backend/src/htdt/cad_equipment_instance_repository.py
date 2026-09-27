"""Persistence for installed-equipment instance authority (#569).

Append-only tables living in the same project database as the rest of the
CAD authority stack. Every row stores the complete validated payload and is
re-validated on read, so corruption fails closed rather than surfacing a
doctored record.

#854 hardening: a ``definition_ref`` on an instance or binding only means
"catalog-resolved" when the exact hash resolves to a persisted
``EquipmentDefinition`` whose id/version match the declared tuple — an
unresolved tuple is never persisted. Scene-entity bindings must satisfy a
bounded equipment-class → entity-kind compatibility map; rack/inventory-only
units simply carry ``scene_entity_id=None``. #842 hardening:
``replace_instance`` validates the successor and appends the replacement
lineage inside one ``BEGIN IMMEDIATE`` transaction, and the replacements
table enforces a single successor per predecessor plus a single role per
successor through UNIQUE indexes.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Literal, Sequence

from .cad_equipment import EquipmentDefinition
from .cad_equipment_instance import (
    ExternalEquipmentIdentity,
    InstanceDefinitionResolution,
    InstalledDefinitionBinding,
    InstalledDefinitionRef,
    InstalledDeviceObservation,
    InstalledEquipmentInstance,
    InstalledEquipmentReplacement,
    build_installed_equipment_replacement,
)
from .cad_equipment import EquipmentDataProvenance
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


InstalledEffectiveState = Literal['current', 'replaced', 'removed']

# Bounded semantic compatibility between an equipment class and the scene
# entity kinds it may physically represent (#854). Rack/inventory-only units
# keep ``scene_entity_id=None``; a bound entity must be one of the listed
# kinds — physical role is never inferred from name strings.
_EQUIPMENT_CLASS_SCENE_KINDS: dict[str, tuple[str, ...]] = {
    'speaker': ('speaker',),
    'subwoofer': ('speaker',),
    'projector': ('projector',),
    'display': ('display', 'screen'),
    'avr': ('av_equipment',),
    'processor': ('av_equipment',),
    'amplifier': ('av_equipment',),
    'dsp': ('av_equipment',),
    'source_device': ('av_equipment',),
    'other': ('av_equipment', 'furniture'),
}


class CadInstalledEquipmentRepository:
    """Append-only persistence for installed-unit authority.

    ``equipment_repository`` (a ``CadEquipmentRepository`` bound to the same
    database) is required whenever a catalog-resolution claim is written:
    an ``InstalledDefinitionRef`` — on an instance or a binding — is only
    persisted after exact resolution (id + version + semantic hash). Reads
    re-check indexed columns against payloads and never upgrade external
    identity evidence into a resolution claim (#819).
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        equipment_repository=None,
    ) -> None:
        self.scene_repository = scene_repository
        self.equipment_repository = equipment_repository

        self.path = Path(scene_repository.path)
        if (
            self.equipment_repository is not None
            and Path(self.equipment_repository.path) != self.path
        ):
            raise ValueError(
                'scene and equipment repositories must share one native '
                'CAD database'
            )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_installed_equipment_instances', 'cad_installed_definition_bindings', 'cad_installed_device_observations', 'cad_installed_equipment_replacements')

    # ------------------------------------------------------------------
    # Shared validation helpers

    def _check_entity_binding(
        self, instance: InstalledEquipmentInstance
    ) -> None:
        """A scene binding names a compatible entity in the current head.

        ``scene_entity_id=None`` is always valid — rack/inventory-only
        equipment has no scene geometry. When bound, the entity must exist
        in the document's head revision and its kind must be compatible
        with the instance's equipment class (#854).
        """
        if instance.scene_entity_id is None:
            return
        head = self.scene_repository.current_head(instance.document_id)
        if head is None:
            raise ValueError(
                f'document {instance.document_id} has no scene revision to '
                'bind an installed instance against'
            )
        entity = next(
            (
                item
                for item in head.document.entities
                if item.entity_id == instance.scene_entity_id
            ),
            None,
        )
        if entity is None:
            raise ValueError(
                f'scene entity {instance.scene_entity_id} is not present in '
                f'the current scene revision of {instance.document_id}'
            )
        allowed = _EQUIPMENT_CLASS_SCENE_KINDS[instance.equipment_class]
        if entity.kind not in allowed:
            raise ValueError(
                f'{instance.equipment_class} instance cannot bind scene '
                f'entity {instance.scene_entity_id} of kind {entity.kind}'
            )

    def _validate_instance(
        self, instance: InstalledEquipmentInstance
    ) -> InstalledEquipmentInstance:
        """Model re-validation + exact-ref and scene-binding resolution."""
        instance = InstalledEquipmentInstance.model_validate(
            instance.model_dump(mode='python')
        )
        if instance.definition_ref is not None:
            self._resolve_ref_exact(instance.definition_ref)
        self._check_entity_binding(instance)
        return instance

    # ------------------------------------------------------------------
    # Instances

    @staticmethod
    def _instance_row(
        connection: sqlite3.Connection, instance_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            'SELECT instance_id, document_id, equipment_class, state, '
            'payload_json FROM cad_installed_equipment_instances '
            'WHERE instance_id=?',
            (instance_id,),
        ).fetchone()

    @staticmethod
    def _insert_instance_row(
        connection: sqlite3.Connection,
        instance: InstalledEquipmentInstance,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cad_installed_equipment_instances(
                instance_id, document_id, equipment_class, state,
                payload_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instance.instance_id,
                instance.document_id,
                instance.equipment_class,
                instance.state,
                instance.model_dump_json(),
                instance.created_at_utc,
            ),
        )


    def _resolve_ref_exact(
        self,
        ref,
        *what: str,
    ) -> EquipmentDefinition:
        """Resolve a resolution claim to an exact persisted definition.

        The catalog is keyed by semantic hash; id and version must then
        match the claim exactly — anything else fails closed.
        """
        if self.equipment_repository is None:
            raise ValueError(
                'catalog-resolution claims require a bound '
                'CadEquipmentRepository'
            )
        definition = self.equipment_repository.get_definition_by_hash(
            ref.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'installed equipment references an unpersisted '
                'EquipmentDefinition'
            )
        if (
            definition.definition_id != ref.equipment_definition_id
            or definition.version != ref.equipment_definition_version
        ):
            raise ValueError(
                'installed equipment definition ref resolves to a '
                'different EquipmentDefinition identity than declared '
                '(id/version diverge from the claimed hash)'
            )
        return definition

    def save_instance(
        self,
        instance: InstalledEquipmentInstance,
    ) -> InstalledEquipmentInstance:
        """Persist one immutable installed-unit record.

        The catalog ref (when present) resolves to an exact persisted
        ``EquipmentDefinition`` — hash plus declared id/version — and a
        scene-entity binding must be kind-compatible. An already-persisted
        instance_id is immutable — a divergent resave fails closed.
        """
        instance = self._validate_instance(instance)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = self._instance_row(connection, instance.instance_id)
            if existing is not None:
                persisted = InstalledEquipmentInstance.model_validate_json(
                    existing['payload_json']
                )
                if persisted.semantic_sha256 != instance.semantic_sha256:
                    raise ValueError(
                        f'installed equipment instance {instance.instance_id} '
                        'already exists with different content'
                    )
                return persisted
            self._insert_instance_row(connection, instance)
        return instance

    @staticmethod
    def _instance_from_row(row) -> InstalledEquipmentInstance:
        """Deserialize a row, cross-checking its indexed columns against the
        payload — a disagreement fails closed (#819)."""
        instance = InstalledEquipmentInstance.model_validate_json(
            row['payload_json']
        )
        if (
            row['instance_id'] != instance.instance_id
            or row['document_id'] != instance.document_id
            or row['equipment_class'] != instance.equipment_class
            or row['state'] != instance.state
        ):
            raise ValueError(
                f'persisted installed instance row disagrees with its '
                f'payload (row/payload mismatch for {row["instance_id"]})'
            )
        return instance

    def get_instance(
        self,
        instance_id: str,
    ) -> InstalledEquipmentInstance | None:
        with closing(self._connect()) as connection:
            row = self._instance_row(connection, instance_id)
        if row is None:
            return None
        return self._instance_from_row(row)

    def list_instances(
        self,
        document_id: str,
        *,
        include_removed: bool = False,
    ) -> tuple[InstalledEquipmentInstance, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT instance_id, document_id, equipment_class, state, '
                'payload_json FROM cad_installed_equipment_instances '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        instances = tuple(
            self._instance_from_row(row) for row in rows
        )
        if include_removed:
            return instances
        replaced = {
            replacement.removed_instance_id
            for replacement in self.list_replacements(document_id)
        }
        return tuple(
            instance
            for instance in instances
            if instance.state == 'current'
            and instance.instance_id not in replaced
        )

    def effective_state(
        self,
        instance_id: str,
    ) -> InstalledEffectiveState | None:
        instance = self.get_instance(instance_id)
        if instance is None:
            return None
        if instance.state == 'removed':
            return 'removed'
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT 1 FROM cad_installed_equipment_replacements '
                'WHERE removed_instance_id=? LIMIT 1',
                (instance_id,),
            ).fetchone()
        return 'replaced' if row is not None else 'current'

    # ------------------------------------------------------------------
    # Catalog-resolution bindings


    def save_binding(
        self,
        binding: InstalledDefinitionBinding,
    ) -> InstalledDefinitionBinding:
        """Append one catalog-resolution record; history is never rewritten."""
        binding = InstalledDefinitionBinding.model_validate(
            binding.model_dump(mode='python')
        )
        # A binding claims exact local catalog resolution — it must resolve
        # before the row is admitted.
        self._resolve_ref_exact(binding.definition_ref)
        if self.get_instance(binding.instance_id) is None:
            raise ValueError(
                f'unknown installed equipment instance: {binding.instance_id}'
            )
        self._resolve_ref_exact(binding.definition_ref)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT payload_json FROM cad_installed_definition_bindings '
                'WHERE binding_id=?',
                (binding.binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = InstalledDefinitionBinding.model_validate_json(
                    existing['payload_json']
                )
                if persisted.semantic_sha256 != binding.semantic_sha256:
                    raise ValueError(
                        f'installed definition binding {binding.binding_id} '
                        'already exists with different content'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_installed_definition_bindings(
                    binding_id, instance_id, equipment_definition_sha256,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.instance_id,
                    binding.definition_ref.equipment_definition_sha256,
                    binding.model_dump_json(),
                    binding.bound_at_utc,
                ),
            )
        return binding

    @staticmethod
    def _binding_from_row(row) -> InstalledDefinitionBinding:
        binding = InstalledDefinitionBinding.model_validate_json(
            row['payload_json']
        )
        if (
            row['binding_id'] != binding.binding_id
            or row['instance_id'] != binding.instance_id
            or row['equipment_definition_sha256']
            != binding.definition_ref.equipment_definition_sha256
        ):
            raise ValueError(
                f'persisted installed definition binding row disagrees with its '
                f'payload (row/payload mismatch for {row["binding_id"]})'
            )
        return binding

    def list_bindings(
        self,
        instance_id: str,
    ) -> tuple[InstalledDefinitionBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT binding_id, instance_id, '
                'equipment_definition_sha256, payload_json '
                'FROM cad_installed_definition_bindings '
                'WHERE instance_id=? ORDER BY seq ASC',
                (instance_id,),
            ).fetchall()
        return tuple(
            self._binding_from_row(row) for row in rows
        )

    def current_binding(
        self,
        instance_id: str,
    ) -> InstalledDefinitionBinding | None:
        """The most recent catalog resolution for the instance, if any."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT binding_id, instance_id, '
                'equipment_definition_sha256, payload_json '
                'FROM cad_installed_definition_bindings '
                'WHERE instance_id=? ORDER BY seq DESC LIMIT 1',
                (instance_id,),
            ).fetchone()
        if row is None:
            return None
        return self._binding_from_row(row)

    # ------------------------------------------------------------------
    # Read-side resolution (#819)

    def resolve_instance_definition(
        self,
        instance_id: str,
    ) -> InstanceDefinitionResolution | None:
        """Typed resolution state for a persisted instance.

        The in-effect claim is the latest binding's ref when one exists,
        else the instance's own ``definition_ref``; external identity
        evidence is never promoted to a resolution.
        """
        if self.equipment_repository is None:
            raise ValueError(
                'definition resolution requires a bound '
                'CadEquipmentRepository'
            )
        instance = self.get_instance(instance_id)
        if instance is None:
            return None
        binding = self.current_binding(instance_id)
        ref = (
            binding.definition_ref
            if binding is not None
            else instance.definition_ref
        )
        if ref is None:
            if instance.external_identity is not None:
                return InstanceDefinitionResolution(
                    instance_id=instance_id,
                    status='EXTERNAL_UNRESOLVED',
                    detail=(
                        'external identity evidence only — no local '
                        'catalog resolution in effect'
                    ),
                )
            return InstanceDefinitionResolution(
                instance_id=instance_id,
                status='UNRESOLVED',
                detail='no catalog claim or external identity evidence',
            )
        definition = self.equipment_repository.get_definition_by_hash(
            ref.equipment_definition_sha256
        )
        if definition is None:
            return InstanceDefinitionResolution(
                instance_id=instance_id,
                status='MISSING_LOCAL_DEFINITION',
                binding=binding,
                detail=(
                    'no persisted EquipmentDefinition carries the claimed '
                    'semantic hash'
                ),
            )
        if (
            definition.definition_id != ref.equipment_definition_id
            or definition.version != ref.equipment_definition_version
        ):
            return InstanceDefinitionResolution(
                instance_id=instance_id,
                status='CONFLICT',
                definition=definition,
                binding=binding,
                detail=(
                    'a definition with the claimed hash persists but its '
                    'id/version differ from the claim'
                ),
            )
        return InstanceDefinitionResolution(
            instance_id=instance_id,
            status='RESOLVED_EXACT',
            definition=definition,
            binding=binding,
        )

    def instances_using_definition(
        self,
        document_id: str | None,
        definition_sha256: str,
    ) -> tuple[InstalledEquipmentInstance, ...]:
        """Instances whose *in-effect* resolution is this exact definition.

        Used by upgrade/adoption inventory (#608): external identity
        evidence and superseded bindings never count as users.
        """
        with closing(self._connect()) as connection:
            if document_id is None:
                rows = connection.execute(
                    'SELECT instance_id, document_id, equipment_class, '
                    'state, payload_json FROM '
                    'cad_installed_equipment_instances ORDER BY seq ASC'
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT instance_id, document_id, equipment_class, '
                    'state, payload_json FROM '
                    'cad_installed_equipment_instances WHERE document_id=? '
                    'ORDER BY seq ASC',
                    (document_id,),
                ).fetchall()
        matched: list[InstalledEquipmentInstance] = []
        for row in rows:
            instance = self._instance_from_row(row)
            binding = self.current_binding(instance.instance_id)
            ref = (
                binding.definition_ref
                if binding is not None
                else instance.definition_ref
            )
            if (
                ref is not None
                and ref.equipment_definition_sha256 == definition_sha256
            ):
                matched.append(instance)
        return tuple(matched)

    # ------------------------------------------------------------------
    # Observations and replacements


    def save_observation(
        self,
        observation: InstalledDeviceObservation,
    ) -> InstalledDeviceObservation:
        observation = InstalledDeviceObservation.model_validate(
            observation.model_dump(mode='python')
        )
        if self.get_instance(observation.instance_id) is None:
            raise ValueError(
                f'unknown installed equipment instance: {observation.instance_id}'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT payload_json FROM cad_installed_device_observations '
                'WHERE observation_id=?',
                (observation.observation_id,),
            ).fetchone()
            if existing is not None:
                persisted = InstalledDeviceObservation.model_validate_json(
                    existing['payload_json']
                )
                if persisted.semantic_sha256 != observation.semantic_sha256:
                    raise ValueError(
                        f'device observation {observation.observation_id} '
                        'already exists with different content'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_installed_device_observations(
                    observation_id, instance_id, observation_kind,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.instance_id,
                    observation.observation_kind,
                    observation.model_dump_json(),
                    observation.observed_at_utc,
                ),
            )
        return observation

    def list_observations(
        self,
        instance_id: str,
    ) -> tuple[InstalledDeviceObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT observation_id, instance_id, observation_kind, '
                'payload_json FROM cad_installed_device_observations '
                'WHERE instance_id=? ORDER BY seq ASC',
                (instance_id,),
            ).fetchall()
        return tuple(
            self._observation_from_row(row) for row in rows
        )

    @staticmethod
    def _replacement_row(
        connection: sqlite3.Connection, replacement_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            'SELECT replacement_id, document_id, removed_instance_id, '
            'installed_instance_id, payload_json '
            'FROM cad_installed_equipment_replacements '
            'WHERE replacement_id=?',
            (replacement_id,),
        ).fetchone()

    def _effective_state_on(
        self,
        connection: sqlite3.Connection,
        instance_id: str,
    ) -> InstalledEffectiveState | None:
        """Effective state recomputed inside the caller's transaction."""
        row = self._instance_row(connection, instance_id)
        if row is None:
            return None
        instance = InstalledEquipmentInstance.model_validate_json(
            row['payload_json']
        )
        if instance.state == 'removed':
            return 'removed'
        successor = connection.execute(
            'SELECT 1 FROM cad_installed_equipment_replacements '
            'WHERE removed_instance_id=? LIMIT 1',
            (instance_id,),
        ).fetchone()
        return 'replaced' if successor is not None else 'current'

    def _recheck_replacement_contract(
        self,
        connection: sqlite3.Connection,
        replacement: InstalledEquipmentReplacement,
    ) -> None:
        """Replacement invariants re-checked under the write lock (#842).

        Both instances must exist in the same document; the predecessor must
        still be effective 'current'; the successor must itself be an
        effective 'current' unit that is not already the predecessor or the
        successor of another replacement.
        """
        removed_row = self._instance_row(
            connection, replacement.removed_instance_id
        )
        installed_row = self._instance_row(
            connection, replacement.installed_instance_id
        )
        if removed_row is None or installed_row is None:
            raise ValueError(
                'replacement requires both instances to be persisted'
            )
        removed = InstalledEquipmentInstance.model_validate_json(
            removed_row['payload_json']
        )
        installed = InstalledEquipmentInstance.model_validate_json(
            installed_row['payload_json']
        )
        if removed.document_id != replacement.document_id:
            raise ValueError(
                'removed instance belongs to a different document'
            )
        if installed.document_id != replacement.document_id:
            raise ValueError(
                'installed instance belongs to a different document'
            )
        if self._effective_state_on(
            connection, removed.instance_id
        ) != 'current':
            raise ValueError(
                'removed instance is already replaced or removed'
            )
        # #854-E: the successor must be a valid current unit and must not
        # already be anyone's replacement or anyone's successor.
        if self._effective_state_on(
            connection, installed.instance_id
        ) != 'current':
            raise ValueError(
                'installed successor is not an effective current unit'
            )
        successor_row = connection.execute(
            'SELECT replacement_id FROM cad_installed_equipment_replacements '
            'WHERE installed_instance_id=? LIMIT 1',
            (installed.instance_id,),
        ).fetchone()
        if (
            successor_row is not None
            and str(successor_row['replacement_id'])
            != replacement.replacement_id
        ):
            raise ValueError(
                'installed successor already fills another replacement'
            )

    @staticmethod
    def _insert_replacement_row(
        connection: sqlite3.Connection,
        replacement: InstalledEquipmentReplacement,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cad_installed_equipment_replacements(
                replacement_id, document_id, removed_instance_id,
                installed_instance_id, payload_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                replacement.replacement_id,
                replacement.document_id,
                replacement.removed_instance_id,
                replacement.installed_instance_id,
                replacement.model_dump_json(),
                replacement.replaced_at_utc,
            ),
        )

    @staticmethod
    def _observation_from_row(row) -> InstalledDeviceObservation:
        observation = InstalledDeviceObservation.model_validate_json(
            row['payload_json']
        )
        if (
            row['observation_id'] != observation.observation_id
            or row['instance_id'] != observation.instance_id
            or row['observation_kind'] != observation.observation_kind
        ):
            raise ValueError(
                f'persisted installed device observation row disagrees with its '
                f'payload (row/payload mismatch for {row["observation_id"]})'
            )
        return observation

    def save_replacement(
        self,
        replacement: InstalledEquipmentReplacement,
    ) -> InstalledEquipmentReplacement:
        """Persist replacement lineage between two persisted instances."""
        replacement = InstalledEquipmentReplacement.model_validate(
            replacement.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = self._replacement_row(
                connection, replacement.replacement_id
            )
            if existing is not None:
                persisted = InstalledEquipmentReplacement.model_validate_json(
                    existing['payload_json']
                )
                if persisted.semantic_sha256 != replacement.semantic_sha256:
                    raise ValueError(
                        f'equipment replacement {replacement.replacement_id} '
                        'already exists with different content'
                    )
                return persisted
            # #842: predecessor state is re-checked under the write lock and
            # UNIQUE(removed_instance_id)/UNIQUE(installed_instance_id)
            # backstop a raced second successor.
            self._recheck_replacement_contract(connection, replacement)
            self._insert_replacement_row(connection, replacement)
        return replacement

    def replace_instance(
        self,
        *,
        replacement_id: str,
        removed_instance_id: str,
        installed_instance: InstalledEquipmentInstance,
        replaced_at_utc: str,
        provenance: Sequence[EquipmentDataProvenance],
        rationale: str | None = None,
    ) -> InstalledEquipmentReplacement:
        """Persist the new unit and the replacement lineage atomically.

        Both writes commit inside one ``BEGIN IMMEDIATE`` transaction (#842)
        — on any error neither the successor instance nor the replacement
        lineage becomes visible. The removed unit's record is untouched —
        its effective state becomes 'replaced' purely through the appended
        lineage record.
        """
        # Pre-validate everything before the transaction so a bad successor
        # or a missing predecessor never commits a partial result.
        installed_instance = self._validate_instance(installed_instance)
        removed = self.get_instance(removed_instance_id)
        if removed is None:
            raise ValueError(
                f'unknown installed equipment instance: {removed_instance_id}'
            )
        replacement = build_installed_equipment_replacement(
            replacement_id=replacement_id,
            document_id=removed.document_id,
            removed_instance_id=removed_instance_id,
            installed_instance_id=installed_instance.instance_id,
            replaced_at_utc=replaced_at_utc,
            provenance=provenance,
            rationale=rationale,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing_replacement = self._replacement_row(
                connection, replacement.replacement_id
            )
            if existing_replacement is not None:
                persisted = InstalledEquipmentReplacement.model_validate_json(
                    existing_replacement['payload_json']
                )
                if persisted.semantic_sha256 != replacement.semantic_sha256:
                    raise ValueError(
                        f'equipment replacement {replacement.replacement_id} '
                        'already exists with different content'
                    )
                return persisted
            installed_row = self._instance_row(
                connection, installed_instance.instance_id
            )
            if installed_row is None:
                self._insert_instance_row(connection, installed_instance)
            else:
                persisted = InstalledEquipmentInstance.model_validate_json(
                    installed_row['payload_json']
                )
                if (
                    persisted.semantic_sha256
                    != installed_instance.semantic_sha256
                ):
                    raise ValueError(
                        f'installed equipment instance '
                        f'{installed_instance.instance_id} already exists '
                        'with different content'
                    )
            self._recheck_replacement_contract(connection, replacement)
            self._insert_replacement_row(connection, replacement)
        return replacement


    def list_replacements(
        self,
        document_id: str,
    ) -> tuple[InstalledEquipmentReplacement, ...]:
        """Every persisted replacement for the document, oldest first.

        Reads replay the lineage invariants (#842): each predecessor yields
        at most one successor and the chain must be acyclic — a corrupt or
        imported row fails closed rather than surfacing a branched or
        cyclic replacement history.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT replacement_id, document_id, removed_instance_id, '
                'installed_instance_id, payload_json '
                'FROM cad_installed_equipment_replacements '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        replacements = tuple(
            self._replacement_from_row(row) for row in rows
        )
        outgoing = {}
        for replacement in replacements:
            previous = outgoing.setdefault(
                replacement.removed_instance_id, replacement
            )
            if previous.replacement_id != replacement.replacement_id:
                raise ValueError(
                    'replacement lineage branches: instance '
                    f'{replacement.removed_instance_id} has multiple '
                    'successors'
                )
        # Cycle detection on the successor chain: following each
        # replacement's installed unit to its own outgoing edge must
        # terminate — a looped chain fails closed.
        for replacement in replacements:
            seen = {replacement.replacement_id}
            current = replacement
            while current.installed_instance_id in outgoing:
                current = outgoing[current.installed_instance_id]
                if current.replacement_id in seen:
                    raise ValueError(
                        'replacement lineage contains a cycle'
                    )
                seen.add(current.replacement_id)
        return replacements

    @staticmethod
    def _replacement_from_row(row) -> InstalledEquipmentReplacement:
        replacement = InstalledEquipmentReplacement.model_validate_json(
            row['payload_json']
        )
        if (
            row['replacement_id'] != replacement.replacement_id
            or row['document_id'] != replacement.document_id
            or row['removed_instance_id'] != replacement.removed_instance_id
            or row['installed_instance_id']
            != replacement.installed_instance_id
        ):
            raise ValueError(
                f'persisted equipment replacement row disagrees with its '
                f'payload (row/payload mismatch for {row["replacement_id"]})'
            )
        return replacement


__all__ = [
    'CadInstalledEquipmentRepository',
    'ExternalEquipmentIdentity',
    'InstalledEffectiveState',
]
