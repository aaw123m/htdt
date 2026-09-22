from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import NamedTuple

from .cad_calibration import CadCalibrationPlan
from .cad_calibration_repository import CadCalibrationRepository
from .cad_extended_search import CadExtendedSearchSpec
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_joint_optimization import (
    JointCandidate,
    JointCandidateEvaluationBinding,
    JointCandidateSelection,
    JointOptimizationSpec,
    build_joint_candidate,
    build_joint_optimization_spec,
    device_capability_sha256,
    physical_variables_from_authority,
    require_joint_decision_materialization,
)
from .cad_measurement_quality import CadMeasurementQualityReport
from .cad_repository import SceneRepository, SceneRevision
from .cad_robustness_repository import CadRobustnessRepository
from .cad_schema import ensure_native_schema
from .cad_search_models import CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .cad_system_variant import SystemVariant
from .cad_system_variant_repository import CadSystemVariantRepository
from .optimization_robustness import RobustnessAxisParameter


class _ResolvedSpecAuthorities(NamedTuple):
    """Exact persisted authorities resolved by JointOptimizationSpec replay."""

    scene_revision: SceneRevision
    base_system_variant: SystemVariant
    physical_search_spec: CadSearchSpec
    extended_search_spec: CadExtendedSearchSpec | None
    base_calibration_plan: CadCalibrationPlan | None
    measurement_quality_report: CadMeasurementQualityReport | None


_O90_AXIS_PARAMETERS: dict[str, frozenset[RobustnessAxisParameter]] = {
    'x_m': frozenset({'speaker_x_m', 'listener_x_m'}),
    'y_m': frozenset({'speaker_y_m', 'listener_y_m'}),
    'z_m': frozenset({'speaker_z_m', 'listener_z_m'}),
    'aim_yaw_deg': frozenset({'aim_yaw_deg'}),
    'body_yaw_deg': frozenset({'body_yaw_deg'}),
}


