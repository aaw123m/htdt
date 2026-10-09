#!/usr/bin/env python3
"""Experimental native PFFDTD polynomially exact point delta; original q0 unchanged.

The upstream simulator and its native 8-node production model are not patched.
A copy of each independently recorded exact same-rigid-room original setup
gets a 27-node numerical point discretization in comms_out.h5. Experimental
results can NEVER qualify the original 8-node PFFDTD or physical BRAS test.
"""
from __future__ import annotations
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import sys
import time
import numpy as np
import h5py

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.acoustic_pffdtd_adapter import (
    pffdtd_velocity_potential_to_pressure_trace,
    recombine_pffdtd_receiver_traces,
    finite_record_pressure_transfer)
from htdt.pffdtd_boundary_halo import apply_boundary_halo_separation
from htdt.r130d_general3d_validation import compare_complex_transfer

PPW=(28,32,36,40,44)
ORIG_SHA="9a0c7c4cf6080fdd75ce42a4e4a4a08256973b1d8c73724974073782dd9aeb34"
PIN="aa319f6c86517cb95aabfae8656277da62c3ead5"
PLAN_SCHEMA="htdt.r130d.original-point-quadratic-pffdtd-plan-1"
def file_hash(p:Path)->str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda:f.read(1<<20),b""):
            h.update(chunk)
    return h.hexdigest()

