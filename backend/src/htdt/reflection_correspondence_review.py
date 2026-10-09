"""#1002 — measured↔predicted reflection correspondence review viewmodel.

Qt-free builder for the measurement workspace's '反射対応を確認' compare
surface. It assembles — and only *replays* — the existing authorities:

- ``cad_reflection_correspondence`` pairings / sets / verdicts (the sealed
  correspondence authority; verdicts come exclusively from
  :func:`evaluate_reflection_correspondence` — this module never re-judges).
- #564 ``PredictionMeasurementRegistration`` rows (the clock/spatial
  registration the correspondence set pins).
- persisted ``DeterministicPathArtifact`` rows via
  ``reflection_guidance_presentation.load_reflection_guidance_view`` (the
  light-read predicted-path projection — no re-solve).
- stored ``CadImpulseResponseDataset`` rows for the ETC display curve. The
  curve is a *display-only* envelope derivation of sealed samples — it is
  labelled as such and is never treated as an observation.

Honesty contract: when the measurement, its IR data, the registration, or
the predicted paths are absent/stale the view says so with reasons and
lists the measurements that *do* carry the data, instead of fabricating.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from math import log10
from typing import Literal

from .cad_authority_resolver import AuthorityRef
from .cad_reflection_correspondence import (
    ObservedReflectionEvent,
    PredictedReflectionPath,
    ReflectionCorrespondencePairing,
    build_reflection_pairing,
)
from .cad_reflection_correspondence_repository import (
    CadReflectionCorrespondenceRepository,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, ensure_native_schema
from .measurement.domain.cad_measurement_ir import CadImpulseResponseDataset
from .measurement.domain.cad_prediction_measurement_registration import (
    PredictionMeasurementRegistration,
    evaluate_registration_freshness,
)
from .measurement.persistence.cad_measurement_repository import (
    CadMeasurementRepository,
)
from .measurement.persistence.cad_prediction_measurement_registration_repository import (
    CadPredictionMeasurementRegistrationRepository,
)
from .reflection_guidance_presentation import (
    ReflectionGuidanceEntry,
    load_reflection_guidance_view,
)

#: Version stamped on hypothesis annotations authored from this surface.
MANUAL_HYPOTHESIS_ALGORITHM_VERSION = 'ui-manual-hypothesis-1'

ReviewAvailability = Literal[
    'ready',
    'no_measurement',
    'no_ir_dataset',
    'no_registration',
    'no_predicted_paths',
    'load_failed',
]
"""#1002 §6 — the surface fails closed with a reason, never fabricates."""


@dataclass(frozen=True)
class CorrespondenceRegistrationRow:
    """One #564 registration that could bind this measurement."""

    registration_id: str
    registration_sha256: str
    scene_revision_id: str
    stale: bool
    comparability_state: str
    comparability_reasons: tuple[str, ...]
    arrival_time_supported: bool
    comparable_band_hz: tuple[float, float] | None
    partition: str
    prediction_id: str
    source_entity_id: str | None
    receiver_entity_id: str | None
    timing_method: str
    timing_offset_s: float | None
    timing_uncertainty_s: float | None


@dataclass(frozen=True)
class CorrespondencePredictedRow:
    """One predicted reflection path replayed from the sealed artifact."""

    row_id: str
    """Stable row identity — ``<artifact_id>:<path_id>``."""
    artifact_id: str
    path_id: str
    scene_revision_id: str
    source_entity_id: str
    receiver_entity_id: str
    surface_ids: tuple[str, ...]
    predicted_arrival_s: float
    excess_delay_s: float
    reflection_order: int
    arrival_direction: tuple[float, float, float] | None
    predicted_level_db: float | None
    solver_ref_id: str
    scene_revision_ref_sha256: str | None
    #: source → ordered interaction points → receiver, for the overlay.
    geometry_points: tuple[tuple[float, float, float], ...]
    stale: bool
    predicted_path: PredictedReflectionPath
    """The sealed-authority view of this path (used for exact matching)."""


