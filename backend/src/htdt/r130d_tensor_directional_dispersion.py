"""Conservative exact-roof x/yz stiffness dispersion components.

The x-extruded physical cut-cell geometry factorizes exactly:
M = Mx ⊗ Myz and K = Kx ⊗ Myz + Mx ⊗ Kyz.
The fixed uniform-grid h²/(12c²) correction separates into:
  K M^-1 K = (Kx Mx^-1 Kx) ⊗ Myz
            + Mx ⊗ (Kyz Myz^-1 Kyz)
            + 2 Kx ⊗ Kyz.
One-dimensional/roof-cross-section corrected arms are *new candidate
operators*, preserving physical mass and rigid-wall constant mode.
No mode filtering, delta alteration or global fourth-order claim.
"""
from __future__ import annotations

import numpy as np
from scipy import sparse

from .r130d_native_exact_roof_separable import NativeRoofSeparable


ARMS=("baseline","x_only","yz_only","x_plus_yz","full_kmk")


def directional_eigenvalues(
    lambda_x: np.ndarray, lambda_yz: np.ndarray, *,
    h_m: float, c_m_s: float=343.2,
) -> dict[str,np.ndarray]:
    """All-mode eigenvalue arrays, with no cut or modified source coefficients."""
    x=np.asarray(lambda_x,dtype=float)
    y=np.asarray(lambda_yz,dtype=float)
    if (x.ndim!=1 or y.ndim!=1 or min(len(x),len(y))<2
        or np.min(x)<-1e-7 or np.min(y)<-1e-7
        or not np.isfinite(x).all() or not np.isfinite(y).all()
        or h_m<=0 or c_m_s<=0):
        raise ValueError("invalid original Neumann tensor eigenbasis")
    x=np.maximum(x,0.)[:,None]
    y=np.maximum(y,0.)[None,:]
    alpha=h_m*h_m/(12*c_m_s*c_m_s)
    base=x+y
    return {
        "baseline":base,
        "x_only":base+alpha*x*x,
        "yz_only":base+alpha*y*y,
        "x_plus_yz":base+alpha*(x*x+y*y),
        "full_kmk":base+alpha*(x+y)**2,
    }


def directional_stiffness(
    sep:NativeRoofSeparable, *, arm:str, c_m_s:float=343.2,
)->sparse.csr_matrix:
    """Assemble actual 3D positive conservative corrected K operator."""
    if arm not in ARMS or c_m_s<=0:
        raise ValueError("unregistered directional correction arm")
    mx=sep.Mx
    my=sep.Myz
    kx=sep.Kx
    ky=sep.Kyz
    kbase=(sparse.kron(kx,my,format="csr")+
           sparse.kron(mx,ky,format="csr")).tocsr()
    if arm=="baseline":
        result=kbase
    else:
        alpha=sep.grid_h_m**2/(12*c_m_s*c_m_s)
        kcorr=sparse.csr_matrix(kbase.shape)
        if arm in ("x_only","x_plus_yz","full_kmk"):
            # All original x Voronoi control-volume masses unchanged.
            cx=kx @ (sparse.diags(1./sep.x_mass,format="csr") @ kx)
            kcorr=kcorr+sparse.kron(cx,my,format="csr")
        if arm in ("yz_only","x_plus_yz","full_kmk"):
            # Sloping roof tiny physical cutcell masses are kept exactly.
            cy=ky @ (sparse.diags(1./sep.yz_mass,format="csr") @ ky)
            kcorr=kcorr+sparse.kron(mx,cy,format="csr")
        if arm=="full_kmk":
            kcorr=kcorr+2*sparse.kron(kx,ky,format="csr")
        result=(kbase+alpha*kcorr).tocsr()
    result.sum_duplicates()
    if not np.all(np.isfinite(result.data)):
        raise ValueError("nonfinite corrected physical spatial stiffness")
    skew=(result-result.T).tocoo()
    scale=max(float(np.max(np.abs(result.diagonal()))),1.)
    relative_skew=(float(np.max(np.abs(skew.data))) if skew.nnz else 0.)/scale
    if relative_skew>2e-12:
        raise ValueError("corrected Neumann stiffness not symmetric")
    if skew.nnz:
        result=((result+result.T)*.5).tocsr()
    null=float(np.max(np.abs(result@np.ones(result.shape[0]))))/scale
    if null>1e-8:
        raise ValueError(f"directional corrected Neumann constant nullspace drift {null}")
    return result
