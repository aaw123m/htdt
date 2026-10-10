"""First planar reflections enrich an undamped field; FEM closes all boundaries.

All six images are outside the physical convex room. They introduce no new
interior source. Each has the unchanged original source weights (Neumann +1).
Finite images alone are not claimed to be a closed-room Green function.
"""
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import cg,LinearOperator
from .r130d_mfem_binary import csr_norm_inf
from .r130d_newmark_point_green import newmark_point_green_stream

PLANES=np.array([[-1.,0.,0.,0.],[1.,0.,0.,4.],[0.,-1.,0.,0.],
    [0.,1.,0.,4.],[0.,0.,-1.,0.],[0.,.25,1.,4.]])
PLANES[-1]/=np.linalg.norm(PLANES[-1,:3])
PLANES.setflags(write=False)

def first_reflection_sources(source_xyz):
    source=np.asarray(source_xyz,float)
    if source.shape!=(8,3) or not np.isfinite(source).all():raise ValueError('original eight interior sources required')
    margin=PLANES[:,3,None]-PLANES[:,:3]@source.T
    if np.min(margin)<=1e-12:raise ValueError('source is not strictly inside exact R130D room')
    groups=[source]
    for normal,offset in ((p[:3],p[3]) for p in PLANES):
        image=source+2*(offset-source@normal)[:,None]*normal
        if np.min(image@normal-offset)<=1e-12:raise RuntimeError('mirror source is not exterior')
        groups.append(image)
    return groups

def image_field_stream(groups,receiver_xyz,sw,rw,boundary_xyz,normals,B,dt,nt,*,c=343.2):
    """Emit (analytic receiver phi, required complete-boundary FEM force)."""
    streams=[];weight=np.outer(sw,rw).ravel()
    for source in groups:
        delta=boundary_xyz[None,:,:]-source[:,None,:]
        distance=np.linalg.norm(delta,axis=2)
        if np.min(distance)<=0:raise ValueError('source coincides with physical boundary quadrature')
        projection=np.einsum('ijk,jk->ij',delta,normals)/distance
        direct_distance=np.linalg.norm(receiver_xyz[None,:,:]-source[:,None,:],axis=2)
        streams.append((newmark_point_green_stream(distance.ravel(),dt,nt,c=c),
            newmark_point_green_stream(direct_distance.ravel(),dt,nt,c=c),projection))
    for index in range(nt):
        flux=np.zeros(len(boundary_xyz));free=0.
        for boundary,direct,projection in streams:
            _,gradient=next(boundary);potential,_=next(direct)
            flux+=(sw[:,None]*gradient.reshape(projection.shape)*projection).sum(axis=0)
            free+=weight@potential
        yield float(free),-c*c*(B@flux)

