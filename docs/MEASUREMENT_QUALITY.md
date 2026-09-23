# Measurement quality authority — Issue #172

Tracking: Issue #172; evidence-authority resolution extended by #392  
Base: main `c205e4dbb772a8a8efc95b5178655cebfa934f4a`  
Scope: immutable per-measurement quality evidence and downstream capability gates

## Authority boundary

This implementation extends the existing native N60 measurement authority. It does not replace the REW import/measurement engine, `CadMeasurementRecord`, `CadFrequencyResponseDataset`, SceneRevision persistence, O50 MeasurementPlan, or O60 validation/campaign authority.

The quality layer consumes one already-persisted native measurement/dataset and records a separate immutable report. It never infers acquisition facts from frequency/level samples. In particular:

- finite frequency+dB samples do not establish clipping, noise floor, SNR, calibration, common timing, polarity, or IR quality;
- a phase array, including `phase_status=valid`, does not establish a common timing reference;
- missing evidence is `UNKNOWN` or `NOT_EVALUATED`, never implicit `PASS`;
- `FAIL` means contrary evidence exists and remains distinct from missing evidence;
- quality is not reduced to one score.

`CadAcquisitionContextBinding` is an immutable reference (ID + content hash + source kind) to a persisted `CadAcquisitionContext` authority — it is intentionally not a second AcquisitionContext implementation. Since #392 the repository resolves that reference against the persisted authority on save and on every read: a non-`unknown` `source_kind` string is never proof of existence, the context's content hash and source kind must match the binding exactly, the context must list the report's measurement as a subject, and the report's timing evidence must equal the context's attested values verbatim. Timing evidence without a resolvable context — or diverging from the resolved one — fails closed rather than fabricating microphone/AVR state.

The same authority pattern covers the remaining caller-supplied evidence:

- `CadMeasurementObservation` is an immutable per-measurement observation authority (subject measurement, observation timestamp, typed `source_kind`, provenance). A report binds it through `CadMeasurementObservationBinding`; every non-default observation evidence field (clipping, peak, noise floor, signal level, SNR, usable band, polarity, IR window/truncation) must equal the resolved observation verbatim, and the free-form `evidence_source` label must equal the resolved typed `source_kind`. Machine-derived observations (`rew_metadata`, `raw_asset`, `mixed`) must pin the subject measurement's verified raw asset; `manual`/`unknown` observations are themselves the authority and may not bind a source asset.
- `repeatability_rms_db` is recomputed at save and read from the exact immutable repeat datasets via `measurement_repeatability_rms_db` (identical frequency grid and level reference required); a submitted value that diverges from the canonical recomputation is rejected.
- calibration-file claims must resolve to a retained calibration asset (`save_calibration_file`/`validate_calibration_file`) that still passes the managed asset contract — a bare matching hash pair cannot reach calibration PASS.

## Immutable identity

Every `CadMeasurementQualityReport` is bound to:

- measurement ID and deterministic hash of the immutable `CadMeasurementRecord`;
- dataset ID and deterministic hash of the immutable `CadFrequencyResponseDataset`;
- exact raw-asset SHA-256 already used by N60 content-addressed storage;
- document ID, SceneRevision ID/content hash, measurement entity ID, and frozen measurement position;
- optional exact AcquisitionContext ID/hash reference and optional measurement-observation ID/hash reference;
- quality algorithm version plus deterministic semantics SHA-256;
- complete quality profile plus deterministic profile SHA-256;
- complete explicit evidence payload and per-check decisions.

Repository save re-reads the N60 measurement/dataset authority, verifies every binding/hash, resolves the persisted acquisition-context/observation/calibration-file authorities, recomputes derivable repeatability claims from the exact repeat datasets, validates repeat measurements against the same acquisition binding, reconstructs the canonical report from its inputs, and rejects a report whose stored decisions/capabilities do not match the algorithm output. The same resolution and replay run on every read.

Changing a threshold/profile creates another report. Existing reports are append-only and are never rewritten.
Historical reports validate their own stored report/profile hashes and may reopen with the canonical claim subset that existed when they were created; they are not re-evaluated under a newer algorithm merely by reading them. A downstream request for a claim absent from such a historical report resolves to `UNKNOWN`, not an exception or implicit PASS. New saves must match the current algorithm output.

## Quality decisions

