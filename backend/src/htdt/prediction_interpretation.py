from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from .cad_prediction_models import CadPredictionResult
from .cad_scene import Position3, SceneDocument, is_unassigned_speaker_role


"""Solver-neutral prediction interpretation view model (Issue #469).

Sits above the exact prediction/provider authorities (persisted
``CadPredictionResult`` runs, and optionally the R170A ``LowBandPredictionProvider``
path introduced by #457) and summarizes *stored* evidence into user-facing
findings, reliability/capability state, neutral next steps and Advanced
provenance.

Honesty contract: this layer never invents physics, severity or winners.
Every finding keeps the exact authority refs it was derived from, capability
claims come only from persisted assumptions/provider capability records, and
unknown/unavailable capability stays explicit instead of being hidden.
"""


PredictionFindingKind = Literal[
    'compatibility',
    'warning',
    'room_mode',
    'mode_cluster',
    'reflection_path',
    'coverage',
    'validation',
]
PredictionFindingTone = Literal['info', 'attention', 'limitation']
PredictionSpatialLinkKind = Literal['reflection_path', 'receiver', 'source']
PredictionCapabilityState = Literal['READY', 'UNSUPPORTED', 'UNKNOWN']
PredictionActionBasis = Literal['hypothesis', 'validated']

# How many individual findings of one kind are surfaced before the rest is
# folded into the coverage finding that always reports the persisted count.
_MAX_MODE_FINDINGS = 4
_MAX_CLUSTER_FINDINGS = 2
_MAX_REFLECTION_FINDINGS = 6
# Stored mode candidates whose neighbour spacing stays inside this window are
# reported as a concentration; the number is a presentation grouping only.
_MODE_CLUSTER_GAP_HZ = 5.0
_MODE_CLUSTER_MIN_SIZE = 3

_SURFACE_LABELS = {
    'left_x0': '左壁',
    'right_xW': '右壁',
    'front_y0': '前壁',
    'rear_yD': '後壁',
    'floor_z0': '床',
    'ceiling_zH': '天井',
}
_MODE_CLASS_LABELS = {
    'axial': '軸方向(axial)',
    'tangential': '接線(tangential)',
    'oblique': '斜め(oblique)',
}
_OBSERVABLE_LABELS = {
    'frequency_response_magnitude': '周波数応答・振幅',
    'frequency_response_phase': '周波数応答・位相',
    'impulse_response': 'インパルス応答',
    'rt60': '残響時間 RT60',
    'edt': '初期減衰時間 EDT',
    'c50': '明瞭度 C50',
    'c80': '明瞭度 C80',
    'arrival_timing': '到達時間',
    'spatial_pressure_field': '音場分布',
    'broadband_hybrid': '広帯域 hybrid',
}
_ASSUMPTION_LABELS = {
    'rectangular_room': '矩形部屋の仮定',
    'point_source_geometry': '点音源の幾何',
    'modal_frequency_only_amplitude_and_damping_not_modelled': (
        'モードは周波数のみ · 振幅/減衰は未評価'
    ),
    'specular_first_order_reflection_geometry': '鏡面一次反射の幾何のみ',
    'reflection_amplitude_and_phase_not_modelled': '反射の振幅/位相は未評価',
    'speaker_directivity_not_modelled': 'speaker指向性は未評価',
    'candidate_frequencies_are_not_measured_diagnoses': '候補周波数は実測診断ではない',
}
_EVIDENCE_STATE_LABELS = {
    'unvalidated': '実室 validation 未完了',
    'candidate': 'candidate · 実室 validation 未完了',
    'validated': 'validated',
    'production': 'production 採用済み',
}
_EVIDENCE_SCOPE_LABELS = {
    'unvalidated': '未検証',
    'synthetic_fixture': 'synthetic fixture evidence',
    'owned_room': '実室 evidence',
}


@dataclass(frozen=True, slots=True)
class PredictionAuthorityRef:
    """Exact persisted/replayable authority behind one interpretation element."""

    kind: str
    ref_id: str
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PredictionSpatialLink:
    """Spatial linkage authority for one finding.

    Positions/identities are copied verbatim from the persisted result payload
    (``reflections`` rows or the canonical ``input_snapshot_json``), so a view
    can highlight exactly what the stored evidence attests — nothing more.
    """

    kind: PredictionSpatialLinkKind
    speaker_entity_id: str | None = None
    receiver_entity_id: str | None = None
    surface_key: str | None = None
    surface_identity: str | None = None
    reflection_index: int | None = None
    source_position: Position3 | None = None
    receiver_position: Position3 | None = None
    reflection_position: Position3 | None = None


@dataclass(frozen=True, slots=True)
class PredictionFinding:
    """One user-facing finding card backed by exact authority refs."""

    finding_id: str
    kind: PredictionFindingKind
    tone: PredictionFindingTone
    title: str
    detail: str
    frequency_hz: float | None = None
    spatial: PredictionSpatialLink | None = None
    authorities: tuple[PredictionAuthorityRef, ...] = ()


