"""Experimental original-Cartesian Q1 variational Neumann FEM on exact roof.

Unlike P1 Delaunay triangles, use the ORIGINAL native Cartesian (y,z)
rectangle-bilinear basis unchanged, including exterior original nodes
whose shape supports physically intersect the room. Integrate EVERY Q1
mass and gradient product over the exact polygon cut by z=4-y/4, physical
y/z walls and each original native Cartesian rectangle. This is real
consistent Galerkin, no stiffness/mass floor, sliver or mode deletion,
and all 8 original HDF5 source/receiver nodes remain exactly mapped.

Integrals are exactly polynomial (mass total degree <=4; stiffness <=2)
using the positive six-point degree-four triangular Gaussian quadrature
on fans of the TRUE clipped physical polygons. Neumann is a natural BC.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from scipy import sparse

from .r130d_embedded_neumann_fv import _clip_roof


@dataclass(frozen=True)
class NativePhysicalCutQ1CrossSection:
    y_native_positions_m: np.ndarray
    z_native_positions_m: np.ndarray
    yz_original_flat_indices: np.ndarray
    physical_active_yz_node_positions_m: np.ndarray
    mass: sparse.csr_matrix
    stiffness: sparse.csr_matrix
    physical_area_m2: float
    polygon_intersecting_native_cells: int
    minimum_strict_positive_cut_polygon_m2: float
    original_native_yz_to_active: dict[int,int]
    occupied_cartesian_original_exterior_nodes: int

    @property
    def native_yz_modes(self)->int:
        return len(self.yz_original_flat_indices)


def _degree_four_six_positive_triangle_rule():
    a=0.445948490915965
    b=0.108103018168070
    c=0.091576213509771
    d=0.816847572980459
    w1=0.223381589678011
    w2=0.109951743655322
    bary=np.array([
        (a,a,b),(a,b,a),(b,a,a),
        (c,c,d),(c,d,c),(d,c,c)],dtype=float)
    weights=np.array([w1]*3+[w2]*3,dtype=float)
    if abs(weights.sum()-1)>1e-14 or abs(bary.sum(axis=1)-1).max()>1e-14:
        raise ValueError("exact degree-four 6-node true roof Q1 quadrature is invalid")
    if np.min(weights)<=0 or np.min(bary)<=0:
        raise ValueError("true positive physical volume quadrature invalid")
    return bary,weights


def cut_roof_native_original_Q1_galerkin_yz(
    y_original_native_m:np.ndarray,
    z_original_native_m:np.ndarray, *,
    c_m_s:float=343.2,
    max_active_yz_nodes:int=3300,
)->NativePhysicalCutQ1CrossSection:
    y=np.asarray(y_original_native_m,dtype=float)
    z=np.asarray(z_original_native_m,dtype=float)
    if (y.ndim!=1 or z.ndim!=1 or len(y)<5 or len(z)<5
        or not np.isfinite(y).all() or not np.isfinite(z).all()
        or abs(c_m_s-343.2)>1e-12):
        raise ValueError("invalid original native physical Q1 room or sound speed")
    hy=np.diff(y);hz=np.diff(z)
    if (min(hy)<=0 or min(hz)<=0
        or not np.allclose(hy,hy[0],rtol=0,atol=2e-11)
        or not np.allclose(hz,hz[0],rtol=0,atol=2e-11)
        or abs(hy[0]-hz[0])>2e-11):
        raise ValueError("true original Cartesian Q1 native h altered")
    ny,nz=len(y),len(z)
    bary,weights=_degree_four_six_positive_triangle_rule()
    nall=ny*nz
    rr=[];cc=[];mm=[];kk=[]
    total_area=0.
    ncut=0
    min_area=float("inf")
    coords=np.column_stack((np.repeat(y,nz),np.tile(z,ny)))
    for j in range(ny-1):
        yl=max(float(y[j]),0.)
        yr=min(float(y[j+1]),4.)
        if yr<=yl:continue
        for k in range(nz-1):
            zl=max(float(z[k]),0.)
            zr=min(float(z[k+1]),4.)
            if zr<=zl:continue
            poly=_clip_roof(
                [(yl,zl),(yr,zl),(yr,zr),(yl,zr)],
                roof0=4.,slope=.25)
            if len(poly)<3:continue
            points=np.asarray(poly,dtype=float)
            local_mass=np.zeros((4,4),dtype=float)
            local_stiffness=np.zeros((4,4),dtype=float)
            cell_area=0.
            for q in range(1,len(poly)-1):
                corners=points[[0,q,q+1]]
                ds=corners[1]-corners[0]
                dt=corners[2]-corners[0]
                tri_area=.5*abs(ds[0]*dt[1]-ds[1]*dt[0])
                if tri_area<=0:continue
                # Three physical triangle vertices, six true Gaussian nodes.
                pts=bary@corners
                xi=(pts[:,0]-y[j])/hy[j]
                eta=(pts[:,1]-z[k])/hz[k]
                phi=np.column_stack(((1-xi)*(1-eta),xi*(1-eta),
                                     (1-xi)*eta,xi*eta))
                gy=np.column_stack((-(1-eta),1-eta,-eta,eta))/hy[j]
                gz=np.column_stack((-(1-xi),-xi,1-xi,xi))/hz[k]
                local_mass+=tri_area*(phi.T*weights)@phi
                local_stiffness+=c_m_s**2*tri_area*(
                    (gy.T*weights)@gy+(gz.T*weights)@gz)
                cell_area+=tri_area
            if cell_area<=0:continue
            total_area+=cell_area
            ncut+=1
            min_area=min(min_area,cell_area)
            flat=[j*nz+k,(j+1)*nz+k,j*nz+k+1,(j+1)*nz+k+1]
            for a in range(4):
                for b in range(4):
                    rr.append(flat[a]);cc.append(flat[b])
                    mm.append(local_mass[a,b]);kk.append(local_stiffness[a,b])
    if ncut<10 or abs(total_area-14)>2e-8 or not np.isfinite(total_area):
        raise ValueError(f"original true roof Q1 polygon integration lost physical 14m2 area: {total_area}")
    allm=sparse.coo_matrix((mm,(rr,cc)),shape=(nall,nall)).tocsr()
    allk=sparse.coo_matrix((kk,(rr,cc)),shape=(nall,nall)).tocsr()
    allm.sum_duplicates();allk.sum_duplicates()
    diag=np.asarray(allm.diagonal(),dtype=float)
    # Select only mathematically zero support functions, NOT small positive
    # physical roof-cut quadrilateral mass/area or high-frequency modes.
    active=np.flatnonzero(diag>0)
    if len(active)>max_active_yz_nodes or len(active)<10:
        raise ValueError("original native Q1 true-domain active physical nodes outside frozen resource bounds")
    if np.any(diag[active]<=0) or min_area<=0:
        raise ValueError("physical Q1 positive support mass absent")
    M=allm[active,:][:,active].tocsr()
    K=allk[active,:][:,active].tocsr()
    # Mathematically Q1 sums to 1, and K1=0 on true physical Neumann
    # geometry. Correct only floating diagonal row-cancellation, keeping
    # every true Q1 source/mass and physical element/stiffness coefficient.
    offdiag=(K-sparse.diags(K.diagonal(),format="csr")).tocsr()
    K=(offdiag+sparse.diags(-np.asarray(offdiag.sum(axis=1)).ravel(),
                           format="csr")).tocsr()
    scale=max(float(max(abs(K.data),default=0.)),1.)
    if (float(max(abs((M-M.T).data),default=0.))>1e-8
        or float(max(abs((K-K.T).data),default=0.))/scale>1e-12
        or float(max(abs(K@np.ones(len(active)))))/scale>1e-10
        or abs(float(M.sum())-14)>2e-8
        or not np.isfinite(M.data).all()
        or not np.isfinite(K.data).all()):
        raise ValueError("true original Q1 exact physical Neumann/Galerkin integration failed")
    lut={int(original):i for i,original in enumerate(active)}
    outside=sum(not(0<=coords[i,0]<=4 and 0<=coords[i,1]<=4-.25*coords[i,0])
                for i in active)
    return NativePhysicalCutQ1CrossSection(
        y,z,active,coords[active],M,K,total_area,ncut,min_area,lut,int(outside))


def original_eightnode_native_HDF5_Q1_source_receiver(
    native_full_dims:tuple[int,int,int],
    x_native_index_to_physical_FEM:dict[int,int],
    cross:NativePhysicalCutQ1CrossSection,
    native_original_eight_indices:np.ndarray,
    native_original_eight_weights:np.ndarray,
    x_physical_node_count:int,
)->tuple[np.ndarray,np.ndarray,float]:
    src=np.asarray(native_original_eight_indices,dtype=np.int64)
    weights=np.asarray(native_original_eight_weights,dtype=float)
    if (src.shape!=(8,) or weights.shape!=(8,)
        or not np.isfinite(weights).all() or
        abs(float(weights.sum())-1)>1e-12
        or np.any(src<0) or np.any(src>=np.prod(native_full_dims))):
        raise ValueError("true original PFFDTD q0 HDF5 eightpoint source modified")
    native_ijk=np.asarray(np.unravel_index(src,native_full_dims)).T
    tensor=np.zeros((x_physical_node_count,cross.native_yz_modes),dtype=float)
    for (i,j,k),w in zip(native_ijk,weights):
        yzid=int(j)*len(cross.z_native_positions_m)+int(k)
        if (int(i) not in x_native_index_to_physical_FEM
            or yzid not in cross.original_native_yz_to_active):
            raise ValueError("original eightnode native source/receiver basis not supported by Q1 cut physical roof")
        xi=x_native_index_to_physical_FEM[int(i)]
        yi=cross.original_native_yz_to_active[yzid]
        tensor[xi,yi]+=float(w)
    px=tensor.sum(axis=1)
    py=tensor.sum(axis=0)
    factor_error=float(np.max(abs(tensor-np.outer(px,py))))
    if factor_error>1e-12 or abs(px.sum()-1)>1e-12 or abs(py.sum()-1)>1e-12:
        raise ValueError("native eight original trilinear physical Q1 source tensor factorization failed")
    return px,py,factor_error


def generalized_native_original_Q1_full_physical_neumann_modes(
    M:sparse.csr_matrix,K:sparse.csr_matrix,
)->tuple[np.ndarray,np.ndarray,dict]:
    """All physical ghost-supported cut-Q1 generalized eigenmodes, no cuts.

    Near-zero consistent mass on true original-grid roof slivers yields
    generalized frequencies up to ~1e12 1/s², and a generic dense LAPACK
    solver may round the rigid Neumann eigenvalue to -1e-6 1/s². This is
    1e-18 *largest* physical eigenvalue, not a negative elastic mode.
    Impose ONLY exact analytical rigid Neumann constant eigenvector,
    recompute strict physical full eigen residual and M Gram on EVERY mode.
    Reject ANY negative second/nonconstant eigenvalue, true K null or
    modal residual failure. No mass floor, no high-mode truncation.
    """
    from scipy import linalg
    if M.shape!=K.shape or M.shape[0]<3:
        raise ValueError("invalid physically truncated original native Q1 generalized operators")
    mass=M.toarray()
    stiffness=K.toarray()
    vals,vec=linalg.eigh(stiffness,mass,check_finite=True,driver="gvd")
    if (not np.isfinite(vals).all() or vals[1]<=0
        or np.any(np.diff(vals)<-1e-7)
        or abs(float(vals[0]))/max(float(vals[-1]),1)>1e-13):
        raise ValueError("physical original native Q1 generalized eigen spectrum is not Neumann positive")
    raw_zero=float(vals[0])
    physical_vol=float(M.sum())
    if physical_vol<=0 or not np.isfinite(physical_vol):
        raise ValueError("true original Q1 Neumann volume invalid")
    vzero=np.ones(len(vals))/np.sqrt(physical_vol)
    rel_zero=float(np.linalg.norm(K@vzero)/max(
        1.,np.linalg.norm(K@vzero),np.linalg.norm(M@vzero)))
    if rel_zero>2e-7:
        raise ValueError("physical Q1 rigid mode is not an actual Neumann zero")
    vals[0]=0.
    vec[:,0]=vzero
    Gram=vec.T@(M@vec)
    gramerr=float(np.max(np.abs(Gram-np.eye(len(vals)))))
    worst=0.;worst_idx=0
    for j,lam in enumerate(vals):
        Kv=K@vec[:,j]
        Mv=(M@vec[:,j])*lam
        res=float(np.linalg.norm(Kv-Mv)/max(1.,np.linalg.norm(Kv),np.linalg.norm(Mv)))
        if res>worst:worst=res;worst_idx=j
    if gramerr>5e-8 or worst>2e-7:
        raise ValueError(f"original physical Q1 fully retained eigenmode strict residual failed worst={worst} i={worst_idx} M-gram={gramerr}")
    return vals,vec,{
        "full_original_native_Q1_Galerkin_modes_retained_no_cut":True,
        "analytic_exact_neumann_rigid_zero":True,
        "raw_lapack_first_eigenvalue_roundoff_1_s2":raw_zero,
        "raw_neumann_zero_to_full_spectrum_ratio":float(abs(raw_zero)/max(float(vals[-1]),1.)),
        "strict_all_original_Q1_modes_generalized_residual":worst,
        "strict_M_orthonormality_Gram_maxerror":gramerr,
        "physical_first_nonzero_original_Q1_frequency_hz":float(np.sqrt(vals[1])/(2*np.pi)),
        "highest_true_original_Q1_eigenvalue_1_s2":float(vals[-1])}