@dataclass(frozen=True)
class CorrespondenceObservedRow:
    """One declared observed reflection event (from pairings/manual notes)."""

    row_id: str
    """Stable identity — sha of the ObservedReflectionEvent payload."""
    event: ObservedReflectionEvent
    measurement_ref_id: str
    extraction_algorithm: str
    extraction_version: str
    observed_time_s: float
    observed_extent_s: tuple[float, float] | None
    observed_doa: tuple[float, float, float] | None
    doa_uncertainty: float | None
    observed_level_db: float | None


@dataclass(frozen=True)
class CorrespondencePairingRow:
    """One sealed pairing as a display row — verdict-adjacent, never re-judged."""

    pairing_id: str
    pairing_sha256: str
    correspondence_state: str
    matching_algorithm: str
    matching_algorithm_version: str
    evidence_dimensions: tuple[str, ...]
    cluster_member_ids: tuple[str, ...]
    validation_role: str
    ambiguity_note: str | None
    declared_at_utc: str
    predicted_row_id: str | None
    observed_row_id: str | None
    is_hypothesis: bool
    """``manual_expert_label`` pairings are operator hypotheses, not
    algorithmic authority — the UI styles them as such."""
    set_ids: tuple[str, ...]


@dataclass(frozen=True)
class CorrespondenceSetRow:
    set_id: str
    registration_ref_id: str | None
    pairing_ids: tuple[str, ...]
    registration_present: bool
    """False when the pinned registration is not persisted for this
    measurement — an orphaned/stale pin, shown honestly."""


@dataclass(frozen=True)
class CorrespondenceVerdictRow:
    verdict_id: str
    set_id: str
    state: str
    matched_pair_count: int
    ambiguous_pair_count: int
    unmatched_predicted_count: int
    unmatched_observed_count: int
    calibration_contaminated: bool
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    evaluated_at_utc: str


@dataclass(frozen=True)
class EtcDisplayCurve:
    """Display-only energy envelope of a sealed IR dataset.

    NOT an observation: it is a smoothed |h(t)|² rendering of stored
    samples for visual alignment, explicitly labelled.
    """

    dataset_id: str
    times_s: tuple[float, ...]
    level_db: tuple[float, ...]
    sample_rate_hz: float
    start_time_s: float
    t0_semantics: str
    ir_semantics: str
    calibration_state: str


@dataclass(frozen=True)
class CorrespondenceReviewView:
    """Everything the compare surface renders, already resolved."""

    document_id: str
    availability: ReviewAvailability
    availability_reasons: tuple[str, ...] = ()
    measurement_id: str | None = None
    measurement_options: tuple[str, ...] = ()
    """Measurements that carry at least an IR dataset — navigation targets
    presented when the requested one is missing/unsupported."""
    etc_curve: EtcDisplayCurve | None = None
    registrations: tuple[CorrespondenceRegistrationRow, ...] = ()
    predicted_rows: tuple[CorrespondencePredictedRow, ...] = ()
    observed_rows: tuple[CorrespondenceObservedRow, ...] = ()
    pairing_rows: tuple[CorrespondencePairingRow, ...] = ()
    set_rows: tuple[CorrespondenceSetRow, ...] = ()
    verdict_rows: tuple[CorrespondenceVerdictRow, ...] = ()
    guidance_issues: tuple[str, ...] = ()
    current_scene_revision_id: str | None = None


# ----------------------------------------------------------------------
# Display derivation


