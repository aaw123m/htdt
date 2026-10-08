"""PFFDTD absorbing-halo / rigid-wall separation authority (#947).

Pinned upstream PFFDTD derives its absorbing boundary layer ``bna_ixyz``
purely from grid dimensions: every interior node at grid index ``1`` or
``N-2`` (per axis) joins the ABC set, and ``nb_update_abc`` applies the
absorbing update to it each step. The set is computed without knowledge
of ``bn_ixyz`` (the room boundary mask), so any boundary node — rigid or
lossy — placed in the ABC ring receives the absorbing update after the
boundary/leapfrog updates and *absorbs instead of reflecting* (see
``docs/R130D_GENERAL3D_REPRODUCTION_ISOLATION_RESULT_2026-10-08.md``:
wall at index 1 drains the room, tail/head RMS 0.010; wall at index 2
with a dead ring rings at 1.3275).

This module implements the HTDT-side wall-to-halo separation convention:

* :func:`assess_boundary_halo_adjacency` counts boundary nodes that lie
  inside the ABC ring (index 1 / N-2 on any axis) so mask/halo proximity
  is never silently tolerated.
* :func:`apply_boundary_halo_separation` removes boundary nodes from the
  engine's ABC set so each ring node is governed by a single update
  (the boundary physics update), restoring reflective behaviour. The
  treatment is inert on masks that are already separated — removing a
  disjoint node set cannot change the recorded trace — so replayed
  evidence keeps its pinned trace hashes.
* :func:`enforce_boundary_halo_separation` is the fail-closed gate:
  an assessed mask with adjacency that did not receive the treatment is
  ``UNSUPPORTED``/``NOT_VALIDATED``, never run quietly.

The chosen approach is an HTDT driver-side exclusion (no upstream file
edit): it preserves the pinned solver implementation authority, keeps
bit-identity on separated masks, and records pre/post ABC-set hashes in
provenance.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .canonical_json import canonical_sha256 as semantic_hash


PFFDTD_ABC_RING_CONVENTION_ID = 'pffdtd.abc_ring.index1_or_N_minus_2'
PFFDTD_HALO_SEPARATION_CONVENTION_VERSION = 'halo-separation-exclusion-v1'


class BoundaryHaloAssessment(BaseModel):
    """Count of boundary-mask nodes lying inside the PFFDTD ABC ring."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict: Literal['separated', 'halo_adjacent']
    boundary_node_count: int = Field(ge=0)
    abc_ring_boundary_node_count: int = Field(ge=0)
    rigid_ring_node_count: int = Field(ge=0)
    lossy_ring_node_count: int = Field(ge=0)
    axis_counts: dict[str, int]
    ring_linear_index_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    grid_dimensions: tuple[int, int, int]
    convention_id: str = Field(min_length=1)


def _abc_ring_mask(dimensions: tuple[int, int, int]) -> np.ndarray:
    """True at nodes the engine's ``nb_get_abc_ib`` selects: strictly
    interior nodes (all coords in [1, N-2]) lying at index 1 or N-2 on at
    least one axis — matching Nba = 2(NxNy+NxNz+NyNz) - 12(Nx+Ny+Nz) + 56.
    """
    nx, ny, nz = dimensions
    ring = np.zeros((nx, ny, nz), dtype=bool)
    interior = np.zeros((nx, ny, nz), dtype=bool)
    interior[1:-1, 1:-1, 1:-1] = True
    for axis, size in enumerate(dimensions):
        if size > 2:
            low = [slice(None)] * 3
            low[axis] = 1
            ring[tuple(low)] = True
            high = [slice(None)] * 3
            high[axis] = size - 2
            ring[tuple(high)] = True
    return ring & interior


