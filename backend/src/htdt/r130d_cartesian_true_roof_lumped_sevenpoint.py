"""Physically weighted Cartesian 7-point interior with EXACT inclined roof.

Experimental: positive row-sum Mx and exact cut-Q1 Myz rowsums, combined with
the already preregistered Cartesian 5-point+true physical roof Neumann stiffness.
No eigenmode, positive-support node, cut sliver or original HDF5 point is removed.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from scipy import sparse
from .r130d_native_cut_Q1_positive_lumped import (
    physical_positive_row_sum_lump,true_full_3d_lumped_roof_MK,
    Q1ExactRoofPositiveRowLump)
from .r130d_cartesian_true_roof_hybrid_flux import CartesianTrueRoofHybridFlux

@dataclass(frozen=True)
class SevenPointTrueRoof:
    physical: Q1ExactRoofPositiveRowLump
    mass: sparse.csr_matrix
    stiffness: sparse.csr_matrix
    representative_interior_mass_m3: float
    seven_neighbor_stencil_max_relative: float
    total_true_room_m3: float


def build_cartesian_sevenpoint_true_roof(
    axes:list[np.ndarray],x_mass:sparse.csr_matrix,
    x_stiffness:sparse.csr_matrix,hybrid:CartesianTrueRoofHybridFlux,
    *,c_m_s:float=343.2,
)->SevenPointTrueRoof:
    """Verify native 7point interior M^-1K INCLUDING physical dimensions.

    K axis-offdiagonal = -c² h, K diagonal=6c² h; M diagonal=h³
    for representative full-wet interior. No tuned mass coefficient.
    """
    if len(axes)!=3 or x_mass.shape[0]!=len(axes[0]):
        raise ValueError("invalid true native original x/y/z physical axes")
    l=physical_positive_row_sum_lump(
        x_mass,hybrid.mass,x_stiffness,hybrid.stiffness)
    M,K=true_full_3d_lumped_roof_MK(l)
    h=float(axes[1][1]-axes[1][0])
    if (h<=0 or not np.allclose(np.diff(axes[1]),h,rtol=1e-8,atol=2e-10)
        or not np.allclose(np.diff(axes[2]),h,rtol=1e-8,atol=2e-10)):
        raise ValueError("not the unchanged original isotropic Cartesian native yz grid")
    # Original physical x P1 is bounded at x=0 and x=4; end segments may
    # be clipped, unlike the fully interior original native x pitch.
    x=int(np.argmin(abs(np.asarray(axes[0])-2.)))
    if not np.allclose(np.diff(axes[0][x-2:x+3]),h,rtol=1e-8,atol=2e-10):
        raise ValueError("original physical x grid interior pitch changed")
    y=int(np.argmin(abs(np.asarray(axes[1])-2.)))
    z=int(np.argmin(abs(np.asarray(axes[2])-2.)))
    a,b=float(axes[1][y]),float(axes[2][z])
    if (min(x,len(axes[0])-x-1,y,len(axes[1])-y-1,
            z,len(axes[2])-z-1)<3
        or min(a,4-a,b,4-.25*a-b)<3*h):
        raise ValueError("selected Cartesian stencil is not in strictly full interior")
    q=hybrid.cut_q1
    nz=len(axes[2])
    yi=q.original_native_yz_to_active.get(y*nz+z)
    if yi is None: raise ValueError("full interior original native Q1 node omitted")
    loc=x*q.native_yz_modes+yi
    row=K.getrow(loc)
    significant=int(np.count_nonzero(abs(row.data)>1e-9*c_m_s**2*h))
    if significant!=7:
        raise ValueError(f"not an exact 7point physical Cartesian full interior: {significant}")
    if np.count_nonzero(abs(row.data)<=1e-9*c_m_s**2*h):
        # Tiny cancellation artifacts from Q1 checkerboard correction are
        # allowed ONLY numerically; full stencil residual is checked below.
        pass
    targets=[(x,y,z,6.),(x-1,y,z,-1.),(x+1,y,z,-1.),
             (x,y-1,z,-1.),(x,y+1,z,-1.),
             (x,y,z-1,-1.),(x,y,z+1,-1.)]
    actual=dict(zip(row.indices,row.data))
    errs=[abs(float(M[loc,loc])-h**3)/h**3]
    for ix,iy,iz,unit in targets:
        key=ix*q.native_yz_modes+q.original_native_yz_to_active[iy*nz+iz]
        errs.append(abs(actual[key]-unit*c_m_s**2*h)/
                    max(abs(unit*c_m_s**2*h),1.))
    res=max(errs)
    if res>1e-8:
        raise ValueError(f"physical 7-point original native stencil mismatch {res}")
    if abs(float(M.sum())-56.)>2e-8:
        raise ValueError("true physical roof room volume changed")
    return SevenPointTrueRoof(l,M,K,float(M[loc,loc]),res,float(M.sum()))
