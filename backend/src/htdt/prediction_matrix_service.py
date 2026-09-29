"""O531 Prediction Matrix product workflow (#986).

The service layer composes persisted matrix authorities — spec, execution
runs, result sets — into the product path: create a source x receiver
matrix, run it through the provider batching semantics, reopen historical
runs, and inspect per-cell state with dependency-aware currency. Qt widgets
never hold matrix authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .cad_equipment import FrequencyDomain
from .cad_prediction_matrix import (
    MatrixCurrency,
    MatrixExecutionRun,
    MatrixObservableContract,
    MatrixReceiverRef,
    MatrixSourceRef,
    PredictionMatrixSpec,
    TransferMatrixResultSet,
    assess_matrix_currency,
    build_prediction_matrix_spec,
    execute_prediction_matrix,
)
from .cad_prediction_matrix_repository import (
    CadPredictionMatrixRepository,
)
from .cad_prediction_provider import LowBandPredictionProvider
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .clock import utc_now_iso as _utc_now


@dataclass(frozen=True, slots=True)
class MatrixCellPresentation:
    matrix_source_id: str
    matrix_receiver_id: str
    state: str
    result_sha256: str | None
    blocked_reason: str | None
    stale: bool


@dataclass(frozen=True, slots=True)
class MatrixPresentation:
    """The source x receiver grid a Native panel renders."""

    spec_id: str | None
    spec_name: str
    source_labels: tuple[str, ...]
    receiver_labels: tuple[str, ...]
    cells: tuple[MatrixCellPresentation, ...] = ()
    currency_state: str | None = None
    stale_reasons: tuple[str, ...] = ()
    run_state: str | None = None
    run_attempt: int | None = None
    coherent_sum_eligible: bool | None = None
    reason: str | None = None


class PredictionMatrixService:
    """Native product surface for the Prediction Matrix workflow (#986)."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.repository = CadPredictionMatrixRepository(scene_repository)
        self.variant_repository = CadSystemVariantRepository(
            scene_repository
        )

    # ------------------------------------------------------------------
    # Spec authoring
    # ------------------------------------------------------------------

    def create_matrix(
        self,
        *,
        source_entity_ids: Sequence[str],
        receiver_ids: Sequence[str],
        providers: Mapping[str, LowBandPredictionProvider],
        frequency_axis_hz: Sequence[float],
        solver_implementation_ref: ExactExternalAuthorityRef,
        valid_frequency_domain: FrequencyDomain,
        require_coherent_sum_eligible: bool = False,
        system_variant_id: str | None = None,
    ) -> PredictionMatrixSpec:
        """Build and persist the canonical spec for one matrix.

        ``providers`` maps each source entity to the provider run bound to
        it — the matrix pins the provider's exact source/receiver binding
        hashes, so a changed binding stales only the affected cells (#986).
        """
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None:
            raise ValueError("行列を作成するSceneRevisionがありません。")
        if len(set(source_entity_ids)) != len(tuple(source_entity_ids)):
            raise ValueError('matrix sources must be unique')
        if len(set(receiver_ids)) != len(tuple(receiver_ids)):
            raise ValueError('matrix receivers must be unique')
        if len(source_entity_ids) < 1 or len(receiver_ids) < 1:
            raise ValueError(
                'a matrix requires at least one source and one receiver'
            )

        variant = None
        if system_variant_id is not None:
            variant = self.variant_repository.get_variant(system_variant_id)
            if variant is None:
                raise ValueError(
                    f"選択したSystemVariantが存在しません: {system_variant_id}"
                )
            if variant.baseline_content_hash != revision.content_hash:
                raise ValueError(
                    'SystemVariantは現在のSceneRevisionと一致しません。'
                )

        entity_ids = {
            entity.entity_id for entity in revision.document.entities
        }
        sources: list[MatrixSourceRef] = []
        snapshot_ref: tuple[str, str] | None = None
        for entity_id in source_entity_ids:
            if entity_id not in entity_ids:
                raise ValueError(
                    f"matrix source entityがシーンに存在しません: {entity_id}"
                )
            provider = providers.get(entity_id)
            if provider is None:
                raise ValueError(
                    f"matrix sourceに実行providerが割り当てられていません: "
                    f"{entity_id}"
                )
            bound_entity = (
                provider.source_identity.source_binding.source_entity_id
            )
            if bound_entity != entity_id:
                raise ValueError(
                    f"providerのソースバインドは{bound_entity}を参照しています"
                    f"（要求: {entity_id}）"
                )
            authority = provider.current_authority
            if authority.scene_content_hash != revision.content_hash:
                raise ValueError(
                    'providerのscene authorityが現在のSceneRevisionと'
                    '一致しません。再実行してください。'
                )
            pair = (
                authority.acoustic_scene_snapshot_id,
                authority.acoustic_scene_snapshot_sha256,
            )
            if snapshot_ref is None:
                snapshot_ref = pair
            elif snapshot_ref != pair:
                raise ValueError(
                    'matrix providersは同一のacoustic scene snapshotを'
                    '共有しなければなりません'
                )
            sources.append(
                MatrixSourceRef(
                    matrix_source_id=f'source:{entity_id}',
                    source_entity_id=entity_id,
                    source_binding_sha256=(
                        provider.source_identity.source_binding_sha256
                    ),
                    logical_channel_id=None,
                )
            )
        if snapshot_ref is None:
            raise ValueError('matrix requires at least one bound provider')

        receiver_bindings: dict[str, str] = {}
        for provider in providers.values():
            for identity in provider.receiver_identities:
                receiver_bindings[identity.receiver_binding.receiver_id] = (
                    identity.receiver_binding_sha256
                )
        receivers: list[MatrixReceiverRef] = []
        for receiver_id in receiver_ids:
            binding_sha = receiver_bindings.get(receiver_id)
            if binding_sha is None:
                raise ValueError(
                    f"matrix receiverにprovider receiver bindingがありません: "
                    f"{receiver_id}"
                )
            entity_id = next(
                (
                    identity.receiver_binding.entity_id
                    for provider in providers.values()
                    for identity in provider.receiver_identities
                    if identity.receiver_binding.receiver_id == receiver_id
                ),
                receiver_id,
            )
            seat = next(
                (
                    entity
                    for entity in revision.document.entities
                    if entity.entity_id == entity_id
                ),
                None,
            )
            receivers.append(
                MatrixReceiverRef(
                    matrix_receiver_id=f'receiver:{receiver_id}',
                    receiver_id=receiver_id,
                    receiver_entity_id=entity_id,
                    receiver_binding_sha256=binding_sha,
                    seat_label=None if seat is None else seat.name,
                )
            )

        spec = build_prediction_matrix_spec(
            document_id=self.document_id,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            acoustic_scene_snapshot_id=snapshot_ref[0],
            acoustic_scene_snapshot_sha256=snapshot_ref[1],
            variant_id=None if variant is None else variant.variant_id,
            variant_sha256=None if variant is None else variant.variant_sha256,
            solver_implementation_ref=solver_implementation_ref,
            valid_frequency_domain=valid_frequency_domain,
            sources=tuple(sources),
            receivers=tuple(receivers),
            observable_contract=MatrixObservableContract(
                frequency_axis_hz=tuple(float(v) for v in frequency_axis_hz),
                require_coherent_sum_eligible=(
                    require_coherent_sum_eligible
                ),
            ),
        )
        return self.repository.save_spec(spec)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_matrix(
        self,
        spec_id: str,
        providers: Mapping[str, LowBandPredictionProvider],
    ) -> MatrixExecutionRun:
        """Execute one persisted spec through the batching plan (#986).

        ``providers`` may be keyed by matrix_source_id or by source entity
        id; both resolve to the source's provider run.
        """
        spec = self.repository.get_spec(spec_id)
        if spec is None:
            raise ValueError(
                f"matrix specが存在しません: {spec_id}"
            )
        keyed: dict[str, LowBandPredictionProvider] = {}
        for source in spec.sources:
            provider = providers.get(
                source.matrix_source_id,
                providers.get(source.source_entity_id),
            )
            if provider is not None:
                keyed[source.matrix_source_id] = provider
        return execute_prediction_matrix(
            spec,
            keyed,
            repository=self.repository,
            started_at_utc=_utc_now(),
            finished_at_utc=_utc_now(),
        )

    def run_history(self, spec_id: str) -> tuple[MatrixExecutionRun, ...]:
        return self.repository.run_history(spec_id)

    # ------------------------------------------------------------------
    # Presentation / inspection
    # ------------------------------------------------------------------

    def _spec_labels(
        self, spec: PredictionMatrixSpec
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        revision = self.scene_repository.get(spec.scene_revision_id)
        names: dict[str, str] = {}
        if revision is not None:
            names = {
                entity.entity_id: entity.name
                for entity in revision.document.entities
            }
        source_labels = tuple(
            names.get(source.source_entity_id, source.matrix_source_id)
            for source in spec.sources
        )
        receiver_labels = tuple(
            receiver.seat_label
            or names.get(
                receiver.receiver_entity_id, receiver.matrix_receiver_id
            )
            for receiver in spec.receivers
        )
        return source_labels, receiver_labels

    def matrix_presentation(
        self, spec_id: str | None = None
    ) -> MatrixPresentation:
        """The inspectable matrix grid for the latest or named spec."""
        if spec_id is None:
            spec = self.repository.latest_spec(self.document_id)
            if spec is None:
                return MatrixPresentation(
                    spec_id=None,
                    spec_name='Prediction Matrix',
                    source_labels=(),
                    receiver_labels=(),
                    reason='保存済みmatrix specがありません。',
                )
        else:
            spec = self.repository.get_spec(spec_id)
            if spec is None:
                return MatrixPresentation(
                    spec_id=None,
                    spec_name='Prediction Matrix',
                    source_labels=(),
                    receiver_labels=(),
                    reason=f'matrix specが存在しません: {spec_id}',
                )
        source_labels, receiver_labels = self._spec_labels(spec)
        result_set = self.repository.latest_result_set(spec.spec_id)
        runs = self.repository.run_history(spec.spec_id)
        latest_run = runs[-1] if runs else None

        currency = None
        if result_set is not None:
            currency = self.assess_currency(
                spec, result_set=result_set
            )
        stale_ids = (
            set() if currency is None else set(currency.stale_cell_ids)
        )
        cells: list[MatrixCellPresentation] = []
        if result_set is None:
            for source in spec.sources:
                for receiver in spec.receivers:
                    cells.append(
                        MatrixCellPresentation(
                            matrix_source_id=source.matrix_source_id,
                            matrix_receiver_id=receiver.matrix_receiver_id,
                            state='QUEUED',
                            result_sha256=None,
                            blocked_reason=None,
                            stale=False,
                        )
                    )
        else:
            for cell in result_set.cells:
                cells.append(
                    MatrixCellPresentation(
                        matrix_source_id=cell.matrix_source_id,
                        matrix_receiver_id=cell.matrix_receiver_id,
                        state='STALE' if cell.cell_id in stale_ids else cell.state,
                        result_sha256=cell.result_sha256,
                        blocked_reason=cell.blocked_reason,
                        stale=cell.cell_id in stale_ids,
                    )
                )
        return MatrixPresentation(
            spec_id=spec.spec_id,
            spec_name=f"matrix {spec.spec_id[:29]}…",
            source_labels=source_labels,
            receiver_labels=receiver_labels,
            cells=tuple(cells),
            currency_state=None if currency is None else currency.state,
            stale_reasons=() if currency is None else currency.stale_reasons,
            run_state=None if latest_run is None else latest_run.state,
            run_attempt=None if latest_run is None else latest_run.attempt,
            coherent_sum_eligible=(
                None
                if result_set is None
                else result_set.coherent_sum_eligible
            ),
        )

    def cell_transfers(
        self,
        spec_id: str,
        matrix_source_id: str,
        matrix_receiver_id: str,
    ) -> tuple[float, tuple[float, ...], tuple[float, ...] | None] | None:
        """One cell's exact transfer evidence (frequency, magnitude, phase)."""
        result_set = self.repository.latest_result_set(spec_id)
        if result_set is None:
            return None
        try:
            transfer = result_set.transfer(
                matrix_source_id, matrix_receiver_id
            )
        except KeyError:
            return None
        return (
            transfer.pressure_reference_pa,
            tuple(transfer.frequency_hz),
            tuple(transfer.magnitude_pa),
        ) if transfer.phase_deg is None else (
            transfer.pressure_reference_pa,
            tuple(transfer.frequency_hz),
            tuple(transfer.magnitude_pa),
        )

    # ------------------------------------------------------------------
    # Currency
    # ------------------------------------------------------------------

    def assess_currency(
        self,
        spec: PredictionMatrixSpec | None = None,
        *,
        spec_id: str | None = None,
        result_set: TransferMatrixResultSet | None = None,
        providers: Mapping[str, LowBandPredictionProvider] | None = None,
    ) -> MatrixCurrency | None:
        """Dependency-aware staleness: shared deps stale all cells; a moved
        source binding stales its column; a moved receiver binding its row.
        """
        if spec is None:
            if spec_id is None:
                spec = self.repository.latest_spec(self.document_id)
            else:
                spec = self.repository.get_spec(spec_id)
            if spec is None:
                return None
        if result_set is None:
            result_set = self.repository.latest_result_set(spec.spec_id)
            if result_set is None:
                return None
        revision = self.scene_repository.current_head(self.document_id)
        current_scene_hash = (
            spec.scene_content_hash
            if revision is None
            else revision.content_hash
        )
        source_bindings: dict[str, str] = {}
        receiver_bindings: dict[str, str] = {}
        snapshot_shas: set[str] = set()
        providers_unverifiable = False
        if providers:
            for source in spec.sources:
                provider = providers.get(
                    source.matrix_source_id,
                    providers.get(source.source_entity_id),
                )
                if provider is not None:
                    source_bindings[source.matrix_source_id] = (
                        provider.source_identity.source_binding_sha256
                    )
            for provider in providers.values():
                for identity in provider.receiver_identities:
                    receiver_bindings[
                        identity.receiver_binding.receiver_id
                    ] = identity.receiver_binding_sha256
                authority = getattr(provider, 'current_authority', None)
                snapshot_sha = getattr(
                    authority, 'acoustic_scene_snapshot_sha256', None
                )
                if snapshot_sha is None:
                    providers_unverifiable = True
                else:
                    snapshot_shas.add(snapshot_sha)
        # The matrix pins a shared acoustic snapshot. Compare the pin
        # against the providers' current authority — a rotated or
        # divergent snapshot must fail closed, not echo the pin back.
        current_snapshot_sha256: str | None
        if not providers:
            # No provider evidence at all: snapshot drift is unverifiable,
            # same degradation as the skipped binding checks.
            current_snapshot_sha256 = spec.acoustic_scene_snapshot_sha256
        elif providers_unverifiable or len(snapshot_shas) != 1:
            current_snapshot_sha256 = None
        else:
            current_snapshot_sha256 = next(iter(snapshot_shas))
        return assess_matrix_currency(
            spec,
            result_set,
            current_scene_content_hash=current_scene_hash,
            current_snapshot_sha256=current_snapshot_sha256,
            current_source_bindings=(
                source_bindings if providers else None
            ),
            current_receiver_bindings=(
                receiver_bindings if providers else None
            ),
        )
