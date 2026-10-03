"""Reflection-guidance read projection for the Room acoustics dock.

Read-only projection over persisted R150 deterministic path artifacts:
each ``cad_deterministic_path_artifacts`` row bound to a scene snapshot of
this document is replayed into ``ReflectionGuidanceReport``s — one
``ReflectionDiagnosticRequest`` per (source → receiver, first-order
specular path) pair, the exact per-pair binding the diagnostic model
declares. Zones and the labelled interference hypothesis are derived for
each pair so the report emits its ``treat_reflection_zone`` /
``reposition_source`` / ``verify_with_measurement`` items where the
geometry proves them.

Nothing here mutates the database and nothing fabricates: a row whose
payload fails validation, whose declared snapshot binding does not resolve
to this document, or whose request/hypothesis/report derivation raises is
listed as an explicit issue instead of being silently skipped. Measured
ETC support is never invented — without a persisted
``ReflectionMeasuredTimingEvidence`` lane every item stays honestly at
``unverified_hypothesis`` confidence.

The heavier ``CadDeterministicPathArtifactRepository`` read path replays
the full authority chain (dispatch, configuration, geometry resolvers);
this projection follows the solver-output ledger instead — column-verified
payload replay plus the artifact↔snapshot semantic-hash binding, which is
exactly the linkage the write path pins.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from math import sqrt
from typing import Any, Literal

from .cad_acoustic_snapshot import SnapshotEnvironmentAuthorityRef
from .cad_geometric_acoustics_adapter import (
    DeterministicAcousticPath,
    DeterministicPathArtifact,
)
from .cad_reflection_diagnostic import (
    DEFAULT_SPEED_OF_SOUND_M_S,
    InterferenceHypothesis,
    ReflectionDiagnosticRequest,
    build_interference_hypothesis,
    build_reflection_diagnostic_request,
)
from .cad_reflection_guidance import (
    ReflectionGuidanceReport,
    ReflectionGuidanceSession,
    build_reflection_guidance,
    open_guidance_session,
)
from .cad_scene import Position3


EnvironmentBindingState = Literal['bound', 'absent', 'unreadable']

IssueKind = Literal[
    'unreadable_artifact',
    'binding_mismatch',
    'derivation_failed',
]

# Why an entry cannot open an interactive scrub session. The committed
# report stays displayable — the blocker only gates the interactive lane,
# so a degraded snapshot binding surfaces its reason instead of silently
# dropping the feature.
ScrubUnavailableReason = Literal[
    'snapshot_payload_unreadable',
    'receiver_binding_missing',
    'receiver_binding_unreadable',
    'source_binding_missing',
    'source_binding_unreadable',
    'plane_derivation_failed',
]


@dataclass(frozen=True, slots=True)
class ReflectionGuidanceEntry:
    """One derived guidance report bound to a persisted path artifact.

    A report per (artifact, source → receiver, first-order specular path):
    the diagnostic request names the exact reflection path under
    diagnosis, so the report's ``request_semantic_sha256`` binding is
    exact — sibling paths are never bundled under a request they are not
    part of.
    """

    artifact_id: str
    snapshot_id: str
    scene_revision_id: str
    recorded_at_utc: str | None
    source_entity_id: str
    receiver_id: str
    receiver_entity_id: str
    surface_id: str
    direct_path_id: str
    reflection_path_id: str
    excess_delay_s: float
    speed_of_sound_m_s: float
    environment_state: EnvironmentBindingState
    hypothesis: InterferenceHypothesis | None
    report: ReflectionGuidanceReport
    # Scrub-session authority: the exact request/path pair the report was
    # derived from, plus the snapshot-pinned geometry the interactive
    # preview needs (receiver world position, the committed source
    # reference point, and the specular plane implied by the proven path).
    request: ReflectionDiagnosticRequest
    direct_path: DeterministicAcousticPath
    reflection_path: DeterministicAcousticPath
    plane_point: Position3
    plane_normal: tuple[float, float, float] | None
    receiver_position: Position3 | None
    source_reference_point: Position3 | None
    scrub_unavailable_reason: ScrubUnavailableReason | None

    def open_scrub_session(self) -> ReflectionGuidanceSession:
        """Open the interactive scrub session pinned to this pair.

        The session replays the request's pinned plane through exact
        image-method geometry — never a new solver claim. Raises
        ``ValueError`` when the snapshot bindings could not be resolved.
        """
        if self.scrub_unavailable_reason is not None:
            raise ValueError(
                f'scrub session unavailable: {self.scrub_unavailable_reason}'
            )
        if self.receiver_position is None or self.plane_normal is None:
            raise ValueError('scrub session geometry was not resolved')
        return open_guidance_session(
            self.request,
            self.direct_path,
            self.reflection_path,
            receiver_position=self.receiver_position,
            plane_point=self.plane_point,
            plane_normal=self.plane_normal,
        )


@dataclass(frozen=True, slots=True)
class ReflectionGuidanceIssue:
    """A persisted row that could not be replayed into guidance."""

    kind: IssueKind
    detail: str
    artifact_id: str | None = None
    snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReflectionGuidanceView:
    """Whole-document guidance projection."""

    document_id: str
    entries: tuple[ReflectionGuidanceEntry, ...]
    issues: tuple[ReflectionGuidanceIssue, ...]

    @property
    def scene_revision_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(entry.scene_revision_id for entry in self.entries)
        )

    def for_revision(
        self, revision_id: str
    ) -> tuple[ReflectionGuidanceEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.scene_revision_id == revision_id
        )


def _snapshot_payload_dict(payload_json: str | None) -> dict[str, Any] | None:
    """Parsed snapshot payload for the light-read seam; None when absent
    or unparseable — callers map that to an honest blocker, not a guess.
    """
    if not payload_json:
        return None
    try:
        raw = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _receiver_position(
    payload: dict[str, Any] | None,
    *,
    receiver_id: str,
    receiver_entity_id: str,
) -> tuple[Position3 | None, ScrubUnavailableReason | None]:
    """Receiver world position from the snapshot's receiver bindings.

    The light-read seam re-validates only the fields the scrub consumes:
    the binding must name both the receiver id and entity id the path
    authority pins, and ``world_position`` must parse as a ``Position3``.
    """
    if payload is None:
        return None, 'snapshot_payload_unreadable'
    receivers = payload.get('receivers')
    if not isinstance(receivers, list):
        return None, 'receiver_binding_missing'
    for raw in receivers:
        if not isinstance(raw, dict):
            continue
        if (
            raw.get('receiver_id') != receiver_id
            or raw.get('entity_id') != receiver_entity_id
        ):
            continue
        try:
            return Position3.model_validate(raw.get('world_position')), None
        except ValueError:
            return None, 'receiver_binding_unreadable'
    return None, 'receiver_binding_missing'


def _source_reference_point(
    payload: dict[str, Any] | None,
    *,
    source_entity_id: str,
) -> tuple[Position3 | None, ScrubUnavailableReason | None]:
    """Committed source reference point — the scrub's honest baseline."""
    if payload is None:
        return None, 'snapshot_payload_unreadable'
    sources = payload.get('sources')
    if not isinstance(sources, list):
        return None, 'source_binding_missing'
    for raw in sources:
        if not isinstance(raw, dict):
            continue
        if raw.get('source_entity_id') != source_entity_id:
            continue
        try:
            return Position3.model_validate(
                raw.get('source_reference_point')
            ), None
        except ValueError:
            return None, 'source_binding_unreadable'
    return None, 'source_binding_missing'


