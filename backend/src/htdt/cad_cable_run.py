"""Cable-run / wiring-plan authority (#538, fulfills the #176 contract).

A cable run is a first-class, named, versioned authority: the route a
physical cable actually takes — path kind per segment, explicit segment
lengths, service loop, endpoints bound to scene entities and optionally the
signal-path edge it realizes. Install-plan and cost/export outputs consume
the *recorded* run instead of inferring length from Euclidean placement
distance (which under-reads real routing through walls/ceilings/conduit and
over-reads nothing else).

Contract properties:

- route segments and lengths are saved explicitly per segment —
  ``total_length_m`` is a deterministic sum (segments + service loop), so a
  plan/cost/export derivation never re-infers distance from placement;
- the run pins a SceneRevision content hash and may reference a
  ``SignalPathEdge.edge_id`` and scene ``entity_id`` endpoints — drift is
  surfaced by :func:`evaluate_cable_run_freshness` as ``current``/``stale``/
  ``missing``, never by rewriting the record;
- ``path_kind`` is explicit per segment (in-wall, conduit, under-floor, …) —
  routing is an installation fact, not an assumption label;
- cable identity (kind/medium/gauge) is explicit on the run so BOM-style
  exports can group by it without parsing descriptions.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


CABLE_RUN_AUTHORITY_VERSION = 'cable-run-1'

CableRunKind = Literal[
    'speaker_signal',
    'analog_audio',
    'digital_audio',
    'video',
    'power',
    'network',
    'control',
    'other',
]
CableMedium = Literal['copper', 'fiber', 'other']
CablePathKind = Literal[
    'in_wall',
    'in_ceiling',
    'under_floor',
    'conduit',
    'surface_raceway',
    'exposed',
    'other',
]
CableRunFreshnessStatus = Literal['current', 'stale', 'missing']


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


class CableRunEndpoint(BaseModel):
    """One end of a run. ``entity_id`` binds to a scene entity when the end
    is placed equipment; ``label`` stays mandatory so a panel/rack end that
    is not a scene entity is still named explicitly."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    entity_id: str | None = Field(default=None, min_length=1)


class CableRunSegment(BaseModel):
    """One explicit route segment with its declared path kind and length."""

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=0)
    path_kind: CablePathKind
    length_m: float = Field(gt=0.0)
    description: str | None = None

    @model_validator(mode='after')
    def finite_length(self) -> 'CableRunSegment':
        if not isfinite(float(self.length_m)):
            raise ValueError('segment length must be finite')
        return self


