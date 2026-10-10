"""Undamped singular-source enrichment with exact free Newmark point fields.

The closed-room field is free-space phi plus a regular FEM correction w.
The latter is driven by -c^2 d_n(phi_free) on every physical room boundary.
Only the correction has a finite FEM energy; its boundary work is audited.
This is a distinct spatial discretization, not a relabelling of old FDTD.
"""
from pathlib import Path
import hashlib
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import cg,LinearOperator
from .r130d_mfem_binary import csr_norm_inf
from .r130d_newmark_point_green import newmark_point_green_stream


def load_boundary_cloud(path: Path,expected_sha256: str,n,order,refinement):
    with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
    if actual!=expected_sha256:raise ValueError('boundary binary identity failed')
    blob=np.memmap(path,dtype='u1',mode='r');offset=0
    def take(dtype,size):
        nonlocal offset
        extent=np.dtype(dtype).itemsize*size
        if size<0 or offset+extent>len(blob):raise ValueError('truncated boundary binary')
        value=np.frombuffer(blob,dtype=dtype,count=size,offset=offset);offset+=extent;return value
    if bytes(take('u1',8))!=b'R130DB01':raise ValueError('boundary magic changed')
    version,dofs,degree,level,quadrature,count=map(int,take('<u4',6))
    if (version,dofs,degree,level)!=(1,n,order,refinement) or not 2<=quadrature<=24 or count>1_000_000:
        raise ValueError('boundary discretization changed')
    xyz=np.empty((count,3));normals=np.empty((count,3));weight=np.empty(count)
    rows=[];cols=[];data=[]
    for i in range(count):
        xyz[i]=take('<f8',3);normals[i]=take('<f8',3);weight[i]=take('<f8',1)[0]
        size=int(take('<u4',1)[0]);indices=take('<i4',size);values=take('<f8',size)
        if (not 0<size<=1000 or np.min(indices)<0 or np.max(indices)>=n
            or not np.isfinite(values).all() or abs(sum(values)-1)>2e-11):
            raise ValueError('boundary FE shape invalid')
        rows.extend(indices);cols.extend([i]*size);data.extend(values*weight[i])
    if offset!=len(blob) or not np.isfinite(xyz).all() or not np.isfinite(normals).all() or not np.isfinite(weight).all() or min(weight)<=0:
        raise ValueError('boundary quadrature payload invalid')
    np.testing.assert_allclose(np.linalg.norm(normals,axis=1),1.,atol=1e-12,rtol=0)
    area=72.+16*np.sqrt(1.0625)
    np.testing.assert_allclose(sum(weight),area,rtol=0,atol=2e-8)
    np.testing.assert_allclose(weight@normals,[0.,0.,0.],rtol=0,atol=2e-9)
    np.testing.assert_allclose(xyz.T@(weight[:,None]*normals),56*np.eye(3),rtol=0,atol=2e-8)
    B=csr_matrix((data,(rows,cols)),shape=(n,count))
    return xyz,normals,B,{'sha256':actual,'quadrature_order':quadrature,'quadrature_points':count,
        'surface_area_m2':float(sum(weight)),'divergence_moments_verified':True}