def derive_etc_display_curve(
    dataset: CadImpulseResponseDataset,
    *,
    smoothing_ms: float = 1.0,
    floor_db: float = -120.0,
) -> EtcDisplayCurve:
    """Smoothed energy envelope of the sealed IR samples — display only.

    ``level_db`` uses an arbitrary dB reference (0 dB at the peak of the
    smoothed envelope); it must never be read as an absolute level.
    """

    sr = float(dataset.sample_rate_hz)
    window = max(1, int(round(sr * smoothing_ms / 1000.0)))
    energy = [float(a) * float(a) for a in dataset.amplitudes]
    smoothed: list[float] = []
    acc = 0.0
    for i, e in enumerate(energy):
        acc += e
        if i >= window:
            acc -= energy[i - window]
        smoothed.append(acc)
    peak = max(smoothed) if smoothed else 0.0
    peak = peak if peak > 0.0 else 1.0
    floor_lin = 10.0 ** (floor_db / 10.0)
    level_db = tuple(
        10.0 * log10(max(v / peak, floor_lin)) for v in smoothed
    )
    times_s = tuple(
        dataset.start_time_s + i / sr for i in range(len(smoothed))
    )
    return EtcDisplayCurve(
        dataset_id=dataset.dataset_id,
        times_s=times_s,
        level_db=level_db,
        sample_rate_hz=sr,
        start_time_s=dataset.start_time_s,
        t0_semantics=dataset.t0_semantics,
        ir_semantics=dataset.ir_semantics,
        calibration_state=dataset.calibration_state,
    )


# ----------------------------------------------------------------------
# Authority mapping helpers (reuse only — never re-judge)


def predicted_path_from_guidance_entry(
    entry: ReflectionGuidanceEntry,
    *,
    scene_revision_ref: AuthorityRef | None = None,
) -> PredictedReflectionPath:
    """Map a persisted deterministic path onto the correspondence identity.

    ``interaction_refs`` pins the sealed deterministic path itself — its
    payload already carries the ordered surface/edge sequence verbatim —
    then adds one ref per surface id that parses as a hash-bearing
    ``semantic-surface:<sha>`` identity. Other surface ids stay visible
    in the UI through the path pin; an unresolvable name is never
    wrapped in a fabricated hash.
    """

    path = entry.reflection_path
    solver = path.solver_implementation_ref
    refs = [
        AuthorityRef(
            kind='deterministic_acoustic_path',
            ref_id=path.path_id,
            ref_sha256=path.semantic_sha256,
        )
    ]
    for surface_id in path.ordered_interaction_surface_ids:
        sha = _surface_id_sha(surface_id)
        if sha is None:
            continue
        refs.append(
            AuthorityRef(
                kind='semantic_surface',
                ref_id=surface_id,
                ref_sha256=sha,
            )
        )
    return PredictedReflectionPath(
        solver_ref=AuthorityRef(
            kind='solver_implementation',
            ref_id=solver.authority_id,
            ref_sha256=solver.semantic_hash_sha256,
        ),
        scene_revision_ref=scene_revision_ref,
        interaction_refs=tuple(refs),
        path_class='specular',
        predicted_arrival_s=path.propagation_delay_s,
        reflection_order=max(1, len(path.ordered_interaction_surface_ids)),
        arrival_direction=(
            path.arrival_direction.x,
            path.arrival_direction.y,
            path.arrival_direction.z,
        ),
    )


def _surface_id_sha(surface_id: str) -> str | None:
    prefix = 'semantic-surface:'
    if surface_id.startswith(prefix):
        suffix = surface_id[len(prefix):]
        if len(suffix) == 64 and all(c in '0123456789abcdef' for c in suffix):
            return suffix
    return None


def observed_event_from_manual_gate(
    *,
    measurement_id: str,
    dataset: CadImpulseResponseDataset,
    observed_time_s: float,
    observed_extent_s: tuple[float, float] | None = None,
    observed_doa: tuple[float, float, float] | None = None,
    doa_uncertainty: float | None = None,
) -> ObservedReflectionEvent:
    """An operator-declared ETC gate as an honest ``manual_annotation``.

    The measurement ref pins the exact sealed IR dataset the gate was read
    against; extraction is declared as manual, never algorithmic.
    """

    return ObservedReflectionEvent(
        measurement_ref=AuthorityRef(
            kind='impulse_response_measurement',
            ref_id=measurement_id,
            ref_sha256=dataset.dataset_sha256,
        ),
        extraction_algorithm='manual_annotation',
        extraction_version=MANUAL_HYPOTHESIS_ALGORITHM_VERSION,
        observed_time_s=observed_time_s,
        observed_extent_s=observed_extent_s,
        observed_doa=observed_doa,
        doa_uncertainty=doa_uncertainty,
        extraction_parameters=('declared_on=etc_cursor',),
    )