@dataclass(frozen=True, slots=True)
class PredictionCapabilityItem:
    """One observable/capability state — explicit even when unsupported."""

    observable: str
    label: str
    state: PredictionCapabilityState
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PredictionValidBand:
    """Evaluated frequency band of one authority; ``None`` bound = unbounded."""

    label: str
    minimum_hz: float | None
    maximum_hz: float | None


@dataclass(frozen=True, slots=True)
class PredictionReliability:
    """Reliability/capability summary — states, never an opaque score."""

    freshness: Literal['current', 'stale']
    freshness_detail: str
    evidence_state: str
    evidence_scope: str | None
    evidence_label: str
    approximation_state: str
    valid_bands: tuple[PredictionValidBand, ...]
    capabilities: tuple[PredictionCapabilityItem, ...]
    provider_stale_state: str | None = None
    provider_stale_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PredictionNextAction:
    """Neutral next step — labelled hypothesis unless an evidence gate exists."""

    action_id: str
    label: str
    detail: str
    basis: PredictionActionBasis = 'hypothesis'
    related_finding_id: str | None = None
    focus_entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class PredictionInterpretation:
    """Complete normal-view interpretation of one prediction run."""

    findings: tuple[PredictionFinding, ...]
    reliability: PredictionReliability
    next_actions: tuple[PredictionNextAction, ...]
    advanced_lines: tuple[str, ...]

    def finding(self, finding_id: str) -> PredictionFinding | None:
        for finding in self.findings:
            if finding.finding_id == finding_id:
                return finding
        return None


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    """Solver-neutral projection of a prediction provider's stored evidence.

    ``provider_evidence()`` builds this from the R170A
    ``LowBandPredictionProvider`` (or any structurally compatible provider), so
    the interpretation stays solver-neutral while every field still comes from
    exact persisted authority.
    """

    provider_id: str
    semantic_sha256: str | None
    evidence_state: str
    evidence_scope: str
    stale_state: str  # 'CURRENT' | 'STALE' | 'unknown'
    stale_reasons: tuple[str, ...]
    valid_band_hz: tuple[float, float] | None
    capabilities: tuple[PredictionCapabilityItem, ...]
    authority_refs: tuple[PredictionAuthorityRef, ...]
    adapter_id: str | None = None
    adapter_version: str | None = None
    authority_version: str | None = None
    source_entity_id: str | None = None
    receiver_entity_ids: tuple[str, ...] = ()


def _observable_label(observable: str) -> str:
    return _OBSERVABLE_LABELS.get(observable, observable)


def provider_evidence(
    provider: object,
    resolution: object | None = None,
) -> ProviderEvidence:
    """Project a provider authority (e.g. ``LowBandPredictionProvider``) into
    solver-neutral evidence, plus an optional ``PredictionProviderResolution``
    for current/stale assessment. Reads are structural so the seam stays
    solver-neutral; every value is copied verbatim from stored fields."""

    capabilities = tuple(
        PredictionCapabilityItem(
            observable=str(item.observable),
            label=_observable_label(str(item.observable)),
            state=(
                str(item.state)
                if str(item.state) in ('READY', 'UNSUPPORTED')
                else 'UNKNOWN'
            ),
            reason=item.reason,
        )
        for item in provider.observable_capabilities
    )
    domain = provider.valid_frequency_domain
    band = (
        (float(domain.minimum_hz), float(domain.maximum_hz))
        if domain is not None
        else None
    )
    refs = [
        PredictionAuthorityRef(
            'prediction_provider',
            str(provider.provider_id),
            getattr(provider, 'semantic_sha256', None),
        ),
        PredictionAuthorityRef(
            'solver_result',
            str(provider.result_envelope_id),
            provider.result_envelope_sha256,
        ),
    ]
    current = getattr(provider, 'current_authority', None)
    source_entity_id = None
    if current is not None:
        source_entity_id = getattr(current, 'source_entity_id', None)
        refs.extend(
            (
                PredictionAuthorityRef(
                    'acoustic_scene_snapshot',
                    str(current.acoustic_scene_snapshot_id),
                    current.acoustic_scene_snapshot_sha256,
                ),
                PredictionAuthorityRef(
                    'prediction_request',
                    str(current.prediction_request_id),
                    current.prediction_request_sha256,
                ),
                PredictionAuthorityRef(
                    'scene_revision',
                    str(current.scene_revision_id),
                    current.scene_content_hash,
                ),
            )
        )
    receiver_entity_ids = tuple(
        str(item.receiver_entity_id) for item in provider.receiver_responses
    )
    if resolution is None:
        stale_state, stale_reasons = 'unknown', ()
    else:
        stale_state = str(resolution.stale_state)
        stale_reasons = tuple(str(reason) for reason in resolution.reasons)
    return ProviderEvidence(
        provider_id=str(provider.provider_id),
        semantic_sha256=getattr(provider, 'semantic_sha256', None),
        evidence_state=str(provider.evidence_state),
        evidence_scope=str(provider.evidence_scope),
        stale_state=stale_state,
        stale_reasons=stale_reasons,
        valid_band_hz=band,
        capabilities=capabilities,
        authority_refs=tuple(refs),
        adapter_id=getattr(provider, 'adapter_id', None),
        adapter_version=getattr(provider, 'adapter_version', None),
        authority_version=getattr(provider, 'authority_version', None),
        source_entity_id=source_entity_id,
        receiver_entity_ids=receiver_entity_ids,
    )


