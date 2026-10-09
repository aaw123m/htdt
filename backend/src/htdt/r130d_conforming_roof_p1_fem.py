"""Experimental true-roof conforming P1 Galerkin Neumann FEM (x ⊗ yz).

A physically conforming triangular mesh of the sloped yz section is created
without shrinking/dropping any PFFDTD Cartesian interior node. Exact roof
intersection nodes are added, but the original 8-point source/receiver
weights are injected at their exact original Cartesian nodes; added boundary
nodes have zero source load. No high-frequency spectral modes are removed.

Weak Neumann Laplacian: K_ab=c²*int(grad N_a ⋅ grad N_b).
Physical lumped P1 mass: M_aa=sum_{triangles containing a} area/3.
x uses the analogous exact physical-domain lumped 1D P1 FEM. All endpoint,
roof and room-boundary Neumann conditions are natural (no prescribed values).

Not a production/original upstream PFFDTD qualification solver.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy import sparse
from scipy.spatial import Delaunay


@dataclass(frozen=True)
class ConformingRoofP1Separable:
    x_positions_m: np.ndarray
    yz_positions_m: np.ndarray
    yz_triangles: np.ndarray
    x_mass: np.ndarray
    yz_mass: np.ndarray
    Mx: sparse.csr_matrix
    Myz: sparse.csr_matrix
    Kx: sparse.csr_matrix
    Kyz: sparse.csr_matrix
    native_axes: tuple[np.ndarray,np.ndarray,np.ndarray]
    x_native_map: dict[int,int]
    yz_native_map: dict[tuple[int,int],int]
    exact_cross_section_area_m2: float
    minimum_yz_triangle_area_m2: float
    maximum_yz_triangle_aspect_ratio: float

    @property
    def total_cells(self)->int:
        return len(self.x_positions_m)*len(self.yz_positions_m)

    def sparse_full_3d_operators(self)->tuple[sparse.csr_matrix,sparse.csr_matrix]:
        mass=sparse.kron(self.Mx,self.Myz,format="csr")
        stiffness=(sparse.kron(self.Kx,self.Myz,format="csr")+
                   sparse.kron(self.Mx,self.Kyz,format="csr")).tocsr()
        return mass,stiffness


def _key(v:float)->float:
    return round(float(v),12)


def build_original_native_conforming_roof_p1(
    axes:tuple[np.ndarray,np.ndarray,np.ndarray]|list[np.ndarray], *,
    length_m:float=4., roof0_m:float=4., slope:float=.25,
    speed_m_s:float=343.2, max_yz_nodes:int=2200,max_3d_nodes:int=180000,
)->ConformingRoofP1Separable:
    if (len(axes)!=3 or abs(length_m-4.)>1e-12
        or abs(roof0_m-4.)>1e-12 or abs(slope-.25)>1e-12
        or abs(speed_m_s-343.2)>1e-12):
        raise ValueError("registered original true physical 56m3 roof changed")
    axes=tuple(np.asarray(q,dtype=float) for q in axes)
    if (any(q.ndim!=1 or len(q)<5 or not np.isfinite(q).all()
            or not np.all(np.diff(q)>0) for q in axes)):
        raise ValueError("original Cartesian node axes invalid")
    h=float(axes[0][1]-axes[0][0])
    if (h<=0 or any(not np.allclose(np.diff(q),h,rtol=0,atol=2e-11) for q in axes)):
        raise ValueError("native original space axes changed or no longer uniform")

    # Full physical x interval [0,4]. Retain all original physically interior
    # PFFDTD nodes, including their exact coordinates.
    x_coords={_key(0.):0.,_key(4.):4.}
    x_native_map={}
    for i,v in enumerate(axes[0]):
        if -1e-12<=v<=length_m+1e-12:
            coord=min(max(float(v),0.),length_m)
            x_coords[_key(coord)]=coord
    xs=np.array(sorted(x_coords.values()),dtype=float)
    xlookup={_key(x):i for i,x in enumerate(xs)}
    for i,v in enumerate(axes[0]):
        if _key(v) in xlookup and 0<=v<=4:
            x_native_map[i]=xlookup[_key(v)]
    x_mass=np.zeros(len(xs),dtype=float)
    xa=[];xb=[];xw=[]
    for i,dx in enumerate(np.diff(xs)):
        if dx<=1e-13:
            raise ValueError("duplicate original native x or physical wall vertices")
        x_mass[i]+=dx/2;x_mass[i+1]+=dx/2
        xa.append(i);xb.append(i+1);xw.append(speed_m_s**2/dx)

    # Coordinates of true convex polygon: (0,0), (4,0), (4,3), (0,4).
    # Use exact slanted roof intersections at every original y- and z-grid
    # line. Cartesian nodes inside are preserved in exactly these positions.
    vertices:dict[tuple[float,float],tuple[float,float]]={}
    def add(y:float,z:float):
        y=float(y);z=float(z)
        if (-1e-10<=y<=4.+1e-10 and -1e-10<=z<=4.-slope*y+1e-10
            and z>=-1e-10):
            y=min(4.,max(0.,y))
            z=max(0.,min(z,4.-slope*y))
            vertices[(_key(y),_key(z))]=(y,z)
    for y,z in ((0,0),(4,0),(4,3),(0,4)):add(y,z)
    for y in axes[1]:
        if 0<=y<=4:
            add(y,0)
            add(y,4.-slope*y)
    for z in axes[2]:
        if 0<=z<=4:
            add(0,z)
            if z<=3+1e-12:add(4,z)
            if 3<=z<=4:add((4.-z)/slope,z)
    for y in axes[1]:
        if not 0<=y<=4:continue
        for z in axes[2]:
            if 0<=z<=4.-slope*y+1e-12:
                add(y,z)
    coords=np.array(sorted(vertices.values()),dtype=float)
    if (len(coords)<10 or len(coords)>max_yz_nodes):
        raise ValueError("true roof P1 conforming triangle vertex count outside preregistered limit")
    d=Delaunay(coords)
    tri=np.asarray(d.simplices,dtype=np.int32)
    p=coords[tri]  # (ntri,3,2)
    b=p[:,1]-p[:,0]
    c=p[:,2]-p[:,0]
    cross=b[:,0]*c[:,1]-b[:,1]*c[:,0]
    area=.5*np.abs(cross)
    # The physical domain is exactly convex. No triangle/vertex removal,
    # and all Delaunay elements necessarily lie within the polygon.
    if (not np.isfinite(area).all() or np.min(area)<=1e-14
        or abs(float(np.sum(area))-14.)>2e-8
        or np.max(p[:,:,1]+slope*p[:,:,0]-roof0_m)>1e-10):
        raise ValueError("physical original roof P1 triangulation invalid or nonconforming")
    yz_mass=np.bincount(tri.ravel(),weights=np.repeat(area/3,3),minlength=len(coords))
    if np.min(yz_mass)<=0 or abs(float(sum(yz_mass))-14)>2e-8:
        raise ValueError("invalid true FEM lumped P1 physical mass")
    # Gradients of each local P1 hat function are constant per triangle.
    # Signed determinant ensures correct gradients for either orientation.
    grad=np.empty((len(tri),3,2),dtype=float)
    for local in range(3):
        j=(local+1)%3;k=(local+2)%3
        grad[:,local,0]=(p[:,j,1]-p[:,k,1])/cross
        grad[:,local,1]=(p[:,k,0]-p[:,j,0])/cross
    element=speed_m_s**2 * area[:,None,None]*np.einsum(
        "tic,tjc->tij",grad,grad)
    row=np.broadcast_to(tri[:,:,None],element.shape).ravel()
    col=np.broadcast_to(tri[:,None,:],element.shape).ravel()
    ky=sparse.coo_matrix((element.ravel(),(row,col)),
        shape=(len(coords),len(coords))).tocsr()
    ky.sum_duplicates()
    # The linear P1 partition of unity implies K @ 1 = 0 analytically.
    # Summing narrow roof-triangle element contributions independently can
    # leave O(eps*||K||) floating cancellation on the raw assembled diagonal.
    # Rebuild it from the physical off-diagonal weak-form terms, without
    # removing any triangle, changing mass, or modifying the weak bilinear
    # form beyond its round-off error. This enforces the exact rigid mode.
    offdiag=(ky-sparse.diags(ky.diagonal(),format='csr')).tocsr()
    ky=(offdiag+sparse.diags(-np.asarray(offdiag.sum(axis=1)).ravel(),
         format='csr')).tocsr()
    if not np.isfinite(ky.data).all():
        raise ValueError("nonfinite physically conforming roof FEM stiffness")
    xw=np.asarray(xw,dtype=float)
    xdeg=np.bincount(np.r_[xa,xb],weights=np.r_[xw,xw],minlength=len(xs))
    kx=sparse.coo_matrix(
        (np.r_[xdeg,-xw,-xw],
         (np.r_[np.arange(len(xs)),xa,xb],
          np.r_[np.arange(len(xs)),xb,xa])),
        shape=(len(xs),len(xs))).tocsr()
    mx=sparse.diags(x_mass,format="csr")
    my=sparse.diags(yz_mass,format="csr")
    if (abs(float(x_mass.sum())-4)>1e-10
        or abs(float(yz_mass.sum()*x_mass.sum())-56)>2e-8):
        raise ValueError("true original 56m3 Neumann P1 FEM volume violated")
    for stiffness in (kx,ky):
        asym=stiffness-stiffness.T
        relative_asym=float(max(abs(asym.data),default=0.))/max(
            float(max(abs(stiffness.data))),1.)
        residual=float(max(abs(stiffness@np.ones(stiffness.shape[0]))))/max(
            float(max(abs(stiffness.diagonal()))),1.)
        if relative_asym>1e-11 or residual>5e-10:
            raise ValueError("P1 symmetric zero-flux Neumann operator consistency failed")
    yz_lookup={(_key(y),_key(z)):i for i,(y,z) in enumerate(coords)}
    yz_native_map={}
    for j,y in enumerate(axes[1]):
        if not 0<=y<=4:continue
        for k,z in enumerate(axes[2]):
            key=(_key(y),_key(z))
            if key in yz_lookup and 0<=z<=4.-slope*y+1e-12:
                yz_native_map[(j,k)]=yz_lookup[key]
    all_edge_lengths=np.concatenate(
        [np.linalg.norm(p[:,1]-p[:,0],axis=1),
         np.linalg.norm(p[:,2]-p[:,1],axis=1),
         np.linalg.norm(p[:,0]-p[:,2],axis=1)])
    sizes=all_edge_lengths.reshape((3,-1))
    aspect=float(np.max(sizes.max(axis=0)/sizes.min(axis=0)))
    return ConformingRoofP1Separable(
        xs,coords,tri,x_mass,yz_mass,mx,my,kx,ky,
        axes,x_native_map,yz_native_map,
        float(sum(area)),float(min(area)),aspect)


def original_eightnode_FEM_source_receiver(
    system:ConformingRoofP1Separable, native_flat_indices:np.ndarray,
    native_original_weights:np.ndarray,
)->tuple[np.ndarray,np.ndarray,float]:
    """Inject/observe only original SHA-pinned source/receiver eight nodes."""
    inds=np.asarray(native_flat_indices,dtype=np.int64)
    weights=np.asarray(native_original_weights,dtype=float)
    if (inds.shape!=(8,) or weights.shape!=(8,)
        or not np.isfinite(weights).all() or abs(float(weights.sum())-1)>1e-12):
        raise ValueError("original eight source/receiver native nodal coefficients changed")
    shape=tuple(len(a) for a in system.native_axes)
    if np.any(inds<0) or np.any(inds>=math.prod(shape)):
        raise ValueError("original native HDF5 source indices out of original domain")
    x,y,z=np.unravel_index(inds,shape)
    table=np.zeros((len(system.x_positions_m),len(system.yz_positions_m)),
                   dtype=float)
    for i,j,k,weight in zip(x,y,z,weights):
        if int(i) not in system.x_native_map or (int(j),int(k)) not in system.yz_native_map:
            raise ValueError("original true point sample not retained in physical P1 mesh")
        table[system.x_native_map[int(i)],system.yz_native_map[int(j),int(k)]]+=weight
    wx=table.sum(axis=1)
    wy=table.sum(axis=0)
    residue=float(np.max(np.abs(table-np.outer(wx,wy))))
    if residue>1e-12 or abs(float(sum(wx))-1)>1e-12 or abs(float(sum(wy))-1)>1e-12:
        raise ValueError("original PFFDTD eightnode source or receiver no longer tensor factorizes")
    return wx,wy,residue

def generalized_conforming_p1_neumann_modes(
    M:sparse.csr_matrix,K:sparse.csr_matrix,
)->tuple[np.ndarray,np.ndarray,dict]:
    """All generalized physical P1 FE modes, with exact rigid Neumann zero.

    Skinny sloping-boundary P1 triangles can yield the constant eigenmode's
    *absolute* residual O(eps*max(K)) in a generic dense eigensolver, even
    though K*ones=0 numerically and the 1st eigenvalue is mathematically
    exactly zero. Replace ONLY this analytical rigid mode by the known
    normalized constant vector. The physical stiffness, EVERY non-constant
    mode and all excitation/observation weights are unchanged. Verify all
    modes against the original strict relative generalized residual gate.
    """
    from scipy import linalg
    mass=np.asarray(M.diagonal(),dtype=float)
    if mass.ndim!=1 or min(mass)<=0 or not np.isfinite(mass).all():
        raise ValueError("physical FEM lumped mass invalid")
    invroot=mass**-.5
    a=(sparse.diags(invroot,format="csr")@K@
       sparse.diags(invroot,format="csr")).toarray()
    values,unit=linalg.eigh(a,check_finite=True)
    if values[0]<-5e-7 or values[1]<=0 or not np.isfinite(values).all():
        raise ValueError("true physical FEM Neumann positive spectrum invalid")
    vec=invroot[:,None]*unit
    eig_before=float(values[0])
    vec[:,0]=np.ones(len(mass))/np.sqrt(mass.sum())
    values[0]=0.0
    gram=vec.T@(mass[:,None]*vec)
    massorth=float(np.max(np.abs(gram-np.eye(len(mass)))))
    residuals=np.empty(len(values),dtype=float)
    for j,lam in enumerate(values):
        Kvec=K@vec[:,j]
        Mvec=mass*vec[:,j]*lam
        residuals[j]=np.linalg.norm(Kvec-Mvec)/max(
            1.,np.linalg.norm(Kvec),np.linalg.norm(Mvec))
    worst=float(max(residuals))
    if massorth>5e-8 or worst>2e-7:
        raise ValueError(
            f"true physical conforming P1 3D source modes numerical "
            f"accuracy failed Gram={massorth} maximum residual={worst} "
            f"at {int(np.argmax(residuals))}")
    return values,vec,{
        "analytically_exact_rigid_neumann_zero_mode":True,
        "unreplaced_numerical_1st_eigenvalue_1_over_s2":eig_before,
        "strict_max_generalized_relative_eigenresidual":worst,
        "strict_max_M_orthonormality_error":massorth,
        "first_physical_nonzero_frequency_hz":float(np.sqrt(values[1])/(2*np.pi)),
        "all_original_physical_FEM_eigenmodes_preserved":True}
