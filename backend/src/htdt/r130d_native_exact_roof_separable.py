"""Exact sloped-room conservative cut-cell FV tensor separation x vs yz.

For roof z <= 4 - .25*y independent of x:
M3 = Mx (Kronecker) Myz
K3 = Kx (Kronecker) Myz + Mx (Kronecker) Kyz
with no artificial modification of point source or boundary geometry.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy import linalg
from scipy.sparse.linalg import eigsh

from .r130d_embedded_neumann_fv import SlopedPrism,_fluid_section_area


@dataclass(frozen=True)
class NativeRoofSeparable:
    x_original_indices: np.ndarray
    yz_original_flat_indices: np.ndarray
    x_mass: np.ndarray
    yz_mass: np.ndarray
    Mx: sparse.csr_matrix
    Myz: sparse.csr_matrix
    Kx: sparse.csr_matrix
    Kyz: sparse.csr_matrix
    y_size: int
    z_size: int
    full_native_dimensions: tuple[int,int,int]
    grid_h_m: float

    @property
    def total_cells(self):
        return len(self.x_mass)*len(self.yz_mass)

    @property
    def cross_section_cells(self):
        return len(self.yz_mass)


def original_native_xy_z_factorization(axes,*,geometry=None):
    """Derive exact mass and Neumann face matrices independently from 3D CSR."""
    g=geometry or SlopedPrism()
    a=tuple(np.asarray(q,dtype=float) for q in axes)
    if len(a)!=3 or any(q.ndim!=1 or len(q)<5 for q in a):
        raise ValueError("original Cartesian axes unavailable")
    h=float(a[0][1]-a[0][0])
    if not h>0 or any(not np.allclose(np.diff(x),h,rtol=0,atol=2e-11) for x in a):
        raise ValueError("nonuniform PFFDTD original Cartesian axes")
    x,y,z=a
    lo=[np.maximum(w-h/2,0.) for w in a]
    hi=[np.minimum(w+h/2,g.length_m) for w in a]
    widths=[np.maximum(u-v,0.) for v,u in zip(lo,hi)]
    xids=np.flatnonzero(widths[0]>1e-14*h)
    xmass=widths[0][xids].copy()
    yz_mass=np.zeros((len(y),len(z)),dtype=float)
    for j in range(len(y)):
        for k in range(len(z)):
            if widths[1][j]>0 and widths[2][k]>0:
                yz_mass[j,k]=_fluid_section_area(
                    lo[1][j],hi[1][j],lo[2][k],hi[2][k],g)
    yzids=np.flatnonzero(yz_mass.ravel()>1e-15*h*h)
    yzmass=yz_mass.ravel()[yzids]
    if (not len(xids) or not len(yzids) or
        not np.all(np.isfinite(yzmass)) or np.min(yzmass)<=0):
        raise ValueError("original exact room has empty/degenerated cross-section")

    lx={int(i):j for j,i in enumerate(xids)}
    lyz={int(i):j for j,i in enumerate(yzids)}
    xx=[];xy=[];xw=[]
    for i in xids[:-1]:
        if int(i+1) in lx:
            ii=lx[int(i)]
            xx.append(ii);xy.append(lx[int(i+1)]);xw.append(g.sound_speed_m_s**2/h)
    yy=[];yz=[];yw=[]
    for native_yz in yzids:
        jj,kk=np.unravel_index(int(native_yz),(len(y),len(z)))
        jj=int(jj);kk=int(kk)
        i=lyz[int(native_yz)]
        if jj+1<len(y) and int((jj+1)*len(z)+kk) in lyz:
            yface=(y[jj]+y[jj+1])/2
            roof=g.roof_height_at_y0_m-g.roof_drop_per_y_m*yface
            area=max(0.,min(hi[2][kk],roof)-lo[2][kk])
            if area>1e-14*h:
                yy.append(i);yz.append(lyz[int((jj+1)*len(z)+kk)])
                yw.append(g.sound_speed_m_s**2/h*area)
        if kk+1<len(z) and int(jj*len(z)+kk+1) in lyz:
            zface=(z[kk]+z[kk+1])/2
            ymax=(g.roof_height_at_y0_m-zface)/g.roof_drop_per_y_m if g.roof_drop_per_y_m else (
                g.length_m if zface<=g.roof_height_at_y0_m else 0.)
            area=max(0.,min(hi[1][jj],ymax)-lo[1][jj])
            if area>1e-14*h:
                yy.append(i);yz.append(lyz[int(jj*len(z)+kk+1)])
                yw.append(g.sound_speed_m_s**2/h*area)

    def stiffness(n,rr,cc,weights):
        rr=np.asarray(rr,dtype=np.int32)
        cc=np.asarray(cc,dtype=np.int32)
        ww=np.asarray(weights,dtype=float)
        deg=np.bincount(np.r_[rr,cc],weights=np.r_[ww,ww],minlength=n)
        mat=sparse.coo_matrix(
            (np.r_[deg,-ww,-ww],(
             np.r_[np.arange(n),rr,cc],
             np.r_[np.arange(n),cc,rr])),shape=(n,n)).tocsr()
        if (mat-mat.T).nnz or np.max(abs(np.asarray(mat@np.ones(n))))>3e-8:
            raise ValueError("native roof x/yz cut-face operator is nonconservative")
        return mat

    kx=stiffness(len(xids),xx,xy,xw)
    kyz=stiffness(len(yzids),yy,yz,yw)
    mxx=sparse.diags(xmass,format="csr")
    myz=sparse.diags(yzmass,format="csr")
    if abs(float(xmass.sum()*yzmass.sum())-56.)>2e-8:
        raise ValueError("native Kronecker x/yz volume does not reproduce exact 56m3")
    return NativeRoofSeparable(
        xids,yzids,xmass,yzmass,mxx,myz,kx,kyz,
        len(y),len(z),tuple(len(v) for v in a),h)

def prove_native_full_kronecker_equal(existing,sep,*,max_mass_abs=1e-10,
                                      max_stiffness_abs=2e-8):
    """Independent exact matrix/ordered index identity, not just same eigenvalue."""
    xnative=sep.x_original_indices[:,None]
    yz_native=sep.yz_original_flat_indices[None,:]
    flat=(xnative*(sep.y_size*sep.z_size)+yz_native).ravel()
    if not np.array_equal(flat,existing.original_native_flat_indices):
        raise ValueError("3D exact room source-connected native index order not tensor separable")
    m=sparse.kron(sep.Mx,sep.Myz,format="csr")
    k=(sparse.kron(sep.Kx,sep.Myz,format="csr")+
       sparse.kron(sep.Mx,sep.Kyz,format="csr")).tocsr()
    dm=(m-existing.mass_matrix).tocsr()
    dk=(k-existing.stiffness_matrix).tocsr()
    mass_error=float(max(abs(dm.data),default=0.))
    k_error=float(max(abs(dk.data),default=0.))
    if (mass_error>max_mass_abs or k_error>max_stiffness_abs
        or existing.number_of_cells!=sep.total_cells):
        raise ValueError(f"exact original full 3D FV matrices failed Kronecker equality mass={mass_error}, stiffness={k_error}")
    return {"ordered_original_native_indices_exact":True,
            "3D_original_full_mass_Kronecker_max_absolute":mass_error,
            "3D_original_full_stiffness_Kronecker_max_absolute":k_error,
            "native_full_active_nodes":sep.total_cells,
            "x_active_nodes":len(sep.x_mass),
            "yz_active_nodes":len(sep.yz_mass),
            "exact_total_fluid_volume_m3":float(sum(sep.x_mass)*sum(sep.yz_mass)),
            "full_3D_stiffness_nnz":int(existing.stiffness_matrix.nnz)}

def generalized_neumann_modes(M,K,nmodes=None):
    mass=np.asarray(M.diagonal(),dtype=float)
    if mass.ndim!=1 or np.min(mass)<=0 or not np.all(np.isfinite(mass)):
        raise ValueError("cut-volume mass matrix singular")
    im=mass**-.5
    A=sparse.diags(im,format="csr")@K@sparse.diags(im,format="csr")
    n=len(mass)
    if nmodes is None:
        eigenvalues,unit=linalg.eigh(A.toarray())
    else:
        if not 3<=nmodes<n:raise ValueError("invalid frozen positive yz eigenspectrum count")
        eigenvalues,unit=eigsh(A,k=nmodes,sigma=-1.,which="LM",tol=1e-10,
                              maxiter=7000)
        ordering=np.argsort(eigenvalues,kind="stable")
        eigenvalues=eigenvalues[ordering];unit=unit[:,ordering]
    if eigenvalues[0]<-5e-7:
        raise ValueError("negative physical Neumann eigenvalue")
    eigenvalues=np.maximum(eigenvalues,0.)
    vec=im[:,None]*unit
    checkmass=vec.T@(mass[:,None]*vec)
    error=float(np.max(np.abs(checkmass-np.eye(len(eigenvalues)))))
    residuals=np.empty(len(eigenvalues))
    for i,lamb in enumerate(eigenvalues):
        v=vec[:,i];Kv=K@v;lv=mass*v*lamb
        residuals[i]=np.linalg.norm(Kv-lv)/max(1.,np.linalg.norm(Kv),np.linalg.norm(lv))
    if error>5e-8 or max(residuals)>2e-7:
        raise ValueError(f"wrong Neumann x/yz normalized modal accuracy Gram={error} residual={max(residuals)}")
    return eigenvalues,vec,{
        "highest_true_mass_normalized_eigen_residual":float(max(residuals)),
        "maximum_M_orthonormality_error":error,
        "first_zero_neumann_eigen_frequency_hz":float(np.sqrt(eigenvalues[0])/(2*np.pi))}
