import numpy as np
from scipy.linalg import expm
from htdt.r130d_vanishing_viscosity_impulse import viscous_original_q0_transfer,viscous_roots,viscous_original_q0_trace


def test_signed_original_observer_against_independent_state_matrix_exponential():
    dt,nt,h,c,rho=.0002,340,.12,343.2,1.2
    for kappa in (0.,1.):
        lam=np.array([0.,(2*np.pi*40)**2,(2*np.pi*90)**2,2e8,8e9])
        cp=np.array([1.,-.7,.4,.03,-.01])
        nu=kappa*h**3/c**3
        phi=np.zeros(nt)
        for l,w in zip(lam,cp):
            A=np.array([[0.,1.],[-l,-nu*l*l]])
            step=expm(A*dt);state=np.array([0.,c*c*dt*w])
            for n in range(nt):phi[n]+=state[0];state=step@state
        pressure=rho*np.gradient(phi,dt,edge_order=2)
        expected=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*np.arange(nt)[None,:]*dt)@pressure
        got=viscous_original_q0_transfer(lam,cp,dt,nt,h,kappa=kappa)
        np.testing.assert_allclose(got,expected,rtol=2e-9,atol=2e-7)
        t,potential,p=viscous_original_q0_trace(lam,cp,dt,nt,h,kappa=kappa,chunk=2)
        np.testing.assert_allclose(potential,phi,rtol=2e-9,atol=2e-9)
        np.testing.assert_allclose(p,pressure,rtol=2e-8,atol=2e-7)


def test_exact_rigid_mode_critical_mode_and_cubic_vanishing_damping():
    c,h,k=343.2,.12,1.
    nu=k*h**3/c**3
    # nu*lambda^2=2*sqrt(lambda) is the exact critically damped condition.
    l=(2/nu)**(2/3)
    dt,nt=.0001,300
    times=np.arange(nt)*dt
    cp=np.array([1.])
    phi=c*c*dt*times*np.exp(-np.sqrt(l)*times)
    expected=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*times)@(1.2*np.gradient(phi,dt,edge_order=2))
    np.testing.assert_allclose(viscous_original_q0_transfer(np.array([l]),cp,dt,nt,h),expected,rtol=2e-8,atol=1e-8)
    r1,r2,a=viscous_roots(np.array([0.,100.,10000.]),nu)
    assert r1[0]==0 and r2[0]==0
    assert np.max(r1.real)<=0 and np.max(r2.real)<=0
    a2=viscous_roots(np.array([0.,100.,10000.]),nu/8.)[2]
    np.testing.assert_allclose(a2[1:]/a[1:],1/8.)


def test_fixed_physical_modes_recover_undamped_original_observer_at_cubic_order():
    # This checks consistency, not point-source continuum qualification.
    lam=(2*np.pi*np.array([0.,40.,80.,150.]))**2
    cp=np.array([.5,1.,-.3,.02]);dt,nt=.0001,600
    exact=viscous_original_q0_transfer(lam,cp,dt,nt,.12,kappa=0.)
    errors=[]
    for h in (.12,.06,.03,.015):
        value=viscous_original_q0_transfer(lam,cp,dt,nt,h)
        errors.append(np.linalg.norm(value-exact)/np.linalg.norm(exact))
    orders=np.log2(np.array(errors[:-1])/errors[1:])
    assert min(orders)>2.9 and max(orders)<3.1