The report has independent checks for clipping, noise/SNR, usable frequency band, timing reference, polarity, IR window/truncation, calibration provenance, and repeatability.

Thresholds are never invented by the evaluator. SNR, polarity-confidence, and repeatability thresholds are optional profile fields; when the relevant evidence exists but its threshold is not configured, that check is `NOT_EVALUATED`, never `PASS`.

The evidence contract is explicit rather than inferred from FR samples:

| Quality item | Required evidence / acquisition method | Unit / scope | Profile / threshold | Stored reason | Downstream claim |
| --- | --- | --- | --- | --- | --- |
| Clipping | REW/raw acquisition metadata stating whether clipping occurred; optional peak level | boolean, optional dBFS | algorithm/profile identity is frozen; no FR-derived threshold | explicit no-clipping / clipping / metadata unavailable | supports trust in captured response; failure drives retake |
| Noise / SNR | explicit noise-floor + signal metadata or explicit SNR from acquisition evidence | dB SPL metadata and dB SNR | `minimum_snr_db` | measured SNR vs profile threshold, or unavailable | quality-dependent magnitude/calibration consumers |
| Usable band | explicit quality-qualified low/high frequency limits | Hz band | optional `required_usable_band_hz` plus consumer requested band | coverage pass/fail/unknown | every band-aware downstream claim via `gate_measurement_claim` |
| Timing reference | explicit timing-reference validity, reference ID, clock source, sample rate and delay correction | ID, Hz, seconds | no inference threshold; completeness required | valid / invalid / incomplete | `common_timing`, then `arrival_time` |
| Polarity | explicit polarity observation plus confidence | boolean + 0..1 confidence | `minimum_polarity_confidence` | correct / reversed / confidence insufficient | polarity-sensitive downstream work |
| IR window / truncation | an actual IR plus window bounds and explicit truncation metadata | seconds | algorithm completeness rule | window valid / truncated / incomplete / no IR | `arrival_time`, `decay` |
| Calibration provenance | applied calibration raw-asset SHA and expected calibration SHA; filename is descriptive only | SHA-256 | exact hash equality | match / mismatch / incomplete | `calibrated_response` |
| Repeatability | at least two explicitly linked same-binding measurements plus a repeatability metric | RMS dB | `maximum_repeatability_rms_db` | metric vs profile threshold or not evaluated | `repeatability` |

| Check | PASS evidence | FAIL evidence | Missing / not applicable |
| --- | --- | --- | --- |
| Clipping | explicit metadata says no clipping | explicit clipping indication | UNKNOWN |
| Noise / SNR | explicit SNR meets profile threshold | explicit SNR below threshold | UNKNOWN; FR levels never imply SNR |
| Usable band | explicit usable band satisfies profile band, or an explicit usable band exists when no profile band is required | explicit band fails required profile coverage | UNKNOWN |
| Timing reference | explicit valid reference plus reference ID, clock source, sample rate, and delay correction | explicit invalid timing reference | UNKNOWN |
| Polarity | explicit correct polarity with confidence meeting profile threshold | explicit reversed polarity | UNKNOWN when evidence/confidence is insufficient |
| IR window | IR window bounds present and explicit non-truncation | explicit truncation | NOT_EVALUATED without IR; UNKNOWN when IR metadata is incomplete |
| Calibration | applied calibration SHA equals expected SHA | hashes are both known and mismatch | UNKNOWN when provenance is absent/incomplete |
| Repeatability | >=2 explicit repeat IDs plus RMS within profile threshold | explicit RMS exceeds threshold | NOT_EVALUATED without sufficient repeat evidence |

The retake recommendation is also non-scalar: any explicit quality `FAIL` yields `RETAKE`; otherwise unresolved `UNKNOWN` yields `UNKNOWN`; fully evaluated required checks yield `NOT_NEEDED`. A `NOT_EVALUATED` check does not by itself claim failure.

## Downstream capability matrix

Capabilities use `ALLOWED | BLOCKED | UNKNOWN` and are stored per claim. `gate_measurement_claim(..., required_band_hz=...)` gives #173/#174 a fail-closed, band-aware interface: an allowed base claim becomes UNKNOWN when usable-band evidence is missing and BLOCKED when the explicit usable band does not cover the requested band.

