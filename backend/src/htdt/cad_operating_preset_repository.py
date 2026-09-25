"""Append-only persistence for theater operating presets (#567).

Reference validation landed in #743: ``save_preset`` resolves every
``PresetComponentRef`` against the canonical repository for its kind and
``save_measurement_binding`` verifies bound measurements exist, belong to the
preset's document, match the pinned SceneRevision, and carry an eligible
disposition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import TYPE_CHECKING

from .cad_operating_preset import (
    AppliedPresetState,
    PresetComponentRef,
    PresetMeasurementBinding,
    TheaterOperatingPreset,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cad_authority_refs import AuthorityRefResolver
    from .cad_measurement_models import CadMeasurementRecord
    from .cad_measurement_quality_repository import CadMeasurementQualityRepository
    from .cad_measurement_repository import CadMeasurementRepository


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


@dataclass(frozen=True, slots=True)
class PresetComponentResolution:
    """What one ``PresetComponentRef`` resolved to in a canonical repository.

    ``resolved_id`` is the resolved authority's own identity field — a
    resolution that found a *different* authority than ``ref_id`` names is a
    mismatch, not a success. ``document_id`` / ``scene_revision_id`` /
    ``semantic_sha256`` are the compatibility facets the authority exposes;
    ``None`` means the authority does not carry that facet and no check on it
    applies.
    """

    resolved_id: str
    document_id: str | None = None
    scene_revision_id: str | None = None
    semantic_sha256: str | None = None


PresetComponentResolver = Callable[[PresetComponentRef], 'PresetComponentResolution | None']


def default_component_resolvers(
    scene_repository: SceneRepository,
) -> dict[str, PresetComponentResolver]:
    """Canonical resolvers wiring preset component kinds to their repositories.

    Kinds with no canonical backend authority yet — ``system_topology``,
    ``presentation_profile``, ``photometric_state``, ``playback_chain``,
    ``listening_population``, ``other`` — get no resolver: such refs must set
    ``external_dependency=True`` or be rejected at save.
    """
    from .cad_av_sync_repository import CadAVSyncRepository
    from .cad_calibration_repository import CadCalibrationRepository
    from .cad_calibration_workflow_repository import CadAppliedSettingsRepository
    from .cad_direct_level_repository import CadDirectLevelRepository
    from .cad_equipment_repository import CadEquipmentRepository
    from .cad_measurement_quality_repository import CadMeasurementQualityRepository
    from .cad_measurement_repository import CadMeasurementRepository
    from .cad_room_operating_state_repository import CadRoomOperatingStateRepository
    from .cad_system_variant_repository import CadSystemVariantRepository
    from .cad_target_profile_repository import CadTargetProfileRepository

    variants = CadSystemVariantRepository(scene_repository)
    measurements = CadMeasurementRepository(scene_repository)
    measurement_quality = CadMeasurementQualityRepository(measurements)
    calibrations = CadCalibrationRepository(
        scene_repository=scene_repository,
        system_variant_repository=variants,
        measurement_repository=measurements,
        quality_repository=measurement_quality,
    )
    applied_settings = CadAppliedSettingsRepository(scene_repository)
    av_sync = CadAVSyncRepository(scene_repository)
    direct_levels = CadDirectLevelRepository(
        scene_repository, variants, CadEquipmentRepository(scene_repository)
    )
    operating_states = CadRoomOperatingStateRepository(scene_repository)
    target_profiles = CadTargetProfileRepository(
        scene_repository, calibration_repository=calibrations
    )

    def system_variant(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = variants.get_variant(ref.ref_id)
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.variant_id,
            document_id=item.document_id,
            scene_revision_id=item.baseline_revision_id,
            semantic_sha256=item.variant_sha256,
        )

    def excitation_scenario(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = direct_levels.get_scenario(ref.ref_id)
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.scenario_id,
            semantic_sha256=item.scenario_sha256,
        )

    def routing_profile(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = measurement_quality.get_routing_profile(ref.ref_id)
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.routing_profile_id,
            semantic_sha256=item.routing_profile_sha256,
        )

    def calibration_plan(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = calibrations.get_plan(ref.ref_id)
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.plan_id,
            document_id=item.document_id,
            scene_revision_id=item.scene_revision_id,
            semantic_sha256=item.plan_semantic_sha256,
        )

    def calibration_export(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = calibrations.get_export(ref.ref_id)
        if item is None:
            return None
        # The export carries no document_id of its own; scope compat flows
        # through the calibration plan it was emitted from.
        plan = calibrations.get_plan(item.calibration_plan_id)
        return PresetComponentResolution(
            resolved_id=item.export_id,
            document_id=plan.document_id if plan is not None else None,
            scene_revision_id=plan.scene_revision_id if plan is not None else None,
            semantic_sha256=item.exported_settings_semantic_sha256,
        )

    def applied_settings_ref(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = applied_settings.get_applied(ref.ref_id)
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.applied_id,
            document_id=item.document_id,
            semantic_sha256=item.applied_sha256,
        )

    def target_curve(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = (
            target_profiles.get_profile_by_hash(ref.ref_sha256)
            if ref.ref_sha256 is not None
            else None
        )
        if item is None:
            versions = target_profiles.list_profile_versions(ref.ref_id)
            item = versions[-1] if versions else None
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.profile_id,
            document_id=item.document_id,
            semantic_sha256=item.semantic_sha256,
        )

    def av_sync_condition(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = av_sync.get_condition(ref.ref_id)
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.condition_id,
            document_id=item.document_id,
            scene_revision_id=item.scene_revision_id,
            semantic_sha256=item.condition_sha256,
        )

    def room_operating_state(ref: PresetComponentRef) -> PresetComponentResolution | None:
        item = (
            operating_states.get_state_by_hash(ref.ref_sha256)
            if ref.ref_sha256 is not None
            else None
        )
        if item is None:
            versions = operating_states.list_state_versions(ref.ref_id)
            item = versions[-1] if versions else None
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.state_id,
            document_id=item.document_id,
            scene_revision_id=item.scene_revision_id,
            semantic_sha256=item.semantic_sha256,
        )

    def playback_level_condition(ref: PresetComponentRef) -> PresetComponentResolution | None:
        from .cad_playback_level_repository import CadPlaybackLevelRepository

        levels = CadPlaybackLevelRepository(scene_repository)
        item = (
            levels.get_condition_by_hash(ref.ref_sha256)
            if ref.ref_sha256 is not None
            else levels.get_condition(ref.ref_id)
        )
        if item is None:
            return None
        return PresetComponentResolution(
            resolved_id=item.condition_id,
            document_id=item.document_id,
            scene_revision_id=item.scene_revision_id,
            semantic_sha256=item.condition_sha256,
        )

    return {
        'system_variant': system_variant,
        'excitation_scenario': excitation_scenario,
        'routing_profile': routing_profile,
        'calibration_plan': calibration_plan,
        'calibration_export': calibration_export,
        'applied_settings': applied_settings_ref,
        'target_curve': target_curve,
        'av_sync_condition': av_sync_condition,
        'room_operating_state': room_operating_state,
        'playback_level_condition': playback_level_condition,
    }


def _parse_iso8601(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class CadOperatingPresetRepository:
    """Native storage for TheaterOperatingPreset + applied/measured bindings.

    Presets are immutable: changing what "Movie" means creates a new preset,
    never an UPDATE. Applied-state confirmations and measurement bindings
    are append-only facts that reference the exact ``preset_sha256``.

    ``component_resolvers`` maps ``PresetComponentKind`` → resolver and
    defaults to :func:`default_component_resolvers`; tests may inject a
    narrower mapping. ``measurement_repository`` /
    ``measurement_quality_repository`` may be injected to share existing
    instances; otherwise they are built lazily on first binding save.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        component_resolvers: Mapping[str, PresetComponentResolver] | None = None,
        measurement_repository: 'CadMeasurementRepository | None' = None,
        measurement_quality_repository: 'CadMeasurementQualityRepository | None' = None,
        ref_resolver: 'AuthorityRefResolver | None' = None,
    ) -> None:
        self.scene_repository = scene_repository
        if ref_resolver is None:
            from .cad_authority_refs import CanonicalAuthorityRefResolver

            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver
        self.path = scene_repository.path
        self._component_resolvers = (
            dict(component_resolvers) if component_resolvers is not None else None
        )
        self._measurement_repository = measurement_repository
        self._measurement_quality_repository = measurement_quality_repository
        self._initialize()

    def _resolvers(self) -> dict[str, PresetComponentResolver]:
        if self._component_resolvers is None:
            self._component_resolvers = default_component_resolvers(
                self.scene_repository
            )
        return self._component_resolvers

    def _measurement_repositories(
        self,
    ) -> tuple['CadMeasurementRepository', 'CadMeasurementQualityRepository']:
        if self._measurement_repository is None:
            from .cad_measurement_repository import CadMeasurementRepository

            self._measurement_repository = CadMeasurementRepository(
                self.scene_repository
            )
        if self._measurement_quality_repository is None:
            from .cad_measurement_quality_repository import (
                CadMeasurementQualityRepository,
            )

            self._measurement_quality_repository = CadMeasurementQualityRepository(
                self._measurement_repository
            )
        return self._measurement_repository, self._measurement_quality_repository

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_operating_presets',
                'cad_applied_preset_states',
                'cad_preset_measurement_bindings',
            )


    def _validate_component_refs(self, preset: TheaterOperatingPreset) -> None:
        resolvers = self._resolvers()
        for ref in preset.component_refs:
            if ref.external_dependency:
                continue
            resolver = resolvers.get(ref.kind)
            if resolver is None:
                raise ValueError(
                    f'preset component {ref.kind}:{ref.ref_id} has no canonical '
                    'authority resolver and is not marked external_dependency'
                )
            resolved = resolver(ref)
            if resolved is None:
                raise ValueError(
                    f'preset component {ref.kind}:{ref.ref_id} does not resolve '
                    'to a persisted authority'
                )
            if resolved.resolved_id != ref.ref_id:
                raise ValueError(
                    f'preset component {ref.kind}:{ref.ref_id} resolves to a '
                    f'different authority ({resolved.resolved_id})'
                )
            if (
                resolved.semantic_sha256 is not None
                and ref.ref_sha256 != resolved.semantic_sha256
            ):
                raise ValueError(
                    f'preset component {ref.kind}:{ref.ref_id} must pin the '
                    'authority semantic hash via ref_sha256'
                )
            if (
                resolved.document_id is not None
                and resolved.document_id != preset.document_id
            ):
                raise ValueError(
                    f'preset component {ref.kind}:{ref.ref_id} belongs to '
                    'another document'
                )
            if (
                resolved.scene_revision_id is not None
                and resolved.scene_revision_id != preset.scene_revision_id
            ):
                raise ValueError(
                    f'preset component {ref.kind}:{ref.ref_id} is bound to a '
                    'different SceneRevision'
                )

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
        self._validate_component_refs(preset)
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

    def _validate_measurement(
        self,
        binding: PresetMeasurementBinding,
        preset: TheaterOperatingPreset,
        measurement: 'CadMeasurementRecord',
        pinned_sha256: str | None,
    ) -> None:
        from .cad_measurement_disposition import MEASUREMENT_ELIGIBLE_DISPOSITIONS
        from .cad_measurement_quality import measurement_sha256

        if measurement.document_id != binding.document_id:
            raise ValueError(
                'preset measurement binding references a measurement from '
                'another document'
            )
        if measurement.scene_revision_id != preset.scene_revision_id:
            raise ValueError(
                'preset measurement binding references a measurement captured '
                'on a different SceneRevision'
            )
        if pinned_sha256 is not None and pinned_sha256 != measurement_sha256(
            measurement
        ):
            raise ValueError(
                'preset measurement binding measurement hash mismatch'
            )
        _, quality = self._measurement_repositories()
        disposition = quality.latest_disposition(measurement.measurement_id)
        if (
            disposition is not None
            and disposition.disposition not in MEASUREMENT_ELIGIBLE_DISPOSITIONS
        ):
            raise ValueError(
                f'measurement {measurement.measurement_id} is not eligible '
                'preset evidence (disposition '
                f'{disposition.disposition})'
            )
        captured_at = measurement.captured_at or measurement.imported_at
        if (
            not binding.historical_attestation
            and _parse_iso8601(captured_at) < _parse_iso8601(preset.created_at_utc)
        ):
            raise ValueError(
                'preset measurement binding attests a measurement captured '
                'before the preset; set historical_attestation to allow this '
                'explicitly'
            )

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
        measurements, _ = self._measurement_repositories()
        shas = binding.measurement_sha256s
        for index, measurement_id in enumerate(binding.measurement_ids):
            measurement = measurements.get_measurement(measurement_id)
            if measurement is None:
                raise ValueError(
                    'preset measurement binding references unknown measurement '
                    f'{measurement_id}'
                )
            self._validate_measurement(
                binding,
                preset,
                measurement,
                shas[index] if shas is not None else None,
            )
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
