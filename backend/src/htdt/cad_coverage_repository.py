from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_coverage import (
    COVERAGE_AUTHORITY_VERSION,
    CoverageEvaluation,
    CoverageEvaluationScenario,
    evaluate_coverage,
)
from .cad_directivity import validate_directivity_dataset_binding
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .cad_seat_priority import CadSeatPriorityProfileRepository
from .cad_system_variant_repository import CadSystemVariantRepository


CoverageEvaluator = Callable[..., CoverageEvaluation]

# Canonical evaluator registry pinned by the authority_version recorded on the
# evaluation model; a recorded version without a pinned evaluator fails closed.
COVERAGE_EVALUATORS: dict[str, CoverageEvaluator] = {
    COVERAGE_AUTHORITY_VERSION: evaluate_coverage,
}


class CadCoverageRepository:
    """Append-only O100D coverage scenario and evaluation storage.

    Persisted evaluations are never trusted as self-hashed payloads: every
    save and every authoritative read re-resolves the exact scenario,
    SceneRevision, SystemVariant, EquipmentDefinition, DirectivityDataset and
    persisted equipment binding, replays the pinned canonical evaluator from
    those authorities, and requires exact equality with the stored
    evaluation.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
        equipment_repository: CadEquipmentRepository,
        directivity_repository: CadDirectivityRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.equipment_repository = equipment_repository
        self.directivity_repository = directivity_repository
        # seat_priority evaluations replay through the bound profile — the
        # repository resolves it from the same native database rather than
        # trusting a caller-supplied object.
        self.seat_priority_repository = CadSeatPriorityProfileRepository(
            scene_repository
        )
        self.path = Path(scene_repository.path)
        for label, repository_path in (
            ('SystemVariant', variant_repository.path),
            ('EquipmentDefinition', equipment_repository.path),
            ('DirectivityDataset', directivity_repository.path),
        ):
            if Path(repository_path) != self.path:
                raise ValueError(
                    f'scene and {label} repositories must share one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_coverage_scenarios', 'cad_coverage_evaluations')

    def _resolve_scenario_authorities(
        self,
        scenario: CoverageEvaluationScenario,
    ) -> None:
        definition = self.equipment_repository.get_definition_by_hash(
            scenario.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'coverage scenario references an unpersisted EquipmentDefinition'
            )
        if (
            definition.definition_id != scenario.equipment_definition_id
            or definition.version != scenario.equipment_definition_version
        ):
            raise ValueError('coverage scenario EquipmentDefinition identity mismatch')

        dataset = self.directivity_repository.get_dataset_by_hash(
            scenario.directivity_dataset_sha256
        )
        if dataset is None:
            raise ValueError(
                'coverage scenario references an unpersisted DirectivityDataset'
            )
        if (
            dataset.dataset_id != scenario.directivity_dataset_id
            or dataset.version != scenario.directivity_dataset_version
        ):
            raise ValueError('coverage scenario DirectivityDataset identity mismatch')
        validate_directivity_dataset_binding(dataset, definition)
        if (
            scenario.source_angle_convention.dataset_angle_semantics
            != dataset.coordinate_convention.angle_semantics
            or scenario.source_angle_convention.dataset_reference_axis
            != dataset.coordinate_convention.reference_axis
        ):
            raise ValueError(
                'coverage scenario source-angle/dataset coordinate authority mismatch'
            )
        population = scenario.receiver_population
        if population.population_weighting == 'seat_priority':
            profile = self.seat_priority_repository.get(
                population.priority_profile_id
            )
            if (
                profile is None
                or profile.profile_sha256
                != population.priority_profile_sha256
            ):
                raise ValueError(
                    'coverage scenario references an unpersisted '
                    'SeatPriorityProfile'
                )

    def save_scenario(
        self,
        scenario: CoverageEvaluationScenario,
    ) -> CoverageEvaluationScenario:
        scenario = CoverageEvaluationScenario.model_validate(
            scenario.model_dump(mode='python')
        )
        self._resolve_scenario_authorities(scenario)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_scenarios
                WHERE scenario_id=?
                """,
                (scenario.scenario_id,),
            ).fetchone()
            if existing is not None:
                persisted = CoverageEvaluationScenario.model_validate_json(
                    existing['payload_json']
                )
                if persisted != scenario:
                    raise ValueError(
                        'coverage scenario ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_coverage_scenarios(
                    scenario_id, scenario_sha256,
                    equipment_definition_sha256,
                    directivity_dataset_sha256,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.equipment_definition_sha256,
                    scenario.directivity_dataset_sha256,
                    scenario.model_dump_json(),
                ),
            )
        return scenario

    def get_scenario(
        self,
        scenario_id: str,
    ) -> CoverageEvaluationScenario | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_scenarios
                WHERE scenario_id=?
                """,
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = CoverageEvaluationScenario.model_validate_json(
            row['payload_json']
        )
        self._resolve_scenario_authorities(scenario)
        return scenario

    def list_scenarios(self) -> tuple[CoverageEvaluationScenario, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_scenarios
                ORDER BY seq ASC
                """
            ).fetchall()
        scenarios = tuple(
            CoverageEvaluationScenario.model_validate_json(row['payload_json'])
            for row in rows
        )
        for scenario in scenarios:
            self._resolve_scenario_authorities(scenario)
        return scenarios

    def _validate_evaluation_binding(
        self,
        evaluation: CoverageEvaluation,
    ) -> None:
        scenario = self.get_scenario(evaluation.scenario.scenario_id)
        if scenario is None:
            raise ValueError(
                'coverage evaluation references an unpersisted scenario'
            )
        if scenario != evaluation.scenario:
            raise ValueError('coverage evaluation scenario authority mismatch')

        revision = self.scene_repository.get(evaluation.scene_revision_id)
        if revision is None:
            raise ValueError('coverage source SceneRevision does not exist')
        if (
            revision.document_id != evaluation.document_id
            or revision.content_hash != evaluation.scene_content_hash
        ):
            raise ValueError('coverage SceneRevision authority mismatch')

        variant = self.variant_repository.get_variant(evaluation.variant_id)
        if variant is None:
            raise ValueError('coverage SystemVariant does not exist')
        if variant.variant_sha256 != evaluation.variant_sha256:
            raise ValueError('coverage SystemVariant hash mismatch')
        if (
            variant.document_id != evaluation.document_id
            or variant.baseline_revision_id != evaluation.scene_revision_id
            or variant.baseline_content_hash != evaluation.scene_content_hash
        ):
            raise ValueError('coverage SystemVariant baseline authority mismatch')

        definition = self.equipment_repository.get_definition_by_hash(
            evaluation.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'coverage evaluation references an unpersisted EquipmentDefinition'
            )
        if (
            definition.definition_id != evaluation.equipment_definition_id
            or definition.version != evaluation.equipment_definition_version
        ):
            raise ValueError('coverage EquipmentDefinition identity mismatch')

        dataset = self.directivity_repository.get_dataset_by_hash(
            evaluation.directivity_dataset_sha256
        )
        if dataset is None:
            raise ValueError(
                'coverage evaluation references an unpersisted DirectivityDataset'
            )
        if (
            dataset.dataset_id != evaluation.directivity_dataset_id
            or dataset.version != evaluation.directivity_dataset_version
        ):
            raise ValueError('coverage DirectivityDataset identity mismatch')
        validate_directivity_dataset_binding(dataset, definition)

        bindings = [
            item
            for item in variant.equipment_bindings
            if item.entity_id == evaluation.scenario.source_entity_id
        ]
        if len(bindings) != 1:
            raise ValueError(
                'coverage source requires exactly one persisted equipment binding'
            )
        binding = bindings[0]
        if (
            binding.equipment_definition_id != definition.definition_id
            or binding.equipment_definition_version != definition.version
            or binding.equipment_definition_sha256 != definition.semantic_sha256
        ):
            raise ValueError('coverage persisted equipment binding mismatch')

        evaluator = COVERAGE_EVALUATORS.get(evaluation.authority_version)
        if evaluator is None:
            raise ValueError(
                'coverage evaluator authority version is not pinned'
            )
        # seat_priority scenarios bind an exact SeatPriorityProfile — the
        # replay must resolve the same persisted profile or it can never
        # reproduce the recorded evaluation.
        population = scenario.receiver_population
        priority_profile = None
        if population.population_weighting == 'seat_priority':
            priority_profile = self.seat_priority_repository.get(
                population.priority_profile_id
            )
            if (
                priority_profile is None
                or priority_profile.profile_sha256
                != population.priority_profile_sha256
            ):
                raise ValueError(
                    'coverage seat priority profile authority missing'
                )
        regenerated = evaluator(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            directivity_dataset=dataset,
            scenario=scenario,
            priority_profile=priority_profile,
        )
        if regenerated != evaluation:
            raise ValueError(
                'coverage evaluation does not match evaluator authority'
            )

    def save_evaluation(
        self,
        evaluation: CoverageEvaluation,
    ) -> CoverageEvaluation:
        evaluation = CoverageEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        self._validate_evaluation_binding(evaluation)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = CoverageEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'coverage evaluation ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_coverage_evaluations(
                    evaluation_id, evaluation_sha256, scenario_id,
                    document_id, scene_revision_id, variant_id,
                    equipment_definition_sha256,
                    directivity_dataset_sha256,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.scenario.scenario_id,
                    evaluation.document_id,
                    evaluation.scene_revision_id,
                    evaluation.variant_id,
                    evaluation.equipment_definition_sha256,
                    evaluation.directivity_dataset_sha256,
                    evaluation.model_dump_json(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> CoverageEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = CoverageEvaluation.model_validate_json(
            row['payload_json']
        )
        self._validate_evaluation_binding(evaluation)
        return evaluation

    def list_evaluations_for_variant(
        self,
        variant_id: str,
    ) -> tuple[CoverageEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_evaluations
                WHERE variant_id=?
                ORDER BY seq ASC
                """,
                (variant_id,),
            ).fetchall()
        evaluations = tuple(
            CoverageEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_evaluation_binding(evaluation)
        return evaluations

    def list_evaluations_for_scenario(
        self,
        scenario_id: str,
    ) -> tuple[CoverageEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_coverage_evaluations
                WHERE scenario_id=?
                ORDER BY seq ASC
                """,
                (scenario_id,),
            ).fetchall()
        evaluations = tuple(
            CoverageEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_evaluation_binding(evaluation)
        return evaluations