def validate_plan(p):
    if (p.get("schema_version")!=PLAN_SCHEMA
        or p["identities"]["pinned_upstream"]!=PIN
        or p["identities"]["original_high_ppw_evidence_sha256"]!=ORIG_SHA
        or p["frozen_ppw"]!=list(PPW)
        or p["source_fixture"]["source_xyz_m"]!=[1.5,2,2]
        or p["source_fixture"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["source_fixture"]["frequency_hz"]!=[40,80]
        or p["source_fixture"]["duration_s"]!=.25
        or p["numerical_delta"]["axis_node_offsets"]!=[-1,0,1]
        or p["numerical_delta"]["mode"]!="tensor_quadratic_lagrange_3x3x3_exact_point"
        or not p["numerical_delta"]["allow_negative_weights"]
        or p["comparisons"]["original_numeric_thresholds"]!={"complex_rms_relative_max":.2,"magnitude_max_relative":.25,"phase_max_deg":15}
        or p["resource_limits"]["max_wave_cases"]!=5
        or p["authority"]["canonical_original_8_node_point_point"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["production_ready"] is not False):
        raise ValueError("prospective original physical point PFFDTD experiment changed")
    return p

def q2_stencil(axes,point,*,boundary_nodes=(),abc_nodes=()):
    axes=[np.asarray(x,dtype=np.float64) for x in axes]
    xyz=np.asarray(point,dtype=np.float64)
    if len(axes)!=3 or xyz.shape!=(3,) or not np.all(np.isfinite(xyz)):
        raise ValueError("point coordinate/axes must be finite three-space")
    dims=tuple(len(v) for v in axes)
    centers=[]; fractions=[]; spacing=[]
    for ax,v in enumerate(axes):
        if v.ndim!=1 or v.size<5 or not np.all(np.isfinite(v)):
            raise ValueError("Cartesian axis too short or nonfinite")
        h=float(v[1]-v[0])
        if h<=0 or not np.allclose(np.diff(v),h,rtol=1e-9,atol=1e-10):
            raise ValueError("native Cartesian axis is not uniform")
        center=int(np.argmin(abs(v-xyz[ax])))
        if not (1<=center<=v.size-2):
            raise ValueError("numerical delta support would leave grid")
        t=float((xyz[ax]-v[center])/h)
        if abs(t)>.50000000001:
            raise ValueError("nearest-grid Q2 offset exceeds half node")
        centers.append(center);fractions.append(t);spacing.append(h)
    if not np.allclose(spacing,spacing[0],rtol=1e-9,atol=1e-10):
        raise ValueError("PFFDTD requires equal Cartesian axes h")
    axis_weights=[(t*(t-1)/2,1-t*t,t*(t+1)/2) for t in fractions]
    coords=[];weights=[]
    for a,b,c in itertools.product(range(3),repeat=3):
        ix,iy,iz=centers[0]+a-1,centers[1]+b-1,centers[2]+c-1
        coords.append((ix,iy,iz))
        weights.append(axis_weights[0][a]*axis_weights[1][b]*axis_weights[2][c])
    idx=np.ravel_multi_index(np.asarray(coords,dtype=np.int64).T,dims)
    w=np.asarray(weights,dtype=np.float64)
    if (idx.size!=27 or len(np.unique(idx))!=27
        or np.any(np.isin(idx,np.asarray(boundary_nodes,dtype=np.int64)))
        or np.any(np.isin(idx,np.asarray(abc_nodes,dtype=np.int64)))):
        raise ValueError("numerical point delta overlaps blocked boundary/ABC node")
    actual=np.stack([axes[axis][np.asarray(coords)[:,axis]] for axis in range(3)],axis=-1)
    displacement=actual-xyz
    sums={
        "zeroth":float(w.sum()),
        "first_m":[float(x) for x in np.einsum("i,ij->j",w,displacement)],
        "axis_second_m2":[float(x) for x in np.einsum("i,ij->j",w,displacement**2)],
        "cross_second_m2":[float(sum(w*displacement[:,i]*displacement[:,j]))
                           for i,j in ((0,1),(0,2),(1,2))],
    }
    if (abs(sums["zeroth"]-1)>1e-12
        or max(map(abs,(*sums["first_m"],*sums["axis_second_m2"],*sums["cross_second_m2"])))>1e-10
        or not np.all(np.isfinite(w))):
        raise ValueError("Q2 point functional failed constant/linear/quadratic reproduction")
    info={"grid_dimensions":dims,"central_indices":centers,
          "relative_grid_offsets":fractions,"moments":sums,
          "negative_node_count":int(np.count_nonzero(w<0)),
          "absolute_weight_sum":float(np.abs(w).sum()),
          "support_count":27}
    return idx.astype(np.int64),w,info

def replace_dataset(h,name,values):
    if name in h:del h[name]
    h.create_dataset(name,data=values)

def source_and_receiver_from_sim(original_sim,p):
    with h5py.File(original_sim/"cart_grid.h5","r") as h:
        axes=[np.asarray(h[k][...],dtype=float) for k in ("xv","yv","zv")]
    with h5py.File(original_sim/"vox_out.h5","r") as h:
        boundary=np.asarray(h["bn_ixyz"][...],dtype=np.int64)
    # Source and receiver are central, and all 27 points must be safely interior
    # and disjoint from the absorbing ring, keeping wall physics unchanged.
    dims=tuple(len(v) for v in axes)
    ring=np.zeros(dims,dtype=bool)
    ring[1,:,:]=ring[-2,:,:]=True
    ring[:,1,:]=ring[:,-2,:]=True
    ring[:,:,1]=ring[:,:,-2]=True
    abc=np.flatnonzero(ring.ravel())
    src,sw,sm=q2_stencil(axes,p["source_fixture"]["source_xyz_m"],
                         boundary_nodes=boundary,abc_nodes=abc)
    recv,rw,rm=q2_stencil(axes,p["source_fixture"]["receiver_xyz_m"],
                          boundary_nodes=boundary,abc_nodes=abc)
    return src,sw,sm,recv,rw,rm

def materialize_experimental_point_operator(original,variant,p):
    if variant.exists():raise ValueError("output must be a previously unused directory")
    shutil.copytree(original,variant)
    # Never accept stale original 8-node pressure samples as a Q2 solve.
    (variant/"sim_outs.h5").unlink(missing_ok=True)
    src,sw,sm,rec,rw,rm=source_and_receiver_from_sim(original,p)
    with h5py.File(original/"comms_out.h5","r") as orig:
        prev=np.asarray(orig["in_sigs"][...],dtype=np.float64)
        nt=int(orig["Nt"][()])
        assert prev.shape==(8,nt) and nt<=p["resource_limits"]["max_native_steps"]
        if not np.array_equal(prev[:,1:],np.zeros_like(prev[:,1:])):
            raise ValueError("original native q0 waveform was not discrete impulse")
        native_injection=float(prev[:,0].sum())
        if not np.isfinite(native_injection) or native_injection<=0:
            raise ValueError("original source impulse sum invalid")
        if (int(orig["Ns"][()])!=8 or int(orig["Nr"][()])!=8
            or orig["out_alpha"].shape!=(1,8)
            or int(orig["diff"][()])!=0):
            raise ValueError("frozen upstream original eight-node source/receiver changed")
    modified=np.zeros((27,nt),dtype=np.float64)
    modified[:,0]=sw*native_injection
    with h5py.File(variant/"comms_out.h5","r+") as h:
        for name,val in (
            ("in_ixyz",src),("in_sigs",modified),
            ("out_ixyz",rec),("out_alpha",rw.reshape(1,27)),
            ("out_reorder",np.arange(27,dtype=np.int64)),
            ("Ns",np.int64(27)),("Nr",np.int64(27))):
            replace_dataset(h,name,val)
        if not np.array_equal(h["in_sigs"][:,1:],np.zeros((27,nt-1))):
            raise RuntimeError("unexpected temporal source change")
        if not np.isclose(float(h["in_sigs"][:,0].sum()),native_injection,rtol=0,atol=1e-12):
            raise RuntimeError("original temporal q0 normalized source changed")
    return {"source":sm,"receiver":rm,
            "original_native_total_unit_impulse":native_injection,
            "quadratic_total_unit_impulse":float(modified[:,0].sum()),
            "quadratic_comms_sha256":file_hash(variant/"comms_out.h5"),
            "native_original_27node_engine_contract":True}

def analyze_trace(sim,weights,density,frequency):
    with h5py.File(sim/"sim_outs.h5","r") as h:
        raw=np.asarray(h["u_out"][...],dtype=np.float64)
    with h5py.File(sim/"sim_consts.h5","r") as h:
        dt=float(h["Ts"][()])
    with h5py.File(sim/"comms_out.h5","r") as h:
        nt=int(h["Nt"][()])
        if int(h["Nr"][()])!=len(weights):
            raise ValueError("postprocessing receiver node count mismatch")
    phi=recombine_pffdtd_receiver_traces(raw,weights.reshape(1,-1),
                                           receiver_count=1,nt=nt)[0]
    pressure=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt,density_kg_m3=density)
    q=np.zeros(nt,dtype=float);q[0]=1.0
    t=finite_record_pressure_transfer(
        pressure,q,time_step_s=dt,frequency_hz=np.asarray(frequency,dtype=float))
    if t.shape!=(2,) or not np.all(np.isfinite(t)):
        raise RuntimeError("nonfinite or missing original physical 40/80 Hz output")
    return [[float(z.real),float(z.imag)] for z in t]

def frozen_baseline_original_result(original_sim,original_row,p):
    with h5py.File(original_sim/"comms_out.h5","r") as h:
        alpha=np.asarray(h["out_alpha"][...],dtype=float).reshape(-1)
    spectrum=analyze_trace(original_sim,alpha,p["source_fixture"]["density_kg_m3"],
                            p["source_fixture"]["frequency_hz"])
    expected=np.asarray(original_row["transfer_pa_per_m3_s"],dtype=float)
    if not np.allclose(spectrum,expected,rtol=1e-6,atol=1e-5):
        raise ValueError(f"original upstream control trace does not reproduce frozen 40/80 reference {spectrum} != {expected.tolist()}")
    return spectrum

def run_pffdtd(variant,p,upstream):
    from fdtd.sim_fdtd import SimEngine
    original_grid=variant/"vox_out.h5"
    with h5py.File(original_grid,"r") as h:
        dims=tuple(int(h[key][()]) for key in ("Nx","Ny","Nz"))
    if int(np.prod(dims))>p["resource_limits"]["max_grid_cells"]:
        raise ValueError("bounded full PFFDTD grid exceeded")
    start=time.perf_counter()
    engine=SimEngine(variant,energy_on=False,nthreads=2)
    engine.load_h5_data()
    engine.setup_mask()
    halo=apply_boundary_halo_separation(engine)
    engine.allocate_mem()
    engine.set_coeffs()
    engine.checks()
    if int(engine.Ns)!=27 or int(engine.Nr)!=27:
        raise ValueError("pinned PFFDTD engine did not load 27-point source/receiver")
    print("ACTUAL_NATIVE_PFFDTD_SOLVE",dims,engine.Nt,flush=True)
    engine.run_all(nsteps=int(engine.Nt))
    elapsed=time.perf_counter()-start
    if elapsed>p["resource_limits"]["max_true_wave_wall_seconds_per_case"]:
        raise RuntimeError("native wave run exceeded preregistered wall ceiling")
    engine.save_outputs()
    if not (variant/"sim_outs.h5").is_file():
        raise RuntimeError("pinned PFFDTD failed to output raw native results")
    return {"wall_seconds":elapsed,
            "grid_dimensions":list(dims),"dt_s":float(engine.Ts),
            "sample_count":int(engine.Nt),
            "pffdtd_engine_same_pinned_upstream":True,
            "boundary_halo_treatment":halo.model_dump(mode="json"),
            "sim_outs_sha256":file_hash(variant/"sim_outs.h5")}

def native_pair_metrics(levels,p):
    result=[]
    f=p["source_fixture"]["frequency_hz"]
    for coarse,fine in zip(levels,levels[1:]):
        check=compare_complex_transfer(
            reference=fine["quadratic_transfer_pa_per_m3_s"],
            candidate=coarse["quadratic_transfer_pa_per_m3_s"],
            frequency_hz=f,
            magnitude_mask_relative_db=p["comparisons"]["magnitude_mask_relative_db"]).model_dump(mode="json")
        result.append({"coarse_ppw":coarse["ppw"],"fine_ppw":fine["ppw"],**check})
    limits=p["comparisons"]["original_numeric_thresholds"]
    flags=[row["complex_rms_relative"]<=limits["complex_rms_relative_max"]
        and row["magnitude_max_relative"]<=limits["magnitude_max_relative"]
        and row["phase_max_deg"]<=limits["phase_max_deg"] for row in result]
    decreasing=all(
        (result[j]["complex_rms_relative"]<result[j-1]["complex_rms_relative"]
         and result[j]["magnitude_max_relative"]<result[j-1]["magnitude_max_relative"]
         and result[j]["phase_max_deg"]<result[j-1]["phase_max_deg"])
        for j in range(1,len(result)))
    return {"pairs":result,"within_original_thresholds":flags,
            "all_three_metrics_strictly_decreasing":decreasing,
            "last_pair_passes_original_frozen_thresholds":flags[-1],
            "diagnostic_refinement_pass":bool(flags[-1] and decreasing),
            "canonical_PPWs_8_10_12_not_rerun":True,
            "this_is_not_a_fullband_original_pffdtd_qualification":True}

def work_ledger(p,original_high,original_root):
    rows=[r for r in original_high["levels"] if int(r["ppw"]) in PPW]
    if [int(r["ppw"]) for r in rows]!=list(PPW):
        raise ValueError("original high-PPW controls are missing or out of order")
    base=Path(original_root)
    cases={}
    for folder in base.rglob("comms_out.h5"):
        sim=folder.parent
        with h5py.File(sim/"sim_consts.h5","r") as h:
            grid_h=float(h["h"][()])
        matches=[ppw for ppw in PPW if np.isclose(grid_h,343.2/(100*ppw),rtol=0,atol=1e-12)]
        if len(matches)!=1:raise ValueError("unexpected original source sim grid")
        ppw=matches[0]
        if ppw in cases:raise ValueError("duplicate PPW setup")
        if file_hash(folder)!=p["original_native_comms_sha_by_ppw"][str(ppw)]:
            raise ValueError(f"original native PFFDTD source/receiver comms hash drift PPW {ppw}")
        cases[ppw]=sim
    if set(cases)!=set(PPW):raise ValueError("all five original native PPW cases required")
    for original_row in rows:
        ppw=int(original_row["ppw"])
        if original_row["time_step_count"]>p["resource_limits"]["max_native_steps"]:
            raise ValueError("time step ceiling exceeded")
        with h5py.File(cases[ppw]/"sim_consts.h5","r") as h:
            dt=float(h["Ts"][()]);h_m=float(h["h"][()])
        if (abs(dt-original_row["time_step_s"])>1e-12
            or abs(h_m-original_row["grid_spacing_m"])>1e-12):
            raise ValueError("native sim time/space differs from frozen original row")
    return list(zip(rows,[cases[int(row["ppw"])] for row in rows]))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--pffdtd-root",type=Path,required=True)
    ap.add_argument("--original-high-ppw-evidence",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--variant-work",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--dry-run",action="store_true")
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    archive_bytes=a.original_high_ppw_evidence.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest()!=ORIG_SHA:
        raise ValueError("unmerged native PFFDTD five-level frozen original high-PPW record changed")
    original=json.loads(archive_bytes)
    import subprocess
    upstream=subprocess.check_output(["git","-C",str(a.pffdtd_root),"rev-parse","HEAD"],text=True).strip()
    if upstream!=PIN:raise ValueError("native PFFDTD SHA drift")
    changed=subprocess.check_output(["git","-C",str(a.pffdtd_root),"diff","--name-only"],text=True).splitlines()
    expected=["python/common/myfuncs.py","python/voxelizer/vox_grid_base.py",
              "python/voxelizer/vox_scene.py"]
    if sorted(changed)!=expected:
        raise ValueError(f"unexpected modifications of pinned native solver: {changed}")
    sys.path.insert(0,str(a.pffdtd_root/"python"))
    ledger=work_ledger(p,original,a.original_sims_root)
    result={"schema_version":"htdt.r130d.native-original-physical-point-quadratic-q0-evidence-1",
            "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
            "preregistered_plan":p,
            "frozen_original_high_ppw_record_sha256":ORIG_SHA,
            "pinned_upstream_sha":upstream,
            "upstream_modified_compatibility_files":expected,
            "actual_native_wave_cases":[]}
    for row,sim in ledger:
        ppw=int(row["ppw"])
        control=frozen_baseline_original_result(sim,row,p)
        s,r=source_and_receiver_from_sim(sim,p)[:3],source_and_receiver_from_sim(sim,p)[3:]
        print("ORIGINAL_NATIVE_CONTROL_VERIFIED",ppw,control,"source/receiver q2 moment checks PASS",flush=True)
        if a.dry_run:continue
        variant=a.variant_work/f"native-q2-ppw{ppw}"
        variant.parent.mkdir(parents=True,exist_ok=True)
        prep=materialize_experimental_point_operator(sim,variant,p)
        engine=run_pffdtd(variant,p,a.pffdtd_root)
        with h5py.File(variant/"comms_out.h5","r") as h:
            received=np.asarray(h["out_alpha"][...],dtype=float).reshape(-1)
        transfer=analyze_trace(variant,received,p["source_fixture"]["density_kg_m3"],
                               p["source_fixture"]["frequency_hz"])
        result["actual_native_wave_cases"].append({
            "ppw":ppw,
            "unmodified_original_transfer_pa_per_m3_s":control,
            "quadratic_transfer_pa_per_m3_s":transfer,
            "original_native_comm_sha256":file_hash(sim/"comms_out.h5"),
            "original_solver_geometry_sha256":file_hash(sim/"vox_out.h5"),
            "original_same_geometry_and_point_locations":True,
            "native_original_discrete_q0_unchanged":True,
            "numerical_operator":prep,
            "native_solver":engine,
        })
        partial=a.output.with_name(a.output.stem+"_partial.json")
        partial.parent.mkdir(parents=True,exist_ok=True)
        partial.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("ACTUAL_NATIVE_Q2_PPWT",ppw,transfer,flush=True)
    if a.dry_run:return
    if len(result["actual_native_wave_cases"])!=5:
        raise ValueError("cannot omit preregistered full PFFDTD wave cases")
    result["adjacent_quadratic_refinement"]=native_pair_metrics(result["actual_native_wave_cases"],p)
    result.update({"canonical_original_8_node_point_point":"SELF_CONVERGENCE_FAILED",
                   "canonical_original_point_source_physical_validation":"NOT_VALIDATED",
                   "original_8_node_PFFDTD_code_modified":False,
                   "experimental_27_point_discretization_not_promoted":True,
                   "production_ready":False})
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("FULL_FIVE_LEVEL_PFFDTD_Q2_FINISHED",[(x["coarse_ppw"],x["fine_ppw"],x["complex_rms_relative"])
        for x in result["adjacent_quadratic_refinement"]["pairs"]],flush=True)
    print("FULL_ORIGINAL_EIGHT_NODE_POINT_8_10_12_STILL_FAIL",flush=True)
if __name__=="__main__":
    main()
