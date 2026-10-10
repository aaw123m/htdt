
import sqlite3
from contextlib import (
    closing,
)
from pathlib import (
    Path,
)
from ..domain.cad_acoustic_snapshot import (
    AcousticPredictionRequest,
    AcousticSceneSnapshot,
)
from ...cad_repository import (
    SceneRepository,
    SceneRevision,
)
from ...cad_schema import (
    connect_sqlite,
    ensure_native_schema,
    require_native_tables,
)
from ...cad_system_variant import (
    SystemVariant,
)
from ...cad_system_variant_repository import (
    CadSystemVariantRepository,
)
from ...clock import (
    utc_now_iso as _utc_now,
)
from ..domain.cad_acoustic_treatment_comparison import (
    TreatmentComparisonOutcome,
    TreatmentDesignComparisonSpec,
    treatment_placement_comparison_ref,
    treatment_prediction_request_comparison_ref,
    treatment_snapshot_comparison_ref,
)
from ..persistence.cad_acoustic_snapshot_repository import (
    CadAcousticSnapshotRepository,
)
from ..persistence.cad_acoustic_treatment_repository import (
    CadAcousticTreatmentRepository,
)

class CadAcousticTreatmentComparisonRepository:
    """Append-only exact treatment-design comparison persistence."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        variant_repository: CadSystemVariantRepository | None = None,
        treatment_repository: CadAcousticTreatmentRepository | None = None,
        snapshot_repository: CadAcousticSnapshotRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.treatment_repository = (
            treatment_repository
            if treatment_repository is not None
            else CadAcousticTreatmentRepository(
                scene_repository,
                self.variant_repository,
            )
        )
        self.snapshot_repository = snapshot_repository
        self.path = Path(scene_repository.path)
        for label, repository in (
            ('SystemVariant', self.variant_repository),
            ('AcousticTreatment', self.treatment_repository),
            ('AcousticSnapshot', self.snapshot_repository),
        ):
            if repository is not None and Path(repository.path) != self.path:
                raise ValueError(
                    f'treatment comparison and {label} repositories must share '
                    'one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_acoustic_treatment_comparisons',
                'cad_treatment_comparison_outcomes',
            )

    def _validate(self, spec: TreatmentDesignComparisonSpec) -> None:
        revision = self.scene_repository.get(spec.baseline_scene_revision_id)
        if revision is None:
            raise ValueError('treatment comparison baseline SceneRevision does not exist')
        if (
            revision.document_id != spec.document_id
            or revision.content_hash != spec.baseline_scene_content_hash
        ):
            raise ValueError('treatment comparison baseline SceneRevision mismatch')

        for candidate in spec.candidates:
            variant = None
            if candidate.system_variant_id is not None:
                variant = self.variant_repository.get_variant(
                    candidate.system_variant_id
                )
                if variant is None:
                    raise ValueError(
                        'treatment comparison references missing SystemVariant'
                    )
                if variant.variant_sha256 != candidate.system_variant_sha256:
                    raise ValueError('treatment comparison SystemVariant hash mismatch')
                if (
                    variant.document_id != spec.document_id
                    or variant.baseline_revision_id != spec.baseline_scene_revision_id
                    or variant.baseline_content_hash != spec.baseline_scene_content_hash
                ):
                    raise ValueError(
                        'treatment comparison SystemVariant baseline mismatch'
                    )

            for placement_ref in candidate.placements:
                placement = self.treatment_repository.get_placement(
                    placement_ref.instance_id,
                    placement_ref.placement_version,
                )
                if placement is None:
                    raise ValueError(
                        'treatment comparison references missing treatment placement'
                    )
                if treatment_placement_comparison_ref(placement) != placement_ref:
                    raise ValueError(
                        'treatment comparison placement exact identity mismatch'
                    )
                if (
                    placement.document_id != spec.document_id
                    or placement.scene_revision_id
                    != spec.baseline_scene_revision_id
                    or placement.scene_content_hash
                    != spec.baseline_scene_content_hash
                ):
                    raise ValueError(
                        'treatment comparison placement baseline mismatch'
                    )
                if variant is None:
                    if placement.system_variant_id is not None:
                        raise ValueError(
                            'baseline treatment comparison candidate has '
                            'variant-bound placement'
                        )
                elif (
                    placement.system_variant_id != variant.variant_id
                    or placement.system_variant_sha256 != variant.variant_sha256
                ):
                    raise ValueError(
                        'treatment comparison placement SystemVariant mismatch'
                    )

            snapshot_ref = candidate.acoustic_scene_snapshot
            if snapshot_ref is not None:
                if self.snapshot_repository is None:
                    raise ValueError(
                        'prediction-traceable treatment comparison requires '
                        'CadAcousticSnapshotRepository'
                    )
                snapshot = self.snapshot_repository.get_snapshot(
                    snapshot_ref.snapshot_id
                )
                if snapshot is None:
                    raise ValueError(
                        'treatment comparison references missing AcousticSceneSnapshot'
                    )
                if treatment_snapshot_comparison_ref(snapshot) != snapshot_ref:
                    raise ValueError(
                        'treatment comparison AcousticSceneSnapshot exact identity mismatch'
                    )
                for request_ref in candidate.prediction_requests:
                    request = self.snapshot_repository.get_prediction_request(
                        request_ref.request_id,
                        _validated_snapshot=snapshot,
                    )
                    if request is None:
                        raise ValueError(
                            'treatment comparison references missing '
                            'AcousticPredictionRequest'
                        )
                    if (
                        treatment_prediction_request_comparison_ref(request)
                        != request_ref
                    ):
                        raise ValueError(
                            'treatment comparison AcousticPredictionRequest '
                            'exact identity mismatch'
                        )

    def save(
        self,
        spec: TreatmentDesignComparisonSpec,
    ) -> TreatmentDesignComparisonSpec:
        spec = TreatmentDesignComparisonSpec.model_validate(
            spec.model_dump(mode='python')
        )
        self._validate(spec)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_treatment_comparisons
                WHERE comparison_id=?
                """,
                (spec.comparison_id,),
            ).fetchone()
            if existing is not None:
                persisted = TreatmentDesignComparisonSpec.model_validate_json(
                    existing['payload_json']
                )
                if persisted != spec:
                    raise ValueError(
                        'TreatmentDesignComparisonSpec id exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_acoustic_treatment_comparisons(
                    comparison_id,
                    comparison_sha256,
                    document_id,
                    scene_revision_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.comparison_id,
                    spec.comparison_sha256,
                    spec.document_id,
                    spec.baseline_scene_revision_id,
                    spec.model_dump_json(),
                    _utc_now(),
                ),
            )
        return spec

    def get(
        self,
        comparison_id: str,
    ) -> TreatmentDesignComparisonSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_treatment_comparisons
                WHERE comparison_id=?
                """,
                (comparison_id,),
            ).fetchone()
        if row is None:
            return None
        spec = TreatmentDesignComparisonSpec.model_validate_json(
            row['payload_json']
        )
        self._validate(spec)
        return spec

    def list_all(
        self,
    ) -> tuple[TreatmentDesignComparisonSpec, ...]:
        """Every persisted comparison spec, validated on read (#451).

        Application-wide: only audit/export surfaces should consume this —
        project-owned UI must scope through ``list_for_document`` so a
        foreign project's comparisons can never appear (#917).
        """
        return self._list_specs()

    def list_for_document(
        self,
        document_id: str,
    ) -> tuple[TreatmentDesignComparisonSpec, ...]:
        """Comparison specs owned by one project document (#917)."""
        return self._list_specs(
            where="document_id=?",
            args=(document_id,),
        )

    def _list_specs(
        self,
        *,
        where: str = "",
        args: tuple[object, ...] = (),
    ) -> tuple[TreatmentDesignComparisonSpec, ...]:
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM cad_acoustic_treatment_comparisons
                {clause}
                ORDER BY seq ASC
                """,
                args,
            ).fetchall()
        specs: list[TreatmentDesignComparisonSpec] = []
        for row in rows:
            spec = TreatmentDesignComparisonSpec.model_validate_json(
                row['payload_json']
            )
            self._validate(spec)
            specs.append(spec)
        return tuple(specs)

    def _validate_outcome(
        self,
        outcome: TreatmentComparisonOutcome,
    ) -> TreatmentDesignComparisonSpec:
        spec = self.get(outcome.comparison_id)
        if spec is None:
            raise ValueError(
                'outcome references missing treatment comparison spec'
            )
        if spec.comparison_sha256 != outcome.comparison_sha256:
            raise ValueError('outcome comparison semantic hash mismatch')
        candidate_ids = {item.candidate_id for item in spec.candidates}
        for entry in outcome.outcomes:
            if entry.candidate_id not in candidate_ids:
                raise ValueError(
                    'outcome references a candidate outside the spec'
                )
        return spec

    def save_outcome(
        self,
        outcome: TreatmentComparisonOutcome,
    ) -> TreatmentComparisonOutcome:
        outcome = TreatmentComparisonOutcome.model_validate(
            outcome.model_dump(mode='python')
        )
        self._validate_outcome(outcome)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_treatment_comparison_outcomes
                WHERE outcome_id=?
                """,
                (outcome.outcome_id,),
            ).fetchone()
            if existing is not None:
                persisted = TreatmentComparisonOutcome.model_validate_json(
                    existing['payload_json']
                )
                if persisted != outcome:
                    raise ValueError(
                        'TreatmentComparisonOutcome id exists with '
                        'different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_treatment_comparison_outcomes(
                    outcome_id,
                    outcome_sha256,
                    comparison_id,
                    document_id,
                    compatibility,
                    evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.outcome_id,
                    outcome.outcome_sha256,
                    outcome.comparison_id,
                    outcome.document_id,
                    outcome.compatibility,
                    outcome.evaluated_at_utc,
                    outcome.model_dump_json(),
                ),
            )
        return outcome

    def get_outcome(
        self,
        outcome_id: str,
    ) -> TreatmentComparisonOutcome | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_treatment_comparison_outcomes
                WHERE outcome_id=?
                """,
                (outcome_id,),
            ).fetchone()
        if row is None:
            return None
        outcome = TreatmentComparisonOutcome.model_validate_json(
            row['payload_json']
        )
        self._validate_outcome(outcome)
        return outcome

    def list_outcomes(
        self,
        comparison_id: str,
    ) -> tuple[TreatmentComparisonOutcome, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_treatment_comparison_outcomes
                WHERE comparison_id=?
                ORDER BY seq ASC
                """,
                (comparison_id,),
            ).fetchall()
        outcomes: list[TreatmentComparisonOutcome] = []
        for row in rows:
            outcome = TreatmentComparisonOutcome.model_validate_json(
                row['payload_json']
            )
            self._validate_outcome(outcome)
            outcomes.append(outcome)
        return tuple(outcomes)

    def latest_outcome(
        self,
        comparison_id: str,
    ) -> TreatmentComparisonOutcome | None:
        outcomes = self.list_outcomes(comparison_id)
        return None if not outcomes else outcomes[-1]

__all__ = [
    'CadAcousticTreatmentComparisonRepository',
]
