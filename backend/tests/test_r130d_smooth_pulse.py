import numpy as np
from scipy.integrate import quad, solve_ivp
from htdt.r130d_smooth_pulse import finite_gaussian_transform, exact_finite_gaussian_pressure_modes, all_mode_newmark_gaussian_trace, all_mode_midpoint_gaussian_trace


def test_finite_gaussian_transform_against_quadrature_and_tail_asymptotic():
    for a in (0., 300., -1200., 9000.):
        q = lambda t: np.exp(-.5*((t-.04)/.004)**2)
        r = quad(lambda t: q(t)*np.cos(a*t),0,.25,epsabs=1e-14,limit=500)[0]
        j = quad(lambda t: q(t)*np.sin(a*t),0,.25,epsabs=1e-14,limit=500)[0]
        np.testing.assert_allclose(finite_gaussian_transform(a),r+1j*j,atol=1e-13)
    # High modes remain finite and have the physical causal-start tail.
    a = 1e7
    actual = finite_gaussian_transform(a)
    assert abs(actual) > 0
    np.testing.assert_allclose(actual, 1j*np.exp(-50)/a,rtol=1e-3,atol=0)


def test_exact_pressure_transform_against_independent_ode_with_resonance_and_zero_mode():
    for lam in (0., (2*np.pi*40)**2, (2*np.pi*110)**2):
        cp = .07
        def rhs(t, z):
            q = np.exp(-.5*((t-.04)/.004)**2)
            return [z[1],343.2**2*cp*q-lam*z[0],
                    1.2*z[1]*np.cos(2*np.pi*40*t),1.2*z[1]*np.sin(2*np.pi*40*t)]
        sol = solve_ivp(rhs,[0,.25],[0,0,0,0],rtol=1e-10,atol=1e-11,max_step=.0001)
        expected = (sol.y[2,-1]+1j*sol.y[3,-1])/finite_gaussian_transform(2*np.pi*40)
        actual = exact_finite_gaussian_pressure_modes(np.array([lam]),np.array([cp]))[0,0]
        np.testing.assert_allclose(actual,expected,atol=1e-6,rtol=3e-9)


def test_full_newmark_trace_converges_to_exact_pressure_transform():
    lam = np.array([0.,(2*np.pi*55)**2,(2*np.pi*200)**2,(2*np.pi*1300)**2])
    cp = np.array([.02,.03,-.006,.005])
    exact = exact_finite_gaussian_pressure_modes(lam,cp).sum(axis=1)
    errors = []
    for dt in (.00025,.000125,.0000625):
        observed,_,_,_ = all_mode_newmark_gaussian_trace(lam,cp,dt,int(.25/dt)+1)
        errors.append(np.linalg.norm(observed-exact))
    assert errors[0]/errors[1] > 3.8 and errors[1]/errors[2] > 3.8


def test_midpoint_pressure_and_exact_finite_time_integral_have_second_order_error():
    lam = np.array([0.,(2*np.pi*55)**2,(2*np.pi*200)**2,(2*np.pi*1300)**2])
    cp = np.array([.02,.03,-.006,.005])
    exact = exact_finite_gaussian_pressure_modes(lam,cp).sum(axis=1)
    errors = []
    for steps in (1000,2000,4000):
        observed,t,p,q = all_mode_midpoint_gaussian_trace(lam,cp,steps)
        assert abs(t[-1]+.25/steps/2-.25) < 1e-14
        errors.append(np.linalg.norm(observed-exact))
    assert errors[0]/errors[1] > 3.8 and errors[1]/errors[2] > 3.8


def test_discrete_midpoint_frequencies_against_independent_ode_integrals():
    frequencies=np.array([41.,51.,79.])
    lam=(2*np.pi*51)**2
    cp=.02
    def rhs(t,z):
        q=np.exp(-.5*((t-.04)/.004)**2)
        p=1.2*z[1]
        return np.r_[z[1],343.2**2*cp*q-lam*z[0],
                     p*np.cos(2*np.pi*frequencies*t),p*np.sin(2*np.pi*frequencies*t)]
    sol=solve_ivp(rhs,[0,.25],np.zeros(8),rtol=1e-10,atol=1e-11,max_step=.0001)
    expected=(sol.y[2:5,-1]+1j*sol.y[5:8,-1])/finite_gaussian_transform(2*np.pi*frequencies)
    actual,_,_,_=all_mode_midpoint_gaussian_trace(np.array([lam]),np.array([cp]),64000,frequencies_hz=frequencies)
    np.testing.assert_allclose(actual,expected,rtol=2e-5,atol=1e-5)
