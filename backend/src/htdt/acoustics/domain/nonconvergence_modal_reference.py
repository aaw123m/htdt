"""Pure-Python finite-element modal reference for non-convergence diagnosis.

Implements the discrete mathematics that the audited MFEM probes encode
(mass / c^2-weighted stiffness assembly, point-delta source and receiver
functionals, generalized modal reconstruction of the impulse response) in
plain numpy/scipy, so that non-converging benchmarks can be re-derived
without a compiled MFEM toolchain.

Scope and honesty rules (REV71):

* This module is an INDEPENDENT reference implementation.  It does not
  claim MFEM bit-identity; evidence produced from it is labelled
  ``python_independent_reference`` and carries its own environment block.
* Element matrices are exact to machine precision: 10-node tetrahedra are
  integrated in closed form from barycentric monomial identities; tensor
  hexahedra of order p use exact (p+1)-point-per-axis Gauss-Legendre
  quadrature of degree-(2p) products.
* Uniform tetrahedral refinement uses a fixed, documented octahedron
  diagonal convention (the diagonal joining the midpoints of the base
  edges (v0,v1) and (v2,v3)); MFEM picks its own internal convention, so
  refined meshes agree in element/vertex/edge counts and continuum limit
  but not necessarily in bit-identical interior connectivity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Exact barycentric monomial integration on the reference tetrahedron.
# ---------------------------------------------------------------------------
#
# On the reference tet T = {lambda_i >= 0, sum lambda_i = 1} (volume 1/6):
#
#     ∫_T λ0^a λ1^b λ2^c λ3^d dV = (a! b! c! d!) / (a+b+c+d+3)!.
#
# (The 6·V factor is already absorbed: 6·(1/6) = 1.)


def _monomial_integral(exp: tuple[int, int, int, int]) -> float:
    total = sum(exp)
    numerator = math.factorial(exp[0]) * math.factorial(exp[1]) * math.factorial(exp[2]) * math.factorial(exp[3])
    return numerator / math.factorial(total + 3)


def _poly_add(
    a: dict[tuple[int, int, int, int], float],
    b: dict[tuple[int, int, int, int], float],
    scale_b: float = 1.0,
) -> dict[tuple[int, int, int, int], float]:
    out = dict(a)
    for key, value in b.items():
        out[key] = out.get(key, 0.0) + scale_b * value
    return out


def _poly_mul(
    a: dict[tuple[int, int, int, int], float],
    b: dict[tuple[int, int, int, int], float],
) -> dict[tuple[int, int, int, int], float]:
    out: dict[tuple[int, int, int, int], float] = {}
    for ea, ca in a.items():
        for eb, cb in b.items():
            key = (ea[0] + eb[0], ea[1] + eb[1], ea[2] + eb[2], ea[3] + eb[3])
            out[key] = out.get(key, 0.0) + ca * cb
    return out


def _poly_diff(
    p: dict[tuple[int, int, int, int], float], axis: int
) -> dict[tuple[int, int, int, int], float]:
    out: dict[tuple[int, int, int, int], float] = {}
    for exp, coeff in p.items():
        k = exp[axis]
        if k == 0:
            continue
        lowered = list(exp)
        lowered[axis] = k - 1
        key = tuple(lowered)
        out[key] = out.get(key, 0.0) + coeff * k
    return out


def _poly_integrate(p: dict[tuple[int, int, int, int], float]) -> float:
    return sum(coeff * _monomial_integral(exp) for exp, coeff in p.items())


def _poly_eval(
    p: dict[tuple[int, int, int, int], float], lam: Sequence[float]
) -> float:
    lam = tuple(float(v) for v in lam)
    total = 0.0
    for exp, coeff in p.items():
        term = coeff
        for i in range(4):
            if exp[i]:
                term *= lam[i] ** exp[i]
        total += term
    return total


_TET_EDGE_NODES = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))


def _tet_p2_basis() -> list[dict[tuple[int, int, int, int], float]]:
    """P2 Lagrange basis on the reference tet, MFEM-style node order.

    Nodes: 4 vertices, then the 6 edge midpoints in the order
    (0,1),(0,2),(0,3),(1,2),(1,3),(2,3).
    """
    basis: list[dict[tuple[int, int, int, int], float]] = []
    for i in range(4):
        vertex_sq = [0, 0, 0, 0]
        vertex_sq[i] = 2
        vertex_lin = [0, 0, 0, 0]
        vertex_lin[i] = 1
        basis.append({tuple(vertex_sq): 2.0, tuple(vertex_lin): -1.0})
    for i, j in _TET_EDGE_NODES:
        exp = [0, 0, 0, 0]
        exp[i] += 1
        exp[j] += 1
        basis.append({tuple(exp): 4.0})
    return basis


def tet_p2_element_matrices(
    coords: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Exact mass and (unscaled) stiffness matrices of a 10-node tet.

    ``coords`` is a (4, 3) array of vertex coordinates.  The returned
    stiffness is ∫∇φ·∇φ — the caller multiplies by c^2 when forming the
    velocity-potential stiffness (DiffusionIntegrator(c^2)).
    """
    coords = np.asarray(coords, dtype=np.float64)
    jac = np.column_stack(
        (coords[1] - coords[0], coords[2] - coords[0], coords[3] - coords[0])
    )
    det_j = float(np.linalg.det(jac))
    if det_j <= 0.0:
        raise ValueError('tetrahedron has non-positive Jacobian')
    # ∇_x φ = J^{-T} ∇_λ̃ φ ; ∇φ_i·∇φ_j = d_i^T (J^{-1} J^{-T}) d_j.
    gram = np.linalg.inv(jac) @ np.linalg.inv(jac).T

    basis = _tet_p2_basis()
    mass = np.zeros((10, 10))
    stiff = np.zeros((10, 10))
    grad_polys = []
    for phi in basis:
        # ∂φ/∂λ̃_k = ∂φ/∂λ_k - ∂φ/∂λ_0   (lambda_0 = 1 - sum lambda_k)
        grad_polys.append(
            [
                _poly_add(_poly_diff(phi, k), _poly_diff(phi, 0), scale_b=-1.0)
                for k in (1, 2, 3)
            ]
        )
    for i in range(10):
        for j in range(i, 10):
            m_val = _poly_integrate(_poly_mul(basis[i], basis[j]))
            k_val = 0.0
            for a in range(3):
                for b in range(3):
                    k_val += gram[a, b] * _poly_integrate(
                        _poly_mul(grad_polys[i][a], grad_polys[j][b])
                    )
            mass[i, j] = mass[j, i] = det_j * m_val
            stiff[i, j] = stiff[j, i] = det_j * k_val
    return mass, stiff


