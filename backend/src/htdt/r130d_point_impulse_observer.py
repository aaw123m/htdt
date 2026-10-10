"""Independent analytic free-space counterexample for an impulse observer.

This is NOT an R130D room solver and never substitutes an acceptance score.
For a radial spatial Fourier ball |k|<=K, solve the 3D wave equation exactly.
The potential converges distributionally to delta(t-r/c)/(4*pi*r), while
its pointwise value at a sharp record endpoint has nondecaying oscillations.
Integrating differentiated potential over a rectangular time record leaves
that endpoint term. Stable propagation alone does not prove this observer
converges for an impulsive point source. Other discretizations may converge;
the counterexample establishes insufficiency, not general impossibility.
"""
from __future__ import annotations
import numpy as np
from scipy.special import sici


def radial_bandlimited_point_green(time_s, distance_m, cutoff_rad_m, *, c_m_s=343.2):
    t = np.asarray(time_s, float)
    r, K, c = float(distance_m), float(cutoff_rad_m), float(c_m_s)
    if r <= 0 or K <= 0 or c <= 0 or not np.isfinite(t).all() or np.any(t < 0):
        raise ValueError("causal positive point Green arguments required")
    return c*K/(4*np.pi**2*r) * (
        np.sinc(K*(r-c*t)/np.pi) - np.sinc(K*(r+c*t)/np.pi))


def _primitive(u, K, a):
    # Integral sin(K*u)*exp(i*a*u)/u; common logarithmic Ci singularities
    # cancel, and the imaginary part has the exact finite limit at u=0.
    u = np.asarray(u, float)
    real = .5*(sici((K+a)*u)[0] + sici((K-a)*u)[0])
    ua = abs(u)
    safe = np.where(ua == 0, 1., ua)
    imaginary = .5*(sici(abs(K-a)*safe)[1] - sici(abs(K+a)*safe)[1])
    imaginary = np.where(ua == 0, .5*np.log(abs((K-a)/(K+a))), imaginary)
    return real + 1j*imaginary


def rectangular_pressure_transfer(distance_m, cutoff_rad_m, record_s, frequency_hz,
                                   *, c_m_s=343.2, rho_kg_m3=1.2):
    r, K, T = float(distance_m), float(cutoff_rad_m), float(record_s)
    w, c, rho = 2*np.pi*float(frequency_hz), c_m_s, rho_kg_m3
    if r <= 0 or T <= r/c or K <= abs(w/c) or frequency_hz <= 0:
        raise ValueError("complete direct arrival and cutoff above scoring bin required")
    a = w/c
    integral_green = (
        np.exp(1j*a*r)*(_primitive(r,K,-a)-_primitive(r-c*T,K,-a))
        -np.exp(-1j*a*r)*(_primitive(r+c*T,K,a)-_primitive(r,K,a))
    ) / (4*np.pi**2*r)
    boundary = rho*radial_bandlimited_point_green(T,r,K,c_m_s=c)*np.exp(1j*w*T)
    bulk = -1j*w*rho*integral_green
    exact_distribution = -1j*w*rho*np.exp(1j*a*r)/(4*np.pi*r)
    return {"bulk": complex(bulk), "endpoint": complex(boundary),
            "rectangular_pressure": complex(bulk+boundary),
            "distributional_direct_pressure": complex(exact_distribution)}
