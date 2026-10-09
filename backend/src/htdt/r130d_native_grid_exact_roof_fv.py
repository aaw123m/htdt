"""Experimental EXACT planar roof dual-cell conservative Neumann FV on native PFFDTD grid.

The source and receiver are those in the original native PFFDTD comms;
no original PFFDTD numerical kernels, file contents, or production settings
are changed. Full signed point-q0 impulse supported, not Gaussian-smoothed.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Callable

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import LinearOperator,cg

from .r130d_embedded_neumann_fv import SlopedPrism,_fluid_section_area


@dataclass(frozen=True)
class NativeExactRoofCutcell:
    native_dimensions: tuple[int,int,int]
    native_grid_spacing_m: float
    original_native_flat_indices: np.ndarray
    fluid_volume_m3: np.ndarray
    mass_matrix: sparse.csr_matrix
    stiffness_matrix: sparse.csr_matrix
    exact_flux_face_open_area_m2: np.ndarray
    room_fluid_volume_m3: float
    min_volume_fraction: float

    @property
    def number_of_cells(self)->int:
        return len(self.fluid_volume_m3)


def build_native_exact_roof_fv(
    axes:list[np.ndarray] | tuple[np.ndarray,...], *,
    geometry:SlopedPrism|None=None, max_nodes:int=180000,
)->NativeExactRoofCutcell:
    """Cartesian node-dual Voronoi cubes intersect true planar Neumann room.

    Cell masses are true 3D intersection volumes, not artificial floor masses.
    In particular, tiny roof slivers are retained and handled implicitly.
    """
    g=geometry or SlopedPrism()
    if len(axes)!=3:raise ValueError("3 native node coordinates required")
    axes=[np.asarray(v,dtype=np.float64) for v in axes]
    if any(x.ndim!=1 or len(x)<5 or not np.all(np.isfinite(x)) for x in axes):
        raise ValueError("invalid original native Cartesian coordinate axis")
    h=float(axes[0][1]-axes[0][0])
    if h<=0 or any(not np.allclose(np.diff(v),h,rtol=1e-10,atol=1e-11)
                   for v in axes):
        raise ValueError("native axes must have equal uniform Cartesian spacing")
    shape=tuple(len(v) for v in axes)
    if math.prod(shape)>240000:raise ValueError("native allocated grid size exceeded")
    # Voronoi intervals around original native NODE coordinates, physically
    # intersected with all six [0,L]^3 box walls.
    lows=[np.maximum(v-h/2,0.0) for v in axes]
    highs=[np.minimum(v+h/2,g.length_m) for v in axes]
    widths=[np.maximum(high-low,0.0) for low,high in zip(lows,highs)]
    yz_area=np.zeros((shape[1],shape[2]),dtype=np.float64)
    for j in range(shape[1]):
        if widths[1][j]<=0:continue
        for k in range(shape[2]):
            if widths[2][k]<=0:continue
            yz_area[j,k]=_fluid_section_area(
                lows[1][j],highs[1][j],lows[2][k],highs[2][k],g)
    mass_grid=widths[0][:,None,None]*yz_area[None,:,:]
    nonempty=mass_grid>1e-15*h**3
    if not np.any(nonempty):raise ValueError("empty native-grid exact physical room")
    native_indices=np.flatnonzero(nonempty.ravel(order="C"))
    if len(native_indices)>max_nodes:raise ValueError("cutcell active nodes exceed bounded memory")
    mass=np.asarray(mass_grid.ravel()[native_indices],dtype=np.float64)
    if not np.all(np.isfinite(mass)) or np.min(mass)<=0:
        raise ValueError("negative or nonfinite Voronoi cutcell mass")
    total_volume=float(np.sum(mass))
    exact_volume=g.length_m**2*(g.roof_height_at_y0_m-0.5*g.roof_drop_per_y_m*g.length_m)
    if abs(total_volume-exact_volume)>2e-8:
        raise ValueError(f"exact planar physical room volume mismatch {total_volume} vs {exact_volume}")
    lookup=np.full(math.prod(shape),-1,dtype=np.int32)
    lookup[native_indices]=np.arange(len(native_indices),dtype=np.int32)
    lookup=lookup.reshape(shape)
    ijk=np.asarray(np.unravel_index(native_indices,shape)).T
    rr=[];cc=[];apertures=[]
    for idx,(i,j,k) in enumerate(ijk):
        i=int(i);j=int(j);k=int(k)
        for axis in range(3):
            coord=[i,j,k]
            coord[axis]+=1
            if coord[axis]>=shape[axis]:continue
            other=int(lookup[tuple(coord)])
            if other<0:continue
            if axis==0:
                area=yz_area[j,k]
            elif axis==1:
                yface=(axes[1][j]+axes[1][j+1])/2
                roof=g.roof_height_at_y0_m-g.roof_drop_per_y_m*yface
                area=widths[0][i]*max(0.0,min(highs[2][k],roof)-lows[2][k])
            else:
                zface=(axes[2][k]+axes[2][k+1])/2
                if g.roof_drop_per_y_m==0:
                    admissible_y=g.length_m if zface<=g.roof_height_at_y0_m else 0.
                else:
                    admissible_y=(g.roof_height_at_y0_m-zface)/g.roof_drop_per_y_m
                area=widths[0][i]*max(
                    0.0,min(highs[1][j],admissible_y)-lows[1][j])
            if area>1e-14*h*h:
                rr.append(idx);cc.append(other);apertures.append(area)
    rr=np.asarray(rr,dtype=np.int32)
    cc=np.asarray(cc,dtype=np.int32)
    apertures=np.asarray(apertures,dtype=np.float64)
    flux_weights=(g.sound_speed_m_s**2/h)*apertures
    degree=np.bincount(
        np.concatenate((rr,cc)),weights=np.concatenate((flux_weights,flux_weights)),
        minlength=len(native_indices))
    rows=np.concatenate((np.arange(len(native_indices)),rr,cc))
    cols=np.concatenate((np.arange(len(native_indices)),cc,rr))
    data=np.concatenate((degree,-flux_weights,-flux_weights))
    K=sparse.coo_matrix((data,(rows,cols)),
                        shape=(len(native_indices),len(native_indices))).tocsr()
    M=sparse.diags(mass,format="csr")
    if ((K-K.T).nnz or np.max(np.abs(np.asarray(K@np.ones(len(mass)))))>1e-8
        or np.min(degree)<=0):
        raise ValueError("exact planar aperture Neumann operator is not symmetric/conservative/connected")
    return NativeExactRoofCutcell(
        native_dimensions=shape,native_grid_spacing_m=h,
        original_native_flat_indices=native_indices,
        fluid_volume_m3=mass,mass_matrix=M,stiffness_matrix=K,
        exact_flux_face_open_area_m2=apertures,
        room_fluid_volume_m3=total_volume,
        min_volume_fraction=float(np.min(mass)/h**3))


def implicit_newmark_original_q0(
    system:NativeExactRoofCutcell,*,native_dt_s:float,
    native_source_ix:np.ndarray,native_source_q0_weights:np.ndarray,
    native_receiver_ix:np.ndarray,native_receiver_weights:np.ndarray,
    native_record_samples:int, sound_speed_m_s:float=343.2,
    solver_rtol:float=1e-10,solver_atol:float=1e-12,max_cg_iter:int=500,
    max_true_relative_residual:float=5e-10,
)->tuple[np.ndarray,dict]:
    """True 250ms physical q0 wave: implicit average-acceleration beta=1/4.

    Output u_current BEFORE sample-n update, same original PFFDTD convention.
    F[n] is original point source q0 at n=0 ONLY, not a Gaussian.
    """
    n=system.number_of_cells
    if not(3<=native_record_samples<=2000 and native_dt_s>0):
        raise ValueError("original native 0.25s record outside bounded solver")
    lut=np.full(math.prod(system.native_dimensions),-1,dtype=np.int32)
    lut[system.original_native_flat_indices]=np.arange(n)
    src=np.asarray(native_source_ix,dtype=np.int64)
    recv=np.asarray(native_receiver_ix,dtype=np.int64)
    src_w=np.asarray(native_source_q0_weights,dtype=np.float64)
    rec_w=np.asarray(native_receiver_weights,dtype=np.float64)
    if (src.shape!=(8,) or recv.shape!=(8,) or src_w.shape!=(8,) or rec_w.shape!=(8,)
        or np.any(lut[src]<0) or np.any(lut[recv]<0)
        or abs(float(src_w.sum())-1)>1e-12 or abs(float(rec_w.sum())-1)>1e-12):
        raise ValueError("original point source/receiver eight-node samples changed/outside physical cutcells")
    A=system.stiffness_matrix
    mass=system.fluid_volume_m3
    dt=native_dt_s
    B=(system.mass_matrix+(dt**2/4)*A).tocsr()
    Bdiag=np.asarray(B.diagonal(),dtype=np.float64)
    inv_diag=1/Bdiag
    precond=LinearOperator((n,n),matvec=lambda x:inv_diag*x,dtype=np.float64)
    max_it=0
    max_rel=0.
    all_iterations=0
    solves=0
    def solve(rhs,x0=None):
        nonlocal max_it,max_rel,all_iterations,solves
        count=[0]
        def cb(_):count[0]+=1
        x,code=cg(B,rhs,rtol=solver_rtol,atol=solver_atol,
                  M=precond,maxiter=max_cg_iter,x0=x0,callback=cb)
        rel=float(np.linalg.norm(B@x-rhs)/max(np.linalg.norm(rhs),1e-30))
        if code!=0 or rel>max_true_relative_residual:
            raise ValueError(f"implicit cutcell CG failure code={code} its={count[0]} rel={rel}")
        max_it=max(max_it,count[0]);max_rel=max(max_rel,rel)
        all_iterations+=count[0];solves+=1
        return x
    forcing=np.zeros(n,dtype=np.float64)
    np.add.at(forcing,lut[src],sound_speed_m_s**2*src_w)
    source_kick=dt**2*solve(forcing)
    prev=np.zeros(n,dtype=np.float64)
    current=np.zeros(n,dtype=np.float64)
    waveform=np.empty(native_record_samples,dtype=np.float64)
    probe_indices={1,native_record_samples//4,native_record_samples//2,native_record_samples-1}
    energies=[]
    previous_accel=np.zeros(n,dtype=np.float64)
    for step in range(native_record_samples):
        waveform[step]=float(np.dot(rec_w,current[lut[recv]]))
        if step in probe_indices:
            d=current-prev
            m=(current+prev)/2
            value=float(np.dot(mass,d*d)/dt**2 +np.dot(m,A@m))
            energies.append({"sample_index":int(step),"modified_midpoint_discrete_energy":value})
        if step==0:
            # Original time sample q[0] acts at step0, after observing u[0].
            following=source_kick
        else:
            next_accel=solve(A@current,x0=previous_accel)
            following=2*current-prev-dt**2*next_accel
            previous_accel=next_accel
        prev,current=current,following
    en=[p["modified_midpoint_discrete_energy"] for p in energies]
    drift=(max(en)-min(en))/max(abs(en[0]),1e-30)
    if drift>1e-6:raise ValueError(f"cutcell Newmark energy drift {drift} > frozen 1e-6")
    return waveform,{
        "linear_solver":"Jacobi-preconditioned scipy sparse CG with verified true residual",
        "linear_solve_count":solves,
        "maximum_CG_iterations":max_it,"total_CG_iterations":all_iterations,
        "maximum_true_linear_relative_residual":max_rel,
        "newmark_midpoint_homogeneous_energy_probes":energies,
        "relative_energy_drift_after_source":float(drift),
        "original_discrete_q0_unchanged":True,
        "no_point_source_smoothing_or_taper":True}
