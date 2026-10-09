"""Rigorous manufactured harmonic Neumann operator consistency at true oblique roof.

Apply EXACT original native HDF5 6-neighbor boundary stencil and EXACT
physical cut-Q1 weak K to SAME roof-tangential affine harmonic function.
Original PFFDTD native strong L/h² and physical Q1 weak Ku have DIFFERENT
UNITS and are never naively divided or ratio compared. The diagnostic
exposes mathematical boundary consistency, not full 250ms q0 causality.
"""
from __future__ import annotations

import numpy as np

from .r130d_native_cut_roof_Q1_galerkin import (
    NativePhysicalCutQ1CrossSection)

SLOPE=0.25
TRUE_NORMAL=np.array([.25,1.])/np.hypot(.25,1)
TRUE_TANGENT=np.array([1.,-.25])


def true_roof_affine_local_neumann_residual(
    y_axis_m:np.ndarray,z_axis_m:np.ndarray,
    native_bn_ixyz:np.ndarray,native_adj_bn:np.ndarray,
    native_dims:tuple[int,int,int],
    cut_Q1:NativePhysicalCutQ1CrossSection,
)->dict:
    y=np.asarray(y_axis_m,dtype=float)
    z=np.asarray(z_axis_m,dtype=float)
    bn=np.asarray(native_bn_ixyz,dtype=np.int64)
    adj=np.asarray(native_adj_bn,dtype=np.int64)
    nx,ny,nz=native_dims
    if (nx<5 or ny!=len(y) or nz!=len(z)
        or bn.ndim!=1 or adj.shape!=(len(bn),6)
        or len(bn)<10 or np.any(bn<0) or np.any(bn>=nx*ny*nz)
        or np.any((adj!=0)&(adj!=1))
        or not np.isfinite(y).all() or not np.isfinite(z).all()):
        raise ValueError("original actual native HDF5 6-neighbor original roof data malformed")
    h=float(y[1]-y[0])
    if (h<=0 or abs(h-(z[1]-z[0]))>1e-11
        or not np.allclose(np.diff(y),h,atol=1e-11,rtol=0)
        or not np.allclose(np.diff(z),h,atol=1e-11,rtol=0)
        or abs(cut_Q1.physical_area_m2-14)>2e-8):
        raise ValueError("physical native Q1 roof geometry or original h changed")
    ix=bn//(ny*nz)
    iy=(bn//nz)%ny
    iz=bn%nz
    yc=y[iy];zc=z[iz]
    true_roof_z=4-SLOPE*yc
    # Physical roof-only Q1 test basis support strictly interior to all
    # OTHER physical side and bottom walls (basis width <= h). Include
    # original actual boundary node whose +y/+z axis stencil crosses true roof.
    wet=(zc<=true_roof_z+1e-10)
    otherwalls=(ix>=1)&(ix<=nx-2)&(yc>2*h)&(yc<4-2*h)&(zc>2*h)
    close=(true_roof_z-zc>=-1e-10)&(true_roof_z-zc<=2*h+1e-10)
    y_next=np.minimum(iy+1,ny-1)
    z_next=np.minimum(iz+1,nz-1)
    across_y=zc>4-SLOPE*y[y_next]+1e-10
    across_z=z[z_next]>4-SLOPE*yc+1e-10
    blocked_roof_y=(adj[:,2]==0)&across_y
    blocked_roof_z=(adj[:,4]==0)&across_z
    select=wet&otherwalls&close&(blocked_roof_y|blocked_roof_z)
    pos=np.flatnonzero(select)
    if len(pos)<30:
        raise ValueError("cannot find genuine original roof-only Neumann graph nodes")
    # L_h u / h² for affine harmonic u=y−z/4. Differences across +/- y
    # = +/-h and across +/-z = -/+h/4. Crucially, this evaluates
    # EXACT original HDF5 adjacency, not a guessed staircase shape.
    axis_lap=(adj[pos,2]-adj[pos,3]-
              SLOPE*(adj[pos,4]-adj[pos,5]))/h
    if np.any(~np.isfinite(axis_lap)):
        raise ValueError("original native graph manufactured harmonic residual nonfinite")
    # Independent actual arithmetic of original native 7-point stencil on
    # affine field, no timestep/source adjustment. Interior centered
    # stencil exactly annihilates affine harmonic; missing native
    # original Cartesian edges leave 1/h-sized strong boundary defect.
    values=yc-SLOPE*zc
    explicit=np.zeros(len(pos))
    for k,dy,dz in ((2,h,0.),(3,-h,0.),(4,0.,h),(5,0.,-h)):
        explicit+=adj[pos,k]*(yc[pos]+dy-SLOPE*(zc[pos]+dz)-values[pos])
    explicit/=h*h
    if not np.allclose(axis_lap,explicit,atol=1e-9,rtol=2e-11):
        raise ValueError("original roof native 6-neighbor row differs from independent affine action")
    coords=cut_Q1.physical_active_yz_node_positions_m
    target=coords[:,0]-SLOPE*coords[:,1]
    physical_weak=cut_Q1.stiffness@target
    original_yz=iy[pos]*nz+iz[pos]
    mapped=np.array([cut_Q1.original_native_yz_to_active.get(int(k),-1)
                     for k in original_yz])
    if np.any(mapped<0):
        raise ValueError("true original wet roof node missing positive Q1 physical basis support")
    local_weak=physical_weak[mapped]
    # The same yz node repeats across original physical x planes; also
    # evaluate each distinct FE basis exactly once to avoid accidental
    # x-count-related improvement.
    unique=np.unique(mapped)
    unique_residual=physical_weak[unique]
    physical_scale=max(1.,float(np.max(abs(cut_Q1.stiffness.diagonal()))))
    true_n=float(TRUE_NORMAL@TRUE_TANGENT)
    if abs(true_n)>1e-14 or np.any(~np.isfinite(local_weak)):
        raise ValueError("true physical planar roof affine normal not Neumann")
    return {
       "actual_original_native_genuine_roof_stencil_rows":int(len(pos)),
       "physical_true_cut_Q1_unique_roof_only_basis_rows":int(len(unique)),
       "original_real_roof_stencil_y_crossings":int(np.count_nonzero(blocked_roof_y[pos])),
       "original_real_roof_stencil_z_crossings":int(np.count_nonzero(blocked_roof_z[pos])),
       "original_strong_laplacian_affine_harmonic_residual_RMS_inverse_m":float(
           np.linalg.norm(axis_lap)/np.sqrt(len(pos))),
       "original_strong_laplacian_affine_harmonic_residual_peak_inverse_m":float(
           np.max(abs(axis_lap))),
       "original_h_times_strong_laplacian_roof_residual_RMS_dimensionless":float(
           h*np.linalg.norm(axis_lap)/np.sqrt(len(pos))),
       "native_original_strong_action_independent_direct_sixneighbor_max_difference":
           float(np.max(abs(axis_lap-explicit))),
       "true_cut_Q1_roof_only_affine_weak_K_u_RMS_in_Ku_units":float(
           np.linalg.norm(unique_residual)/np.sqrt(len(unique))),
       "true_cut_Q1_roof_only_affine_weak_K_u_peak_in_Ku_units":float(
           np.max(abs(unique_residual))),
       "true_cut_Q1_roof_only_affine_weak_K_u_peak_relative_to_global_K_diagonal":
           float(np.max(abs(unique_residual))/physical_scale),
       "physical_true_roof_affine_exact_normal_derivative":true_n,
       "original_graph_strong_residual_and_true_Q1_weak_residual_have_DIFFERENT_UNITS":True,
       "do_NOT_ascribe_entire_250ms_q0_failure_solely_to_this_boundary_example":True,
       "all_native_Q1_true_cut_positive_support_nodes_preserved":True,
       "fixed_manufactured_affine_field_not_fit_to_original_wave":True}
