# REV36-NITPICK — deferred-work marker sweep

Sweep objective (REV36-NITPICK item 3): find `TODO`/`FIXME`/`HACK`/`XXX`
markers left behind by the REV review waves, fix the cheap ones, and
inventory the rest here.

## Verdict

**Zero actionable markers.** No `TODO`/`FIXME`/`HACK`/`XXX` debt comments
exist anywhere in `backend/src` or `backend/tests`. Every REV-wave
`REVnn[-TRACK]` token found in source comments is a provenance citation
(e.g. `# REV32-TERMS: every dock field explains itself`, `#REV19/D2`),
recording which wave introduced the behaviour — documentation, not
deferred work. This matches the baseline already established by
`round14-final.md` ("No new code TODO/FIXME introduced by the round") and
`rev34-featureaudit.md`.

## Patterns swept

```
grep -rn -i "TODO|FIXME|HACK|XXX"  backend/src backend/tests   # *.py
grep -rn -i "TODO|FIXME|HACK"      .                           # py/md/txt/yaml/yml/toml
grep -rn "NotImplementedError|unreachable" backend/src/htdt    # production paths
grep -rn "^\s*pass\s*$"            backend/src/htdt            # stub bodies
```

## Non-markers (recorded so future sweeps skip them)

| Location | What it actually is |
| --- | --- |
| `backend/src/htdt/analysis_export.py:93` | Docstring literal ``\\uXXXX`` describing JSON escaping |
| `backend/tests/test_capture_binary_formats.py:70` | `b'XXXXXXXX'` placeholder magic bytes in a format test |
| `backend/tests/test_capture_receiver.py:153` | `XXXX-XXXX` in a comment describing the pairing-code shape |
| `backend/src/htdt/palette_search.py:114` | `raise NotImplementedError` — abstract `PaletteProvider.search` contract, overridden by every provider |
| `backend/src/htdt/cad_playback_level_compensation.py:290`, `managed_assets.py:294`, `storage_maintenance.py:478`, `workflow_application.py:923` | fail-closed `unreachable` assertions, not stubs |
| `backend/src/htdt/capture_bundle.py`, `project_bundle.py`, `migration_guard.py` | `except NotImplementedError` — optional-backend capability probing |
| `class *(...): pass` throughout | exception type declarations and `close()` no-ops |

## Follow-ups created

None — there was nothing to fix and nothing to defer.
