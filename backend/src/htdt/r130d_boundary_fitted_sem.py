"""Experimental conservative GLL spectral elements on the true R130D roof.

The conforming map z=(4-y/4)*eta has no cut cells or outside basis supports.
Original physical eight-node functionals are evaluated on this NEW basis;
this changes spatial discretization and does not repair the original graph.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
from numpy.polynomial.legendre import Legendre
from scipy import linalg, sparse


@dataclass(frozen=True)
class SpectralAxis:
    length: float
    elements: int
    degree: int
    reference_nodes: np.ndarray
    quadrature_weights: np.ndarray
    derivative: np.ndarray
    nodes: np.ndarray
    mass: np.ndarray
    stiffness: sparse.csr_matrix


def gll_rule(degree: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not isinstance(degree, int) or degree < 2 or degree > 12:
        raise ValueError("GLL degree must be an integer in [2,12]")
    poly = Legendre.basis(degree)
    nodes = np.r_[-1., poly.deriv().roots(), 1.]
    weights = 2. / (degree * (degree + 1) * poly(nodes)**2)
    bary = np.array([1. / np.prod(nodes[i] - np.delete(nodes, i))
                     for i in range(degree + 1)])
    D = np.zeros((degree + 1, degree + 1))
    for i in range(degree + 1):
        for j in range(degree + 1):
            if i != j:
                D[i, j] = bary[j] / (bary[i] * (nodes[i] - nodes[j]))
        D[i, i] = -D[i].sum()
    return nodes, weights, D


def build_spectral_axis(length: float, elements: int, degree: int,
                        *, c_m_s: float = 343.2) -> SpectralAxis:
    if not np.isfinite(length) or length <= 0 or elements < 1:
        raise ValueError("invalid physical spectral axis")
    g, w, D = gll_rule(degree)
    dx = length / elements
    n = elements * degree + 1
    nodes = np.empty(n)
    mass = np.zeros(n)
    rows, cols, data = [], [], []
    local = c_m_s**2 * 2. / dx * (D.T @ (w[:, None] * D))
    # Remove only matrix multiplication roundoff asymmetry, not physics terms.
    local = (local + local.T) / 2.
    for e in range(elements):
        ids = e * degree + np.arange(degree + 1)
        nodes[ids] = dx * (e + (g + 1.) / 2.)
        mass[ids] += dx / 2. * w
        rows.extend(np.repeat(ids, degree + 1))
        cols.extend(np.tile(ids, degree + 1))
        data.extend(local.ravel())
    K = sparse.coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()
    return SpectralAxis(length, elements, degree, g, w, D, nodes, mass, K)


@dataclass(frozen=True)
class BoundaryFittedSEM:
    x: SpectralAxis
    y: SpectralAxis
    eta: SpectralAxis
    yz_positions: np.ndarray
    yz_mass: np.ndarray
    yz_stiffness: sparse.csr_matrix
    native_h_m: float


def build_boundary_fitted_sem(native_h_m: float, *, degree: int = 4,
                              c_m_s: float = 343.2) -> BoundaryFittedSEM:
    if not np.isfinite(native_h_m) or not 0 < native_h_m <= .5:
        raise ValueError("invalid native h")
    ne = int(math.ceil(4. / (degree * native_h_m)))
    x = build_spectral_axis(4., ne, degree, c_m_s=c_m_s)
    y = build_spectral_axis(4., ne, degree, c_m_s=c_m_s)
    eta = build_spectral_axis(1., ne, degree, c_m_s=c_m_s)
    m = degree + 1
    n = len(y.nodes) * len(eta.nodes)
    mass = np.zeros(n)
    rows, cols, data = [], [], []
    Dy = np.kron(y.derivative, np.eye(m))
    De = np.kron(np.eye(m), eta.derivative)
    W = np.outer(y.quadrature_weights, eta.quadrature_weights).ravel()
    ay = 2. / ne
    ae = .5 / ne
    for ey in range(ne):
        yi = ey * degree + np.arange(m)
        for ee in range(ne):
            ei = ee * degree + np.arange(m)
            ids = (yi[:, None] * len(eta.nodes) + ei[None, :]).ravel()
            yy = np.repeat(y.nodes[yi], m)
            et = np.tile(eta.nodes[ei], m)
            H = 4. - yy / 4.
            jac = ay * ae * H
            gy = Dy / ay + (.25 * et / (H * ae))[:, None] * De
            gz = (1. / (H * ae))[:, None] * De
            mw = W * jac
            ke = c_m_s**2 * (gy.T @ (mw[:, None] * gy)
                              + gz.T @ (mw[:, None] * gz))
            ke = (ke + ke.T) / 2.
            mass[ids] += mw
            rows.extend(np.repeat(ids, m*m))
            cols.extend(np.tile(ids, m*m))
            data.extend(ke.ravel())
    K = sparse.coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()
    yy = np.repeat(y.nodes, len(eta.nodes))
    zz = (4. - yy / 4.) * np.tile(eta.nodes, len(y.nodes))
    if min(mass) <= 0 or abs(mass.sum() - 14.) > 2e-10:
        raise ValueError("physical conforming spectral mass invalid")
    return BoundaryFittedSEM(x, y, eta, np.column_stack((yy, zz)),
                             mass, K, float(native_h_m))


def axis_functional(axis: SpectralAxis, position: float) -> np.ndarray:
    if not np.isfinite(position) or position < 0 or position > axis.length:
        raise ValueError("point outside physical spectral axis")
    t = position / axis.length * axis.elements
    e = min(int(np.floor(t)), axis.elements - 1)
    xi = 2. * (t - e) - 1.
    g = axis.reference_nodes
    values = np.array([np.prod((xi - np.delete(g, i)) /
                               (g[i] - np.delete(g, i)))
                       for i in range(axis.degree + 1)])
    result = np.zeros(len(axis.nodes))
    result[e * axis.degree + np.arange(axis.degree + 1)] = values
    return result


def original_eight_functional(fem: BoundaryFittedSEM, xyz: np.ndarray,
                              weights: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    xyz, weights = np.asarray(xyz, float), np.asarray(weights, float)
    if xyz.shape != (8, 3) or weights.shape != (8,) or not np.isfinite(xyz).all():
        raise ValueError("exact original eight-node point data required")
    if not np.isfinite(weights).all() or abs(weights.sum() - 1.) > 1e-12:
        raise ValueError("original point weights invalid")
    tensor = np.zeros((len(fem.x.nodes), len(fem.yz_mass)))
    for (x, y, z), w in zip(xyz, weights):
        ex = axis_functional(fem.x, x)
        ey = axis_functional(fem.y, y)
        ee = axis_functional(fem.eta, z / (4. - y / 4.))
        tensor += w * np.outer(ex, np.outer(ey, ee).ravel())
    sx, sy = tensor.sum(axis=1), tensor.sum(axis=0)
    factor_error = float(np.max(abs(tensor - np.outer(sx, sy))))
    before = weights @ xyz
    after = np.r_[sx @ fem.x.nodes, sy @ fem.yz_positions]
    moment_error = float(np.max(abs(after - before)))
    if factor_error > 1e-11 or moment_error > 1e-10:
        raise ValueError("original physical tensor/centroid not preserved")
    return sx, sy, {"original_node_count": 8, "original_centroid_m": before.tolist(),
                    "sem_centroid_m": after.tolist(), "moment_error_m": moment_error,
                    "tensor_factorization_error": factor_error,
                    "new_spatial_basis_not_original_pffdtd": True}


def all_mass_normalized_modes(mass: np.ndarray, K: sparse.csr_matrix):
    mass = np.asarray(mass, float)
    if mass.shape != (K.shape[0],) or min(mass) <= 0:
        raise ValueError("positive complete nodal masses required")
    inverse = 1. / np.sqrt(mass)
    B = (sparse.diags(inverse) @ K @ sparse.diags(inverse)).toarray()
    values, U = linalg.eigh(B, driver="evd", check_finite=True)
    if values[0] < -1e-6 or values[1] <= 0:
        raise ValueError("nonphysical complete Neumann spectrum")
    original_zero = float(values[0])
    values[0] = 0.
    U[:, 0] = np.sqrt(mass / mass.sum())
    V = inverse[:, None] * U
    residual = K @ V - mass[:, None] * V * values[None, :]
    scale = np.maximum(1., np.linalg.norm(mass[:, None] * V * values[None, :], axis=0))
    worst = float(np.max(np.linalg.norm(residual, axis=0) / scale))
    gram = float(np.max(abs(U.T @ U - np.eye(len(mass)))))
    if worst > 3e-7 or gram > 5e-8:
        raise ValueError(f"complete eigenbasis residual={worst}, gram={gram}")
    return values, V, {"retained_modes": len(values), "all_modes": True,
                       "max_eigenpair_residual": worst, "max_mass_gram_error": gram,
                       "raw_zero_eigenvalue": original_zero}
