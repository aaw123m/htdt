"""Append-only persistence for the feature-authority batch (#886).

The acoustic performance target, sound-isolation, rack/BOM, drawing-set and
field-label authorities were introduced as immutable, semantically-hashed
in-memory models (#558 #559 #562 #639 #644 #665) but had no canonical store:
a saved record could neither be re-opened nor resolved through the
canonical authority registry. This repository is that store — one typed,
append-only table per authority kind over the shared scene database — and
:class:`build_canonical_authority_registry` adopts every kind, so a feature
ref resolves exactly like any other pinned authority.

Contract properties:

- records are immutable and versioned where the model carries a version:
  ``(id, version)`` may be saved exactly once; id-only records are keyed by
  their content hash, so re-saving identical payload is an idempotent no-op
  and a different payload under an existing id is a collision, never an
  in-place update;
- ``document_id`` is the project scope the record was bound to at save —
  records with an intrinsic document field (target profile, BOM, field
  label) are stored under exactly that document; records without one
  (isolation, rack, drawing authorities) take the explicit binding the
  caller supplies, or stay global-library when none is given;
- reads re-validate the persisted payload through the owning pydantic
  model — corrupt rows fail closed;
- :meth:`resolve` backs the canonical registry adapters: a ref only
  resolves when the stored scope satisfies the asking document (global
  records satisfy any document; project records satisfy only their own).
"""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel

from .cad_acoustic_target import AcousticPerformanceTargetProfile
from .cad_drawing_set import DrawingSetSpec, InstallationDrawingSet
from .cad_field_labels import FieldLabel, LabelSheet
from .cad_project_bom import ProjectBOM
from .cad_rack_infrastructure import RackDefinition, RackLayout
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_sound_isolation import (
    IsolationAssembly,
    IsolationEstimate,
    IsolationMeasurement,
    IsolationScenario,
)


class FeatureAuthorityConflictError(ValueError):
    """A feature-authority save violated append-only identity rules."""


FeatureAuthorityKind = Literal[
    'acoustic_target_profile',
    'isolation_assembly',
    'isolation_scenario',
    'isolation_estimate',
    'isolation_measurement',
    'rack_definition',
    'rack_layout',
    'project_bom',
    'drawing_set_spec',
    'installation_drawing_set',
    'field_label',
    'label_sheet',
]


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _payload_digest(record: BaseModel) -> str:
    """Content hash for records that carry no intrinsic semantic hash."""

    return _digest(record.model_dump(mode='json'))


