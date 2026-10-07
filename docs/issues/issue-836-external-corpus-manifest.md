# #836 Action 1 — sealed external-validation corpus manifest

Issue #836 turns the external-qualification research synthesis into an
executable program. Action 1 is the foundation every later action pins:
a versioned manifest that says *exactly* which external datasets HTDT may
qualify against, at which strength, carrying which claims.

## What this adds (`cad_external_corpus_manifest`)

- `BRAS_V3_ADMISSION` / `BRAS_RS8_ADMISSION` — `ExternalAssetAdmission`
  records (shared #834 ledger vocabulary) pinning both DepositOnce
  datasets by version DOI, record URI, licence, and per-file
  name/size/checksum. No payload is vendored.
- `CorpusScene` — sealed per-scene declaration: dataset pin
  (`AuthorityRef` with hash), the exact source/receiver/material files
  the scene consumes, `reference_strength`
  (`direct_reference`/`supporting`), `expected_validity_band_hz` plus its
  basis, phenomena exercised, and the claims the scene may support.
- `ExternalCorpusManifest` — one sealed, versioned authority over the
  datasets and all 20 scenes; its hash is what #809 preregistrations and
  #801 evidence pin. Rebuilding is deterministic.
- `corpus_fetch_plan` / `verify_fetched_file` +
  `scripts/fetch_external_corpus.py` — the deterministic fetch/import
  command: plan, URIs and checksums all derive from the manifest;
  downloaded bytes that fail size/MD5/SHA-256 verification are deleted,
  not kept; a file with no pinned checksum is `unverifiable`, never
  silently accepted.

## Pinned data

**BRAS v3** — DOI `10.14279/depositonce-6726.3`, handle `11303/7506.3`,
CC BY-SA 4.0 per the record's `dc.rights.uri`. 19 ORIGINAL-bundle files
pinned by publisher MD5 + size; SHA-256 computed on retrieval for every
file except the 4.7 GB `FABIAN_HRIRs` pack (kept as publisher-MD5 pin).

**BRAS RS8** — DOI `10.14279/depositonce-25649`, handle `11303/26816`,
CC BY-SA 4.0. All 4 files carry retrieved SHA-256. The nine measured
configurations are declared as their own scenes
(`RS8_01a/01b/01c/02/03a–03e` — the directory ids inside
`1_Scene_descriptions.zip`), each marked `direct_reference` with
`applicability_limit_evidence`: the paper's ~350 Hz curvature / ~450 Hz
reflector-height criteria are recorded as scene-specific physical limits
in `applicability_notes`, and the validity band (100 Hz–4 kHz) is pinned
to its published basis (the material-data range), not presented as a
solver crossover.

## Structural rules

- `RS1`–`RS7` are `direct_reference` (phenomenon qualification oracles);
  `CR1`–`CR4` are `supporting` — the validator rejects any qualification
  claim on a supporting scene, so the dataset warning ("must not be
  treated as direct reference truth") cannot be edited away.
- `CLAIMS_NEVER_DERIVABLE_FROM_CORPUS` names what no corpus scene can
  ever claim (owned-room validation, production recommendation,
  measured room truth) — those stay behind #813/#801.
- Scenes that resolve to no dataset, pin a stale dataset hash, or list
  files absent from their dataset are rejected at manifest construction.
- `expected_validity_band_hz` is `None` where the dataset publishes no
  bound (v3 scenes): the band then comes from the metric manifest
  (Action 3), not from an invented number.

## Fetch/import

```
python scripts/fetch_external_corpus.py --target-dir <dir> [--dry-run]
python scripts/fetch_external_corpus.py --target-dir <dir> \
    --dataset bras-rs8 [--verify-existing]
```

Writes `<dir>/corpus_fetch_receipt.jsonl` — one verdict line per file,
manifest order. Exit status is non-zero if any file fails verification.

## Deliberately not done (later actions)

- Importer for scene geometry/SOFA RIRs/material payloads (Action 2)
- Per-scene metric manifests (Action 3)
- Solver qualification runs that consume this manifest (Actions 4–6)

Refs #836. Does not close the issue.