| Claim | ALLOWED requirement | BLOCKED case | UNKNOWN case |
| --- | --- | --- | --- |
| `magnitude_response` | immutable FR magnitude dataset exists | — | required-band query with no usable-band evidence |
| `phase_response` | phase array exists and `phase_status=valid` | phase explicitly absent | phase validity unknown |
| `common_timing` | AcquisitionContext binding plus complete explicit timing-reference evidence | explicit invalid timing reference | missing context/reference metadata |
| `arrival_time` | IR exists, IR window passes, and common timing is allowed | no IR, explicit timing failure, or bad/truncated IR | partial timing/window evidence |
| `decay` | IR exists and IR window passes | no IR or explicit bad/truncated IR | incomplete IR-window evidence |
| `calibrated_response` | no clipping, passing SNR, explicit usable band, matching calibration provenance, and AcquisitionContext binding | explicit clipping/SNR/band/calibration failure | missing capture-quality, calibration, band, or context evidence |
| `repeatability` | explicit repeated measurements and passing repeatability metric | explicit repeatability failure | insufficient repeatability evidence |
| `polarity` | explicit correct polarity with confidence meeting the profile threshold | explicit reversed polarity | missing/low-confidence polarity evidence |

Magnitude inspection remains available for legacy FR-only imports. That does not open phase, arrival, decay, timing, calibrated-response, repeatability, or polarity claims. `calibrated_response` is intentionally stricter than raw magnitude inspection so #173/#174 cannot recommend filters from a calibration-file match while clipping/SNR/band evidence is still missing.

## Retake lineage

A retake remains a separate native Measurement with its own Dataset/raw asset. `CadMeasurementLineageRecord` is append-only and records:

- new measurement ID;
- superseded measurement ID;
- explicitly selected measurement ID;
- reason and creation time;
- deterministic lineage SHA-256.

The repository requires old/new measurements to retain the same document, SceneRevision/content hash, measurement entity/position, channel role, source-speaker IDs, and radiation scope. It never deletes or overwrites the prior Measurement, Dataset, raw asset, or quality report.

Retake records form a single-head chain per measurement binding, enforced under `BEGIN IMMEDIATE`: the superseded measurement must be the current head (each measurement is superseded at most once, so competing retakes of the same head are rejected as stale), and the retake measurement must carry no existing lineage edge (no merges, no cycles). Reads replay the persisted rows as validated chains — payload/column disagreement, dead measurement bindings, forks, and cycles raise `ValueError` — and `selected_measurement_for_lineage` resolves the selection declared by the chain head's record instead of trusting insertion order.

The lineage repository does not update O50 MeasurementPlan or O60 campaign/calibration/holdout assignments. Reassignment, if desired, remains an explicit operation in those existing authorities.

## Persistence

Additive tables live in the same native SQLite database:

- `cad_measurement_quality_reports`
- `cad_measurement_lineage`
- `cad_acquisition_contexts` (#392)
- `cad_measurement_observations` (#392)
- `cad_quality_calibration_files` (#392 — content-addressed retained calibration assets)

The repository passes through the existing native schema compatibility gate before DDL/read/write and uses foreign keys to the existing N60 measurement/dataset/raw-asset rows. Stored payloads deserialize back to frozen Pydantic models; identity hashes are revalidated on reopen.

No GUI, REW capture engine, CalibrationPlan (#173), joint DSP optimization (#174), or owned-room physical acceptance is introduced here.

## Regression fixtures

Focused tests cover:

- FR-only measurement;
- valid phase without common timing evidence;
- missing clipping/noise/SNR metadata;
- complete quality metadata;
- calibration-file mismatch;
- profile/threshold change creating a second report;
- retake retaining old measurement/report and explicit selected lineage;
- claim-level ALLOWED/BLOCKED/UNKNOWN outcomes;
- requested-band gating;
- persistence/reopen and hash-binding rejection.

## Verification and remaining work

Software verification for this change is the focused `test_cad_measurement_quality.py` plus repository CI. Owned-room measurement is not an acceptance prerequisite.

Remaining work is intentionally outside Issue #172:

- #173 consumes the per-claim/band capability gate when building CalibrationPlan authority;
- #174 consumes the same capability interface for joint DSP optimization;
- #392 resolved evidence inputs to typed persisted authorities: acquisition contexts, measurement observations, retained calibration files, and canonical repeatability recomputation; a richer native AcquisitionContext provenance (capture-session export) may later populate `CadAcquisitionContext` directly rather than being recorded manually;
- GUI presentation of quality evidence and retake actions is separate work.
