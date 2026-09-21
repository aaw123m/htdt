from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_calibration_repository import CadCalibrationRepository
from .cad_joint_optimization import (
    JointCandidate,
    JointCandidateEvaluationBinding,
    JointCandidateSelection,
    JointOptimizationSpec,
)
from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema
from .cad_system_variant_repository import CadSystemVariantRepository


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
    ) -> None:
        paths = {
            Path(scene_repository.path),
            Path(system_variant_repository.path),
            Path(calibration_repository.path),
        }
        if len(paths) != 1:
            raise ValueError(
                'joint optimization authorities must share one native repository'
            )
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.calibration_repository = calibration_repository
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

    def _validate_spec_authorities(self, spec: JointOptimizationSpec) -> None:
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

    def save_spec(self, spec: JointOptimizationSpec) -> JointOptimizationSpec:
        spec = JointOptimizationSpec.model_validate(spec.model_dump(mode='python'))
        self._validate_spec_authorities(spec)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_optimization_specs
                WHERE spec_id=?
                """,
                (spec.spec_id,),
            ).fetchone()
            if existing is not None:
                persisted = JointOptimizationSpec.model_validate_json(
                    existing['payload_json']
                )
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

    def get_spec(self, spec_id: str) -> JointOptimizationSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_optimization_specs
                WHERE spec_id=?
                """,
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        spec = JointOptimizationSpec.model_validate_json(row['payload_json'])
        self._validate_spec_authorities(spec)
        return spec

    def list_specs(
        self,
        document_id: str,
    ) -> tuple[JointOptimizationSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_joint_optimization_specs
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        specs = tuple(
            JointOptimizationSpec.model_validate_json(row['payload_json'])
            for row in rows
        )
        for spec in specs:
            self._validate_spec_authorities(spec)
        return specs

    def _persisted_spec_for_candidate(
        self,
        candidate: JointCandidate,
    ) -> JointOptimizationSpec:
        spec = self.get_spec(candidate.parent_spec_id)
        if spec is None:
            raise ValueError('JointCandidate references unpersisted JointOptimizationSpec')
        if spec.semantic_sha256 != candidate.parent_spec_sha256:
            raise ValueError('JointCandidate parent spec hash mismatch')
        if candidate.evaluator != spec.evaluator:
            raise ValueError('JointCandidate evaluator authority mismatch')
        return spec

    def _validate_candidate_authorities(
        self,
        candidate: JointCandidate,
    ) -> JointOptimizationSpec:
        spec = self._persisted_spec_for_candidate(candidate)
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
        return spec

    def save_candidate(self, candidate: JointCandidate) -> JointCandidate:
        candidate = JointCandidate.model_validate(
            candidate.model_dump(mode='python')
        )
        spec = self._validate_candidate_authorities(candidate)
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
        self._validate_candidate_authorities(candidate)
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
            self._validate_candidate_authorities(candidate)
        return candidates

    def _validate_evaluation(
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
        self._validate_evaluation(evaluation)
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
        self._validate_evaluation(evaluation)
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
            self._validate_evaluation(evaluation)
        return evaluations

    def _validate_selection(
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
        self._validate_selection(selection)
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
        self._validate_selection(selection)
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
        self._validate_selection(selection)
        return selection
