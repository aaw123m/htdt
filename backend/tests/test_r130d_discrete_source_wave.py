"""Actual arbitrary sampled source injection and fail-closed interpolation.

Do not claim original PFFDTD impulse production gate is solved.
"""
from __future__ import annotations
import numpy as np
import pytest

from htdt.r130d_embedded_neumann_fv import (
    build_sloped_embedded_neumann,smooth_source_complex_transfer,
    discrete_source_complex_transfer,
)


@pytest.mark.parametrize("n",[6,8,12])
def test_actual_gaussian_contract_matches_previously_frozen_solver(n):
    system=build_sloped_embedded_neumann(n)
    dt=.00025
    t=(np.arange(1000)+.5)*dt
    q=np.exp(-.5*((t-.012)/.003)**2)
    original=smooth_source_complex_transfer(system)
    generic=discrete_source_complex_transfer(
        system,q,frequency_hz=(40,80),time_step_s=dt)
    np.testing.assert_allclose(generic,original,rtol=2e-12,atol=1e-10)


@pytest.mark.parametrize("n",[6,8,12])
def test_exact_unit_discrete_impulse_is_not_substituted_by_gaussian(n):
    system=build_sloped_embedded_neumann(n)
    q=np.zeros(1000);q[0]=1.
    discrete=discrete_source_complex_transfer(
        system,q,time_step_s=.00025)
    smoothed=smooth_source_complex_transfer(system)
    assert discrete.shape==(2,)
    assert np.all(np.isfinite(discrete))
    assert not np.allclose(discrete,smoothed,rtol=.005,atol=.005)


def test_exact_q_amplitude_cancels_and_midpoint_time_origin_correct():
    system=build_sloped_embedded_neumann(8)
    q=np.zeros(1000);q[0]=1.
    a=discrete_source_complex_transfer(system,q)
    q*=2
    b=discrete_source_complex_transfer(system,q)
    np.testing.assert_allclose(a,b,rtol=1e-12,atol=1e-10)


@pytest.mark.parametrize("invalid",[
    [],
    [0]*1000,
    [1,float('nan')]+[0]*998,
    [float('inf'),1]+[0]*998,
    [[1]*10]*10,
])
def test_reject_invalid_sampled_waveform(invalid):
    system=build_sloped_embedded_neumann(6)
    with pytest.raises(ValueError):
        discrete_source_complex_transfer(system,np.asarray(invalid))


@pytest.mark.parametrize("freq",[(-1,40),(0,80),(3000,80),(float('nan'),80)])
def test_reject_bad_frequency_bins(freq):
    system=build_sloped_embedded_neumann(6)
    q=np.zeros(1000);q[0]=1
    with pytest.raises(ValueError):
        discrete_source_complex_transfer(system,q,frequency_hz=freq)


def test_missing_observation_source_spectrum_fails_not_bypassed():
    s=build_sloped_embedded_neumann(6)
    q=np.zeros(1000)
    # Midpoint times differ by exactly 0.125 s: a 40-Hz coherent
    # integer-cycle cancellation, without violating the 2-kHz Nyquist.
    q[0]=1;q[500]=-1
    with pytest.raises(ValueError,match="source energy absent"):
        discrete_source_complex_transfer(s,q,frequency_hz=(40.,44.))