def _specular_plane_normal(
    reflection_path: DeterministicAcousticPath,
) -> tuple[float, float, float] | None:
    """Specular plane normal implied by the proven path directions.

    For a first-order specular path the departure direction (source →
    interaction point) and arrival direction (interaction point →
    receiver) pin one plane through the interaction point: the unit
    normal is ``departure − arrival`` normalized. This is the plane the
    image-method preview must stay consistent with — it is derived from
    solver authority, never assumed from live scene geometry.
    """
    dx = float(reflection_path.departure_direction.x) - float(
        reflection_path.arrival_direction.x
    )
    dy = float(reflection_path.departure_direction.y) - float(
        reflection_path.arrival_direction.y
    )
    dz = float(reflection_path.departure_direction.z) - float(
        reflection_path.arrival_direction.z
    )
    norm = sqrt(dx * dx + dy * dy + dz * dz)
    if norm <= 1e-12:
        return None
    return (dx / norm, dy / norm, dz / norm)


def _snapshot_environment(
    payload_json: str | None,
) -> tuple[float, EnvironmentBindingState]:
    """Sound speed bound by the snapshot's environment authority.

    Indexed snapshot columns are written by the authority save path from a
    validated model and identify the binding; only the environment section
    is re-validated here (the full snapshot read replays the authority
    chain — the ledger's light-read seam). A snapshot declaring no
    environment authority leaves the diagnostic request at its documented
    default speed; a present-but-unreadable authority is flagged, never
    guessed.
    """
    if not payload_json:
        return DEFAULT_SPEED_OF_SOUND_M_S, 'absent'
    try:
        raw = json.loads(payload_json)
    except (TypeError, ValueError):
        return DEFAULT_SPEED_OF_SOUND_M_S, 'unreadable'
    environment = raw.get('environment') if isinstance(raw, dict) else None
    if environment is None:
        return DEFAULT_SPEED_OF_SOUND_M_S, 'absent'
    try:
        authority = SnapshotEnvironmentAuthorityRef.model_validate(
            environment
        )
    except ValueError:
        return DEFAULT_SPEED_OF_SOUND_M_S, 'unreadable'
    if authority.sound_speed_m_s is None:
        return DEFAULT_SPEED_OF_SOUND_M_S, 'absent'
    return float(authority.sound_speed_m_s), 'bound'


