"""Persistence for LayoutProfile + CurrentSystemTopology (#505)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_equipment_instance_repository import CadInstalledEquipmentRepository
from .cad_equipment_repository import CadEquipmentRepository
from .cad_layout_profile import (
    CurrentSystemTopology,
    LayoutProfile,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


class CadLayoutProfileRepository:
    """Append-only LayoutProfile library; (profile_id, version) is immutable."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_layout_profiles')

    def save_profile(self, profile: LayoutProfile) -> LayoutProfile:
        profile = LayoutProfile.model_validate(profile.model_dump(mode='python'))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_layout_profiles
                WHERE profile_id=? AND version=?
                """,
                (profile.profile_id, profile.version),
            ).fetchone()
            if existing is not None:
                persisted = LayoutProfile.model_validate_json(
                    existing['payload_json']
                )
                if persisted != profile:
                    raise ValueError(
                        'layout profile id/version already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_layout_profiles(
                    profile_id, version, semantic_sha256, name, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.semantic_sha256,
                    profile.name,
                    profile.model_dump_json(),
                ),
            )
        return profile

    def get_profile(
        self,
        profile_id: str,
        version: str,
    ) -> LayoutProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_layout_profiles
                WHERE profile_id=? AND version=?
                """,
                (profile_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else LayoutProfile.model_validate_json(row['payload_json'])
        )

    def get_profile_by_hash(
        self,
        semantic_sha256: str,
    ) -> LayoutProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_layout_profiles
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else LayoutProfile.model_validate_json(row['payload_json'])
        )

    def list_profiles(self) -> tuple[LayoutProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_layout_profiles ORDER BY seq ASC'
            ).fetchall()
        return tuple(
            LayoutProfile.model_validate_json(row['payload_json'])
            for row in rows
        )


class CadCurrentTopologyRepository:
    """Append-only CurrentSystemTopology records, exact-refs replayed on read."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        profile_repository: CadLayoutProfileRepository,
        equipment_repository: CadEquipmentRepository | None = None,
        instance_repository: CadInstalledEquipmentRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.profile_repository = profile_repository
        self.equipment_repository = equipment_repository
        self.instance_repository = instance_repository
        self.path = Path(scene_repository.path)
        if (
            equipment_repository is not None
            and Path(equipment_repository.path) != self.path
        ) or (
            instance_repository is not None
            and Path(instance_repository.path) != self.path
        ):
            raise ValueError(
                'topology repositories must share one native CAD database'
            )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_current_topologies')

    def _validate_topology_refs(
        self,
        topology: CurrentSystemTopology,
    ) -> CurrentSystemTopology:
        revision = self.scene_repository.get(topology.scene_revision_id)
        if revision is None:
            raise ValueError(
                'current system topology references missing SceneRevision'
            )
        if revision.content_hash != topology.scene_content_hash:
            raise ValueError(
                'current system topology scene content hash mismatch'
            )
        if revision.document_id != topology.document_id:
            raise ValueError('current system topology document mismatch')
        profile = self.profile_repository.get_profile_by_hash(
            topology.layout_profile.semantic_sha256
        )
        if profile is None:
            raise ValueError(
                'current system topology references missing LayoutProfile'
            )
        if (
            profile.profile_id != topology.layout_profile.authority_id
            or profile.version != topology.layout_profile.version
        ):
            raise ValueError(
                'current system topology LayoutProfile identity mismatch'
            )
        for item in topology.bindings:
            if item.equipment is not None:
                if self.equipment_repository is None:
                    raise ValueError(
                        'equipment-bearing topology requires an '
                        'equipment repository'
                    )
                definition = self.equipment_repository.get_definition_by_hash(
                    item.equipment.semantic_sha256
                )
                if (
                    definition is None
                    or definition.definition_id != item.equipment.authority_id
                    or definition.version != item.equipment.version
                ):
                    raise ValueError(
                        'topology binding references missing exact '
                        'EquipmentDefinition'
                    )
            if item.installed_instance_id is not None:
                if self.instance_repository is None:
                    raise ValueError(
                        'instance-bearing topology requires an installed '
                        'equipment repository'
                    )
                instance = self.instance_repository.get_instance(
                    item.installed_instance_id
                )
                if (
                    instance is None
                    or instance.semantic_sha256
                    != item.installed_instance_sha256
                ):
                    raise ValueError(
                        'topology binding references missing exact '
                        'InstalledEquipmentInstance'
                    )
                if instance.scene_entity_id != item.entity_id:
                    raise ValueError(
                        'topology binding installed instance does not back '
                        'the bound scene entity'
                    )
        return topology

    def save_topology(
        self,
        topology: CurrentSystemTopology,
    ) -> CurrentSystemTopology:
        topology = CurrentSystemTopology.model_validate(
            topology.model_dump(mode='python')
        )
        self._validate_topology_refs(topology)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_current_topologies
                WHERE topology_id=?
                """,
                (topology.topology_id,),
            ).fetchone()
            if existing is not None:
                persisted = CurrentSystemTopology.model_validate_json(
                    existing['payload_json']
                )
                if persisted != topology:
                    raise ValueError(
                        'current topology id already exists with different '
                        'semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_current_topologies(
                    topology_id, semantic_sha256, document_id,
                    scene_revision_id, layout_profile_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    topology.topology_id,
                    topology.semantic_sha256,
                    topology.document_id,
                    topology.scene_revision_id,
                    topology.layout_profile.semantic_sha256,
                    topology.model_dump_json(),
                ),
            )
        return topology

    def get_topology(self, topology_id: str) -> CurrentSystemTopology | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_current_topologies
                WHERE topology_id=?
                """,
                (topology_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_topology_refs(
            CurrentSystemTopology.model_validate_json(row['payload_json'])
        )

    def current_topology(
        self,
        document_id: str,
    ) -> CurrentSystemTopology | None:
        """The latest topology bound to the document's current head."""
        head = self.scene_repository.current_head(document_id)
        if head is None:
            return None
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_current_topologies
                WHERE document_id=? ORDER BY seq DESC
                """,
                (document_id,),
            ).fetchall()
        for row in rows:
            topology = CurrentSystemTopology.model_validate_json(
                row['payload_json']
            )
            if topology.scene_revision_id == head.revision_id:
                return self._validate_topology_refs(topology)
        return None


__all__ = ['CadCurrentTopologyRepository', 'CadLayoutProfileRepository']
