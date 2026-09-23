"""Interactive SBIR / reflection diagnosis authority (#533).

When a user scrubs a source position, this module recomputes exact path
distance and delay changes against the direct path and turns the geometry
into labelled interference hypotheses (comb-notch frequencies under an
explicit path-model-only semantics — never a full-field acoustic claim).
Geometry is previewed via the image method; first-reflection zones expose
the surface regions the direct + first-order specular paths actually touch.
Measured ETC matches are labelled match / ambiguous / unsupported rather
than silently merged.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_geometric_acoustics_adapter import DeterministicAcousticPath
from .cad_scene import Position3
from .r120_geometry_compiler import ExactExternalAuthorityRef


REFLECTION_DIAGNOSTIC_SCHEMA_VERSION = 1
REFLECTION_DIAGNOSTIC_AUTHORITY_VERSION = 'reflection-diagnostic-1'
DEFAULT_SPEED_OF_SOUND_M_S = 343.0
MAX_HYPOTHESIZED_COMB_TERMS = 16


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


def _distance(a: Position3, b: Position3) -> float:
    return math.sqrt(
        (float(a.x_m) - float(b.x_m)) ** 2
        + (float(a.y_m) - float(b.y_m)) ** 2
        + (float(a.z_m) - float(b.z_m)) ** 2
    )


class ReflectionDiagnosticRequest(BaseModel):
    """One direct path + one first-order specular path under diagnosis."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = REFLECTION_DIAGNOSTIC_SCHEMA_VERSION
    authority_version: Literal[
        'reflection-diagnostic-1'
    ] = REFLECTION_DIAGNOSTIC_AUTHORITY_VERSION
    request_id: str = Field(pattern=r'^reflection-diagnostic-request:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_entity_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    direct_path_id: str = Field(min_length=1)
    reflection_path_id: str = Field(min_length=1)
    speed_of_sound_m_s: float = Field(
        gt=0.0, default=DEFAULT_SPEED_OF_SOUND_M_S
    )
    solver_implementation_ref: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def validate_request(self) -> 'ReflectionDiagnosticRequest':
        if self.direct_path_id == self.reflection_path_id:
            raise ValueError('direct and reflection paths must be distinct')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('reflection diagnostic request hash mismatch')
        if self.request_id != f'reflection-diagnostic-request:{expected}':
            raise ValueError('reflection diagnostic request id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'request_id', 'semantic_sha256'},
        )


def build_reflection_diagnostic_request(**kwargs: Any) -> ReflectionDiagnosticRequest:
    probe = ReflectionDiagnosticRequest.model_construct(
        request_id='reflection-diagnostic-request:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return ReflectionDiagnosticRequest(
        request_id=f'reflection-diagnostic-request:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


class InterferenceHypothesis(BaseModel):
    """Labelled comb-interference hypothesis — path-model-only semantics.

    The notch series ``f_n = c * (2n + 1) / (2 * delta_L)`` assumes a rigid,
    phase-inverting-free first-order interaction and pure delay superposition.
    It is NOT a solver claim: boundary impedance, phase at reflection and
    source directivity are outside this model unless the authority
    explicitly supplies them.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    hypothesis_id: str = Field(pattern=r'^interference-hypothesis:[0-9a-f]{64}$')
    surface_id: str = Field(min_length=1)
    excess_path_length_m: float = Field(ge=0.0)
    excess_delay_s: float = Field(ge=0.0)
    comb_notch_hz: tuple[float, ...] = ()
    comb_peak_hz: tuple[float, ...] = ()
    hypothesis_semantics: Literal[
        'path_model_only_no_full_field_claim'
    ] = 'path_model_only_no_full_field_claim'
    phase_semantics: Literal[
        'coherent_phase_unavailable_not_synthesized',
        'phase_authority_supplied',
    ] = 'coherent_phase_unavailable_not_synthesized'

    @model_validator(mode='after')
    def validate_hypothesis(self) -> 'InterferenceHypothesis':
        if self.phase_semantics != 'phase_authority_supplied' and (
            self.comb_notch_hz or self.comb_peak_hz
        ):
            raise ValueError(
                'comb frequencies require phase authority; without it only '
                'delay deltas may be reported'
            )
        for axis in (self.comb_notch_hz, self.comb_peak_hz):
            if tuple(axis) != tuple(sorted(axis)):
                raise ValueError('comb frequencies must be sorted')
            if any(float(v) <= 0.0 for v in axis):
                raise ValueError('comb frequencies must be positive')
        expected = _digest(
            {
                'kind': 'interference-hypothesis',
                'surface_id': self.surface_id,
                'excess_path_length_m': self.excess_path_length_m,
                'excess_delay_s': self.excess_delay_s,
                'comb_notch_hz': list(self.comb_notch_hz),
                'comb_peak_hz': list(self.comb_peak_hz),
                'hypothesis_semantics': self.hypothesis_semantics,
                'phase_semantics': self.phase_semantics,
            }
        )
        if self.hypothesis_id != f'interference-hypothesis:{expected}':
            raise ValueError('interference hypothesis id mismatch')
        return self


def build_interference_hypothesis(
    request: ReflectionDiagnosticRequest,
    direct_path: DeterministicAcousticPath,
    reflection_path: DeterministicAcousticPath,
    *,
    phase_authority_supplied: bool = False,
    max_notch_hz: float = 500.0,
) -> InterferenceHypothesis:
    """Turn exact path geometry into a labelled interference hypothesis."""
    _validate_path_pair(request, direct_path, reflection_path)
    delta_l = float(reflection_path.geometric_path_length_m) - float(
        direct_path.geometric_path_length_m
    )
    if delta_l < 0.0:
        raise ValueError('specular path is shorter than the direct path')
    delta_t = float(reflection_path.propagation_delay_s) - float(
        direct_path.propagation_delay_s
    )
    surface_id = reflection_path.ordered_interaction_surface_ids[0]

    comb_notch: list[float] = []
    comb_peak: list[float] = []
    if phase_authority_supplied and delta_l > 0.0:
        c = float(request.speed_of_sound_m_s)
        n = 0
        while len(comb_notch) < MAX_HYPOTHESIZED_COMB_TERMS:
            notch = c * (2 * n + 1) / (2.0 * delta_l)
            peak = c * n / delta_l
            if notch > max_notch_hz:
                break
            comb_notch.append(notch)
            if peak > 0.0 and peak <= max_notch_hz:
                comb_peak.append(peak)
            n += 1

    hypothesis_semantics: Literal[
        'path_model_only_no_full_field_claim'
    ] = 'path_model_only_no_full_field_claim'
    phase_semantics: Literal[
        'coherent_phase_unavailable_not_synthesized',
        'phase_authority_supplied',
    ] = (
        'phase_authority_supplied'
        if phase_authority_supplied
        else 'coherent_phase_unavailable_not_synthesized'
    )
    digest = _digest(
        {
            'kind': 'interference-hypothesis',
            'surface_id': surface_id,
            'excess_path_length_m': delta_l,
            'excess_delay_s': delta_t,
            'comb_notch_hz': comb_notch,
            'comb_peak_hz': comb_peak,
            'hypothesis_semantics': hypothesis_semantics,
            'phase_semantics': phase_semantics,
        }
    )
    return InterferenceHypothesis(
        hypothesis_id=f'interference-hypothesis:{digest}',
        surface_id=surface_id,
        excess_path_length_m=delta_l,
        excess_delay_s=delta_t,
        comb_notch_hz=tuple(comb_notch),
        comb_peak_hz=tuple(comb_peak),
        phase_semantics=phase_semantics,
    )


def _validate_path_pair(
    request: ReflectionDiagnosticRequest,
    direct_path: DeterministicAcousticPath,
    reflection_path: DeterministicAcousticPath,
) -> None:
    if direct_path.path_id != request.direct_path_id:
        raise ValueError('direct path does not match the diagnostic request')
    if reflection_path.path_id != request.reflection_path_id:
        raise ValueError('reflection path does not match the diagnostic request')
    if direct_path.path_type != 'direct':
        raise ValueError('direct_path_id must reference a direct path')
    if reflection_path.path_type != 'specular_reflection':
        raise ValueError('reflection_path_id must reference a specular path')
    if len(reflection_path.ordered_interaction_surface_ids) != 1:
        raise ValueError('SBIR diagnosis covers first-order reflections only')
    for path in (direct_path, reflection_path):
        if (
            path.source_entity_id != request.source_entity_id
            or path.receiver_id != request.receiver_id
            or path.receiver_entity_id != request.receiver_entity_id
        ):
            raise ValueError('path source/receiver identity mismatch')
        if path.solver_implementation_ref != request.solver_implementation_ref:
            raise ValueError('path solver authority mismatch')


def mirror_source_across_plane(
    source_position: Position3,
    *,
    plane_point: Position3,
    plane_normal: tuple[float, float, float],
) -> Position3:
    """Image-method mirror of the source across a reflection plane."""
    nx, ny, nz = (float(v) for v in plane_normal)
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    if norm <= 0.0:
        raise ValueError('reflection plane normal must be non-zero')
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    dx = float(source_position.x_m) - float(plane_point.x_m)
    dy = float(source_position.y_m) - float(plane_point.y_m)
    dz = float(source_position.z_m) - float(plane_point.z_m)
    distance = dx * nx + dy * ny + dz * nz
    return Position3(
        x_m=float(source_position.x_m) - 2.0 * distance * nx,
        y_m=float(source_position.y_m) - 2.0 * distance * ny,
        z_m=float(source_position.z_m) - 2.0 * distance * nz,
    )


class ReflectionGeometryPreview(BaseModel):
    """Preview of a candidate source position's first-order geometry."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    request_id: str = Field(min_length=1)
    candidate_source: Position3
    mirror_source: Position3
    direct_length_m: float = Field(gt=0.0)
    specular_length_m: float = Field(gt=0.0)
    excess_path_length_m: float = Field(ge=0.0)
    excess_delay_s: float = Field(ge=0.0)
    reflection_point: Position3


def preview_reflection_geometry(
    request: ReflectionDiagnosticRequest,
    *,
    candidate_source: Position3,
    receiver_position: Position3,
    plane_point: Position3,
    plane_normal: tuple[float, float, float],
) -> ReflectionGeometryPreview:
    """Image-method preview while scrubbing a source position.

    Returns direct/specular lengths, the excess delay and the reflection
    point (intersection of the mirror-source→receiver segment with the
    plane). Segment-plane intersection: mirror and receiver on the same
    side produces an extrapolated point still reported as the geometric
    preview.
    """
    mirror = mirror_source_across_plane(
        candidate_source,
        plane_point=plane_point,
        plane_normal=plane_normal,
    )
    nx, ny, nz = (float(v) for v in plane_normal)
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    # Reflection point = where the mirror→receiver segment crosses the plane.
    sx, sy, sz = float(mirror.x_m), float(mirror.y_m), float(mirror.z_m)
    rx, ry, rz = (
        float(receiver_position.x_m),
        float(receiver_position.y_m),
        float(receiver_position.z_m),
    )
    s_plane = (sx - float(plane_point.x_m)) * nx + (
        sy - float(plane_point.y_m)
    ) * ny + (sz - float(plane_point.z_m)) * nz
    r_plane = (rx - float(plane_point.x_m)) * nx + (
        ry - float(plane_point.y_m)
    ) * ny + (rz - float(plane_point.z_m)) * nz
    denominator = s_plane - r_plane
    if abs(denominator) <= 1e-12:
        raise ValueError(
            'mirror source and receiver are plane-parallel; '
            'no finite reflection point'
        )
    t = s_plane / denominator
    reflection_point = Position3(
        x_m=sx + t * (rx - sx),
        y_m=sy + t * (ry - sy),
        z_m=sz + t * (rz - sz),
    )
    direct = _distance(candidate_source, receiver_position)
    specular = _distance(candidate_source, reflection_point) + _distance(
        reflection_point, receiver_position
    )
    excess = specular - direct
    return ReflectionGeometryPreview(
        request_id=request.request_id,
        candidate_source=candidate_source,
        mirror_source=mirror,
        direct_length_m=direct,
        specular_length_m=specular,
        excess_path_length_m=max(0.0, excess),
        excess_delay_s=max(0.0, excess) / float(request.speed_of_sound_m_s),
        reflection_point=reflection_point,
    )


class FirstReflectionZone(BaseModel):
    """Surface zone reachable by first-order specular paths for a source."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    surface_id: str = Field(min_length=1)
    centroid: Position3
    boundary_points: tuple[Position3, ...] = Field(min_length=1)


def first_reflection_zones(
    request: ReflectionDiagnosticRequest,
    reflection_paths: tuple[DeterministicAcousticPath, ...],
) -> tuple[FirstReflectionZone, ...]:
    """Aggregate first-order reflection points per surface into zones."""
    zones: dict[str, list[Position3]] = {}
    for path in reflection_paths:
        if path.path_type != 'specular_reflection':
            continue
        if (
            path.source_entity_id != request.source_entity_id
            or path.receiver_id != request.receiver_id
        ):
            continue
        if len(path.ordered_interaction_surface_ids) != 1:
            continue
        surface_id = path.ordered_interaction_surface_ids[0]
        zones.setdefault(surface_id, []).extend(
            path.ordered_interaction_points
        )
    results: list[FirstReflectionZone] = []
    for surface_id in sorted(zones):
        points = zones[surface_id]
        centroid = Position3(
            x_m=sum(float(p.x_m) for p in points) / len(points),
            y_m=sum(float(p.y_m) for p in points) / len(points),
            z_m=sum(float(p.z_m) for p in points) / len(points),
        )
        results.append(
            FirstReflectionZone(
                surface_id=surface_id,
                centroid=centroid,
                boundary_points=tuple(points),
            )
        )
    return tuple(results)


class EtcPeakMatch(BaseModel):
    """Measured-ETC peak matched to a path delay — or labelled otherwise."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    etc_peak_index: int = Field(ge=0)
    measured_delay_s: float = Field(ge=0.0)
    verdict: Literal['match', 'ambiguous', 'unsupported']
    matched_path_id: str | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def validate_match(self) -> 'EtcPeakMatch':
        if self.verdict == 'match':
            if self.matched_path_id is None:
                raise ValueError('a matched ETC peak requires its path id')
        elif self.matched_path_id is not None:
            raise ValueError('non-match verdicts cannot claim a path id')
        if self.verdict != 'match' and not self.detail:
            raise ValueError('ambiguous/unsupported verdicts require detail')
        return self


def match_etc_peaks(
    request: ReflectionDiagnosticRequest,
    paths: tuple[DeterministicAcousticPath, ...],
    etc_peak_delays_s: tuple[float, ...],
    *,
    tolerance_s: float = 0.002,
) -> tuple[EtcPeakMatch, ...]:
    """Match measured ETC peaks against deterministic path delays.

    Each measured peak gets exactly one verdict: `match` (a unique path
    within tolerance), `ambiguous` (two or more paths inside tolerance —
    no silent pick) or `unsupported` (no deterministic path explains it).
    """
    if tolerance_s <= 0.0:
        raise ValueError('ETC match tolerance must be positive')
    matches: list[EtcPeakMatch] = []
    for index, measured in enumerate(etc_peak_delays_s):
        if float(measured) < 0.0:
            raise ValueError('ETC peak delays must be non-negative')
        candidates = [
            path
            for path in paths
            if path.source_entity_id == request.source_entity_id
            and path.receiver_id == request.receiver_id
            and abs(float(path.propagation_delay_s) - float(measured))
            <= tolerance_s
        ]
        if len(candidates) == 1:
            matches.append(
                EtcPeakMatch(
                    etc_peak_index=index,
                    measured_delay_s=float(measured),
                    verdict='match',
                    matched_path_id=candidates[0].path_id,
                )
            )
        elif len(candidates) > 1:
            matches.append(
                EtcPeakMatch(
                    etc_peak_index=index,
                    measured_delay_s=float(measured),
                    verdict='ambiguous',
                    detail=(
                        'multiple deterministic paths within tolerance: '
                        + ','.join(sorted(p.path_id for p in candidates))
                    ),
                )
            )
        else:
            matches.append(
                EtcPeakMatch(
                    etc_peak_index=index,
                    measured_delay_s=float(measured),
                    verdict='unsupported',
                    detail='no deterministic path explains this peak',
                )
            )
    return tuple(matches)