def build_manual_hypothesis_pairing(
    *,
    document_id: str,
    predicted_path: PredictedReflectionPath | None,
    observed_event: ObservedReflectionEvent | None,
    declared_at_utc: str | None = None,
) -> ReflectionCorrespondencePairing:
    """Seal the operator's candidate selection as a *hypothesis* annotation.

    The pairing is declared ``ambiguous`` with ``time_alignment`` evidence
    only — the canonical evaluator counts time-only pairings ambiguous, so
    a human pick can never silently qualify a correspondence.
    """

    return build_reflection_pairing(
        document_id=document_id,
        predicted_path=predicted_path,
        observed_event=observed_event,
        correspondence_state='ambiguous',
        matching_algorithm='manual_expert_label',
        matching_algorithm_version=MANUAL_HYPOTHESIS_ALGORITHM_VERSION,
        evidence_dimensions=('time_alignment',),
        ambiguity_note=(
            'operator hypothesis declared on the correspondence review '
            'surface — pending canonical evaluation, not a verdict'
        ),
        declared_at_utc=declared_at_utc,
    )


# ----------------------------------------------------------------------
# View assembly


def _predicted_row_from_entry(
    entry: ReflectionGuidanceEntry,
    *,
    revision_sha_by_id: dict[str, str],
    current_revision_id: str | None,
) -> CorrespondencePredictedRow:
    path = entry.reflection_path
    revision_sha = revision_sha_by_id.get(entry.scene_revision_id)
    scene_revision_ref = (
        AuthorityRef(
            kind='scene_revision',
            ref_id=entry.scene_revision_id,
            ref_sha256=revision_sha,
        )
        if revision_sha is not None
        else None
    )
    points: list[tuple[float, float, float]] = []
    if entry.source_reference_point is not None:
        points.append(
            (
                entry.source_reference_point.x_m,
                entry.source_reference_point.y_m,
                entry.source_reference_point.z_m,
            )
        )
    for point in path.ordered_interaction_points:
        points.append((point.x_m, point.y_m, point.z_m))
    if entry.receiver_position is not None:
        points.append(
            (
                entry.receiver_position.x_m,
                entry.receiver_position.y_m,
                entry.receiver_position.z_m,
            )
        )
    return CorrespondencePredictedRow(
        row_id=f'{entry.artifact_id}:{path.path_id}',
        artifact_id=entry.artifact_id,
        path_id=path.path_id,
        scene_revision_id=entry.scene_revision_id,
        source_entity_id=entry.source_entity_id,
        receiver_entity_id=entry.receiver_entity_id,
        surface_ids=path.ordered_interaction_surface_ids,
        predicted_arrival_s=path.propagation_delay_s,
        excess_delay_s=entry.excess_delay_s,
        reflection_order=max(1, len(path.ordered_interaction_surface_ids)),
        arrival_direction=(
            path.arrival_direction.x,
            path.arrival_direction.y,
            path.arrival_direction.z,
        ),
        predicted_level_db=None,
        solver_ref_id=path.solver_implementation_ref.authority_id,
        scene_revision_ref_sha256=revision_sha,
        geometry_points=tuple(points),
        stale=entry.scene_revision_id != current_revision_id,
        predicted_path=predicted_path_from_guidance_entry(
            entry, scene_revision_ref=scene_revision_ref
        ),
    )


def _observed_row(event: ObservedReflectionEvent) -> CorrespondenceObservedRow:
    row_id = 'obs-' + sha256(
        event.model_dump_json().encode('utf-8')
    ).hexdigest()[:24]
    return CorrespondenceObservedRow(
        row_id=row_id,
        event=event,
        measurement_ref_id=event.measurement_ref.ref_id,
        extraction_algorithm=event.extraction_algorithm,
        extraction_version=event.extraction_version,
        observed_time_s=event.observed_time_s,
        observed_extent_s=event.observed_extent_s,
        observed_doa=event.observed_doa,
        doa_uncertainty=event.doa_uncertainty,
        observed_level_db=event.observed_level_db,
    )


