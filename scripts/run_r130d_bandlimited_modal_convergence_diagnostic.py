"""Preregistered band-limited modal convergence diagnostic (issue #53 / PR #118).

Deterministic rescore of two committed all-mode evidences: the original
native staircase graph modal transfer and the exact-geometry hybrid modal
endpoint attribution. No new solver runs; every input is SHA-pinned. The
band-limited scores are diagnostics only — the canonical full-band original
authority is never recomputed from a truncated modal sum.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = Path("benchmarks/acoustics/r130d_original_q0_bandlimited_modal_convergence_plan_2026-10-11.json")
NATIVE_PATH = Path("benchmarks/acoustics/r130d_original_pffdtd_native_full_modal_q0_evidence_2026-10-09.json")
HYBRID_PATH = Path("benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_evidence_2026-10-10.json")
PPW = (28, 32, 36, 40, 44)
CUTOFFS = (100., 200., 400., 800., 1600., 3200.)
GATE = {"complex_rms_relative": 0.2, "magnitude_max_relative": 0.25, "phase_max_deg": 15.0}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _cx(pairs) -> np.ndarray:
    return np.array([complex(z[0], z[1]) for z in pairs])


def _score(tc: np.ndarray, tf: np.ndarray) -> dict:
    d = tc - tf
    den = np.linalg.norm(tf)
    cr = float(np.linalg.norm(d) / den)
    mg = float(np.abs((np.abs(tc) - np.abs(tf)) / np.abs(tf)).max())
    ph = float(np.degrees(np.abs(np.angle(tc * np.conj(tf) / (np.abs(tc) * np.abs(tf))))).max())
    gates = {
        "complex_rms_relative": cr <= GATE["complex_rms_relative"],
        "magnitude_max_relative": mg <= GATE["magnitude_max_relative"],
        "phase_max_deg": ph <= GATE["phase_max_deg"],
    }
    return {"complex_rms_relative": cr, "magnitude_max_relative": mg,
            "phase_max_deg": ph, "all_three_gates": all(gates.values()), "gates": gates}


def _native_band_t(c: dict, hi: float) -> np.ndarray:
    t = np.zeros(2, complex)
    for b in c["all_native_actual_original_PFFDTD_modes_signed_spectral_band_transfer"]:
        lo, h2 = b["semidiscrete_mode_frequency_band_hz"]
        if h2 <= hi + 1e-9:
            t += _cx(b["signed_original_8node_q0_full250ms_P_T_over_Q_T_40_80"])
    return t


def _hybrid_band_t(c: dict, hi: float) -> np.ndarray:
    t = np.zeros(2, complex)
    for b in c["frequency_bands_signed_all_modes"]:
        hb = b["frequency_high_hz"]
        if hb is not None and hb <= hi + 1e-9:
            t += _cx(b["signed_40_80"])
    return t


def _rescore(cases: dict, band_t, full_key, full_sum=None) -> dict:
    out = {"conservation_rel": {}, "cutoffs": {}}
    # conservation: band sum must reconstruct the frozen full transfer
    for p in PPW:
        t = full_sum(cases[p]) if full_sum else band_t(cases[p], float("inf"))
        f = _cx(cases[p][full_key])
        rel = float(np.linalg.norm(t - f) / np.linalg.norm(f))
        out["conservation_rel"][str(p)] = rel
        if rel > 1e-10:
            raise ValueError(f"band reconstruction lost at ppw {p}: {rel}")
    for hi in CUTOFFS:
        seq = []
        for i in range(4):
            s = _score(band_t(cases[PPW[i]], hi), band_t(cases[PPW[i + 1]], hi))
            seq.append({"pair": [PPW[i], PPW[i + 1]], **s})
        cs = [s["complex_rms_relative"] for s in seq]
        ms = [s["magnitude_max_relative"] for s in seq]
        ps = [s["phase_max_deg"] for s in seq]
        out["cutoffs"][str(int(hi))] = {
            "pairs": seq,
            "all_pairs_all_gates": all(s["all_three_gates"] for s in seq),
            "strict_monotone": {
                "complex_rms_relative": all(cs[j] > cs[j + 1] for j in range(3)),
                "magnitude_max_relative": all(ms[j] > ms[j + 1] for j in range(3)),
                "phase_max_deg": all(ps[j] > ps[j + 1] for j in range(3)),
            },
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    plan = json.loads((ROOT / PLAN_PATH).read_text(encoding="utf8"))
    native = json.loads((ROOT / NATIVE_PATH).read_text(encoding="utf8"))
    hybrid = json.loads((ROOT / HYBRID_PATH).read_text(encoding="utf8"))
    ncases = {c["ppw"]: c for c in native["actual_original_unmodified_all_mode_native_cases"]}
    hcases = {c["ppw"]: c for c in hybrid["all_five_original_q0_modal_endpoint_cases"]}
    if set(ncases) != set(PPW) or set(hcases) != set(PPW):
        raise ValueError("missing SHA-pinned modal cases")

    def _h_sum(c: dict) -> np.ndarray:
        t = np.zeros(2, complex)
        for b in c["frequency_bands_signed_all_modes"]:
            t += _cx(b["signed_40_80"])
        return t

    native_r = _rescore(ncases, _native_band_t, "exact_original_saved_true_PFFDTD_q0_signed_40_80")
    hybrid_r = _rescore(hcases, _hybrid_band_t, "frozen_full_signed_40_80", full_sum=_h_sum)

    # verdicts
    h200 = hybrid_r["cutoffs"]["200"]
    h100 = hybrid_r["cutoffs"]["100"]
    n100 = native_r["cutoffs"]["100"]
    verdicts = []
    if h200["all_pairs_all_gates"] or h100["all_pairs_all_gates"]:
        verdicts.append("BANDLIMITED_CONVERGENCE_ACHIEVED_TRUE_GEOMETRY")
    else:
        verdicts.append("BANDLIMITED_CONVERGENCE_NOT_ACHIEVED_TRUE_GEOMETRY")
    if n100["all_pairs_all_gates"]:
        verdicts.append("ORIGINAL_NATIVE_LOWBAND_CONVERGENT")
    else:
        verdicts.append("ORIGINAL_NATIVE_LOWBAND_NOT_CONVERGENT")
    convergent_keys = [k for k, v in hybrid_r["cutoffs"].items() if v["all_pairs_all_gates"]]
    mono_ok = any(hybrid_r["cutoffs"][k]["strict_monotone"]["complex_rms_relative"] for k in convergent_keys)
    if convergent_keys and not mono_ok:
        verdicts.append("MONOTONICITY_AT_NOISE_FLOOR")

    e = {
        "schema_version": "htdt.r130d.original-q0-bandlimited-modal-convergence-evidence-1",
        "preregistered_plan_sha256_lf": _sha(ROOT / PLAN_PATH),
        "preregistered_plan": plan,
        "input_sha256": {"native_modal": _sha(ROOT / NATIVE_PATH), "hybrid_endpoint": _sha(ROOT / HYBRID_PATH)},
        "canonical_full_band_original_authority": "SELF_CONVERGENCE_FAILED",
        "independent_physics": "NOT_VALIDATED",
        "product": "NO_GO",
        "new_solver_runs": 0,
        "new_github_actions_runs": 0,
        "band_limited_scores_are_diagnostics_not_authority": True,
        "original_native_staircase_eigensystem": native_r,
        "exact_geometry_hybrid_eigensystem": hybrid_r,
        "verdicts": verdicts,
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(e, indent=2, allow_nan=False) + "\n", encoding="utf8")
    print(json.dumps({"verdicts": verdicts,
                      "hybrid_all_pairs_pass_cutoffs_hz": convergent_keys,
                      "native_100hz_all_pairs": n100["all_pairs_all_gates"]}, indent=1))


if __name__ == "__main__":
    main()
