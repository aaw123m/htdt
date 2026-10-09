"""Prospectively fixed physical cut-roof CFL eigenvector and Rayleigh audit.

This is an *analysis* of the unmodified full 3D row-lumped sevenpoint
operator and complete M-generalized eigenbasis, NOT a filtered solver.
"""
from __future__ import annotations
import numpy as np
from scipy import sparse

CUT_THRESHOLDS=(.01,.05,.10,.50)
ROOF_MULTIPLIERS=(1.,2.)
TOP_EIGENCOUNT=3
TOP_NODECOUNT=5


def fixed_physical_CFL_localization(
    Mx:sparse.spmatrix,Kx:sparse.spmatrix,
    Myz:sparse.spmatrix,Kyz:sparse.spmatrix,
    x_lambda:np.ndarray,yz_lambda:np.ndarray,yz_eigenvectors:np.ndarray,
    yz_coordinates:np.ndarray,h:float,dt:float,
)->dict:
    """Localize top 3 generalized yz modes and certificate on exact basis nodes.

    Positive physical mass is diagonal. Mass fractions sum to one because
    v.T M v=1; this is NOT a nonnegative decomposition of the signed q0 bins.
    """
    wx=np.asarray(Mx.diagonal(),float)
    wy=np.asarray(Myz.diagonal(),float)
    kx=np.asarray(Kx.diagonal(),float)
    ky=np.asarray(Kyz.diagonal(),float)
    lx=np.asarray(x_lambda,float)
    ly=np.asarray(yz_lambda,float)
    vec=np.asarray(yz_eigenvectors,float)
    xy=np.asarray(yz_coordinates,float)
    if (wx.ndim!=1 or wy.ndim!=1 or np.min(wx)<=0 or np.min(wy)<=0
        or wx.size!=len(kx) or wy.size!=len(ky)
        or len(lx)!=len(wx) or len(ly)!=len(wy)
        or vec.shape!=(len(wy),len(wy)) or xy.shape!=(len(wy),2)
        or h<=0 or dt<=0 or len(wy)<10 or len(wx)<4
        or np.min(ly)<-5e-7 or np.min(lx)<-5e-7
        or not np.isfinite(vec).all() or not np.isfinite(wy).all()):
        raise ValueError("invalid true physical full roof CFL eigenspace")
    if max(abs(float(wx.sum())-4.),abs(float(wy.sum())-14.))>1e-8:
        raise ValueError("original physical roof area or x length changed")
    if not (np.all(np.diff(lx)>=-1e-7) and np.all(np.diff(ly)>=-1e-7)):
        raise ValueError("complete generalized eigenspectra not sorted")
    yz_ratio=wy/h**2
    roof_d=np.abs(4.-.25*xy[:,0]-xy[:,1])/h
    qx=kx/wx
    qy=ky/wy
    ix=int(np.argmax(qx));iy=int(np.argmax(qy))
    max_lam=float(lx[-1]+ly[-1])
    global_node_lower=float(qx[ix]+qy[iy])
    # Rayleigh-Ritz min/max principle. A coordinate basis vector is a valid
    # trial state, so K_ii/M_ii cannot exceed global lambda_max.
    if (min(qx.min(),qy.min())<-1e-6
        or global_node_lower>max_lam*(1+5e-8)+1e-6):
        raise ValueError("diagonal basis Rayleigh bound contradicts full eigenspectrum")
    def share(mask,mass):
        total=float(mass.sum())
        assert total>0
        return {"nodes":int(mask.sum()),"baseline_physical_support_mass_fraction":float(mass[mask].sum()/total)}
    def mode_share(weights,mask):
        return float(weights[mask].sum()/weights.sum())
    masses={}
    for factor in ROOF_MULTIPLIERS:
        m=roof_d<=factor
        masses[f"abs_true_roof_distance_le_{int(factor)}h"]=share(m,wy)
    for th in CUT_THRESHOLDS:
        m=yz_ratio<th
        masses[f"positive_node_area_lt_{th:g}_h2"]=share(m,wy)
    top_modes=[]
    for k in range(1,TOP_EIGENCOUNT+1):
        loc=len(ly)-k
        mweight=wy*vec[:,loc]**2
        norm=float(mweight.sum())
        if abs(norm-1.)>5e-7:raise ValueError("original generalized eigenvector M norm is not one")
        shares={}
        for name,info in masses.items():
            if name.startswith("abs_true_roof_distance_le_"):
                f=float(name.rsplit("_",1)[-1][:-1])
                mask=roof_d<=f
            else:
                th=float(name.split("_")[4])  # positive_node_area_lt_0.01_h2
                mask=yz_ratio<th
            b=info["baseline_physical_support_mass_fraction"]
            a=mode_share(mweight,mask)
            shares[name]={
                "eigenvector_M_mass_fraction":a,
                "baseline_physical_support_mass_fraction":b,
                "M_mass_enrichment":None if b<=0 else a/b}
        top_modes.append({
            "descending_mode_rank":k,
            "native_crosssection_index":int(loc),
            "true_yz_lambda_per_s2":float(ly[loc]),
            "native_dt2_yz_lambda":float(dt**2*ly[loc]),
            "M_orthonormality_self_error":abs(norm-1.),
            "fractions":shares})
    order=np.argsort(qy)[::-1][:TOP_NODECOUNT]
    critical=[]
    for i in order:
        critical.append({
            "active_yz_index":int(i),"position_yz_m":xy[i].tolist(),
            "positive_exact_physical_node_area_m2":float(wy[i]),
            "node_area_fraction_of_h2":float(yz_ratio[i]),
            "absolute_signed_roof_distance_h":float(roof_d[i]),
            "cut_roof_band_1h":bool(roof_d[i]<=1.),
            "yz_basis_vector_Rayleigh_per_s2":float(qy[i]),
            "native_dt2_full_xnode_plus_yznode_Rayleigh_lowerbound":
                float(dt**2*(qx[ix]+qy[i]))
        })
    return {
        "positive_original_x_node_count":int(len(wx)),
        "positive_original_yz_node_count":int(len(wy)),
        "full_untruncated_3d_modes":int(len(wx)*len(wy)),
        "true_x_length_m":float(wx.sum()),
        "true_yz_area_m2":float(wy.sum()),
        "true_3d_volume_m3":float(wx.sum()*wy.sum()),
        "minimum_positive_yz_physical_area_m2":float(wy.min()),
        "min_yz_area_fraction_of_h2":float(yz_ratio.min()),
        "maximum_x_lambda_per_s2":float(lx[-1]),
        "maximum_yz_lambda_per_s2":float(ly[-1]),
        "full_max_3d_lambda_per_s2":max_lam,
        "full_native_dt2_max_lambda":float(dt**2*max_lam),
        "maximum_x_eigenvalue_fraction_of_full_max":float(lx[-1]/max_lam),
        "maximum_yz_eigenvalue_fraction_of_full_max":float(ly[-1]/max_lam),
        "local_x_diagonal_Rayleigh_node_index":ix,
        "local_x_max_diagonal_Rayleigh_per_s2":float(qx[ix]),
        "local_yz_max_diagonal_Rayleigh_per_s2":float(qy[iy]),
        "full_3d_single_basis_Rayleigh_lowerbound_per_s2":global_node_lower,
        "full_dt2_single_basis_Rayleigh_lowerbound":float(dt**2*global_node_lower),
        "basis_node_Rayleigh_fraction_of_true_lambda_max":global_node_lower/max_lam,
        "basis_node_alone_certifies_native_leapfrog_unstable":bool(dt**2*global_node_lower>=4.),
        "roof_and_small_area_original_physical_support":masses,
        "top_three_true_generalized_yz_eigenvector_localization":top_modes,
        "top_five_yz_diagonal_Rayleigh_nodes":critical,
        "preserved_original_full_physical_operator_without_mode_filter":True}
