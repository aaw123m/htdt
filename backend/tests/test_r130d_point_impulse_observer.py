import numpy as np
from scipy.integrate import quad
from htdt.r130d_point_impulse_observer import (
    radial_bandlimited_point_green, rectangular_pressure_transfer)


def test_closed_form_integral_against_independent_direct_time_quadrature():
    r, K, T, f = 1., 30., .025, 40.
    w = 2*np.pi*f
    re = quad(lambda t: radial_bandlimited_point_green(t,r,K)*np.cos(w*t),
              0,T,epsabs=1e-12,limit=500)[0]
    im = quad(lambda t: radial_bandlimited_point_green(t,r,K)*np.sin(w*t),
              0,T,epsabs=1e-12,limit=500)[0]
    result = rectangular_pressure_transfer(r,K,T,f)
    np.testing.assert_allclose(result["bulk"], -1j*w*1.2*(re+1j*im), atol=2e-10)


def test_pressure_derivative_integral_against_direct_analytic_kernel_derivative():
    r, K, T, c, rho, f = 1., 30., .025, 343.2, 1.2, 40.
    def derivative(t):
        u, v = r-c*t, r+c*t
        def d_sinc(x):
            if abs(x) < 1e-6:
                return -K**3*x/3.
            return (K*x*np.cos(K*x)-np.sin(K*x))/x**2
        return -c*c/(4*np.pi**2*r)*(d_sinc(u)+d_sinc(v))
    re = quad(lambda t: rho*derivative(t)*np.cos(2*np.pi*f*t),0,T,epsabs=1e-10,limit=500)[0]
    im = quad(lambda t: rho*derivative(t)*np.sin(2*np.pi*f*t),0,T,epsabs=1e-10,limit=500)[0]
    result = rectangular_pressure_transfer(r,K,T,f)
    np.testing.assert_allclose(result["rectangular_pressure"], re+1j*im, atol=2e-9)


def test_unfiltered_endpoint_does_not_decay_under_resolution_increase():
    r, T, c = 1., .25, 343.2
    # Choose two predeclared subsequences where the leading endpoint sine
    # alternates sign. Nothing is applied to original R130D data.
    vals = []
    for n in (106, 106+212*100, 106+212*10000):
        pair = []
        for sign in (1, -1):
            K = (2*np.pi*n + sign*np.pi/2)/(c*T-r)
            pair.append(rectangular_pressure_transfer(r,K,T,40.))
        vals.append(pair)
    for pair in vals:
        assert abs(pair[0]["endpoint"]-pair[1]["endpoint"]) > .2
    exact = vals[-1][0]["distributional_direct_pressure"]
    # The smooth potential integral error decreases O(1/K), in contrast
    # with the endpoint, whose two subsequences stay >0.2 apart.
    assert max(abs(p["bulk"]-exact) for p in vals[-1]) < .0002