def enriched_q0_trace(M,K,receiver,native,source_xyz,receiver_xyz,source_weights,receiver_weights,
    boundary_xyz,normals,B,*,c=343.2,rho=1.2,progress=None,memory_budget_bytes=512*1024**2):
    n=M.shape[0];dt=native['dt'];nt=native['Nt']
    source_xyz=np.asarray(source_xyz,float);receiver_xyz=np.asarray(receiver_xyz,float)
    sw=np.asarray(source_weights,float);rw=np.asarray(receiver_weights,float)
    boundary_xyz=np.asarray(boundary_xyz,float);normals=np.asarray(normals,float)
    if (source_xyz.shape!=(8,3) or receiver_xyz.shape!=(8,3) or sw.shape!=(8,) or rw.shape!=(8,)
        or M.shape!=(n,n) or K.shape!=(n,n) or receiver.shape!=(n,) or B.shape!=(n,len(boundary_xyz))):
        raise ValueError('complete original eight-point functionals required')
    if (not np.isfinite(dt) or dt<=0 or type(nt) is not int or nt<3
        or normals.shape!=boundary_xyz.shape or boundary_xyz.shape[1:]!=(3,)
        or any(not np.isfinite(x).all() for x in (source_xyz,receiver_xyz,sw,rw,boundary_xyz,normals,receiver))
        or abs(sum(sw)-1)>2e-10 or abs(sum(rw)-1)>2e-10 or c<=0 or rho<=0):
        raise ValueError('invalid source, clock or boundary geometry')
    estimate=64*(M.nnz+K.nnz+B.nnz)+8*(100*len(boundary_xyz)+30*n+8*nt)
    if estimate>memory_budget_bytes:raise ValueError('enriched solve exceeds memory allocation budget')
    delta=boundary_xyz[None,:,:]-source_xyz[:,None,:];distance=np.linalg.norm(delta,axis=2)
    projection=np.einsum('ijk,jk->ij',delta,normals)/distance
    direct_distance=np.linalg.norm(receiver_xyz[None,:,:]-source_xyz[:,None,:],axis=2)
    if np.min(distance)<=0 or np.min(direct_distance)<=0:raise ValueError('coincident point singularity')
    direct_weight=(sw[:,None]*rw[None,:]).ravel()
    boundary_stream=newmark_point_green_stream(distance.ravel(),dt,nt,c=c)
    direct_stream=newmark_point_green_stream(direct_distance.ravel(),dt,nt,c=c)
    A=csr_matrix(M+dt*dt*K/4);D=M-dt*dt*K/4
    diagonal=A.diagonal();preconditioner=LinearOperator(A.shape,matvec=lambda x:x/diagonal)
    u=np.zeros(n);v=np.zeros(n);old_g=np.zeros(n);potential=np.zeros(nt);correction=np.zeros(nt)
    work=0.;absolute_work=0.;worst_work=0.;worst_residual=0.;normA=csr_norm_inf(A);iterations=0
    next(boundary_stream);next(direct_stream)
    for index in range(1,nt):
        free,gradient=next(boundary_stream)
        flux=(sw[:,None]*gradient.reshape(distance.shape)*projection).sum(axis=0)
        g=-c*c*(B@flux)
        rhs=D@u+dt*(M@v)+dt*dt/4*(old_g+g)
        def callback(_):
            nonlocal iterations
            iterations+=1
        u1,info=cg(A,rhs,x0=u,M=preconditioner,rtol=1e-13,atol=0.,maxiter=5000,callback=callback)
        if info!=0:raise RuntimeError('enriched complete-DOF linear solve failed')
        denominator=normA*np.linalg.norm(u1,ord=np.inf)+np.linalg.norm(rhs,ord=np.inf)
        residual=float(np.linalg.norm(A@u1-rhs,ord=np.inf)/denominator) if denominator>0 else 0.
        v1=2*(u1-u)/dt-v
        increment=float((u1-u)@((old_g+g)/2));work+=increment;absolute_work+=abs(increment)
        energy=float((v1@(M@v1)+u1@(K@u1))/2)
        defect=abs(energy-work)/max(energy,absolute_work,1.)
        worst_work=max(worst_work,defect);worst_residual=max(worst_residual,residual)
        if not np.isfinite(energy) or energy<0 or defect>2e-10 or residual>5e-13:
            raise RuntimeError(f'enriched boundary work or residual audit failed at {index}')
        u,v,old_g=u1,v1,g
        direct,_=next(direct_stream)
        correction[index]=receiver@u;potential[index]=correction[index]+direct_weight@direct
        if progress is not None and (index%max(1,nt//10)==0 or index==nt-1):progress(index,nt-1,defect)
    t=np.arange(nt)*dt;pressure=rho*np.gradient(potential,dt,edge_order=2)
    H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@pressure
    return t,potential,pressure,correction,H,{'all_fem_dofs':n,'viscosity':0.,'modal_truncation':False,
        'source_singularity_analytic':True,'max_correction_energy_work_defect':worst_work,
        'max_linear_backward_error':worst_residual,'cg_iterations_total':iterations,
        'estimated_allocation_bytes':int(estimate),'memory_budget_bytes':memory_budget_bytes,
        'total_singular_field_energy_claimed_finite':False,'convergence_qualified':False}
