"""Persistence for installed-equipment instance authority (#569).

Append-only tables living in the same project database as the rest of the
CAD authority stack. Every row stores the complete validated payload and is
re-validated on read, so corruption fails closed rather than surfacing a
doctored record.
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
    InstalledDeviceObservation,
    InstalledEquipmentInstance,
    InstalledEquipmentReplacement,
    build_installed_equipment_replacement,
)
from .cad_equipment import EquipmentDataProvenance
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


InstalledEffectiveState = Literal['current', 'replaced', 'removed']


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
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_installed_equipment_instances', 'cad_installed_definition_bindings', 'cad_installed_device_observations', 'cad_installed_equipment_replacements')

    # ------------------------------------------------------------------
    # Instances

    def _check_entity_binding(self, instance: InstalledEquipmentInstance) -> None:
        """A scene-entity binding must name an entity in the current head."""
        if instance.scene_entity_id is None:
            return
        head = self.scene_repository.current_head(instance.document_id)
        if head is None:
            raise ValueError(
                f'document {instance.document_id} has no scene revision to '
                'bind an installed instance against'
            )
        entity_ids = {entity.entity_id for entity in head.document.entities}
        if instance.scene_entity_id not in entity_ids:
            raise ValueError(
                f'scene entity {instance.scene_entity_id} is not present in '
                f'the current scene revision of {instance.document_id}'
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
                'installed equipment definition identity mismatch '
                '(id/version diverge from the claimed hash)'
            )
        return definition

    def save_instance(
        self,
        instance: InstalledEquipmentInstance,
    ) -> InstalledEquipmentInstance:
        """Persist one immutable installed-unit record.

        The catalog ref (when present) must resolve to an exact persisted
        ``EquipmentDefinition`` before the row is admitted; an
        already-persisted instance_id is immutable — a divergent resave
        fails closed.
        """
        instance = InstalledEquipmentInstance.model_validate(
            instance.model_dump(mode='python')
        )
        if instance.definition_ref is not None:
            self._resolve_ref_exact(instance.definition_ref)
        self._check_entity_binding(instance)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_installed_equipment_instances '
                'WHERE instance_id=?',
                (instance.instance_id,),
            ).fetchone()
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
                f'installed instance row/payload mismatch for '
                f'{row["instance_id"]}'
            )
        return instance

    def get_instance(
        self,
        instance_id: str,
    ) -> InstalledEquipmentInstance | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT instance_id, document_id, equipment_class, state, '
                'payload_json FROM cad_installed_equipment_instances '
                'WHERE instance_id=?',
                (instance_id,),
            ).fetchone()
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
        with closing(self._connect()) as connection, connection:
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
                f'installed binding row/payload mismatch for '
                f'{row["binding_id"]}'
            )
        return binding

    def list_bindings(
        self,
        instance_id: str,
    ) -> tuple[InstalledDefinitionBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT binding_id, instance_id, equipment_definition_sha256, '
                'payload_json FROM cad_installed_definition_bindings '
                'WHERE instance_id=? ORDER BY seq ASC',
                (instance_id,),
            ).fetchall()
        return tuple(self._binding_from_row(row) for row in rows)

    def current_binding(
        self,
        instance_id: str,
    ) -> InstalledDefinitionBinding | None:
        """The most recent catalog resolution for the instance, if any."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT binding_id, instance_id, equipment_definition_sha256, '
                'payload_json FROM cad_installed_definition_bindings '
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
                f'device observation row/payload mismatch for '
                f'{row["observation_id"]}'
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
        removed = self.get_instance(replacement.removed_instance_id)
        installed = self.get_instance(replacement.installed_instance_id)
        if removed is None or installed is None:
            raise ValueError(
                'replacement requires both instances to be persisted'
            )
        if removed.document_id != replacement.document_id:
            raise ValueError('removed instance belongs to a different document')
        if installed.document_id != replacement.document_id:
            raise ValueError('installed instance belongs to a different document')
        if self.effective_state(removed.instance_id) != 'current':
            raise ValueError(
                'removed instance is already replaced or removed'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_installed_equipment_replacements '
                'WHERE replacement_id=?',
                (replacement.replacement_id,),
            ).fetchone()
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

        The removed unit's record is untouched — its effective state becomes
        'replaced' purely through the appended lineage record.
        """
        installed = self.get_instance(installed_instance.instance_id)
        if installed is None:
            installed = self.save_instance(installed_instance)
        removed = self.get_instance(removed_instance_id)
        if removed is None:
            raise ValueError(
                f'unknown installed equipment instance: {removed_instance_id}'
            )
        replacement = build_installed_equipment_replacement(
            replacement_id=replacement_id,
            document_id=removed.document_id,
            removed_instance_id=removed_instance_id,
            installed_instance_id=installed.instance_id,
            replaced_at_utc=replaced_at_utc,
            provenance=provenance,
            rationale=rationale,
        )
        return self.save_replacement(replacement)

    def list_replacements(
        self,
        document_id: str,
    ) -> tuple[InstalledEquipmentReplacement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT replacement_id, document_id, removed_instance_id, '
                'installed_instance_id, payload_json FROM '
                'cad_installed_equipment_replacements WHERE document_id=? '
                'ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            self._replacement_from_row(row) for row in rows
        )

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
                f'equipment replacement row/payload mismatch for '
                f'{row["replacement_id"]}'
            )
        return replacement


__all__ = [
    'CadInstalledEquipmentRepository',
    'ExternalEquipmentIdentity',
    'InstalledEffectiveState',
]
