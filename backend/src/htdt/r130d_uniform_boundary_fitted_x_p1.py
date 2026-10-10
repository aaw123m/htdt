"""Deterministic genuinely physical [0,4] uniform-x P1 Neumann arm.

Experimental source/receiver transfer: the ORIGINAL 8 discrete HDF5 nodes
are interpolated from their original physical coordinates, not dropped,
changed in their native file, rounded or post-hoc fitted. The resulting
numerical x basis is changed, so this is NOT identical original point q0.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import numpy as np
from scipy import sparse
from .r130d_native_cut_roof_Q1_galerkin import NativePhysicalCutQ1CrossSection


@dataclass(frozen=True)
class UniformBoundaryFittedX:
    nodes_m:np.ndarray
    Mx:sparse.csr_matrix
    Kx:sparse.csr_matrix
    segments:int
    dx_m:float
    native_h_m:float


def build_uniform_boundary_fitted_x_p1(original_native_h_m:float,*,c_m_s:float=343.2):
    """No grid-specific N selection: N=ceil(4/native h) frozen."""
    h=float(original_native_h_m)
    if not np.isfinite(h) or h<=0 or h>.5 or c_m_s!=343.2:
        raise ValueError("native h or original sound speed changed")
    n=int(math.ceil(4./h))
    x=np.linspace(0.,4.,n+1)
    dx=4./n
    # Fully physical Neumann P1 finite element [0,4], row-lumped M.
    # End support is exactly half dx, with NO tiny truncated segments.
    mass=np.full(n+1,dx)
    mass[0]=mass[-1]=dx/2
    M=sparse.diags(mass,format="csr")
    edges=np.full(n,c_m_s*c_m_s/dx)
    degree=np.bincount(np.r_[np.arange(n),np.arange(1,n+1)],
                      weights=np.r_[edges,edges],minlength=n+1)
    K=sparse.coo_matrix((
        np.r_[degree,-edges,-edges],
        (np.r_[np.arange(n+1),np.arange(n),np.arange(1,n+1)],
         np.r_[np.arange(n+1),np.arange(1,n+1),np.arange(n)])),
        shape=(n+1,n+1)).tocsr()
    if (abs(float(M.sum())-4.)>2e-13
        or max(abs(K@np.ones(n+1)))>5e-8
        or abs(M.diagonal()[0]-dx/2)>2e-14
        or abs(M.diagonal()[-1]-dx/2)>2e-14
        or (K-K.T).nnz):
        raise ValueError("physical uniform-x P1 conservative operator invalid")
    return UniformBoundaryFittedX(x,M,K,n,dx,h)


def project_original_8point_HDF5_to_fitted_x(
    native_axes:list[np.ndarray],native_eight_ixyz:np.ndarray,
    native_eight_weights:np.ndarray,fitted:UniformBoundaryFittedX,
    yz:NativePhysicalCutQ1CrossSection,
    expected_xyz_m:tuple[float,float,float],
)->tuple[np.ndarray,np.ndarray,dict]:
    """Exact piecewise-linear x reprojection, untouched native y-z Q1 nodes.

    Original point-functional is 8 ORIGINAL native xyz nodes and 8 weights.
    HDF5 is *not* rewritten. This exact linear interpolation replaces each
    x node with two physical x-P1 fitted nodes. Physical zeroth and first x
    moments and the full three-dimensional centroid are preserved.
    """
    ax=[np.asarray(q,float) for q in native_axes]
    dims=tuple(len(q) for q in ax)
    inds=np.asarray(native_eight_ixyz,np.int64)
    w=np.asarray(native_eight_weights,float)
    if (len(ax)!=3 or inds.shape!=(8,) or w.shape!=(8,)
        or abs(w.sum()-1)>1e-12 or not np.isfinite(w).all()
        or np.any(inds<0) or np.any(inds>=np.prod(dims))):
        raise ValueError("original native 8 HDF5 source/receiver changed")
    coords=np.asarray(np.unravel_index(inds,dims)).T
    before=np.column_stack([ax[0][coords[:,0]],ax[1][coords[:,1]],ax[2][coords[:,2]]])
    # The original 8 point stencil is *exactly* the continuous target
    # coordinate as a barycentric first moment.
    original_moment=w@before
    if max(abs(original_moment-np.asarray(expected_xyz_m)))>2e-9:
        raise ValueError("original eight HDF5 source/receiver physical centroid wrong")
    tensor=np.zeros((len(fitted.nodes_m),yz.native_yz_modes))
    nz=len(ax[2])
    for (xi,yi,zi),weight in zip(coords,w):
        yp=int(yi)*nz+int(zi)
        if yp not in yz.original_native_yz_to_active:
            raise ValueError("native HDF5 y/z original source outside physical supported Q1 basis")
        idy=yz.original_native_yz_to_active[yp]
        physical_x=float(ax[0][xi])
        t=physical_x/fitted.dx_m
        left=min(int(np.floor(t)),fitted.segments-1)
        frac=t-left
        if physical_x<-1e-12 or physical_x>4+1e-12 or frac<-1e-12 or frac>1+1e-12:
            raise ValueError("original HDF5 x source/receiver outside unchanged physical room")
        tensor[left,idy]+=float(weight)*(1-frac)
        tensor[left+1,idy]+=float(weight)*frac
    px=tensor.sum(axis=1)
    py=tensor.sum(axis=0)
    factor_error=float(np.max(abs(tensor-np.outer(px,py))))
    reprojected=np.array([px@fitted.nodes_m,py@yz.physical_active_yz_node_positions_m[:,0],
                          py@yz.physical_active_yz_node_positions_m[:,1]])
    moment_error=float(np.max(abs(reprojected-original_moment)))
    if (factor_error>1e-12 or moment_error>2e-9
        or abs(px.sum()-1)>1e-12 or abs(py.sum()-1)>1e-12):
        raise ValueError("original eightpoint barycentric physical moments/tensor lost")
    return px,py,{
        "original_native_HDF5_eight_weights_preserved":True,
        "original_physical_centroid_xyz_m":original_moment.tolist(),
        "new_uniform_fitted_P1_centroid_xyz_m":reprojected.tolist(),
        "first_physical_moment_max_absolute_error_m":moment_error,
        "source_receiver_original_Q1_tensor_factorization_max":factor_error,
        "fitted_x_positive_nonzero_P1_weights":int(np.count_nonzero(abs(px)>1e-14)),
        "number_original_native_HDF5_nodes":8}