def _derive_entry(
    artifact: DeterministicPathArtifact,
    *,
    snapshot_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    speed_of_sound_m_s: float,
    environment_state: EnvironmentBindingState,
    recorded_at_utc: str | None,
    snapshot_payload: dict[str, Any] | None,
    direct: DeterministicAcousticPath,
    specular: DeterministicAcousticPath,
) -> ReflectionGuidanceEntry:
    """Replay one persisted direct+specular pair into a guidance report."""
    request = build_reflection_diagnostic_request(
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        source_entity_id=specular.source_entity_id,
        receiver_id=specular.receiver_id,
        receiver_entity_id=specular.receiver_entity_id,
        direct_path_id=direct.path_id,
        reflection_path_id=specular.path_id,
        speed_of_sound_m_s=speed_of_sound_m_s,
        solver_implementation_ref=specular.solver_implementation_ref,
    )
    hypothesis = build_interference_hypothesis(request, direct, specular)
    report = build_reflection_guidance(
        request,
        direct,
        (specular,),
        hypotheses=(hypothesis,),
    )
    receiver_position, receiver_reason = _receiver_position(
        snapshot_payload,
        receiver_id=specular.receiver_id,
        receiver_entity_id=specular.receiver_entity_id,
    )
    source_reference_point, source_reason = _source_reference_point(
        snapshot_payload,
        source_entity_id=specular.source_entity_id,
    )
    plane_normal = _specular_plane_normal(specular)
    if receiver_reason == 'snapshot_payload_unreadable':
        scrub_unavailable_reason: ScrubUnavailableReason | None = (
            'snapshot_payload_unreadable'
        )
    else:
        scrub_unavailable_reason = (
            receiver_reason
            or source_reason
            or (
                None
                if plane_normal is not None
                else 'plane_derivation_failed'
            )
        )
    return ReflectionGuidanceEntry(
        artifact_id=artifact.artifact_id,
        snapshot_id=snapshot_id,
        scene_revision_id=scene_revision_id,
        recorded_at_utc=recorded_at_utc,
        source_entity_id=specular.source_entity_id,
        receiver_id=specular.receiver_id,
        receiver_entity_id=specular.receiver_entity_id,
        surface_id=specular.ordered_interaction_surface_ids[0],
        direct_path_id=direct.path_id,
        reflection_path_id=specular.path_id,
        excess_delay_s=float(specular.propagation_delay_s)
        - float(direct.propagation_delay_s),
        speed_of_sound_m_s=speed_of_sound_m_s,
        environment_state=environment_state,
        hypothesis=hypothesis,
        report=report,
        request=request,
        direct_path=direct,
        reflection_path=specular,
        plane_point=specular.ordered_interaction_points[0],
        plane_normal=plane_normal,
        receiver_position=receiver_position,
        source_reference_point=source_reference_point,
        scrub_unavailable_reason=scrub_unavailable_reason,
    )


