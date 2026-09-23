from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest, AcousticSceneSnapshot
from .cad_acoustic_snapshot_repository import CadAcousticSnapshotRepository
from .cad_acoustic_treatment import AcousticTreatmentPlacement
from .cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_schema import ensure_native_schema
from .cad_system_variant import SystemVariant
from .cad_system_variant_repository import CadSystemVariantRepository


TREATMENT_COMPARISON_SCHEMA_VERSION = 1
TREATMENT_COMPARISON_AUTHORITY_VERSION = 'acoustic-treatment-comparison-1'
TREATMENT_COMPARISON_CANDIDATE_VERSION = 'acoustic-treatment-comparison-candidate-1'

TreatmentComparisonRole = Literal['no_treatment', 'treatment']


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TreatmentPlacementComparisonRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    instance_id: str = Field(min_length=1)
    placement_version: int = Field(ge=1)
    placement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    lifecycle: Literal['proposed', 'installed']


class TreatmentSnapshotComparisonRef(BaseModel):
    """Exact snapshot identity plus treatment-placement lineage exposed by overlays."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    snapshot_id: str = Field(pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_schema_version: int = Field(ge=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    treatment_placement_sha256s: tuple[str, ...] = ()

    @model_validator(mode='after')
    def validate_ref(self) -> 'TreatmentSnapshotComparisonRef':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('snapshot SystemVariant id/hash must be supplied together')
        if self.treatment_placement_sha256s != tuple(
            sorted(set(self.treatment_placement_sha256s))
        ):
            raise ValueError(
                'snapshot treatment placement hashes must be unique and sorted'
            )
        return self


class TreatmentPredictionRequestComparisonRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    request_id: str = Field(pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$')
    request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_id: str = Field(pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class TreatmentDesignComparisonCandidate(BaseModel):
    """One named treatment design. No objective value is recomputed here."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'acoustic-treatment-comparison-candidate-1'
    ] = TREATMENT_COMPARISON_CANDIDATE_VERSION
    candidate_id: str = Field(
        pattern=r'^treatment-comparison-candidate:[0-9a-f]{64}$'
    )
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    label: str = Field(min_length=1)
    role: TreatmentComparisonRole

    document_id: str = Field(min_length=1)
    baseline_scene_revision_id: str = Field(min_length=1)
    baseline_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    placements: tuple[TreatmentPlacementComparisonRef, ...] = ()
    acoustic_scene_snapshot: TreatmentSnapshotComparisonRef | None = None
    prediction_requests: tuple[TreatmentPredictionRequestComparisonRef, ...] = ()

    @model_validator(mode='after')
    def validate_candidate(self) -> 'TreatmentDesignComparisonCandidate':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('candidate SystemVariant id/hash must be supplied together')

        placement_keys = [
            (item.instance_id, item.placement_version)
            for item in self.placements
        ]
        if len(placement_keys) != len(set(placement_keys)):
            raise ValueError('candidate treatment placement refs must be unique')

        if self.role == 'no_treatment' and self.placements:
            raise ValueError('no-treatment candidate cannot contain treatment placements')
        if self.role == 'treatment' and not self.placements:
            raise ValueError('treatment candidate requires at least one placement')

        snapshot = self.acoustic_scene_snapshot
        if snapshot is None and self.prediction_requests:
            raise ValueError(
                'prediction request refs require an exact AcousticSceneSnapshot ref'
            )
        if snapshot is not None:
            if (
                snapshot.scene_revision_id != self.baseline_scene_revision_id
                or snapshot.scene_content_hash != self.baseline_scene_content_hash
            ):
                raise ValueError('candidate snapshot baseline SceneRevision mismatch')
            if (
                snapshot.system_variant_id != self.system_variant_id
                or snapshot.system_variant_sha256 != self.system_variant_sha256
            ):
                raise ValueError('candidate snapshot SystemVariant mismatch')
            expected_placement_hashes = tuple(
                sorted(item.placement_sha256 for item in self.placements)
            )
            if snapshot.treatment_placement_sha256s != expected_placement_hashes:
                raise ValueError(
                    'candidate snapshot treatment-placement lineage mismatch'
                )
            for request in self.prediction_requests:
                if (
                    request.snapshot_id != snapshot.snapshot_id
                    or request.snapshot_sha256 != snapshot.snapshot_sha256
                ):
                    raise ValueError(
                        'candidate prediction request snapshot identity mismatch'
                    )

        digest = _digest(self.semantic_payload())
        if self.candidate_sha256 != digest:
            raise ValueError('TreatmentDesignComparisonCandidate semantic hash mismatch')
        if self.candidate_id != f'treatment-comparison-candidate:{digest}':
            raise ValueError('TreatmentDesignComparisonCandidate id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'candidate_id', 'candidate_sha256'},
        )