# ---------------------------------------------------------------------------
# Sloped-room audited base mesh (r130d-polyhedral-candidate-wave-v1/sloped).
# ---------------------------------------------------------------------------

SLOPED_VERTICES = np.asarray(
    [
        [0.0, 0.0, 0.0],
        [4.0, 0.0, 0.0],
        [4.0, 4.0, 0.0],
        [0.0, 4.0, 0.0],
        [0.0, 0.0, 4.0],
        [4.0, 0.0, 4.0],
        [4.0, 4.0, 3.0],
        [0.0, 4.0, 3.0],
    ]
)

SLOPED_BASE_TETS = (
    (0, 1, 2, 6),
    (0, 2, 3, 6),
    (0, 3, 7, 6),
    (0, 7, 4, 6),
    (0, 4, 5, 6),
    (0, 5, 1, 6),
)


def refine_tet_mesh(
    vertices: np.ndarray, tets: Sequence[Sequence[int]]
) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    """Uniform 8-way refinement.  Documented octahedron diagonal:

    the octahedron interior tets are split along the edge joining the
    midpoints of base edges (v0,v1) and (v2,v3).
    """
    vertices = np.asarray(vertices, dtype=np.float64)
    verts = vertices.tolist()
    midpoint_cache: dict[tuple[int, int], int] = {}

    def midpoint(a: int, b: int) -> int:
        key = (min(a, b), max(a, b))
        index = midpoint_cache.get(key)
        if index is None:
            index = len(verts)
            verts.append(
                [
                    0.5 * (vertices[a][0] + vertices[b][0]),
                    0.5 * (vertices[a][1] + vertices[b][1]),
                    0.5 * (vertices[a][2] + vertices[b][2]),
                ]
            )
            midpoint_cache[key] = index
        return index

    out_tets: list[tuple[int, int, int, int]] = []
    for a, b, c, d in tets:
        ab = midpoint(a, b)
        ac = midpoint(a, c)
        ad = midpoint(a, d)
        bc = midpoint(b, c)
        bd = midpoint(b, d)
        cd = midpoint(c, d)
        # Four corner tets.
        out_tets.append((a, ab, ac, ad))
        out_tets.append((ab, b, bc, bd))
        out_tets.append((ac, bc, c, cd))
        out_tets.append((ad, bd, cd, d))
        # Octahedron: split along the diagonal (ab, cd).
        out_tets.append((ab, ac, ad, cd))
        out_tets.append((ab, ac, bc, cd))
        out_tets.append((ab, ad, bd, cd))
        out_tets.append((ab, bc, bd, cd))

    fixed: list[tuple[int, int, int, int]] = []
    all_verts = np.asarray(verts)
    for tet in out_tets:
        coords = all_verts[list(tet)]
        jac = np.column_stack(
            (coords[1] - coords[0], coords[2] - coords[0], coords[3] - coords[0])
        )
        if np.linalg.det(jac) < 0.0:
            tet = (tet[0], tet[1], tet[3], tet[2])
        fixed.append(tuple(int(v) for v in tet))
    return np.asarray(verts), fixed


