#!/usr/bin/env python3
"""Fixed-prior physical point q0 3-phase grid quadrature, no new wave solver runs."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_general3d_validation import compare_complex_transfer
from run_r130d_native_exact_roof_fv_q0 import pairs,unpairs

SCHEMA="htdt.r130d.exact-roof-physical-point-q0-grid-phase-ensemble-plan-1"
PPW=(40,44)
PHASES=(-0.25,0.,0.25)
def verify_plan(p):
    if (p.get("schema_version")!=SCHEMA or
        p["frozen_inputs"]["PPW"]!=list(PPW) or
        p["frozen_inputs"]["x_shift_fraction_of_own_native_h"]!=list(PHASES)
        or p["frozen_inputs"]["physical_source_xyz_m"]!=[1.5,2,2]
        or p["frozen_inputs"]["physical_receiver_xyz_m"]!=[2.5,2,2]
        or p["frozen_inputs"]["frequencies_hz"]!=[40,80]
        or p["frozen_inputs"]["record_s"]!=.25
        or p["estimator"]["weights"]!=[1/3]*3
        or p["evaluation"]["compare_frozen_original_threshold_complex"]!=.2
        or p["evaluation"]["compare_frozen_original_threshold_magnitude"]!=.25
        or p["evaluation"]["compare_frozen_original_threshold_phase_deg"]!=15
        or p["limits"]["max_new_wave_runs"]!=0 or
        p["limits"]["no_github_actions_runs"] is not True
        or p["authority"]["original_q0_fullband"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["product"]!="NO_GO"):
        raise ValueError("original q0 three x phase quadrature frozen contract modified")
    return p

def score(x,y):
    return compare_complex_transfer(
        reference=pairs(y),candidate=pairs(x),
        frequency_hz=[40,80],magnitude_mask_relative_db=-50).model_dump(mode="json")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    raw=args.plan.read_bytes()
    p=verify_plan(json.loads(raw.decode("utf-8")))
    base=json.loads((ROOT/p["frozen_inputs"]["exact_roof_native_grid_unshifted_original"]).read_text(encoding="utf-8"))
    shifted=json.loads((ROOT/p["frozen_inputs"]["actual_x_grid_shifted_full_native_wave"]).read_text(encoding="utf-8"))
    if (base["canonical_original_point_q0"]!="SELF_CONVERGENCE_FAILED"
        or base["product"]!="NO_GO" or
        shifted["original_canonical"]!="SELF_CONVERGENCE_FAILED"
        or shifted["product"]!="NO_GO"):
        raise ValueError("original source physical unchanged controls were promoted")
    controls={r["ppw"]:r for r in base["actual_native_grid_point_impulse_exact_roof_cases"] if r["ppw"] in PPW}
    data={}
    for r in shifted["actual_shifted_x_grid_full_original_q0_wave_cases"]:
        key=(r["ppw"],r["native_grid_x_shift_fraction_h"])
        if key in data:raise ValueError("duplicate saved real shifted physical point q0 full-wave")
        data[key]=r
    if set(controls)!=set(PPW) or set(data)!={(ppw,ph) for ppw in PPW for ph in (-.25,.25)}:
        raise ValueError("all 4 unmodified actual prior shifted wave experiments required")
    rows=[]
    for ppw in PPW:
        ctrl=controls[ppw]
        observations=[]
        for phase in PHASES:
            if phase==0:
                z=unpairs(ctrl["experimental_signed_P_T_over_Q_T_40_80"])
                num=ctrl["original_record_samples"]
                dt=ctrl["original_native_dt_s"]
                exactvolume=ctrl["exact_physical_room_volume_m3"]
            else:
                r=data[(ppw,phase)]
                z=unpairs(r["new_actual_unmodified_physical_point_q0_signed_40_80"])
                num=r["unmodified_native_sample_count"]
                dt=r["unmodified_native_dt_s"]
                exactvolume=r["physical_volume_m3"]
                if (r["source_receiver_xyz_and_q0_unchanged"] is not True
                    or r["native_unmodified_original_q0_stencil_confirmed"] is not True):
                    raise ValueError("physical point xyz or original unit q0 changed")
            if (num!=ctrl["original_record_samples"]
                or abs(dt-ctrl["original_native_dt_s"])>1e-12
                or abs(exactvolume-56)>2e-8):
                raise ValueError("source q0 native clock or analytic 56m3 room changed")
            observations.append({"x_phase_h":phase,"signed_native_40_80":pairs(z)})
        averaged=np.mean(np.stack([unpairs(row["signed_native_40_80"]) for row in observations]),axis=0)
        rows.append({"ppw":ppw,"three_actual_native_fullwave_phase_signed_transfers":observations,
             "original_q0_equal_weight_three_x_phase_signed_ensemble":pairs(averaged),
             "original_physical_source_receiver_fixed":True,
             "original_temporal_q0_and_full_250ms_bins_fixed":True,
             "no_original_native_PFFDTD_fullwave_modification":True,
             "no_new_wave_computation":True})
        print("THREE_PHASE_ORIGINAL_POINT_Q0",ppw,
              "averaged signed physical 40/80",pairs(averaged),flush=True)
    original=score(unpairs(rows[0]["three_actual_native_fullwave_phase_signed_transfers"][1]["signed_native_40_80"]),
                   unpairs(rows[1]["three_actual_native_fullwave_phase_signed_transfers"][1]["signed_native_40_80"]))
    mean=score(unpairs(rows[0]["original_q0_equal_weight_three_x_phase_signed_ensemble"]),
               unpairs(rows[1]["original_q0_equal_weight_three_x_phase_signed_ensemble"]))
    gate=p["evaluation"]
    accepted=bool(mean["complex_rms_relative"]<=gate["compare_frozen_original_threshold_complex"]
       and mean["magnitude_max_relative"]<=gate["compare_frozen_original_threshold_magnitude"]
       and mean["phase_max_deg"]<=gate["compare_frozen_original_threshold_phase_deg"])
    out={"schema_version":"htdt.r130d.exact-roof-physical-point-q0-grid-phase-ensemble-evidence-1",
        "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
        "preregistered_plan":p,"actual_unmodified_native_q0_phase_ensemble":rows,
        "original_unshifted_exact_roof_fullwave_PP40_44":original,
        "equal_weight_three_phase_fullwave_PP40_44":mean,
        "equal_weight_three_phase_pair_below_frozen_numerical_limits":accepted,
        "one_experimental_pair_not_full_grid_refinement_qualification":True,
        "original_canonical_PFFDTD_point_q0":"SELF_CONVERGENCE_FAILED",
        "physical_validation":"NOT_VALIDATED","product":"NO_GO",
        "new_full_wave_runs":0,"new_github_actions_runs":0}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(out,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("PREDECLARED_THREE_PHASE_ORIGINAL_Q0_NUMERICAL_CANDIDATE",mean,
          "experimental pair gate",accepted,
          "original NEVER REQUALIFIED",flush=True)
if __name__=="__main__":main()
