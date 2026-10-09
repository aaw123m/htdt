"""Positive physical row-sum Q1 mass / true cut-roof weak Neumann stiffness.

This changes ONLY the mass discretization family relative to physical cut-Q1
Galerkin. It retains exact original Cartesian Q1 positive support nodes,
original 8node source+receiver, native q0 impulse, and entire physical
Neumann weak stiffness construction. The full 3D Kronecker K necessarily
uses lumped cross-sectional tensor masses: no claim full K unchanged.
No mass floors or cutcells/eigenvectors removed.

This is an experimental solver, NEVER upstream original PFFDTD qualified.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse

from .r130d_causal_prefirst_weak import compact_odd_witness
from .r130d_causal_first_roof_echo import (
    ROOF_CENTER_S, ROOF_RADIUS_S, ROOF_WIDTHS_S)


@dataclass(frozen=True)
class Q1ExactRoofPositiveRowLump:
    Mx: sparse.csr_matrix
    Myz: sparse.csr_matrix
    Kx: sparse.csr_matrix
    Kyz: sparse.csr_matrix
    physical_x_rowsum_m: np.ndarray
    physical_yz_rowsum_m2: np.ndarray
    total_original_physical_3d_volume_m3: float

    @property
    def all_modes(self)->int:
        return self.Mx.shape[0]*self.Myz.shape[0]


def physical_positive_row_sum_lump(
    original_x_consistent_mass:sparse.csr_matrix,
    original_yz_consistent_cut_Q1_mass:sparse.csr_matrix,
    original_x_stiffness:sparse.csr_matrix,
    original_yz_exact_cut_Q1_stiffness:sparse.csr_matrix,
)->Q1ExactRoofPositiveRowLump:
    """Replace both physical consistent mass tensors by exact POSITIVE row sums.

    Zero-mass ghost nodes were already omitted ONLY if they have absolutely
    zero support over the original physically cut room. All positive slivers
    remain and no numerical floor/clamp or graph regularization is allowed.
    """
    mx=original_x_consistent_mass.tocsr()
    my=original_yz_consistent_cut_Q1_mass.tocsr()
    kx=original_x_stiffness.tocsr()
    ky=original_yz_exact_cut_Q1_stiffness.tocsr()
    if (mx.shape!=kx.shape or my.shape!=ky.shape
        or mx.shape[0]<4 or my.shape[0]<50
        or max(abs((mx-mx.T).data),default=0.)>1e-11
        or max(abs((my-my.T).data),default=0.)>1e-11):
        raise ValueError("invalid original true physical Q1 exact mass/Neumann stiffness")
    wx=np.asarray(mx.sum(axis=1)).ravel()
    wy=np.asarray(my.sum(axis=1)).ravel()
    if (np.min(wx)<=0 or np.min(wy)<=0
        or not np.isfinite(wx).all() or not np.isfinite(wy).all()
        or abs(float(wx.sum())-4)>1e-9 or abs(float(wy.sum())-14)>1e-8):
        raise ValueError("positive physical row-sum diagonal lump would delete a true native Q1 node")
    # No alteration to either exact original Q1 boundary stiffness matrix.
    if (max(abs(kx@np.ones(len(wx))))>1e-8 or
        max(abs(ky@np.ones(len(wy))))>1e-8):
        raise ValueError("true Neumann physical weak flux doesn't annihilate constants")
    Mx=sparse.diags(wx,format="csr")
    My=sparse.diags(wy,format="csr")
    physical_volume=float(wx.sum()*wy.sum())
    if abs(physical_volume-56)>2e-8:
        raise ValueError("original roof exact 56m3 physical mass was not preserved")
    return Q1ExactRoofPositiveRowLump(
        Mx,My,kx,ky,wx,wy,physical_volume)


def true_full_3d_lumped_roof_MK(
    l:Q1ExactRoofPositiveRowLump
)->tuple[sparse.csr_matrix,sparse.csr_matrix]:
    M=sparse.kron(l.Mx,l.Myz,format="csr")
    K=(sparse.kron(l.Kx,l.Myz,format="csr")+
       sparse.kron(l.Mx,l.Kyz,format="csr")).tocsr()
    if (M.shape[0]!=l.all_modes
        or np.min(M.diagonal())<=0
        or M.nnz!=l.all_modes
        or abs(float(M.sum())-56)>2e-8
        or float(max(abs(K@np.ones(l.all_modes))))/
           max(1,float(max(abs(K.diagonal()))))>1e-10):
        raise ValueError("true physical Neumann lump mass Q1 conservation failed")
    return M,K


def allmode_native_newmark_roof_causal_pressure_weak(
    eigenvalues_3d:np.ndarray,
    native_q0_amplitude_per_3d_mode:np.ndarray,
    dt_s:float, Nt:int, *,
    density_kg_m3:float=1.2,
    center_s:float=ROOF_CENTER_S,
    radius_s:float=ROOF_RADIUS_S,
    widths_s:tuple[float,...]=ROOF_WIDTHS_S,
)->list[dict]:
    """Exact native implicit-Newmark all-mode pressure at fixed roof witness.

    For original causal native q0: phi[n]=amp*sin(n theta)/sin(theta),
    theta=2 atan(.5*dt sqrt(lam)), and for 1<=n<=Nt-2
       p[n] = rho * amp/dt * cos(n theta).
    Causal compact witnesses here are disjoint from the n=0 and final
    derivative endpoints; no time masking changes the original 250ms P/Q.
    Every generalized mode, including above Nyquist, contributes in full.
    """
    vals=np.asarray(eigenvalues_3d,dtype=float).ravel()
    amps=np.asarray(native_q0_amplitude_per_3d_mode,dtype=float).ravel()
    if (vals.shape!=amps.shape or not len(vals) or
        not np.isfinite(vals).all() or not np.isfinite(amps).all()
        or np.min(vals)<-5e-7 or dt_s<=0 or Nt<30
        or abs(center_s-ROOF_CENTER_S)>1e-14
        or abs(radius_s-ROOF_RADIUS_S)>1e-14
        or tuple(widths_s)!=ROOF_WIDTHS_S):
        raise ValueError("full original native q0 roof causal witness changed")
    times=np.arange(Nt,dtype=float)*dt_s
    take=np.flatnonzero((times>=center_s-radius_s)&
                        (times<=center_s+radius_s))
    if len(take)<8 or take[0]<1 or take[-1]>=Nt-1:
        raise ValueError("original native q0 first-roof witnesses not inside native sample interior")
    theta=2*np.arctan(.5*dt_s*np.sqrt(np.maximum(vals,0)))
    # Full modal eigen spectrum, NO high-mode truncation. Chunking is only
    # an array-memory optimization, not spectral selection.
    matrix=np.empty((len(take),len(vals)),dtype=float)
    for i,n in enumerate(take):
        matrix[i,:]=np.cos(theta*n)
    p_window=(density_kg_m3/dt_s)*(matrix@amps)
    if not np.isfinite(p_window).all():
        raise ValueError("exact all-mode native Newmark pressure inside roof window invalid")
    out=[]
    for width in widths_s:
        witness=compact_odd_witness(times[take],width,
            center_s=center_s,radius_s=radius_s)
        out.append({"width_s":float(width),
            "original_native_q0_same_record_allmode_weak_p_over_Q":
                float(np.dot(p_window,witness)),
            "all_original_native_3D_modes_retained":len(vals),
            "original_native_pressure_window_sample_count":int(len(take)),
            "analytical_causal_Newmark_modal_pressure_not_fitted":True})
    return out
