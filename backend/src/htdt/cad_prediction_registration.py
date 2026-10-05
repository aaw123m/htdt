"""In-app registration lane over the persisted acoustic solver stack.

Solver lanes (``scripts/run_r130a_candidate_wave_execution.py`` and related
pipelines) persist the full acoustic authority chain into the shared native
database plus a content-addressed ``ExactJsonAuthorityStore`` under
``<db dir>/authorities``: scene snapshot, prediction request, solver
descriptor + dispatch binding, solver result envelope and every exact
external payload those models reference. The solver-stack tables are
``STRUCTURAL_ONLY`` authorities in the native authority audit — this lane
re-verifies them by replaying the persisted stack through the *registered*
repositories themselves, feeding every resolver from the content-addressed
store instead of trusting the caller:

* ``environment``: the typed :class:`SnapshotEnvironmentAuthorityRef` is
  only ever carried by a persisted snapshot — the lane indexes every
  persisted snapshot's declared environment ref and resolves it only when
  the store still holds the sealed payload.
* environment scalars: the ``{'quantity': Q, 'value': V}`` payload
  convention the solver lanes already write.
* ``valid_frequency_domain`` / ``receiver_measurement`` /
  ``numerical_fidelity_policy`` / ``geometric_topology_preflight``: payload
  conventions reconstructing the typed authority (full model dump, or the
  dump without ``authority_ref`` which is reattached and equality-checked).
* ``external_authority`` and solver artifact manifests: resolved directly
  through :class:`ExactJsonAuthorityStore` (hash-verified on every read).

Anything that cannot be re-derived from persisted + content-addressed bytes
returns ``None`` and the repositories fail closed — the lane never
fabricates a resolver. Rows produced by lanes that did not persist a
reconstructable authority (e.g. claims-only fidelity payloads) surface as
``登録不可`` with the repository's own reason, never as silent skips.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .cad_acoustic_snapshot import (
    AcousticSceneSnapshot,
    ReceiverMeasurementAuthority,
)
from .cad_acoustic_snapshot_repository import (
    AcousticSnapshotAuthorityResolvers,
    CadAcousticSnapshotRepository,
)
from .cad_acoustic_solver_adapter import (
    AcousticNumericalFidelityPolicy,
)
from .cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from .cad_acoustic_solver_result import (
    AcousticSolverArtifactManifestResolver,
    AcousticSolverResultEnvelope,
    CadAcousticSolverResultRepository,
)
from .cad_candidate_wave_execution import ExactJsonAuthorityStore
from .cad_equipment import FrequencyDomain
from .cad_geometric_acoustics_portal import GeometricPortalGraph
from .cad_prediction_matrix import (
    MATRIX_RUN_VERIFICATION_AUTHORITY_VERSION,
    MatrixRunVerification,
    build_matrix_run_verification,
)
from .cad_prediction_matrix_repository import (
    CadPredictionMatrixRepository,
)
from .cad_prediction_provider import (
    CadPredictionProviderRepository,
    LowBandPredictionProvider,
    PredictionProviderRef,
    build_r130_low_band_prediction_provider,
)
from .cad_repository import SceneRepository
from .r120_geometry_compiler import ExactExternalAuthorityRef

DEFAULT_AUTHORITY_ROOT_NAME = 'authorities'

_SNAPSHOT_TABLE = 'cad_acoustic_scene_snapshots'
_REQUEST_TABLE = 'cad_acoustic_prediction_requests'
_RESULT_TABLE = 'cad_acoustic_solver_results'

_FIDELITY_POLICY_FIELDS = frozenset(
    AcousticNumericalFidelityPolicy.model_fields
) - {'authority_ref'}


def _ref_key(ref: ExactExternalAuthorityRef) -> tuple[str, str, str]:
    return (
        ref.authority_id,
        ref.authority_version,
        ref.semantic_hash_sha256,
    )


def _read_payload(
    store: ExactJsonAuthorityStore,
    ref: ExactExternalAuthorityRef,
) -> object | None:
    try:
        return store.read_payload(ref)
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class RegistrableSolverResult:
    """One persisted solver envelope's registration state for this document."""

    result_id: str
    source_entity_id: str | None
    receiver_entity_ids: tuple[str, ...]
    adapter_descriptor_id: str
    registered: bool
    eligible: bool
    reason: str | None
    snapshot_id: str | None = None
    scene_revision_id: str | None = None