class TreatmentDesignComparisonSpec(BaseModel):
    """Named exact A/B/no-treatment comparison without hidden scoring."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TREATMENT_COMPARISON_SCHEMA_VERSION
    authority_version: Literal[
        'acoustic-treatment-comparison-1'
    ] = TREATMENT_COMPARISON_AUTHORITY_VERSION
    comparison_id: str = Field(
        pattern=r'^treatment-design-comparison:[0-9a-f]{64}$'
    )
    comparison_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    name: str = Field(min_length=1)

    document_id: str = Field(min_length=1)
    baseline_scene_revision_id: str = Field(min_length=1)
    baseline_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    candidates: tuple[TreatmentDesignComparisonCandidate, ...] = Field(min_length=2)

    @model_validator(mode='after')
    def validate_comparison(self) -> 'TreatmentDesignComparisonSpec':
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('treatment comparison candidates must be unique')
        labels = [item.label for item in self.candidates]
        if len(labels) != len(set(labels)):
            raise ValueError('treatment comparison candidate labels must be unique')
        for item in self.candidates:
            if (
                item.document_id != self.document_id
                or item.baseline_scene_revision_id != self.baseline_scene_revision_id
                or item.baseline_scene_content_hash != self.baseline_scene_content_hash
            ):
                raise ValueError('treatment comparison candidate baseline mismatch')

        digest = _digest(self.semantic_payload())
        if self.comparison_sha256 != digest:
            raise ValueError('TreatmentDesignComparisonSpec semantic hash mismatch')
        if self.comparison_id != f'treatment-design-comparison:{digest}':
            raise ValueError('TreatmentDesignComparisonSpec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'comparison_id', 'comparison_sha256'},
        )


def treatment_placement_comparison_ref(
    placement: AcousticTreatmentPlacement,
) -> TreatmentPlacementComparisonRef:
    placement = AcousticTreatmentPlacement.model_validate(
        placement.model_dump(mode='python')
    )
    return TreatmentPlacementComparisonRef(
        instance_id=placement.instance_id,
        placement_version=placement.placement_version,
        placement_sha256=placement.placement_sha256,
        definition_id=placement.definition_id,
        definition_version=placement.definition_version,
        definition_sha256=placement.definition_sha256,
        lifecycle=placement.lifecycle,
    )


def treatment_snapshot_comparison_ref(
    snapshot: AcousticSceneSnapshot,
) -> TreatmentSnapshotComparisonRef:
    snapshot = AcousticSceneSnapshot.model_validate(
        snapshot.model_dump(mode='python')
    )
    placement_hashes = sorted(
        {
            overlay.treatment_placement_hash_sha256
            for binding in snapshot.treatment_boundary_bindings
            for overlay in binding.attached_treatment_overlays
        }
    )
    return TreatmentSnapshotComparisonRef(
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.semantic_sha256,
        snapshot_schema_version=snapshot.schema_version,
        scene_revision_id=snapshot.scene_revision_id,
        scene_content_hash=snapshot.scene_content_hash,
        system_variant_id=snapshot.system_variant_id,
        system_variant_sha256=snapshot.system_variant_sha256,
        treatment_placement_sha256s=tuple(placement_hashes),
    )


def treatment_prediction_request_comparison_ref(
    request: AcousticPredictionRequest,
) -> TreatmentPredictionRequestComparisonRef:
    request = AcousticPredictionRequest.model_validate(
        request.model_dump(mode='python')
    )
    return TreatmentPredictionRequestComparisonRef(
        request_id=request.request_id,
        request_semantic_sha256=request.request_semantic_sha256,
        deterministic_input_hash=request.deterministic_input_hash,
        snapshot_id=request.acoustic_scene_snapshot_id,
        snapshot_sha256=request.acoustic_scene_snapshot_sha256,
    )


def build_treatment_design_candidate(
    *,
    baseline: SceneRevision,
    label: str,
    role: TreatmentComparisonRole,
    system_variant: SystemVariant | None = None,
    placements: Sequence[AcousticTreatmentPlacement] = (),
    acoustic_scene_snapshot: AcousticSceneSnapshot | None = None,
    prediction_requests: Sequence[AcousticPredictionRequest] = (),
) -> TreatmentDesignComparisonCandidate:
    placement_tuple = tuple(placements)
    if system_variant is not None:
        if (
            system_variant.document_id != baseline.document_id
            or system_variant.baseline_revision_id != baseline.revision_id
            or system_variant.baseline_content_hash != baseline.content_hash
        ):
            raise ValueError('treatment comparison SystemVariant baseline mismatch')
    for placement in placement_tuple:
        if (
            placement.document_id != baseline.document_id
            or placement.scene_revision_id != baseline.revision_id
            or placement.scene_content_hash != baseline.content_hash
        ):
            raise ValueError('treatment comparison placement baseline mismatch')
        if system_variant is None:
            if placement.system_variant_id is not None:
                raise ValueError(
                    'baseline treatment candidate cannot use variant-bound placement'
                )
        elif (
            placement.system_variant_id != system_variant.variant_id
            or placement.system_variant_sha256 != system_variant.variant_sha256
        ):
            raise ValueError('treatment comparison placement SystemVariant mismatch')

    snapshot_ref = (
        None
        if acoustic_scene_snapshot is None
        else treatment_snapshot_comparison_ref(acoustic_scene_snapshot)
    )
    request_refs = tuple(
        treatment_prediction_request_comparison_ref(item)
        for item in prediction_requests
    )
    placement_refs = tuple(
        sorted(
            (treatment_placement_comparison_ref(item) for item in placement_tuple),
            key=lambda item: (item.instance_id, item.placement_version),
        )
    )
    core = {
        'authority_version': TREATMENT_COMPARISON_CANDIDATE_VERSION,
        'label': label,
        'role': role,
        'document_id': baseline.document_id,
        'baseline_scene_revision_id': baseline.revision_id,
        'baseline_scene_content_hash': baseline.content_hash,
        'system_variant_id': (
            None if system_variant is None else system_variant.variant_id
        ),
        'system_variant_sha256': (
            None if system_variant is None else system_variant.variant_sha256
        ),
        'placements': [item.model_dump(mode='json') for item in placement_refs],
        'acoustic_scene_snapshot': (
            None if snapshot_ref is None else snapshot_ref.model_dump(mode='json')
        ),
        'prediction_requests': [
            item.model_dump(mode='json') for item in request_refs
        ],
    }
    digest = _digest(core)
    return TreatmentDesignComparisonCandidate(
        candidate_id=f'treatment-comparison-candidate:{digest}',
        candidate_sha256=digest,
        label=label,
        role=role,
        document_id=baseline.document_id,
        baseline_scene_revision_id=baseline.revision_id,
        baseline_scene_content_hash=baseline.content_hash,
        system_variant_id=(
            None if system_variant is None else system_variant.variant_id
        ),
        system_variant_sha256=(
            None if system_variant is None else system_variant.variant_sha256
        ),
        placements=placement_refs,
        acoustic_scene_snapshot=snapshot_ref,
        prediction_requests=request_refs,
    )


def build_treatment_design_comparison(
    *,
    name: str,
    baseline: SceneRevision,
    candidates: Sequence[TreatmentDesignComparisonCandidate],
) -> TreatmentDesignComparisonSpec:
    candidate_tuple = tuple(candidates)
    core = {
        'schema_version': TREATMENT_COMPARISON_SCHEMA_VERSION,
        'authority_version': TREATMENT_COMPARISON_AUTHORITY_VERSION,
        'name': name,
        'document_id': baseline.document_id,
        'baseline_scene_revision_id': baseline.revision_id,
        'baseline_scene_content_hash': baseline.content_hash,
        'candidates': [item.model_dump(mode='json') for item in candidate_tuple],
    }
    digest = _digest(core)
    return TreatmentDesignComparisonSpec(
        comparison_id=f'treatment-design-comparison:{digest}',
        comparison_sha256=digest,
        name=name,
        document_id=baseline.document_id,
        baseline_scene_revision_id=baseline.revision_id,
        baseline_scene_content_hash=baseline.content_hash,
        candidates=candidate_tuple,
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
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_acoustic_treatment_comparisons (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    comparison_id TEXT NOT NULL UNIQUE,
                    comparison_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id)
                        REFERENCES scene_revisions(revision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_treatment_comparison_scene_seq
                    ON cad_acoustic_treatment_comparisons(
                        scene_revision_id,
                        seq ASC
                    );
                """
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
                        request_ref.request_id
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