class CadFeatureAuthorityRepository:
    """Native append-only storage for the feature-authority batch (#886).

    Every table lives on the shared scene database path so a project file
    carries its feature authorities with the rest of the twin. ``save_*``
    never UPDATE: identical re-saves are idempotent no-ops, divergent
    payloads under a claimed identity raise
    :class:`FeatureAuthorityConflictError`.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_acoustic_target_profiles',
                'cad_isolation_assemblies',
                'cad_isolation_scenarios',
                'cad_isolation_estimates',
                'cad_isolation_measurements',
                'cad_rack_definitions',
                'cad_rack_layouts',
                'cad_project_boms',
                'cad_drawing_set_specs',
                'cad_installation_drawing_sets',
                'cad_field_labels',
                'cad_field_label_sheets',
            )

    def _insert(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        values: tuple[Any, ...],
        unique_key_where: str,
        unique_key_args: tuple[Any, ...],
        semantic_sha256: str,
    ) -> None:
        """Insert one authority row under append-only semantics.

        An existing row with the same unique key must carry the identical
        semantic hash — then the save is an idempotent no-op. A divergent
        hash under a claimed identity is a collision.
        """
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                f'SELECT semantic_sha256 FROM {table} '  # noqa: S608 - table/columns are module constants
                f'WHERE {unique_key_where}',
                unique_key_args,
            ).fetchone()
            if existing is not None:
                if existing['semantic_sha256'] == semantic_sha256:
                    return
                raise FeatureAuthorityConflictError(
                    f'{table} record is append-only; the claimed identity '
                    'already exists with different content'
                )
            connection.execute(
                f'INSERT INTO {table} ({", ".join(columns)}) '  # noqa: S608
                f'VALUES ({", ".join("?" for _ in columns)})',
                (*values,),
            )

    def _rows(
        self,
        table: str,
        *,
        where: str = '',
        args: tuple[Any, ...] = (),
    ) -> list[sqlite3.Row]:
        query = f'SELECT * FROM {table}'  # noqa: S608
        if where:
            query += f' WHERE {where}'
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            return connection.execute(query, args).fetchall()

    # ------------------------------------------------------------------
    # Acoustic performance target profiles (#558)

    def save_acoustic_target_profile(
        self, profile: AcousticPerformanceTargetProfile
    ) -> None:
        self._insert(
            table='cad_acoustic_target_profiles',
            columns=(
                'profile_id', 'profile_version', 'document_id',
                'semantic_sha256', 'payload_json', 'created_at_utc',
            ),
            values=(
                profile.profile_id,
                profile.profile_version,
                profile.document_id,
                profile.target_semantic_hash,
                profile.model_dump_json(),
                profile.created_at_utc,
            ),
            unique_key_where='profile_id=? AND profile_version=?',
            unique_key_args=(profile.profile_id, profile.profile_version),
            semantic_sha256=profile.target_semantic_hash,
        )

    def get_acoustic_target_profile(
        self, profile_id: str, profile_version: str
    ) -> AcousticPerformanceTargetProfile | None:
        rows = self._rows(
            'cad_acoustic_target_profiles',
            where='profile_id=? AND profile_version=?',
            args=(profile_id, profile_version),
        )
        if not rows:
            return None
        return AcousticPerformanceTargetProfile.model_validate_json(
            rows[0]['payload_json']
        )

    def list_acoustic_target_profiles(
        self, document_id: str
    ) -> tuple[AcousticPerformanceTargetProfile, ...]:
        return tuple(
            AcousticPerformanceTargetProfile.model_validate_json(
                row['payload_json']
            )
            for row in self._rows(
                'cad_acoustic_target_profiles',
                where='document_id=?',
                args=(document_id,),
            )
        )

    # ------------------------------------------------------------------
    # Sound isolation (#559)

    def save_isolation_assembly(
        self, assembly: IsolationAssembly, *, document_id: str | None = None
    ) -> None:
        self._insert(
            table='cad_isolation_assemblies',
            columns=(
                'assembly_id', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                assembly.assembly_id,
                document_id,
                assembly.semantic_sha256,
                assembly.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='assembly_id=?',
            unique_key_args=(assembly.assembly_id,),
            semantic_sha256=assembly.semantic_sha256,
        )

    def get_isolation_assembly(
        self, assembly_id: str
    ) -> IsolationAssembly | None:
        rows = self._rows(
            'cad_isolation_assemblies',
            where='assembly_id=?',
            args=(assembly_id,),
        )
        if not rows:
            return None
        return IsolationAssembly.model_validate_json(rows[0]['payload_json'])

    def list_isolation_assemblies(
        self, document_id: str | None = None
    ) -> tuple[IsolationAssembly, ...]:
        rows = (
            self._rows('cad_isolation_assemblies')
            if document_id is None
            else self._rows(
                'cad_isolation_assemblies',
                where='document_id=? OR document_id IS NULL',
                args=(document_id,),
            )
        )
        return tuple(
            IsolationAssembly.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_isolation_scenario(
        self, scenario: IsolationScenario, *, document_id: str | None = None
    ) -> str:
        digest = _payload_digest(scenario)
        self._insert(
            table='cad_isolation_scenarios',
            columns=(
                'scenario_id', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                scenario.scenario_id,
                document_id,
                digest,
                scenario.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='scenario_id=?',
            unique_key_args=(scenario.scenario_id,),
            semantic_sha256=digest,
        )
        return digest

    def get_isolation_scenario(
        self, scenario_id: str
    ) -> IsolationScenario | None:
        rows = self._rows(
            'cad_isolation_scenarios',
            where='scenario_id=?',
            args=(scenario_id,),
        )
        if not rows:
            return None
        return IsolationScenario.model_validate_json(rows[0]['payload_json'])

    def save_isolation_estimate(
        self, estimate: IsolationEstimate, *, document_id: str | None = None
    ) -> None:
        # The estimate requires its declared scenario to be persisted first —
        # an orphan estimate can never resolve its evaluation context.
        if self.get_isolation_scenario(estimate.scenario_id) is None:
            raise FeatureAuthorityConflictError(
                'isolation estimate requires its persisted scenario'
            )
        self._insert(
            table='cad_isolation_estimates',
            columns=(
                'estimate_id', 'scenario_id', 'document_id',
                'semantic_sha256', 'payload_json', 'created_at_utc',
            ),
            values=(
                estimate.estimate_sha256,
                estimate.scenario_id,
                document_id,
                estimate.estimate_sha256,
                estimate.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='estimate_id=?',
            unique_key_args=(estimate.estimate_sha256,),
            semantic_sha256=estimate.estimate_sha256,
        )

    def get_isolation_estimate(
        self, estimate_sha256: str
    ) -> IsolationEstimate | None:
        rows = self._rows(
            'cad_isolation_estimates',
            where='estimate_id=?',
            args=(estimate_sha256,),
        )
        if not rows:
            return None
        return IsolationEstimate.model_validate_json(rows[0]['payload_json'])

    def save_isolation_measurement(
        self, measurement: IsolationMeasurement, *, document_id: str | None = None
    ) -> str:
        digest = _payload_digest(measurement)
        self._insert(
            table='cad_isolation_measurements',
            columns=(
                'measurement_id', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                measurement.measurement_id,
                document_id,
                digest,
                measurement.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='measurement_id=?',
            unique_key_args=(measurement.measurement_id,),
            semantic_sha256=digest,
        )
        return digest

    def get_isolation_measurement(
        self, measurement_id: str
    ) -> IsolationMeasurement | None:
        rows = self._rows(
            'cad_isolation_measurements',
            where='measurement_id=?',
            args=(measurement_id,),
        )
        if not rows:
            return None
        return IsolationMeasurement.model_validate_json(rows[0]['payload_json'])

    # ------------------------------------------------------------------
    # Rack infrastructure (#562)

    def save_rack_definition(
        self, rack: RackDefinition, *, document_id: str | None = None
    ) -> None:
        self._insert(
            table='cad_rack_definitions',
            columns=(
                'rack_id', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                rack.rack_id,
                document_id,
                rack.semantic_sha256,
                rack.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='rack_id=?',
            unique_key_args=(rack.rack_id,),
            semantic_sha256=rack.semantic_sha256,
        )

    def get_rack_definition(self, rack_id: str) -> RackDefinition | None:
        rows = self._rows(
            'cad_rack_definitions', where='rack_id=?', args=(rack_id,)
        )
        if not rows:
            return None
        return RackDefinition.model_validate_json(rows[0]['payload_json'])

    def save_rack_layout(
        self, layout: RackLayout, *, document_id: str | None = None
    ) -> str:
        """Persist a layout keyed by content — layouts carry no own id."""
        if self.get_rack_definition(layout.rack_id) is None:
            raise FeatureAuthorityConflictError(
                'rack layout requires its persisted rack definition'
            )
        digest = _payload_digest(layout)
        layout_id = f'rack-layout:{digest}'
        self._insert(
            table='cad_rack_layouts',
            columns=(
                'layout_id', 'rack_id', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                layout_id,
                layout.rack_id,
                document_id,
                digest,
                layout.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='layout_id=?',
            unique_key_args=(layout_id,),
            semantic_sha256=digest,
        )
        return layout_id

    def get_rack_layout(self, layout_id: str) -> RackLayout | None:
        rows = self._rows(
            'cad_rack_layouts', where='layout_id=?', args=(layout_id,)
        )
        if not rows:
            return None
        return RackLayout.model_validate_json(rows[0]['payload_json'])

    # ------------------------------------------------------------------
    # Project BOM (#639)

    def save_project_bom(self, bom: ProjectBOM) -> None:
        self._insert(
            table='cad_project_boms',
            columns=(
                'bom_id', 'version', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                bom.bom_id,
                bom.version,
                bom.document_id,
                bom.bom_semantic_hash,
                bom.model_dump_json(),
                bom.generated_at_utc,
            ),
            unique_key_where='bom_id=? AND version=?',
            unique_key_args=(bom.bom_id, bom.version),
            semantic_sha256=bom.bom_semantic_hash,
        )

    def get_project_bom(
        self, bom_id: str, version: str
    ) -> ProjectBOM | None:
        rows = self._rows(
            'cad_project_boms',
            where='bom_id=? AND version=?',
            args=(bom_id, version),
        )
        if not rows:
            return None
        return ProjectBOM.model_validate_json(rows[0]['payload_json'])

    def list_project_boms(self, document_id: str) -> tuple[ProjectBOM, ...]:
        return tuple(
            ProjectBOM.model_validate_json(row['payload_json'])
            for row in self._rows(
                'cad_project_boms',
                where='document_id=?',
                args=(document_id,),
            )
        )

    # ------------------------------------------------------------------
    # Installation drawing sets (#644)

    def save_drawing_set_spec(
        self, spec: DrawingSetSpec, *, document_id: str | None = None
    ) -> None:
        self._insert(
            table='cad_drawing_set_specs',
            columns=(
                'spec_id', 'spec_version', 'document_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                spec.spec_id,
                spec.spec_version,
                document_id,
                spec.spec_semantic_hash,
                spec.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='spec_id=? AND spec_version=?',
            unique_key_args=(spec.spec_id, spec.spec_version),
            semantic_sha256=spec.spec_semantic_hash,
        )

    def get_drawing_set_spec(
        self, spec_id: str, spec_version: str
    ) -> DrawingSetSpec | None:
        rows = self._rows(
            'cad_drawing_set_specs',
            where='spec_id=? AND spec_version=?',
            args=(spec_id, spec_version),
        )
        if not rows:
            return None
        return DrawingSetSpec.model_validate_json(rows[0]['payload_json'])

    def save_installation_drawing_set(
        self, drawing_set: InstallationDrawingSet, *,
        document_id: str | None = None,
    ) -> str:
        digest = _payload_digest(drawing_set)
        self._insert(
            table='cad_installation_drawing_sets',
            columns=(
                'drawing_set_id', 'document_id',
                'installation_output_sha256', 'spec_sha256',
                'semantic_sha256', 'payload_json', 'created_at_utc',
            ),
            values=(
                drawing_set.drawing_set_id,
                document_id,
                drawing_set.installation_output_sha256,
                drawing_set.spec_sha256,
                digest,
                drawing_set.model_dump_json(),
                drawing_set.generated_at_utc,
            ),
            unique_key_where='drawing_set_id=?',
            unique_key_args=(drawing_set.drawing_set_id,),
            semantic_sha256=digest,
        )
        return digest

    def get_installation_drawing_set(
        self, drawing_set_id: str
    ) -> InstallationDrawingSet | None:
        rows = self._rows(
            'cad_installation_drawing_sets',
            where='drawing_set_id=?',
            args=(drawing_set_id,),
        )
        if not rows:
            return None
        return InstallationDrawingSet.model_validate_json(
            rows[0]['payload_json']
        )

    # ------------------------------------------------------------------
    # Field labels (#665)

    def save_field_label(self, label: FieldLabel) -> None:
        self._insert(
            table='cad_field_labels',
            columns=(
                'label_id', 'project_id', 'target_id', 'generation',
                'semantic_sha256', 'payload_json', 'created_at_utc',
            ),
            values=(
                label.label_id,
                label.payload.p,
                label.payload.t,
                label.payload.g,
                _digest(label.payload.qr_text()),
                label.model_dump_json(),
                label.created_at_utc,
            ),
            unique_key_where='target_id=? AND generation=?',
            unique_key_args=(label.payload.t, label.payload.g),
            semantic_sha256=_digest(label.payload.qr_text()),
        )

    def get_field_label(self, label_id: str) -> FieldLabel | None:
        rows = self._rows(
            'cad_field_labels', where='label_id=?', args=(label_id,)
        )
        if not rows:
            return None
        return FieldLabel.model_validate_json(rows[0]['payload_json'])

    def list_field_labels(
        self, project_id: str, *, target_id: str | None = None
    ) -> tuple[FieldLabel, ...]:
        rows = (
            self._rows(
                'cad_field_labels',
                where='project_id=? AND target_id=?',
                args=(project_id, target_id),
            )
            if target_id is not None
            else self._rows(
                'cad_field_labels',
                where='project_id=?',
                args=(project_id,),
            )
        )
        return tuple(
            FieldLabel.model_validate_json(row['payload_json']) for row in rows
        )

    def save_label_sheet(
        self, sheet: LabelSheet, *, project_id: str | None = None
    ) -> None:
        # Every label on a persisted sheet must itself be persisted — a
        # printed sheet can never carry an unminted label identity.
        for label in sheet.labels:
            stored = self.get_field_label(label.label_id)
            if stored is None or stored != label:
                raise FeatureAuthorityConflictError(
                    'label sheet requires every label to be persisted first'
                )
        self._insert(
            table='cad_field_label_sheets',
            columns=(
                'sheet_id', 'project_id', 'semantic_sha256',
                'payload_json', 'created_at_utc',
            ),
            values=(
                sheet.sheet_id,
                project_id,
                sheet.sheet_content_sha256,
                sheet.model_dump_json(),
                _utc_now(),
            ),
            unique_key_where='sheet_id=?',
            unique_key_args=(sheet.sheet_id,),
            semantic_sha256=sheet.sheet_content_sha256,
        )

    def get_label_sheet(self, sheet_id: str) -> LabelSheet | None:
        rows = self._rows(
            'cad_field_label_sheets', where='sheet_id=?', args=(sheet_id,)
        )
        if not rows:
            return None
        return LabelSheet.model_validate_json(rows[0]['payload_json'])

    # ------------------------------------------------------------------
    # Canonical resolution (backs cad_authority_registry adapters)

    def resolve(
        self, kind: FeatureAuthorityKind, ref_id: str, document_id: str
    ) -> tuple[str | None, str] | None:
        """Resolve ``kind``/``ref_id`` for a record owned by ``document_id``.

        Returns ``(owning_document_id_or_None, semantic_sha256)`` — the same
        pair :class:`CanonicalAuthority` needs — or ``None`` when no
        persisted record proves the ref for this document's scope. Global
        (unbound) records satisfy any document; bound records satisfy only
        their own.
        """

        row = self._resolve_row(kind, ref_id)
        if row is None:
            return None
        owner = row['document_id'] if 'document_id' in row.keys() else None
        if owner is None:
            owner = row['project_id'] if 'project_id' in row.keys() else None
        if owner is not None and owner != document_id:
            return None
        return (owner, row['semantic_sha256'])

    def _resolve_row(
        self, kind: FeatureAuthorityKind, ref_id: str
    ) -> sqlite3.Row | None:
        if kind == 'acoustic_target_profile':
            # Refs name the profile id; the exact authority is the latest
            # persisted version.
            rows = self._rows(
                'cad_acoustic_target_profiles',
                where='profile_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        if kind == 'isolation_assembly':
            rows = self._rows(
                'cad_isolation_assemblies',
                where='assembly_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        if kind == 'isolation_scenario':
            rows = self._rows(
                'cad_isolation_scenarios',
                where='scenario_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        if kind == 'isolation_estimate':
            rows = self._rows(
                'cad_isolation_estimates',
                where='estimate_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        if kind == 'isolation_measurement':
            rows = self._rows(
                'cad_isolation_measurements',
                where='measurement_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        if kind == 'rack_definition':
            rows = self._rows(
                'cad_rack_definitions', where='rack_id=?', args=(ref_id,)
            )
            return rows[-1] if rows else None
        if kind == 'rack_layout':
            rows = self._rows(
                'cad_rack_layouts', where='layout_id=?', args=(ref_id,)
            )
            return rows[-1] if rows else None
        if kind == 'project_bom':
            rows = self._rows(
                'cad_project_boms', where='bom_id=?', args=(ref_id,)
            )
            return rows[-1] if rows else None
        if kind == 'drawing_set_spec':
            rows = self._rows(
                'cad_drawing_set_specs', where='spec_id=?', args=(ref_id,)
            )
            return rows[-1] if rows else None
        if kind == 'installation_drawing_set':
            rows = self._rows(
                'cad_installation_drawing_sets',
                where='drawing_set_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        if kind == 'field_label':
            rows = self._rows(
                'cad_field_labels', where='label_id=?', args=(ref_id,)
            )
            return rows[-1] if rows else None
        if kind == 'label_sheet':
            rows = self._rows(
                'cad_field_label_sheets',
                where='sheet_id=?',
                args=(ref_id,),
            )
            return rows[-1] if rows else None
        raise ValueError(f'unknown feature authority kind {kind!r}')


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
