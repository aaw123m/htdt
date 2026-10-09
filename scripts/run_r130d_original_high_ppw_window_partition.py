#!/usr/bin/env python3
"""Untouched original PFFDTD eight-node point/point finite-window signed DTFT audit."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import h5py

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,recombine_pffdtd_receiver_traces,
    finite_record_pressure_transfer)
from run_r130d_original_point_quadratic_pffdtd import file_hash

SCHEMA="htdt.r130d.original-native-pffdtd-high-ppw-window-partition-plan-1"
PPW=(28,32,36,40,44)
ORIG_SHA="9a0c7c4cf6080fdd75ce42a4e4a4a08256973b1d8c73724974073782dd9aeb34"
BANDS=((0,0.05),(0.05,0.15),(0.15,0.25))

def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["original_pffdtd"]["frozen_ppw"]!=list(PPW)
        or p["original_pffdtd"]["original_native_high_ppw_evidence_sha256"]!=ORIG_SHA
        or p["original_pffdtd"]["source_control_evidence"]!="benchmarks/acoustics/r130d_original_point_quadratic_pffdtd_evidence_2026-10-09.json"
        or p["original_pffdtd"]["exact_source_xyz_m"]!=[1.5,2,2]
        or p["original_pffdtd"]["exact_receiver_xyz_m"]!=[2.5,2,2]
        or p["original_pffdtd"]["frequencies_hz"]!=[40,80]
        or p["window"]["physical_intervals_s"]!=[list(b) for b in BANDS]
        or p["resource_limits"]["allowed_segment_count"]!=3
        or p["resource_limits"]["max_cases"]!=5
        or p["authority"]["original_point_q0_state"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("frozen native original PFFDTD time partition plan changed")
    return p

def complex_pairs(values):
    z=np.asarray(values,dtype=complex)
    if z.shape!=(2,) or not np.all(np.isfinite(z)):
        raise ValueError("missing original two coherent signed spectral values")
    return [[float(v.real),float(v.imag)] for v in z]

def partition_one(pressure,dt,frequencies,original_full):
    p=np.asarray(pressure,dtype=np.float64)
    f=np.asarray(frequencies,dtype=float)
    if p.ndim!=1 or p.size<10 or p.size>2000 or not np.all(np.isfinite(p)):
        raise ValueError("original pressure record missing or illegal")
    n=np.arange(p.size)
    t=dt*n
    if (t[-1]>=0.25+1e-10 or dt<=0 or not np.all(np.isfinite(t))):
        raise ValueError("original PFFDTD full record timing illegal")
    kernel=np.exp(2j*np.pi*f[:,None]*t[None,:])
    spectrum=kernel@p # the source q0 denominator exactly 1 after dt cancels
    if not np.allclose(spectrum,original_full,rtol=1e-9,atol=2e-8):
        raise ValueError("original full transfer does not equal decomposed no-taper sum")
    masks=[(t>=a)&(t<b) for a,b in BANDS]
    occupancy=np.sum(np.stack(masks),axis=0)
    if np.any(occupancy!=1):raise ValueError("half-open exact partition did not cover native waveform")
    contributions=[kernel[:,mask]@p[mask] for mask in masks]
    if not np.allclose(np.sum(contributions,axis=0),spectrum,rtol=1e-10,atol=2e-8):
        raise ValueError("frozen 3-band partition failed full complex-sum reconstruction")
    return {
        "full_original_40_80_complex":complex_pairs(spectrum),
        "signed_partitions_40_80_complex":[complex_pairs(c) for c in contributions],
        "segments":[{"start_s":a,"end_s":b,"sample_count":int(mask.sum()),
                     "pressure_rms_pa":float(np.sqrt(np.mean(p[mask]**2))),
                     "pressure_max_absolute_pa":float(np.max(np.abs(p[mask])))}
                    for (a,b),mask in zip(BANDS,masks)],
        "full_pressure_rms_pa":float(np.sqrt(np.mean(p**2))),
        "full_pressure_last_sample_pa":float(p[-1]),
        "record_samples":int(p.size),
        "native_dt_s":float(dt),
        "exact_complex_partition_conservation":True,
    }

def complex_from_pairs(p):
    a=np.asarray(p,dtype=float)
    if a.shape!=(2,2):raise ValueError("invalid signed complex bin pair")
    return a[:,0]+1j*a[:,1]

def adjacent_analysis(levels):
    out=[]
    for first,last in zip(levels,levels[1:]):
        d0=complex_from_pairs(first["time_components"]["full_original_40_80_complex"])
        d1=complex_from_pairs(last["time_components"]["full_original_40_80_complex"])
        delta=d0-d1
        fullnorm=float(np.linalg.norm(delta))
        diffs=[]
        summed=np.zeros(2,dtype=complex)
        for ib in range(3):
            x=complex_from_pairs(first["time_components"]["signed_partitions_40_80_complex"][ib])
            y=complex_from_pairs(last["time_components"]["signed_partitions_40_80_complex"][ib])
            delta_band=x-y
            summed+=delta_band
            diffs.append({"segment_s":list(BANDS[ib]),
                          "complex_delta_40_80":complex_pairs(delta_band),
                          "absolute_two_bin_delta_norm":float(np.linalg.norm(delta_band)),
                          "relative_to_full_delta_norm":float(np.linalg.norm(delta_band)/max(fullnorm,1e-12))})
        if not np.allclose(summed,delta,rtol=1e-10,atol=2e-8):
            raise ValueError("adjacent source-preserved full mismatch partition conservation")
        out.append({
            "coarse_ppw":first["ppw"],"fine_ppw":last["ppw"],
            "full_signed_delta_40_80":complex_pairs(delta),
            "full_delta_norm":fullnorm,
            "segments":diffs,
            "partitions_can_cancel_complexly":True,
            "original_8node_full_record_only_is_qualification_gate":True})
    return out

def find_native_original_cases(p,raw_root,original_record,control_record):
    cases={int(x["ppw"]):x for x in control_record["actual_native_wave_cases"]}
    native={int(x["ppw"]):x for x in original_record["levels"] if int(x["ppw"]) in PPW}
    if set(cases)!=set(PPW) or set(native)!=set(PPW):
        raise ValueError("all five exact original native source controls are required")
    originals={}
    for f in raw_root.rglob("comms_out.h5"):
        sha=file_hash(f)
        found=[ppw for ppw in PPW if sha==p["original_pffdtd"]["original_8_node_comms_sha_by_ppw"][str(ppw)]]
        if len(found)==1:
            ppw=found[0]
            if ppw in originals:raise ValueError("same original PPW duplicate HDF5")
            originals[ppw]=f.parent
    if set(originals)!=set(PPW):
        raise ValueError("original native source/receiver raw HDF5 missing")
    return [(ppw,originals[ppw],native[ppw],cases[ppw]) for ppw in PPW]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-high-ppw-evidence",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    orig=a.original_high_ppw_evidence.read_bytes()
    if hashlib.sha256(orig).hexdigest()!=ORIG_SHA:
        raise ValueError("original eight-node high-PPW raw evidence SHA changed")
    original=json.loads(orig)
    if original["solver_sha"]!=p["original_pffdtd"]["upstream_sha"]:
        raise ValueError("original pinned solver sha changed")
    prior=json.loads((ROOT/p["original_pffdtd"]["source_control_evidence"]).read_text(encoding="utf-8"))
    rows=find_native_original_cases(p,a.original_sims_root,original,prior)
    result={"schema_version":"htdt.r130d.original-native-pffdtd-high-ppw-window-partition-evidence-1",
            "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
            "preregistered_plan":p,
            "original_native_high_ppw_external_sha256":ORIG_SHA,
            "all_five_original_point_point_waves_unchanged":True,
            "levels":[]}
    for ppw,sim,native,ctrl in rows:
        with h5py.File(sim/"comms_out.h5","r") as h:
            alpha=np.asarray(h["out_alpha"][...],dtype=float)
            nt=int(h["Nt"][()])
            in_sigs=np.asarray(h["in_sigs"][...],dtype=float)
            if (alpha.shape!=(1,8) or in_sigs.shape!=(8,nt)
                or not np.array_equal(in_sigs[:,1:],np.zeros((8,nt-1)))
                or int(h["diff"][()])!=0 or
                abs(float(alpha.sum())-1)>1e-12):
                raise ValueError("original temporal q0/native point stencil contract changed")
        with h5py.File(sim/"sim_consts.h5","r") as h:
            dt=float(h["Ts"][()])
            spacing=float(h["h"][()])
        if (nt!=int(native["time_step_count"])
            or abs(dt-float(native["time_step_s"]))>1e-12
            or abs(spacing-float(native["grid_spacing_m"]))>1e-12
            or nt>p["resource_limits"]["max_samples_per_case"]):
            raise ValueError("original native time sampling or spatial PPW drifted")
        with h5py.File(sim/"sim_outs.h5","r") as h:
            potential_node_traces=np.asarray(h["u_out"][...],dtype=float)
        if (potential_node_traces.shape!=(8,nt)
            or (sim/"sim_outs.h5").stat().st_size>p["resource_limits"]["max_original_full_trace_bytes"]):
            raise ValueError("original native full PFFDTD trace wrong shape or oversized")
        phi=recombine_pffdtd_receiver_traces(
            potential_node_traces,alpha,receiver_count=1,nt=nt)[0]
        pressure=pffdtd_velocity_potential_to_pressure_trace(
            phi,time_step_s=dt,density_kg_m3=p["original_pffdtd"]["density_kg_m3"])
        source_q0=np.zeros(nt,dtype=float);source_q0[0]=1.0
        full=finite_record_pressure_transfer(
            pressure,source_q0,time_step_s=dt,
            frequency_hz=np.asarray(p["original_pffdtd"]["frequencies_hz"],dtype=float))
        original_control=complex_from_pairs(ctrl["unmodified_original_transfer_pa_per_m3_s"])
        high_native_control=complex_from_pairs(native["transfer_pa_per_m3_s"])
        if not np.allclose(full,original_control,rtol=1e-9,atol=2e-8):
            raise ValueError("original exact native 8-node control P_T/Q_T differs from frozen evidence")
        if not np.allclose(full,high_native_control,rtol=1e-9,atol=2e-8):
            raise ValueError("original highPPW raw native P_T/Q_T differs from prior evidence")
        components=partition_one(pressure,dt,p["original_pffdtd"]["frequencies_hz"],full)
        result["levels"].append({
            "ppw":ppw,
            "original_8node_comms_sha256":file_hash(sim/"comms_out.h5"),
            "original_8node_native_output_sha256":file_hash(sim/"sim_outs.h5"),
            "original_solver_voxel_sha256":file_hash(sim/"vox_out.h5"),
            "full_original_control_signed_complex":complex_pairs(original_control),
            "time_components":components,
            "source_temporal_unit_q0_and_original_native_points_unchanged":True})
        part=a.output.with_name(a.output.stem+"_partial.json")
        part.parent.mkdir(parents=True,exist_ok=True)
        part.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ORIGINAL_FULL_NATIVE_TIME_GATE",ppw,
              "early/mid/late RMS", [round(z["pressure_rms_pa"],8) for z in components["segments"]],
              "full original",components["full_original_40_80_complex"],flush=True)
    result["adjacent_native_8node_original_signed_difference_contributions"]=adjacent_analysis(result["levels"])
    result.update({
        "canonical_original_full_window_point_q0":"SELF_CONVERGENCE_FAILED",
        "modified_source_receiver_waveform_or_boundary":False,
        "diagnostic_partition_is_not_new_qualification":True,
        "physical_validation":"NOT_VALIDATED","production_ready":False})
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    for pair in result["adjacent_native_8node_original_signed_difference_contributions"]:
        print("ORIGINAL_NATIVE_ADJACENT_TIME_GATE",pair["coarse_ppw"],pair["fine_ppw"],
              "total",pair["full_delta_norm"],
              "early/mid/late ratios",[round(z["relative_to_full_delta_norm"],5)
                    for z in pair["segments"]],flush=True)
    print("AUDIT_COMPLETE all original 8-node data preserved, canonical still FAIL",flush=True)

if __name__=="__main__":
    main()