def integrate_boundary_correction(M,K,receiver,dt,nt,field_stream,*,rho=1.2,
    progress=None,memory_budget_bytes=512*1024**2,field_allocation_bytes=0):
    n=M.shape[0]
    if (M.shape!=(n,n) or K.shape!=(n,n) or receiver.shape!=(n,) or n<1
        or type(nt) is not int or nt<3 or not np.isfinite([dt,rho]).all() or min(dt,rho)<=0):
        raise ValueError('complete field, native clock and receiver required')
    estimate=64*(M.nnz+K.nnz)+8*(30*n+8*nt)+field_allocation_bytes
    if estimate>memory_budget_bytes:raise ValueError('image enriched solve exceeds memory allocation budget')
    A=csr_matrix(M+dt*dt*K/4);D=M-dt*dt*K/4
    diagonal=A.diagonal()
    if not np.isfinite(diagonal).all() or np.min(diagonal)<=0:raise ValueError('positive midpoint diagonal required')
    preconditioner=LinearOperator(A.shape,matvec=lambda x:x/diagonal);normA=csr_norm_inf(A)
    u=np.zeros(n);v=np.zeros(n);potential=np.zeros(nt);correction=np.zeros(nt)
    free,old_g=next(field_stream)
    if free!=0. or old_g.shape!=(n,) or np.count_nonzero(old_g):raise ValueError('zero initial potential and boundary forcing required')
    work=0.;absolute_work=0.;worst_work=0.;worst_residual=0.;iterations=0
    for index in range(1,nt):
        free,g=next(field_stream)
        if g.shape!=(n,) or not np.isfinite(g).all() or not np.isfinite(free):raise ValueError('finite complete boundary force required')
        rhs=D@u+dt*(M@v)+dt*dt/4*(old_g+g)
        def callback(_):
            nonlocal iterations
            iterations+=1
        u1,info=cg(A,rhs,x0=u,M=preconditioner,rtol=1e-13,atol=0.,maxiter=5000,callback=callback)
        if info!=0:raise RuntimeError('image complete-DOF solve failed')
        denominator=normA*np.linalg.norm(u1,ord=np.inf)+np.linalg.norm(rhs,ord=np.inf)
        residual=float(np.linalg.norm(A@u1-rhs,ord=np.inf)/denominator) if denominator>0 else 0.
        v1=2*(u1-u)/dt-v
        increment=float((u1-u)@((old_g+g)/2));work+=increment;absolute_work+=abs(increment)
        energy=float((v1@(M@v1)+u1@(K@u1))/2)
        defect=abs(energy-work)/max(energy,absolute_work,1.)
        worst_work=max(worst_work,defect);worst_residual=max(worst_residual,residual)
        if not np.isfinite(energy) or energy<0 or defect>2e-10 or residual>5e-13:
            raise RuntimeError(f'image energy/boundary work or residual failed at {index}')
        u,v,old_g=u1,v1,g;correction[index]=receiver@u;potential[index]=correction[index]+free
        if progress is not None and (index%max(1,nt//10)==0 or index==nt-1):progress(index,nt-1,defect)
    t=np.arange(nt)*dt;pressure=rho*np.gradient(potential,dt,edge_order=2)
    H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@pressure
    return t,potential,pressure,correction,H,{'all_fem_dofs':n,'viscosity':0.,'modal_truncation':False,
        'max_correction_energy_work_defect':worst_work,'max_linear_backward_error':worst_residual,
        'cg_iterations_total':iterations,'estimated_allocation_bytes':int(estimate),'memory_budget_bytes':memory_budget_bytes,
        'all_boundaries_corrected':True,'total_singular_field_energy_claimed_finite':False,'convergence_qualified':False}

def enriched_image_q0_trace(M,K,receiver,native,source_xyz,receiver_xyz,source_weights,receiver_weights,
    boundary_xyz,normals,B,*,c=343.2,rho=1.2,progress=None,memory_budget_bytes=512*1024**2):
    source=np.asarray(source_xyz,float);recv=np.asarray(receiver_xyz,float)
    sw=np.asarray(source_weights,float);rw=np.asarray(receiver_weights,float)
    xyz=np.asarray(boundary_xyz,float);normals=np.asarray(normals,float)
    n=M.shape[0];nt=native['Nt'];dt=native['dt']
    if (recv.shape!=(8,3) or sw.shape!=(8,) or rw.shape!=(8,) or not len(xyz) or xyz.shape[1:]!=(3,)
        or normals.shape!=xyz.shape or B.shape!=(n,len(xyz))
        or any(not np.isfinite(a).all() for a in (recv,sw,rw,xyz,normals))
        or abs(sum(sw)-1)>2e-10 or abs(sum(rw)-1)>2e-10 or not np.isfinite(c) or c<=0):
        raise ValueError('original eight-point weights and complete boundary required')
    if np.min(PLANES[:,3,None]-PLANES[:,:3]@recv.T)<=1e-12:raise ValueError('receiver must be inside exact R130D room')
    groups=first_reflection_sources(source)
    field_estimate=64*B.nnz+8*100*len(xyz)*len(groups)
    # The integration routine checks the combined estimate before consuming
    # this generator or allocating any large image/Green arrays.
    stream=image_field_stream(groups,recv,sw,rw,xyz,normals,B,dt,nt,c=c)
    result=integrate_boundary_correction(M,K,receiver,dt,nt,stream,rho=rho,progress=progress,
        memory_budget_bytes=memory_budget_bytes,field_allocation_bytes=field_estimate)
    audit=result[-1];audit.update({'analytic_source_groups':7,'original_interior_source_strength':float(sum(sw)),
        'exterior_images_only':True,'exterior_strength_not_renormalized':True,'mirror_reflection_sign':1,
        'finite_images_alone_claimed_closed_room':False,'source_singularity_analytic':True})
    return result
