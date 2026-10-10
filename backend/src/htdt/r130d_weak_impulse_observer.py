"""Compact smooth-in-time observation of an unchanged instantaneous source.

This changes the observer, not the forcing. It cannot qualify the legacy
rectangular sampled-pressure gate. No modes or source samples are removed.
The quintic end ramps make the test function C2 after extension by zero.
"""
from __future__ import annotations

import numpy as np
from scipy.special import spherical_jn


def endpoint_window(t, duration_s=.25, start_ramp_s=.001, end_ramp_s=.01):
    t = np.asarray(t, float)
    T, a, b = duration_s, start_ramp_s, end_ramp_s
    if not (T > 0 and a > 0 and b > 0 and a+b < T):
        raise ValueError("positive disjoint endpoint ramps required")
    def s(u):
        u = np.clip(u, 0., 1.)
        return u**3*(10.-15.*u+6.*u*u)
    return s(t/a)*s((T-t)/b)


def endpoint_window_derivative(t, duration_s=.25, start_ramp_s=.001, end_ramp_s=.01):
    t = np.asarray(t, float)
    T, a, b = duration_s, start_ramp_s, end_ramp_s
    # Validate exactly the same support and disjoint-ramp conditions.
    endpoint_window(t, T, a, b)
    left, right = t/a, (T-t)/b
    dl = np.where((left > 0) & (left < 1), 30.*left**2*(1.-left)**2/a, 0.)
    dr = np.where((right > 0) & (right < 1), -30.*right**2*(1.-right)**2/b, 0.)
    return dl+dr


def _beta33_transform(z):
    """30 integral_0^1 u^2(1-u)^2 exp(i*z*u) du, including z=0."""
    z = np.asarray(z, float)
    x = z/2.
    small = abs(x) < .01
    ratio = np.empty_like(x)
    ratio[small] = 1.-x[small]**2/14.+x[small]**4/504.-x[small]**6/33264.
    ratio[~small] = 15.*spherical_jn(2, abs(x[~small]))/x[~small]**2
    return np.exp(1j*x)*ratio


def endpoint_window_transform(beta_rad_s, duration_s=.25, start_ramp_s=.001,
                              end_ramp_s=.01):
    """Exact integral w(t) exp(i beta t) dt, stable also at beta=0.

Integration by parts evaluates beta(3,3) ramp derivatives analytically.
Only near zero beta, evaluate the same polynomial ramps with Gauss nodes;
the exponential there has argument <1e-3, so 12 nodes suffice to roundoff.
"""
    beta = np.asarray(beta_rad_s, float)
    T, a, b = duration_s, start_ramp_s, end_ramp_s
    if not (T > 0 and a > 0 and b > 0 and a+b < T) or not np.isfinite(beta).all():
        raise ValueError("finite frequencies and positive disjoint ramps required")
    flat = beta.ravel()
    result = np.empty(flat.shape, complex)
    near = abs(flat*T) < 1e-3
    far = flat[~near]
    result[~near] = (np.exp(1j*far*T)*_beta33_transform(-far*b)
                     -_beta33_transform(far*a))/(1j*far)
    if np.any(near):
        x, weights = np.polynomial.legendre.leggauss(12)
        val = np.zeros(np.count_nonzero(near), complex)
        for lo, hi in [(0., a), (a, T-b), (T-b, T)]:
            t = lo+(x+1.)*(hi-lo)/2.
            weight = weights*(hi-lo)/2.*endpoint_window(t, T, a, b)
            val += np.exp(1j*flat[near, None]*t) @ weight
        result[near] = val
    return result.reshape(beta.shape)


def exact_weak_impulse_transfer(eigenvalues, source_receiver_coupling, *,
                               frequencies_hz=(40., 80.), duration_s=.25,
                               start_ramp_s=.001, end_ramp_s=.01,
                               c_m_s=343.2, rho_kg_m3=1.2):
    """P_w/Q for q(t)=A delta(t), Q=A: impulse area cancels exactly.

    phi=A*c^2*cp*sin(omega*t)/omega; p=rho*A*c^2*cp*cos(omega*t).
    ALL modal couplings are retained, including the exact rigid zero mode.
    No sampled finite differences or frequency cutoff are involved.
    """
    lam = np.asarray(eigenvalues, float).ravel()
    cp = np.asarray(source_receiver_coupling, float).ravel()
    f = np.asarray(frequencies_hz, float).ravel()
    if lam.shape != cp.shape or not len(lam) or np.min(lam) < 0 or not np.isfinite(lam).all() or not np.isfinite(cp).all():
        raise ValueError("finite nonnegative full modal system required")
    omega = np.sqrt(lam)
    result = []
    for w in 2*np.pi*f:
        plus = endpoint_window_transform(w+omega, duration_s, start_ramp_s, end_ramp_s)
        minus = endpoint_window_transform(w-omega, duration_s, start_ramp_s, end_ramp_s)
        result.append(rho_kg_m3*c_m_s**2*np.dot(cp, .5*(plus+minus)))
    return np.asarray(result)


def newmark_weak_impulse_transfer(eigenvalues, source_receiver_coupling, dt_s, *,
                                 frequencies_hz=(40.,80.), duration_s=.25,
                                 start_ramp_s=.001,end_ramp_s=.01,
                                 c_m_s=343.2,rho_kg_m3=1.2):
    """Weak derivative of the exact sinusoidal interpolant of q0 Newmark.

    Native kick is c^2*dt^2*cp/(1+dt^2*lambda/4). Newmark phase is
    theta=2 atan(dt*omega/2). Hence phi=A*c^2*cp*sin(omega_eff*t)/omega
    for A=dt, exactly reproducing every native potential sample. The
    derivative of this interpolant is observed, not the legacy stencil.
    """
    lam=np.asarray(eigenvalues,float).ravel()
    cp=np.asarray(source_receiver_coupling,float).ravel()
    if lam.shape!=cp.shape or not len(lam) or np.min(lam)<0 or dt_s<=0 or not np.isfinite(dt_s):
        raise ValueError('nonnegative modes and positive finite Newmark timestep required')
    omega=np.sqrt(lam)
    eff=2.*np.arctan(.5*dt_s*omega)/dt_s
    factor=np.divide(eff,omega,out=np.ones_like(omega),where=omega>0)
    return exact_weak_impulse_transfer(eff**2,cp*factor,frequencies_hz=frequencies_hz,
        duration_s=duration_s,start_ramp_s=start_ramp_s,end_ramp_s=end_ramp_s,
        c_m_s=c_m_s,rho_kg_m3=rho_kg_m3)
