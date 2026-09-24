"""Append-only persistence for theater operating presets (#567)."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import TYPE_CHECKING

from .cad_operating_preset import (
    AppliedPresetState,
    PresetMeasurementBinding,
    TheaterOperatingPreset,
)
from .cad_repository import SceneRepository

if TYPE_CHECKING:
    from .cad_authority_refs import AuthorityRefResolver


class OperatingPresetConflictError(ValueError):
    """A preset/applied/binding save violated append-only identity rules."""


#: Preset component kinds that name canonical persisted authorities and can
#: therefore be re-verified by exact reference resolution. Component kinds
#: naming declared-only or UI-level artifacts (e.g. ``playback_chain``,
#: ``presentation_profile``) have no canonical table and stay declared-only —
#: an authority's absence there is never fabricated as a failure.
_COMPONENT_RESOLVER_KINDS: dict[str, str] = {
    'system_variant': 'system_variant',
    'operating_preset': 'operating_preset',
    'design_checkpoint': 'design_checkpoint',
}


class CadOperatingPresetRepository:
    """Native storage for TheaterOperatingPreset + applied/measured bindings.

    Presets are immutable: changing what "Movie" means creates a new preset,
    never an UPDATE. Applied-state confirmations and measurement bindings
    are append-only facts that reference the exact ``preset_sha256``.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        ref_resolver: 'AuthorityRefResolver | None' = None,
    ) -> None:
        self.scene_repository = scene_repository
        if ref_resolver is None:
            from .cad_authority_refs import CanonicalAuthorityRefResolver

            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_operating_presets (
                    preset_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    preset_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_applied_preset_states (
                    applied_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    preset_id TEXT NOT NULL,
                    preset_sha256 TEXT NOT NULL,
                    confirmed_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_preset_measurement_bindings (
                    binding_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    preset_id TEXT NOT NULL,
                    preset_sha256 TEXT NOT NULL,
                    bound_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    # ------------------------------------------------------------------
    # Presets

    def save_preset(self, preset: TheaterOperatingPreset) -> None:
        if self.get_preset(preset.preset_id) is not None:
            raise OperatingPresetConflictError(
                'TheaterOperatingPreset ids are append-only'
            )
        self._assert_scene_pin(
            preset.scene_revision_id,
            preset.scene_content_hash,
            preset.document_id,
            'preset',
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_operating_presets (
                    preset_id, document_id, category, preset_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    preset.preset_id,
                    preset.document_id,
                    preset.category,
                    preset.preset_sha256,
                    preset.created_at_utc,
                    preset.model_dump_json(),
                ),
            )

    def get_preset(self, preset_id: str) -> TheaterOperatingPreset | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_operating_presets WHERE preset_id=?',
                (preset_id,),
            ).fetchone()
        if row is None:
            return None
        return TheaterOperatingPreset.model_validate_json(row['payload_json'])

    def list_presets(
        self,
        document_id: str,
    ) -> tuple[TheaterOperatingPreset, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_operating_presets
                WHERE document_id=?
                ORDER BY created_at_utc, preset_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            TheaterOperatingPreset.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Applied device state

    def save_applied_state(self, applied: AppliedPresetState) -> None:
        if self.get_applied_state(applied.applied_id) is not None:
            raise OperatingPresetConflictError(
                'AppliedPresetState ids are append-only'
            )
        preset = self.get_preset(applied.preset_id)
        if preset is None:
            raise ValueError('applied state requires a persisted preset')
        if preset.preset_sha256 != applied.preset_sha256:
            raise ValueError('applied state is bound to a different preset revision')
        if preset.document_id != applied.document_id:
            raise ValueError('applied state document does not match preset')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_applied_preset_states (
                    applied_id, document_id, preset_id, preset_sha256,
                    confirmed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    applied.applied_id,
                    applied.document_id,
                    applied.preset_id,
                    applied.preset_sha256,
                    applied.confirmed_at_utc,
                    applied.model_dump_json(),
                ),
            )

    def get_applied_state(self, applied_id: str) -> AppliedPresetState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_applied_preset_states WHERE applied_id=?',
                (applied_id,),
            ).fetchone()
        if row is None:
            return None
        return AppliedPresetState.model_validate_json(row['payload_json'])

    def list_applied_states(
        self,
        preset_id: str,
    ) -> tuple[AppliedPresetState, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_applied_preset_states
                WHERE preset_id=?
                ORDER BY confirmed_at_utc, applied_id
                """,
                (preset_id,),
            ).fetchall()
        return tuple(
            AppliedPresetState.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Measurement bindings

    def save_measurement_binding(self, binding: PresetMeasurementBinding) -> None:
        if self.get_measurement_binding(binding.binding_id) is not None:
            raise OperatingPresetConflictError(
                'PresetMeasurementBinding ids are append-only'
            )
        preset = self.get_preset(binding.preset_id)
        if preset is None:
            raise ValueError('measurement binding requires a persisted preset')
        if preset.preset_sha256 != binding.preset_sha256:
            raise ValueError('measurement binding is bound to a different preset')
        if preset.document_id != binding.document_id:
            raise ValueError('measurement binding document does not match preset')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_preset_measurement_bindings (
                    binding_id, document_id, preset_id, preset_sha256,
                    bound_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.document_id,
                    binding.preset_id,
                    binding.preset_sha256,
                    binding.bound_at_utc,
                    binding.model_dump_json(),
                ),
            )

    def get_measurement_binding(
        self,
        binding_id: str,
    ) -> PresetMeasurementBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_preset_measurement_bindings WHERE binding_id=?',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return PresetMeasurementBinding.model_validate_json(row['payload_json'])

    def list_measurement_bindings(
        self,
        preset_id: str,
    ) -> tuple[PresetMeasurementBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_preset_measurement_bindings
                WHERE preset_id=?
                ORDER BY bound_at_utc, binding_id
                """,
                (preset_id,),
            ).fetchall()
        return tuple(
            PresetMeasurementBinding.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Persisted-record verification (#757 semantic audit)

    def verify_persisted_preset(self, preset_id: str) -> TheaterOperatingPreset:
        """Re-run the save-time invariants on a persisted preset.

        Raises ``ValueError`` when the row fails any invariant — the scene
        pin must resolve, and every component ref whose kind names a
        canonical authority must resolve, stay in-project, and match the
        pinned semantic hash.
        """
        preset = self.get_preset(preset_id)
        if preset is None:
            raise ValueError(f'preset {preset_id} no longer resolves')
        self._assert_scene_pin(
            preset.scene_revision_id,
            preset.scene_content_hash,
            preset.document_id,
            'preset',
        )
        for ref in preset.component_refs:
            resolver_kind = _COMPONENT_RESOLVER_KINDS.get(ref.kind)
            if resolver_kind is None:
                continue
            self._assert_resolves(
                resolver_kind, ref.ref_id, ref.ref_sha256, preset.document_id
            )
        return preset

    def verify_persisted_applied_state(self, applied_id: str) -> AppliedPresetState:
        applied = self.get_applied_state(applied_id)
        if applied is None:
            raise ValueError(f'applied preset state {applied_id} no longer resolves')
        self._assert_preset_binding(
            applied.preset_id, applied.preset_sha256, applied.document_id
        )
        return applied

    def verify_persisted_measurement_binding(
        self, binding_id: str
    ) -> PresetMeasurementBinding:
        binding = self.get_measurement_binding(binding_id)
        if binding is None:
            raise ValueError(
                f'measurement binding {binding_id} no longer resolves'
            )
        self._assert_preset_binding(
            binding.preset_id, binding.preset_sha256, binding.document_id
        )
        for measurement_id in binding.measurement_ids:
            self._assert_resolves(
                'measurement', measurement_id, None, binding.document_id
            )
        return binding

    def _assert_scene_pin(
        self,
        scene_revision_id: str,
        scene_content_hash: str,
        document_id: str,
        owner: str,
    ) -> None:
        revision = self.scene_repository.get(scene_revision_id)
        if revision is None:
            raise ValueError(f'{owner} pins a SceneRevision that is not persisted')
        if revision.content_hash != scene_content_hash:
            raise ValueError(f'{owner} SceneRevision content hash mismatch')
        if revision.document_id != document_id:
            raise ValueError(f'{owner} SceneRevision belongs to another document')

    def _assert_preset_binding(
        self, preset_id: str, preset_sha256: str, document_id: str
    ) -> None:
        preset = self.get_preset(preset_id)
        if preset is None:
            raise ValueError('referenced preset is not persisted')
        if preset.preset_sha256 != preset_sha256:
            raise ValueError('bound preset revision does not match the persisted preset')
        if preset.document_id != document_id:
            raise ValueError('bound preset belongs to another document')

    def _assert_resolves(
        self,
        resolver_kind: str,
        ref_id: str,
        ref_sha256: str | None,
        document_id: str,
    ) -> None:
        resolved = self.ref_resolver.resolve(resolver_kind, ref_id, document_id)
        if resolved is None:
            raise ValueError(
                f'references a {resolver_kind} authority that does not resolve'
            )
        if (
            resolved.document_id is not None
            and resolved.document_id != document_id
        ):
            raise ValueError(
                f'{resolver_kind} authority belongs to another document'
            )
        if resolved.semantic_sha256 is not None:
            if ref_sha256 is None:
                raise ValueError(
                    f'ref must pin the {resolver_kind} semantic hash to claim '
                    'an exact reference'
                )
            if ref_sha256 != resolved.semantic_sha256:
                raise ValueError(
                    f'{resolver_kind} hash does not match the canonical authority'
                )
        elif ref_sha256 is not None:
            raise ValueError(
                f'ref supplies a hash the id-only {resolver_kind} authority '
                'does not expose'
            )
