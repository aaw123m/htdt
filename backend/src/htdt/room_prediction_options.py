"""Solver-neutral prediction orchestration for the Room acoustics context (#457).

The Room prediction flow must not assume the rectangular geometry model.
This module resolves the product-level model/provider option set for one exact
``SceneRevision`` + receiver: which lanes exist, which are READY/BLOCKED/
UNSUPPORTED and *why* — evidence scope, staleness, receiver membership,
observable capability and valid band — so the UI can offer user-level labels
(``簡易矩形モデル``, ``低域wave prediction``, ``hybrid prediction``) only where
the capability actually exists, and never silently stronger than the evidence.

Rectangular compute remains the only executable lane today; provider lanes are
consumption bindings over persisted R170A authority, surfaced with their exact
evidence state — a ``candidate``/``synthetic_fixture`` provider is labelled
development-only and never presented as production-eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from .cad_acoustic_environment import AcousticEnvironmentProfile
from .cad_display_labels import solver_reason_label
from .cad_listener_pose import (
    ListenerPoseAuthority,
    resolve_listener_receiver,
)
from .cad_room_operating_state import (
    RoomOperatingState,
    compile_operating_state_consumption,
)
from .cad_hybrid_prediction_provider import (
    HybridPredictionProvider,
)
from .cad_prediction_provider import (
    LowBandPredictionProvider,
    PredictionProviderResolution,
)
from .cad_predictions import exact_rectangular_room_frame
from .cad_repository import SceneRevision
from .cad_scene import (
    Position3,
    is_listener_receiver_eligible,
)
RECTANGULAR_MODEL_KEY = 'rectangular'
WAVE_MODEL_KEY_PREFIX = 'low-band-wave:'
HYBRID_MODEL_KEY = 'hybrid'
HYBRID_MODEL_KEY_PREFIX = 'hybrid:'

PredictionOptionState = Literal['READY', 'BLOCKED', 'UNSUPPORTED']

_PRODUCT_OBSERVABLE = 'frequency_response_magnitude'


@dataclass(frozen=True, slots=True)
class RoomPredictionModelOption:
    """One user-facing prediction capability resolved for the current scene."""

    model_key: str
    label: str
    state: PredictionOptionState
    reasons: tuple[str, ...] = ()
    detail: str = ''
    runnable: bool = False
    provider_id: str | None = None
    provider_sha256: str | None = None
    stale_state: Literal['CURRENT', 'STALE'] | None = None
    evidence_label: str | None = None
    solver_label: str | None = None


_EVIDENCE_LABELS = {
    ('candidate', 'unvalidated'): '開発用(候補・未検証)',
    ('validated', 'synthetic_fixture'): 'フィクスチャ検証済み(開発用)',
    ('validated', 'owned_room'): '実室検証済み',
    ('production', 'owned_room'): '本番採用済み',
}

_SOURCE_KIND_LABELS = {
    'nominal_assumption': '標準仮定',
    'derived_from_temperature': '温度導出',
    'derived_from_air_state': '空気状態導出',
    'manual_measured': '手動測定',
    'unknown': '不明',
}


def provider_model_key(provider_id: str) -> str:
    return f'{WAVE_MODEL_KEY_PREFIX}{provider_id}'


def hybrid_model_key(provider_id: str) -> str:
    return f'{HYBRID_MODEL_KEY_PREFIX}{provider_id}'


def provider_evidence_label(
    provider: 'LowBandPredictionProvider | HybridPredictionProvider',
) -> str:
    return _EVIDENCE_LABELS.get(
        (provider.evidence_state, provider.evidence_scope),
        f'{provider.evidence_state}/{provider.evidence_scope}',
    )


def _provider_resolution(
    provider: LowBandPredictionProvider,
    revision: SceneRevision,
) -> PredictionProviderResolution:
    """Scene-level staleness for the product view (snapshot-level checks stay
    on the binding authority)."""

    authority = provider.current_authority
    reasons: list[str] = []
    if authority.scene_revision_id != revision.revision_id:
        reasons.append('プロバイダーの基となったシーンリビジョンではありません')
    elif authority.scene_content_hash != revision.content_hash:
        reasons.append('シーン内容がプロバイダー作成後に変更されました')
    if authority.document_id != revision.document_id:
        reasons.append('プロバイダーが別のドキュメントに属します')
    return PredictionProviderResolution(
        provider_ref=provider.ref(),
        stale_state='STALE' if reasons else 'CURRENT',
        reasons=tuple(reasons),
    )


def _receiver_position(
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    listener_pose: ListenerPoseAuthority | None = None,
) -> Position3 | None:
    try:
        entity = revision.document.entity(receiver_entity_id)
    except KeyError:
        return None
    if not is_listener_receiver_eligible(entity) and not (
        entity.kind == 'seat' and listener_pose is not None
    ):
        return None
    resolved = resolve_listener_receiver(entity, listener_pose)
    return None if resolved is None else resolved.position


def _rectangular_option(
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    environment_profile: AcousticEnvironmentProfile | None,
    max_mode_hz: float,
    listener_pose: ListenerPoseAuthority | None = None,
    operating_state: RoomOperatingState | None = None,
) -> RoomPredictionModelOption:
    reasons: list[str] = []
    receiver = _receiver_position(
        revision,
        receiver_entity_id,
        listener_pose=listener_pose,
    )
    if receiver is None:
        try:
            entity = revision.document.entity(receiver_entity_id)
        except KeyError as exc:
            raise ValueError('選択した受音点が存在しません') from exc
        if entity.kind == 'speaker':
            reasons.append('スピーカーは音源です。受音点には選べません')
        else:
            reasons.append('選択した受音点に音響基準点がありません')
    room = revision.document.room
    if room is None:
        reasons.append('部屋形状がありません')
        frame = None
    else:
        frame = exact_rectangular_room_frame(room)
        if frame is None:
            return RoomPredictionModelOption(
                model_key=RECTANGULAR_MODEL_KEY,
                label='簡易矩形モデル',
                state='UNSUPPORTED',
                reasons=(
                    'この部屋形状は矩形モデルの対象外です'
                    '(部屋を矩形に変形する必要はありません — 別のモデルを選択してください)',
                ),
                detail='非矩形・任意形状には矩形近似を適用しません',
                solver_label='htdt.rectangular_geometry',
            )
    if receiver is not None and frame is not None:
        if not (
            frame.origin_x_m - 1e-9 <= receiver.x_m <= frame.origin_x_m + frame.width_m + 1e-9
            and frame.origin_y_m - 1e-9 <= receiver.y_m <= frame.origin_y_m + frame.depth_m + 1e-9
            and -1e-9 <= receiver.z_m <= frame.height_m + 1e-9
        ):
            reasons.append('受音点が矩形ルーム内部にありません')
    if environment_profile is not None and environment_profile.sound_speed_m_s is None:
        reasons.append('選択中の環境プロファイルの音速が不明です')
    if operating_state is not None and (
        operating_state.scene_revision_id != revision.revision_id
        or operating_state.document_id != revision.document_id
    ):
        reasons.append('選択した部屋状態は現在のシーンリビジョン用ではありません')
    if listener_pose is not None:
        detail_bits = [
            f'姿勢:{listener_pose.label} '
            f'({listener_pose.posture_kind}) · '
            'ポイント受音点は向きを使いません',
            f'モード上限 {max_mode_hz:g} Hz · 一次反射+モード近似',
        ]
    else:
        detail_bits = [f'モード上限 {max_mode_hz:g} Hz · 一次反射+モード近似']
    if operating_state is not None:
        consumption = compile_operating_state_consumption(
            operating_state, revision.document
        )
        consumed = ', '.join(consumption.consumed_domains) or 'なし'
        detail_bits.append(
            f'部屋状態:{operating_state.name} · 消費領域:{consumed}'
        )
    if environment_profile is not None:
        source = _SOURCE_KIND_LABELS.get(
            environment_profile.sound_speed_source_kind,
            environment_profile.sound_speed_source_kind,
        )
        detail_bits.append(
            f'環境 {environment_profile.label} ({source} '
            f'{environment_profile.sound_speed_m_s:g} m/s)'
        )
    if reasons:
        return RoomPredictionModelOption(
            model_key=RECTANGULAR_MODEL_KEY,
            label='簡易矩形モデル',
            state='BLOCKED',
            reasons=tuple(reasons),
            detail=' · '.join(detail_bits),
            solver_label='htdt.rectangular_geometry',
        )
    return RoomPredictionModelOption(
        model_key=RECTANGULAR_MODEL_KEY,
        label='簡易矩形モデル',
        state='READY',
        detail=' · '.join(detail_bits),
        runnable=True,
        solver_label='htdt.rectangular_geometry',
    )


def _provider_option(
    provider: LowBandPredictionProvider,
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float,
) -> RoomPredictionModelOption:
    evidence = provider_evidence_label(provider)
    resolution = _provider_resolution(provider, revision)
    reasons: list[str] = list(resolution.reasons)
    response = next(
        (
            item
            for item in provider.receiver_responses
            if item.receiver_entity_id == receiver_entity_id
        ),
        None,
    )
    if response is None:
        reasons.append('選択した受音点はこのプロバイダーの受音点集合に含まれません')
    capability = provider.capability(_PRODUCT_OBSERVABLE)
    if capability.state != 'READY':
        reasons.append(
            solver_reason_label(capability.reason)
            if capability.reason
            else '周波数応答観測量がプロバイダー外です'
        )
    domain = provider.valid_frequency_domain
    if float(max_mode_hz) > float(domain.maximum_hz):
        reasons.append(
            f'要求帯域 (~{max_mode_hz:g} Hz) がプロバイダー有効帯域 '
            f'({domain.minimum_hz:g}–{domain.maximum_hz:g} Hz) を超えます'
        )
    if float(max_mode_hz) < float(domain.minimum_hz):
        reasons.append(
            f'要求帯域 (~{max_mode_hz:g} Hz) がプロバイダー有効帯域 '
            f'({domain.minimum_hz:g}–{domain.maximum_hz:g} Hz) の下限を下回ります'
        )
    state: PredictionOptionState = 'BLOCKED' if reasons else 'READY'
    band = (
        f'帯域 {domain.minimum_hz:g}–{domain.maximum_hz:g} Hz · '
        f'{len(provider.receiver_responses)}受音点 · {evidence}'
    )
    return RoomPredictionModelOption(
        model_key=provider_model_key(provider.provider_id),
        label='低域波動予測',
        state=state,
        reasons=tuple(reasons),
        detail=band,
        # #938: a READY provider lane is runnable — the run consumes the
        # exact stored authority output for the selected receiver.
        runnable=state == 'READY',
        provider_id=provider.provider_id,
        provider_sha256=provider.semantic_sha256,
        stale_state=resolution.stale_state,
        evidence_label=evidence,
        solver_label=f'{provider.adapter_id} v{provider.adapter_version}',
    )


def _hybrid_option(
    provider: HybridPredictionProvider,
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float,
) -> RoomPredictionModelOption:
    evidence = provider_evidence_label(provider)
    authority = provider.base_current_authority
    reasons: list[str] = []
    if authority.scene_revision_id != revision.revision_id:
        reasons.append('プロバイダーの基となったシーンリビジョンではありません')
    elif authority.scene_content_hash != revision.content_hash:
        reasons.append('シーン内容がプロバイダー作成後に変更されました')
    if authority.document_id != revision.document_id:
        reasons.append('プロバイダーが別のドキュメントに属します')
    stale_state: Literal['CURRENT', 'STALE'] = (
        'STALE' if reasons else 'CURRENT'
    )
    if (
        provider.receiver_identity.receiver_binding.entity_id
        != receiver_entity_id
    ):
        reasons.append('選択した受音点はこのプロバイダーの受音点集合に含まれません')
    capability = provider.capability(_PRODUCT_OBSERVABLE)
    if capability.state != 'READY':
        reasons.append(
            solver_reason_label(capability.reason)
            if capability.reason
            else '周波数応答観測量がプロバイダー外です'
        )
    domain = provider.valid_frequency_domain
    if float(max_mode_hz) > float(domain.maximum_hz):
        reasons.append(
            f'要求帯域 (~{max_mode_hz:g} Hz) がプロバイダー有効帯域 '
            f'({domain.minimum_hz:g}–{domain.maximum_hz:g} Hz) を超えます'
        )
    if float(max_mode_hz) < float(domain.minimum_hz):
        reasons.append(
            f'要求帯域 (~{max_mode_hz:g} Hz) がプロバイダー有効帯域 '
            f'({domain.minimum_hz:g}–{domain.maximum_hz:g} Hz) の下限を下回ります'
        )
    state: PredictionOptionState = 'BLOCKED' if reasons else 'READY'
    band = (
        f'帯域 {domain.minimum_hz:g}–{domain.maximum_hz:g} Hz · '
        f'hybrid ({provider.blend_law}) · {evidence}'
    )
    return RoomPredictionModelOption(
        model_key=hybrid_model_key(provider.provider_id),
        label='ハイブリッド予測',
        state=state,
        reasons=tuple(reasons),
        detail=band,
        runnable=state == 'READY',
        provider_id=provider.provider_id,
        provider_sha256=provider.semantic_sha256,
        stale_state=stale_state,
        evidence_label=evidence,
        solver_label=f'{provider.adapter_id} v{provider.adapter_version}',
    )


def resolve_room_prediction_options(
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    providers: Sequence[LowBandPredictionProvider] = (),
    hybrid_providers: Sequence[HybridPredictionProvider] = (),
    environment_profile: AcousticEnvironmentProfile | None = None,
    max_mode_hz: float = 300.0,
    include_wave_placeholder: bool = True,
    include_hybrid_placeholder: bool = True,
    listener_pose: ListenerPoseAuthority | None = None,
    operating_state: RoomOperatingState | None = None,
) -> tuple[RoomPredictionModelOption, ...]:
    """The product model/provider option set for one receiver and revision.

    The rectangular lane is only READY when the geometry is an exact fit and
    the receiver/environment inputs resolve; provider lanes enumerate the
    persisted R170A catalog and report capability/evidence/staleness verbatim.
    Missing wave/hybrid coverage is surfaced as explicit UNSUPPORTED entries —
    the user is told the capability does not exist, never asked to reshape the
    room or trust an unvalidated lane.
    """

    options: list[RoomPredictionModelOption] = [
        _rectangular_option(
            revision,
            receiver_entity_id,
            environment_profile=environment_profile,
            max_mode_hz=max_mode_hz,
            listener_pose=listener_pose,
            operating_state=operating_state,
        )
    ]
    for provider in sorted(providers, key=lambda item: item.provider_id):
        options.append(
            _provider_option(
                provider,
                revision,
                receiver_entity_id,
                max_mode_hz=max_mode_hz,
            )
        )
    # #938: the hybrid lane enumerates the persisted R170B catalog — it is
    # permanently UNSUPPORTED only when no provider exists at all.
    for provider in sorted(hybrid_providers, key=lambda item: item.provider_id):
        options.append(
            _hybrid_option(
                provider,
                revision,
                receiver_entity_id,
                max_mode_hz=max_mode_hz,
            )
        )
    if include_wave_placeholder and not providers:
        options.append(
            RoomPredictionModelOption(
                model_key='low-band-wave',
                label='低域波動予測',
                state='UNSUPPORTED',
                reasons=(
                    'このドキュメントには低域予測プロバイダーが登録されていません '
                    '(R170A プロバイダー権威が必要です)',
                ),
                detail='波動レーン能力は登録済みプロバイダーで有効になります',
            )
        )
    if include_hybrid_placeholder and not hybrid_providers:
        options.append(
            RoomPredictionModelOption(
                model_key=HYBRID_MODEL_KEY,
                label='ハイブリッド予測',
                state='UNSUPPORTED',
                reasons=(
                    'このドキュメントにはハイブリッド予測プロバイダーが登録されていません '
                    '(R170B プロバイダー権威が必要です)',
                ),
                detail='ハイブリッドレーン能力は登録済みプロバイダーで有効になります',
            )
        )
    return tuple(options)


