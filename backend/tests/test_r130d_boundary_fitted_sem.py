"""Independent polynomial, weak roof and complete modal physical checks."""
import numpy as np
import pytest
from htdt.r130d_boundary_fitted_sem import (
    gll_rule, build_spectral_axis, axis_functional, build_boundary_fitted_sem,
    original_eight_functional, all_mass_normalized_modes)


@pytest.mark.parametrize("p", [2, 4, 6])
def test_gll_polynomial_integration_and_derivative(p):
    x, w, D = gll_rule(p)
    for k in range(2*p):
        expected = 0. if k % 2 else 2. / (k+1)
        assert abs(w @ x**k - expected) < 2e-13
    for k in range(p+1):
        exact = np.zeros_like(x) if k == 0 else k*x**(k-1)
        np.testing.assert_allclose(D @ x**k, exact, atol=2e-13)


def test_conforming_physical_mass_neumann_and_affine_energy():
    f = build_boundary_fitted_sem(.5)
    assert abs(f.x.mass.sum() * f.yz_mass.sum() - 56.) < 1e-10
    assert min(f.x.mass) > 0 and min(f.yz_mass) > 0
    np.testing.assert_allclose(f.yz_stiffness @ np.ones(len(f.yz_mass)), 0., atol=1e-8)
    np.testing.assert_allclose(f.x.stiffness @ np.ones(len(f.x.mass)), 0., atol=1e-8)
    c2 = 343.2**2
    y, z = f.yz_positions.T
    for u, expected in [(y, 14.*c2), (z, 14.*c2), (y-.25*z, 14.*1.0625*c2)]:
        assert abs(u @ (f.yz_stiffness @ u) / expected - 1.) < 1e-12
    roof = np.arange(1, len(f.y.nodes)-1)*len(f.eta.nodes)+len(f.eta.nodes)-1
    np.testing.assert_allclose((f.yz_stiffness @ (y-.25*z))[roof], 0., atol=2e-8)


def test_original_eight_physical_functionals_reproduce_coordinates():
    f = build_boundary_fitted_sem(.5)
    xyz = np.array([[x, y, z] for x in [1.4, 1.6] for y in [1.9, 2.1] for z in [1.9, 2.1]])
    w = np.array([a*b*c for a in [.4, .6] for b in [.3, .7] for c in [.8, .2]])
    sx, sy, proof = original_eight_functional(f, xyz, w)
    np.testing.assert_allclose(np.r_[sx @ f.x.nodes, sy @ f.yz_positions], w @ xyz, atol=1e-13)
    assert proof["moment_error_m"] < 1e-13
    assert proof["tensor_factorization_error"] < 1e-13
    # Spectral nodal interpolation reproduces degree-four physical x exactly.
    ex = axis_functional(f.x, 1.53)
    assert abs(ex @ f.x.nodes**4 - 1.53**4) < 1e-12


def test_all_eigenmodes_independent_rectangular_neumann_x_frequency():
    x = build_spectral_axis(4., 4, 4)
    lam, V, proof = all_mass_normalized_modes(x.mass, x.stiffness)
    expected = (343.2 * np.pi / 4.)**2
    assert len(lam) == len(x.nodes)
    assert abs(lam[1]/expected-1.) < 2e-8
    np.testing.assert_allclose(V[:,0], np.full(len(x.nodes), .5), atol=1e-14)
    assert proof["max_eigenpair_residual"] < 1e-7


def test_outside_physical_point_is_rejected():
    x = build_spectral_axis(4., 2, 4)
    with pytest.raises(ValueError):
        axis_functional(x, 4.01)