class CableRun(BaseModel):
    """Versioned cable-run record pinned to one SceneRevision."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['cable-run-1'] = CABLE_RUN_AUTHORITY_VERSION
    run_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    label: str = Field(min_length=1)
    kind: CableRunKind
    medium: CableMedium = 'copper'
    gauge: str | None = None
    from_endpoint: CableRunEndpoint
    to_endpoint: CableRunEndpoint
    segments: tuple[CableRunSegment, ...] = Field(min_length=1)
    service_loop_m: float = Field(default=0.0, ge=0.0)
    total_length_m: float = Field(gt=0.0)
    #: Optional binding to the signal-path edge this run realizes.
    signal_path_edge_id: str | None = Field(default=None, min_length=1)
    install_notes: str | None = None
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_run(self) -> 'CableRun':
        sequences = [segment.sequence for segment in self.segments]
        if len(set(sequences)) != len(sequences):
            raise ValueError('cable run segment sequences must be unique')
        if sequences != sorted(sequences):
            raise ValueError('cable run segments must be ordered by sequence')
        if not isfinite(float(self.service_loop_m)):
            raise ValueError('service loop must be finite')
        expected = sum(
            segment.length_m for segment in self.segments
        ) + self.service_loop_m
        if abs(self.total_length_m - expected) > 1e-9:
            raise ValueError(
                'total_length_m must equal segments plus service loop'
            )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CableRun semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'run_id': self.run_id,
            'version': self.version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'label': self.label,
            'kind': self.kind,
            'medium': self.medium,
            'gauge': self.gauge,
            'from_endpoint': self.from_endpoint.model_dump(mode='json'),
            'to_endpoint': self.to_endpoint.model_dump(mode='json'),
            'segments': [
                segment.model_dump(mode='json') for segment in self.segments
            ],
            'service_loop_m': self.service_loop_m,
            'total_length_m': self.total_length_m,
            'signal_path_edge_id': self.signal_path_edge_id,
            'install_notes': self.install_notes,
            'created_at_utc': self.created_at_utc,
        }


def build_cable_run(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    label: str,
    kind: CableRunKind,
    from_endpoint: CableRunEndpoint,
    to_endpoint: CableRunEndpoint,
    segments: Sequence[CableRunSegment],
    created_at_utc: str,
    run_id: str | None = None,
    version: str = '1',
    medium: CableMedium = 'copper',
    gauge: str | None = None,
    service_loop_m: float = 0.0,
    signal_path_edge_id: str | None = None,
    install_notes: str | None = None,
) -> CableRun:
    ordered = tuple(segments)
    total = sum(segment.length_m for segment in ordered) + service_loop_m
    payload: dict[str, Any] = {
        'authority_version': CABLE_RUN_AUTHORITY_VERSION,
        'run_id': run_id or str(uuid4()),
        'version': version,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'label': label,
        'kind': kind,
        'medium': medium,
        'gauge': gauge,
        'from_endpoint': from_endpoint,
        'to_endpoint': to_endpoint,
        'segments': ordered,
        'service_loop_m': service_loop_m,
        'total_length_m': total,
        'signal_path_edge_id': signal_path_edge_id,
        'install_notes': install_notes,
        'created_at_utc': created_at_utc,
    }
    provisional = CableRun.model_construct(**payload, semantic_sha256='0' * 64)
    return CableRun(
        **payload,
        semantic_sha256=_hash(provisional.semantic_payload()),
    )


class CableRunFreshness(BaseModel):
    """Read-only drift check for one persisted run (never rewrites it)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    version: str
    status: CableRunFreshnessStatus
    reasons: tuple[str, ...] = ()


def evaluate_cable_run_freshness(
    run: CableRun,
    *,
    scene_content_hash: str,
    present_entity_ids: Sequence[str],
    present_signal_path_edge_ids: Sequence[str],
) -> CableRunFreshness:
    """Report whether a saved run still maps onto current scene/edges."""

    reasons: list[str] = []
    status: CableRunFreshnessStatus = 'current'
    if run.scene_content_hash != scene_content_hash:
        status = 'stale'
        reasons.append(
            'scene content hash changed since the cable run was recorded'
        )
    entity_ids = set(present_entity_ids)
    for side, endpoint in (
        ('from', run.from_endpoint),
        ('to', run.to_endpoint),
    ):
        if endpoint.entity_id is not None and endpoint.entity_id not in entity_ids:
            status = 'missing'
            reasons.append(
                f'{side} endpoint entity absent: {endpoint.entity_id}'
            )
    if (
        run.signal_path_edge_id is not None
        and run.signal_path_edge_id not in set(present_signal_path_edge_ids)
    ):
        status = 'missing'
        reasons.append(
            f'signal path edge absent: {run.signal_path_edge_id}'
        )
    return CableRunFreshness(
        run_id=run.run_id,
        version=run.version,
        status=status,
        reasons=tuple(reasons),
    )


__all__ = [
    'CABLE_RUN_AUTHORITY_VERSION',
    'CableMedium',
    'CablePathKind',
    'CableRun',
    'CableRunEndpoint',
    'CableRunFreshness',
    'CableRunFreshnessStatus',
    'CableRunKind',
    'CableRunSegment',
    'build_cable_run',
    'evaluate_cable_run_freshness',
]
