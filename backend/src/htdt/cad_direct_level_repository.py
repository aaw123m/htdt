from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_direct_level import (
    DIRECT_LEVEL_AUTHORITY_VERSION,
    DirectLevelEvaluation,
    PlaybackExcitationScenario,
    evaluate_direct_level,
)
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .cad_system_variant_repository import CadSystemVariantRepository


DirectLevelEvaluator = Callable[..., DirectLevelEvaluation]

# Canonical evaluator registry pinned by the authority_version recorded on the
# evaluation model; a recorded version without a pinned evaluator fails closed.
DIRECT_LEVEL_EVALUATORS: dict[str, DirectLevelEvaluator] = {
    DIRECT_LEVEL_AUTHORITY_VERSION: evaluate_direct_level,
}


class CadDirectLevelRepository:
    """Append-only O100D direct/equipment-derived scenario and evaluation storage.

    Persisted evaluations are never trusted as self-hashed payloads: every
    save and every authoritative read re-resolves the exact scenario,
    SceneRevision, SystemVariant, EquipmentDefinition and persisted equipment
    binding, replays the pinned canonical evaluator from those authorities,
    and requires exact equality with the stored evaluation.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
        equipment_repository: CadEquipmentRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.equipment_repository = equipment_repository
        self.path = Path(scene_repository.path)
        if Path(variant_repository.path) != self.path:
            raise ValueError(
                'scene and SystemVariant repositories must share one native CAD database'
            )
        if Path(equipment_repository.path) != self.path:
            raise ValueError(
                'scene and EquipmentDefinition repositories must share one native CAD database'
            )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_direct_level_scenarios', 'cad_direct_level_evaluations')

    def save_scenario(
        self,
        scenario: PlaybackExcitationScenario,
    ) -> PlaybackExcitationScenario:
        scenario = PlaybackExcitationScenario.model_validate(
            scenario.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_scenarios
                WHERE scenario_id=?
                """,
                (scenario.scenario_id,),
            ).fetchone()
            if existing is not None:
                persisted = PlaybackExcitationScenario.model_validate_json(
                    existing['payload_json']
                )
                if persisted != scenario:
                    raise ValueError(
                        'direct-level scenario ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_direct_level_scenarios(
                    scenario_id, scenario_sha256, payload_json
                ) VALUES (?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.model_dump_json(),
                ),
            )
        return scenario

    def get_scenario(
        self,
        scenario_id: str,
    ) -> PlaybackExcitationScenario | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_scenarios
                WHERE scenario_id=?
                """,
                (scenario_id,),
            ).fetchone()
        return (
            None
            if row is None
            else PlaybackExcitationScenario.model_validate_json(
                row['payload_json']
            )
        )

    def list_scenarios(self) -> tuple[PlaybackExcitationScenario, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_scenarios
                ORDER BY seq ASC
                """
            ).fetchall()
        return tuple(
            PlaybackExcitationScenario.model_validate_json(row['payload_json'])
            for row in rows
        )

    def _validate_evaluation_binding(
        self,
        evaluation: DirectLevelEvaluation,
    ) -> None:
        scenario = self.get_scenario(evaluation.scenario.scenario_id)
        if scenario is None:
            raise ValueError(
                'direct-level evaluation references an unpersisted playback scenario'
            )
        if scenario != evaluation.scenario:
            raise ValueError('direct-level playback scenario authority mismatch')

        revision = self.scene_repository.get(evaluation.scene_revision_id)
        if revision is None:
            raise ValueError('direct-level source SceneRevision does not exist')
        if (
            revision.document_id != evaluation.document_id
            or revision.content_hash != evaluation.scene_content_hash
        ):
            raise ValueError('direct-level SceneRevision authority mismatch')

        variant = self.variant_repository.get_variant(evaluation.variant_id)
        if variant is None:
            raise ValueError('direct-level SystemVariant does not exist')
        if variant.variant_sha256 != evaluation.variant_sha256:
            raise ValueError('direct-level SystemVariant hash mismatch')
        if (
            variant.document_id != evaluation.document_id
            or variant.baseline_revision_id != evaluation.scene_revision_id
            or variant.baseline_content_hash != evaluation.scene_content_hash
        ):
            raise ValueError('direct-level SystemVariant baseline authority mismatch')

        definition = self.equipment_repository.get_definition_by_hash(
            evaluation.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'direct-level evaluation references an unpersisted EquipmentDefinition'
            )
        if (
            definition.definition_id != evaluation.equipment_definition_id
            or definition.version != evaluation.equipment_definition_version
        ):
            raise ValueError('direct-level EquipmentDefinition identity mismatch')

        bindings = [
            item
            for item in variant.equipment_bindings
            if item.entity_id == evaluation.scenario.source_entity_id
        ]
        if len(bindings) != 1:
            raise ValueError(
                'direct-level source requires exactly one persisted equipment binding'
            )
        binding = bindings[0]
        if (
            binding.equipment_definition_id != definition.definition_id
            or binding.equipment_definition_version != definition.version
            or binding.equipment_definition_sha256 != definition.semantic_sha256
        ):
            raise ValueError('direct-level persisted equipment binding mismatch')

        # The embedded usable-output profile is the replay authority
        # (#1047): its declared binding must match the resolved exact
        # EquipmentDefinition triple, and its self-hash is re-verified by
        # model validation on load.
        profile = evaluation.usable_output_profile
        if profile is not None and (
            profile.equipment_definition_id != definition.definition_id
            or (
                profile.equipment_definition_version is not None
                and profile.equipment_definition_version != definition.version
            )
            or (
                profile.equipment_definition_sha256 is not None
                and profile.equipment_definition_sha256
                != definition.semantic_sha256
            )
        ):
            raise ValueError(
                'direct-level usable-output profile binding mismatch'
            )

        evaluator = DIRECT_LEVEL_EVALUATORS.get(evaluation.authority_version)
        if evaluator is None:
            raise ValueError(
                'direct-level evaluator authority version is not pinned'
            )
        regenerated = evaluator(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            scenario=scenario,
            usable_output_profile=evaluation.usable_output_profile,
        )
        if regenerated != evaluation:
            raise ValueError(
                'direct-level evaluation does not match evaluator authority'
            )

    def save_evaluation(
        self,
        evaluation: DirectLevelEvaluation,
    ) -> DirectLevelEvaluation:
        evaluation = DirectLevelEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        self._validate_evaluation_binding(evaluation)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = DirectLevelEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'direct-level evaluation ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_direct_level_evaluations(
                    evaluation_id, evaluation_sha256, scenario_id,
                    document_id, scene_revision_id, variant_id,
                    equipment_definition_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.scenario.scenario_id,
                    evaluation.document_id,
                    evaluation.scene_revision_id,
                    evaluation.variant_id,
                    evaluation.equipment_definition_sha256,
                    evaluation.model_dump_json(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> DirectLevelEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = DirectLevelEvaluation.model_validate_json(
            row['payload_json']
        )
        self._validate_evaluation_binding(evaluation)
        return evaluation

    def list_evaluations_for_variant(
        self,
        variant_id: str,
    ) -> tuple[DirectLevelEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_evaluations
                WHERE variant_id=?
                ORDER BY seq ASC
                """,
                (variant_id,),
            ).fetchall()
        evaluations = tuple(
            DirectLevelEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_evaluation_binding(evaluation)
        return evaluations

    def list_evaluations_for_scenario(
        self,
        scenario_id: str,
    ) -> tuple[DirectLevelEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_direct_level_evaluations
                WHERE scenario_id=?
                ORDER BY seq ASC
                """,
                (scenario_id,),
            ).fetchall()
        evaluations = tuple(
            DirectLevelEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_evaluation_binding(evaluation)
        return evaluations
