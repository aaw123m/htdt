"""Experimental conservative positive Neumann FV interior-dispersion correction.

This is a genuine stiffness operator, not a modal filter or change of the
physical source/receiver. K4 = K + h²/(12*c²) K M^-1 K is symmetric PSD,
preserves rigid-wall constant mode, and improves the uniform-grid leading
second-order phase dispersion term. Cutcell-boundary global fourth-order
accuracy is NOT asserted. It is not a validated upstream PFFDTD replacement.
"""
from __future__ import annotations

from dataclasses import replace
import numpy as np
from scipy import sparse

from .r130d_native_grid_exact_roof_fv import NativeExactRoofCutcell


def corrected_neumann_stiffness(
    mass: sparse.spmatrix,
    stiffness: sparse.spmatrix,
    *, h_m: float,
    sound_speed_m_s: float = 343.2,
) -> sparse.csr_matrix:
    """Build strictly fixed-coefficient symmetric correction in physical units.

    Quadratic form: x.T @ K4 @ x =
       x.T @ K @ x + h²/(12c²) * || M^-1/2 K x ||².
    No eigenmode discarded, no change to true cutcell volumes.
    """
    M = mass.tocsr().astype(np.float64)
    K = stiffness.tocsr().astype(np.float64)
    n = K.shape[0]
    if (n<3 or K.shape!=(n,n) or M.shape!=(n,n)
        or h_m<=0 or sound_speed_m_s<=0
        or not np.isfinite(h_m) or not np.isfinite(sound_speed_m_s)):
        raise ValueError("invalid conservative Neumann FV correction inputs")
    if (M-M.T).nnz or (K-K.T).nnz:
        raise ValueError("non-symmetric mass or stiffness cannot be corrected")
    masses = M.diagonal()
    if (np.any(masses<=0) or not np.isfinite(masses).all()
        or (M-sparse.diags(masses,format="csr")).nnz):
        raise ValueError("correction needs unchanged strictly positive diagonal FV masses")
    if not np.isfinite(K.data).all():
        raise ValueError("nonfinite original stiffness")
    diag = K.diagonal()
    if (np.min(diag)<=0 or np.max(np.abs(np.asarray(K@np.ones(n))))>
        1e-8*max(float(np.max(diag)),1.0)):
        raise ValueError("original Neumann constant nullspace or positivity corrupted")
    coefficient=h_m*h_m/(12*sound_speed_m_s*sound_speed_m_s)
    result=(K+coefficient*(K @ (sparse.diags(1/masses,format="csr") @ K))).tocsr()
    result.sum_duplicates()
    # Rounded sparse products maintain symmetry up to floating point,
    # not necessarily exact bitwise equality for different multiplication orders.
    skew=(result-result.T).tocoo()
    asym=max((abs(skew.data).max() if skew.nnz else 0.0),0.0)
    scale=max(float(np.abs(result.diagonal()).max()),1.0)
    if asym>2e-12*scale:
        raise ValueError("corrected stiffness lost Neumann symmetric structure")
    if asym:
        result=((result+result.T)*.5).tocsr()
    one=np.ones(n)
    # Limit reflects floating cancellation of the large-dynamic-range sliver masses.
    null_res=np.linalg.norm(result@one,np.inf)/scale
    if null_res>1e-8:
        raise ValueError(f"corrected exact-roof constant Neumann mode lost: {null_res}")
    return result


def dispersion_corrected_exact_roof_system(
    system: NativeExactRoofCutcell,
    *, sound_speed_m_s: float=343.2,
) -> NativeExactRoofCutcell:
    """Return a new experimental solver system; never mutate the baseline."""
    return replace(system,stiffness_matrix=corrected_neumann_stiffness(
        system.mass_matrix,system.stiffness_matrix,
        h_m=system.native_grid_spacing_m,sound_speed_m_s=sound_speed_m_s))


def corrected_eigenvalues(lambda_native: np.ndarray, *, h_m: float,
                          sound_speed_m_s: float=343.2) -> np.ndarray:
    """Full-mode exact generalized eigenvalues of K4 in SAME M eigenbasis."""
    lam=np.asarray(lambda_native,dtype=np.float64)
    if (not np.isfinite(lam).all() or np.min(lam)<-1e-7
        or h_m<=0 or sound_speed_m_s<=0):
        raise ValueError("invalid all-mode generalized Neumann eigenvalues")
    lam=np.maximum(lam,0.0)
    return lam+(h_m*h_m/(12*sound_speed_m_s*sound_speed_m_s))*lam**2
