#!/usr/bin/env python3
"""Recompute fail-closed candidate-only numerical gate from immutable inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from htdt.r130d_candidate_same_source_qualification import assess


def main() -> None:
    p=argparse.ArgumentParser()
    p.add_argument("--candidate-plan",type=Path,required=True)
    p.add_argument("--parent-plan",type=Path,required=True)
    p.add_argument("--fv-evidence",type=Path,required=True)
    p.add_argument("--mfem-r123-evidence",type=Path,required=True)
    p.add_argument("--mfem-r4-evidence",type=Path)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    def load(x):
        return json.loads(x.read_text(encoding="utf-8")) if x else None
    result=assess(
        candidate_plan=load(a.candidate_plan),
        parent_frozen_plan=load(a.parent_plan),
        fv_evidence=load(a.fv_evidence),
        mfem_r123_evidence=load(a.mfem_r123_evidence),
        mfem_r4_evidence=load(a.mfem_r4_evidence),
    )
    output={
        "schema_version":"htdt.r130d.candidate-same-source-numerical-gate-evidence-1",
        "gate":result,
        "plan":str(a.candidate_plan.name),
        "original_frozen_r130d_still_failed":True,
        "production_ready":False,
    }
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(output,indent=2,sort_keys=True)+"\n",
                        encoding="utf-8")
    print(json.dumps(result,indent=2),flush=True)


if __name__=="__main__":
    main()
