"""Actionable SBIR / reflection guidance authority (#876).

:mod:`htdt.cad_reflection_diagnostic` proves *what the geometry is*: exact
path lengths, excess delay, first-reflection zones, labelled interference
hypotheses and ETC match verdicts. This module is the next layer the issue
asks for — turning that proven path authority into *actionable* guidance a
user can act on while scrubbing a speaker position:

- ``treat_reflection_zone`` — place treatment over the surface zone the
  first-order path actually touches (the zone's interaction points, never
  a generic "put a panel on the wall");
- ``reposition_source`` — move the source so the mirror/geometry changes
  the excess delay, with the direction of change quantified;
- ``verify_with_measurement`` — confirm or retire a hypothesis against
  measured ETC evidence before treating;
- ``resolve_ambiguity`` — an ETC peak matched by several paths needs a
  discriminator, not a guess.

Every guidance item pins the request hash and the exact authorities it was
derived from; an unsupported/ambiguous measured peak can drive a
``resolve_ambiguity``/``verify_with_measurement`` item but never a
treatment claim. Guidance is advisory metadata over proven authority — it
claims nothing about full-field acoustics the underlying diagnosis did not
prove.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_geometric_acoustics_adapter import DeterministicAcousticPath
from .cad_reflection_diagnostic import (
    EtcPeakMatchReport,
    FirstReflectionZone,
    InterferenceHypothesis,
    ReflectionGeometryPreview,
    first_reflection_zones,
    preview_reflection_geometry,
    ReflectionDiagnosticRequest,
)
from .cad_scene import Position3
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


REFLECTION_GUIDANCE_SCHEMA_VERSION = 1
REFLECTION_GUIDANCE_AUTHORITY_VERSION = 'reflection-guidance-1'






GuidanceKind = Literal[
    'treat_reflection_zone',
    'reposition_source',
    'verify_with_measurement',
    'resolve_ambiguity',
]

GuidanceConfidence = Literal[
    'authority_backed',
    'measured_supported',
    'unverified_hypothesis',
]


class ReflectionGuidanceItem(BaseModel):
    """One actionable recommendation bound to exact path authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    item_id: str = Field(pattern=r'^reflection-guidance:[0-9a-f]{64}$')
    kind: GuidanceKind
    confidence: GuidanceConfidence
    rank: int = Field(ge=1)
    rationale: str = Field(min_length=1)
    action: str = Field(min_length=1)
    surface_id: str | None = Field(default=None, min_length=1)
    reflection_point: Position3 | None = None
    source_entity_id: str | None = Field(default=None, min_length=1)
    path_id: str | None = Field(default=None, min_length=1)
    hypothesis_id: str | None = Field(default=None, min_length=1)
    etc_report_id: str | None = Field(default=None, min_length=1)
    excess_delay_s: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def validate_item(self) -> 'ReflectionGuidanceItem':
        if self.kind in (
            'treat_reflection_zone',
            'resolve_ambiguity',
        ) and self.surface_id is None:
            raise ValueError(
                f'{self.kind} items must name the surface they act on'
            )
        if self.kind == 'reposition_source' and self.source_entity_id is None:
            raise ValueError(
                'reposition_source items must name the source entity'
            )
        expected = _digest(self.semantic_payload())
        if self.item_id != f'reflection-guidance:{expected}':
            raise ValueError('reflection guidance item id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'item_id'})


def _guidance_item(payload: dict[str, Any]) -> ReflectionGuidanceItem:
    probe = ReflectionGuidanceItem.model_construct(
        item_id='reflection-guidance:' + '0' * 64, **payload
    )
    digest = _digest(probe.semantic_payload())
    return ReflectionGuidanceItem(
        item_id=f'reflection-guidance:{digest}', **payload
    )


class ReflectionGuidanceReport(BaseModel):
    """Ordered guidance set for one diagnostic request — immutable output."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = REFLECTION_GUIDANCE_SCHEMA_VERSION
    authority_version: Literal[
        'reflection-guidance-1'
    ] = REFLECTION_GUIDANCE_AUTHORITY_VERSION
    report_id: str = Field(
        pattern=r'^reflection-guidance-report:[0-9a-f]{64}$'
    )
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    request_id: str = Field(min_length=1)
    request_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    status: Literal['actionable', 'diagnostics_only', 'blocked']
    items: tuple[ReflectionGuidanceItem, ...]

    @model_validator(mode='after')
    def validate_report(self) -> 'ReflectionGuidanceReport':
        ranks = [item.rank for item in self.items]
        if sorted(ranks) != ranks:
            raise ValueError('guidance items must be ordered by rank')
        if self.status == 'actionable' and not self.items:
            raise ValueError('an actionable report requires items')
        if self.status == 'blocked' and self.items:
            raise ValueError('a blocked report cannot carry guidance items')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('reflection guidance report hash mismatch')
        if self.report_id != f'reflection-guidance-report:{expected}':
            raise ValueError('reflection guidance report id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'report_id', 'semantic_sha256'},
        )


def build_reflection_guidance(
    request: ReflectionDiagnosticRequest,
    direct_path: DeterministicAcousticPath,
    reflection_paths: tuple[DeterministicAcousticPath, ...],
    *,
    hypotheses: tuple[InterferenceHypothesis, ...] = (),
    zones: tuple[FirstReflectionZone, ...] | None = None,
    etc_report: EtcPeakMatchReport | None = None,
) -> ReflectionGuidanceReport:
    """Turn proven path authority into an ordered actionable guidance set.

    ``reflection_paths`` must each satisfy the diagnostic request contract
    (same source/receiver, first-order specular only) — anything else is a
    binding error, not a skipped path. ``zones`` defaults to
    :func:`first_reflection_zones` over the given paths.

    Confidence ladder: a reflection the ETC report marked
    ``timing_compatible_candidate`` yields ``measured_supported``
    treatment/reposition items; without measured support the same items are
    ``unverified_hypothesis`` and a ``verify_with_measurement`` item is
    emitted first when a hypothesis exists but no compatible measured peak
    was bound. ``ambiguous_candidate`` verdicts emit ``resolve_ambiguity``
    items; a report whose every reflection peak is ``unsupported`` blocks
    treatment claims entirely.
    """

    if direct_path.path_id != request.direct_path_id:
        raise ValueError('direct path does not match the diagnostic request')
    eligible: list[DeterministicAcousticPath] = []
    for path in reflection_paths:
        if path.path_type != 'specular_reflection':
            raise ValueError('guidance covers first-order specular paths')
        if len(path.ordered_interaction_surface_ids) != 1:
            raise ValueError('guidance covers first-order specular paths')
        if (
            path.source_entity_id != request.source_entity_id
            or path.receiver_id != request.receiver_id
            or path.receiver_entity_id != request.receiver_entity_id
        ):
            raise ValueError('path source/receiver identity mismatch')
        if path.solver_implementation_ref != request.solver_implementation_ref:
            raise ValueError('path solver authority mismatch')
        eligible.append(path)

    if zones is None:
        zones = first_reflection_zones(request, tuple(eligible))

    hypothesis_by_surface = {
        hypothesis.surface_id: hypothesis for hypothesis in hypotheses
    }

    # ETC verdict per candidate path.
    supported_path_ids: set[str] = set()
    ambiguous_peak = False
    unsupported_reflection_peak = False
    if etc_report is not None:
        if etc_report.request_id != request.request_id:
            raise ValueError(
                'ETC match report does not belong to this diagnostic request'
            )
        eligible_ids = {path.path_id for path in eligible}
        for match in etc_report.matches:
            if match.verdict == 'timing_compatible_candidate':
                # Only a supported reflection corroborates treatment —
                # a matched *direct* peak says nothing about the boundary.
                if match.candidate_path_id in eligible_ids:
                    supported_path_ids.add(match.candidate_path_id)
            elif match.verdict == 'ambiguous_candidate':
                ambiguous_peak = True
            elif (
                match.verdict == 'unsupported'
                and match.candidate_path_id is None
            ):
                unsupported_reflection_peak = True

    items: list[ReflectionGuidanceItem] = []
    rank = 1

    for zone in zones:
        surface = zone.surface_id
        surface_paths = [
            path
            for path in eligible
            if path.ordered_interaction_surface_ids[0] == surface
        ]
        hypothesis = hypothesis_by_surface.get(surface)
        measured = any(
            path.path_id in supported_path_ids for path in surface_paths
        )
        excess_delays = tuple(
            float(path.propagation_delay_s)
            - float(direct_path.propagation_delay_s)
            for path in surface_paths
        )
        worst_excess = max(excess_delays) if excess_delays else 0.0
        confidence: GuidanceConfidence = (
            'measured_supported'
            if measured
            else 'unverified_hypothesis'
        )

        centroid = zone.centroid
        items.append(
            _guidance_item(
                {
                    'kind': 'treat_reflection_zone',
                    'confidence': confidence,
                    'rank': rank,
                    'rationale': (
                        f'first-order specular path(s) touch {surface}; '
                        f'excess delay up to {worst_excess * 1e3:.1f} ms'
                        + (
                            ' with a timing-compatible measured ETC peak'
                            if measured
                            else ''
                        )
                    ),
                    'action': (
                        f'place absorptive/diffusive treatment on {surface} '
                        f'covering the first-reflection zone centred at '
                        f'({centroid.x_m:.2f}, {centroid.y_m:.2f}, '
                        f'{centroid.z_m:.2f}) m'
                    ),
                    'surface_id': surface,
                    'reflection_point': centroid,
                    'source_entity_id': request.source_entity_id,
                    'path_id': (
                        request.reflection_path_id
                        if request.reflection_path_id
                        in {p.path_id for p in surface_paths}
                        else (
                            sorted(p.path_id for p in surface_paths)[0]
                            if surface_paths
                            else None
                        )
                    ),
                    'hypothesis_id': (
                        hypothesis.hypothesis_id
                        if hypothesis is not None
                        else None
                    ),
                    'etc_report_id': (
                        etc_report.report_id
                        if etc_report is not None and measured
                        else None
                    ),
                    'excess_delay_s': worst_excess,
                }
            )
        )
        rank += 1

        items.append(
            _guidance_item(
                {
                    'kind': 'reposition_source',
                    'confidence': confidence,
                    'rank': rank,
                    'rationale': (
                        f'repositioning {request.source_entity_id} changes '
                        f'the {surface} path geometry and the excess delay '
                        'that sets the interference spacing'
                    ),
                    'action': (
                        f'move {request.source_entity_id} to lengthen the '
                        f'direct/excess ratio for {surface}, or pull it off '
                        'the boundary to push first-order energy later'
                    ),
                    'surface_id': surface,
                    'source_entity_id': request.source_entity_id,
                    'path_id': items[-1].path_id,
                    'hypothesis_id': items[-1].hypothesis_id,
                    'etc_report_id': items[-1].etc_report_id,
                    'excess_delay_s': worst_excess,
                }
            )
        )
        rank += 1

        if hypothesis is not None and not measured:
            items.append(
                _guidance_item(
                    {
                        'kind': 'verify_with_measurement',
                        'confidence': 'unverified_hypothesis',
                        'rank': rank,
                        'rationale': (
                            f'the {surface} interference hypothesis is '
                            'path-model-only; no timing-compatible measured '
                            'ETC peak is bound to it'
                        ),
                        'action': (
                            'capture a measured ETC at the receiver and match '
                            'peaks before committing treatment for '
                            f'{surface}'
                        ),
                        'surface_id': surface,
                        'source_entity_id': request.source_entity_id,
                        'hypothesis_id': hypothesis.hypothesis_id,
                        'etc_report_id': (
                            etc_report.report_id
                            if etc_report is not None
                            else None
                        ),
                        'excess_delay_s': float(
                            hypothesis.excess_delay_s
                        ),
                    }
                )
            )
            rank += 1

    if ambiguous_peak and etc_report is not None:
        # Ambiguous matches deliberately carry no candidate_path_id —
        # recover the competing surfaces by re-running the tolerance
        # comparison against the reported measured delay.
        tolerance = float(etc_report.tolerance_s)
        direct_peak_delay: float | None = None
        if (
            etc_report.matching_mode == 'relative_to_direct'
            and etc_report.direct_peak_index is not None
            and etc_report.direct_peak_index < len(etc_report.matches)
        ):
            direct_peak_delay = float(
                etc_report.matches[
                    etc_report.direct_peak_index
                ].measured_delay_s
            )

        def _peak_compatible(
            path: DeterministicAcousticPath, measured_delay_s: float
        ) -> bool:
            if (
                etc_report.matching_mode == 'relative_to_direct'
                and direct_peak_delay is not None
            ):
                model_delay = float(path.propagation_delay_s) - float(
                    direct_path.propagation_delay_s
                )
                measured_delay = measured_delay_s - direct_peak_delay
            else:
                model_delay = float(path.propagation_delay_s)
                measured_delay = measured_delay_s
            return abs(model_delay - measured_delay) <= tolerance

        for match in etc_report.matches:
            if match.verdict != 'ambiguous_candidate':
                continue
            surfaces = sorted(
                {
                    path.ordered_interaction_surface_ids[0]
                    for path in eligible
                    if _peak_compatible(path, match.measured_delay_s)
                }
            )
            if not surfaces:
                continue
            items.append(
                _guidance_item(
                    {
                        'kind': 'resolve_ambiguity',
                        'confidence': 'authority_backed',
                        'rank': rank,
                        'rationale': (
                            'a measured ETC peak is within tolerance of '
                            'multiple deterministic paths'
                        ),
                        'action': (
                            'tighten the ETC match tolerance, add a '
                            'time-window or take a second measurement '
                            f'position before acting on {surfaces[0]}'
                            + (
                                f' (also {", ".join(surfaces[1:])})'
                                if len(surfaces) > 1
                                else ''
                            )
                        ),
                        'surface_id': surfaces[0],
                        'source_entity_id': request.source_entity_id,
                        'etc_report_id': etc_report.report_id,
                    }
                )
            )
            rank += 1

    status: Literal['actionable', 'diagnostics_only', 'blocked']
    if items and any(
        item.kind in ('treat_reflection_zone', 'reposition_source')
        for item in items
    ):
        status = 'actionable'
    elif items:
        status = 'diagnostics_only'
    else:
        status = 'blocked'
    # A fully-unsupported measured record narrows the report to
    # diagnostics-only: the path authority is proven, but nothing measured
    # corroborates acting on it.
    if (
        status == 'actionable'
        and etc_report is not None
        and unsupported_reflection_peak
        and not supported_path_ids
        and not any(
            item.kind in ('treat_reflection_zone', 'reposition_source')
            and item.confidence == 'measured_supported'
            for item in items
        )
    ):
        status = 'diagnostics_only'

    payload = {
        'schema_version': REFLECTION_GUIDANCE_SCHEMA_VERSION,
        'authority_version': REFLECTION_GUIDANCE_AUTHORITY_VERSION,
        'request_id': request.request_id,
        'request_semantic_sha256': request.semantic_sha256,
        'scene_revision_id': request.scene_revision_id,
        'scene_content_hash': request.scene_content_hash,
        'status': status,
        'items': [item.model_dump(mode='json') for item in items],
    }
    digest = _digest(payload)
    return ReflectionGuidanceReport(
        request_id=request.request_id,
        request_semantic_sha256=request.semantic_sha256,
        scene_revision_id=request.scene_revision_id,
        scene_content_hash=request.scene_content_hash,
        status=status,
        items=tuple(items),
        report_id=f'reflection-guidance-report:{digest}',
        semantic_sha256=digest,
    )


class ReflectionGuidanceSession(BaseModel):
    """Interactive diagnosis session — the UX view-model (#876).

    The user scrubs a candidate source position; each ``scrub`` recomputes
    the exact mirror/preview geometry under the pinned request and reports
    what the move would do to the diagnosed excess delay — guidance state
    for one interactive frame. The committed request/path authority never
    mutates; a candidate preview is a *proposal* that only becomes
    authority when applied to the scene.
    """

    model_config = ConfigDict(extra='forbid')

    request: ReflectionDiagnosticRequest
    direct_path: DeterministicAcousticPath
    reflection_path: DeterministicAcousticPath
    receiver_position: Position3
    plane_point: Position3
    plane_normal: tuple[float, float, float]
    baseline_excess_delay_s: float = Field(ge=0.0)
    frames: tuple['ReflectionGuidanceFrame', ...] = ()

    @model_validator(mode='after')
    def validate_session(self) -> 'ReflectionGuidanceSession':
        for path in (self.direct_path, self.reflection_path):
            if (
                path.source_entity_id != self.request.source_entity_id
                or path.receiver_id != self.request.receiver_id
                or path.receiver_entity_id != self.request.receiver_entity_id
            ):
                raise ValueError('session path source/receiver mismatch')
        if self.direct_path.path_id != self.request.direct_path_id:
            raise ValueError('session direct path does not match request')
        if self.reflection_path.path_id != self.request.reflection_path_id:
            raise ValueError('session reflection path does not match request')
        return self


def open_guidance_session(
    request: ReflectionDiagnosticRequest,
    direct_path: DeterministicAcousticPath,
    reflection_path: DeterministicAcousticPath,
    *,
    receiver_position: Position3,
    plane_point: Position3,
    plane_normal: tuple[float, float, float],
) -> ReflectionGuidanceSession:
    """Open an interactive session pinned to the diagnosed path pair."""
    baseline = max(
        0.0,
        float(reflection_path.propagation_delay_s)
        - float(direct_path.propagation_delay_s),
    )
    return ReflectionGuidanceSession(
        request=request,
        direct_path=direct_path,
        reflection_path=reflection_path,
        receiver_position=receiver_position,
        plane_point=plane_point,
        plane_normal=plane_normal,
        baseline_excess_delay_s=baseline,
    )


class ReflectionGuidanceFrame(BaseModel):
    """One interactive frame: candidate position → preview + delta."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    frame_index: int = Field(ge=0)
    candidate_source: Position3
    preview: ReflectionGeometryPreview
    excess_delay_delta_s: float
    direction: Literal['improves', 'worsens', 'unchanged']


def scrub_source(
    session: ReflectionGuidanceSession, candidate_source: Position3
) -> ReflectionGuidanceSession:
    """Append one scrub frame to the session.

    The preview is exact image-method geometry under the request's pinned
    plane; ``direction`` compares the candidate's excess delay to the
    committed baseline — ``improves`` means the move lengthens the excess
    delay (the reflection arrives later relative to the direct), never a
    claim about audibility.
    """
    preview = preview_reflection_geometry(
        session.request,
        candidate_source=candidate_source,
        receiver_position=session.receiver_position,
        plane_point=session.plane_point,
        plane_normal=session.plane_normal,
    )
    delta = preview.excess_delay_s - session.baseline_excess_delay_s
    if delta > 1e-6:
        direction: Literal['improves', 'worsens', 'unchanged'] = 'improves'
    elif delta < -1e-6:
        direction = 'worsens'
    else:
        direction = 'unchanged'
    frame = ReflectionGuidanceFrame(
        frame_index=len(session.frames),
        candidate_source=candidate_source,
        preview=preview,
        excess_delay_delta_s=delta,
        direction=direction,
    )
    return session.model_copy(
        update={'frames': (*session.frames, frame)}
    )
