from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import sqlite3
from threading import Event

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_acoustic_environment import (
    AcousticEnvironmentProfile,
    CadAcousticEnvironmentRepository,
)
from .cad_constraint_repository import CadConstraintRepository
from .cad_prediction_jobs import PredictionJobApplyContext, PredictionJobGuard, PredictionJobToken
from .cad_prediction_models import CadPredictionResult
from .cad_hybrid_prediction_provider import (
    CadHybridPredictionProviderRepository,
    HybridPredictionProvider,
)
from .cad_prediction_provider import (
    CadPredictionProviderRepository,
    LowBandPredictionProvider,
    PredictionProviderResolution,
)
from .cad_provider_response import (
    HYBRID_RESPONSE_MODEL_ID,
    PROVIDER_RESPONSE_MODEL_ID,
    ProviderResponseRequestIdentity,
    analyze_provider_frequency_response,
    embedded_run_provider,
    provider_response_request_identity,
)
from .cad_listener_pose import (
    CadListenerPoseRepository,
    ListenerPoseAuthority,
)
from .cad_prediction_repository import CadPredictionRepository
from .cad_prediction_request import (
    RectangularGeometryRequestIdentity,
    rectangular_geometry_environment_profile_ref,
    rectangular_geometry_request_identity,
)
from .cad_predictions import analyze_native_rectangular_geometry
from .cad_repository import SceneRepository, SceneRevision
from .cad_room_operating_state import (
    RoomOperatingState,
    compile_operating_state_consumption,
    evaluate_operating_state_freshness,
)
from .cad_room_operating_state_repository import (
    CadRoomOperatingStateRepository,
)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_scene import (
    acoustic_reference_position,
    is_listener_receiver_eligible,
    receiver_option_label,
    scene_content_hash,
)
from .cad_search_models import constraint_workspace_snapshot
from .native_worker import WORKER_CANCELLED, NativeWorker, NativeWorkerPool
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .user_facing_error import operation_error_message
from .room_prediction_options import (
    HYBRID_MODEL_KEY,
    HYBRID_MODEL_KEY_PREFIX,
    RECTANGULAR_MODEL_KEY,
    WAVE_MODEL_KEY_PREFIX,
    RoomPredictionModelOption,
    _provider_resolution,
    resolve_room_prediction_options,
)
from .room_prediction_target import RoomPredictionTarget
from .prediction_interpretation import (
    PredictionAuthorityRef,
    PredictionCapabilityItem,
    PredictionFinding,
    PredictionInterpretation,
    ProviderEvidence,
    interpret_prediction_results,
)
from .room_workspace import RoomWorkspaceController
from .ui_theme import (
    DARK_THEME,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)


@dataclass(frozen=True, slots=True)
class RoomPredictionRunSpec:
    revision: SceneRevision
    receiver_entity_id: str
    max_mode_hz: float
    sound_speed_m_s: float
    constraint_workspace_hash: str
    identity: RectangularGeometryRequestIdentity | ProviderResponseRequestIdentity
    token: PredictionJobToken
    environment_profile: ExactExternalAuthorityRef | None = None
    listener_pose: ListenerPoseAuthority | None = None
    operating_state: RoomOperatingState | None = None
    #: Exact proposed prediction target (#983). ``revision`` is always the
    #: baseline SceneRevision; when set, the materialized variant document
    #: supplies the source/receiver set and the request identity carries
    #: the exact variant id/hash — the variant is never applied to run.
    system_variant: SystemVariant | None = None
    #: #938 provider lane: the exact persisted R170A/R170B authority whose
    #: stored output this run consumes. ``None`` marks the rectangular lane.
    provider: 'LowBandPredictionProvider | HybridPredictionProvider | None' = None


@dataclass(frozen=True, slots=True)
class RoomPredictionRunState:
    busy: bool
    message: str
    error: bool = False


