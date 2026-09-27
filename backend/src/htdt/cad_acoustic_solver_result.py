from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_acoustic_snapshot import AcousticPredictionRequest
from .cad_acoustic_solver_adapter import AcousticSolverDispatchBinding
from .cad_equipment import FrequencyDomain
from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash
from .clock import utc_now_iso as _utc_now


ACOUSTIC_SOLVER_RESULT_SCHEMA_VERSION = 1
ACOUSTIC_SOLVER_RESULT_AUTHORITY_VERSION = 'acoustic-solver-result-1'

ExternalAuthorityResolver = Callable[
    [ExactExternalAuthorityRef],
    ExactExternalAuthorityRef | None,
]


class AcousticDispatchResolver(Protocol):
    path: Path

    def get_dispatch(
        self,
        binding_id: str,
    ) -> AcousticSolverDispatchBinding | None:
        ...


class AcousticPredictionRequestResolver(Protocol):
    path: Path

    def get_prediction_request(
        self,
        request_id: str,
    ) -> AcousticPredictionRequest | None:
        ...


def _domain_contains(
    container: FrequencyDomain,
    requested: FrequencyDomain,
) -> bool:
    return (
        float(requested.minimum_hz) >= float(container.minimum_hz)
        and float(requested.maximum_hz) <= float(container.maximum_hz)
    )


class AcousticSolverObservableArtifact(BaseModel):
    """One exact externally persisted solver-output artifact manifest binding.

    The artifact bytes and their encoding/schema remain external authorities.
    ``observable``, ``encoding_schema_ref`` and ``valid_frequency_domain`` must
    reproduce the resolved :class:`AcousticSolverArtifactManifest` of
    ``artifact_authority`` exactly; HTDT never trusts them from the caller
    alone and does not infer numerical semantics beyond that manifest.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable: str = Field(min_length=1)
    artifact_authority: ExactExternalAuthorityRef
    encoding_schema_ref: ExactExternalAuthorityRef
    valid_frequency_domain: FrequencyDomain


class AcousticSolverArtifactManifest(BaseModel):
    """Typed manifest resolved from the exact solver-artifact authority.

    The manifest is the artifact storage system's own declaration — reproduced
    by an :data:`AcousticSolverArtifactManifestResolver`, never trusted from a
    caller submission — of which observable the exact artifact encodes, which
    exact encoding schema governs it and which frequency domain the artifact
    actually covers. ``channel_identity``/``solver_lineage`` carry
    observable-specific grid/channel/receiver identity and deterministic
    solver-input/result lineage for downstream consumers; binding equality is
    enforced over ``artifact_ref``, ``observable``, ``encoding_schema_ref`` and
    ``valid_frequency_domain``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    artifact_ref: ExactExternalAuthorityRef
    observable: str = Field(min_length=1)
    encoding_schema_ref: ExactExternalAuthorityRef
    valid_frequency_domain: FrequencyDomain
    channel_identity: dict[str, Any] = Field(default_factory=dict)
    solver_lineage: dict[str, str] = Field(default_factory=dict)


AcousticSolverArtifactManifestResolver = Callable[
    [ExactExternalAuthorityRef],
    AcousticSolverArtifactManifest | None,
]


def _require_manifest_binding(
    binding: AcousticSolverObservableArtifact,
    manifest: AcousticSolverArtifactManifest,
) -> None:
    """Fail closed unless the submitted binding equals the resolved manifest."""
    if manifest.artifact_ref != binding.artifact_authority:
        raise ValueError(
            f'{binding.observable} artifact manifest resolves a different '
            'exact artifact authority'
        )
    if manifest.observable != binding.observable:
        raise ValueError(
            f'{binding.observable} artifact manifest observable mismatch'
        )
    if manifest.encoding_schema_ref != binding.encoding_schema_ref:
        raise ValueError(
            f'{binding.observable} artifact manifest encoding schema mismatch'
        )
    if manifest.valid_frequency_domain != binding.valid_frequency_domain:
        raise ValueError(
            f'{binding.observable} artifact manifest valid frequency domain '
            'mismatch'
        )


