"""Complete-DOF undamped impulse evolution, without a dense eigenbasis.

The implicit midpoint/Newmark update preserves M/K energy. Source impulse
area and native pressure clocks survive optional internal substeps. A
successful linear solve is not a convergence qualification.
"""
import numpy as np
from scipy import sparse
from scipy.sparse.linalg import splu,cg,LinearOperator
from .r130d_mfem_binary import csr_norm_inf


def sparse_undamped_q0(mass, stiffness_c2, source, receiver, dt, nt, *,
    substeps=1,c=343.2,rho=1.2,memory_budget_bytes=512*1024*1024,linear_solver='auto',progress=None):
    M=sparse.csr_matrix(mass,dtype=float);K=sparse.csr_matrix(stiffness_c2,dtype=float)
    b=np.asarray(source,float);r=np.asarray(receiver,float);n=M.shape[0]
    if (n<1 or M.shape!=(n,n) or K.shape!=M.shape or b.shape!=(n,) or r.shape!=(n,)
        or not np.isfinite(M.data).all() or not np.isfinite(K.data).all()
        or not np.isfinite(b).all() or not np.isfinite(r).all()
        or not np.isfinite([dt,c,rho]).all() or min(dt,c,rho)<=0
        or type(nt) is not int or nt<3 or type(substeps) is not int or substeps<1
        or nt*substeps>200000 or type(memory_budget_bytes) is not int or memory_budget_bytes<=0
        or linear_solver not in ('auto','direct','cg') or (progress is not None and not callable(progress))):
        raise ValueError('finite complete operators, native clock and bounded work required')
    # Pessimistic dense fill bound for two real LU factors, indices, operators
    # and work. Checked BEFORE factorization; this is an allocation guard,
    # not a measurement or guarantee of peak operating-system memory.
    dense_bound=64*n*n+64*(M.nnz+K.nnz)+64*nt+128*n
    selected='direct' if linear_solver=='auto' and dense_bound<=memory_budget_bytes else linear_solver
    if selected=='auto':selected='cg'
    allocation_bound=dense_bound if selected=='direct' else 128*(M.nnz+K.nnz)+64*nt+512*n
    if allocation_bound>memory_budget_bytes:
        raise ValueError('complete-DOF solve exceeds declared memory budget')
    for operator in (M,K):
        scale=max(csr_norm_inf(operator),1.)
        if np.max(abs((operator-operator.T).data),initial=0)>1e-12*scale:
            raise ValueError('energy-conserving propagation requires symmetric operators')
    if np.min(M.diagonal())<=0 or np.min(K.diagonal())<0:
        raise ValueError('invalid mass or stiffness diagonal')
    step=dt/substeps;A=(M+step*step*K/4).tocsc();B=M-step*step*K/4
    cg_statistics={'solves':0,'iterations':0}
    def build_solver(operator):
        if selected=='direct':
            factor=splu(operator.tocsc())
            return lambda rhs,initial=None:factor.solve(rhs)
        diagonal=operator.diagonal()
        if np.min(diagonal)<=0:raise ValueError('positive Jacobi preconditioner required')
        preconditioner=LinearOperator(operator.shape,matvec=lambda v:v/diagonal)
        def solve(rhs,initial=None):
            def iteration(_):cg_statistics['iterations']+=1
            x,info=cg(operator,rhs,x0=initial,M=preconditioner,rtol=1e-13,atol=0.,maxiter=5000,callback=iteration)
            cg_statistics['solves']+=1
            if info!=0:raise RuntimeError('complete-DOF conjugate-gradient solve did not reach tolerance')
            return x
        return solve
    solveM=build_solver(M);solveA=build_solver(A)
    u=np.zeros(n);v=c*c*dt*solveM(b)
    energy0=float(v@(M@v))
    if not energy0>0:raise ValueError('source has no positive kinetic energy')
    phi=np.zeros(nt);max_drift=0.;max_residual=0.;min_energy=energy0
    normA=csr_norm_inf(A)
    for index in range(1,nt):
        for _ in range(substeps):
            rhs=B@u+step*(M@v);u1=solveA(rhs,initial=u)
            residual=float(np.linalg.norm(A@u1-rhs,ord=np.inf)/(normA*np.linalg.norm(u1,ord=np.inf)+np.linalg.norm(rhs,ord=np.inf)))
            v1=2*(u1-u)/step-v;u,v=u1,v1
            energy=float(v@(M@v)+u@(K@u))
            min_energy=min(min_energy,energy);max_drift=max(max_drift,abs(energy/energy0-1))
            max_residual=max(max_residual,residual)
            if not np.isfinite(energy) or energy<=0 or max_drift>2e-10 or max_residual>5e-13:
                raise RuntimeError(f'complete-DOF energy or residual failed at native state {index}')
        phi[index]=r@u
        if progress is not None and (index%max(1,nt//10)==0 or index==nt-1):
            progress(index,nt-1,max_drift)
    if not np.isfinite(phi).all() or min_energy<=0 or max_drift>2e-10 or max_residual>5e-13:
        raise RuntimeError('complete-DOF energy or linear residual audit failed')
    t=np.arange(nt)*dt;p=rho*np.gradient(phi,dt,edge_order=2)
    H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
    return t,phi,p,H,{'all_dofs':n,'modal_truncation':False,'viscosity':0.,
        'energy_audited_at_every_internal_step':True,'max_energy_relative_drift':max_drift,
        'max_linear_backward_error':max_residual,'native_dt_s':dt,'internal_substeps':substeps,
        'source_impulse_area_s':dt,'allocation_guard_bytes':allocation_bound,
        'linear_solver':selected,'cg_relative_tolerance':1e-13 if selected=='cg' else None,
        'cg_solves':cg_statistics['solves'],'cg_iterations_total':cg_statistics['iterations'],
        'peak_memory_measured':False,'convergence_qualified':False}
