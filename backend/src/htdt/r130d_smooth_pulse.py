"""Independent continuous physical pulse control, never original point-q0.

q(t)=exp(-((t-.040)/.004)^2/2), t>=0, in volume-velocity units.
Exact finite causal forcing Fourier integral retains Gaussian tails and all
spatial modes. No damping, window taper, modal threshold or level fitting.
"""
from __future__ import annotations
import numpy as np
from scipy.special import erfcx


def finite_gaussian_transform(angular_frequency, record_s=.25, *, center_s=.04, sigma_s=.004):
    a = np.asarray(angular_frequency, float)
    T, mu, sig = record_s, center_s, sigma_s
    if not 0 < mu < T or sig <= 0 or not np.isfinite(a).all():
        raise ValueError("invalid physical Gaussian drive")
    full = np.sqrt(2*np.pi)*sig*np.exp(-.5*(sig*a)**2+1j*a*mu)
    left = sig*np.sqrt(np.pi/2)*np.exp(-.5*(mu/sig)**2)*erfcx(mu/(np.sqrt(2)*sig)+1j*a*sig/np.sqrt(2))
    right = sig*np.sqrt(np.pi/2)*np.exp(-.5*((T-mu)/sig)**2+1j*a*T)*erfcx((T-mu)/(np.sqrt(2)*sig)-1j*a*sig/np.sqrt(2))
    return full-left-right


def exact_finite_gaussian_pressure_modes(lam, coupling, *, record_s=.25,
                                         frequencies_hz=(40.,80.), center_s=.04, sigma_s=.004,
                                         c_m_s=343.2, rho_kg_m3=1.2):
    lam, cp = np.asarray(lam, float), np.asarray(coupling, float)
    if lam.shape != cp.shape or np.min(lam) < 0 or not np.isfinite(cp).all():
        raise ValueError("all physical eigenmodes required")
    omega = np.sqrt(lam)
    Qmode = finite_gaussian_transform(omega, record_s, center_s=center_s, sigma_s=sigma_s)
    out = np.empty((len(frequencies_hz), len(lam)), complex)
    for i, f in enumerate(frequencies_hz):
        a = 2*np.pi*f
        Q = finite_gaussian_transform(a, record_s, center_s=center_s, sigma_s=sigma_s)
        plus = (np.exp(1j*(a+omega)*record_s)*Qmode.conj()-Q)/(1j*(a+omega))
        b = a-omega
        near = abs(b) < 1e-7
        minus = np.empty_like(plus)
        minus[~near] = (np.exp(1j*b[~near]*record_s)*Qmode[~near]-Q)/(1j*b[~near])
        q0 = np.exp(-.5*(center_s/sigma_s)**2)
        qT = np.exp(-.5*((record_s-center_s)/sigma_s)**2)
        moment = (center_s+1j*sigma_s**2*a)*Q-sigma_s**2*(qT*np.exp(1j*a*record_s)-q0)
        minus[near] = record_s*Q-moment
        out[i] = rho_kg_m3*c_m_s**2*cp*.5*(plus+minus)/Q
    if not np.isfinite(out).all():
        raise ValueError("physical finite pulse transfer nonfinite")
    return out


def all_mode_newmark_gaussian_trace(lam, coupling, dt, nt, *, center_s=.04, sigma_s=.004,
                                    c_m_s=343.2, rho_kg_m3=1.2):
    lam, cp = np.asarray(lam, float), np.asarray(coupling, float)
    t = np.arange(nt)*dt
    q = np.exp(-.5*((t-center_s)/sigma_s)**2)
    u, v = np.zeros_like(cp), np.zeros_like(cp)
    force = c_m_s**2*cp
    acceleration = force*q[0]
    denominator = 1.+dt*dt/4.*lam
    phi = np.empty(nt)
    phi[0] = 0.
    for n in range(1, nt):
        prediction = u+dt*v+dt*dt/4.*acceleration
        next_acceleration = (force*q[n]-lam*prediction)/denominator
        u = prediction+dt*dt/4.*next_acceleration
        v += dt/2.*(acceleration+next_acceleration)
        acceleration = next_acceleration
        phi[n] = u.sum()
    p = rho_kg_m3*np.gradient(phi, dt, edge_order=2)
    E = np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t[None,:])
    # Physical finite-time integrals use trapezoidal quadrature. Counting
    # both endpoint samples with full dt weight would add a first-order
    # record-boundary bias even for a smooth resolved signal.
    weights = np.ones(nt)
    weights[[0,-1]] = .5
    return (E @ (weights*p))/(E @ (weights*q)), phi, p, q


def all_mode_midpoint_gaussian_trace(lam, coupling, steps, *, record_s=.25,
                                     center_s=.04, sigma_s=.004,
                                     c_m_s=343.2, rho_kg_m3=1.2):
    """Actual per-step physical drive, all modes, exact full record endpoint.

    Pressure is rho*phi_t from the evolved velocity at temporal midpoints;
    no pressure differentiation across a truncated record boundary.
    """
    lam, cp = np.asarray(lam, float), np.asarray(coupling, float)
    if lam.shape != cp.shape or min(lam) < 0 or steps < 2:
        raise ValueError("invalid complete midpoint spectrum")
    dt = record_s/steps
    times = (np.arange(steps)+.5)*dt
    q = np.exp(-.5*((times-center_s)/sigma_s)**2)
    u, v = np.zeros_like(cp), np.zeros_like(cp)
    a = dt*dt*lam/4
    inverse = 1./(1.+a)
    force = dt*dt*c_m_s**2*cp/2
    p = np.empty(steps)
    for n in range(steps):
        u1 = ((1.-a)*u+dt*v+force*q[n])*inverse
        v1 = 2.*(u1-u)/dt-v
        p[n] = rho_kg_m3*.5*(v+v1).sum()
        u, v = u1, v1
    E = np.exp(2j*np.pi*np.array([40.,80.])[:,None]*times[None,:])
    return (E @ p)/(E @ q), times, p, q
