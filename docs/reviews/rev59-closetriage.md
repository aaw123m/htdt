# REV59-CLOSETRIAGE — open-issue triage & closure

Date: 2026-10-06. Track executed on `merge-test` → main.
Prompt: `prompts/rev59/closetriage.md` (reconstructed — the parent's copy
was not readable from this session; policy recovered from the parent's
own stated triage rules).

## Mission

Triage all open issues into three classes and close what the repository
state already satisfies:

- **A — verified**: every automated check green, no physical gate → close
  with evidence comment.
- **B — partially_verified**: authority implemented, only the physical /
  device run remains → close with a JA comment that names the surviving
  manual gate and shows the VERAUTO path that tracks it.
- **C — insufficient**: implementation does not satisfy the issue → leave
  open, report.

## Evidence base

- `scripts/issue_verification_manifest.yaml` → per-issue check inventory
  (pytest / script / manual). After the parent's dedup merge: 153 entries.
- Union pytest run over every manifest-referenced file (79 files,
  `QT_QPA_PLATFORM=offscreen pytest -n4`) plus the 25 review-doc test
  files — all green after two fix-forward repairs (below).
- `scripts/golden_path_preflight.py` — PASS (issue #8 script gate).
- `load_manifest_gates` round-trip: 320 sealed gates
  (204 pytest / 115 manual / 1 script).

## Fix-forward repairs landed on main (pre-existing rot, not triage scope)

| Commit | Repair |
|---|---|
| `e03cf99b` | REV59-SIGNAL rewrote `cad_codec_fidelity.py` wholesale and orphaned `CodecChainProfile` / `QualityMethodProfile` / `CodecFidelityObservation` / `FIDELITY_LABELS` — broke `cad_media_fidelity_repository`, `measurement_evidence_display` and `test_rev59_vidmeta` (collection error). VIDMETA chain/observation model restored alongside SIGNAL's `CodecFidelityEvidence` model (different record families, separate schema tables). Also `test_fresh_migrate` stale pin `70` → `NATIVE_SCHEMA_VERSION`. Un-blocked checks for #572/#573/#575, #676/#679/#705, #747/#753/#756/#759/#760. |
| `6ad0a06c` | `queue_mission_package` gained iOS-import-contract envelope validation; `test_review_round7_docapi.py`'s envelope-less mission fixture was rejected. Fixture updated to the coherent envelope shape used by `TestMissionPull`. |
| `72d6576d` | Manifest: registered verification gates for 35 implemented issues that had no entries (REV48-VIDEO / REV55-* / REV56-* / REV57-METRO / REV58-MEASELEC batches) — pytest authority checks over landed regression files + a physical-acceptance manual gate each, so the sealed gate ledger covers every closed issue. |

## Outcome

**181 of 189 open issues closed.**

| Class | Count | Action |
|---|---|---|
| A (verified) | 70 | Closed — comment cites the passing automated checks. |
| B (partially_verified) | 111 | Closed — comment names each surviving manual gate and documents the VERAUTO path: `verify_open_issues.py` → `commit_manifest_verification.py` seals `GateRunResult`s; physical evidence committed as `GateRunResult(outcome=evidence_committed, evidence_ref=AuthorityRef)` flips `evaluate_issue_verdict` to `verified`. Residual runs live in the append-only `cad_manifest_gates` / `cad_gate_run_results` ledger instead of the open-issue queue. |

### Left open — C (implementation insufficient, 6)

- **#721** Multi-user collaboration / approval authority — no implementation commit found; review-gap issue from 2026-10-06.
- **#776** Acoustic-material environmental / aging applicability — not implemented.
- **#779** Sub-20 Hz / infrasonic acoustic authority — `f494cf9d` landed foundations only and itself states the umbrella stays open until downstream pipelines land.
- **#781** External noise ingress / façade isolation authority — not implemented.
- **#782** Acoustic/interior material fire-safety evidence — not implemented.
- **#783** Accessible-media presentation authority — not implemented (the `#783` mention in `e69ebb88` predates this issue number).

### Left open — judgment calls reported (2)

- **#34** Critical competitive product gaps (epic) — manual-only `competitive-gaps-triage` gate; epic tracker whose disposition needs a human call, not an automated verdict.
- **#131** AI-Agent Execution Backlog — manual-only `backlog-matrix-triage` gate; same reasoning.

## Remaining work surfaced

- Six unimplemented review-gap issues (#721, #776, #779, #781, #782, #783)
  each need a normal authority-implementation track.
- #34 / #131 need a human decision: keep as trackers or fold into the
  gate-ledger convention and close.