class PredictionAuthorityLane:
    """Store-backed registration lane over one document's solver stack.

    The lane owns the resolver chain that lets the *real* snapshot /
    dispatch / result / provider repositories verify persisted solver rows
    inside the app: every resolver is derived from
    :class:`ExactJsonAuthorityStore` payloads or from authorities declared
    by the persisted models themselves.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        authority_root: Path | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        self.authority_store = ExactJsonAuthorityStore(
            authority_root
            if authority_root is not None
            else self.path.parent / DEFAULT_AUTHORITY_ROOT_NAME
        )
        self._environment_index: (
            dict[tuple[str, str, str], object] | None
        ) = None
        self._manifest_resolvers: dict[
            tuple[str, str, str], AcousticSolverArtifactManifestResolver
        ] = {}

        self.snapshot_repository = CadAcousticSnapshotRepository(
            scene_repository,
            fidelity_policy_resolver=self._fidelity_policy,
            authority_resolvers=AcousticSnapshotAuthorityResolvers(
                environment=self._environment,
                sound_speed_source=self._scalar('sound_speed_m_s'),
                temperature_source=self._scalar('temperature_c'),
                air_density_source=self._scalar('air_density_kg_m3'),
                air_pressure_source=self._scalar('air_pressure_pa'),
                relative_humidity_source=self._scalar(
                    'relative_humidity_percent'
                ),
                receiver_measurement=self._receiver_measurement,
                valid_frequency_domain=self._frequency_domain,
                geometric_topology_preflight=self._topology_preflight,
                external_authority=self.authority_store.resolve,
            ),
        )
        self.dispatch_repository = CadAcousticSolverDispatchRepository(
            scene_repository,
            snapshot_repository=self.snapshot_repository,
            external_authority_resolver=self.authority_store.resolve,
            fidelity_policy_resolver=self._fidelity_policy,
        )
        self.result_repository = CadAcousticSolverResultRepository(
            scene_repository,
            dispatch_resolver=self.dispatch_repository,
            request_resolver=self.snapshot_repository,
            external_authority_resolver=self.authority_store.resolve,
            artifact_manifest_resolver=self._artifact_manifest,
        )
        self.provider_repository = CadPredictionProviderRepository(
            scene_repository,
            snapshot_request_resolver=self.snapshot_repository,
            solver_result_resolver=self,
            external_payload_resolver=self.authority_store.read_payload,
        )
        self.matrix_repository = CadPredictionMatrixRepository(
            scene_repository
        )

    # -- persistence-verifying resolvers ------------------------------------

    def _payload_dict(
        self,
        ref: ExactExternalAuthorityRef,
    ) -> dict[str, Any] | None:
        payload = _read_payload(self.authority_store, ref)
        return payload if isinstance(payload, dict) else None

    def _environments(self) -> dict[tuple[str, str, str], object]:
        if self._environment_index is None:
            self._environment_index = {}
            connection = self.scene_repository._read()
            rows = connection.execute(
                f'SELECT payload_json FROM {_SNAPSHOT_TABLE}'
            ).fetchall()
            for row in rows:
                try:
                    snapshot = AcousticSceneSnapshot.model_validate_json(
                        row['payload_json']
                    )
                except ValidationError:
                    # A persisted payload that fails its own self-sealed
                    # schema never resolves anything.
                    continue
                environment = snapshot.environment
                if environment is not None:
                    self._environment_index[_ref_key(environment.authority)] = (
                        environment
                    )
        return self._environment_index

    def _environment(
        self, ref: ExactExternalAuthorityRef
    ) -> object | None:
        """Typed env ref declared by a persisted snapshot + sealed payload."""
        environment = self._environments().get(_ref_key(ref))
        if environment is None:
            return None
        return (
            environment
            if self.authority_store.resolve(ref) is not None
            else None
        )

    def _scalar(self, quantity: str):
        def resolve(ref: ExactExternalAuthorityRef) -> float | None:
            payload = self._payload_dict(ref)
            if payload is None or payload.get('quantity') != quantity:
                return None
            value = payload.get('value')
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
            ):
                return None
            return float(value)

        return resolve

    def _frequency_domain(
        self, ref: ExactExternalAuthorityRef
    ) -> FrequencyDomain | None:
        payload = self._payload_dict(ref)
        if payload is None:
            return None
        fields = {
            key: payload[key]
            for key in ('minimum_hz', 'maximum_hz')
            if key in payload
        }
        try:
            return FrequencyDomain.model_validate(fields)
        except ValidationError:
            return None

    def _receiver_measurement(
        self, ref: ExactExternalAuthorityRef
    ) -> ReceiverMeasurementAuthority | None:
        payload = self._payload_dict(ref)
        if payload is None:
            return None
        candidate = dict(payload)
        if 'authority_ref' not in candidate:
            candidate['authority_ref'] = ref.model_dump(mode='json')
        try:
            authority = ReceiverMeasurementAuthority.model_validate(candidate)
        except ValidationError:
            return None
        return authority if authority.authority_ref == ref else None

    def _fidelity_policy(
        self, ref: ExactExternalAuthorityRef
    ) -> AcousticNumericalFidelityPolicy | None:
        payload = self._payload_dict(ref)
        if payload is None:
            return None
        candidate = {
            key: payload[key]
            for key in _FIDELITY_POLICY_FIELDS
            if key in payload
        }
        candidate['authority_ref'] = payload.get(
            'authority_ref', ref.model_dump(mode='json')
        )
        try:
            policy = AcousticNumericalFidelityPolicy.model_validate(candidate)
        except ValidationError:
            return None
        return policy if policy.authority_ref == ref else None

    def _topology_preflight(
        self, ref: ExactExternalAuthorityRef
    ) -> GeometricPortalGraph | None:
        payload = self._payload_dict(ref)
        if payload is None:
            return None
        try:
            graph = GeometricPortalGraph.model_validate(payload)
        except ValidationError:
            return None
        return graph if graph.as_external_ref() == ref else None

    def _artifact_manifest(
        self, ref: ExactExternalAuthorityRef
    ):
        resolver = self._manifest_resolvers.get(_ref_key(ref))
        return None if resolver is None else resolver(ref)

    def _bind_artifact_manifests(
        self, envelope: AcousticSolverResultEnvelope
    ) -> None:
        for artifact in envelope.artifacts:
            self._manifest_resolvers[_ref_key(artifact.artifact_authority)] = (
                self.authority_store.solver_artifact_manifest_resolver(
                    encoding_schema_ref=artifact.encoding_schema_ref
                )
            )

    # -- solver-result resolver protocol ------------------------------------

    def get(self, result_id: str) -> AcousticSolverResultEnvelope | None:
        """``SolverResultResolver`` protocol entry for the provider repo."""
        row = self._result_row(result_id)
        if row is None:
            return None
        envelope = AcousticSolverResultEnvelope.model_validate_json(
            row['payload_json']
        )
        # The manifest resolver is keyed by each artifact's declared
        # encoding-schema binding — sealed inside the envelope itself.
        self._bind_artifact_manifests(envelope)
        return self.result_repository.get(result_id)

    def _result_row(self, result_id: str) -> sqlite3.Row | None:
        connection = self.scene_repository._read()
        return connection.execute(
            f'SELECT payload_json FROM {_RESULT_TABLE} WHERE result_id = ?',
            (result_id,),
        ).fetchone()

    # -- registration surfaces ----------------------------------------------

    def list_registrable_results(
        self, document_id: str
    ) -> tuple[RegistrableSolverResult, ...]:
        """Enumerate this document's persisted solver envelopes.

        ``eligible`` requires the full persisted chain to re-verify through
        the registered repositories *and* satisfy the R170A provider
        preconditions (exactly one source, declared environment, at least
        one receiver, a single complex_pressure artifact); ``reason`` keeps
        ineligible rows honest in the UI instead of dropping them.
        """
        connection = self.scene_repository._read()
        rows = connection.execute(
            f'SELECT r.result_id, r.payload_json FROM {_RESULT_TABLE} r '
            f'JOIN {_SNAPSHOT_TABLE} s '
            f'ON r.acoustic_scene_snapshot_id = s.snapshot_id '
            f'WHERE s.document_id = ? ORDER BY r.recorded_at_utc',
            (document_id,),
        ).fetchall()
        registered = {
            provider.result_envelope_id
            for provider in self.provider_repository.list_providers(
                document_id
            )
        }
        entries: list[RegistrableSolverResult] = []
        for row in rows:
            entries.append(
                self._registrable_entry(
                    row['result_id'], row['payload_json'], registered
                )
            )
        return tuple(entries)

    def _registrable_entry(
        self,
        result_id: str,
        payload_json: str,
        registered: frozenset[str] | set[str],
    ) -> RegistrableSolverResult:
        source_entity_id: str | None = None
        receiver_entity_ids: tuple[str, ...] = ()
        adapter_descriptor_id = ''
        snapshot_id: str | None = None
        scene_revision_id: str | None = None
        try:
            envelope = AcousticSolverResultEnvelope.model_validate_json(
                payload_json
            )
            adapter_descriptor_id = envelope.adapter_descriptor_id
            snapshot_id = envelope.acoustic_scene_snapshot_id
            self._bind_artifact_manifests(envelope)
            # Re-verify the persisted stack exactly as the provider
            # repository will on save and on every read.
            self.result_repository._validate(envelope)
            snapshot = self.snapshot_repository.get_snapshot(
                envelope.acoustic_scene_snapshot_id
            )
            if snapshot is None:
                raise ValueError('acoustic scene snapshot does not exist')
            scene_revision_id = snapshot.scene_revision_id
            request = self.snapshot_repository.get_prediction_request(
                envelope.prediction_request_id
            )
            if request is None:
                raise ValueError('acoustic prediction request does not exist')
            if len(snapshot.sources) != 1:
                raise ValueError(
                    'low-band prediction snapshot requires exactly one '
                    'acoustic scene source'
                )
            source_entity_id = snapshot.sources[0].source_entity_id
            if snapshot.environment is None:
                raise ValueError(
                    'low-band prediction snapshot requires a resolved '
                    'environment authority'
                )
            if not snapshot.receivers:
                raise ValueError(
                    'low-band prediction snapshot requires receivers'
                )
            receiver_entity_ids = tuple(
                receiver.receiver_id for receiver in snapshot.receivers
            )
            if len(envelope.artifacts) != 1:
                raise ValueError(
                    'low-band prediction requires exactly one solver artifact'
                )
            if envelope.artifacts[0].observable != 'complex_pressure':
                raise ValueError(
                    'low-band prediction artifact observable must be '
                    "'complex_pressure'"
                )
        except (ValidationError, ValueError) as exc:
            return RegistrableSolverResult(
                result_id=result_id,
                source_entity_id=source_entity_id,
                receiver_entity_ids=receiver_entity_ids,
                adapter_descriptor_id=adapter_descriptor_id,
                registered=result_id in registered,
                eligible=False,
                reason=str(exc),
                snapshot_id=snapshot_id,
                scene_revision_id=scene_revision_id,
            )
        return RegistrableSolverResult(
            result_id=result_id,
            source_entity_id=source_entity_id,
            receiver_entity_ids=receiver_entity_ids,
            adapter_descriptor_id=adapter_descriptor_id,
            registered=result_id in registered,
            eligible=True,
            reason=None,
            snapshot_id=snapshot_id,
            scene_revision_id=scene_revision_id,
        )

    def register_provider(
        self, result_id: str
    ) -> LowBandPredictionProvider:
        """Adapt one verified solver envelope into a candidate provider.

        The app can only mint ``candidate``/``unvalidated`` providers —
        ``validated``/``production`` require evidence refs no UI surface
        can honestly supply. All semantic gates run inside
        :func:`build_r130_low_band_prediction_provider` and
        :meth:`CadPredictionProviderRepository.save_provider`.
        """
        envelope = self.get(result_id)
        if envelope is None:
            raise ValueError('acoustic solver result does not exist')
        snapshot = self.snapshot_repository.get_snapshot(
            envelope.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError('acoustic scene snapshot does not exist')
        request = self.snapshot_repository.get_prediction_request(
            envelope.prediction_request_id
        )
        if request is None:
            raise ValueError('acoustic prediction request does not exist')
        revision = self.scene_repository.get(snapshot.scene_revision_id)
        if revision is None:
            raise ValueError('scene revision does not exist')
        provider = build_r130_low_band_prediction_provider(
            revision=revision,
            snapshot=snapshot,
            request=request,
            result=envelope,
            external_payload_resolver=self.authority_store.read_payload,
        )
        return self.provider_repository.save_provider(provider)

    def persist_matrix_run_verification(
        self,
        run_id: str,
    ) -> tuple[MatrixRunVerification, ExactExternalAuthorityRef]:
        """Verify-only persistence for one persisted matrix run (REV52).

        Re-derives every produced cell's result authority from the
        persisted provider records — the replay the matrix repository
        cannot do on reopen — and stores the verification record in the
        authority store. Nothing about the run or providers is mutated;
        cells that did not verify stay unclaimed. The returned ref is
        honest validation-authority material for provider promotion at
        candidate/fixture scope.
        """
        run = self.matrix_repository.get_run(run_id)
        if run is None:
            raise ValueError('matrix run is not persisted')
        spec = self.matrix_repository.get_spec(run.spec_id)
        result_set = next(
            (
                item
                for item in self.matrix_repository.list_result_sets(
                    run.spec_id
                )
                if item.semantic_sha256 == run.result_set_sha256
            ),
            None,
        )
        if spec is None or result_set is None:
            raise ValueError(
                'matrix run authorities are not fully persisted'
            )
        cell_source = {
            cell.cell_id: cell.matrix_source_id
            for cell in result_set.cells
        }
        providers: dict[str, LowBandPredictionProvider] = {}
        for transfer in result_set.transfers:
            ref = transfer.provider_ref
            if not isinstance(ref, PredictionProviderRef):
                continue
            source_id = cell_source.get(transfer.cell_id)
            if source_id is None:
                raise ValueError(
                    'matrix transfer references a cell outside the result set'
                )
            provider = self.provider_repository.get_provider(ref.provider_id)
            if (
                provider is None
                or provider.semantic_sha256 != ref.semantic_sha256
            ):
                raise ValueError(
                    'verified matrix cell provider is not persisted'
                )
            providers[source_id] = provider
        verification = build_matrix_run_verification(
            spec=spec,
            result_set=result_set,
            run=run,
            providers=providers,
        )
        # Persist the full record so reopen validates the hash-bound ids.
        ref = self.authority_store.put_json(
            'prediction-matrix-run-verification',
            MATRIX_RUN_VERIFICATION_AUTHORITY_VERSION,
            verification.model_dump(mode='json'),
        )
        return verification, ref

    def promote_provider_via_matrix_run(
        self,
        provider_id: str,
        verification_ref: ExactExternalAuthorityRef,
    ) -> LowBandPredictionProvider:
        """Promote a candidate provider on a persisted matrix verification.

        The verification record must exist in the authority store and must
        verify this provider with complete receiver coverage — a partial
        or absent coverage is no promotion evidence (fail closed). Matrix
        verification is not measured owned-room evidence, so the promotion
        lands exactly at ``validated``/``synthetic_fixture``.
        """
        provider = self.provider_repository.get_provider(provider_id)
        if provider is None:
            raise ValueError('prediction provider is not persisted')
        payload = self.authority_store.read_payload(verification_ref)
        verification = MatrixRunVerification.model_validate(payload)
        entry = verification.provider_entry(provider_id)
        if entry is None:
            raise ValueError(
                'matrix run verification does not cover this provider'
            )
        if not entry.coverage_complete:
            raise ValueError(
                'matrix run verification does not verify every receiver '
                'this provider covers'
            )
        envelope = self.get(provider.result_envelope_id)
        if envelope is None:
            raise ValueError('acoustic solver result does not exist')
        snapshot = self.snapshot_repository.get_snapshot(
            envelope.acoustic_scene_snapshot_id
        )
        if snapshot is None:
            raise ValueError('acoustic scene snapshot does not exist')
        request = self.snapshot_repository.get_prediction_request(
            envelope.prediction_request_id
        )
        if request is None:
            raise ValueError('acoustic prediction request does not exist')
        revision = self.scene_repository.get(snapshot.scene_revision_id)
        if revision is None:
            raise ValueError('scene revision does not exist')
        promoted = build_r130_low_band_prediction_provider(
            revision=revision,
            snapshot=snapshot,
            request=request,
            result=envelope,
            external_payload_resolver=self.authority_store.read_payload,
            evidence_state='validated',
            evidence_scope='synthetic_fixture',
            validation_authority_ref=verification_ref,
        )
        return self.provider_repository.save_provider(promoted)


__all__ = [
    'DEFAULT_AUTHORITY_ROOT_NAME',
    'PredictionAuthorityLane',
    'RegistrableSolverResult',
]
