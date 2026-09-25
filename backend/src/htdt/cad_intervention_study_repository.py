"""Append-only SQLite persistence for intervention-study authority (#519)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Literal

from .cad_intervention_study import (
    InterventionAlternative,
    InterventionAuthorityRef,
    InterventionStudySpec,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables
from .cad_system_variant_repository import CadSystemVariantRepository


# Typed authority kind -> (table, id column, semantic sha column). Every
# entry stores immutable rows whose semantic digest column pins the exact
# persisted authority (#960); resolution is existence + hash equality.
_AUTHORITY_TABLES: dict[str, tuple[str, str, str]] = {
    'adaptive_plan': ('cad_adaptive_plans', 'plan_id', 'adaptive_sha256'),
    'applied_settings': ('cad_applied_settings', 'applied_id', 'applied_sha256'),
    'acoustic_prediction_request': (
        'cad_acoustic_prediction_requests',
        'request_id',
        'request_semantic_sha256',
    ),
    'acoustic_scene_snapshot': (
        'cad_acoustic_scene_snapshots',
        'snapshot_id',
        'semantic_sha256',
    ),
    'acoustic_solver_result': (
        'cad_acoustic_solver_results',
        'result_id',
        'semantic_sha256',
    ),
    'acoustic_treatment_definition': (
        'cad_acoustic_treatment_definitions',
        'definition_id',
        'definition_sha256',
    ),
    'acoustic_treatment_placement': (
        'cad_acoustic_treatment_placements',
        'instance_id',
        'placement_sha256',
    ),
    'calibration_export': (
        'cad_calibration_exports',
        'export_id',
        'exported_settings_semantic_sha256',
    ),
    'calibration_plan': (
        'cad_calibration_plans',
        'plan_id',
        'plan_semantic_sha256',
    ),
    'coverage_evaluation': (
        'cad_coverage_evaluations',
        'evaluation_id',
        'evaluation_sha256',
    ),
    'direct_level_evaluation': (
        'cad_direct_level_evaluations',
        'evaluation_id',
        'evaluation_sha256',
    ),
    'extended_search_spec': (
        'cad_extended_search_specs',
        'extended_search_id',
        'extended_search_sha256',
    ),
    'joint_candidate': ('cad_joint_candidates', 'candidate_id', 'candidate_sha256'),
    'joint_candidate_evaluation': (
        'cad_joint_candidate_evaluations',
        'evaluation_binding_id',
        'evaluation_binding_sha256',
    ),
    'joint_candidate_selection': (
        'cad_joint_candidate_selections',
        'selection_id',
        'selection_sha256',
    ),
    'joint_optimization_spec': (
        'cad_joint_optimization_specs',
        'spec_id',
        'semantic_sha256',
    ),
    'model_validation': (
        'cad_model_validations',
        'validation_id',
        'validation_sha256',
    ),
    'objective_evaluation': (
        'cad_objective_evaluations',
        'evaluation_id',
        'evaluation_sha256',
    ),
    'pareto_set': ('cad_pareto_sets', 'pareto_set_id', 'pareto_sha256'),
    'prediction_result': ('cad_prediction_results', 'prediction_id', 'result_sha256'),
    'roomsim_batch_spec': (
        'cad_roomsim_batch_specs',
        'batch_run_id',
        'batch_spec_sha256',
    ),
    'search_spec': ('cad_search_specs', 'search_spec_id', 'search_spec_sha256'),
    'system_variant': ('cad_system_variants', 'variant_id', 'variant_sha256'),
    'topology_candidate': (
        'cad_topology_placement_candidates',
        'candidate_id',
        'candidate_sha256',
    ),
    'topology_search_spec': (
        'cad_topology_search_specs',
        'search_id',
        'search_sha256',
    ),
    'treatment_boundary_composition': (
        'cad_treatment_boundary_compositions',
        'composition_id',
        'composition_hash_sha256',
    ),
    'treatment_boundary_overlay': (
        'cad_treatment_boundary_overlays',
        'overlay_id',
        'overlay_hash_sha256',
    ),
    'treatment_comparison': (
        'cad_acoustic_treatment_comparisons',
        'comparison_id',
        'comparison_sha256',
    ),
    'treatment_evidence_authority': (
        'cad_treatment_evidence_authorities',
        'evidence_id',
        'evidence_sha256',
    ),
    'validation_campaign': (
        'cad_validation_campaigns',
        'campaign_id',
        'campaign_sha256',
    ),
}

# Finding source kind -> the one legal authority kind it may bind.
_FINDING_SOURCE_KINDS: dict[str, str] = {
    'prediction_result': 'prediction_result',
    'coverage_evaluation': 'coverage_evaluation',
    'direct_level_evaluation': 'direct_level_evaluation',
    'measurement': 'measurement',
}

# Measurement-scoped seals that may pin a 'measurement' authority: the
# cad_measurements row itself carries no semantic sha, so the pinned digest
# must be one of its sealed artifacts (dataset / disposition / quality
# report / observation / lineage).
_MEASUREMENT_SEAL_QUERIES = (
    (
        'SELECT 1 FROM cad_frequency_responses '
        'WHERE measurement_id = ? AND dataset_sha256 = ?'
    ),
    (
        'SELECT 1 FROM cad_measurement_dispositions '
        'WHERE measurement_id = ? AND disposition_sha256 = ?'
    ),
    (
        'SELECT 1 FROM cad_measurement_quality_reports '
        'WHERE measurement_id = ? AND report_sha256 = ?'
    ),
    (
        'SELECT 1 FROM cad_measurement_observations '
        'WHERE measurement_id = ? AND observation_sha256 = ?'
    ),
    (
        'SELECT 1 FROM cad_measurement_lineage '
        'WHERE measurement_id = ? AND lineage_sha256 = ?'
    ),
)


class CadInterventionStudyRepository:
    """Persist ``InterventionStudySpec`` + ``InterventionAlternative`` rows.

    Writes and reads re-establish the authority chain: a spec must bind a
    resolvable SceneRevision and a SystemVariant bound to that revision; an
    alternative must bind a persisted spec row. Persisted payloads are
    re-validated through the immutable models on every authoritative read, so
    tampered rows fail closed instead of being trusted.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.db_path = Path(scene_repository.path)
        if Path(self.variant_repository.path) != self.db_path:
            raise ValueError(
                'intervention study and variant repositories must share '
                'one native CAD database'
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.db_path)
        self._initialize()

    @property
    def path(self) -> Path:
        return self.db_path

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.db_path)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_intervention_study_specs',
                'cad_intervention_alternatives',
            )

    def _resolve_authority_ref(
        self,
        connection: sqlite3.Connection,
        ref: InterventionAuthorityRef,
        *,
        purpose: str,
    ) -> None:
        """Resolve a typed authority pin against its canonical table.

        Unknown kinds and absent/mismatched digests fail closed — a
        caller-supplied id is never trusted (#960).
        """
        if ref.authority_kind == 'measurement':
            if self._measurement_ref_resolves(connection, ref):
                return
            raise ValueError(
                f'{purpose} authority measurement/{ref.authority_id} is not '
                'registered for replay'
            )
        table = _AUTHORITY_TABLES.get(ref.authority_kind)
        if table is None:
            raise ValueError(
                f'{purpose} authority kind {ref.authority_kind!r} has no '
                'registered resolver'
            )
        name, id_col, sha_col = table
        row = connection.execute(
            f'SELECT 1 FROM {name} WHERE {id_col} = ? AND {sha_col} = ?',
            (ref.authority_id, ref.authority_sha256),
        ).fetchone()
        if row is None:
            raise ValueError(
                f'{purpose} authority {ref.authority_kind}/'
                f'{ref.authority_id} is not registered for replay'
            )

    def _measurement_ref_resolves(
        self,
        connection: sqlite3.Connection,
        ref: InterventionAuthorityRef,
    ) -> bool:
        row = connection.execute(
            'SELECT 1 FROM cad_measurements WHERE measurement_id = ?',
            (ref.authority_id,),
        ).fetchone()
        if row is None:
            return False
        return any(
            connection.execute(
                sql, (ref.authority_id, ref.authority_sha256)
            ).fetchone()
            is not None
            for sql in _MEASUREMENT_SEAL_QUERIES
        )

    def _resolve_untyped_authority(
        self,
        connection: sqlite3.Connection,
        authority_id: str,
        authority_sha256: str,
        *,
        purpose: str,
    ) -> None:
        """Resolve a legacy id+sha pair against every registered table."""
        for name, id_col, sha_col in _AUTHORITY_TABLES.values():
            row = connection.execute(
                f'SELECT 1 FROM {name} WHERE {id_col} = ? AND {sha_col} = ?',
                (authority_id, authority_sha256),
            ).fetchone()
            if row is not None:
                return
        if self._measurement_ref_resolves(
            connection,
            InterventionAuthorityRef(
                authority_kind='measurement',
                authority_id=authority_id,
                authority_sha256=authority_sha256,
            ),
        ):
            return
        raise ValueError(
            f'{purpose} authority {authority_id} is not registered for replay'
        )

    def _require_spec_dependencies(
        self, spec: InterventionStudySpec
    ) -> None:
        """Resolve every authority the spec is caller-allowed to pin (#960)."""
        finding = spec.finding
        kind = _FINDING_SOURCE_KINDS.get(finding.source_kind)
        with closing(self._connect()) as connection:
            if kind is not None:
                self._resolve_authority_ref(
                    connection,
                    InterventionAuthorityRef(
                        authority_kind=kind,
                        authority_id=finding.source_authority_id or '',
                        authority_sha256=(
                            finding.source_authority_sha256 or '0' * 64
                        ),
                    ),
                    purpose='finding source',
                )
            if (
                spec.treatment_capability_authority_id is not None
                and spec.treatment_capability_authority_sha256 is not None
            ):
                self._resolve_untyped_authority(
                    connection,
                    spec.treatment_capability_authority_id,
                    spec.treatment_capability_authority_sha256,
                    purpose='treatment capability',
                )

    def _require_alternative_dependencies(
        self, alternative: InterventionAlternative
    ) -> None:
        """Resolve generated/evidence/metric-producer authority pins (#960)."""
        with closing(self._connect()) as connection:
            for ref in alternative.generated_authorities:
                self._resolve_authority_ref(
                    connection, ref, purpose='generated'
                )
            for ref in alternative.evidence_authorities:
                self._resolve_authority_ref(
                    connection, ref, purpose='evidence'
                )
            for metric in alternative.metrics:
                if metric.producer is not None:
                    self._resolve_authority_ref(
                        connection,
                        metric.producer,
                        purpose='metric producer',
                    )


    def _require_scene_revision(
        self, spec: InterventionStudySpec
    ) -> None:
        revision = self.scene_repository.get(spec.scene_revision_id)
        if (
            revision is None
            or revision.document_id != spec.document_id
            or revision.content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'intervention study SceneRevision is not registered '
                'for replay'
            )
        variant = self.variant_repository.get_variant(
            spec.base_system_variant_id
        )
        if (
            variant is None
            or variant.variant_sha256 != spec.base_system_variant_sha256
            or variant.baseline_revision_id != spec.scene_revision_id
            or variant.baseline_content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'intervention study base SystemVariant is not registered '
                'for replay'
            )

    def save_spec(self, spec: InterventionStudySpec) -> InterventionStudySpec:
        self._require_scene_revision(spec)
        self._require_spec_dependencies(spec)
        payload = spec.model_dump(mode='json')
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_intervention_study_specs '
                'WHERE spec_id = ?',
                (spec.spec_id,),
            ).fetchone()
            if existing is not None:
                if existing['payload_json'] != payload and (
                    InterventionStudySpec.model_validate_json(
                        existing['payload_json']
                    )
                    != spec
                ):
                    raise ValueError(
                        'conflicting intervention study spec for this ID'
                    )
                return spec
            connection.execute(
                'INSERT INTO cad_intervention_study_specs ('
                '    spec_id, document_id, scene_revision_id, '
                '    scene_content_hash, payload_json, spec_sha256, '
                '    created_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    spec.spec_id,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.scene_content_hash,
                    spec.model_dump_json(),
                    spec.spec_sha256,
                    spec.created_at_utc,
                ),
            )
        return spec

    def get_spec(self, spec_id: str) -> InterventionStudySpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_intervention_study_specs WHERE spec_id = ?',
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validated_spec_row(row)

    def list_specs(
        self,
        document_id: str,
        *,
        scene_revision_id: str | None = None,
    ) -> list[InterventionStudySpec]:
        sql = (
            'SELECT * FROM cad_intervention_study_specs WHERE document_id = ?'
        )
        args: list[str] = [document_id]
        if scene_revision_id is not None:
            sql += ' AND scene_revision_id = ?'
            args.append(scene_revision_id)
        sql += ' ORDER BY created_at_utc, spec_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._validated_spec_row(row) for row in rows]

    def _validated_spec_row(self, row: sqlite3.Row) -> InterventionStudySpec:
        payload = row['payload_json']
        spec = InterventionStudySpec.model_validate_json(payload)
        if (
            spec.spec_id != row['spec_id']
            or spec.spec_sha256 != row['spec_sha256']
            or spec.document_id != row['document_id']
            or spec.scene_revision_id != row['scene_revision_id']
            or spec.scene_content_hash != row['scene_content_hash']
            or spec.created_at_utc != row['created_at_utc']
        ):
            raise ValueError(
                'persisted intervention study spec authority mismatch'
            )
        self._require_scene_revision(spec)
        self._require_spec_dependencies(spec)
        return spec

    def save_alternative(
        self, alternative: InterventionAlternative
    ) -> InterventionAlternative:
        spec = self.get_spec(alternative.study_spec_id)
        if spec is None or spec.spec_sha256 != alternative.study_spec_sha256:
            raise ValueError(
                'intervention alternative study spec is not registered '
                'for replay'
            )
        self._require_alternative_dependencies(alternative)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT payload_json FROM cad_intervention_alternatives '
                'WHERE alternative_id = ?',
                (alternative.alternative_id,),
            ).fetchone()
            if existing is not None:
                if existing['payload_json'] != alternative.model_dump_json() and (
                    InterventionAlternative.model_validate_json(
                        existing['payload_json']
                    )
                    != alternative
                ):
                    raise ValueError(
                        'conflicting intervention alternative for this ID'
                    )
                return alternative
            connection.execute(
                'INSERT INTO cad_intervention_alternatives ('
                '    alternative_id, spec_id, family, payload_json, '
                '    alternative_sha256, created_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?)',
                (
                    alternative.alternative_id,
                    spec.spec_id,
                    alternative.family,
                    alternative.model_dump_json(),
                    alternative.alternative_sha256,
                    spec.created_at_utc,
                ),
            )
        return alternative

    def list_alternatives(
        self, spec_id: str
    ) -> list[InterventionAlternative]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_intervention_alternatives '
                'WHERE spec_id = ? ORDER BY created_at_utc, alternative_id',
                (spec_id,),
            ).fetchall()
        results: list[InterventionAlternative] = []
        for row in rows:
            alternative = InterventionAlternative.model_validate_json(
                row['payload_json']
            )
            if (
                alternative.alternative_id != row['alternative_id']
                or alternative.alternative_sha256 != row['alternative_sha256']
                or alternative.study_spec_id != row['spec_id']
                or alternative.family != row['family']
            ):
                raise ValueError(
                    'persisted intervention alternative authority mismatch'
                )
            # Re-resolve every pinned authority on read: stale or tampered
            # dependencies fail closed rather than being trusted (#960).
            self._require_alternative_dependencies(alternative)
            results.append(alternative)
        return results


ReviewState = Literal['pending', 'reviewed', 'applied', 'discarded']

__all__ = [
    'CadInterventionStudyRepository',
    'ReviewState',
]