def load_reflection_guidance_view(
    connection: sqlite3.Connection,
    document_id: str,
) -> ReflectionGuidanceView:
    """Replay persisted deterministic path artifacts into guidance entries.

    Only artifacts whose declared ``snapshot_id`` resolves to a snapshot of
    ``document_id`` participate — unattributable rows are invisible rather
    than claimed, and the artifact's pinned ``snapshot_sha256`` must match
    the snapshot row's semantic hash or the row surfaces as a binding
    issue instead of a report.
    """
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if (
        'cad_deterministic_path_artifacts' not in tables
        or 'cad_acoustic_scene_snapshots' not in tables
    ):
        return ReflectionGuidanceView(
            document_id=document_id, entries=(), issues=()
        )

    rows = connection.execute(
        """
        SELECT
            a.artifact_id AS row_artifact_id,
            a.snapshot_id AS row_snapshot_id,
            a.recorded_at_utc AS recorded_at_utc,
            a.payload_json AS artifact_payload,
            s.semantic_sha256 AS snapshot_semantic_sha256,
            s.scene_revision_id AS scene_revision_id,
            s.scene_content_hash AS scene_content_hash,
            s.payload_json AS snapshot_payload
        FROM cad_deterministic_path_artifacts a
        JOIN cad_acoustic_scene_snapshots s
            ON s.snapshot_id = a.snapshot_id
        WHERE s.document_id = ?
        ORDER BY a.recorded_at_utc, a.artifact_id
        """,
        (document_id,),
    ).fetchall()

    entries: list[ReflectionGuidanceEntry] = []
    issues: list[ReflectionGuidanceIssue] = []
    seen_report_ids: set[str] = set()

    for row in rows:
        artifact_id = row['row_artifact_id']
        snapshot_id = row['row_snapshot_id']
        try:
            artifact = DeterministicPathArtifact.model_validate_json(
                row['artifact_payload']
            )
        except ValueError as exc:
            issues.append(
                ReflectionGuidanceIssue(
                    kind='unreadable_artifact',
                    detail=str(exc),
                    artifact_id=artifact_id,
                    snapshot_id=snapshot_id,
                )
            )
            continue
        if artifact.artifact_id != artifact_id:
            issues.append(
                ReflectionGuidanceIssue(
                    kind='binding_mismatch',
                    detail='artifact payload id does not match the row id',
                    artifact_id=artifact_id,
                    snapshot_id=snapshot_id,
                )
            )
            continue
        if artifact.snapshot_sha256 != row['snapshot_semantic_sha256']:
            issues.append(
                ReflectionGuidanceIssue(
                    kind='binding_mismatch',
                    detail=(
                        'artifact snapshot binding does not match the '
                        'persisted snapshot semantic hash'
                    ),
                    artifact_id=artifact_id,
                    snapshot_id=snapshot_id,
                )
            )
            continue

        speed, environment_state = _snapshot_environment(
            row['snapshot_payload']
        )
        snapshot_payload = _snapshot_payload_dict(row['snapshot_payload'])

        # Canonical path ordering already groups by (source, receiver):
        # one direct path plus its first-order specular siblings form the
        # pairs the diagnostic request binds.
        groups: dict[
            tuple[str, str, str],
            list[DeterministicAcousticPath],
        ] = {}
        for path in artifact.paths:
            key = (
                path.source_entity_id,
                path.receiver_id,
                path.receiver_entity_id,
            )
            groups.setdefault(key, []).append(path)

        for paths in groups.values():
            directs = [
                path for path in paths if path.path_type == 'direct'
            ]
            speculars = [
                path
                for path in paths
                if path.path_type == 'specular_reflection'
                and len(path.ordered_interaction_surface_ids) == 1
            ]
            for direct in directs:
                for specular in speculars:
                    try:
                        entry = _derive_entry(
                            artifact,
                            snapshot_id=snapshot_id,
                            scene_revision_id=row['scene_revision_id'],
                            scene_content_hash=row['scene_content_hash'],
                            speed_of_sound_m_s=speed,
                            environment_state=environment_state,
                            recorded_at_utc=row['recorded_at_utc'],
                            snapshot_payload=snapshot_payload,
                            direct=direct,
                            specular=specular,
                        )
                    except ValueError as exc:
                        issues.append(
                            ReflectionGuidanceIssue(
                                kind='derivation_failed',
                                detail=str(exc),
                                artifact_id=artifact_id,
                                snapshot_id=snapshot_id,
                            )
                        )
                        continue
                    if entry.report.report_id in seen_report_ids:
                        continue
                    seen_report_ids.add(entry.report.report_id)
                    entries.append(entry)

    return ReflectionGuidanceView(
        document_id=document_id,
        entries=tuple(entries),
        issues=tuple(issues),
    )


__all__ = [
    'EnvironmentBindingState',
    'IssueKind',
    'ReflectionGuidanceEntry',
    'ReflectionGuidanceIssue',
    'ReflectionGuidanceView',
    'ScrubUnavailableReason',
    'load_reflection_guidance_view',
]