def _position(payload: object) -> Position3 | None:
    if not isinstance(payload, dict):
        return None
    try:
        return Position3(
            x_m=float(payload['x_m']),
            y_m=float(payload['y_m']),
            z_m=float(payload['z_m']),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _entity_name(document: SceneDocument | None, entity_id: str | None) -> str | None:
    if document is None or entity_id is None:
        return None
    try:
        entity = document.entity(entity_id)
    except KeyError:
        return None
    return entity.name or None


def _speaker_label(
    document: SceneDocument | None,
    entity_id: str,
    role: str | None,
) -> str:
    name = _entity_name(document, entity_id)
    base = name or entity_id
    if role is None or is_unassigned_speaker_role(role):
        return base
    return f'{base} ({role})'


def _result_authorities(result: CadPredictionResult) -> tuple[PredictionAuthorityRef, ...]:
    return (
        PredictionAuthorityRef('prediction_result', result.prediction_id, result.result_sha256),
        PredictionAuthorityRef('prediction_run', result.run_id),
        PredictionAuthorityRef('scene_revision', result.scene_revision_id, result.scene_content_hash),
    )


def _receiver_link(
    receiver_entity_id: str | None,
    receiver_position: Position3 | None,
) -> PredictionSpatialLink | None:
    if receiver_entity_id is None and receiver_position is None:
        return None
    return PredictionSpatialLink(
        kind='receiver',
        receiver_entity_id=receiver_entity_id,
        receiver_position=receiver_position,
    )


def _mode_clusters(frequencies: tuple[float, ...]) -> tuple[tuple[float, float, int], ...]:
    """Group sorted stored frequencies whose neighbours sit inside the window."""

    clusters: list[tuple[float, float, int]] = []
    if not frequencies:
        return ()
    group_start = frequencies[0]
    previous = frequencies[0]
    count = 1
    for frequency in frequencies[1:]:
        if frequency - previous <= _MODE_CLUSTER_GAP_HZ:
            previous = frequency
            count += 1
            continue
        if count >= _MODE_CLUSTER_MIN_SIZE:
            clusters.append((group_start, previous, count))
        group_start = previous = frequency
        count = 1
    if count >= _MODE_CLUSTER_MIN_SIZE:
        clusters.append((group_start, previous, count))
    return tuple(clusters)


def interpret_prediction_results(
    results: tuple[CadPredictionResult, ...],
    *,
    is_current: bool,
    document: SceneDocument | None = None,
    provider: object | None = None,
    provider_resolution: object | None = None,
) -> PredictionInterpretation | None:
    """Build the normal-view interpretation of one persisted prediction run.

    ``is_current`` is supplied by the caller, which owns the live
    scene/constraint comparison. ``provider`` accepts either a provider
    authority (e.g. the #457 ``LowBandPredictionProvider``) or a pre-built
    ``ProviderEvidence``; ``provider_resolution`` optionally carries the
    provider's current/stale assessment. Returns ``None`` for an empty run —
    the caller renders the "no selection" state.
    """

    if not results:
        return None
    first = results[0]
    try:
        snapshot = json.loads(first.input_snapshot_json)
    except (json.JSONDecodeError, TypeError):
        snapshot = None
    if not isinstance(snapshot, dict):
        # Fail closed: a result whose canonical input cannot be replayed into
        # the snapshot shape is still shown, but no spatial claims are made.
        snapshot = {}
        snapshot_available = False
    else:
        snapshot_available = True
    try:
        parameters = json.loads(first.parameters_json)
    except (json.JSONDecodeError, TypeError):
        parameters = None
    if not isinstance(parameters, dict):
        parameters = {}
    max_mode_hz = parameters.get('max_mode_hz')
    if isinstance(max_mode_hz, bool) or not isinstance(max_mode_hz, (int, float)):
        max_mode_hz = None

    receiver_entity_id = snapshot.get('receiver_entity_id')
    if not isinstance(receiver_entity_id, str) or not receiver_entity_id:
        receiver_entity_id = None
    receiver_position = _position(snapshot.get('receiver_position'))
    receiver_label = (
        _entity_name(document, receiver_entity_id)
        or receiver_entity_id
        or '受音点'
    )
    snapshot_speakers: dict[str, dict[str, object]] = {}
    for entry in snapshot.get('speakers') or ():
        if isinstance(entry, dict) and isinstance(entry.get('entity_id'), str):
            snapshot_speakers[entry['entity_id']] = entry

    run_refs = _result_authorities(first)
    compatibility = first.geometry_compatibility
    findings: list[PredictionFinding] = []

    if not snapshot_available:
        findings.append(
            PredictionFinding(
                finding_id='warning:input-snapshot',
                kind='warning',
                tone='limitation',
                title='入力snapshotを解釈できませんでした',
                detail=(
                    '保存された入力snapshotがcanonical model requestの形式ではないため、'
                    '空間リンクなしで結果だけを表示します。'
                ),
                authorities=run_refs,
            )
        )

    # --- geometry compatibility ------------------------------------------------
    if compatibility == 'unsupported':
        findings.append(
            PredictionFinding(
                finding_id='compatibility:unsupported',
                kind='compatibility',
                tone='limitation',
                title='現在の部屋形状はこのモデルの対象外です',
                detail=(
                    '矩形幾何モデルは軸平行の矩形部屋だけを評価します。'
                    'このrunにはモード候補も反射経路候補も含まれません。'
                ),
                spatial=_receiver_link(receiver_entity_id, receiver_position),
                authorities=run_refs,
            )
        )
    elif compatibility == 'rectangular_approximation':
        findings.append(
            PredictionFinding(
                finding_id='compatibility:approximation',
                kind='compatibility',
                tone='attention',
                title='結果は矩形近似に基づきます',
                detail=(
                    '実際の部屋形状を矩形に近似した入力に対する候補です。'
                    'exact authorityではありません。'
                ),
                spatial=_receiver_link(receiver_entity_id, receiver_position),
                authorities=run_refs,
            )
        )

    # --- stored warnings ---------------------------------------------------------
    seen_warnings: set[str] = set()
    for result in results:
        for warning in result.warnings:
            if warning in seen_warnings:
                continue
            seen_warnings.add(warning)
            if warning == (
                'rectangular_geometry_model_requires_axis_aligned_rectangular_room'
            ):
                continue  # covered by the compatibility finding
            if warning.startswith('speaker_acoustic_reference_outside_room:'):
                entity_id = warning.split(':', 1)[1]
                entry = snapshot_speakers.get(entity_id)
                source_position = (
                    _position(entry.get('acoustic_reference_position'))
                    if entry is not None
                    else None
                )
                label = _speaker_label(
                    document,
                    entity_id,
                    None if entry is None else entry.get('speaker_role'),
                )
                findings.append(
                    PredictionFinding(
                        finding_id=f'warning:{warning}',
                        kind='warning',
                        tone='attention',
                        title=f'speaker {label} の音響基準点が部屋の外です',
                        detail=(
                            'このspeakerの反射経路は計算されませんでした。'
                            '音響基準点の位置を確認してください。'
                        ),
                        spatial=PredictionSpatialLink(
                            kind='source',
                            speaker_entity_id=entity_id,
                            source_position=source_position,
                        ),
                        authorities=_result_authorities(result),
                    )
                )
                continue
            if warning.startswith('speaker_acoustic_reference_unknown:'):
                entity_id = warning.split(':', 1)[1]
                entry = snapshot_speakers.get(entity_id)
                label = _speaker_label(
                    document,
                    entity_id,
                    None if entry is None else entry.get('speaker_role'),
                )
                findings.append(
                    PredictionFinding(
                        finding_id=f'warning:{warning}',
                        kind='warning',
                        tone='attention',
                        title=f'speaker {label} の音響基準点が未設定です',
                        detail=(
                            '音響基準点がないため、このspeakerの一次反射経路候補は'
                            '計算されませんでした。'
                        ),
                        spatial=PredictionSpatialLink(
                            kind='source',
                            speaker_entity_id=entity_id,
                        ),
                        authorities=_result_authorities(result),
                    )
                )
                continue
            findings.append(
                PredictionFinding(
                    finding_id=f'warning:{warning}',
                    kind='warning',
                    tone='attention',
                    title=warning,
                    detail='modelが記録した警告です。',
                    spatial=_receiver_link(receiver_entity_id, receiver_position),
                    authorities=_result_authorities(result),
                )
            )

    # --- room modes -------------------------------------------------------------
    modes_result = next(
        (item for item in results if item.result_kind == 'geometry_modes'), None
    )
    reflections_result = next(
        (item for item in results if item.result_kind == 'geometry_reflections'),
        None,
    )
    if modes_result is not None and modes_result.modes:
        modes = modes_result.modes
        for index, mode in enumerate(modes[:_MAX_MODE_FINDINGS]):
            mode_class = _MODE_CLASS_LABELS.get(mode.mode_class, mode.mode_class)
            findings.append(
                PredictionFinding(
                    finding_id=f'mode:{mode.n_x},{mode.n_y},{mode.n_z}',
                    kind='room_mode',
                    tone='info',
                    title=(
                        f'{mode.frequency_hz:.1f} Hz 付近に{mode_class}'
                        f'モード候補 ({mode.n_x},{mode.n_y},{mode.n_z})'
                    ),
                    detail=(
                        f'{receiver_label} を受音点とした部屋の幾何学的モード候補です。'
                        '振幅・減衰・実測との一致は未評価です。'
                    ),
                    frequency_hz=mode.frequency_hz,
                    spatial=_receiver_link(receiver_entity_id, receiver_position),
                    authorities=_result_authorities(modes_result),
                )
            )
        for cluster_index, (low, high, count) in enumerate(
            _mode_clusters(tuple(mode.frequency_hz for mode in modes))[
                :_MAX_CLUSTER_FINDINGS
            ]
        ):
            findings.append(
                PredictionFinding(
                    finding_id=f'mode-cluster:{cluster_index}',
                    kind='mode_cluster',
                    tone='attention',
                    title=f'{low:.1f}–{high:.1f} Hz に {count} 件のモード候補が集中',
                    detail=(
                        '列挙されたモード候補がこの帯域に集中しています'
                        '(保存済み候補の集計)。各候補の振幅は未評価です。'
                    ),
                    frequency_hz=low,
                    spatial=_receiver_link(receiver_entity_id, receiver_position),
                    authorities=_result_authorities(modes_result),
                )
            )
        if max_mode_hz is not None:
            findings.append(
                PredictionFinding(
                    finding_id='coverage:modes-band',
                    kind='coverage',
                    tone='limitation',
                    title=(
                        f'room mode候補は {float(max_mode_hz):.0f} Hz まで列挙 '
                        f'({len(modes)} 件) · それ以上は未評価'
                    ),
                    detail=(
                        f'この実行のモード上限は {float(max_mode_hz):.0f} Hz です。'
                        '上限を変えて再計算すると別の帯域も候補に入ります。'
                    ),
                    frequency_hz=float(max_mode_hz),
                    spatial=_receiver_link(receiver_entity_id, receiver_position),
                    authorities=_result_authorities(modes_result),
                )
            )
    elif modes_result is not None and compatibility != 'unsupported':
        findings.append(
            PredictionFinding(
                finding_id='coverage:modes-empty',
                kind='coverage',
                tone='info',
                title='評価帯域内にroom mode候補はありません',
                detail=(
                    'モデルの列挙条件内ではモード候補が見つかりませんでした。'
                    '帯域上限を上げると候補が現れる場合があります。'
                ),
                spatial=_receiver_link(receiver_entity_id, receiver_position),
                authorities=_result_authorities(modes_result),
            )
        )

    # --- first-order reflections --------------------------------------------------
    if reflections_result is not None and reflections_result.reflections:
        stored = reflections_result.reflections
        earliest_index = min(
            range(len(stored)), key=lambda index: stored[index].excess_delay_ms
        )
        by_delay = sorted(
            range(len(stored)), key=lambda index: stored[index].excess_delay_ms
        )
        for index in by_delay[:_MAX_REFLECTION_FINDINGS]:
            reflection = stored[index]
            surface_label = _SURFACE_LABELS.get(
                reflection.surface_key, reflection.surface_key
            )
            speaker_label = _speaker_label(
                document,
                reflection.speaker_entity_id,
                reflection.speaker_role,
            )
            detail_parts = [
                (
                    f'{receiver_label} へ direct より '
                    f'+{reflection.excess_delay_ms:.2f} ms 遅れて到達する'
                    '幾何経路候補です。'
                )
            ]
            if index == earliest_index:
                detail_parts.append('このrunで最も早く到達する反射経路候補です。')
            if reflection.first_destructive_hz is not None:
                detail_parts.append(
                    '経路差から約 '
                    f'{reflection.first_destructive_hz:.0f} Hz 付近に'
                    '一次の干渉候補周波数が計算されています。'
                )
            detail_parts.append('振幅・位相は未評価です。')
            detail_parts.append(f'面: {reflection.surface_identity}')
            findings.append(
                PredictionFinding(
                    finding_id=f'reflection:{index}',
                    kind='reflection_path',
                    tone='attention' if index == earliest_index else 'info',
                    title=(
                        f'{speaker_label} → {surface_label} の一次反射経路候補'
                    ),
                    detail=''.join(detail_parts),
                    spatial=PredictionSpatialLink(
                        kind='reflection_path',
                        speaker_entity_id=reflection.speaker_entity_id,
                        receiver_entity_id=receiver_entity_id,
                        surface_key=reflection.surface_key,
                        surface_identity=reflection.surface_identity,
                        reflection_index=index,
                        source_position=reflection.source_position,
                        receiver_position=reflection.receiver_position,
                        reflection_position=reflection.reflection_position,
                    ),
                    authorities=_result_authorities(reflections_result),
                )
            )
        findings.append(
            PredictionFinding(
                finding_id='coverage:reflections',
                kind='coverage',
                tone='info',
                title=(
                    f'一次反射の幾何経路候補は全部で {len(stored)} 件 · '
                    '振幅/位相なし'
                ),
                detail=(
                    '鏡面一次反射の経路幾何だけの候補です。'
                    '反射の強さや位相応答はこのモデルでは評価されません。'
                ),
                spatial=_receiver_link(receiver_entity_id, receiver_position),
                authorities=_result_authorities(reflections_result),
            )
        )

    # --- validation state ----------------------------------------------------------
    findings.append(
        PredictionFinding(
            finding_id='validation:geometry',
            kind='validation',
            tone='limitation',
            title='この結果は幾何学的候補で、実室 validation は未完了です',
            detail=(
                'room mode周波数と一次反射経路は幾何入力からの候補であり、'
                '実測・実室との照合 evidence はありません。'
            ),
            spatial=_receiver_link(receiver_entity_id, receiver_position),
            authorities=run_refs,
        )
    )

    # --- provider path (#457) ---------------------------------------------------
    evidence: ProviderEvidence | None
    if provider is None:
        evidence = None
    elif isinstance(provider, ProviderEvidence):
        evidence = provider
    else:
        evidence = provider_evidence(provider, provider_resolution)

    if evidence is not None:
        if evidence.valid_band_hz is not None:
            low_hz, high_hz = evidence.valid_band_hz
            findings.append(
                PredictionFinding(
                    finding_id='coverage:provider-band',
                    kind='coverage',
                    tone='info',
                    title=(
                        f'{low_hz:.0f}–{high_hz:.0f} Hz に wave prediction の'
                        '結果があります'
                    ),
                    detail='providerの有効帯域内の評価結果です。',
                    authorities=evidence.authority_refs,
                )
            )
            findings.append(
                PredictionFinding(
                    finding_id='coverage:provider-above-band',
                    kind='coverage',
                    tone='limitation',
                    title=(
                        f'{high_hz:.0f} Hz より上はこの provider の有効帯域外です'
                    ),
                    detail='有効帯域外の周波数は評価されていません。',
                    authorities=evidence.authority_refs,
                )
            )
        scope_label = _EVIDENCE_SCOPE_LABELS.get(
            evidence.evidence_scope, evidence.evidence_scope
        )
        if evidence.evidence_state in ('candidate', 'unvalidated'):
            findings.append(
                PredictionFinding(
                    finding_id='validation:provider',
                    kind='validation',
                    tone='limitation',
                    title='provider 結果は candidate · 実室 validation 未完了です',
                    detail=(
                        'solver/provider の結果は candidate 状態で、'
                        '実室での照合 evidence はまだありません。'
                    ),
                    authorities=evidence.authority_refs,
                )
            )
        elif evidence.evidence_state == 'validated':
            findings.append(
                PredictionFinding(
                    finding_id='validation:provider',
                    kind='validation',
                    tone='info',
                    title=f'provider 結果は validated です ({scope_label})',
                    detail='検証 evidence が記録されています。',
                    authorities=evidence.authority_refs,
                )
            )
        elif evidence.evidence_state == 'production':
            findings.append(
                PredictionFinding(
                    finding_id='validation:provider',
                    kind='validation',
                    tone='info',
                    title=f'provider 結果は production 採用済みです ({scope_label})',
                    detail='実室 evidence に基づく production 採用が記録されています。',
                    authorities=evidence.authority_refs,
                )
            )
        if evidence.stale_state == 'STALE':
            findings.append(
                PredictionFinding(
                    finding_id='coverage:provider-stale',
                    kind='coverage',
                    tone='limitation',
                    title='provider 結果は現在の条件に対して STALE です',
                    detail='理由: ' + (', '.join(evidence.stale_reasons) or '不明'),
                    authorities=evidence.authority_refs,
                )
            )
        elif evidence.stale_state == 'unknown':
            findings.append(
                PredictionFinding(
                    finding_id='coverage:provider-freshness',
                    kind='coverage',
                    tone='limitation',
                    title='provider 結果の鮮度は未評価です',
                    detail=(
                        '現在条件との比較 resolution が提供されていないため、'
                        'CURRENT/STALE の判定はできません。'
                    ),
                    authorities=evidence.authority_refs,
                )
            )

    # --- reliability / capability --------------------------------------------------
    assumptions = set(first.assumptions)

    def _assumption_reason(key: str) -> str | None:
        return _ASSUMPTION_LABELS.get(key) if key in assumptions else None

    def _assumption_capability(
        observable: str,
        label: str,
        assumption_key: str,
    ) -> PredictionCapabilityItem:
        """UNSUPPORTED only while the stored assumptions actually say so."""
        reason = _assumption_reason(assumption_key)
        if reason is not None:
            return PredictionCapabilityItem(
                observable=observable,
                label=label,
                state='UNSUPPORTED',
                reason=reason,
            )
        return PredictionCapabilityItem(
            observable=observable,
            label=label,
            state='UNKNOWN',
            reason='このmodelの能力情報は保存されていません',
        )

    supported = compatibility != 'unsupported'
    if modes_result is None:
        mode_state, mode_reason = 'UNKNOWN', 'geometry_modes result なし'
    elif not supported:
        mode_state, mode_reason = 'UNSUPPORTED', '部屋形状がモデルの対象外'
    else:
        mode_state, mode_reason = 'READY', None
    if reflections_result is None:
        reflection_state, reflection_reason = (
            'UNKNOWN',
            'geometry_reflections result なし',
        )
    elif not supported:
        reflection_state, reflection_reason = (
            'UNSUPPORTED',
            '部屋形状がモデルの対象外',
        )
    else:
        reflection_state, reflection_reason = 'READY', '幾何経路のみ'
    if any(item.result_kind == 'scalar_field' for item in results):
        field_state, field_reason = 'READY', None
    else:
        field_state, field_reason = (
            'UNSUPPORTED',
            'このrunに scalar_field result はありません',
        )
    if snapshot_available and 'materials' not in snapshot:
        material_state = 'UNSUPPORTED'
        material_reason = 'このmodelの入力に材料/境界条件は含まれません'
    elif not snapshot_available:
        material_state = 'UNKNOWN'
        material_reason = '入力snapshotを解釈できません'
    else:
        material_state = 'READY'
        material_reason = None

    capabilities: list[PredictionCapabilityItem] = [
        PredictionCapabilityItem(
            observable='room_mode_frequencies',
            label='room mode 周波数候補',
            state=mode_state,
            reason=mode_reason,
        ),
        PredictionCapabilityItem(
            observable='reflection_path_geometry',
            label='一次反射の幾何経路',
            state=reflection_state,
            reason=reflection_reason,
        ),
        PredictionCapabilityItem(
            observable='arrival_timing',
            label='到達遅延',
            state=reflection_state,
            reason=reflection_reason,
        ),
        _assumption_capability(
            'frequency_response_magnitude',
            '周波数応答・振幅',
            'modal_frequency_only_amplitude_and_damping_not_modelled',
        ),
        _assumption_capability(
            'frequency_response_phase',
            '周波数応答・位相',
            'reflection_amplitude_and_phase_not_modelled',
        ),
        _assumption_capability(
            'decay_rt60',
            '残響/減衰',
            'modal_frequency_only_amplitude_and_damping_not_modelled',
        ),
        PredictionCapabilityItem(
            observable='spatial_pressure_field',
            label='音場分布',
            state=field_state,
            reason=field_reason,
        ),
        _assumption_capability(
            'speaker_directivity',
            'speaker指向性',
            'speaker_directivity_not_modelled',
        ),
        PredictionCapabilityItem(
            observable='material_boundary',
            label='材料・境界条件',
            state=material_state,
            reason=material_reason,
        ),
        PredictionCapabilityItem(
            observable='measurement_validation',
            label='実測照合',
            state='UNSUPPORTED',
            reason='実測・実室との照合 authority がありません',
        ),
    ]
    if evidence is not None:
        capabilities.extend(evidence.capabilities)

    valid_bands: list[PredictionValidBand] = []
    if max_mode_hz is not None:
        valid_bands.append(
            PredictionValidBand(
                label='room mode候補',
                minimum_hz=0.0,
                maximum_hz=float(max_mode_hz),
            )
        )
    if evidence is not None and evidence.valid_band_hz is not None:
        valid_bands.append(
            PredictionValidBand(
                label='wave prediction provider',
                minimum_hz=evidence.valid_band_hz[0],
                maximum_hz=evidence.valid_band_hz[1],
            )
        )

    evidence_state = 'unvalidated' if evidence is None else evidence.evidence_state
    evidence_scope = None if evidence is None else evidence.evidence_scope
    evidence_label = _EVIDENCE_STATE_LABELS.get(evidence_state, evidence_state)
    if evidence_scope is not None and evidence_state in ('validated', 'production'):
        evidence_label = (
            f'{evidence_label} · '
            f'{_EVIDENCE_SCOPE_LABELS.get(evidence_scope, evidence_scope)}'
        )
    freshness = 'current' if is_current else 'stale'
    freshness_detail = (
        '現在の条件に一致'
        if is_current
        else '条件が変更されています · 再計算してください'
    )
    reliability = PredictionReliability(
        freshness=freshness,
        freshness_detail=freshness_detail,
        evidence_state=evidence_state,
        evidence_scope=evidence_scope,
        evidence_label=evidence_label,
        approximation_state=compatibility,
        valid_bands=tuple(valid_bands),
        capabilities=tuple(capabilities),
        provider_stale_state=None if evidence is None else evidence.stale_state,
        provider_stale_reasons=() if evidence is None else evidence.stale_reasons,
    )

    # --- neutral next steps ---------------------------------------------------------
    mode_finding = next(
        (finding for finding in findings if finding.kind == 'room_mode'), None
    )
    reflection_findings = [
        finding for finding in findings if finding.kind == 'reflection_path'
    ]
    speaker_ids = tuple(snapshot_speakers) or tuple(
        dict.fromkeys(
            finding.spatial.speaker_entity_id
            for finding in reflection_findings
            if finding.spatial is not None
            and finding.spatial.speaker_entity_id is not None
        )
    )
    next_actions: list[PredictionNextAction] = []
    if not is_current:
        next_actions.append(
            PredictionNextAction(
                action_id='rerun',
                label='条件が変わったため予測を再計算',
                detail='現在の配置・条件で再度実行すると、この結果と比較できます。',
                related_finding_id=None,
            )
        )
    if mode_finding is not None:
        next_actions.append(
            PredictionNextAction(
                action_id='compare-seat',
                label='seat候補を比較',
                detail=(
                    '受音点を変えた予測を別runとして保存し、'
                    'モード候補の変化を比較できます。'
                ),
                related_finding_id=mode_finding.finding_id,
                focus_entity_id=receiver_entity_id,
            )
        )
    if speaker_ids:
        next_actions.append(
            PredictionNextAction(
                action_id='compare-speaker',
                label='speaker位置を比較',
                detail=(
                    'speaker配置を変えた予測を別runとして保存し、'
                    '反射経路・モード候補を比較できます。'
                ),
                related_finding_id=(
                    reflection_findings[0].finding_id
                    if reflection_findings
                    else None
                ),
                focus_entity_id=speaker_ids[0],
            )
        )
    if reflection_findings:
        next_actions.append(
            PredictionNextAction(
                action_id='check-reflection-surface',
                label='この反射面を確認',
                detail=(
                    '候補経路の面を 3D overlay で確認できます。'
                    '面の処理は効果未評価の仮説です。'
                ),
                related_finding_id=reflection_findings[0].finding_id,
            )
        )
        next_actions.append(
            PredictionNextAction(
                action_id='create-treatment',
                label='Treatment案を作成',
                detail=(
                    '候補経路の面への Treatment を検討する下準備です。'
                    '吸音効果はこのモデルでは評価されていません。'
                ),
                related_finding_id=reflection_findings[0].finding_id,
            )
        )
    material_capability = next(
        (item for item in capabilities if item.observable == 'material_boundary'),
        None,
    )
    if material_capability is not None and material_capability.state != 'READY':
        next_actions.append(
            PredictionNextAction(
                action_id='set-materials',
                label='材料を設定',
                detail=(
                    'このモデルは材料を入力しませんが、後続の評価のために'
                    '表面材料を設定できます。'
                ),
            )
        )
    next_actions.append(
        PredictionNextAction(
            action_id='verify-by-measurement',
            label='実測で検証',
            detail=(
                'REW などの実測結果と候補を比較して検証します。'
                '候補の一致を保証するものではありません。'
            ),
            focus_entity_id=receiver_entity_id,
        )
    )

    # --- Advanced provenance --------------------------------------------------------
    advanced: list[str] = [
        f'run_id: {first.run_id}',
        f'model: {first.model_id} / {first.model_version}',
        f'document_id: {first.document_id}',
        f'scene_revision_id: {first.scene_revision_id}',
        f'scene_content_hash: {first.scene_content_hash}',
        f'constraint_workspace_hash: {first.constraint_workspace_hash}',
        f'input_hash: {first.input_hash}',
        f'parameters: {first.parameters_json}',
        f'geometry_compatibility: {first.geometry_compatibility}',
        f'status: {first.status}',
        f'submitted_at_utc: {first.submitted_at_utc}',
        f'completed_at_utc: {first.completed_at_utc}',
    ]
    for result in results:
        advanced.append(
            f'result[{result.result_kind}]: prediction_id={result.prediction_id} '
            f'result_sha256={result.result_sha256}'
        )
    for assumption in first.assumptions:
        advanced.append(f'assumption: {assumption}')
    for warning in first.warnings:
        advanced.append(f'warning: {warning}')
    if evidence is not None:
        advanced.append(f'provider_id: {evidence.provider_id}')
        if evidence.semantic_sha256 is not None:
            advanced.append(f'provider_semantic_sha256: {evidence.semantic_sha256}')
        if evidence.adapter_id is not None:
            advanced.append(
                f'provider_adapter: {evidence.adapter_id} / {evidence.adapter_version}'
            )
        if evidence.authority_version is not None:
            advanced.append(f'provider_authority_version: {evidence.authority_version}')
        advanced.append(
            f'provider_evidence: {evidence.evidence_state} / {evidence.evidence_scope}'
        )
        for ref in evidence.authority_refs:
            suffix = f' sha256={ref.sha256}' if ref.sha256 else ''
            advanced.append(f'provider_ref[{ref.kind}]: {ref.ref_id}{suffix}')

    return PredictionInterpretation(
        findings=tuple(findings),
        reliability=reliability,
        next_actions=tuple(next_actions),
        advanced_lines=tuple(advanced),
    )


__all__ = [
    'PredictionAuthorityRef',
    'PredictionCapabilityItem',
    'PredictionFinding',
    'PredictionInterpretation',
    'PredictionNextAction',
    'PredictionReliability',
    'PredictionSpatialLink',
    'PredictionValidBand',
    'ProviderEvidence',
    'interpret_prediction_results',
    'provider_evidence',
]
