"""Issue #947: wall-to-halo separation authority.

The pinned PFFDTD engine derives its absorbing (ABC) node set from grid
dimensions alone: every interior node at grid index 1 or N-2 on each
axis receives the absorbing update — after the boundary update — so a
rigid wall at index 1/N-2 absorbs instead of reflecting. The treatment
excludes boundary-mask nodes from the engine's ABC set; it must be
inert on masks that are already separated.

``bn_ixyz``/``bna_ixyz`` are linear (raveled) node indices, matching
the vox_out.h5 / SimEngine convention.
"""
from __future__ import annotations

import numpy as np
import pytest

from htdt.pffdtd_boundary_halo import (
    PFFDTD_ABC_RING_CONVENTION_ID,
    PFFDTD_HALO_SEPARATION_CONVENTION_VERSION,
    assess_boundary_halo_adjacency,
    apply_boundary_halo_separation,
    enforce_boundary_halo_separation,
)

DIMS = (6, 6, 6)


def _lin(i, j, k, dims=DIMS):
    return i * dims[1] * dims[2] + j * dims[2] + k


def _mask(*nodes):
    bn = np.array([_lin(*n) for n in nodes], dtype=np.int64)
    return bn, np.full(len(bn), -1, dtype=np.int64)


def _ring_linear(dims=DIMS):
    nx, ny, nz = dims
    ix, iy, iz = np.indices(dims)
    idx = np.stack((ix, iy, iz), axis=-1).reshape(-1, 3)
    on_ring = (idx == 1).any(axis=1) | (
        idx == (np.array(dims) - 2)
    ).any(axis=1)
    interior = ((idx > 0) & (idx < (np.array(dims) - 1))).all(axis=1)
    nodes = idx[on_ring & interior]
    return np.unique(
        nodes[:, 0] * (ny * nz) + nodes[:, 1] * nz + nodes[:, 2]
    )


class _FakeEngine:
    """Minimal stand-in for the loaded SimEngine ABC state."""

    def __init__(self, dims, bn_ixyz=()):
        self.bna_ixyz = _ring_linear(dims)
        self.bn_ixyz = np.asarray(list(bn_ixyz), dtype=np.int64)
        self.Nba = int(self.bna_ixyz.size)
        self.Q_bna = np.ones(self.Nba, dtype=np.float64)


def test_assess_separated_mask_has_no_ring_nodes():
    bn, mat = _mask((3, 3, 3), (2, 3, 3))
    result = assess_boundary_halo_adjacency(
        bn_ixyz=bn, mat_bn=mat, dimensions=DIMS
    )
    assert result.verdict == 'separated'
    assert result.abc_ring_boundary_node_count == 0
    assert result.boundary_node_count == 2


def test_assess_index_one_wall_is_halo_adjacent():
    bn, mat = _mask((1, 3, 3), (3, 3, 3))
    result = assess_boundary_halo_adjacency(
        bn_ixyz=bn, mat_bn=mat, dimensions=DIMS
    )
    assert result.verdict == 'halo_adjacent'
    assert result.abc_ring_boundary_node_count == 1
    assert result.rigid_ring_node_count == 1
    assert result.axis_counts['x'] == 1
    assert result.axis_counts['y'] == 0
    assert result.convention_id == PFFDTD_ABC_RING_CONVENTION_ID


def test_assess_N_minus_2_corner_counts_each_axis():
    bn, mat = _mask((4, 4, 4))
    result = assess_boundary_halo_adjacency(
        bn_ixyz=bn, mat_bn=mat, dimensions=DIMS
    )
    assert result.verdict == 'halo_adjacent'
    assert result.abc_ring_boundary_node_count == 1
    assert result.axis_counts == {'x': 1, 'y': 1, 'z': 1}


def test_assess_rejects_out_of_grid_indices():
    bn = np.array([_lin(7, 1, 1)], dtype=np.int64)
    with pytest.raises(ValueError, match='outside the grid'):
        assess_boundary_halo_adjacency(
            bn_ixyz=bn, mat_bn=np.array([-1]), dimensions=DIMS
        )


def test_enforce_raises_on_untreated_adjacent_mask():
    bn, mat = _mask((1, 3, 3))
    with pytest.raises(RuntimeError, match='UNSUPPORTED'):
        enforce_boundary_halo_separation(
            bn_ixyz=bn,
            mat_bn=mat,
            dimensions=DIMS,
            treatment_applied=False,
        )


def test_enforce_passes_treated_or_separated():
    bn1, m1 = _mask((1, 3, 3))
    assert (
        enforce_boundary_halo_separation(
            bn_ixyz=bn1,
            mat_bn=m1,
            dimensions=DIMS,
            treatment_applied=True,
        ).verdict
        == 'halo_adjacent'
    )
    bn2, m2 = _mask((3, 3, 3))
    assert (
        enforce_boundary_halo_separation(
            bn_ixyz=bn2,
            mat_bn=m2,
            dimensions=DIMS,
            treatment_applied=False,
        ).verdict
        == 'separated'
    )


def test_treatment_removes_boundary_nodes_from_abc_set():
    ring = _ring_linear()
    target = _lin(1, 3, 3)
    assert target in set(int(x) for x in ring)
    engine = _FakeEngine(DIMS, bn_ixyz=[target])
    pre = engine.bna_ixyz.copy()
    result = apply_boundary_halo_separation(engine)
    assert result.excluded_node_count == 1
    assert result.abc_node_count_before == int(ring.size)
    assert result.abc_node_count_after == int(ring.size) - 1
    assert result.convention_version == (
        PFFDTD_HALO_SEPARATION_CONVENTION_VERSION
    )
    assert engine.Nba == int(ring.size) - 1
    assert int(engine.Q_bna.size) == engine.Nba
    assert target not in set(int(x) for x in engine.bna_ixyz)
    assert engine.bna_ixyz.size + 1 == pre.size
    assert (
        result.pre_treatment_abc_set_sha256
        != result.post_treatment_abc_set_sha256
    )


def test_treatment_inert_on_separated_mask():
    engine = _FakeEngine(DIMS, bn_ixyz=[_lin(3, 3, 3)])
    before = engine.bna_ixyz.copy()
    result = apply_boundary_halo_separation(engine)
    assert result.excluded_node_count == 0
    assert np.array_equal(engine.bna_ixyz, before)
    assert (
        result.pre_treatment_abc_set_sha256
        == result.post_treatment_abc_set_sha256
    )
    assert engine.Nba == int(before.size)


def test_treatment_requires_engine_abc_attrs():
    with pytest.raises(RuntimeError, match='PFFDTD engine missing'):
        apply_boundary_halo_separation(object())


def test_treatment_rejects_inconsistent_abc_state():
    engine = _FakeEngine(DIMS)
    engine.Nba += 1
    with pytest.raises(RuntimeError, match='inconsistent'):
        apply_boundary_halo_separation(engine)