@dataclass(frozen=True)
class TetSystem:
    vertices: np.ndarray
    tets: tuple[tuple[int, int, int, int], ...]
    mass: np.ndarray
    stiffness: np.ndarray
    source: np.ndarray
    receiver: np.ndarray


def _tet_barycentric(coords: np.ndarray, point: np.ndarray) -> np.ndarray | None:
    jac = np.column_stack(
        (coords[1] - coords[0], coords[2] - coords[0], coords[3] - coords[0])
    )
    rhs = np.asarray(point, dtype=np.float64) - coords[0]
    try:
        sol = np.linalg.solve(jac, rhs)
    except np.linalg.LinAlgError:
        return None
    lam = np.concatenate(([1.0 - sol.sum()], sol))
    if np.all(lam >= -1e-10):
        lam = np.clip(lam, 0.0, 1.0)
        return lam / lam.sum()
    return None


def assemble_sloped_tet_system(
    refinements: int,
    source_xyz: Sequence[float],
    receiver_xyz: Sequence[float],
) -> TetSystem:
    """Assemble the audited 6-tet sloped room after ``refinements`` uniform
    refinements, with point-delta source/receiver functionals."""
    vertices = SLOPED_VERTICES.copy()
    tets: list[tuple[int, int, int, int]] = list(SLOPED_BASE_TETS)
    for _ in range(refinements):
        vertices, tets = refine_tet_mesh(vertices, tets)

    basis = _tet_p2_basis()

    # P2 global dofs: one per vertex plus one per unique edge.
    edge_set: dict[tuple[int, int], int] = {}
    for tet in tets:
        for i, j in _TET_EDGE_NODES:
            key = (min(tet[i], tet[j]), max(tet[i], tet[j]))
            if key not in edge_set:
                edge_set[key] = len(vertices) + len(edge_set)
    ndof = len(vertices) + len(edge_set)

    mass = np.zeros((ndof, ndof))
    stiff = np.zeros((ndof, ndof))
    source = np.zeros(ndof)
    receiver = np.zeros(ndof)
    src_pt = np.asarray(source_xyz, dtype=np.float64)
    rx_pt = np.asarray(receiver_xyz, dtype=np.float64)
    src_found = rx_found = False

    for tet in tets:
        local: list[int] = [int(tet[i]) for i in range(4)]
        for i, j in _TET_EDGE_NODES:
            local.append(edge_set[(min(tet[i], tet[j]), max(tet[i], tet[j]))])
        coords = vertices[list(tet)]
        me, ke = tet_p2_element_matrices(coords)
        for a in range(10):
            for b in range(10):
                mass[local[a], local[b]] += me[a, b]
                stiff[local[a], local[b]] += ke[a, b]
        # MFEM DeltaCoefficient evaluates each point functional in ONE
        # containing element; on shared faces the C0 basis gives identical
        # values, so first-match is deterministic and non-accumulating.
        for point, vec, found_flag in (
            (src_pt, source, 'src'),
            (rx_pt, receiver, 'rx'),
        ):
            if (found_flag == 'src' and src_found) or (
                found_flag == 'rx' and rx_found
            ):
                continue
            lam = _tet_barycentric(coords, point)
            if lam is None:
                continue
            for a in range(10):
                vec[local[a]] += _poly_eval(basis[a], lam)
            if found_flag == 'src':
                src_found = True
            else:
                rx_found = True
    if not (src_found and rx_found):
        raise ValueError('source or receiver lies outside the sloped domain')
    return TetSystem(
        vertices=vertices,
        tets=tuple(tuple(int(v) for v in tet) for tet in tets),
        mass=mass,
        stiffness=stiff,
        source=source,
        receiver=receiver,
    )


