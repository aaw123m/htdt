"""Preregistered band-limited observable requalification (issue #53 / PR #118).

Executes the contract-change plan committed before this run: evaluate the
signed 40/80 Hz transfer restricted to semidiscrete modes <=200 Hz on the
exact-geometry hybrid eigensystem, under the unchanged three original gates
on all four adjacent pairs plus strict monotonicity. Deterministic rescore
of the SHA-pinned committed hybrid evidence; no new solver runs.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = Path("benchmarks/acoustics/r130d_bandlimited_observable_requalification_plan_2026-10-11.json")
HYBRID_PATH = Path("benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_evidence_2026-10-10.json")
PPW = (28, 32, 36, 40, 44)
CUTOFF = 200.0
SWEEP = (100., 200., 400., 800., 1600., 3200.)
GATE = {"complex_rms_relative": 0.2, "magnitude_max_relative": 0.25, "phase_max_deg": 15.0}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _cx(pairs) -> np.ndarray:
    return np.array([complex(z[0], z[1]) for z in pairs])


def _band_t(c: dict, hi: float) -> np.ndarray:
    t = np.zeros(2, complex)
    for b in c["frequency_bands_signed_all_modes"]:
        hb = b["frequency_high_hz"]
        if hb is not None and hb <= hi + 1e-9:
            t += _cx(b["signed_40_80"])
    return t


def _score(tc: np.ndarray, tf: np.ndarray) -> dict:
    d = tc - tf
    den = np.linalg.norm(tf)
    cr = float(np.linalg.norm(d) / den)
    mg = float(np.abs((np.abs(tc) - np.abs(tf)) / np.abs(tf)).max())
    ph = float(np.degrees(np.abs(np.angle(tc * np.conj(tf) / (np.abs(tc) * np.abs(tf))))).max())
    return {"complex_rms_relative": cr, "magnitude_max_relative": mg, "phase_max_deg": ph,
            "all_three_gates": cr <= 0.2 and mg <= 0.25 and ph <= 15.0}


def _pairs(cases: dict, hi: float) -> list:
    return [{"pair": [PPW[i], PPW[i + 1]],
             **_score(_band_t(cases[PPW[i]], hi), _band_t(cases[PPW[i + 1]], hi))}
            for i in range(4)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    plan = json.loads((ROOT / PLAN_PATH).read_text(encoding="utf8"))
    if plan["requalified_contract"]["discretization"].find("exact-geometry") < 0:
        raise ValueError("plan discretization drift")
    hybrid = json.loads((ROOT / HYBRID_PATH).read_text(encoding="utf8"))
    cases = {c["ppw"]: c for c in hybrid["all_five_original_q0_modal_endpoint_cases"]}
    if set(cases) != set(PPW):
        raise ValueError("missing SHA-pinned hybrid modal cases")

    conservation = {}
    for p in PPW:
        t = np.zeros(2, complex)
        for b in cases[p]["frequency_bands_signed_all_modes"]:
            t += _cx(b["signed_40_80"])
        f = _cx(cases[p]["frozen_full_signed_40_80"])
        conservation[str(p)] = float(np.linalg.norm(t - f) / np.linalg.norm(f))
        if conservation[str(p)] > 1e-10:
            raise ValueError(f"band reconstruction lost at ppw {p}")

    pairs = _pairs(cases, CUTOFF)
    cs = [s["complex_rms_relative"] for s in pairs]
    ms = [s["magnitude_max_relative"] for s in pairs]
    ps = [s["phase_max_deg"] for s in pairs]
    mono = {"complex_rms_relative": all(cs[j] > cs[j + 1] for j in range(3)),
            "magnitude_max_relative": all(ms[j] > ms[j + 1] for j in range(3)),
            "phase_max_deg": all(ps[j] > ps[j + 1] for j in range(3))}
    converged = all(s["all_three_gates"] for s in pairs)
    sweep = {str(int(h)): _pairs(cases, h) for h in SWEEP}
    verdicts = ["REQUALIFIED_CONTRACT_CONVERGENCE_ACHIEVED" if converged
                else "REQUALIFIED_CONTRACT_CONVERGENCE_FAILED"]
    if converged and not all(mono.values()):
        verdicts.append("MONOTONICITY_AT_NOISE_FLOOR")

    e = {
        "schema_version": "htdt.r130d.bandlimited-observable-requalification-evidence-1",
        "preregistered_plan_sha256_lf": _sha(ROOT / PLAN_PATH),
        "preregistered_plan": plan,
        "input_sha256": {"hybrid_endpoint": _sha(ROOT / HYBRID_PATH)},
        "requalified_observable_hz": CUTOFF,
        "conservation_rel": conservation,
        "pairs": pairs,
        "strict_monotonicity": mono,
        "full_cutoff_sweep_pairs": sweep,
        "verdicts": verdicts,
        "canonical_original_contract": "SELF_CONVERGENCE_FAILED",
        "product": "NO_GO",
        "new_solver_runs": 0,
        "new_github_actions_runs": 0,
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(e, indent=2, allow_nan=False) + "\n", encoding="utf8")
    print(json.dumps({"verdicts": verdicts,
                      "worst_complex": max(cs), "worst_mag": max(ms), "worst_phase_deg": max(ps)}, indent=1))


if __name__ == "__main__":
    main()
