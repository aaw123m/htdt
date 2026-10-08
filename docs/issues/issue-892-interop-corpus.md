# Issue #892 — Versioned interoperability corpus + semantic round-trip regression harness

## Scope

Each importer/exporter already carries unit tests of its own grammar; what
they cannot catch alone is a semantic drift between them — a version bump, an
optional field, a unit/coordinate convention, a vendor quirk, or a silently
dropped unsupported command turning a real-world round-trip into a lie. #892
adds two things:

1. **A versioned, redistribution-safe corpus**
   (`htdt/interop_corpus_fixtures.py`): every fixture byte string is
   synthesized deterministically in-repo — no third-party file is vendored —
   and sealed into an `InteropCorpusManifest` (`corpus_version`,
   `manifest_sha256`, `icm-` id) whose per-fixture `InteropFixtureEntry`
   (`icf-` id, `fixture_sha256`) declares:

   - provenance (`source_kind`, producer tool/version, licence id,
     redistribution state — unlicensed external payloads must be
     `withheld`, never `redistributable`);
   - `format_family` + `format_version`, `content_sha256` + `size_bytes`
     pin, and the corpus-relative `relative_path`;
   - declared units/coordinate semantics;
   - `expected_supported_features` / `expected_unsupported_features`;
   - the exact `expected_warnings` / `expected_degradations` token sets;
   - canonical `assertions` — stable dotted paths into the lane's observed
     semantics with `equals` / `approx` / `set_equals` / `contains`
     comparators, so a failure names the exact semantic assertion rather
     than only a parser exception;
   - `round_trip_mode`: `round_trip`, `import_only`, or
     `unsupported_assert`.

2. **A semantic regression harness** (`htdt/interop_corpus_harness.py`)
   running the issue's goal line —

   ```
   EXTERNAL FIXTURE → IMPORT → canonical HTDT state → EXPORT →
   RE-IMPORT → SEMANTIC COMPARE
   ```

   for families with a bounded export path, and IMPORT → canonical state →
   declared assertions elsewhere. Comparison is semantic (geometry/topology,
   channel/routing, filter parameters, measurement axes/units/references,
   warning/degradation sets) — never byte equality, which formats admit
   equivalent representations of.

## Verdict vocabulary

| verdict | meaning |
|---|---|
| `semantically_equal` | all assertions hold; observed warning/degradation sets equal the declared sets; round-trip state semantically equal |
| `degraded_as_declared` | same, with a non-empty declared degradation set observed verbatim |
| `unsupported_as_declared` | an `unsupported_assert` fixture whose declared negative verdict was observed exactly |
| `regression` | any violated *or unverifiable* assertion; warning/degradation sets differing from the declaration in either direction; round-trip semantic mismatch; a `qualified` verdict on a declared-unsupported fixture (invented support) |
| `unexpected_failure` | lane raised, fixture drifted from its sealed pin, file missing/unreadable |

Both directions of the warning/degradation comparison are fail-closed: an
undeclared degradation AND a declared degradation that stopped appearing are
each a `regression`. `skipped_unverifiable` outcomes never count toward a
pass. Corpus-level verdict: `passed` / `regression_detected`.

## Families + lanes exercised (corpus v1)

| family | fixture | mode | lane |
|---|---|---|---|
| `rew_text` | synthesized REW-style text measurement | `import_only` | `parse_rew_frequency_response`; axes/units/phase-absence asserted |
| `equalizer_apo` | bounded ECI10 subset config incl. `Device:` scope + an `Eval:` command | `round_trip` | `build_equalizer_apo_artifact` → `render_equalizer_apo_config` → re-import → channel/preamp/delay/band semantic compare |
| `camilladsp` | YAML subset: biquads + Gain step + a mixer | `import_only` | `build_camilladsp_artifact`; mixer stays `opaque:unsupported_command` |
| `clf` | signature-only `.CF1` binary stub | `unsupported_assert` | `qualify_clf` must return `unsupported`/`binary_cf1` — binary families are unsupported by design (#905); a `qualified` verdict here is a regression |
| `ifc_step` | synthesized metric IFC4X3 model, one extruded `IfcSpace` | `round_trip` | `build_ifc_import` → `build_ifc_export_package` → re-import → room geometry/placement compare in metres |
| `htdt_project_bundle` | JSON scene spec materialized to a document | `round_trip` | `export_project_bundle` → `import_project_bundle` → re-export; per-table row sets compared seq-independent; rows minted by import (the project-library registration) are declared additions via `roundtrip.new_tables` |

Declared-degradation examples in corpus v1: APO `device_scope` +
`unsupported_command` opaque retention, CamillaDSP unmapped-channel
diagnostics + mixer opacity, CLF binary non-support.

## Evidence model + integration

- Sealed run records persist through `CadInteropCorpusRepository`
  (native schema v111): `cad_interop_fixture_runs` (`icr-`) and
  `cad_interop_corpus_runs` (`icx-`), each pinning the manifest sha it ran
  against — evidence can never drift silently onto a different corpus
  revision. `_SealedStore` re-verifies seals on save and validates every
  declared column on read; append-only conflict on id+sha divergence.
- Audit wiring: `_RepositoryChain` builds `interop_corpus`;
  `_REPLAY_PROBES` entries re-read both tables canonically;
  `_ROW_BINDINGS` covers every column; JA lifecycle labels registered in
  `application_pages.py`.
- Release verification: manifest class `interop-corpus` (required, dev +
  release profiles) runs `backend/tests/test_issue_892_interop_corpus.py` —
  every fixture in every strategic family is exercised there, so adding a
  format/adapter requires corpus coverage before production support may be
  claimed.

## Explicitly out of scope / honesty bounds

- No licensed third-party payloads (GLL/CLF polar data, vendor files) are
  vendored; their absence is asserted as `unsupported`, not paraphrased as
  compatibility.
- No claims beyond what each lane proves: `import_only` families carry no
  round-trip claim; `unsupported_assert` fixtures prove non-support, not
  compatibility.
- The corpus does not cover producer-version matrices of real device
  firmware files — only the repo-generable envelope above.
