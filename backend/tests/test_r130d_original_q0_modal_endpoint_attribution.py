"""Strict tests for exact full-modal q0 decomposition (not new 5-grid run)."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest

from htdt.acoustic_pffdtd_adapter import (
    finite_record_pressure_transfer, pffdtd_velocity_potential_to_pressure_trace)
from htdt.r130d_original_q0_modal_endpoint_attribution import (
    BAND_TITLES,ENDPOINT_NAMES,
    original_q0_full_modal_endpoint_terms,
    partition_original_q0_hybrid_allmodes,signed_real_projection_on_full_delta)

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from run_r130d_native_exact_roof_full_xy_z_modal_q0 import (
    entire_original_finite_record_signed_modes)
import run_r130d_original_q0_hybrid_modal_endpoint_attribution as runner
from run_r130d_original_q0_hybrid_modal_endpoint_attribution import validate_plan
PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_plan_2026-10-10.json"


@pytest.mark.parametrize("dt,nt",( (.00020599032818587005,1214),(.000130,1781),(.000170,1600)))
def test_exact_newmark_all_modes_finite_record_pressure_endpoints_independent_time_wave(dt,nt):
    lam=np.array([0.,(2*np.pi*39)**2,(2*np.pi*78)**2,
                  (2*np.pi*400)**2,(2*np.pi*950)**2,(2*np.pi*3300)**2])
    kick=np.array([.002,-.0003,.004,.0001,-.0004,.00007])
    theta=2*np.arctan(.5*dt*np.sqrt(lam))
    ii=np.arange(nt)
    # Independent directly sampled real potential, then original pressure
    # conversion and original full rectangular signed Fourier.
    phi=np.zeros(nt,dtype=float)
    for j in range(len(lam)):
        if lam[j]==0:
            factor=ii.astype(float)
        else:
            factor=np.sin(ii*theta[j])/np.sin(theta[j])
        phi+=kick[j]*factor
    q=np.zeros(nt);q[0]=1.
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=1.2)
    target=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.array([40.,80.]))
    pieces=original_q0_full_modal_endpoint_terms(lam,kick,dt,nt)
    np.testing.assert_allclose(pieces.sum(axis=(1,2)),target,rtol=2e-9,atol=1e-8)
    earlier=entire_original_finite_record_signed_modes(lam,kick,dt,nt,1.2).sum(axis=1)
    np.testing.assert_allclose(pieces.sum(axis=(1,2)),earlier,rtol=2e-11,atol=1e-8)
    # Endpoint terms checked against direct ORIGINAL pressure samples,
    # not against the very same modal algebra.
    for fi,f in enumerate((40.,80.)):
        start=pressure[0]
        middle=sum(pressure[1:-1]*np.exp(2j*np.pi*f*dt*np.arange(1,nt-1)))
        end=pressure[-1]*np.exp(2j*np.pi*f*dt*(nt-1))
        np.testing.assert_allclose(pieces[fi,:, :].sum(axis=1),
                                  [start,middle,end],rtol=2e-9,atol=1e-8)


def test_synthetic_modal_bands_endpoints_nyquist_all_modes_preserved():
    lam=(2*np.pi*np.array([0.,42.,95.,110.,199.,300.,650.,900.,
                           1510.,1800.,3000.,3500.,15000.]))**2
    kick=np.linspace(.001,-.001,len(lam))
    p=partition_original_q0_hybrid_allmodes(lam,kick,.00018,1388)
    assert p["mode_count"]==len(lam)
    assert [b["band_name"] for b in p["bands"]]==list(BAND_TITLES)
    assert len(ENDPOINT_NAMES)==3
    assert sum(b["retained_mode_count"] for b in p["bands"])==len(lam)
    assert p["native_nyquist_or_below_mode_count"]+p["native_above_nyquist_mode_count"]==len(lam)
    np.testing.assert_allclose(
        sum((a["signed_total_40_80"] for a in p["bands"]),np.zeros(2,complex)),
        p["full_signed_40_80"],rtol=1e-12,atol=1e-8)
    np.testing.assert_allclose(p["endpoint_signed_40_80"].sum(axis=1),
                               p["full_signed_40_80"],rtol=1e-12,atol=1e-8)
    assert p["not_a_new_acceptance_or_filtered_solution"]


def test_signed_projection_handles_cancellations_not_abs_weighted_positive():
    coarse=np.array([2+3j,-2+5j])
    fine=np.array([1+1j,-1+2j])
    cp=[np.array([5+3j,2-2j]),coarse-np.array([5+3j,2-2j])]
    fp=[np.array([5+2j,1-1j]),fine-np.array([5+2j,1-1j])]
    result=signed_real_projection_on_full_delta(coarse,fine,cp,fp)
    np.testing.assert_allclose(result.sum(axis=0),[1,1],atol=1e-14)
    assert (result<0).any() or (result>1).any()
    with pytest.raises(ValueError,match="modal 40/80"):
        signed_real_projection_on_full_delta(coarse,fine,cp,fp[:1])


@pytest.mark.parametrize("kind,key,value",[
    ("original","ppw",[28,32,36,40]),
    ("original","signed_bins_hz",[40]),
    ("original","record_length_s",.20),
    ("method","no_high_mode_cut_no_source_smoothing_no_extra_window_no_amplitude_phase_fitting",False)])
def test_fixed_plan_cannot_change_original_q0_or_scope(kind,key,value):
    plan=json.loads(PLAN.read_text(encoding="utf8"))
    plan[kind][key]=value
    with pytest.raises(ValueError,match="original q0 modal"):
        validate_plan(plan)


def test_preregistered_diagnostic_not_release_authority():
    plan=validate_plan(json.loads(PLAN.read_text(encoding="utf8")))
    assert plan["caps"]["new_GitHub_Actions_runs"]==0
    assert plan["authority"]["product"]=="NO_GO"
    assert plan["method"]["report_all_bands_all_grids_both_signed_bins_all_endpoint_components"]


def test_precommit_push_gate_fails_closed_without_remote_branch(monkeypatch):
    # Does not invoke the experiment or network. Simulated missing GitHub ref
    # must be rejected even when the local commit exists.
    class FakeResult:
        def __init__(self,code,stdout=b""):
            self.returncode=code
            self.stdout=stdout
            self.stderr=b""
    local_head=b"f877413bc9af6e9cafb710cb4bf2a61fb987f086\n"
    expected=PLAN.read_bytes().replace(b"\r\n",b"\n")
    def fake(command,**kwargs):
        key=command[3]
        if key=="cat-file":return FakeResult(0)
        if key=="merge-base":return FakeResult(1)
        if key=="show":return FakeResult(0,expected)
        if key=="rev-parse":return FakeResult(0,local_head)
        if key=="ls-remote":return FakeResult(0,
            local_head.strip()+b"\trefs/heads/feat/r130d-embedded-neumann-fv-20261009\n")
        raise AssertionError(f"unexpected git command: {command}")
    monkeypatch.setattr(runner.subprocess,"run",fake)
    with pytest.raises(RuntimeError,match="PREREGISTRATION_NOT_PUSHED"):
        runner.require_prospective_plan_pushed()
