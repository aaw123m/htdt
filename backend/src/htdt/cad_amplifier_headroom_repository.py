from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import NamedTuple

from .cad_amplifier_headroom import (
    PLAYBACK_CHAIN_EVALUATION_VERSION,
    AmplifierOutputCapability,
    PlaybackChainEvaluation,
    PlaybackChainScenario,
    SpeakerElectricalLoadAuthority,
    evaluate_playback_chain,
)
from .cad_equipment import EquipmentDefinition
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
)
from .cad_speaker_impedance import (
    FREQUENCY_RESOLVED_EVALUATION_VERSION,
    AmplifierElectricalLimitAuthority,
    FrequencyResolvedElectricalEvaluation,
    SpeakerElectricalImpedanceAuthority,
    evaluate_frequency_resolved_load,
)
from .cad_system_variant import SystemVariant
from .cad_system_variant_repository import CadSystemVariantRepository


PlaybackChainEvaluator = Callable[..., PlaybackChainEvaluation]

# Canonical evaluator registry pinned by the authority_version recorded on the
# evaluation model; a recorded version without a pinned evaluator fails closed.
PLAYBACK_CHAIN_EVALUATORS: dict[str, PlaybackChainEvaluator] = {
    PLAYBACK_CHAIN_EVALUATION_VERSION: evaluate_playback_chain,
}

FrequencyResolvedEvaluator = Callable[..., FrequencyResolvedElectricalEvaluation]

FREQUENCY_RESOLVED_EVALUATORS: dict[str, FrequencyResolvedEvaluator] = {
    FREQUENCY_RESOLVED_EVALUATION_VERSION: evaluate_frequency_resolved_load,
}


class ResolvedPlaybackChainAuthorities(NamedTuple):
    """Exact persisted authorities a playback-chain scenario/evaluation binds."""

    revision: SceneRevision
    variant: SystemVariant
    equipment_definition: EquipmentDefinition
    amplifier_capability: AmplifierOutputCapability
    speaker_load: SpeakerElectricalLoadAuthority | None


