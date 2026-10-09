"""Full consistent P1 Galerkin physical mass on the true 56m³ Neumann roof.

Unlike row-sum/lumped P1 time mass, the actual Galerkin element matrices
  Mx_e = dx/6 [[2,1],[1,2]]
  Myz_e = Area/12 [[2,1,1],[1,2,1],[1,1,2]]
are assembled completely, including ALL nonzero off-diagonal FE mass terms.
With the exact physical P1 stiffness these form a genuine conservative
variational acoustic wave solver, no source/receiver/truncation modification.
"""
from __future__ import annotations
import numpy as np
from scipy import sparse,linalg

from .r130d_conforming_roof_p1_fem import ConformingRoofP1Separable


def consistent_physical_P1_tensored_operators(
    fem:ConformingRoofP1Separable,
)->tuple[sparse.csr_matrix,sparse.csr_matrix,sparse.csr_matrix,sparse.csr_matrix]:
    xs=fem.x_positions_m
    triangles=fem.yz_triangles
    yz=fem.yz_positions_m
    xrows=[];xcols=[];xdata=[]
    for i,dx in enumerate(np.diff(xs)):
        if dx<=0:raise ValueError("nonpositive original Cartesian physical FE segment")
        for j,k,w in ((i,i,2),(i,i+1,1),(i+1,i,1),(i+1,i+1,2)):
            xrows.append(j);xcols.append(k);xdata.append(dx*w/6)
    mx=sparse.coo_matrix((xdata,(xrows,xcols)),
                         shape=(len(xs),len(xs))).tocsr()
    p=yz[triangles]
    d1=p[:,1]-p[:,0]
    d2=p[:,2]-p[:,0]
    area=.5*np.abs(d1[:,0]*d2[:,1]-d1[:,1]*d2[:,0])
    if np.min(area)<=0 or abs(float(np.sum(area))-14)>2e-8:
        raise ValueError("physical P1 consistent mass triangulation is not 14m²")
    mloc=area[:,None,None]/12*(np.ones((1,3,3))+np.eye(3)[None,:,:])
    # np.ones + identity yields usual [2,1,1] per row.
    row=np.broadcast_to(triangles[:,:,None],mloc.shape).ravel()
    col=np.broadcast_to(triangles[:,None,:],mloc.shape).ravel()
    my=sparse.coo_matrix((mloc.ravel(),(row,col)),
                         shape=(len(yz),len(yz))).tocsr()
    my.sum_duplicates()
    if not np.allclose(np.asarray(mx.sum(axis=1)).ravel(),fem.x_mass,
                       rtol=3e-13,atol=1e-12):
        raise ValueError("x true FEM consistent mass did not reduce to physical lumped mass")
    if not np.allclose(np.asarray(my.sum(axis=1)).ravel(),fem.yz_mass,
                       rtol=5e-12,atol=1e-12):
        raise ValueError("roof true FEM consistent mass row sums are not physical")
    if np.min(mx.diagonal())<=0 or np.min(my.diagonal())<=0:
        raise ValueError("invalid physical P1 consistent mass diagonal")
    if (mx-mx.T).nnz or (my-my.T).nnz:
        # Numerical exactly symmetric local matrix, no post hoc symmetrizing.
        raise ValueError("true consistent mass matrix not symmetric")
    return mx,my,fem.Kx,fem.Kyz


def generalized_symmetric_P1_consistent_modes(
    M:sparse.csr_matrix,K:sparse.csr_matrix,
)->tuple[np.ndarray,np.ndarray,dict]:
    """Complete dense SPD-generalized eigenspectrum, no filtered modes.

    Keep exact analytical Neumann constant and verify mass norm plus ALL
    true generalized eigenpairs. The Neumann PSD zero eigenvalue is exact
    from physical K*1=0, not an artificial low-mode spectral truncation.
    """
    if M.shape!=K.shape or M.shape[0]<3:
        raise ValueError("invalid original physical Galerkin mass/stiffness")
    vals,v=linalg.eigh(K.toarray(),M.toarray(),check_finite=True,driver="gvd")
    if (vals[0]<-5e-7 or vals[1]<=0 or not np.isfinite(vals).all()
        or np.any(np.diff(vals)<-1e-7)):
        raise ValueError("true consistent physical FEM generalized Neumann spectrum invalid")
    first=float(vals[0])
    mass_of_constant=float(np.sum(M@np.ones(len(vals))))
    if mass_of_constant<=0:
        raise ValueError("physical consistent mass 3D constant norm vanished")
    v[:,0]=np.ones(len(vals))/np.sqrt(mass_of_constant)
    vals[0]=0
    gram=v.T@(M@v)
    orth=float(np.max(np.abs(gram-np.eye(len(vals)))))
    worst=0.
    max_pos=0
    for j,lam in enumerate(vals):
        kv=K@v[:,j]
        mv=(M@v[:,j])*lam
        residual=float(np.linalg.norm(kv-mv)/max(
            1.,np.linalg.norm(kv),np.linalg.norm(mv)))
        if residual>worst:worst=residual;max_pos=j
    if orth>5e-8 or worst>2e-7:
        raise ValueError(f"true FEM consistent SPD M generalized eigen residual {worst} "
                         f"at {max_pos}, mass Gram {orth}")
    return vals,v,{
        "all_physical_consistent_mass_generalized_modes_retained":True,
        "exact_rigid_neumann_constant_mode":True,
        "first_unmodified_numeric_zero_eigenvalue":first,
        "strict_max_all_eigenpair_residual":worst,
        "strict_max_M_orthogonality_residual":orth,
        "first_nonzero_physical_frequency_hz":float(np.sqrt(vals[1])/(2*np.pi))}


def true_full_3d_consistent_P1_MK(mx,my,kx,ky):
    M=sparse.kron(mx,my,format="csr")
    K=(sparse.kron(kx,my,format="csr")+
       sparse.kron(mx,ky,format="csr")).tocsr()
    if abs(float((M@np.ones(M.shape[0])).sum())-56)>2e-8:
        raise ValueError("consistent 3D physical mass lost exact 56m³")
    asymK=K-K.T
    asymM=M-M.T
    kscale=max(float(np.max(np.abs(K.diagonal()))),1.)
    if (float(max(abs(asymK.data),default=0.))/kscale>1e-12
        or float(max(abs(asymM.data),default=0.))>1e-11
        or float(np.max(abs(K@np.ones(K.shape[0]))))/kscale>1e-10):
        raise ValueError("true 3D consistent Galerkin mass/stiffness Neumann SPD invariant broken")
    return M,K
