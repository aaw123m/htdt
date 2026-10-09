"""Original-node Cartesian interior flux with exact inclined-Neumann cut-Q1 roof.

All full physical y-z Cartesian rectangles retain the axis-edge 5-point
finite-volume flux. True-roof and other cut rectangles retain EXACT
physical-domain Q1 weak Neumann flux. The transition is variational,
symmetric and conservative; no fitted coefficient or sliver deletion.

On every FULL rectangular element, full Q1 stiffness in node order
(00, 10, 01, 11) is changed by c²/3 * s s.T, s=(1,-1,-1,1).
This algebraically converts local bilinear Q1 stiffness into four equal
axis-edge fluxes (conductance c²/2 per element edge); K_00=K_11=...=c²,
K_adj=-c²/2, K_opposite=0. This is a positive-semidefinite
checkerboard stabilization, and s.T @ u=0 for EVERY affine u, so the
true roof's Neumann tangential manufactured field is not disturbed.
Every cut cell keeps the Q1 physical gradient quadrature unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from scipy import sparse

from .r130d_native_cut_roof_Q1_galerkin import (
    NativePhysicalCutQ1CrossSection, cut_roof_native_original_Q1_galerkin_yz,
)


@dataclass(frozen=True)
class CartesianTrueRoofHybridFlux:
    cut_q1: NativePhysicalCutQ1CrossSection
    mass: sparse.csr_matrix
    stiffness: sparse.csr_matrix
    cartesian_full_rectangle_count: int
    exact_roof_cut_rectangle_count: int
    cartesian_full_rectangle_correction: sparse.csr_matrix
    full_rectangle_coefficient_c_squared_over_three: float

    @property
    def modes(self) -> int:
        return self.mass.shape[0]


def build_native_cartesian_true_roof_hybrid_flux(
    y_axis_m: np.ndarray,
    z_axis_m: np.ndarray,
    *,
    c_m_s: float = 343.2,
    max_active_yz_nodes: int = 3300,
) -> CartesianTrueRoofHybridFlux:
    """Assemble conservative FV interior plus exact planar natural-Neumann roof.

    The same original 8-source/8-observer positive-support Q1 nodes and
    exact consistent physical mass as the preregistered Q1 baseline remain.
    Geometry classification uses *only* physical cell corners, not waves.
    """
    q = cut_roof_native_original_Q1_galerkin_yz(
        y_axis_m, z_axis_m, c_m_s=c_m_s,
        max_active_yz_nodes=max_active_yz_nodes)
    y = q.y_native_positions_m
    z = q.z_native_positions_m
    nz = len(z)
    s = np.array([1., -1., -1., 1.])
    update = (c_m_s**2 / 3.) * np.outer(s, s)
    rr: list[int] = []
    cc: list[int] = []
    vv: list[float] = []
    count = 0
    tol = 1e-12
    lut = q.original_native_yz_to_active
    for j in range(len(y)-1):
        ya, yb = float(y[j]), float(y[j+1])
        if ya < -tol or yb > 4.+tol:
            continue
        for k in range(len(z)-1):
            za, zb = float(z[k]), float(z[k+1])
            if za < -tol or zb > 4.-.25*yb+tol:
                continue
            flat = (j*nz+k, (j+1)*nz+k, j*nz+k+1, (j+1)*nz+k+1)
            try:
                loc = [lut[a] for a in flat]
            except KeyError as exc:
                raise ValueError("full physical Cartesian cell lost Q1 node") from exc
            for a in range(4):
                for b in range(4):
                    rr.append(loc[a])
                    cc.append(loc[b])
                    vv.append(float(update[a,b]))
            count += 1
    if count < 10 or count >= q.polygon_intersecting_native_cells:
        raise ValueError("hybrid needs positive full and partially cut rectangles")
    n = q.native_yz_modes
    correction = sparse.coo_matrix(
        (vv, (rr, cc)), shape=(n, n)).tocsr()
    correction.sum_duplicates()
    K = (q.stiffness + correction).tocsr()
    K.sum_duplicates()
    M = q.mass
    if np.any(M.diagonal() <= 0) or not np.isfinite(K.data).all():
        raise ValueError("hybrid positive mass or finite stiffness lost")
    scale = max(1., float(np.max(abs(K.data))))
    asym = max(abs((K-K.T).data), default=0.)
    neumann = float(np.max(abs(K@np.ones(n))))
    if asym/scale > 5e-13 or neumann/scale > 2e-10:
        raise ValueError("hybrid is not symmetric/conservative Neumann")
    affine = q.physical_active_yz_node_positions_m
    # The extra diagonal mixed derivative term must annihilate ALL affine
    # fields, including those not satisfying the walls' Neumann condition.
    for field in (affine[:,0], affine[:,1], np.ones(n)):
        residual = correction@field
        bound = max(1.,float(np.max(abs(q.stiffness.data))))
        if np.max(abs(residual))/bound > 5e-11:
            raise ValueError("Cartesian checkerboard correction changes affine gradient")
    return CartesianTrueRoofHybridFlux(
        cut_q1=q, mass=M, stiffness=K,
        cartesian_full_rectangle_count=count,
        exact_roof_cut_rectangle_count=q.polygon_intersecting_native_cells-count,
        cartesian_full_rectangle_correction=correction,
        full_rectangle_coefficient_c_squared_over_three=c_m_s**2/3.)