class CadAmplifierHeadroomRepository:
    """Append-only O100D amplifier/electrical headroom authority persistence.

    Persisted evaluations are never trusted as self-hashed payloads: every
    save and every authoritative read re-resolves the exact persisted
    scenario, SceneRevision, SystemVariant, source EquipmentDefinition,
    amplifier capability, optional speaker load authority and equipment
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
            require_native_tables(connection, 'cad_amplifier_output_capabilities', 'cad_speaker_electrical_loads', 'cad_playback_chain_scenarios', 'cad_playback_chain_evaluations', 'cad_speaker_impedances', 'cad_amplifier_electrical_limits', 'cad_frequency_resolved_evaluations')

    def save_amplifier_capability(
        self,
        capability: AmplifierOutputCapability,
    ) -> AmplifierOutputCapability:
        capability = AmplifierOutputCapability.model_validate(
            capability.model_dump(mode='python')
        )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_output_capabilities
                WHERE capability_id=? AND version=?
                """,
                (capability.capability_id, capability.version),
            ).fetchone()
            if existing is not None:
                persisted = AmplifierOutputCapability.model_validate_json(
                    existing['payload_json']
                )
                if persisted != capability:
                    raise ValueError(
                        'amplifier capability id/version already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_amplifier_output_capabilities(
                    capability_id, version, semantic_sha256, output_id, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    capability.capability_id,
                    capability.version,
                    capability.semantic_sha256,
                    capability.output_id,
                    capability.model_dump_json(),
                ),
            )
        return capability

    def get_amplifier_capability(
        self,
        capability_id: str,
        version: str,
    ) -> AmplifierOutputCapability | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_output_capabilities
                WHERE capability_id=? AND version=?
                """,
                (capability_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else AmplifierOutputCapability.model_validate_json(
                row['payload_json']
            )
        )

    def get_amplifier_capability_by_hash(
        self,
        semantic_sha256: str,
    ) -> AmplifierOutputCapability | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_output_capabilities
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else AmplifierOutputCapability.model_validate_json(
                row['payload_json']
            )
        )

    def list_amplifier_capabilities(
        self,
    ) -> tuple[AmplifierOutputCapability, ...]:
        """Every persisted amplifier output capability (library scope)."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_output_capabilities
                ORDER BY seq ASC
                """
            ).fetchall()
        return tuple(
            AmplifierOutputCapability.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_speaker_load(
        self,
        load: SpeakerElectricalLoadAuthority,
    ) -> SpeakerElectricalLoadAuthority:
        load = SpeakerElectricalLoadAuthority.model_validate(
            load.model_dump(mode='python')
        )
        definition = self.equipment_repository.get_definition_by_hash(
            load.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'speaker load authority references an unpersisted EquipmentDefinition'
            )
        if (
            definition.definition_id != load.equipment_definition_id
            or definition.version != load.equipment_definition_version
        ):
            raise ValueError('speaker load EquipmentDefinition identity mismatch')

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_electrical_loads
                WHERE load_id=? AND version=?
                """,
                (load.load_id, load.version),
            ).fetchone()
            if existing is not None:
                persisted = SpeakerElectricalLoadAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != load:
                    raise ValueError(
                        'speaker load id/version already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_speaker_electrical_loads(
                    load_id, version, semantic_sha256,
                    equipment_definition_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    load.load_id,
                    load.version,
                    load.semantic_sha256,
                    load.equipment_definition_sha256,
                    load.model_dump_json(),
                ),
            )
        return load

    def get_speaker_load(
        self,
        load_id: str,
        version: str,
    ) -> SpeakerElectricalLoadAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_electrical_loads
                WHERE load_id=? AND version=?
                """,
                (load_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else SpeakerElectricalLoadAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def get_speaker_load_by_hash(
        self,
        semantic_sha256: str,
    ) -> SpeakerElectricalLoadAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_electrical_loads
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else SpeakerElectricalLoadAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def list_speaker_loads(
        self,
    ) -> tuple[SpeakerElectricalLoadAuthority, ...]:
        """Every persisted speaker load authority (library scope)."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_electrical_loads
                ORDER BY seq ASC
                """
            ).fetchall()
        return tuple(
            SpeakerElectricalLoadAuthority.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def _resolve_scenario_authorities(
        self,
        scenario: PlaybackChainScenario,
    ) -> ResolvedPlaybackChainAuthorities:
        revision = self.scene_repository.get(scenario.scene_revision_id)
        if revision is None:
            raise ValueError('playback-chain source SceneRevision does not exist')
        if (
            revision.document_id != scenario.document_id
            or revision.content_hash != scenario.scene_content_hash
        ):
            raise ValueError('playback-chain SceneRevision authority mismatch')

        variant = self.variant_repository.get_variant(scenario.variant_id)
        if variant is None:
            raise ValueError('playback-chain SystemVariant does not exist')
        if variant.variant_sha256 != scenario.variant_sha256:
            raise ValueError('playback-chain SystemVariant hash mismatch')
        if (
            variant.document_id != scenario.document_id
            or variant.baseline_revision_id != scenario.scene_revision_id
            or variant.baseline_content_hash != scenario.scene_content_hash
        ):
            raise ValueError('playback-chain SystemVariant baseline authority mismatch')

        definition = self.equipment_repository.get_definition_by_hash(
            scenario.source_equipment.semantic_sha256
        )
        if definition is None:
            raise ValueError(
                'playback-chain references an unpersisted source EquipmentDefinition'
            )
        if (
            definition.definition_id != scenario.source_equipment.authority_id
            or definition.version != scenario.source_equipment.version
        ):
            raise ValueError('playback-chain source EquipmentDefinition identity mismatch')

        source_bindings = [
            item
            for item in variant.equipment_bindings
            if item.entity_id == scenario.routing.source_entity_id
        ]
        if len(source_bindings) != 1:
            raise ValueError(
                'playback-chain source requires exactly one persisted equipment binding'
            )
        source_binding = source_bindings[0]
        if (
            source_binding.equipment_definition_id != definition.definition_id
            or source_binding.equipment_definition_version != definition.version
            or source_binding.equipment_definition_sha256 != definition.semantic_sha256
        ):
            raise ValueError('playback-chain persisted source equipment binding mismatch')

        amplifier = self.get_amplifier_capability_by_hash(
            scenario.amplifier_capability.semantic_sha256
        )
        if amplifier is None:
            raise ValueError(
                'playback-chain references an unpersisted amplifier capability'
            )
        if (
            amplifier.capability_id != scenario.amplifier_capability.authority_id
            or amplifier.version != scenario.amplifier_capability.version
            or amplifier.output_id != scenario.routing.amplifier_output_id
        ):
            raise ValueError('playback-chain amplifier capability identity mismatch')

        if scenario.speaker_load is None:
            if (
                scenario.speaker_load_semantics is not None
                or scenario.speaker_load_resistance_ohm is not None
            ):
                raise ValueError('playback-chain carries incomplete speaker load semantics')
            return ResolvedPlaybackChainAuthorities(
                revision=revision,
                variant=variant,
                equipment_definition=definition,
                amplifier_capability=amplifier,
                speaker_load=None,
            )

        load = self.get_speaker_load_by_hash(
            scenario.speaker_load.semantic_sha256
        )
        if load is None:
            raise ValueError(
                'playback-chain references an unpersisted speaker load authority'
            )
        if (
            load.load_id != scenario.speaker_load.authority_id
            or load.version != scenario.speaker_load.version
            or load.equipment_definition_id != definition.definition_id
            or load.equipment_definition_version != definition.version
            or load.equipment_definition_sha256 != definition.semantic_sha256
            or load.semantics != scenario.speaker_load_semantics
            or scenario.speaker_load_resistance_ohm is None
            or abs(
                float(load.resistance_ohm)
                - float(scenario.speaker_load_resistance_ohm)
            ) > 1e-12
        ):
            raise ValueError('playback-chain speaker load authority mismatch')
        return ResolvedPlaybackChainAuthorities(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            amplifier_capability=amplifier,
            speaker_load=load,
        )

    def save_scenario(
        self,
        scenario: PlaybackChainScenario,
    ) -> PlaybackChainScenario:
        scenario = PlaybackChainScenario.model_validate(
            scenario.model_dump(mode='python')
        )
        self._resolve_scenario_authorities(scenario)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_playback_chain_scenarios
                WHERE scenario_id=?
                """,
                (scenario.scenario_id,),
            ).fetchone()
            if existing is not None:
                persisted = PlaybackChainScenario.model_validate_json(
                    existing['payload_json']
                )
                if persisted != scenario:
                    raise ValueError(
                        'playback-chain scenario ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_playback_chain_scenarios(
                    scenario_id, scenario_sha256, document_id,
                    scene_revision_id, variant_id, source_equipment_sha256,
                    amplifier_capability_sha256, speaker_load_sha256,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.document_id,
                    scenario.scene_revision_id,
                    scenario.variant_id,
                    scenario.source_equipment.semantic_sha256,
                    scenario.amplifier_capability.semantic_sha256,
                    (
                        None
                        if scenario.speaker_load is None
                        else scenario.speaker_load.semantic_sha256
                    ),
                    scenario.model_dump_json(),
                ),
            )
        return scenario

    def _persisted_scenario(
        self,
        scenario_id: str,
    ) -> tuple[PlaybackChainScenario, ResolvedPlaybackChainAuthorities] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_playback_chain_scenarios
                WHERE scenario_id=?
                """,
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = PlaybackChainScenario.model_validate_json(row['payload_json'])
        return scenario, self._resolve_scenario_authorities(scenario)

    def get_scenario(
        self,
        scenario_id: str,
    ) -> PlaybackChainScenario | None:
        resolved = self._persisted_scenario(scenario_id)
        return None if resolved is None else resolved[0]

    def list_scenarios_for_variant(
        self,
        variant_id: str,
    ) -> tuple[PlaybackChainScenario, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_playback_chain_scenarios
                WHERE variant_id=?
                ORDER BY seq ASC
                """,
                (variant_id,),
            ).fetchall()
        scenarios = tuple(
            PlaybackChainScenario.model_validate_json(row['payload_json'])
            for row in rows
        )
        for scenario in scenarios:
            self._resolve_scenario_authorities(scenario)
        return scenarios

    def _validate_evaluation_binding(
        self,
        evaluation: PlaybackChainEvaluation,
    ) -> None:
        resolved = self._persisted_scenario(evaluation.scenario.scenario_id)
        if resolved is None:
            raise ValueError(
                'playback-chain evaluation references an unpersisted scenario'
            )
        scenario, authorities = resolved
        if scenario != evaluation.scenario:
            raise ValueError('playback-chain evaluation scenario authority mismatch')

        evaluator = PLAYBACK_CHAIN_EVALUATORS.get(evaluation.authority_version)
        if evaluator is None:
            raise ValueError(
                'playback-chain evaluator authority version is not pinned'
            )
        regenerated = evaluator(
            revision=authorities.revision,
            variant=authorities.variant,
            equipment_definition=authorities.equipment_definition,
            amplifier_capability=authorities.amplifier_capability,
            speaker_load=authorities.speaker_load,
            scenario=scenario,
        )
        if regenerated != evaluation:
            raise ValueError(
                'playback-chain evaluation does not match evaluator authority'
            )

    def save_evaluation(
        self,
        evaluation: PlaybackChainEvaluation,
    ) -> PlaybackChainEvaluation:
        evaluation = PlaybackChainEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        self._validate_evaluation_binding(evaluation)

        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_playback_chain_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = PlaybackChainEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'playback-chain evaluation ID already exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_playback_chain_evaluations(
                    evaluation_id, evaluation_sha256,
                    scenario_id, variant_id, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.scenario.scenario_id,
                    evaluation.scenario.variant_id,
                    evaluation.model_dump_json(),
                ),
            )
        return evaluation

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> PlaybackChainEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_playback_chain_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = PlaybackChainEvaluation.model_validate_json(
            row['payload_json']
        )
        self._validate_evaluation_binding(evaluation)
        return evaluation

    def resolve_evaluation_exact(
        self,
        evaluation_id: str,
        *,
        evaluation_sha256: str,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        variant_id: str,
        variant_sha256: str,
    ) -> PlaybackChainEvaluation | None:
        """Resolve one evaluation and revalidate its exact comparison binding.

        get_evaluation first reopens the persisted PlaybackChainScenario and
        revalidates its source EquipmentDefinition, amplifier capability,
        optional speaker load, and routing authority. This helper then pins the
        comparison-owned SceneRevision/SystemVariant identity and evaluation
        hash without introducing a second playback-chain model.
        """

        evaluation = self.get_evaluation(evaluation_id)
        if evaluation is None:
            return None
        if evaluation.evaluation_sha256 != evaluation_sha256:
            raise ValueError('playback-chain evaluation exact hash mismatch')
        scenario = evaluation.scenario
        if (
            scenario.document_id != document_id
            or scenario.scene_revision_id != scene_revision_id
            or scenario.scene_content_hash != scene_content_hash
        ):
            raise ValueError('playback-chain evaluation baseline authority mismatch')
        if (
            scenario.variant_id != variant_id
            or scenario.variant_sha256 != variant_sha256
        ):
            raise ValueError('playback-chain evaluation SystemVariant authority mismatch')
        return evaluation

    def list_evaluations_for_variant(
        self,
        variant_id: str,
    ) -> tuple[PlaybackChainEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_playback_chain_evaluations
                WHERE variant_id=?
                ORDER BY seq ASC
                """,
                (variant_id,),
            ).fetchall()
        evaluations = tuple(
            PlaybackChainEvaluation.model_validate_json(row['payload_json'])
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_evaluation_binding(evaluation)
        return evaluations

    # ------------------------------------------------------------------
    # Frequency-dependent speaker load authority (#544)

    def save_speaker_impedance(
        self,
        impedance: SpeakerElectricalImpedanceAuthority,
    ) -> SpeakerElectricalImpedanceAuthority:
        impedance = SpeakerElectricalImpedanceAuthority.model_validate(
            impedance.model_dump(mode='python')
        )
        definition = self.equipment_repository.get_definition_by_hash(
            impedance.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError(
                'speaker impedance references an unpersisted EquipmentDefinition'
            )
        if (
            definition.definition_id != impedance.equipment_definition_id
            or definition.version != impedance.equipment_definition_version
        ):
            raise ValueError(
                'speaker impedance EquipmentDefinition identity mismatch'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_impedances
                WHERE impedance_id=? AND version=?
                """,
                (impedance.impedance_id, impedance.version),
            ).fetchone()
            if existing is not None:
                persisted = SpeakerElectricalImpedanceAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != impedance:
                    raise ValueError(
                        'speaker impedance id/version already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_speaker_impedances(
                    impedance_id, version, semantic_sha256,
                    equipment_definition_sha256, tier, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    impedance.impedance_id,
                    impedance.version,
                    impedance.semantic_sha256,
                    impedance.equipment_definition_sha256,
                    impedance.tier,
                    impedance.model_dump_json(),
                ),
            )
        return impedance

    def get_speaker_impedance(
        self,
        impedance_id: str,
        version: str,
    ) -> SpeakerElectricalImpedanceAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_impedances
                WHERE impedance_id=? AND version=?
                """,
                (impedance_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else SpeakerElectricalImpedanceAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def get_speaker_impedance_by_hash(
        self,
        semantic_sha256: str,
    ) -> SpeakerElectricalImpedanceAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_impedances
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else SpeakerElectricalImpedanceAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def list_speaker_impedances(
        self,
        equipment_definition_sha256: str,
    ) -> tuple[SpeakerElectricalImpedanceAuthority, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_speaker_impedances
                WHERE equipment_definition_sha256=?
                ORDER BY seq ASC
                """,
                (equipment_definition_sha256,),
            ).fetchall()
        return tuple(
            SpeakerElectricalImpedanceAuthority.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def save_amplifier_limit(
        self,
        limit: AmplifierElectricalLimitAuthority,
    ) -> AmplifierElectricalLimitAuthority:
        limit = AmplifierElectricalLimitAuthority.model_validate(
            limit.model_dump(mode='python')
        )
        capability = self.get_amplifier_capability_by_hash(
            limit.amplifier_capability.semantic_sha256
        )
        if capability is None:
            raise ValueError(
                'amplifier electrical limit references an unpersisted '
                'capability'
            )
        if (
            capability.capability_id != limit.amplifier_capability.authority_id
            or capability.version != limit.amplifier_capability.version
        ):
            raise ValueError(
                'amplifier electrical limit capability identity mismatch'
            )
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_electrical_limits
                WHERE limit_id=? AND version=?
                """,
                (limit.limit_id, limit.version),
            ).fetchone()
            if existing is not None:
                persisted = AmplifierElectricalLimitAuthority.model_validate_json(
                    existing['payload_json']
                )
                if persisted != limit:
                    raise ValueError(
                        'amplifier electrical limit id/version already exists '
                        'with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_amplifier_electrical_limits(
                    limit_id, version, semantic_sha256,
                    amplifier_capability_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    limit.limit_id,
                    limit.version,
                    limit.semantic_sha256,
                    limit.amplifier_capability.semantic_sha256,
                    limit.model_dump_json(),
                ),
            )
        return limit

    def get_amplifier_limit(
        self,
        limit_id: str,
        version: str,
    ) -> AmplifierElectricalLimitAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_electrical_limits
                WHERE limit_id=? AND version=?
                """,
                (limit_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else AmplifierElectricalLimitAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def get_amplifier_limit_by_hash(
        self,
        semantic_sha256: str,
    ) -> AmplifierElectricalLimitAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_amplifier_electrical_limits
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        return (
            None
            if row is None
            else AmplifierElectricalLimitAuthority.model_validate_json(
                row['payload_json']
            )
        )

    def _resolve_frequency_resolved_authorities(
        self,
        evaluation: FrequencyResolvedElectricalEvaluation,
    ) -> tuple[
        SpeakerElectricalImpedanceAuthority,
        AmplifierOutputCapability,
        AmplifierElectricalLimitAuthority | None,
    ]:
        impedance = self.get_speaker_impedance_by_hash(
            evaluation.impedance_ref.semantic_sha256
        )
        if impedance is None:
            raise ValueError(
                'frequency-resolved evaluation references an unpersisted '
                'impedance authority'
            )
        if (
            impedance.impedance_id != evaluation.impedance_ref.authority_id
            or impedance.version != evaluation.impedance_ref.version
        ):
            raise ValueError(
                'frequency-resolved evaluation impedance identity mismatch'
            )
        capability = self.get_amplifier_capability_by_hash(
            evaluation.amplifier_capability.semantic_sha256
        )
        if capability is None:
            raise ValueError(
                'frequency-resolved evaluation references an unpersisted '
                'amplifier capability'
            )
        if (
            capability.capability_id
            != evaluation.amplifier_capability.authority_id
            or capability.version != evaluation.amplifier_capability.version
        ):
            raise ValueError(
                'frequency-resolved evaluation capability identity mismatch'
            )
        limit: AmplifierElectricalLimitAuthority | None = None
        if evaluation.amplifier_limit is not None:
            limit = self.get_amplifier_limit_by_hash(
                evaluation.amplifier_limit.semantic_sha256
            )
            if limit is None:
                raise ValueError(
                    'frequency-resolved evaluation references an unpersisted '
                    'amplifier electrical limit'
                )
            if (
                limit.limit_id != evaluation.amplifier_limit.authority_id
                or limit.version != evaluation.amplifier_limit.version
            ):
                raise ValueError(
                    'frequency-resolved evaluation limit identity mismatch'
                )
        return impedance, capability, limit

    def _validate_frequency_resolved_binding(
        self,
        evaluation: FrequencyResolvedElectricalEvaluation,
    ) -> None:
        impedance, capability, limit = (
            self._resolve_frequency_resolved_authorities(evaluation)
        )
        evaluator = FREQUENCY_RESOLVED_EVALUATORS.get(
            evaluation.authority_version
        )
        if evaluator is None:
            raise ValueError(
                'frequency-resolved evaluator authority version is not pinned'
            )
        regenerated = evaluator(
            impedance=impedance,
            amplifier_capability=capability,
            frequency_band=evaluation.frequency_band,
            required_voltage_v_rms=evaluation.required_voltage_v_rms,
            amplifier_limit=limit,
        )
        if regenerated != evaluation:
            raise ValueError(
                'frequency-resolved evaluation does not match evaluator '
                'authority'
            )

    def save_frequency_resolved_evaluation(
        self,
        evaluation: FrequencyResolvedElectricalEvaluation,
    ) -> FrequencyResolvedElectricalEvaluation:
        evaluation = FrequencyResolvedElectricalEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        self._validate_frequency_resolved_binding(evaluation)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_frequency_resolved_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                persisted = FrequencyResolvedElectricalEvaluation.model_validate_json(
                    existing['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'frequency-resolved evaluation ID already exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_frequency_resolved_evaluations(
                    evaluation_id, evaluation_sha256, impedance_sha256,
                    amplifier_capability_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.impedance_ref.semantic_sha256,
                    evaluation.amplifier_capability.semantic_sha256,
                    evaluation.model_dump_json(),
                ),
            )
        return evaluation

    def get_frequency_resolved_evaluation(
        self,
        evaluation_id: str,
    ) -> FrequencyResolvedElectricalEvaluation | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_frequency_resolved_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = FrequencyResolvedElectricalEvaluation.model_validate_json(
            row['payload_json']
        )
        self._validate_frequency_resolved_binding(evaluation)
        return evaluation

    def list_frequency_resolved_evaluations(
        self,
        impedance_sha256: str,
    ) -> tuple[FrequencyResolvedElectricalEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_frequency_resolved_evaluations
                WHERE impedance_sha256=?
                ORDER BY seq ASC
                """,
                (impedance_sha256,),
            ).fetchall()
        evaluations = tuple(
            FrequencyResolvedElectricalEvaluation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
        for evaluation in evaluations:
            self._validate_frequency_resolved_binding(evaluation)
        return evaluations