def assess_boundary_halo_adjacency(
    *,
    bn_ixyz: Any,
    mat_bn: Any,
    dimensions: tuple[int, int, int],
    fcc: bool = False,
) -> BoundaryHaloAssessment:
    """Assess whether boundary-mask nodes sit inside the ABC ring."""

    dims = tuple(int(value) for value in dimensions)
    if len(dims) != 3 or any(value <= 2 for value in dims):
        raise ValueError(
            'boundary-halo assessment requires three grid dimensions > 2'
        )
    bn = np.asarray(bn_ixyz, dtype=np.int64).ravel()
    mat = np.asarray(mat_bn).ravel()
    if mat.size != bn.size:
        raise ValueError('mat_bn must align 1:1 with bn_ixyz')
    total = int(np.prod(np.asarray(dims, dtype=np.int64)))
    if bn.size and (int(bn.min()) < 0 or int(bn.max()) >= total):
        raise ValueError('bn_ixyz contains linear indices outside the grid')

    ring = _abc_ring_mask(dims).ravel()
    if fcc:
        # nb_get_abc_ib skips odd-parity nodes on FCC subgrids.
        ix, iy, iz = np.indices(dims)
        parity = ((ix + iy + iz) % 2 == 0).ravel()
        ring = ring & parity
    ring_members = np.flatnonzero(ring[bn]).astype(np.int64)
    axis_counts = {key: 0 for key in ('x', 'y', 'z')}
    nx, ny, nz = dims
    for row in ring_members:
        linear = int(bn[row])
        iz = linear % nz
        iy = (linear // nz) % ny
        ix = linear // (ny * nz)
        if ix in (1, nx - 2):
            axis_counts['x'] += 1
        if iy in (1, ny - 2):
            axis_counts['y'] += 1
        if iz in (1, nz - 2):
            axis_counts['z'] += 1
    rigid = int(np.count_nonzero(mat[ring_members] == -1))
    ring_linear = [int(x) for x in np.sort(bn[ring_members])]
    return BoundaryHaloAssessment(
        verdict='halo_adjacent' if ring_members.size else 'separated',
        boundary_node_count=int(bn.size),
        abc_ring_boundary_node_count=int(ring_members.size),
        rigid_ring_node_count=rigid,
        lossy_ring_node_count=int(ring_members.size) - rigid,
        axis_counts=axis_counts,
        ring_linear_index_sha256=semantic_hash(ring_linear),
        grid_dimensions=dims,
        convention_id=PFFDTD_ABC_RING_CONVENTION_ID,
    )


class HaloSeparationTreatment(BaseModel):
    """Provenance record of an applied wall-to-halo separation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    convention_version: str = Field(min_length=1)
    excluded_node_count: int = Field(ge=0)
    pre_treatment_abc_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    post_treatment_abc_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    abc_node_count_before: int = Field(ge=0)
    abc_node_count_after: int = Field(ge=0)


def _set_sha(values: Any) -> str:
    array = np.asarray(values, dtype=np.int64).ravel()
    return hashlib.sha256(array.tobytes(order='C')).hexdigest()


def apply_boundary_halo_separation(engine: Any) -> HaloSeparationTreatment:
    """Remove boundary nodes from the engine's ABC absorbing set.

    The engine derives ``bna_ixyz`` from grid dimensions alone; nodes
    that are also room-boundary nodes (``bn_ixyz``, rigid or lossy) must
    be governed by the boundary update, not the absorbing update. This
    filters ``bna_ixyz``/``Q_bna`` (and ``V_bna`` when energy tracking is
    on) and updates ``Nba`` so later ``allocate_mem`` sizes stay
    consistent. Inert when the mask is already separated.
    """

    bna = getattr(engine, 'bna_ixyz', None)
    bn = getattr(engine, 'bn_ixyz', None)
    q = getattr(engine, 'Q_bna', None)
    nba = getattr(engine, 'Nba', None)
    if bna is None or bn is None or q is None or nba is None:
        raise RuntimeError(
            'PFFDTD engine missing bna_ixyz/bn_ixyz/Q_bna/Nba — '
            'apply after load_h5_data and before allocate_mem'
        )
    bna = np.asarray(bna, dtype=np.int64).ravel()
    bn_set = np.asarray(bn, dtype=np.int64).ravel()
    q = np.asarray(q).ravel()
    if q.size != bna.size or int(nba) != int(bna.size):
        raise RuntimeError(
            'PFFDTD ABC set is inconsistent (Q_bna/Nba/bna_ixyz mismatch)'
        )

    keep = ~np.isin(bna, bn_set)
    excluded = int(bna.size - np.count_nonzero(keep))
    pre_sha = _set_sha(bna)
    new_bna = bna[keep]
    engine.bna_ixyz = new_bna
    engine.Q_bna = q[keep].astype(engine.Q_bna.dtype, copy=False)
    engine.Nba = int(new_bna.size)
    v_bna = getattr(engine, 'V_bna', None)
    if v_bna is not None:
        v_bna = np.asarray(v_bna).ravel()
        if v_bna.size == bna.size:
            engine.V_bna = v_bna[keep]
    return HaloSeparationTreatment(
        convention_version=PFFDTD_HALO_SEPARATION_CONVENTION_VERSION,
        excluded_node_count=excluded,
        pre_treatment_abc_set_sha256=pre_sha,
        post_treatment_abc_set_sha256=_set_sha(new_bna),
        abc_node_count_before=int(bna.size),
        abc_node_count_after=int(new_bna.size),
    )


def enforce_boundary_halo_separation(
    *,
    bn_ixyz: Any,
    mat_bn: Any,
    dimensions: tuple[int, int, int],
    treatment_applied: bool,
    fcc: bool = False,
) -> BoundaryHaloAssessment:
    """Fail-closed gate: adjacency without treatment is unsupported."""

    assessment = assess_boundary_halo_adjacency(
        bn_ixyz=bn_ixyz,
        mat_bn=mat_bn,
        dimensions=dimensions,
        fcc=fcc,
    )
    if assessment.verdict == 'halo_adjacent' and not treatment_applied:
        raise RuntimeError(
            'boundary mask places '
            f'{assessment.abc_ring_boundary_node_count} node(s) inside the '
            'PFFDTD absorbing-halo ring and halo separation was not applied '
            '— UNSUPPORTED/NOT_VALIDATED (issue #947)'
        )
    return assessment