class RoomPredictionController(QObject):
    """N70 application adapter for the UX120 Room workspace.

    Identity, stale/cancel semantics and immutable persistence are delegated to the
    existing N70 request identity, PredictionJobGuard and CadPredictionRepository.
    """

    stateChanged = Signal(object)
    resultsChanged = Signal()
    runSelected = Signal(object)

    def __init__(
        self,
        scene_repository: SceneRepository,
        room_controller: RoomWorkspaceController,
        *,
        parent: QObject | None = None,
        operation: Callable[
            [RoomPredictionRunSpec, Event],
            tuple[CadPredictionResult, ...] | None,
        ] | None = None,
        environment_repository: CadAcousticEnvironmentRepository | None = None,
        provider_repository: CadPredictionProviderRepository | None = None,
        listener_pose_repository: CadListenerPoseRepository | None = None,
        operating_state_repository: CadRoomOperatingStateRepository | None = None,
        variant_repository: CadSystemVariantRepository | None = None,
        hybrid_provider_repository: CadHybridPredictionProviderRepository | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.room_controller = room_controller
        self.document_id = room_controller.document_id
        self.prediction_repository = CadPredictionRepository(scene_repository)
        self.constraint_repository = CadConstraintRepository(scene_repository.path)
        self.environment_repository = (
            environment_repository
            if environment_repository is not None
            else CadAcousticEnvironmentRepository(scene_repository.path)
        )
        self.provider_repository = provider_repository
        # #938: R170B providers supply the hybrid prediction lane options.
        self.hybrid_provider_repository = hybrid_provider_repository
        # #939: seat receivers resolve through the selected exact
        # ListenerPoseAuthority when one exists; legacy seat offset remains
        # only as the explicitly labelled fallback.
        self.listener_pose_repository = (
            listener_pose_repository
            if listener_pose_repository is not None
            else CadListenerPoseRepository(
                scene_repository.path, scene_repository
            )
        )
        # #941: prediction requests can bind the exact RoomOperatingState in
        # effect — state identity joins the request and only consumed domains
        # (openings→portal topology) change the effective solver input.
        self.operating_state_repository = (
            operating_state_repository
            if operating_state_repository is not None
            else CadRoomOperatingStateRepository(scene_repository)
        )
        # #983: a proposed SystemVariant is an exact prediction target — the
        # persisted proposal materializes over the baseline revision so room
        # prediction can evaluate it before apply.
        self._variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.job_guard = PredictionJobGuard()
        self._operation = operation or self._analyze
        self._tokens: dict[str, PredictionJobToken] = {}
        self._specs: dict[str, RoomPredictionRunSpec] = {}
        self._pool = NativeWorkerPool(self)
        self._completion_states: dict[str, RoomPredictionRunState] = {}
        self._current_job_id: str | None = None
        self._selected_run_id: str | None = None
        self._disposed = False

    @property
    def _tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live worker records owned by ``self._pool`` (kept for tests)."""
        return self._pool.tasks

    @property
    def active_worker_count(self) -> int:
        return self._pool.active_count

    @property
    def is_busy(self) -> bool:
        return self._current_job_id is not None or self.active_worker_count > 0

    @property
    def selected_run_id(self) -> str | None:
        return self._selected_run_id

    def receiver_options(
        self,
        *,
        include_source_receivers: bool = False,
    ) -> tuple[tuple[str, str], ...]:
        """Eligible prediction receivers: listener positions, never speakers.

        Source/receiver semantics are separate (#475): a speaker's acoustic
        reference is for directivity/excitation, not listening. Offering a
        speaker as receiver is only possible through the explicit diagnostic
        mode (``include_source_receivers``), which labels it as a source
        reference rather than a listening position.
        """
        document = self.room_controller.committed_document
        selected_poses = (
            self.listener_pose_repository.selected_poses_for_document(
                self.document_id
            )
        )
        options: list[tuple[str, str]] = []
        for entity in document.entities:
            pose = (
                selected_poses.get(entity.entity_id)
                if entity.kind == 'seat'
                else None
            )
            if is_listener_receiver_eligible(entity) or pose is not None:
                label = receiver_option_label(entity)
                if label is None:
                    label = entity.name
                if entity.kind == 'seat':
                    if pose is not None:
                        label += f" · 姿勢:{pose.label}"
                    else:
                        label += ' · 座席基準点(pose未選択)'
                options.append((entity.entity_id, label))
            elif (
                include_source_receivers
                and entity.kind == 'speaker'
                and acoustic_reference_position(entity) is not None
            ):
                options.append(
                    (
                        entity.entity_id,
                        f"{entity.name} · {entity.kind} · 診断: 音源基準点",
                    )
                )
        return tuple(options)

    def _constraint_hash(self) -> str:
        constraint_set = self.constraint_repository.load(self.document_id)
        _snapshot, digest = constraint_workspace_snapshot(constraint_set)
        return digest

    def _saved_target(
        self,
        receiver_entity_id: str,
        *,
        allow_source_receiver: bool = False,
        system_variant: SystemVariant | None = None,
    ) -> SceneRevision:
        working = self.room_controller.working
        if working.source_revision_id is None:
            raise ValueError("保存済みSceneRevisionが必要です")
        if working.has_preview:
            raise ValueError("編集中の操作を確定またはキャンセルしてください")
        if working.is_dirty:
            raise ValueError("予測の前に現在の配置を保存してください")
        revision = self.scene_repository.get(working.source_revision_id)
        if revision is None:
            raise ValueError("現在のSceneRevisionを読み込めません")
        if revision.content_hash != scene_content_hash(working.committed_document):
            raise ValueError("現在のSceneRevisionと編集状態が一致しません")
        document = revision.document
        if system_variant is not None:
            if system_variant.baseline_revision_id != revision.revision_id:
                raise ValueError(
                    "選択したSystemVariantは現在のSceneRevisionを基にしていません"
                )
            if system_variant.document_id != revision.document_id:
                raise ValueError(
                    "選択したSystemVariantは別のドキュメントに属します"
                )
            if system_variant.baseline_content_hash != revision.content_hash:
                raise ValueError(
                    "選択したSystemVariantのベース内容が一致しません"
                )
            document = materialize_system_variant(revision, system_variant)
        entity = document.entity(receiver_entity_id)
        if acoustic_reference_position(entity) is None and not (
            entity.kind == 'seat'
            and self._selected_pose(entity.entity_id) is not None
        ):
            raise ValueError("選択した受音点に音響基準点がありません")
        if entity.kind == 'speaker' and not allow_source_receiver:
            raise ValueError(
                "スピーカーは音源です。受音点として選択できません "
                "(診断モードのみ明示的に許可)"
            )
        return revision

    def _resolve_prediction_variant(
        self,
        system_variant_id: str | None,
    ) -> SystemVariant | None:
        """Exact persisted proposed prediction target (#983).

        The variant must exist in the repository — never synthesized or
        silently ignored — and its exact baseline binding is validated
        against the saved revision in ``_saved_target``.
        """

        if system_variant_id is None:
            return None
        variant = self._variant_repository.get_variant(system_variant_id)
        if variant is None:
            raise ValueError("選択したSystemVariantが存在しません")
        return variant

    def environment_profiles(self) -> tuple[AcousticEnvironmentProfile, ...]:
        return self.environment_repository.list_profiles()

    def selected_environment_profile(self) -> AcousticEnvironmentProfile | None:
        return self.environment_repository.selected_profile(self.document_id)

    def select_environment_profile(
        self,
        profile: AcousticEnvironmentProfile,
    ) -> None:
        self.environment_repository.select_profile(self.document_id, profile)

    def create_environment_profile(
        self,
        **kwargs: object,
    ) -> AcousticEnvironmentProfile:
        from .cad_acoustic_environment import build_acoustic_environment_profile

        profile = build_acoustic_environment_profile(**kwargs)
        self.environment_repository.save_profile(profile)
        return profile

    def _resolve_environment(
        self,
        environment_profile_id: str | None,
    ) -> AcousticEnvironmentProfile | None:
        if environment_profile_id is not None:
            profile = self.environment_repository.get_profile(environment_profile_id)
            if profile is None:
                raise ValueError("選択した環境プロファイルが存在しません")
            return profile
        return self.selected_environment_profile()

    def available_providers(self) -> tuple[LowBandPredictionProvider, ...]:
        if self.provider_repository is None:
            return ()
        return self.provider_repository.list_providers(self.document_id)

    def available_hybrid_providers(self) -> tuple[HybridPredictionProvider, ...]:
        if self.hybrid_provider_repository is None:
            return ()
        return self.hybrid_provider_repository.list_providers(self.document_id)

    def prediction_options(
        self,
        receiver_entity_id: str,
        *,
        max_mode_hz: float = 300.0,
        environment_profile_id: str | None = None,
        operating_state_id: str | None = None,
        operating_state_version: str | None = None,
        system_variant_id: str | None = None,
    ) -> tuple[RoomPredictionModelOption, ...]:
        """Solver-neutral model/provider option set for the Room flow (#457).

        Resolves READY/BLOCKED/UNSUPPORTED lanes with reasons — the product
        never assumes the rectangular model and never presents unvalidated
        providers as production capability.

        ``system_variant_id`` selects an exact proposed prediction target
        (#983): option lanes resolve against the materialized proposal — a
        provider whose authority binds the current revision shows explicit
        stale/unsupported reasons instead of silently predicting the
        baseline.
        """
        try:
            variant = self._resolve_prediction_variant(system_variant_id)
            revision = self._saved_target(
                receiver_entity_id,
                allow_source_receiver=True,
                system_variant=variant,
            )
        except (ValueError, KeyError) as exc:
            return (
                RoomPredictionModelOption(
                    model_key=RECTANGULAR_MODEL_KEY,
                    label='簡易矩形モデル',
                    state='BLOCKED',
                    reasons=(operation_error_message(exc),),
                ),
            )
        try:
            environment = self._resolve_environment(environment_profile_id)
        except ValueError:
            environment = None
        entity = revision.document.entity(receiver_entity_id)
        try:
            operating_state = self._resolve_operating_state(
                operating_state_id, operating_state_version
            )
        except ValueError:
            operating_state = None
        option_revision = revision
        if variant is not None:
            option_revision = RoomPredictionTarget(
                baseline_revision=revision,
                document=materialize_system_variant(revision, variant),
                system_variant=variant,
            ).input_revision
        return resolve_room_prediction_options(
            option_revision,
            receiver_entity_id,
            providers=self.available_providers(),
            hybrid_providers=self.available_hybrid_providers(),
            environment_profile=environment,
            max_mode_hz=max_mode_hz,
            listener_pose=self._selected_pose(entity.entity_id),
            operating_state=operating_state,
        )

    def _selected_pose(
        self,
        seat_entity_id: str,
    ) -> ListenerPoseAuthority | None:
        return self.listener_pose_repository.selected_pose(
            self.document_id,
            seat_entity_id,
        )

    def operating_state_options(
        self,
    ) -> tuple[tuple[str, str], ...]:
        """Persisted operating states for this document (#941).

        Keys are ``state_id`` (the latest version is used); labels surface
        read-only freshness against the working SceneRevision so the UI can
        show when a recorded state no longer maps onto the current room.
        """

        working = self.room_controller.working
        revision = (
            self.scene_repository.get(working.source_revision_id)
            if working.source_revision_id is not None
            else None
        )
        opening_ids = [
            opening.opening_id
            for opening in (
                revision.document.wall_topology.openings
                if revision is not None
                and revision.document.wall_topology is not None
                else ()
            )
        ]
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for state in self.operating_state_repository.list_states(
            self.document_id
        ):
            if state.state_id in seen:
                continue
            seen.add(state.state_id)
            label = f'{state.name} (v{state.version})'
            if revision is not None:
                freshness = evaluate_operating_state_freshness(
                    state,
                    scene_content_hash=revision.content_hash,
                    present_opening_ids=opening_ids,
                )
                if freshness.status == 'stale':
                    label += ' · 状態は古いシーンに固定されています'
                elif freshness.status == 'missing':
                    label += ' · 参照する開口部がありません'
            options.append((state.state_id, label))
        return tuple(options)

    def _resolve_operating_state(
        self,
        operating_state_id: str | None,
        operating_state_version: str | None,
    ) -> RoomOperatingState | None:
        if operating_state_id is None:
            return None
        if operating_state_version is None:
            versions = self.operating_state_repository.list_state_versions(
                operating_state_id
            )
            state = versions[-1] if versions else None
        else:
            state = self.operating_state_repository.get_state(
                operating_state_id, operating_state_version
            )
        if state is None or state.document_id != self.document_id:
            raise ValueError('選択した部屋状態が存在しません')
        return state

    def provider_view(
        self,
        provider_id: str,
    ) -> tuple[LowBandPredictionProvider, PredictionProviderResolution] | None:
        """One persisted provider plus its scene-level staleness resolution."""

        provider = next(
            (
                item
                for item in self.available_providers()
                if item.provider_id == provider_id
            ),
            None,
        )
        if provider is None:
            return None
        from .room_prediction_options import _provider_resolution

        revision = self.room_controller.working.source_revision_id
        scene_revision = (
            self.scene_repository.get(revision) if revision is not None else None
        )
        if scene_revision is None:
            scene_revision = self.scene_repository.get(
                provider.current_authority.scene_revision_id
            )
        if scene_revision is None:
            return None
        return provider, _provider_resolution(provider, scene_revision)

    def _provider_by_id(
        self,
        provider_id: str,
    ) -> 'LowBandPredictionProvider | HybridPredictionProvider | None':
        for provider in (
            *self.available_providers(),
            *self.available_hybrid_providers(),
        ):
            if provider.provider_id == provider_id:
                return provider
        return None

    def _prepare_provider_run(
        self,
        receiver_entity_id: str,
        *,
        model_key: str,
        max_mode_hz: float,
        allow_source_receiver: bool = False,
        system_variant_id: str | None = None,
    ) -> RoomPredictionRunSpec:
        """Prepare one provider-lane run (#938).

        Provider evidence is pinned to the baseline SceneRevision: a selected
        proposal has no provider evidence to consume, so the lane refuses
        instead of silently predicting the baseline while a variant is shown.
        """
        variant = self._resolve_prediction_variant(system_variant_id)
        if variant is not None:
            raise ValueError(
                'provider lane はベースのSceneRevisionに固定されています '
                '(提案variantにはprovider evidenceがありません)'
            )
        revision = self._saved_target(
            receiver_entity_id,
            allow_source_receiver=allow_source_receiver,
        )
        options = {
            option.model_key: option
            for option in resolve_room_prediction_options(
                revision,
                receiver_entity_id,
                providers=self.available_providers(),
                hybrid_providers=self.available_hybrid_providers(),
                max_mode_hz=max_mode_hz,
            )
        }
        option = options.get(model_key)
        if option is None or option.state != 'READY' or not option.runnable:
            raise ValueError(
                '選択したprediction laneはこのSceneRevisionでは実行できません'
            )
        provider = (
            None
            if option.provider_id is None
            else self._provider_by_id(option.provider_id)
        )
        if provider is None:
            raise ValueError('provider authorityが見つかりません')
        identity = provider_response_request_identity(
            provider,
            revision,
            receiver_entity_id,
            max_mode_hz=max_mode_hz,
        )
        constraint_hash = self._constraint_hash()
        token = self.job_guard.submit(
            revision,
            model_id=identity.model_id,
            model_version=identity.model_version,
            parameters_json=identity.parameters_json,
            input_hash=identity.input_hash,
            constraint_workspace_hash=constraint_hash,
        )
        return RoomPredictionRunSpec(
            revision=revision,
            receiver_entity_id=receiver_entity_id,
            max_mode_hz=float(max_mode_hz),
            sound_speed_m_s=0.0,
            constraint_workspace_hash=constraint_hash,
            identity=identity,
            token=token,
            provider=provider,
        )

    def prepare_run(
        self,
        receiver_entity_id: str,
        *,
        model_key: str = RECTANGULAR_MODEL_KEY,
        max_mode_hz: float = 300.0,
        sound_speed_m_s: float | None = None,
        environment_profile_id: str | None = None,
        operating_state_id: str | None = None,
        operating_state_version: str | None = None,
        allow_source_receiver: bool = False,
        system_variant_id: str | None = None,
    ) -> RoomPredictionRunSpec:
        if model_key != RECTANGULAR_MODEL_KEY:
            return self._prepare_provider_run(
                receiver_entity_id,
                model_key=model_key,
                max_mode_hz=max_mode_hz,
                allow_source_receiver=allow_source_receiver,
                system_variant_id=system_variant_id,
            )
        variant = self._resolve_prediction_variant(system_variant_id)
        revision = self._saved_target(
            receiver_entity_id,
            allow_source_receiver=allow_source_receiver,
            system_variant=variant,
        )
        environment = self._resolve_environment(environment_profile_id)
        environment_profile = (
            None if environment is None else environment.authority_ref()
        )
        if environment is not None:
            if environment.sound_speed_m_s is None:
                raise ValueError(
                    "選択中の環境プロファイルの音速が不明です "
                    "(不明な値を捏造せず予測をブロックします)"
                )
            sound_speed_m_s = environment.sound_speed_m_s
        resolved_sound_speed = (
            float(sound_speed_m_s) if sound_speed_m_s is not None else 343.0
        )
        target_document = revision.document
        if variant is not None:
            target_document = materialize_system_variant(revision, variant)
        receiver_entity = target_document.entity(receiver_entity_id)
        listener_pose = (
            self._selected_pose(receiver_entity.entity_id)
            if receiver_entity.kind == 'seat'
            else None
        )
        operating_state = self._resolve_operating_state(
            operating_state_id, operating_state_version
        )
        if (
            operating_state is not None
            and operating_state.scene_revision_id != revision.revision_id
        ):
            raise ValueError(
                '選択した部屋状態は現在のSceneRevisionに固定されていません '
                '(現在のシーン用の状態を選択してください)'
            )
        identity = rectangular_geometry_request_identity(
            revision,
            receiver_entity_id,
            max_mode_hz=max_mode_hz,
            sound_speed_m_s=resolved_sound_speed,
            environment_profile=environment_profile,
            listener_pose=listener_pose,
            operating_state=operating_state,
            system_variant=variant,
        )
        sound_speed_m_s = resolved_sound_speed
        constraint_hash = self._constraint_hash()
        token = self.job_guard.submit(
            revision,
            model_id=identity.model_id,
            model_version=identity.model_version,
            parameters_json=identity.parameters_json,
            input_hash=identity.input_hash,
            constraint_workspace_hash=constraint_hash,
        )
        return RoomPredictionRunSpec(
            revision=revision,
            receiver_entity_id=receiver_entity_id,
            max_mode_hz=float(max_mode_hz),
            sound_speed_m_s=float(sound_speed_m_s),
            constraint_workspace_hash=constraint_hash,
            identity=identity,
            token=token,
            environment_profile=environment_profile,
            listener_pose=listener_pose,
            operating_state=operating_state,
            system_variant=variant,
        )

    @staticmethod
    def _analyze(
        spec: RoomPredictionRunSpec,
        cancel_event: Event,
    ) -> tuple[CadPredictionResult, ...] | None:
        if cancel_event.is_set():
            return None
        if spec.provider is not None:
            return analyze_provider_frequency_response(
                spec.provider,
                spec.revision,
                spec.receiver_entity_id,
                max_mode_hz=spec.max_mode_hz,
                constraint_workspace_hash=spec.constraint_workspace_hash,
            )
        return analyze_native_rectangular_geometry(
            spec.revision,
            spec.receiver_entity_id,
            max_mode_hz=spec.max_mode_hz,
            sound_speed_m_s=spec.sound_speed_m_s,
            constraint_workspace_hash=spec.constraint_workspace_hash,
            environment_profile=spec.environment_profile,
            listener_pose=spec.listener_pose,
            operating_state=spec.operating_state,
            system_variant=spec.system_variant,
        )

    def start(
        self,
        receiver_entity_id: str,
        *,
        model_key: str = RECTANGULAR_MODEL_KEY,
        max_mode_hz: float = 300.0,
        environment_profile_id: str | None = None,
        operating_state_id: str | None = None,
        operating_state_version: str | None = None,
        allow_source_receiver: bool = False,
        system_variant_id: str | None = None,
    ) -> bool:
        if self._disposed:
            return False
        if model_key != RECTANGULAR_MODEL_KEY:
            options = {
                option.model_key: option
                for option in self.prediction_options(
                    receiver_entity_id,
                    max_mode_hz=max_mode_hz,
                    system_variant_id=system_variant_id,
                )
            }
            option = options.get(model_key)
            if option is None:
                message = "選択した予測モデルはこの画面では利用できません"
            elif option.state != 'READY' or not option.runnable:
                message = (
                    f"{option.label}: {option.state} · "
                    + " / ".join(option.reasons)
                )
            else:
                # #938: READY provider lanes execute — the run consumes the
                # exact persisted authority output through the same
                # guard/pool/persistence path as the rectangular lane.
                if self.is_busy:
                    self.stateChanged.emit(
                        RoomPredictionRunState(True, "予測を実行中です")
                    )
                    return False
                try:
                    spec = self._prepare_provider_run(
                        receiver_entity_id,
                        model_key=model_key,
                        max_mode_hz=max_mode_hz,
                        allow_source_receiver=allow_source_receiver,
                        system_variant_id=system_variant_id,
                    )
                except Exception as exc:
                    self.stateChanged.emit(
                        RoomPredictionRunState(
                            False,
                            f"予測を開始できません · {operation_error_message(exc)}",
                            error=True,
                        )
                    )
                    return False
                self._tokens[spec.token.job_id] = spec.token
                self._specs[spec.token.job_id] = spec
                self._current_job_id = spec.token.job_id
                self.stateChanged.emit(
                    RoomPredictionRunState(
                        True,
                        "登録済みprovider出力を参照しています…",
                    )
                )
                self._pool.start(
                    spec.token.job_id,
                    lambda cancel_event: self._operation(spec, cancel_event),
                    self._task_completed,
                    on_finished=self._task_thread_finished,
                )
                return True
            self.stateChanged.emit(
                RoomPredictionRunState(False, message, error=True)
            )
            return False
        if self.is_busy:
            self.stateChanged.emit(RoomPredictionRunState(True, "予測を実行中です"))
            return False
        try:
            spec = self.prepare_run(
                receiver_entity_id,
                max_mode_hz=max_mode_hz,
                environment_profile_id=environment_profile_id,
                operating_state_id=operating_state_id,
                operating_state_version=operating_state_version,
                allow_source_receiver=allow_source_receiver,
                system_variant_id=system_variant_id,
            )
        except Exception as exc:
            self.stateChanged.emit(
                RoomPredictionRunState(
                    False,
                    f"予測を開始できません · {operation_error_message(exc)}",
                    error=True,
                )
            )
            return False

        self._tokens[spec.token.job_id] = spec.token
        self._specs[spec.token.job_id] = spec
        self._current_job_id = spec.token.job_id
        self.stateChanged.emit(RoomPredictionRunState(True, "予測を計算しています…"))
        self._pool.start(
            spec.token.job_id,
            lambda cancel_event: self._operation(spec, cancel_event),
            self._task_completed,
            on_finished=self._task_thread_finished,
        )
        return True

    def cancel(self) -> bool:
        job_id = self._current_job_id
        if job_id is None:
            return False
        token = self._tokens.get(job_id)
        if token is not None:
            self.job_guard.cancel(token)
        self._pool.cancel(job_id)
        self._current_job_id = None
        self.stateChanged.emit(
            RoomPredictionRunState(
                True,
                "キャンセル処理中です。遅延結果は保存・適用しません",
            )
        )
        return True

    def _current_apply_context(self) -> PredictionJobApplyContext | None:
        working = self.room_controller.working
        if working.source_revision_id is None:
            return None
        return PredictionJobApplyContext(
            document_id=self.document_id,
            scene_revision_id=working.source_revision_id,
            scene_content_hash=scene_content_hash(working.committed_document),
            constraint_workspace_hash=self._constraint_hash(),
        )

    @staticmethod
    def _result_matches_token(
        result: CadPredictionResult,
        token: PredictionJobToken,
    ) -> bool:
        return (
            result.input_hash == token.input_hash
            and result.model_id == token.model_id
            and result.model_version == token.model_version
            and result.scene_revision_id == token.scene_revision_id
            and result.scene_content_hash == token.scene_content_hash
            and result.constraint_workspace_hash == token.constraint_workspace_hash
        )

    def accept_results(
        self,
        spec: RoomPredictionRunSpec,
        results: object,
    ) -> tuple[CadPredictionResult, ...] | None:
        token = spec.token
        if self.job_guard.is_cancelled(token):
            return None
        if (
            not isinstance(results, tuple)
            or not results
            or not all(isinstance(item, CadPredictionResult) for item in results)
        ):
            raise ValueError("prediction result contract mismatch")
        typed = results
        if any(not self._result_matches_token(item, token) for item in typed):
            raise ValueError("prediction immutable input identity mismatch")
        context = self._current_apply_context()
        if context is None or not self.job_guard.can_apply(token, context):
            return None
        self.prediction_repository.save_run(typed)
        self._selected_run_id = typed[0].run_id
        return typed

    @Slot(object, object, object)
    def _task_completed(self, key: object, result: object, error: object) -> None:
        if self._disposed:
            return
        job_id = str(key)
        token = self._tokens.pop(job_id, None)
        spec = self._specs.pop(job_id, None)
        if self._current_job_id == job_id:
            self._current_job_id = None
        if token is None or spec is None:
            self._completion_states[job_id] = RoomPredictionRunState(
                False,
                "予測処理を終了しました",
            )
            return

        final_state: RoomPredictionRunState
        if self.job_guard.is_cancelled(token) or error == WORKER_CANCELLED:
            final_state = RoomPredictionRunState(False, "予測はキャンセルされました")
        elif error is not None:
            final_state = RoomPredictionRunState(
                False,
                f"予測に失敗しました · {operation_error_message(error)}",
                error=True,
            )
        else:
            try:
                accepted = self.accept_results(spec, result)
            except ValueError as exc:
                final_state = RoomPredictionRunState(
                    False,
                    f"予測結果を拒否しました · {operation_error_message(exc)}",
                    error=True,
                )
            except sqlite3.Error as exc:
                final_state = RoomPredictionRunState(
                    False,
                    f"予測結果を保存できませんでした · {operation_error_message(exc)}",
                    error=True,
                )
            else:
                if accepted is None:
                    final_state = RoomPredictionRunState(
                        False,
                        "条件が変更されたため古い予測結果を破棄しました",
                    )
                else:
                    self.resultsChanged.emit()
                    self.runSelected.emit(accepted)
                    compatibility = accepted[0].geometry_compatibility
                    final_state = RoomPredictionRunState(
                        False,
                        (
                            "現在の部屋形状は矩形幾何modelの対象外です"
                            if compatibility == "unsupported"
                            else "予測を保存しました"
                        ),
                    )

        # Keep the worker visible as busy until QThread has actually emitted
        # finished. This prevents workspace disposal/restore/new-run races in the
        # short interval after the worker result signal but before thread teardown.
        self._completion_states[job_id] = final_state
        self.stateChanged.emit(
            RoomPredictionRunState(True, "予測処理を終了しています…")
        )

    def _task_thread_finished(self, job_id: str) -> None:
        """Emit the stashed final state once the worker thread has stopped."""
        if self._disposed:
            return
        final_state = self._completion_states.pop(
            job_id,
            RoomPredictionRunState(False, "予測処理を終了しました"),
        )
        self.stateChanged.emit(final_state)


    def list_run_ids(self) -> tuple[str, ...]:
        run_ids: list[str] = []
        for result in self.prediction_repository.list_results(self.document_id):
            if result.run_id not in run_ids:
                run_ids.append(result.run_id)
        return tuple(run_ids)

    def results_for_run(self, run_id: str) -> tuple[CadPredictionResult, ...]:
        return self.prediction_repository.list_run(run_id)

    def result_is_current(self, result: CadPredictionResult) -> bool:
        working = self.room_controller.working
        if not (
            working.source_revision_id == result.scene_revision_id
            and scene_content_hash(working.committed_document) == result.scene_content_hash
            and self._constraint_hash() == result.constraint_workspace_hash
        ):
            return False
        # #938 provider lanes embed the exact provider authority in the
        # canonical request — staleness against the current revision is
        # already the binding check; the rectangular-only environment ref
        # contract does not apply to their parameters.
        if result.model_id in (
            PROVIDER_RESPONSE_MODEL_ID,
            HYBRID_RESPONSE_MODEL_ID,
        ):
            return True
        # A run bound to an environment profile is stale once the document's
        # selected profile (or its versioned content) no longer matches (#479).
        bound_ref = rectangular_geometry_environment_profile_ref(result.parameters_json)
        selected = self.selected_environment_profile()
        bound_sha = None if bound_ref is None else bound_ref.semantic_hash_sha256
        selected_sha = (
            None if selected is None else selected.semantic_hash_sha256
        )
        return bound_sha == selected_sha

    def _provider_evidence(
        self,
        results: tuple[CadPredictionResult, ...],
    ) -> tuple[object, object] | None:
        """Provider authority behind one run, as ``(provider, resolution)``.

        Solver-neutral seam for the #457 provider path: the N70
        rectangular-geometry lane persists no provider authority and returns
        ``None``. #938 provider runs embed the exact authority in the
        canonical request — the same sealed payload is re-validated here so
        interpretation binds the run's own evidence, never a mutable
        repository lookup.
        """
        provider = embedded_run_provider(results[0])
        if provider is None:
            return None
        revision = self.scene_repository.get(results[0].scene_revision_id)
        if revision is None:
            return (provider, None)
        if isinstance(provider, LowBandPredictionProvider):
            return (provider, _provider_resolution(provider, revision))
        # R170B hybrid authorities carry their current pins under
        # ``base_current_authority`` and a different provider-ref shape than
        # the R170A resolution contract — project the evidence directly so
        # interpretation still binds the exact stored authority (#938).
        authority = provider.base_current_authority
        reasons: list[str] = []
        if authority.scene_revision_id != revision.revision_id:
            reasons.append('providerの基となったSceneRevisionではありません')
        elif authority.scene_content_hash != revision.content_hash:
            reasons.append('Scene内容がprovider作成後に変更されました')
        if authority.document_id != revision.document_id:
            reasons.append('providerが別のドキュメントに属します')
        domain = provider.valid_frequency_domain
        evidence = ProviderEvidence(
            provider_id=provider.provider_id,
            semantic_sha256=provider.semantic_sha256,
            evidence_state=provider.evidence_state,
            evidence_scope=provider.evidence_scope,
            stale_state='STALE' if reasons else 'CURRENT',
            stale_reasons=tuple(reasons),
            valid_band_hz=(
                float(domain.minimum_hz),
                float(domain.maximum_hz),
            ),
            capabilities=tuple(
                PredictionCapabilityItem(
                    observable=str(item.observable),
                    label=str(item.observable),
                    state=(
                        str(item.state)
                        if str(item.state) in ('READY', 'UNSUPPORTED')
                        else 'UNKNOWN'
                    ),
                    reason=item.reason,
                )
                for item in provider.observable_capabilities
            ),
            authority_refs=(
                PredictionAuthorityRef(
                    'prediction_provider',
                    provider.provider_id,
                    provider.semantic_sha256,
                ),
                PredictionAuthorityRef(
                    'scene_revision',
                    str(authority.scene_revision_id),
                    authority.scene_content_hash,
                ),
                PredictionAuthorityRef(
                    'acoustic_scene_snapshot',
                    str(authority.acoustic_scene_snapshot_id),
                    authority.acoustic_scene_snapshot_sha256,
                ),
            ),
            adapter_id=provider.adapter_id,
            adapter_version=provider.adapter_version,
            authority_version=provider.authority_version,
            source_entity_id=provider.source_entity_id,
            receiver_entity_ids=(
                provider.receiver_identity.receiver_binding.entity_id,
            ),
        )
        return (evidence, None)

    def interpretation_for(
        self,
        results: tuple[CadPredictionResult, ...],
    ) -> PredictionInterpretation | None:
        """Solver-neutral interpretation view model for one persisted run.

        The view model only summarizes stored evidence — run payloads, the
        canonical input snapshot, parameters, warnings/assumptions and any
        provider authority — so the UI explains the result without turning
        into prediction authority itself.
        """
        if not results:
            return None
        provider: object | None = None
        provider_resolution: object | None = None
        bundle = self._provider_evidence(results)
        if bundle is not None:
            provider, provider_resolution = bundle
        return interpret_prediction_results(
            results,
            is_current=self.result_is_current(results[0]),
            document=self.room_controller.committed_document,
            provider=provider,
            provider_resolution=provider_resolution,
        )

    def select_run(self, run_id: str | None) -> tuple[CadPredictionResult, ...]:
        self._selected_run_id = run_id
        results = () if run_id is None else self.results_for_run(run_id)
        self.runSelected.emit(results)
        return results

    def refresh_selection(self) -> tuple[CadPredictionResult, ...]:
        run_ids = self.list_run_ids()
        if self._selected_run_id not in run_ids:
            current_run = None
            for run_id in reversed(run_ids):
                results = self.results_for_run(run_id)
                if results and self.result_is_current(results[0]):
                    current_run = run_id
                    break
            self._selected_run_id = current_run or (run_ids[-1] if run_ids else None)
        return self.select_run(self._selected_run_id)

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.is_busy:
            return False, "予測の完了またはキャンセル後に画面を切り替えてください"
        return True, None

    def dispose(self) -> None:
        self._disposed = True
        for token in tuple(self._tokens.values()):
            self.job_guard.cancel(token)
        report = self._pool.shutdown()
        if not report.all_stopped:
            self.stateChanged.emit(
                RoomPredictionRunState(
                    False,
                    "予測処理の停止が遅延しています · 遅延結果は保存・適用しません",
                )
            )
        self._tokens.clear()
        self._specs.clear()
        self._completion_states.clear()
        self._current_job_id = None


class RoomPredictionPanel(QWidget):
    """Dark-first prediction controls for the Room acoustics context.

    The result surface is driven by the solver-neutral interpretation view
    model (Issue #469): finding cards, reliability/capability and neutral next
    steps come first; model/provider/hash provenance stays under Advanced.
    """

    runRequested = Signal()
    cancelRequested = Signal()
    findingSelected = Signal(object)

    _CAPABILITY_STATE_LABELS = {
        "READY": "あり",
        "UNSUPPORTED": "未評価",
        "UNKNOWN": "不明",
    }
    _APPROXIMATION_LABELS = {
        "exact_for_model_geometry": "対象geometryに対してexact",
        "rectangular_approximation": "矩形近似",
        "unsupported": "対象外",
    }
    _FINDING_TONE_COLORS = {
        "attention": DARK_THEME.semantic.warning.hex,
        "limitation": DARK_THEME.semantic.stale.hex,
    }

    def __init__(
        self,
        controller: RoomPredictionController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._interpretation: PredictionInterpretation | None = None
        self._options: dict[str, RoomPredictionModelOption] = {}
        self.setMinimumWidth(300)
        self.setMaximumWidth(390)
        set_surface_role(self, SurfaceRole.RAISED)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("予測")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)

        description = QLabel(
            "予測モデル/providerのcapabilityに応じて利用可能なlaneを表示します。"
            "実測FRやSPL音場ではありません。"
        )
        description.setWordWrap(True)
        set_typography_role(description, TypographyRole.SECONDARY)
        layout.addWidget(description)

        form = QFormLayout()
        self.model = QComboBox()
        self.model.setMinimumContentsLength(12)
        self.model.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.model.currentIndexChanged.connect(self._option_changed)
        form.addRow("モデル", self.model)
        self.receiver = QComboBox()
        self.receiver.setMinimumContentsLength(12)
        self.receiver.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.receiver.currentIndexChanged.connect(self._option_changed)
        form.addRow("受音点", self.receiver)
        self.max_mode = QDoubleSpinBox()
        self.max_mode.setRange(20.0, 1000.0)
        self.max_mode.setValue(300.0)
        self.max_mode.setSuffix(" Hz")
        form.addRow("モード上限", self.max_mode)
        self.environment = QComboBox()
        self.environment.setMinimumContentsLength(12)
        self.environment.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.environment.currentIndexChanged.connect(self._environment_changed)
        form.addRow("環境", self.environment)
        env_row = QHBoxLayout()
        self.environment_new = QPushButton("環境プロファイル新規…")
        self.environment_new.clicked.connect(self._new_environment_profile)
        env_row.addWidget(self.environment_new)
        env_row.addStretch(1)
        form.addRow("", env_row)
        self.source_receiver = QCheckBox(
            "診断: 音源を受音点として使う"
        )
        self.source_receiver.setToolTip(
            "診断/authoring専用モードです — speakerの音響基準点はリスニング位置ではありません"
        )
        self.source_receiver.toggled.connect(lambda _checked: self.refresh())
        form.addRow("", self.source_receiver)
        diagnostic_note = QLabel(
            "speakerの音響基準点はリスニング位置ではありません。"
        )
        diagnostic_note.setWordWrap(True)
        set_typography_role(diagnostic_note, TypographyRole.SECONDARY)
        form.addRow("", diagnostic_note)
        self.option_state = QLabel()
        self.option_state.setWordWrap(True)
        set_typography_role(self.option_state, TypographyRole.SECONDARY)
        form.addRow("状態", self.option_state)
        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.run_button = QPushButton("予測実行")
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_prediction)
        self.cancel_button.clicked.connect(controller.cancel)
        action_row.addWidget(self.run_button)
        action_row.addWidget(self.cancel_button)
        layout.addLayout(action_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        self.state = QLabel("保存済み予測なし")
        self.state.setWordWrap(True)
        body_layout.addWidget(self.state)

        # Primary summary: freshness / evidence / evaluated band / capability.
        self.reliability = QLabel()
        self.reliability.setWordWrap(True)
        body_layout.addWidget(self.reliability)

        findings_title = QLabel("所見")
        set_typography_role(findings_title, TypographyRole.SECONDARY)
        body_layout.addWidget(findings_title)

        self.findings = QListWidget()
        self.findings.setMinimumHeight(80)
        self.findings.setMaximumHeight(170)
        self.findings.itemSelectionChanged.connect(self._finding_selected)
        body_layout.addWidget(self.findings)

        self.finding_detail = QLabel(
            "所見を選択すると、根拠と空間リンクを表示します"
        )
        self.finding_detail.setWordWrap(True)
        set_typography_role(self.finding_detail, TypographyRole.SECONDARY)
        body_layout.addWidget(self.finding_detail)

        self.next_steps = QLabel()
        self.next_steps.setWordWrap(True)
        set_typography_role(self.next_steps, TypographyRole.SECONDARY)
        body_layout.addWidget(self.next_steps)

        history_title = QLabel("保存済み予測")
        set_typography_role(history_title, TypographyRole.SECONDARY)
        body_layout.addWidget(history_title)

        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["予測", "状態"])
        self.runs.setMinimumHeight(150)
        self.runs.itemSelectionChanged.connect(self._selected)
        body_layout.addWidget(self.runs)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("詳細 · 出典情報")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        body_layout.addWidget(self.advanced_toggle)

        self.advanced = QLabel()
        self.advanced.setWordWrap(True)
        set_typography_role(self.advanced, TypographyRole.SECONDARY)
        self.advanced.setVisible(False)
        body_layout.addWidget(self.advanced)
        body_layout.addStretch(1)

        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        controller.stateChanged.connect(self._state_changed)
        controller.resultsChanged.connect(self.refresh)
        controller.runSelected.connect(self.show_selected_results)
        self.refresh()

    def refresh(self) -> None:
        previous = self.controller.selected_run_id
        options = self.controller.receiver_options(
            include_source_receivers=self.source_receiver.isChecked()
        )
        previous_receiver = self.receiver.currentData()
        self.receiver.blockSignals(True)
        self.receiver.clear()
        for entity_id, label in options:
            self.receiver.addItem(label, entity_id)
        preferred = self.controller.room_controller.selected_id or previous_receiver
        if preferred is not None:
            index = self.receiver.findData(preferred)
            if index >= 0:
                self.receiver.setCurrentIndex(index)
        self.receiver.blockSignals(False)
        self._refresh_environments()
        self._refresh_models()

        self.runs.clear()
        selected_item = None
        run_ids = self.controller.list_run_ids()
        for index, run_id in enumerate(run_ids, start=1):
            results = self.controller.results_for_run(run_id)
            if not results:
                continue
            first = results[0]
            current = self.controller.result_is_current(first)
            compatibility = {
                "exact_for_model_geometry": "矩形model対応",
                "rectangular_approximation": "近似",
                "unsupported": "非対応",
            }.get(first.geometry_compatibility, first.geometry_compatibility)
            item = QTreeWidgetItem(
                [f"予測 {index}", "現在" if current else "要再計算"]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, run_id)
            item.setToolTip(0, compatibility)
            self.runs.addTopLevelItem(item)
            if run_id == previous:
                selected_item = item

        if selected_item is None and self.runs.topLevelItemCount():
            selected_item = self.runs.topLevelItem(self.runs.topLevelItemCount() - 1)
        if selected_item is not None:
            self.runs.setCurrentItem(selected_item)
        else:
            self.controller.select_run(None)
            self._show_results(())

    def run_prediction(self) -> bool:
        receiver = self.receiver.currentData()
        if receiver is None:
            self._state_changed(
                RoomPredictionRunState(False, "受音点を選択してください", error=True)
            )
            return False
        environment_id = self.environment.currentData()
        return self.controller.start(
            str(receiver),
            model_key=str(self.model.currentData() or RECTANGULAR_MODEL_KEY),
            max_mode_hz=float(self.max_mode.value()),
            environment_profile_id=(
                None if environment_id is None else str(environment_id)
            ),
            allow_source_receiver=self.source_receiver.isChecked(),
        )

    def _option_changed(self) -> None:
        receiver = self.receiver.currentData()
        model_key = str(self.model.currentData() or RECTANGULAR_MODEL_KEY)
        option = self._options.get(model_key)
        if option is None:
            self.option_state.setText("")
            return
        parts = [f"{option.label}: {option.state}"]
        if option.detail:
            parts.append(option.detail)
        if option.evidence_label:
            parts.append(f"evidence: {option.evidence_label}")
        if option.stale_state is not None:
            parts.append(f"鮮度: {option.stale_state}")
        if option.solver_label:
            parts.append(f"solver: {option.solver_label}")
        parts.extend(option.reasons)
        self.option_state.setText("\n".join(parts))
        if receiver is None or not option.runnable:
            self.run_button.setEnabled(False)
        else:
            self.run_button.setEnabled(option.state == 'READY')

    def _environment_changed(self) -> None:
        authority_id = self.environment.currentData()
        if authority_id is None:
            return
        profile = self.controller.environment_repository.get_profile(str(authority_id))
        if profile is None:
            return
        self.controller.select_environment_profile(profile)

    def _new_environment_profile(self) -> None:
        dialog = EnvironmentProfileDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        profile = self.controller.create_environment_profile(**dialog.profile_kwargs())
        self.controller.select_environment_profile(profile)
        self.refresh()
        index = self.environment.findData(profile.authority_id)
        if index >= 0:
            self.environment.setCurrentIndex(index)

    def _refresh_models(self) -> None:
        receiver = self.receiver.currentData()
        previous = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        self._options = {}
        if receiver is not None:
            for option in self.controller.prediction_options(str(receiver)):
                self._options[option.model_key] = option
                label = (
                    option.label
                    if option.state == 'READY'
                    else f"{option.label} ({option.state})"
                )
                self.model.addItem(label, option.model_key)
        if previous is not None:
            index = self.model.findData(previous)
            if index >= 0:
                self.model.setCurrentIndex(index)
        self.model.blockSignals(False)
        self._option_changed()

    def _refresh_environments(self) -> None:
        repository = self.controller.environment_repository
        if not repository.list_profiles():
            repository.ensure_default_profile()
        selected = self.controller.selected_environment_profile()
        if selected is None:
            # Display the shared default without claiming it — persisting a
            # selection on view would mark the project dirty for an edit the
            # operator never made (#915).
            selected = repository.ensure_default_profile()
        # The persisted selection is authoritative — preferring the combo's
        # currentData would resurrect a selection a Discard just rolled
        # back (#915).
        current = None if selected is None else selected.authority_id
        self.environment.blockSignals(True)
        self.environment.clear()
        for profile in self.controller.environment_profiles():
            speed = (
                "不明"
                if profile.sound_speed_m_s is None
                else f"{profile.sound_speed_m_s:g} m/s"
            )
            self.environment.addItem(
                f"{profile.label} · {speed} ({profile.sound_speed_source_kind})",
                profile.authority_id,
            )
        if current is not None:
            index = self.environment.findData(current)
            if index >= 0:
                self.environment.setCurrentIndex(index)
        self.environment.blockSignals(False)

    def _selected(self) -> None:
        item = self.runs.currentItem()
        run_id = (
            None
            if item is None
            else item.data(0, Qt.ItemDataRole.UserRole)
        )
        results = self.controller.select_run(None if run_id is None else str(run_id))
        self._show_results(results)

    def show_selected_results(self, results: object) -> None:
        if not isinstance(results, tuple):
            return
        self._show_results(results)

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced.setVisible(checked)

    def _band_text(self, interpretation: PredictionInterpretation) -> str:
        parts: list[str] = []
        for band in interpretation.reliability.valid_bands:
            low = "0" if band.minimum_hz is None else f"{band.minimum_hz:g}"
            high = "—" if band.maximum_hz is None else f"{band.maximum_hz:g}"
            parts.append(f"{band.label} {low}–{high} Hz")
        return " / ".join(parts) if parts else "評価帯域なし"

    def _capability_text(self, interpretation: PredictionInterpretation) -> str:
        return " · ".join(
            f"{item.label}:"
            f"{self._CAPABILITY_STATE_LABELS.get(item.state, item.state)}"
            for item in interpretation.reliability.capabilities
        )

    def _show_results(self, results: tuple[CadPredictionResult, ...]) -> None:
        interpretation = self.controller.interpretation_for(results)
        self._interpretation = interpretation
        if interpretation is None:
            self.reliability.setText("予測結果を選択してください")
            set_semantic_state(self.reliability, None)
            self.findings.clear()
            self.finding_detail.setText(
                "予測runを選択すると、所見・信頼性・次の一手を表示します"
            )
            self.next_steps.setText("")
            self.advanced.setText("")
            self.findingSelected.emit(None)
            return

        reliability = interpretation.reliability
        reliability_lines = [
            reliability.freshness_detail,
            f"evidence: {reliability.evidence_label}",
            f"評価帯域: {self._band_text(interpretation)}",
            (
                "近似: "
                + self._APPROXIMATION_LABELS.get(
                    reliability.approximation_state,
                    reliability.approximation_state,
                )
            ),
            f"能力: {self._capability_text(interpretation)}",
        ]
        if reliability.provider_stale_state is not None:
            stale = reliability.provider_stale_state
            reasons = ", ".join(reliability.provider_stale_reasons)
            reliability_lines.append(
                f"provider鮮度: {stale}" + (f" ({reasons})" if reasons else "")
            )
        self.reliability.setText("\n".join(reliability_lines))
        set_semantic_state(
            self.reliability,
            None if reliability.freshness == "current" else SemanticState.STALE,
        )

        self.findings.clear()
        for index, finding in enumerate(interpretation.findings):
            item = QListWidgetItem(finding.title)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(finding.detail)
            color = self._FINDING_TONE_COLORS.get(finding.tone)
            if color is not None:
                item.setForeground(QColor(color))
            self.findings.addItem(item)

        if interpretation.next_actions:
            action_lines = ["次の一手(すべて仮説・自動推奨ではありません):"]
            action_lines.extend(
                f"・{action.label} — {action.detail}"
                for action in interpretation.next_actions
            )
            self.next_steps.setText("\n".join(action_lines))
        else:
            self.next_steps.setText("次の一手: なし")
        self.advanced.setText("\n".join(interpretation.advanced_lines))
        self.finding_detail.setText(
            "所見を選択すると、根拠と空間リンクを表示します"
        )
        self.findingSelected.emit(None)

    def _finding_selected(self) -> None:
        interpretation = self._interpretation
        item = self.findings.currentItem()
        finding: PredictionFinding | None = None
        if item is not None and interpretation is not None:
            index = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(index, int) and 0 <= index < len(interpretation.findings):
                finding = interpretation.findings[index]
        if finding is None:
            self.finding_detail.setText(
                "所見を選択すると、根拠と空間リンクを表示します"
            )
        else:
            parts = [finding.detail]
            link = finding.spatial
            if link is not None:
                if link.kind == "reflection_path":
                    parts.append("3D: 対象の反射経路を強調表示します。")
                elif link.kind == "source":
                    parts.append("3D: speaker位置をマークします。")
                else:
                    parts.append("3D: 受音点位置をマークします。")
            if finding.authorities:
                ref = finding.authorities[0]
                parts.append(f"根拠: {ref.kind} {ref.ref_id[:12]}…")
            self.finding_detail.setText("\n".join(parts))
        self.findingSelected.emit(finding)

    def _state_changed(self, state: RoomPredictionRunState) -> None:
        self.state.setText(state.message)
        set_semantic_state(
            self.state,
            SemanticState.ERROR if state.error else None,
        )
        self.run_button.setEnabled(not state.busy)
        self.cancel_button.setEnabled(state.busy)
        if not state.busy:
            self.refresh()


class EnvironmentProfileDialog(QDialog):
    """Author one AcousticEnvironmentProfile with explicit provenance (#479)."""

    _SOURCE_KINDS = (
        ('nominal_assumption', '標準仮定 (343 m/s, 20 °C等)'),
        ('derived_from_temperature', '温度から導出 (c = 331.3 + 0.606·T)'),
        ('manual_measured', '手動測定値'),
        ('unknown', '不明'),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("環境プロファイル作成")
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.label = QLineEdit()
        self.label.setPlaceholderText("例: 測定日 2026-09 · 室内 22 °C")
        form.addRow("名称", self.label)

        self.source_kind = QComboBox()
        for value, label in self._SOURCE_KINDS:
            self.source_kind.addItem(label, value)
        self.source_kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("音速の出典", self.source_kind)

        self.sound_speed = QDoubleSpinBox()
        self.sound_speed.setRange(250.0, 400.0)
        self.sound_speed.setDecimals(2)
        self.sound_speed.setValue(343.0)
        self.sound_speed.setSuffix(" m/s")
        form.addRow("音速", self.sound_speed)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(-40.0, 60.0)
        self.temperature.setDecimals(1)
        self.temperature.setValue(20.0)
        self.temperature.setSuffix(" °C")
        self.temperature.valueChanged.connect(self._temperature_changed)
        form.addRow("温度", self.temperature)

        self.notes = QLineEdit()
        form.addRow("備考", self.notes)
        layout.addLayout(form)

        hint = QLabel(
            "音速/温度は必ず出典kindとセットで保存されます。"
            "「不明」は値を捏造せず、予測をブロックします。"
        )
        hint.setWordWrap(True)
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._kind_changed()

    def _kind_changed(self) -> None:
        kind = self.source_kind.currentData()
        self.sound_speed.setEnabled(kind in ('manual_measured',))
        self.temperature.setEnabled(kind == 'derived_from_temperature')
        if kind == 'nominal_assumption':
            self.sound_speed.setValue(343.0)
        elif kind == 'derived_from_temperature':
            self._temperature_changed()

    def _temperature_changed(self) -> None:
        if self.source_kind.currentData() == 'derived_from_temperature':
            from .cad_acoustic_environment import sound_speed_from_temperature_c

            self.sound_speed.setValue(
                sound_speed_from_temperature_c(float(self.temperature.value()))
            )

    def accept(self) -> None:
        if not self.label.text().strip():
            QMessageBox.warning(
                self, "環境プロファイル", "名称を入力してください"
            )
            self.label.setFocus()
            return
        super().accept()

    def profile_kwargs(self) -> dict[str, object]:
        kind = str(self.source_kind.currentData())
        kwargs: dict[str, object] = {
            'label': self.label.text().strip(),
            'sound_speed_source_kind': kind,
            'provenance': 'ユーザー作成プロファイル',
            'notes': self.notes.text().strip(),
        }
        if kind == 'unknown':
            return kwargs
        kwargs['sound_speed_m_s'] = float(self.sound_speed.value())
        if kind == 'derived_from_temperature':
            kwargs['temperature_c'] = float(self.temperature.value())
            kwargs['temperature_source_kind'] = 'manual_measured'
        elif self.temperature.isEnabled() or kind == 'nominal_assumption':
            kwargs['temperature_c'] = 20.0 if kind == 'nominal_assumption' else float(
                self.temperature.value()
            )
            kwargs['temperature_source_kind'] = (
                'nominal_assumption' if kind == 'nominal_assumption' else 'manual_measured'
            )
        return kwargs


__all__ = [
    "EnvironmentProfileDialog",
    "RoomPredictionController",
    "RoomPredictionPanel",
    "RoomPredictionRunSpec",
    "RoomPredictionRunState",
]
