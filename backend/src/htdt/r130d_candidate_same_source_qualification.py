"""Fail-closed R130D *experimental* same-source FV/MFEM numerical gate.

The newly defined Gaussian-volume-velocity physical model differs from the
original frozen impulse R130D contract. This module can classify numerical
candidate evidence only; it NEVER selects a production solver, overrides
a legacy failing gate, or promotes physical validation.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


SELF_KEYS = ("complex_rms_relative", "magnitude_max_relative", "phase_max_deg")
CROSS_KEYS = (*SELF_KEYS, "magnitude_max_db")
FV_ORDER = (20,24,28,32)
MFEM_ORDER = (2,3,4)
BINS = (40.0,80.0)


def _complex_transfer(row: dict, key: str) -> np.ndarray:
    vals = np.asarray(row[key],dtype=np.float64)
    if vals.shape != (2,2) or not np.all(np.isfinite(vals)):
        raise ValueError("missing or nonfinite complex transfer for both frozen bins")
    return vals[:,0]+1j*vals[:,1]


def metrics(a: np.ndarray,b: np.ndarray) -> dict[str,float]:
    """Identical fine-reference-normalized complex/magnitude/phase metrics.

    Only 40/80-Hz high-amplitude bins; no clipping, removal or refitting.
    """
    if a.shape != (2,) or b.shape != (2,):
        raise ValueError("must retain both fixed evaluation bins")
    if (not np.all(np.isfinite(a)) or not np.all(np.isfinite(b))
            or np.any(np.abs(b)<1e-30) or np.any(np.abs(a)<1e-30)):
        raise ValueError("near-zero or invalid transfer sample")
    magref = abs(b)
    ratio = abs(a)/magref
    return {
        "complex_rms_relative":float(np.linalg.norm(a-b)/np.linalg.norm(b)),
        "magnitude_max_relative":float(np.max(abs(abs(a)-magref)/magref)),
        "magnitude_max_db":float(np.max(abs(20*np.log10(ratio)))),
        "phase_max_deg":float(np.max(abs(np.angle(a/b,deg=True)))),
    }


def _relative_thresholds(plan: dict, parent: dict) -> dict:
    inherited = {
        "fv_self":parent["acceptance"]["pffdtd_self_convergence"],
        "mfem_self":parent["acceptance"]["reference_self_convergence"],
        "fine_fine":parent["acceptance"]["cross_solver_fine_fine"],
    }
    if plan["strict_thresholds_unmodified_from_parent"] != inherited:
        raise ValueError("source-independent numerical tolerances cannot be retuned")
    if plan["parent_frozen_reference_plan_sha256"]!=(
            "5c753073a88d6705ee2962aa387006d4c563f90f0249b722dcf309a8155f62ec"):
        raise ValueError("frozen reference identity differs")
    if (plan["fv_levels"]!=list(FV_ORDER) or
        plan["mfem_levels"]!=list(MFEM_ORDER) or
        tuple(map(float,plan["bins_hz"]))!=BINS or
        plan["time"]!={"duration_s":0.25,"dt_s":0.00025}):
        raise ValueError("plan changed the bounded physical observation")
    if plan["fail_closed"]["canonical_r130d_impulse"]!="SELF_CONVERGENCE_FAILED":
        raise ValueError("canonical failed gate must be preserved")
    if (plan["fail_closed"]["production_enabled"] is not False
            or plan["fail_closed"]["physical_validation"]!="NOT_VALIDATED"):
        raise ValueError("experimental numeric gate cannot produce authority")
    return inherited


def _last_pair_pass(values: list[dict], limit: dict,
                    *, compare_db: bool=False) -> tuple[bool,bool]:
    required = CROSS_KEYS if compare_db else SELF_KEYS
    last = values[-1]
    within = all(
        last[k] <= limit['complex_rms_relative_max' if k == 'complex_rms_relative' else k]
        for k in required
    )
    descending = all(
        values[i][k]<values[i-1][k] for i in range(1,len(values))
        for k in SELF_KEYS
    )
    return within,descending


def assess(*, candidate_plan:dict, parent_frozen_plan:dict,
           fv_evidence:dict, mfem_r123_evidence:dict,
           mfem_r4_evidence:dict|None=None) -> dict[str,Any]:
    """Assess a prospectively frozen candidate; absent r4 is explicitly BLOCKED."""
    if candidate_plan.get("schema_version")!=(
            "htdt.r130d.candidate-same-source-numerical-acceptance-plan-1"):
        raise ValueError("unregistered candidate gate")
    tolerances = _relative_thresholds(candidate_plan,parent_frozen_plan)
    fv_levels = fv_evidence.get("levels",[])
    if (fv_evidence.get("schema")!="htdt.r130d.embedded-fv-actual-bandlimited-source-1"
        or [r["grid_cells_per_axis"] for r in fv_levels]!=list(FV_ORDER)
        or fv_evidence.get("actual_driven_solver_execution") is not True
        or fv_evidence.get("production_ready") is not False):
        raise ValueError("independent FV source not preregistered or incorrectly qualified")
    fm = mfem_r123_evidence.get("mfem_levels",[])
    if (mfem_r123_evidence.get("schema_version")!=
        "htdt.r130d.fv-mfem-same-drive-comparison-evidence-1"
        or [x["refinement"] for x in fm]!=[1,2,3]
        or any(x["actual_integrated_gaussian_source"] is not True for x in fm)
        or mfem_r123_evidence["gate"]["production_ready"] is not False):
        raise ValueError("independent MFEM actual-drive evidence missing")
    fv_vals=[_complex_transfer(x,"transfer_complex_40_80_hz") for x in fv_levels]
    fv_pairs=[
        {"coarse":FV_ORDER[i],"fine":FV_ORDER[i+1],
         **metrics(fv_vals[i],fv_vals[i+1])}
        for i in range(3)
    ]
    fv_within,fv_decrease = _last_pair_pass(fv_pairs,tolerances["fv_self"])
    fv_state = ("SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
                if fv_within and fv_decrease else "SELF_CONVERGENCE_FAILED")
    mfem_state = "REFINEMENT_R4_MISSING"
    mfem_pairs=[]
    cross={}
    cross_state="CROSS_SOLVER_BLOCKED"
    if mfem_r4_evidence is not None:
        if (mfem_r4_evidence.get("schema_version")!=
                "htdt.r130d.mfem-r4-same-source-independent-result-1"
                or mfem_r4_evidence.get("mfem_dofs")!=35937
                or mfem_r4_evidence.get("mfem_refinement")!=4
                or mfem_r4_evidence.get("gate",{}).get("production_ready") is not False
                or mfem_r4_evidence.get("mfem_source_sha")!=
                   "d964264cdb9a13e94a201b6c236c7721e0c8765f"):
            raise ValueError("independent MFEM r4 identity/physical authority invalid")
        fem_vals=[_complex_transfer(x,"transfer_complex_40_80_hz") for x in fm[1:]]
        fem_vals.append(_complex_transfer(mfem_r4_evidence,"transfer_complex_40_80_hz"))
        mfem_pairs=[
            {"coarse":MFEM_ORDER[i],"fine":MFEM_ORDER[i+1],
             **metrics(fem_vals[i],fem_vals[i+1])}
            for i in range(2)
        ]
        mfem_within,mfem_decrease=_last_pair_pass(
            mfem_pairs,tolerances["mfem_self"])
        mfem_state=("SELF_CONVERGENCE_PASS_CANDIDATE_ONLY" if mfem_within
                    and mfem_decrease else "SELF_CONVERGENCE_FAILED")
        if (fv_state=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
                and mfem_state=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"):
            cross=metrics(fv_vals[-1],fem_vals[-1])
            cross_pass=all(
                cross[k] <= tolerances['fine_fine'][
                    'complex_rms_relative_max' if k == 'complex_rms_relative' else k
                ] for k in CROSS_KEYS
            )
            cross_state=("CROSS_SOLVER_PASS_CANDIDATE_ONLY" if cross_pass
                         else "CROSS_SOLVER_FAILED")
    candidate_pass=(
        cross_state=="CROSS_SOLVER_PASS_CANDIDATE_ONLY" and
        fv_state=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY" and
        mfem_state=="SELF_CONVERGENCE_PASS_CANDIDATE_ONLY"
    )
    return {
        "candidate_numerical_evidence_state":(
            "EXPERIMENTAL_CANDIDATE_NUMERICAL_PASS_ONLY"
            if candidate_pass else "CANDIDATE_NUMERICAL_NOT_QUALIFIED"
        ),
        "fv_self_state":fv_state,
        "mfem_self_state":mfem_state,
        "cross_solver_state":cross_state,
        "fv_adjacent_metrics":fv_pairs,
        "mfem_adjacent_metrics":mfem_pairs,
        "cross_solver_fine_fine_metrics":cross,
        "immutable_production_gate":{
            "canonical_r130d_impulse":"SELF_CONVERGENCE_FAILED",
            "production_enabled":False,
            "physical_validation":"NOT_VALIDATED",
        },
    }