def _resolve_artifact_manifest(
    resolver: AcousticSolverArtifactManifestResolver,
    binding: AcousticSolverObservableArtifact,
) -> AcousticSolverArtifactManifest:
    manifest = resolver(binding.artifact_authority)
    if manifest is None:
        raise ValueError(
            f'{binding.observable} artifact manifest exact external authority '
            'does not exist'
        )
    _require_manifest_binding(binding, manifest)
    return manifest


class AcousticSolverResultEnvelope(BaseModel):
    """Completed exact solver output bound to one persisted READY dispatch.

    This envelope preserves provenance only. It does not claim numerical
    convergence, physical validation, hybrid validity, or production adoption.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ACOUSTIC_SOLVER_RESULT_SCHEMA_VERSION
    authority_version: Literal[
        'acoustic-solver-result-1'
    ] = ACOUSTIC_SOLVER_RESULT_AUTHORITY_VERSION

    result_id: str = Field(pattern=r'^acoustic-solver-result:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    execution_id: str = Field(min_length=1)
    result_state: Literal['COMPLETED'] = 'COMPLETED'

    dispatch_binding_id: str = Field(
        pattern=r'^acoustic-solver-dispatch:[0-9a-f]{64}$'
    )
    dispatch_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    prediction_request_id: str = Field(
        pattern=r'^acoustic-prediction-request:[0-9a-f]{64}$'
    )
    prediction_request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_deterministic_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    acoustic_scene_snapshot_id: str = Field(
        pattern=r'^acoustic-scene-snapshot:[0-9a-f]{64}$'
    )
    acoustic_scene_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    adapter_descriptor_id: str = Field(
        pattern=r'^acoustic-solver-adapter:[0-9a-f]{64}$'
    )
    adapter_descriptor_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deterministic_solver_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    solver_implementation_ref: ExactExternalAuthorityRef
    solver_configuration_ref: ExactExternalAuthorityRef
    execution_provenance_ref: ExactExternalAuthorityRef

    artifacts: tuple[AcousticSolverObservableArtifact, ...] = Field(min_length=1)
    completed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_result_identity(self) -> 'AcousticSolverResultEnvelope':
        observables = [item.observable for item in self.artifacts]
        if len(observables) != len(set(observables)):
            raise ValueError(
                'solver result envelope permits exactly one manifest per observable'
            )
        expected = _semantic_hash(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('AcousticSolverResultEnvelope semantic hash mismatch')
        if self.result_id != f'acoustic-solver-result:{expected}':
            raise ValueError('AcousticSolverResultEnvelope id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'result_id', 'semantic_sha256'},
        )


def build_acoustic_solver_result_envelope(
    *,
    dispatch: AcousticSolverDispatchBinding,
    request: AcousticPredictionRequest,
    execution_id: str,
    execution_provenance_ref: ExactExternalAuthorityRef,
    artifacts: Sequence[AcousticSolverObservableArtifact],
    completed_at_utc: str,
    artifact_manifest_resolver: AcousticSolverArtifactManifestResolver | None = None,
) -> AcousticSolverResultEnvelope:
    dispatch = AcousticSolverDispatchBinding.model_validate(
        dispatch.model_dump(mode='python')
    )
    request = AcousticPredictionRequest.model_validate(
        request.model_dump(mode='python')
    )

    if dispatch.state != 'READY':
        raise ValueError(
            'solver result can only bind a READY AcousticSolverDispatchBinding'
        )
    if (
        dispatch.prediction_request_id != request.request_id
        or dispatch.prediction_request_semantic_sha256
        != request.request_semantic_sha256
        or dispatch.prediction_deterministic_input_hash
        != request.deterministic_input_hash
    ):
        raise ValueError('solver result prediction request identity mismatch')
    if (
        dispatch.acoustic_scene_snapshot_id
        != request.acoustic_scene_snapshot_id
        or dispatch.acoustic_scene_snapshot_sha256
        != request.acoustic_scene_snapshot_sha256
    ):
        raise ValueError('solver result request/snapshot identity mismatch')

    artifact_tuple = tuple(
        sorted(
            (
                AcousticSolverObservableArtifact.model_validate(
                    item.model_dump(mode='python')
                )
                for item in artifacts
            ),
            key=lambda item: item.observable,
        )
    )
    if not artifact_tuple:
        raise ValueError('solver result requires at least one observable artifact')
    observed = {item.observable for item in artifact_tuple}
    requested = set(request.requested_observables)
    if observed != requested:
        missing = sorted(requested - observed)
        extra = sorted(observed - requested)
        raise ValueError(
            'solver result observable set must exactly match prediction request '
            f'(missing={missing}, extra={extra})'
        )
    for item in artifact_tuple:
        coverage = item.valid_frequency_domain
        if artifact_manifest_resolver is not None:
            coverage = _resolve_artifact_manifest(
                artifact_manifest_resolver,
                item,
            ).valid_frequency_domain
        if not _domain_contains(
            coverage,
            request.requested_frequency_domain,
        ):
            raise ValueError(
                f'solver result artifact does not cover requested frequency domain: '
                f'{item.observable}'
            )

    core = {
        'schema_version': ACOUSTIC_SOLVER_RESULT_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_SOLVER_RESULT_AUTHORITY_VERSION,
        'execution_id': execution_id,
        'result_state': 'COMPLETED',
        'dispatch_binding_id': dispatch.binding_id,
        'dispatch_binding_sha256': dispatch.semantic_sha256,
        'prediction_request_id': request.request_id,
        'prediction_request_semantic_sha256': request.request_semantic_sha256,
        'prediction_deterministic_input_hash': request.deterministic_input_hash,
        'acoustic_scene_snapshot_id': request.acoustic_scene_snapshot_id,
        'acoustic_scene_snapshot_sha256': request.acoustic_scene_snapshot_sha256,
        'adapter_descriptor_id': dispatch.adapter_descriptor_id,
        'adapter_descriptor_semantic_sha256': (
            dispatch.adapter_descriptor_semantic_sha256
        ),
        'deterministic_solver_input_hash': (
            dispatch.deterministic_solver_input_hash
        ),
        'solver_implementation_ref': dispatch.solver_implementation_ref.model_dump(
            mode='json'
        ),
        'solver_configuration_ref': dispatch.solver_configuration_ref.model_dump(
            mode='json'
        ),
        'execution_provenance_ref': execution_provenance_ref.model_dump(
            mode='json'
        ),
        'artifacts': [item.model_dump(mode='json') for item in artifact_tuple],
        'completed_at_utc': completed_at_utc,
    }
    digest = _semantic_hash(core)
    return AcousticSolverResultEnvelope(
        result_id=f'acoustic-solver-result:{digest}',
        semantic_sha256=digest,
        execution_id=execution_id,
        dispatch_binding_id=dispatch.binding_id,
        dispatch_binding_sha256=dispatch.semantic_sha256,
        prediction_request_id=request.request_id,
        prediction_request_semantic_sha256=request.request_semantic_sha256,
        prediction_deterministic_input_hash=request.deterministic_input_hash,
        acoustic_scene_snapshot_id=request.acoustic_scene_snapshot_id,
        acoustic_scene_snapshot_sha256=request.acoustic_scene_snapshot_sha256,
        adapter_descriptor_id=dispatch.adapter_descriptor_id,
        adapter_descriptor_semantic_sha256=(
            dispatch.adapter_descriptor_semantic_sha256
        ),
        deterministic_solver_input_hash=dispatch.deterministic_solver_input_hash,
        solver_implementation_ref=dispatch.solver_implementation_ref,
        solver_configuration_ref=dispatch.solver_configuration_ref,
        execution_provenance_ref=execution_provenance_ref,
        artifacts=artifact_tuple,
        completed_at_utc=completed_at_utc,
    )


class CadAcousticSolverResultRepository:
    """Append-only exact arbitrary-room solver-result persistence.

    Every artifact binding is re-resolved against the typed
    ``artifact_manifest_resolver`` on save and on read; the declared
    observable, encoding schema and valid frequency domain must reproduce the
    resolved :class:`AcousticSolverArtifactManifest` exactly.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        dispatch_resolver: AcousticDispatchResolver,
        request_resolver: AcousticPredictionRequestResolver,
        external_authority_resolver: ExternalAuthorityResolver,
        artifact_manifest_resolver: AcousticSolverArtifactManifestResolver,
    ) -> None:
        self.scene_repository = scene_repository
        self.dispatch_resolver = dispatch_resolver
        self.request_resolver = request_resolver
        self.external_authority_resolver = external_authority_resolver
        self.artifact_manifest_resolver = artifact_manifest_resolver
        self.path = Path(scene_repository.path)
        for label, resolver in (
            ('solver dispatch', dispatch_resolver),
            ('prediction request', request_resolver),
        ):
            if Path(resolver.path) != self.path:
                raise ValueError(
                    f'acoustic solver result and {label} repositories must '
                    'share one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_acoustic_solver_results')

    def _resolve_external(
        self,
        ref: ExactExternalAuthorityRef,
        *,
        label: str,
    ) -> ExactExternalAuthorityRef:
        resolved = self.external_authority_resolver(ref)
        if resolved is None:
            raise ValueError(f'{label} exact external authority does not exist')
        if resolved != ref:
            raise ValueError(f'{label} exact external authority mismatch')
        return resolved

    def _validate(
        self,
        result: AcousticSolverResultEnvelope,
    ) -> AcousticSolverResultEnvelope:
        result = AcousticSolverResultEnvelope.model_validate(
            result.model_dump(mode='python')
        )
        dispatch = self.dispatch_resolver.get_dispatch(
            result.dispatch_binding_id
        )
        if dispatch is None:
            raise ValueError(
                'solver result references missing AcousticSolverDispatchBinding'
            )
        if dispatch.semantic_sha256 != result.dispatch_binding_sha256:
            raise ValueError('solver result dispatch binding hash mismatch')

        request = self.request_resolver.get_prediction_request(
            result.prediction_request_id
        )
        if request is None:
            raise ValueError(
                'solver result references missing AcousticPredictionRequest'
            )

        for ref, label in (
            (result.execution_provenance_ref, 'execution provenance'),
            (result.solver_implementation_ref, 'solver implementation'),
            (result.solver_configuration_ref, 'solver configuration'),
        ):
            self._resolve_external(ref, label=label)
        for item in result.artifacts:
            self._resolve_external(
                item.artifact_authority,
                label=f'{item.observable} artifact',
            )
            self._resolve_external(
                item.encoding_schema_ref,
                label=f'{item.observable} encoding schema',
            )
            manifest = _resolve_artifact_manifest(
                self.artifact_manifest_resolver,
                item,
            )
            if not _domain_contains(
                manifest.valid_frequency_domain,
                request.requested_frequency_domain,
            ):
                raise ValueError(
                    'solver result artifact manifest does not cover requested '
                    f'frequency domain: {item.observable}'
                )

        regenerated = build_acoustic_solver_result_envelope(
            dispatch=dispatch,
            request=request,
            execution_id=result.execution_id,
            execution_provenance_ref=result.execution_provenance_ref,
            artifacts=result.artifacts,
            completed_at_utc=result.completed_at_utc,
        )
        if regenerated != result:
            raise ValueError(
                'solver result does not reproduce from exact persisted authorities'
            )
        return result

    def save(
        self,
        result: AcousticSolverResultEnvelope,
    ) -> AcousticSolverResultEnvelope:
        result = self._validate(result)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_results
                WHERE result_id=?
                """,
                (result.result_id,),
            ).fetchone()
            if existing is not None:
                persisted = AcousticSolverResultEnvelope.model_validate_json(
                    existing['payload_json']
                )
                if persisted != result:
                    raise ValueError(
                        'AcousticSolverResultEnvelope id exists with '
                        'different semantics'
                    )
                return self._validate(persisted)
            connection.execute(
                """
                INSERT INTO cad_acoustic_solver_results(
                    result_id,
                    semantic_sha256,
                    execution_id,
                    dispatch_binding_id,
                    prediction_request_id,
                    acoustic_scene_snapshot_id,
                    deterministic_solver_input_hash,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.semantic_sha256,
                    result.execution_id,
                    result.dispatch_binding_id,
                    result.prediction_request_id,
                    result.acoustic_scene_snapshot_id,
                    result.deterministic_solver_input_hash,
                    result.model_dump_json(),
                    _utc_now(),
                ),
            )
        return result

    def get(
        self,
        result_id: str,
    ) -> AcousticSolverResultEnvelope | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_acoustic_solver_results
                WHERE result_id=?
                """,
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            AcousticSolverResultEnvelope.model_validate_json(
                row['payload_json']
            )
        )
