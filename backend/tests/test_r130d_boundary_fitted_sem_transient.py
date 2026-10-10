"""Brute time traces verify two modal observers independently of DTFT formulas."""
from pathlib import Path
import sys
import numpy as np
from scipy.linalg import expm

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import entire_original_finite_record_signed_modes
from htdt.r130d_exact_semidiscrete_causal_q0 import exact_continuous_modal_delta_impulse_signed_original_250ms
from htdt.r130d_boundary_fitted_sem import build_spectral_axis, axis_functional, all_mass_normalized_modes


def test_exact_time_formula_against_matrix_exponential_pressure_trace():
    lam = np.array([0., 150.**2, 16000.**2, 40000.**2])
    coupling = np.array([.3, -.12, .06, -.008])
    dt, nt, c, rho = .00017, 149, 343.2, 1.2
    phi = np.zeros((nt, len(lam)))
    for j, value in enumerate(lam):
        transition = expm(np.array([[0., 1.], [-value, 0.]])*dt)
        state = np.array([0., c*c*dt*coupling[j]])
        for n in range(nt):
            phi[n,j] = state[0]
            state = transition @ state
    pressure = rho*np.gradient(phi, dt, axis=0, edge_order=2)
    transform = np.exp(2j*np.pi*np.array([40.,80.])[:,None]*np.arange(nt)[None,:]*dt)
    expected = transform @ pressure
    actual = exact_continuous_modal_delta_impulse_signed_original_250ms(lam, coupling, dt, nt)
    np.testing.assert_allclose(actual, expected, atol=2e-8, rtol=2e-10)


def test_newmark_formula_against_full_state_implicit_recurrence():
    axis = build_spectral_axis(4., 3, 4)
    lam, V, _ = all_mass_normalized_modes(axis.mass, axis.stiffness)
    s, r = axis_functional(axis, 1.5), axis_functional(axis, 2.5)
    coupling = (s @ V)*(r @ V)
    dt, nt, c, rho = .00021, 139, 343.2, 1.2
    M = np.diag(axis.mass)
    K = axis.stiffness.toarray()
    A = M + dt*dt/4*K
    B = 2*M-dt*dt/2*K
    phi = np.zeros((nt, len(axis.mass)))
    phi[1] = np.linalg.solve(A, (c*dt)**2*s)
    for n in range(1, nt-1):
        phi[n+1] = np.linalg.solve(A, B @ phi[n]-A @ phi[n-1])
    trace = rho*np.gradient(phi @ r, dt, edge_order=2)
    expected = np.exp(2j*np.pi*np.array([40.,80.])[:,None]*np.arange(nt)[None,:]*dt) @ trace
    actual = entire_original_finite_record_signed_modes(lam, (c*dt)**2*coupling/(1.+dt*dt*lam/4), dt, nt, rho).sum(axis=1)
    np.testing.assert_allclose(actual, expected, atol=2e-8, rtol=2e-10)
