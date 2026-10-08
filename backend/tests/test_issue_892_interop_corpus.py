"""Issue #892 — versioned interoperability corpus + semantic round-trip
regression harness.

Covers every verdict path (``semantically_equal``,
``degraded_as_declared``, ``unsupported_as_declared``, ``regression``,
``unexpected_failure``), seal/id integrity on every record type, the
fail-closed warning/degradation set rule in both directions, and the
``_SealedStore`` repository round-trip + tamper detection.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration_deployment_repository import (
    DeploymentConflictError,
    DeploymentIntegrityError,
)
from htdt.cad_interop_corpus import (
    InteropAssertionOutcome,
    InteropCorpusManifest,
    InteropFixtureEntry,
    InteropFixtureRunRecord,
    InteropSemanticAssertion,
    build_interop_fixture_entry,
    build_interop_fixture_run,
)
from htdt.cad_interop_corpus_repository import (
    CadInteropCorpusRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.interop_corpus_fixtures import (
    INTEROP_CORPUS_VERSION,
    build_default_corpus_manifest,
    default_fixture_bytes,
    materialize_corpus,
)
from htdt.interop_corpus_harness import (
    INTEROP_HARNESS_VERSION,
    compute_fixture_verdict,
    evaluate_assertion,
    run_interop_corpus,
    run_interop_fixture,
)

_TS = '2026-10-08T00:00:00+00:00'
_TS_END = '2026-10-08T00:01:00+00:00'


def _fixture(family: str) -> InteropFixtureEntry:
    manifest = build_default_corpus_manifest()
    return next(f for f in manifest.fixtures if f.format_family == family)


def _corpus(tmp_path: Path):
    manifest = build_default_corpus_manifest()
    corpus_dir = tmp_path / 'corpus'
    materialize_corpus(manifest, corpus_dir)
    run, runs = run_interop_corpus(
        manifest,
        corpus_dir,
        document_id='doc-892',
        work_dir=tmp_path / 'work',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    return manifest, run, runs


# ---------------------------------------------------------------------------
# Manifest / fixture entry sealing
# ---------------------------------------------------------------------------


def test_manifest_is_sealed_and_versioned() -> None:
    manifest = build_default_corpus_manifest()
    assert manifest.corpus_version == INTEROP_CORPUS_VERSION
    assert manifest.manifest_id == f'icm-{manifest.manifest_sha256[:24]}'
    for fixture in manifest.fixtures:
        assert fixture.fixture_id == f'icf-{fixture.fixture_sha256[:24]}'
    # Rebuilding the corpus must be byte-stable — the versioned manifest
    # is reproducible, not random.
    manifest_b = build_default_corpus_manifest()
    assert manifest_b.manifest_sha256 == manifest.manifest_sha256


def test_fixture_entry_rejects_tamper() -> None:
    fixture = _fixture('rew_text')
    payload = fixture.model_dump(mode='python')
    payload['format_version'] = 'tampered'
    with pytest.raises(ValueError, match='hash mismatch'):
        InteropFixtureEntry(**payload)


def test_manifest_rejects_tamper() -> None:
    manifest = build_default_corpus_manifest()
    payload = manifest.model_dump(mode='python')
    payload['corpus_version'] = '99'
    with pytest.raises(ValueError, match='manifest hash mismatch'):
        InteropCorpusManifest(**payload)


def test_unlicensed_external_fixture_must_be_withheld() -> None:
    fixture = _fixture('rew_text')
    with pytest.raises(ValueError, match='withheld'):
        build_interop_fixture_entry(
            format_family='rew_text',
            format_version='rew-text-1',
            relative_path='x.txt',
            content_sha256='0' * 64,
            size_bytes=1,
            round_trip_mode='import_only',
            provenance={
                'source_kind': 'external_no_license',
                'producer_tool': 'x',
                'producer_version': '1',
                'licence_id': 'unknown',
                'redistribution_state': 'redistributable',
            },
        )


def test_every_fixture_covers_a_strategic_family_and_declares_state() -> None:
    manifest = build_default_corpus_manifest()
    families = {f.format_family for f in manifest.fixtures}
    assert families == {
        'rew_text',
        'equalizer_apo',
        'camilladsp',
        'clf',
        'ifc_step',
        'htdt_project_bundle',
    }
    for fixture in manifest.fixtures:
        # Provenance + licence/redistribution declared on every file.
        assert fixture.provenance.source_kind == 'repo_synthesized'
        assert fixture.provenance.redistribution_state == 'repo_synthesized'
        assert fixture.content_sha256 == hashlib.sha256(
            default_fixture_bytes()[fixture.relative_path]
        ).hexdigest()
        assert fixture.assertions  # semantic assertions, not just parse


# ---------------------------------------------------------------------------
# End-to-end corpus run — all six family lanes
# ---------------------------------------------------------------------------


def test_corpus_run_green_and_verdicts(tmp_path: Path) -> None:
    manifest, run, runs = _corpus(tmp_path)
    assert run.verdict == 'passed'
    assert run.corpus_run_id == f'icx-{run.corpus_run_sha256[:24]}'
    assert run.manifest_ref.ref_sha256 == manifest.manifest_sha256
    verdicts = {r.format_family: r.verdict for r in runs}
    assert verdicts == {
        'rew_text': 'semantically_equal',
        'equalizer_apo': 'degraded_as_declared',
        'camilladsp': 'degraded_as_declared',
        'clf': 'unsupported_as_declared',
        'ifc_step': 'semantically_equal',
        'htdt_project_bundle': 'semantically_equal',
    }
    assert run.fixture_run_ids == tuple(r.run_id for r in runs)
    # CLF binary is a PASS only in the declared-negative sense — never
    # a semantic-compatibility pass.
    clf_run = next(r for r in runs if r.format_family == 'clf')
    assert clf_run.verdict == 'unsupported_as_declared'


def test_fixture_drift_is_unexpected_failure(tmp_path: Path) -> None:
    fixture = _fixture('rew_text')
    drifted = default_fixture_bytes()[fixture.relative_path] + b' '
    run = run_interop_fixture(
        fixture,
        drifted,
        document_id='doc',
        manifest_sha256='0' * 64,
        corpus_version='1',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    assert run.verdict == 'unexpected_failure'
    assert 'drifted' in run.detail


def test_missing_fixture_file_is_unexpected_failure(tmp_path: Path) -> None:
    manifest = build_default_corpus_manifest()
    corpus_dir = tmp_path / 'corpus'
    materialize_corpus(manifest, corpus_dir)
    (corpus_dir / 'rew/mlp-measurement.txt').unlink()
    run, runs = run_interop_corpus(
        manifest,
        corpus_dir,
        document_id='doc',
        work_dir=tmp_path / 'w',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    rew = next(r for r in runs if r.format_family == 'rew_text')
    assert rew.verdict == 'unexpected_failure'
    assert run.verdict == 'regression_detected'


def test_lane_exception_is_unexpected_failure(tmp_path: Path) -> None:
    # A fixture entry whose own pin matches broken bytes exercises the
    # lane-failure path: a raising lane can never read as a pass.
    fixture = _fixture('ifc_step')
    broken = b'not a STEP file at all'
    rebuilt = build_interop_fixture_entry(
        format_family=fixture.format_family,
        format_version=fixture.format_version,
        relative_path=fixture.relative_path,
        content_sha256=hashlib.sha256(broken).hexdigest(),
        size_bytes=len(broken),
        round_trip_mode=fixture.round_trip_mode,
        provenance=fixture.provenance,
        units_semantics=fixture.units_semantics,
        coordinate_semantics=fixture.coordinate_semantics,
        expected_supported_features=fixture.expected_supported_features,
        expected_unsupported_features=fixture.expected_unsupported_features,
        expected_warnings=fixture.expected_warnings,
        expected_degradations=fixture.expected_degradations,
        assertions=fixture.assertions,
    )
    run = run_interop_fixture(
        rebuilt,
        broken,
        document_id='doc',
        manifest_sha256='0' * 64,
        corpus_version='1',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    assert run.verdict == 'unexpected_failure'
    assert 'lane raised' in run.detail


def test_regression_when_declared_assertion_violates(tmp_path: Path) -> None:
    fixture = _fixture('rew_text')
    tampered = build_interop_fixture_entry(
        format_family=fixture.format_family,
        format_version=fixture.format_version,
        relative_path=fixture.relative_path,
        content_sha256=fixture.content_sha256,
        size_bytes=fixture.size_bytes,
        round_trip_mode=fixture.round_trip_mode,
        provenance=fixture.provenance,
        units_semantics=fixture.units_semantics,
        coordinate_semantics=fixture.coordinate_semantics,
        expected_supported_features=fixture.expected_supported_features,
        expected_unsupported_features=fixture.expected_unsupported_features,
        expected_warnings=fixture.expected_warnings,
        expected_degradations=fixture.expected_degradations,
        assertions=(
            InteropSemanticAssertion(
                path='row_count', expected=999),
        ),
    )
    run = run_interop_fixture(
        tampered,
        default_fixture_bytes()[fixture.relative_path],
        document_id='doc',
        manifest_sha256='0' * 64,
        corpus_version='1',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    assert run.verdict == 'regression'
    violated = next(o for o in run.outcomes if o.state == 'violated')
    assert violated.path == 'row_count'  # exact assertion identified


def test_undeclared_warning_is_regression(tmp_path: Path) -> None:
    fixture = _fixture('camilladsp')
    # Drop a declared warning: if the lane observes it anyway, the
    # declaration is stale -> regression (silence is never a pass).
    tampered = build_interop_fixture_entry(
        format_family=fixture.format_family,
        format_version=fixture.format_version,
        relative_path=fixture.relative_path,
        content_sha256=fixture.content_sha256,
        size_bytes=fixture.size_bytes,
        round_trip_mode=fixture.round_trip_mode,
        provenance=fixture.provenance,
        units_semantics=fixture.units_semantics,
        coordinate_semantics=fixture.coordinate_semantics,
        expected_supported_features=fixture.expected_supported_features,
        expected_unsupported_features=fixture.expected_unsupported_features,
        expected_warnings=(),
        expected_degradations=fixture.expected_degradations,
        assertions=fixture.assertions,
    )
    run = run_interop_fixture(
        tampered,
        default_fixture_bytes()[fixture.relative_path],
        document_id='doc',
        manifest_sha256='0' * 64,
        corpus_version='1',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    assert run.verdict == 'regression'
    assert set(run.observed_warnings) != set()


def test_supported_parse_on_unsupported_assert_is_regression() -> None:
    # A licensed-binary fixture asserting 'unsupported' that somehow
    # qualifies is invented support — the worst failure mode.
    fixture = _fixture('clf')
    entry = InteropFixtureEntry.model_validate(
        fixture.model_dump(mode='python')
    )
    lane_verdict_holds = (
        InteropAssertionOutcome(
            path='verdict', comparator='equals', state='holds'
        ),
    )

    class _Lane:
        observed = {'verdict': 'qualified'}
        warnings: tuple = ()
        degradations: tuple = ()
        round_trip_equal = None

    assert (
        compute_fixture_verdict(entry, lane_verdict_holds, _Lane())
        == 'regression'
    )


def test_absent_observed_value_is_not_a_pass() -> None:
    fixture = _fixture('rew_text')
    outcome = evaluate_assertion(
        InteropSemanticAssertion(path='nonexistent.path', expected=1),
        {},
    )
    assert outcome.state == 'skipped_unverifiable'


def test_approx_and_set_comparators() -> None:
    holds = evaluate_assertion(
        InteropSemanticAssertion(
            path='x', comparator='approx', expected=1.0, tolerance=0.01
        ),
        {'x': 1.005},
    )
    assert holds.state == 'holds'
    bad = evaluate_assertion(
        InteropSemanticAssertion(
            path='x', comparator='approx', expected=1.0, tolerance=0.01
        ),
        {'x': 'text'},
    )
    assert bad.state == 'violated'
    sets = evaluate_assertion(
        InteropSemanticAssertion(
            path='s', comparator='set_equals', expected=['a', 'b']
        ),
        {'s': ('b', 'a')},
    )
    assert sets.state == 'holds'


# ---------------------------------------------------------------------------
# Repository round-trip + tamper detection
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> CadInteropCorpusRepository:
    scene = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    return CadInteropCorpusRepository(scene)


def test_repository_round_trip(tmp_path: Path) -> None:
    manifest, run, runs = _corpus(tmp_path)
    repo = _repo(tmp_path / 'db')
    for record in runs:
        repo.save_fixture_run(record)
    repo.save_corpus_run(run)

    for record in runs:
        loaded = repo.get_fixture_run(record.run_id)
        assert loaded == record
    assert repo.get_corpus_run(run.corpus_run_id) == run
    listed = repo.list_fixture_runs('doc-892')
    assert {r.run_id for r in listed} == {r.run_id for r in runs}
    assert repo.list_corpus_runs('doc-892') == (run,)


def test_repository_tamper_detection(tmp_path: Path) -> None:
    _manifest, run, runs = _corpus(tmp_path)
    repo = _repo(tmp_path / 'db')
    repo.save_fixture_run(runs[0])
    repo.save_corpus_run(run)

    # Flip a declared column — read-time integrity must catch it.
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_interop_fixture_runs SET verdict=? WHERE run_id=?',
            ('regression', runs[0].run_id),
        )
        connection.commit()
    with pytest.raises(DeploymentIntegrityError):
        repo.get_fixture_run(runs[0].run_id)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_interop_fixture_runs SET verdict=? WHERE run_id=?',
            (runs[0].verdict, runs[0].run_id),
        )
        connection.execute(
            'UPDATE cad_interop_corpus_runs SET manifest_sha256=? '
            'WHERE corpus_run_id=?',
            ('f' * 64, run.corpus_run_id),
        )
        connection.commit()
    with pytest.raises(DeploymentIntegrityError):
        repo.get_corpus_run(run.corpus_run_id)


def test_repository_append_only_conflict(tmp_path: Path) -> None:
    _manifest, _run, runs = _corpus(tmp_path)
    repo = _repo(tmp_path / 'db')
    repo.save_fixture_run(runs[0])
    repo.save_fixture_run(runs[0])  # idempotent re-save
    forged = build_interop_fixture_run(
        document_id='doc-892',
        fixture_ref=AuthorityRef(
            kind='interop_fixture',
            ref_id='other',
            ref_sha256='0' * 64,
        ),
        manifest_sha256='1' * 64,
        corpus_version='1',
        harness_version=INTEROP_HARNESS_VERSION,
        format_family='rew_text',
        round_trip_mode='import_only',
        verdict='regression',
        started_at_utc=_TS,
        finished_at_utc=_TS_END,
    )
    # Forging the same id under a different sha is an append-only conflict
    # (the store re-verifies the seal, so the forged record raises on the
    # id-vs-sha check before it can overwrite anything).
    forged = type(runs[0]).model_construct(
        **{**forged.model_dump(mode='python'), 'run_id': runs[0].run_id}
    )
    with pytest.raises(DeploymentIntegrityError):
        repo.save_fixture_run(forged)


def test_run_records_pin_their_manifest(tmp_path: Path) -> None:
    manifest, run, runs = _corpus(tmp_path)
    for record in runs:
        assert record.manifest_sha256 == manifest.manifest_sha256
        assert record.fixture_ref.ref_sha256
        assert len(record.fixture_ref.ref_sha256) == 64
    assert run.manifest_ref.kind == 'interop_corpus_manifest'
    assert run.manifest_ref.ref_id == manifest.manifest_id


def test_run_record_seal_rejects_tamper(tmp_path: Path) -> None:
    _m, _run, runs = _corpus(tmp_path)
    record = runs[0]
    payload = record.model_dump(mode='python')
    payload['verdict'] = 'regression'
    with pytest.raises(ValueError, match='run hash mismatch'):
        InteropFixtureRunRecord(**payload)
