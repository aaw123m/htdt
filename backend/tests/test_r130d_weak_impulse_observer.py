import numpy as np
from scipy.integrate import quad
from htdt.r130d_weak_impulse_observer import (
    endpoint_window, endpoint_window_transform, exact_weak_impulse_transfer,
    endpoint_window_derivative,newmark_weak_impulse_transfer)


def test_closed_transform_against_independent_piecewise_quadrature():
    T, a, b = .25, .001, .01
    for beta in (0., 1e-9, -.02, 40.*2*np.pi, -10000., 45000.):
        expected = 0j
        for lo, hi in [(0,a),(a,T-b),(T-b,T)]:
            re = quad(lambda t: float(endpoint_window(t,T,a,b)),lo,hi,
                      weight='cos',wvar=beta,epsabs=1e-13)[0]
            im = quad(lambda t: float(endpoint_window(t,T,a,b)),lo,hi,
                      weight='sin',wvar=beta,epsabs=1e-13)[0]
            expected += re+1j*im
        np.testing.assert_allclose(endpoint_window_transform(beta,T,a,b),expected,atol=2e-12,rtol=1e-10)


def test_full_modal_pressure_not_finite_difference_including_zero_and_resonance():
    f = 40.
    omegas = np.array([0.,2*np.pi*f,1000.,19000.])
    cp = np.array([1.,-.2,.07,.03])
    got = exact_weak_impulse_transfer(omegas**2,cp,frequencies_hz=(f,))[0]
    re, im = 0., 0.
    for lo,hi in [(0,.001),(.001,.24),(.24,.25)]:
        def p(t):
            return 1.2*343.2**2*np.dot(cp,np.cos(omegas*t))*float(endpoint_window(t))
        re += quad(lambda t:p(t)*np.cos(2*np.pi*f*t),lo,hi,epsabs=1e-8,limit=1500)[0]
        im += quad(lambda t:p(t)*np.sin(2*np.pi*f*t),lo,hi,epsabs=1e-8,limit=1500)[0]
    np.testing.assert_allclose(got,re+1j*im,atol=1e-7)


def test_window_has_unit_neighborhood_at_direct_arrival_and_zero_ends():
    arrival = 1./343.2
    np.testing.assert_array_equal(endpoint_window(np.array([arrival-.0001,arrival,arrival+.0001])),1.)
    np.testing.assert_array_equal(endpoint_window(np.array([-.01,0.,.25,.26])),0.)


def test_window_derivative_and_integration_by_parts():
    t=np.array([.0002,.0008,.005,.245,.249])
    eps=1e-8
    np.testing.assert_allclose(endpoint_window_derivative(t),
        (endpoint_window(t+eps)-endpoint_window(t-eps))/(2*eps),rtol=1e-8,atol=1e-6)
    omega,w=930.,2*np.pi*40.
    result=exact_weak_impulse_transfer(np.array([omega**2]),np.array([1.]),frequencies_hz=(40.,),c_m_s=1.,rho_kg_m3=1.)[0]
    def g(t):
        return -np.sin(omega*t)/omega*np.exp(1j*w*t)*(endpoint_window_derivative(t)+1j*w*endpoint_window(t))
    ref=0j
    for lo,hi in [(0,.001),(.001,.24),(.24,.25)]:
        ref+=quad(lambda t:g(t).real,lo,hi,epsabs=1e-12)[0]+1j*quad(lambda t:g(t).imag,lo,hi,epsabs=1e-12)[0]
    np.testing.assert_allclose(result,ref,atol=1e-12)


def test_newmark_interpolant_exactly_reproduces_direct_native_recurrence():
    omega,dt,cp=930.,.00025,.7
    lam=omega**2;theta=2*np.arctan(dt*omega/2)
    kick=343.2**2*dt**2*cp/(1+dt**2*lam/4)
    native=[0.,kick]
    for _ in range(200):native.append(2*np.cos(theta)*native[-1]-native[-2])
    native=np.asarray(native);times=np.arange(len(native))*dt
    interpolated=343.2**2*dt*cp*np.sin(theta/dt*times)/omega
    np.testing.assert_allclose(interpolated,native,atol=1e-11)
    exact=exact_weak_impulse_transfer(np.array([lam]),np.array([cp]))
    errors=[]
    for steps in (2000,4000,8000):
        got=newmark_weak_impulse_transfer(np.array([lam]),np.array([cp]),.25/steps)
        errors.append(np.linalg.norm(got-exact))
    assert 3.7<errors[0]/errors[1]<4.3
    assert 3.7<errors[1]/errors[2]<4.3


def test_free_space_dirac_derivative_pairs_with_the_test_derivative():
    from htdt.r130d_point_impulse_observer import radial_bandlimited_point_green
    r,c,rho,f,T=1.,343.2,1.2,40.,.025
    w=2*np.pi*f
    expected=-1j*w*rho*np.exp(1j*w*r/c)/(4*np.pi*r)
    values=[]
    for K in (20.,40.,80.,160.):
        def fun(t):
            return -rho*radial_bandlimited_point_green(t,r,K)*np.exp(1j*w*t)*(endpoint_window_derivative(t,T,.001,.01)+1j*w*endpoint_window(t,T,.001,.01))
        val=0j
        for lo,hi in [(0,.001),(.001,T-.01),(T-.01,T)]:
            points=[r/c] if lo<r/c<hi else []
            val+=quad(lambda t:fun(t).real,lo,hi,points=points,epsabs=1e-10,limit=2000)[0]+1j*quad(lambda t:fun(t).imag,lo,hi,points=points,epsabs=1e-10,limit=2000)[0]
        values.append(abs(val-expected)/abs(expected))
    # A C2 (not C-infinity) compact test has an algebraic oscillatory tail.
    # Check a bounded small error and decrease of its envelope, not strict
    # monotonicity of two cancellation-dependent finite cutoff samples.
    assert max(values[-2:])<1e-3, values
    assert max(values[-2:])<min(values[:2])/10.