def _registration_row(
    registration: PredictionMeasurementRegistration,
    *,
    current_revision_id: str | None,
    current_content_hash: str | None,
) -> CorrespondenceRegistrationRow:
    freshness = evaluate_registration_freshness(
        registration,
        current_scene_revision_id=current_revision_id,
        current_scene_content_hash=current_content_hash,
    )
    verdict = registration.comparability
    return CorrespondenceRegistrationRow(
        registration_id=registration.registration_id,
        registration_sha256=registration.semantic_sha256,
        scene_revision_id=registration.scene_revision_id,
        stale=freshness != 'current',
        comparability_state=verdict.state,
        comparability_reasons=tuple(verdict.reasons)
        + tuple(verdict.limitations),
        arrival_time_supported=verdict.arrival_time_supported,
        comparable_band_hz=verdict.comparable_band_hz,
        partition=registration.partition,
        prediction_id=registration.prediction.prediction_id,
        source_entity_id=registration.source.entity_id,
        receiver_entity_id=registration.receiver.entity_id,
        timing_method=registration.timing.method,
        timing_offset_s=registration.timing.applied_offset_s,
        timing_uncertainty_s=registration.timing.uncertainty_s,
    )


def build_correspondence_review(
    scene_repository: SceneRepository,
    document_id: str,
    measurement_id: str | None = None,
) -> CorrespondenceReviewView:
    """Assemble the '反射対応を確認' view for one measurement.

    Everything displayed is replayed from sealed authorities; missing or
    stale inputs surface as availability reasons, never as fabricated rows.
    """

    measurement_repo = CadMeasurementRepository(scene_repository)
    registration_repo = CadPredictionMeasurementRegistrationRepository(
        scene_repository
    )
    correspondence_repo = CadReflectionCorrespondenceRepository(
        scene_repository
    )
    latest = scene_repository.latest(document_id)
    current_revision_id = latest.revision_id if latest is not None else None
    current_content_hash = latest.content_hash if latest is not None else None
    revision_sha_by_id = {
        r.revision_id: r.content_hash
        for r in scene_repository.list_revisions(document_id)
    }

    measurement_options = tuple(
        record.measurement_id
        for record in measurement_repo.list_measurements(document_id)
        if measurement_repo.ir_datasets_for_measurement(
            record.measurement_id
        )
    )
    if measurement_id is None:
        return CorrespondenceReviewView(
            document_id=document_id,
            availability='no_measurement',
            availability_reasons=(
                '測定が選択されていません — 対応確認する測定を選んでください。',
            ),
            measurement_options=measurement_options,
            current_scene_revision_id=current_revision_id,
        )
    if measurement_id not in {
        record.measurement_id
        for record in measurement_repo.list_measurements(document_id)
    }:
        return CorrespondenceReviewView(
            document_id=document_id,
            measurement_id=measurement_id,
            availability='no_measurement',
            availability_reasons=(
                f'測定 {measurement_id} はこのドキュメントに存在しません。',
            ),
            measurement_options=measurement_options,
            current_scene_revision_id=current_revision_id,
        )

    # --- measured side -----------------------------------------------------
    ir_datasets = measurement_repo.ir_datasets_for_measurement(
        measurement_id
    )
    etc_curve = (
        derive_etc_display_curve(ir_datasets[0]) if ir_datasets else None
    )

    # --- registration authority (#564) -------------------------------------
    registrations = registration_repo.list_for_measurement(measurement_id)
    registration_rows = tuple(
        _registration_row(
            registration,
            current_revision_id=current_revision_id,
            current_content_hash=current_content_hash,
        )
        for registration in registrations
    )
    registrations_by_id = {
        row.registration_id: row for row in registration_rows
    }

    # --- predicted side (persisted deterministic path artifacts) -----------
    guidance_issues: tuple[str, ...] = ()
    predicted_rows: tuple[CorrespondencePredictedRow, ...] = ()
    try:
        ensure_native_schema(scene_repository.path)
        with closing(connect_sqlite(scene_repository.path)) as connection:
            guidance_view = load_reflection_guidance_view(
                connection, document_id
            )
        guidance_issues = tuple(
            issue.detail for issue in guidance_view.issues
        )
        bound_revision_ids = {
            row.scene_revision_id for row in registration_rows
        }
        bound_sources = {
            row.source_entity_id
            for row in registration_rows
            if row.source_entity_id is not None
        }
        bound_receivers = {
            row.receiver_entity_id
            for row in registration_rows
            if row.receiver_entity_id is not None
        }
        rows = []
        for entry in guidance_view.entries:
            if (
                bound_revision_ids
                and entry.scene_revision_id not in bound_revision_ids
            ):
                continue
            if (
                bound_sources
                and entry.source_entity_id not in bound_sources
            ):
                continue
            if (
                bound_receivers
                and entry.receiver_entity_id not in bound_receivers
            ):
                continue
            rows.append(
                _predicted_row_from_entry(
                    entry,
                    revision_sha_by_id=revision_sha_by_id,
                    current_revision_id=current_revision_id,
                )
            )
        predicted_rows = tuple(rows)
    except Exception as exc:  # noqa: BLE001 — honest degraded state
        guidance_issues = (f'予測パスの読み込みに失敗しました: {exc}',)

    # --- declared correspondence authority ---------------------------------
    pairings = correspondence_repo.list_pairings(document_id)
    sets = correspondence_repo.list_sets(document_id)
    verdicts = correspondence_repo.list_verdicts(document_id)

    def _path_pin(
        path: PredictedReflectionPath,
    ) -> tuple[str, str] | None:
        """The sealed deterministic-path pin inside ``interaction_refs``.

        Correspondence declarations pin the persisted path artifact itself —
        matching on that sealed ref binds pairing ↔ 3D row on the exact
        source ID even when other pins (e.g. the scene-revision ref the UI
        supplies) differ between declaration-time and replay-time builds.
        """

        for ref in path.interaction_refs:
            if ref.kind == 'deterministic_acoustic_path':
                return (ref.ref_id, ref.ref_sha256 or '')
        return None

    observed_by_key: dict[str, CorrespondenceObservedRow] = {}
    predicted_rows_by_model: dict[str, CorrespondencePredictedRow] = {
        row.predicted_path.model_dump_json(): row
        for row in predicted_rows
    }
    predicted_rows_by_pin: dict[tuple[str, str], CorrespondencePredictedRow] = {
        pin: row
        for row in predicted_rows
        if (pin := _path_pin(row.predicted_path)) is not None
    }
    observed_rows_by_model: dict[str, CorrespondenceObservedRow] = {}

    def _observed_row_for(
        event: ObservedReflectionEvent,
    ) -> CorrespondenceObservedRow | None:
        if event.measurement_ref.ref_id != measurement_id:
            return None
        row = _observed_row(event)
        observed_by_key[row.row_id] = row
        observed_rows_by_model[event.model_dump_json()] = row
        return row

    set_pairing_ids = {
        set_.set_id: tuple(ref.ref_id for ref in set_.pairing_refs)
        for set_ in sets
    }
    pairing_to_sets: dict[str, list[str]] = {}
    for set_id, pairing_ids in set_pairing_ids.items():
        for pairing_id in pairing_ids:
            pairing_to_sets.setdefault(pairing_id, []).append(set_id)

    pairing_rows: list[CorrespondencePairingRow] = []
    for pairing in pairings:
        observed_row = (
            _observed_row_for(pairing.observed_event)
            if pairing.observed_event is not None
            else None
        )
        predicted_row = None
        if pairing.predicted_path is not None:
            pin = _path_pin(pairing.predicted_path)
            predicted_row = (
                predicted_rows_by_pin.get(pin)
                if pin is not None
                else None
            ) or predicted_rows_by_model.get(
                pairing.predicted_path.model_dump_json()
            )
        belongs = observed_row is not None or (
            pairing.predicted_path is not None and predicted_row is not None
        )
        if not belongs:
            # A pairing pinned to another measurement/pair is not shown —
            # the surface only replays rows sharing this measurement's
            # binding, never silently re-scopes them.
            continue
        pairing_rows.append(
            CorrespondencePairingRow(
                pairing_id=pairing.pairing_id,
                pairing_sha256=pairing.pairing_sha256,
                correspondence_state=pairing.correspondence_state,
                matching_algorithm=pairing.matching_algorithm,
                matching_algorithm_version=pairing.matching_algorithm_version,
                evidence_dimensions=pairing.evidence_dimensions,
                cluster_member_ids=pairing.cluster_member_ids,
                validation_role=pairing.validation_role,
                ambiguity_note=pairing.ambiguity_note,
                declared_at_utc=pairing.declared_at_utc,
                predicted_row_id=(
                    predicted_row.row_id if predicted_row is not None else None
                ),
                observed_row_id=(
                    observed_row.row_id if observed_row is not None else None
                ),
                is_hypothesis=pairing.matching_algorithm
                == 'manual_expert_label',
                set_ids=tuple(pairing_to_sets.get(pairing.pairing_id, ())),
            )
        )

    set_rows = tuple(
        CorrespondenceSetRow(
            set_id=set_.set_id,
            registration_ref_id=set_.registration_ref.ref_id,
            pairing_ids=set_pairing_ids[set_.set_id],
            registration_present=(
                set_.registration_ref.ref_id in registrations_by_id
            ),
        )
        for set_ in sets
    )
    verdict_rows = tuple(
        CorrespondenceVerdictRow(
            verdict_id=verdict.verdict_id,
            set_id=verdict.set_ref.ref_id,
            state=verdict.state,
            matched_pair_count=verdict.matched_pair_count,
            ambiguous_pair_count=verdict.ambiguous_pair_count,
            unmatched_predicted_count=verdict.unmatched_predicted_count,
            unmatched_observed_count=verdict.unmatched_observed_count,
            calibration_contaminated=verdict.calibration_contaminated,
            reasons=verdict.reasons,
            limitations=verdict.limitations,
            evaluated_at_utc=verdict.evaluated_at_utc,
        )
        for verdict in verdicts
    )

    observed_rows = tuple(observed_by_key.values())

    # --- availability ------------------------------------------------------
    if etc_curve is None:
        availability: ReviewAvailability = 'no_ir_dataset'
        reasons = (
            'この測定にはインパルス応答データセットがありません — '
            'ETC表示できません。',
        )
    elif not registrations:
        availability = 'no_registration'
        reasons = (
            'この測定には予測↔実測の登録（registration）がありません — '
            '絶対時刻対応は定義されません。',
        )
    elif not predicted_rows:
        availability = 'no_predicted_paths'
        reasons = (
            '登録済みのシーン版・音源・受音点に対応する予測反射パスが'
            'ありません。',
        )
    else:
        availability = 'ready'
        reasons = ()

    return CorrespondenceReviewView(
        document_id=document_id,
        measurement_id=measurement_id,
        availability=availability,
        availability_reasons=reasons,
        measurement_options=measurement_options,
        etc_curve=etc_curve,
        registrations=registration_rows,
        predicted_rows=predicted_rows,
        observed_rows=observed_rows,
        pairing_rows=tuple(pairing_rows),
        set_rows=set_rows,
        verdict_rows=verdict_rows,
        guidance_issues=guidance_issues,
        current_scene_revision_id=current_revision_id,
    )


__all__ = [
    'CorrespondenceObservedRow',
    'CorrespondencePairingRow',
    'CorrespondencePredictedRow',
    'CorrespondenceRegistrationRow',
    'CorrespondenceReviewView',
    'CorrespondenceSetRow',
    'CorrespondenceVerdictRow',
    'EtcDisplayCurve',
    'MANUAL_HYPOTHESIS_ALGORITHM_VERSION',
    'ReviewAvailability',
    'build_correspondence_review',
    'build_manual_hypothesis_pairing',
    'derive_etc_display_curve',
    'observed_event_from_manual_gate',
    'predicted_path_from_guidance_entry',
]
