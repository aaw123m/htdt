"""Persistence for line-level gain structure authority (#646)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_gain_structure import (
    GAIN_STRUCTURE_EVALUATION_VERSION,
    GainStructureEvaluation,
    GainStructureScenario,
    LineLevelStageCapability,
    evaluate_gain_structure,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


GainStructureEvaluator = Callable[..., GainStructureEvaluation]

GAIN_STRUCTURE_EVALUATORS: dict[str, GainStructureEvaluator] = {
    GAIN_STRUCTURE_EVALUATION_VERSION: evaluate_gain_structure,
}


class CadGainStructureRepository:
    """Append-only gain-structure persistence with fail-closed replay.

    Persisted evaluations are never trusted as self-hashed payloads: every
    save and every authoritative read re-resolves the exact persisted stage
    capabilities, replays the pinned canonical evaluator, and requires exact
    equality with the stored evaluation.
    """

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
            require_native_tables(connection, 'cad_line_level_stages', 'cad_gain_structure_scenarios', 'cad_gain_structure_evaluations')

    def save_stage(
        self,
        stage: LineLevelStageCapability,
    ) -> LineLevelStageCapability:
        stage = LineLevelStageCapability.model_validate(
            stage.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_line_level_stages
                WHERE stage_id=? AND version=?
                """,
                (stage.stage_id, stage.version),
            ).fetchone()
            if existing is not None:
                persisted = LineLevelStageCapability.model_validate_json(
                    existing['payload_json']
                )
                if persisted != stage:
                    raise ValueError(
                        'line-level stage id/version already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_line_level_stages(
                    stage_id, version, semantic_sha256, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    stage.stage_id,
                    stage.version,
                    stage.semantic_sha256,
                    stage.model_dump_json(),
                ),
            )
        return stage

    def get_stage(
        self,
        stage_id: str,
        version: str,
    ) -> LineLevelStageCapability | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_line_level_stages
                WHERE stage_id=? AND version=?
                """,
                (stage_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else LineLevelStageCapability.model_validate_json(
                row['payload_json']
            )
        )

    def get_stage_by_hash(
        self,
        semantic_sha256: str,
    ) -> LineLevelStageCapability | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_line_level_stages
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else LineLevelStageCapability.model_validate_json(
                row['payload_json']
            )
        )

    def list_stages(
        self,
        document_id: str | None = None,
    ) -> tuple[LineLevelStageCapability, ...]:
        del document_id  # stage capabilities are project-shared authority
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_line_level_stages
                ORDER BY seq ASC
                """,
            ).fetchall()
        return tuple(
            LineLevelStageCapability.model_validate_json(row['payload_json'])
            for row in rows
        )

    def _resolve_scenario_stages(
        self,
        scenario: GainStructureScenario,
    ) -> tuple[LineLevelStageCapability, ...]:
        stages: list[LineLevelStageCapability] = []
        for ref in scenario.stages:
            stage = self.get_stage_by_hash(ref.semantic_sha256)
            if stage is None:
                raise ValueError(
                    'gain-structure scenario references an unpersisted '
                    'line-level stage'
                )
            if (
                stage.stage_id != ref.authority_id
                or stage.version != ref.version
            ):
                raise ValueError(
                    'gain-structure scenario stage identity mismatch'
                )
            stages.append(stage)
        return tuple(stages)

    def save_scenario(
        self,
        scenario: GainStructureScenario,
    ) -> GainStructureScenario:
        scenario = GainStructureScenario.model_validate(
            scenario.model_dump(mode='python')
        )
        self._resolve_scenario_stages(scenario)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_gain_structure_scenarios
                WHERE scenario_id=?
                """,
                (scenario.scenario_id,),
            ).fetchone()
            if existing is not None:
                persisted = GainStructureScenario.model_validate_json(
                    existing['payload_json']
                )
                if persisted != scenario:
                    raise ValueError(
                        'gain-structure scenario ID already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_gain_structure_scenarios(
                    scenario_id, scenario_sha256, document_id, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.document_id,
                    scenario.model_dump_json(),
                ),
            )
        return scenario

    def get_scenario(
        self,
        scenario_id: str,
    ) -> GainStructureScenario | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_gain_structure_scenarios
                WHERE scenario_id=?
                """,
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = GainStructureScenario.model_validate_json(
            row['payload_json']
        )
        self._resolve_scenario_stages(scenario)
        return scenario

    def _validate_evaluation_binding(
        self,
        evaluation: GainStructureEvaluation,
    ) -> None:
        persisted = self.get_scenario(evaluation.scenario.scenario_id)
        if persisted is None:
            raise ValueError(
                'gain-structure evaluation references an unpersisted scenario'
            )
        if persisted != evaluation.scenario:
            raise ValueError(
                'gain-structure evaluation scenario authority mismatch'
            )
        stages = self._resolve_scenario_stages(persisted)
        evaluator = GAIN_STRUCTURE_EVALUATORS.get(evaluation.authority_version)
        if evaluator is None:
            raise ValueError(
                'gain-structure evaluator authority version is not pinned'
            )
        regenerated = evaluator(scenario=persisted, stages=stages)
        if regenerated != evaluation:
            raise ValueError(
                'gain-structure evaluation does not match evaluator authority'
            )

    def save_evaluation(
        self,
        evaluation: GainStructureEvaluation,
    ) -> GainStructureEvaluation:
        evaluation = GainStructureEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        self._validate_evaluation_binding(evaluation)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json FROM cad_gain_structure_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = GainStructureEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'gain-structure evaluation ID already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_gain_structure_evaluations(
                    evaluation_id, evaluation_sha256, scenario_id,
                    document_id, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.scenario.scenario_id,
                    evaluation.scenario.document_id,
                    evaluation.model_dump_json(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> GainStructureEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_gain_structure_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = GainStructureEvaluation.model_validate_json(
            row['payload_json']
        )
        self._validate_evaluation_binding(evaluation)
        return evaluation

    def list_evaluations_for_document(
        self,
        document_id: str,
    ) -> tuple[GainStructureEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_gain_structure_evaluations
                WHERE document_id=? ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        evaluations = tuple(
            GainStructureEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_evaluation_binding(evaluation)
        return evaluations


__all__ = [
    'CadGainStructureRepository',
    'GAIN_STRUCTURE_EVALUATORS',
]