class CadJointOptimizationRepository:
    """Append-only Issue #174 orchestration authority.

    Selection is deliberately non-applying: no SceneRevision mutation and no
    CalibrationPlan export/lifecycle transition occurs in this repository.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        system_variant_repository: CadSystemVariantRepository,
        calibration_repository: CadCalibrationRepository,
        search_repository: CadSearchRepository,
        extended_search_repository: CadExtendedSearchRepository,
        robustness_repository: CadRobustnessRepository,
    ) -> None:
        paths = {
            Path(scene_repository.path),
            Path(system_variant_repository.path),
            Path(calibration_repository.path),
            Path(search_repository.path),
            Path(extended_search_repository.path),
            Path(robustness_repository.db_path),
        }
        if len(paths) != 1:
            raise ValueError(
                'joint optimization authorities must share one native repository'
            )
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.calibration_repository = calibration_repository
        self.search_repository = search_repository
        self.extended_search_repository = extended_search_repository
        self.robustness_repository = robustness_repository
        self.path = paths.pop()
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        ensure_native_schema(self.path)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_joint_optimization_specs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    spec_id TEXT NOT NULL UNIQUE,
                    semantic_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    base_system_variant_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_joint_opt_spec_document_seq
                    ON cad_joint_optimization_specs(document_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_joint_candidates (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL UNIQUE,
                    candidate_sha256 TEXT NOT NULL UNIQUE,
                    spec_id TEXT NOT NULL,
                    physical_system_variant_id TEXT NOT NULL,
                    calibration_plan_id TEXT,
                    candidate_class TEXT NOT NULL,
                    eligibility_state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(spec_id)
                        REFERENCES cad_joint_optimization_specs(spec_id)
                );
                CREATE INDEX IF NOT EXISTS idx_joint_candidate_spec_seq
                    ON cad_joint_candidates(spec_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_joint_candidate_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_binding_id TEXT NOT NULL UNIQUE,
                    evaluation_binding_sha256 TEXT NOT NULL UNIQUE,
                    spec_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(spec_id)
                        REFERENCES cad_joint_optimization_specs(spec_id),
                    FOREIGN KEY(candidate_id)
                        REFERENCES cad_joint_candidates(candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_joint_evaluation_spec_seq
                    ON cad_joint_candidate_evaluations(spec_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_joint_candidate_selections (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    selection_id TEXT NOT NULL UNIQUE,
                    selection_sha256 TEXT NOT NULL UNIQUE,
                    spec_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    evaluation_binding_id TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(spec_id)
                        REFERENCES cad_joint_optimization_specs(spec_id),
                    FOREIGN KEY(candidate_id)
                        REFERENCES cad_joint_candidates(candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_joint_selection_spec_seq
                    ON cad_joint_candidate_selections(spec_id, seq ASC);
                """
            )

    def _require_spec_authority(
        self,
        spec: JointOptimizationSpec,
    ) -> _ResolvedSpecAuthorities:
        """Recompile the declared JointOptimizationSpec from exact authorities.

        Resolves the baseline SceneRevision, base SystemVariant, the exact base
        SearchSpec and declared Extended SearchSpec, the base CalibrationPlan
        (and its measurement-quality authority) when DSP search is enabled, and
        the O90 RobustnessSpec. The physical decision variables, DSP authority,
        and hard-constraint refs are regenerated from those exact authorities,
        each O90 variable mapping is validated against the real robustness axes,
        and the submitted record must equal the canonical compilation of its
        declared inputs. Dangling, mismatched, or non-canonical authority fails
        closed; used by both save-time validation and authoritative reads.

        Returns the resolved authorities so dependent records (e.g. joint
        candidates) can replay their own canonical semantics against the same
        exact objects without trusting any payload-embedded copies.
        """

        revision = self.scene_repository.get(spec.scene_revision_id)
        if revision is None:
            raise ValueError('JointOptimizationSpec references unknown SceneRevision')
        if (
            revision.document_id != spec.document_id
            or revision.content_hash != spec.scene_content_hash
        ):
            raise ValueError('JointOptimizationSpec SceneRevision authority mismatch')

        variant = self.system_variant_repository.get_variant(
            spec.base_system_variant_id
        )
        if variant is None:
            raise ValueError('JointOptimizationSpec references unknown base SystemVariant')
        if variant.variant_sha256 != spec.base_system_variant_sha256:
            raise ValueError('JointOptimizationSpec base SystemVariant hash mismatch')
        if (
            variant.document_id != spec.document_id
            or variant.baseline_revision_id != spec.scene_revision_id
            or variant.baseline_content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'JointOptimizationSpec base SystemVariant baseline authority mismatch'
            )

        search_spec = self.search_repository.get(spec.physical_search_spec_id)
        if search_spec is None:
            raise ValueError(
                'JointOptimizationSpec references unknown physical SearchSpec'
            )
        if search_spec.search_spec_sha256 != spec.physical_search_spec_sha256:
            raise ValueError(
                'JointOptimizationSpec physical SearchSpec hash mismatch'
            )
        if (
            search_spec.document_id != spec.document_id
            or search_spec.scene_revision_id != spec.scene_revision_id
            or search_spec.scene_content_hash != spec.scene_content_hash
        ):
            raise ValueError(
                'JointOptimizationSpec physical SearchSpec must bind the exact '
                'baseline SceneRevision'
            )

        extended_search_spec = None
        if spec.extended_search_spec_id is not None:
            extended_search_spec = self.extended_search_repository.get_spec(
                spec.extended_search_spec_id
            )
            if extended_search_spec is None:
                raise ValueError(
                    'JointOptimizationSpec references unknown extended SearchSpec'
                )
            if (
                extended_search_spec.extended_search_sha256
                != spec.extended_search_spec_sha256
            ):
                raise ValueError(
                    'JointOptimizationSpec extended SearchSpec hash mismatch'
                )
            if (
                extended_search_spec.document_id != spec.document_id
                or extended_search_spec.base_search_spec_id
                != spec.physical_search_spec_id
                or extended_search_spec.base_search_spec_sha256
                != spec.physical_search_spec_sha256
            ):
                raise ValueError(
                    'JointOptimizationSpec extended SearchSpec must bind the '
                    'exact base physical search'
                )

        if spec.physical_variables != physical_variables_from_authority(
            search_spec,
            extended_search_spec,
        ):
            raise ValueError(
                'JointOptimizationSpec physical variables are not the canonical '
                'derivation of the resolved search authorities'
            )

        plan = None
        quality_report = None
        if spec.dsp_authority is not None:
            plan = self.calibration_repository.get_plan(
                spec.dsp_authority.base_calibration_plan_id
            )
            if plan is None:
                raise ValueError(
                    'JointOptimizationSpec references unknown base CalibrationPlan'
                )
            if (
                plan.plan_semantic_sha256
                != spec.dsp_authority.base_calibration_plan_sha256
            ):
                raise ValueError(
                    'JointOptimizationSpec base CalibrationPlan hash mismatch'
                )
            if (
                plan.scene_revision_id != spec.scene_revision_id
                or plan.scene_content_hash != spec.scene_content_hash
                or plan.system_variant_id != spec.base_system_variant_id
                or plan.system_variant_sha256
                != spec.base_system_variant_sha256
            ):
                raise ValueError(
                    'JointOptimizationSpec base CalibrationPlan authority mismatch'
                )
            quality_report = self.calibration_repository.quality_repository.get_report(
                plan.measurement_quality_report_id
            )
            if (
                quality_report is None
                or quality_report.report_sha256
                != plan.measurement_quality_report_sha256
            ):
                raise ValueError(
                    'JointOptimizationSpec base CalibrationPlan quality authority '
                    'is not resolvable'
                )

        for ref in spec.hard_constraints:
            if ref.constraint_kind == 'physical_search_workspace':
                if (
                    ref.authority_id != search_spec.search_spec_id
                    or ref.authority_sha256 != search_spec.constraint_workspace_hash
                ):
                    raise ValueError(
                        'JointOptimizationSpec physical-search hard constraint '
                        'does not resolve to the base SearchSpec workspace'
                    )
            elif ref.constraint_kind == 'device_capability':
                if plan is None:
                    raise ValueError(
                        'JointOptimizationSpec device-capability hard constraint '
                        'requires DSP CalibrationPlan authority'
                    )
                capability = plan.device_constraints
                if (
                    ref.authority_id != capability.capability_id
                    or ref.authority_sha256 != device_capability_sha256(capability)
                ):
                    raise ValueError(
                        'JointOptimizationSpec device-capability hard constraint '
                        'does not resolve to the base CalibrationPlan capability'
                    )
            else:
                raise ValueError(
                    'JointOptimizationSpec external hard constraint has no '
                    'resolvable authority'
                )

        try:
            robustness_spec = self.robustness_repository.get_spec(
                spec.robustness.robustness_spec_id
            )
        except KeyError as exc:
            raise ValueError(
                'JointOptimizationSpec references unknown O90 RobustnessSpec'
            ) from exc
        if (
            robustness_spec.robustness_spec_sha256
            != spec.robustness.robustness_spec_sha256
        ):
            raise ValueError('JointOptimizationSpec O90 RobustnessSpec hash mismatch')
        if (
            robustness_spec.document_id != spec.document_id
            or robustness_spec.scene_revision_id != spec.scene_revision_id
            or robustness_spec.scene_content_hash != spec.scene_content_hash
            or robustness_spec.search_spec_id != spec.physical_search_spec_id
            or robustness_spec.search_spec_sha256
            != spec.physical_search_spec_sha256
        ):
            raise ValueError(
                'JointOptimizationSpec O90 RobustnessSpec must bind the exact '
                'baseline and physical search authority'
            )

        axes_by_id = {axis.axis_id: axis for axis in robustness_spec.axes}
        physical_by_id = {
            item.variable_id: item for item in spec.physical_variables
        }
        for mapping in spec.robustness.variable_mapping:
            variable = physical_by_id.get(mapping.joint_variable_id)
            if variable is None:
                raise ValueError(
                    'JointOptimizationSpec O90 mapping does not resolve to a '
                    'physical joint decision variable'
                )
            axis = axes_by_id.get(mapping.o90_axis_id)
            if axis is None:
                raise ValueError(
                    'JointOptimizationSpec O90 mapping references an unknown '
                    'robustness axis'
                )
            if axis.entity_id != variable.entity_id or axis.unit != variable.unit:
                raise ValueError(
                    'JointOptimizationSpec O90 axis does not match the joint '
                    'variable subject'
                )
            if axis.parameter not in _O90_AXIS_PARAMETERS[variable.parameter]:
                raise ValueError(
                    'JointOptimizationSpec O90 axis parameter is unrelated to '
                    'the joint variable'
                )

        expected = build_joint_optimization_spec(
            scene_revision=revision,
            base_system_variant=variant,
            physical_search_spec=search_spec,
            extended_search_spec=extended_search_spec,
            base_calibration_plan=plan,
            measurement_quality_report=quality_report,
            dsp_variables=spec.dsp_variables,
            objectives=spec.objectives,
            robustness=spec.robustness,
            evaluator=spec.evaluator,
            candidate_budget=spec.candidate_budget,
            created_at_utc=spec.created_at_utc,
            spec_id=spec.spec_id,
        )
        if expected != spec:
            raise ValueError(
                'JointOptimizationSpec is not the canonical compilation of its '
                'declared search, DSP, and constraint authorities'
            )
        return _ResolvedSpecAuthorities(
            scene_revision=revision,
            base_system_variant=variant,
            physical_search_spec=search_spec,
            extended_search_spec=extended_search_spec,
            base_calibration_plan=plan,
            measurement_quality_report=quality_report,
        )

    def _validated_spec_authorities(
        self,
        row: sqlite3.Row,
    ) -> tuple[JointOptimizationSpec, _ResolvedSpecAuthorities]:
        """Deserialize one persisted spec row and replay its exact authority."""

        spec = JointOptimizationSpec.model_validate_json(row['payload_json'])
        if (
            row['spec_id'] != spec.spec_id
            or row['semantic_sha256'] != spec.semantic_sha256
            or row['document_id'] != spec.document_id
            or row['scene_revision_id'] != spec.scene_revision_id
            or row['base_system_variant_id'] != spec.base_system_variant_id
        ):
            raise ValueError(
                'persisted JointOptimizationSpec row disagrees with its payload'
            )
        return spec, self._require_spec_authority(spec)

    def _validated_spec(self, row: sqlite3.Row) -> JointOptimizationSpec:
        spec, _authorities = self._validated_spec_authorities(row)
        return spec

    def save_spec(self, spec: JointOptimizationSpec) -> JointOptimizationSpec:
        spec = JointOptimizationSpec.model_validate(spec.model_dump(mode='python'))
        self._require_spec_authority(spec)
        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock so the duplicate recheck and
            # the insert are serialized: racing writers cannot both observe the
            # spec_id as absent.
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT *
                FROM cad_joint_optimization_specs
                WHERE spec_id=?
                """,
                (spec.spec_id,),
            ).fetchone()
            if existing is not None:
                persisted = self._validated_spec(existing)
                if persisted != spec:
                    raise ValueError(
                        'JointOptimizationSpec ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_joint_optimization_specs(
                    spec_id, semantic_sha256, document_id, scene_revision_id,
                    base_system_variant_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.spec_id,
                    spec.semantic_sha256,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.base_system_variant_id,
                    spec.model_dump_json(),
                ),
            )
        return spec

    def _spec_row(self, spec_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection, connection:
            return connection.execute(
                """
                SELECT *
                FROM cad_joint_optimization_specs
                WHERE spec_id=?
                """,
                (spec_id,),
            ).fetchone()

    def get_spec(self, spec_id: str) -> JointOptimizationSpec | None:
        row = self._spec_row(spec_id)
        return None if row is None else self._validated_spec(row)

    def list_specs(
        self,
        document_id: str,
    ) -> tuple[JointOptimizationSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_joint_optimization_specs
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._validated_spec(row) for row in rows)

    def _persisted_spec_for_candidate(
        self,
        candidate: JointCandidate,
    ) -> tuple[JointOptimizationSpec, _ResolvedSpecAuthorities]:
        row = self._spec_row(candidate.parent_spec_id)
        if row is None:
            raise ValueError('JointCandidate references unpersisted JointOptimizationSpec')
        spec, authorities = self._validated_spec_authorities(row)
        if spec.semantic_sha256 != candidate.parent_spec_sha256:
            raise ValueError('JointCandidate parent spec hash mismatch')
        if candidate.evaluator != spec.evaluator:
            raise ValueError('JointCandidate evaluator authority mismatch')
        return spec, authorities

    def _require_candidate_authority(
        self,
        candidate: JointCandidate,
    ) -> JointOptimizationSpec:
        spec, spec_authorities = self._persisted_spec_for_candidate(candidate)
        variant = self.system_variant_repository.get_variant(
            candidate.physical_system_variant_id
        )
        if variant is None:
            raise ValueError('JointCandidate references unknown physical SystemVariant')
        if variant.variant_sha256 != candidate.physical_system_variant_sha256:
            raise ValueError('JointCandidate physical SystemVariant hash mismatch')
        if (
            variant.document_id != spec.document_id
            or variant.baseline_revision_id != spec.scene_revision_id
            or variant.baseline_content_hash != spec.scene_content_hash
        ):
            raise ValueError('JointCandidate physical SystemVariant baseline mismatch')

        plan = None
        quality_report = None
        calibration = candidate.calibration_candidate
        if calibration is not None:
            plan = self.calibration_repository.get_plan(calibration.plan_id)
            if plan is None:
                raise ValueError('JointCandidate references unknown CalibrationPlan')
            if plan.plan_semantic_sha256 != calibration.plan_semantic_sha256:
                raise ValueError('JointCandidate CalibrationPlan hash mismatch')
            if plan.support_state != calibration.support_state:
                raise ValueError('JointCandidate CalibrationPlan support state mismatch')
            if (
                plan.system_variant_id != candidate.physical_system_variant_id
                or plan.system_variant_sha256
                != candidate.physical_system_variant_sha256
            ):
                raise ValueError(
                    'JointCandidate CalibrationPlan/physical SystemVariant mismatch'
                )
            if (
                plan.measurement_quality_report_id
                != calibration.measurement_quality_report_id
                or plan.measurement_quality_report_sha256
                != calibration.measurement_quality_report_sha256
            ):
                raise ValueError(
                    'JointCandidate CalibrationPlan quality authority mismatch'
                )
            quality_report = self.calibration_repository.quality_repository.get_report(
                plan.measurement_quality_report_id
            )
            if (
                quality_report is None
                or quality_report.report_sha256
                != plan.measurement_quality_report_sha256
            ):
                raise ValueError(
                    'JointCandidate CalibrationPlan quality authority is not '
                    'resolvable'
                )

        # Rebuild the canonical candidate from the exact resolved authorities:
        # decision-variable existence, declared bounds/step grid, candidate
        # class, base-vs-changed SystemVariant semantics, CalibrationPlan
        # measurement/device/routing authority, recomputed support and
        # capability gates, eligibility/blocked reasons, and production
        # eligibility must all match the submitted payload exactly.
        expected = build_joint_candidate(
            spec=spec,
            physical_system_variant=variant,
            decisions=candidate.decision_vector,
            calibration_plan=plan,
            measurement_quality_report=quality_report,
        )
        if expected != candidate:
            raise ValueError(
                'JointCandidate is not the canonical compilation of its '
                'declared decision vector and physical/DSP authorities'
            )
        require_joint_decision_materialization(
            spec=spec,
            baseline=spec_authorities.scene_revision,
            physical_system_variant=variant,
            calibration_plan=plan,
            decisions=candidate.decision_vector,
        )
        return spec

    def save_candidate(self, candidate: JointCandidate) -> JointCandidate:
        candidate = JointCandidate.model_validate(
            candidate.model_dump(mode='python')
        )
        spec = self._require_candidate_authority(candidate)
        calibration_plan_id = (
            None
            if candidate.calibration_candidate is None
            else candidate.calibration_candidate.plan_id
        )
        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock for the whole admission so
            # the duplicate recheck, persisted-budget count, and insert are
            # serialized: concurrent writers cannot both observe count < budget.
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidates
                WHERE candidate_id=?
                """,
                (candidate.candidate_id,),
            ).fetchone()
            if existing is not None:
                persisted = JointCandidate.model_validate_json(
                    existing['payload_json']
                )
                if persisted != candidate:
                    raise ValueError(
                        'JointCandidate ID already exists with different semantics'
                    )
                return persisted
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS candidate_count
                FROM cad_joint_candidates
                WHERE spec_id=?
                """,
                (candidate.parent_spec_id,),
            ).fetchone()
            assert count_row is not None
            if int(count_row['candidate_count']) >= spec.candidate_budget:
                raise ValueError(
                    'JointOptimizationSpec candidate budget is exhausted'
                )
            connection.execute(
                """
                INSERT INTO cad_joint_candidates(
                    candidate_id, candidate_sha256, spec_id,
                    physical_system_variant_id, calibration_plan_id,
                    candidate_class, eligibility_state, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.candidate_sha256,
                    candidate.parent_spec_id,
                    candidate.physical_system_variant_id,
                    calibration_plan_id,
                    candidate.candidate_class,
                    candidate.eligibility_state,
                    candidate.model_dump_json(),
                ),
            )
        return candidate

    def get_candidate(self, candidate_id: str) -> JointCandidate | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidates
                WHERE candidate_id=?
                """,
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        candidate = JointCandidate.model_validate_json(row['payload_json'])
        self._require_candidate_authority(candidate)
        return candidate

    def list_candidates(
        self,
        spec_id: str,
    ) -> tuple[JointCandidate, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidates
                WHERE spec_id=?
                ORDER BY seq ASC
                """,
                (spec_id,),
            ).fetchall()
        candidates = tuple(
            JointCandidate.model_validate_json(row['payload_json'])
            for row in rows
        )
        for candidate in candidates:
            self._require_candidate_authority(candidate)
        return candidates

    def _require_evaluation_authority(
        self,
        evaluation: JointCandidateEvaluationBinding,
    ) -> None:
        spec = self.get_spec(evaluation.parent_spec_id)
        if spec is None:
            raise ValueError(
                'joint evaluation references unpersisted JointOptimizationSpec'
            )
        if spec.semantic_sha256 != evaluation.parent_spec_sha256:
            raise ValueError('joint evaluation parent spec hash mismatch')
        if evaluation.evaluator != spec.evaluator:
            raise ValueError('joint evaluation evaluator authority mismatch')
        candidate = self.get_candidate(evaluation.candidate_id)
        if candidate is None:
            raise ValueError('joint evaluation references unpersisted JointCandidate')
        if candidate.candidate_sha256 != evaluation.candidate_sha256:
            raise ValueError('joint evaluation candidate hash mismatch')

    def save_evaluation(
        self,
        evaluation: JointCandidateEvaluationBinding,
    ) -> JointCandidateEvaluationBinding:
        evaluation = JointCandidateEvaluationBinding.model_validate(
            evaluation.model_dump(mode='python')
        )
        self._require_evaluation_authority(evaluation)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidate_evaluations
                WHERE evaluation_binding_id=?
                """,
                (evaluation.evaluation_binding_id,),
            ).fetchone()
            if existing is not None:
                persisted = JointCandidateEvaluationBinding.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'joint evaluation ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_joint_candidate_evaluations(
                    evaluation_binding_id, evaluation_binding_sha256,
                    spec_id, candidate_id, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_binding_id,
                    evaluation.evaluation_binding_sha256,
                    evaluation.parent_spec_id,
                    evaluation.candidate_id,
                    evaluation.model_dump_json(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_binding_id: str,
    ) -> JointCandidateEvaluationBinding | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidate_evaluations
                WHERE evaluation_binding_id=?
                """,
                (evaluation_binding_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = JointCandidateEvaluationBinding.model_validate_json(
            row['payload_json']
        )
        self._require_evaluation_authority(evaluation)
        return evaluation

    def list_evaluations(
        self,
        spec_id: str,
    ) -> tuple[JointCandidateEvaluationBinding, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidate_evaluations
                WHERE spec_id=?
                ORDER BY seq ASC
                """,
                (spec_id,),
            ).fetchall()
        evaluations = tuple(
            JointCandidateEvaluationBinding.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._require_evaluation_authority(evaluation)
        return evaluations

    def _require_selection_authority(
        self,
        selection: JointCandidateSelection,
    ) -> None:
        spec = self.get_spec(selection.parent_spec_id)
        if spec is None:
            raise ValueError(
                'joint selection references unpersisted JointOptimizationSpec'
            )
        if spec.semantic_sha256 != selection.parent_spec_sha256:
            raise ValueError('joint selection parent spec hash mismatch')

        candidate = self.get_candidate(selection.candidate_id)
        if candidate is None:
            raise ValueError('joint selection references unpersisted JointCandidate')
        if candidate.candidate_sha256 != selection.candidate_sha256:
            raise ValueError('joint selection candidate hash mismatch')

        if selection.evaluation_binding_id is not None:
            evaluation = self.get_evaluation(selection.evaluation_binding_id)
            if evaluation is None:
                raise ValueError(
                    'joint selection references unpersisted evaluation binding'
                )
            if (
                evaluation.evaluation_binding_sha256
                != selection.evaluation_binding_sha256
                or evaluation.candidate_id != selection.candidate_id
            ):
                raise ValueError('joint selection evaluation authority mismatch')

    def save_selection(
        self,
        selection: JointCandidateSelection,
    ) -> JointCandidateSelection:
        selection = JointCandidateSelection.model_validate(
            selection.model_dump(mode='python')
        )
        self._require_selection_authority(selection)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidate_selections
                WHERE selection_id=?
                """,
                (selection.selection_id,),
            ).fetchone()
            if existing is not None:
                persisted = JointCandidateSelection.model_validate_json(
                    existing['payload_json']
                )
                if persisted != selection:
                    raise ValueError(
                        'joint selection ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_joint_candidate_selections(
                    selection_id, selection_sha256, spec_id, candidate_id,
                    evaluation_binding_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.selection_id,
                    selection.selection_sha256,
                    selection.parent_spec_id,
                    selection.candidate_id,
                    selection.evaluation_binding_id,
                    selection.model_dump_json(),
                ),
            )
        return selection

    def get_selection(
        self,
        selection_id: str,
    ) -> JointCandidateSelection | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidate_selections
                WHERE selection_id=?
                """,
                (selection_id,),
            ).fetchone()
        if row is None:
            return None
        selection = JointCandidateSelection.model_validate_json(
            row['payload_json']
        )
        self._require_selection_authority(selection)
        return selection

    def latest_selection(
        self,
        spec_id: str,
    ) -> JointCandidateSelection | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_candidate_selections
                WHERE spec_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        selection = JointCandidateSelection.model_validate_json(
            row['payload_json']
        )
        self._require_selection_authority(selection)
        return selection