# ---------------------------------------------------------------------------
# Tensor-product hexahedra (order p, Gauss-Lobatto node layout on [-1,1]).
# ---------------------------------------------------------------------------

def gauss_lobatto_points(order: int) -> np.ndarray:
    """Gauss-Lobatto-Legendre points on [-1, 1], order p has p+1 points."""
    if order < 1:
        raise ValueError('order must be >= 1')
    if order == 1:
        return np.asarray([-1.0, 1.0])
    # Lobatto points: -1, 1, and roots of P'_p.
    from numpy.polynomial import legendre as L

    p_poly = L.Legendre.basis(order)
    dp = p_poly.deriv()
    roots = np.asarray(dp.roots())
    interior = np.asarray(sorted(np.real(roots[np.abs(roots.imag) < 1e-10])))
    return np.concatenate(([-1.0], interior, [1.0]))


def _lagrange_matrix_and_derivative(
    nodes: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (M1, S1) reference matrices for the 1D Lagrange basis on the
    given nodes, integrated exactly on [-1, 1]."""
    p1 = len(nodes)
    # Polynomial coefficients of each basis function in power basis.
    polys = []
    for i in range(p1):
        poly = np.polynomial.Polynomial([1.0])
        for j in range(p1):
            if i == j:
                continue
            poly = poly * np.polynomial.Polynomial([-nodes[j], 1.0]) / (
                nodes[i] - nodes[j]
            )
        polys.append(poly.coef)
    mass = np.zeros((p1, p1))
    stiff = np.zeros((p1, p1))

    def _int_monomial(k: int) -> float:
        return 0.0 if k % 2 else 2.0 / (k + 1)

    for i in range(p1):
        ci = polys[i]
        di = np.asarray([k * ci[k] for k in range(1, len(ci))])
        for j in range(p1):
            cj = polys[j]
            prod = np.convolve(ci, cj)
            mass[i, j] = sum(c * _int_monomial(k) for k, c in enumerate(prod))
            dj = np.asarray([k * cj[k] for k in range(1, len(cj))])
            dprod = np.convolve(di, dj)
            stiff[i, j] = sum(c * _int_monomial(k) for k, c in enumerate(dprod))
    return mass, stiff


@dataclass(frozen=True)
class HexCell:
    """Axis-aligned hexahedral cell with origin and full sizes."""

    x0: float
    y0: float
    z0: float
    hx: float
    hy: float
    hz: float


@dataclass(frozen=True)
class HexSystem:
    nodes: np.ndarray  # (ndof, 3)
    mass: np.ndarray
    stiffness: np.ndarray
    source: np.ndarray
    receiver: np.ndarray
    elements: int


def _lagrange_eval(nodes_1d: np.ndarray, xi: float) -> np.ndarray:
    p1 = len(nodes_1d)
    out = np.ones(p1)
    for i in range(p1):
        for j in range(p1):
            if i != j:
                out[i] *= (xi - nodes_1d[j]) / (nodes_1d[i] - nodes_1d[j])
    return out


def _lagrange_deriv_eval(nodes_1d: np.ndarray, xi: float) -> np.ndarray:
    """Derivative of each 1-D Lagrange basis function at ``xi``."""
    p1 = len(nodes_1d)
    out = np.zeros(p1)
    for i in range(p1):
        for j in range(p1):
            if i == j:
                continue
            term = 1.0 / (nodes_1d[i] - nodes_1d[j])
            for k in range(p1):
                if k != i and k != j:
                    term *= (xi - nodes_1d[k]) / (nodes_1d[i] - nodes_1d[k])
            out[i] += term
    return out


def assemble_hex_system(
    cells: Sequence[HexCell],
    order: int,
    source_xyz: Sequence[float],
    receiver_xyz: Sequence[float],
) -> HexSystem:
    """Assemble mass/stiffness and point functionals for an axis-aligned hex
    mesh whose elements share Gauss-Lobatto nodes on their boundaries."""
    pts = gauss_lobatto_points(order)
    m1, s1 = _lagrange_matrix_and_derivative(pts)
    n1 = order + 1
    nloc = n1 * n1 * n1

    node_index: dict[tuple[int, int, int], int] = {}
    nodes: list[tuple[float, float, float]] = []
    elem_dofs: list[list[int]] = []

    # Node positions are lattice-unique; round to 1e-9 to merge shared nodes
    # across cells (GL points are only shared on element faces where the
    # physical coordinate is identical in exact arithmetic after rounding).
    def node_key(x: float, y: float, z: float) -> tuple[int, int, int]:
        return (
            int(round(x * 1e9)),
            int(round(y * 1e9)),
            int(round(z * 1e9)),
        )

    for cell in cells:
        dofs: list[int] = []
        for k in range(n1):
            z = cell.z0 + 0.5 * (pts[k] + 1.0) * cell.hz
            for j in range(n1):
                y = cell.y0 + 0.5 * (pts[j] + 1.0) * cell.hy
                for i in range(n1):
                    x = cell.x0 + 0.5 * (pts[i] + 1.0) * cell.hx
                    key = node_key(x, y, z)
                    idx = node_index.get(key)
                    if idx is None:
                        idx = len(nodes)
                        node_index[key] = idx
                        nodes.append((x, y, z))
                    dofs.append(idx)
        elem_dofs.append(dofs)

    ndof = len(nodes)
    mass = np.zeros((ndof, ndof))
    stiff = np.zeros((ndof, ndof))
    source = np.zeros(ndof)
    receiver = np.zeros(ndof)
    src_pt = np.asarray(source_xyz, dtype=np.float64)
    rx_pt = np.asarray(receiver_xyz, dtype=np.float64)
    seen_map = {'_seen_src': False, '_seen_rx': False}

    # Local node layout: index = i + n1*j + n1*n1*k (i = x, fastest;
    # k = z, slowest).  np.kron(A, B) indexes row (a1*n2 + a2), so the
    # slowest axis must be the LEFT factor: kron(m1_z, kron(m1_y, m1_x)).
    kron = np.kron
    mm = kron(m1, kron(m1, m1))
    ms_k_x = kron(m1, kron(m1, s1))   # z outer -> y -> x = s1 on x
    m_sm_y = kron(m1, kron(s1, m1))   # s1 on y
    m_ms_z = kron(s1, kron(m1, m1))   # s1 on z

    for cell, dofs in zip(cells, elem_dofs):
        hx, hy, hz = cell.hx, cell.hy, cell.hz
        me = (hx * hy * hz / 8.0) * mm
        ke = (
            (hy * hz / (2.0 * hx)) * ms_k_x
            + (hx * hz / (2.0 * hy)) * m_sm_y
            + (hx * hy / (2.0 * hz)) * m_ms_z
        )
        dofs_arr = np.asarray(dofs)
        mass[np.ix_(dofs_arr, dofs_arr)] += me
        stiff[np.ix_(dofs_arr, dofs_arr)] += ke
        for point, vec, seen in ((src_pt, source, 'src'), (rx_pt, receiver, 'rx')):
            # First containing element only (see tet path for rationale).
            seen_key = f'_seen_{seen}'
            if seen_map[seen_key]:
                continue
            xis = (
                2.0 * (point[0] - cell.x0) / cell.hx - 1.0,
                2.0 * (point[1] - cell.y0) / cell.hy - 1.0,
                2.0 * (point[2] - cell.z0) / cell.hz - 1.0,
            )
            if any(xi < -1.0 - 1e-9 or xi > 1.0 + 1e-9 for xi in xis):
                continue
            ex = _lagrange_eval(pts, xis[0])
            ey = _lagrange_eval(pts, xis[1])
            ez = _lagrange_eval(pts, xis[2])
            loc = 0
            for k in range(n1):
                for j in range(n1):
                    for i in range(n1):
                        vec[dofs[loc]] += ez[k] * ey[j] * ex[i]
                        loc += 1
            seen_map[seen_key] = True
    if not (seen_map['_seen_src'] and seen_map['_seen_rx']):
        raise ValueError('source or receiver lies outside the hex domain')
    return HexSystem(
        nodes=np.asarray(nodes),
        mass=mass,
        stiffness=stiff,
        source=source,
        receiver=receiver,
        elements=len(cells),
    )


def rectangular_room_cells(refinements: int) -> list[HexCell]:
    """wave-rectangular-convergence-v1: 6 x 4 x 2.5 m rigid room."""
    nx = ny = nz = 2 ** refinements
    hx, hy, hz = 6.0 / nx, 4.0 / ny, 2.5 / nz
    cells = []
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                cells.append(HexCell(ix * hx, iy * hy, iz * hz, hx, hy, hz))
    return cells


def lroom_hex_cells(refinements: int) -> list[HexCell]:
    """wave-concave-l-room-v1: the audited five-hex L prism, uniformly
    refined (each axis split in two per refinement)."""
    base = ((0, 0), (1, 0), (2, 0), (0, 1), (2, 1))
    n = 2 ** refinements
    h = 2.0 / n
    hz = 2.5 / n
    cells = []
    for iz in range(n):
        for bx, by in base:
            for iy in range(n):
                for ix in range(n):
                    cells.append(
                        HexCell(
                            2.0 * bx + ix * h,
                            2.0 * by + iy * h,
                            iz * hz,
                            h,
                            h,
                            hz,
                        )
                    )
    return cells


# ---------------------------------------------------------------------------
# Modal impulse-response reconstruction + finite-record transfer.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModalReconstruction:
    eigenvalues: np.ndarray
    eigenvectors: np.ndarray
    modal_frequencies_hz: np.ndarray
    pressure_trace: np.ndarray
    transfer: np.ndarray
    sample_count: int
    dt: float
    mass_orthonormality_max_abs: float
    eigen_residual_relative_max: float


def modal_impulse_transfer(
    mass: np.ndarray,
    stiffness_c2: np.ndarray,
    source_functional: np.ndarray,
    receiver_functional: np.ndarray,
    *,
    density_kg_m3: float,
    sound_speed_m_s: float,
    dt: float,
    duration_s: float,
    frequencies_hz: Sequence[float],
    max_modes: int | None = None,
) -> ModalReconstruction:
    """Reproduce the audited modal reconstruction:

    phi_t(0+) = c^2 * dt * M^{-1} * b * q0   (q0 = 1 m^3/s)
    p(t) = rho * sum_j (r^T v_j) (v_j^T M phi_t0) cos(sqrt(lambda_j) t)
    transfer(f) = sum_n p[n] exp(+i 2 pi f n dt)   (P_T / Q_T, Q_T = dt)
    """
    from scipy.linalg import eigh

    eigvals, eigvecs = eigh(stiffness_c2, mass)
    if max_modes is not None and len(eigvals) > max_modes:
        eigvals = eigvals[:max_modes]
        eigvecs = eigvecs[:, :max_modes]
    if np.any(eigvals < -1e-8 * max(1.0, float(np.max(np.abs(eigvals))))):
        raise ValueError('generalized eigenproblem produced a negative eigenvalue')
    omega = np.sqrt(np.clip(eigvals, 0.0, None))

    c2 = sound_speed_m_s ** 2
    phi_t0 = np.linalg.solve(mass, c2 * dt * source_functional)
    modal_velocity = eigvecs.T @ (mass @ phi_t0)
    receiver_response = receiver_functional @ eigvecs
    amplitudes = density_kg_m3 * receiver_response * modal_velocity

    sample_count = int(round(duration_s / dt))
    times = np.arange(sample_count, dtype=np.float64) * dt
    # p[n] = sum_j amp_j cos(omega_j t_n)
    pressure = (amplitudes[None, :] * np.cos(np.outer(times, omega))).sum(axis=1)
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    kernel = np.exp(+2j * np.pi * np.outer(freqs, times))
    transfer = kernel @ pressure  # dt * sum p e / (dt * q0), q0 = 1

    ortho = eigvecs.T @ mass @ eigvecs
    ortho_err = float(np.max(np.abs(ortho - np.eye(ortho.shape[0]))))
    residual = stiffness_c2 @ eigvecs - mass @ eigvecs * eigvals[None, :]
    scale = np.abs(eigvals) * np.linalg.norm(mass @ eigvecs, axis=0)
    # Skip (near-)rigid modes: their Kv and lambda*Mv are both ~0, so the
    # relative residual is a meaningless 0/0 ratio.
    lam_max = float(np.max(eigvals)) if len(eigvals) else 1.0
    active = eigvals > 1e-10 * max(1.0, lam_max)
    resid_rel = float(
        np.max(
            np.linalg.norm(residual[:, active], axis=0)
            / np.maximum(scale[active], np.finfo(np.float64).tiny)
        )
    ) if np.any(active) else 0.0
    return ModalReconstruction(
        eigenvalues=eigvals,
        eigenvectors=eigvecs,
        modal_frequencies_hz=omega / (2.0 * np.pi),
        pressure_trace=pressure,
        transfer=transfer,
        sample_count=sample_count,
        dt=dt,
        mass_orthonormality_max_abs=ortho_err,
        eigen_residual_relative_max=resid_rel,
    )
