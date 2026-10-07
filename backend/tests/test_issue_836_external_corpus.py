"""#836 Action 1 — sealed external-validation corpus manifest tests."""

from __future__ import annotations

import hashlib

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_external_corpus_manifest import (
    BRAS_RS8_ADMISSION,
    BRAS_V3_ADMISSION,
    CLAIMS_NEVER_DERIVABLE_FROM_CORPUS,
    CORPUS_PERMITTED_CLAIMS,
    EXTERNAL_CORPUS_AUTHORITY_VERSION,
    CorpusFetchStep,
    ExternalCorpusManifest,
    build_corpus_scene,
    corpus_dataset,
    corpus_fetch_plan,
    corpus_scene,
    external_corpus_manifest,
    verify_fetched_file,
)


MANIFEST = external_corpus_manifest()

RS_IDS = {f'RS{i}' for i in range(1, 8)}
CR_IDS = {f'CR{i}' for i in range(1, 5)}
RS8_CONFIG_IDS = {
    'RS8_01a',
    'RS8_01b',
    'RS8_01c',
    'RS8_02',
    'RS8_03a',
    'RS8_03b',
    'RS8_03c',
    'RS8_03d',
    'RS8_03e',
}


def _scene(scene_id: str):
    scene = corpus_scene(MANIFEST, scene_id)
    assert scene is not None, f'missing scene {scene_id}'
    return scene


def test_manifest_is_sealed_and_versioned() -> None:
    assert MANIFEST.manifest_id.startswith('ecm-')
    assert len(MANIFEST.manifest_id) == len('ecm-') + 24
    assert len(MANIFEST.manifest_sha256) == 64
    assert MANIFEST.authority_version == EXTERNAL_CORPUS_AUTHORITY_VERSION
    assert MANIFEST.manifest_version == '1'
    assert MANIFEST.corpus_id == 'bras-external-validation-corpus'
    # Rebuilding the manifest must reproduce the identical seal — the
    # identity is fully determined by the declared content, not a wall
    # clock or call ordering.
    assert (
        external_corpus_manifest().manifest_sha256
        == MANIFEST.manifest_sha256
    )


def test_bras_v3_dataset_identity_and_licence() -> None:
    v3 = corpus_dataset(MANIFEST, 'bras-v3')
    assert v3 is not None
    assert v3.semantic_sha256 == BRAS_V3_ADMISSION.semantic_sha256
    assert v3.version_doi == '10.14279/depositonce-6726.3'
    assert v3.version_record_id == '11303/7506.3'
    assert '38410727-febb-4769-8002-9c710ba393c4' in v3.record_uri
    assert v3.license_id == 'cc-by-sa-4.0'
    assert v3.license_family == 'cc_by_sa'
    assert v3.license_uri == (
        'https://creativecommons.org/licenses/by-sa/4.0/'
    )
    assert v3.admission_state == 'download_on_demand_candidate'
    # Payloads are never vendored: every file is fetched by URI.
    assert all(f.uri.startswith('https://') for f in v3.files)


def test_bras_rs8_dataset_identity_and_licence() -> None:
    rs8 = corpus_dataset(MANIFEST, 'bras-rs8')
    assert rs8 is not None
    assert rs8.semantic_sha256 == BRAS_RS8_ADMISSION.semantic_sha256
    assert rs8.version_doi == '10.14279/depositonce-25649'
    assert rs8.version_record_id == '11303/26816'
    assert '326fed8c-636a-47f2-977d-b4fd6a0ce771' in rs8.record_uri
    assert rs8.license_family == 'cc_by_sa'
    assert rs8.admission_state == 'download_on_demand_candidate'


def test_reference_scenes_rs1_to_rs7_are_direct_reference() -> None:
    for scene_id in sorted(RS_IDS):
        scene = _scene(scene_id)
        assert scene.reference_strength == 'direct_reference'
        assert 'phenomenon_direct_qualification' in scene.permitted_claims
        assert scene.dataset_ref.ref_id == 'bras-v3'
        # Each RS scene consumes its own scene package plus the shared
        # source/receiver/material authorities it was measured against.
        assert f'1_scene_descriptions-{scene_id}.zip' in (
            scene.asset_file_names
        )
        assert '3_surface_descriptions.zip' in scene.asset_file_names
        assert any(
            'source_and_receiver' in name
            for name in scene.asset_file_names
        )


def test_complex_scenes_are_supporting_only() -> None:
    for scene_id in sorted(CR_IDS):
        scene = _scene(scene_id)
        assert scene.reference_strength == 'supporting'
        # The dataset warning is structural: a CR scene can never carry a
        # qualification claim.
        assert set(scene.permitted_claims) <= {
            'room_scale_supporting_comparison',
            'importer_regression',
        }
        assert 'phenomenon_direct_qualification' not in (
            scene.permitted_claims
        )


def test_rs8_nine_configurations_cover_the_curved_lane() -> None:
    declared = {
        scene.scene_id
        for scene in MANIFEST.scenes
        if scene.dataset_ref.ref_id == 'bras-rs8'
    }
    assert declared == RS8_CONFIG_IDS
    for scene_id in sorted(RS8_CONFIG_IDS):
        scene = _scene(scene_id)
        assert scene.reference_strength == 'direct_reference'
        assert 'applicability_limit_evidence' in scene.permitted_claims
        assert scene.expected_validity_band_hz == (100.0, 4000.0)
        assert scene.validity_band_basis
        assert '350' in scene.applicability_notes
        assert '450' in scene.applicability_notes


def test_every_pinned_asset_carries_a_checksum() -> None:
    # No anonymous benchmark constants: every file the corpus can fetch
    # is hash-pinned.
    for dataset in MANIFEST.datasets:
        assert dataset.files
        for file in dataset.files:
            assert file.md5 is not None or file.sha256 is not None, (
                f'{file.file_name} carries no checksum'
            )
            assert file.checksum_source in ('publisher', 'computed')
            if file.sha256 is None:
                # un-retrieved payloads must say so instead of
                # fabricating a hash
                assert file.md5 is not None
                assert file.checksum_source == 'publisher'


def test_retrieved_sha256_pins_are_real() -> None:
    # Files hashed on retrieval carry BOTH the publisher MD5 and the
    # computed SHA-256 — cross-checked, distinct values.
    computed = [
        f
        for dataset in MANIFEST.datasets
        for f in dataset.files
        if f.checksum_source == 'computed'
    ]
    assert len(computed) >= 20  # all of RS8 + all of v3 minus FABIAN_HRIRs
    seen: set[str] = set()
    for file in computed:
        assert file.md5 is not None
        assert file.sha256 is not None
        assert file.sha256 != file.md5
        assert file.sha256 not in seen
        seen.add(file.sha256)


def test_supporting_scene_rejects_qualification_claim() -> None:
    ref = AuthorityRef(
        kind='external_asset_admission',
        ref_id='bras-v3',
        ref_sha256=BRAS_V3_ADMISSION.semantic_sha256,
    )
    with pytest.raises(ValueError, match='never direct oracles'):
        build_corpus_scene(
            scene_id='FAKE',
            dataset_ref=ref,
            title='supporting scene claiming qualification',
            phenomenon_ids=('x',),
            reference_strength='supporting',
            permitted_claims=('phenomenon_direct_qualification',),
            asset_file_names=('Documentation.pdf',),
        )


def test_claim_vocabulary_excludes_owned_room_and_production() -> None:
    # Claims an external corpus can never produce are not even in the
    # permitted vocabulary — no scene can carry them.
    assert not (
        CORPUS_PERMITTED_CLAIMS & set(CLAIMS_NEVER_DERIVABLE_FROM_CORPUS)
    )
    for scene in MANIFEST.scenes:
        assert set(scene.permitted_claims) <= CORPUS_PERMITTED_CLAIMS


def test_scene_rejects_unknown_dataset_and_stale_pin() -> None:
    ref = AuthorityRef(
        kind='external_asset_admission',
        ref_id='not-a-dataset',
        ref_sha256='0' * 64,
    )
    scene = build_corpus_scene(
        scene_id='X1',
        dataset_ref=ref,
        title='orphan scene',
        phenomenon_ids=('x',),
        reference_strength='direct_reference',
        permitted_claims=('importer_regression',),
        asset_file_names=('a.zip',),
    )
    with pytest.raises(ValueError, match='unknown dataset'):
        ExternalCorpusManifest.create(
            manifest_version='1',
            corpus_id='c',
            issued_on='2026-10-07',
            retrieved_on='2026-10-07',
            datasets=MANIFEST.datasets,
            scenes=(*MANIFEST.scenes, scene),
        )
    stale = AuthorityRef(
        kind='external_asset_admission',
        ref_id='bras-v3',
        ref_sha256='0' * 64,
    )
    scene = build_corpus_scene(
        scene_id='X2',
        dataset_ref=stale,
        title='stale pin scene',
        phenomenon_ids=('x',),
        reference_strength='direct_reference',
        permitted_claims=('importer_regression',),
        asset_file_names=('Documentation.pdf',),
    )
    with pytest.raises(ValueError, match='stale dataset'):
        ExternalCorpusManifest.create(
            manifest_version='1',
            corpus_id='c',
            issued_on='2026-10-07',
            retrieved_on='2026-10-07',
            datasets=MANIFEST.datasets,
            scenes=(scene,),
        )


def test_scene_rejects_assets_absent_from_dataset() -> None:
    ref = AuthorityRef(
        kind='external_asset_admission',
        ref_id='bras-rs8',
        ref_sha256=BRAS_RS8_ADMISSION.semantic_sha256,
    )
    scene = build_corpus_scene(
        scene_id='X3',
        dataset_ref=ref,
        title='bad asset scene',
        phenomenon_ids=('x',),
        reference_strength='direct_reference',
        permitted_claims=('importer_regression',),
        asset_file_names=('not_in_dataset.zip',),
    )
    with pytest.raises(ValueError, match='not present'):
        ExternalCorpusManifest.create(
            manifest_version='1',
            corpus_id='c',
            issued_on='2026-10-07',
            retrieved_on='2026-10-07',
            datasets=MANIFEST.datasets,
            scenes=(scene,),
        )


def test_scene_seal_detects_tamper() -> None:
    scene = _scene('RS1')
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        type(scene)(
            **{
                **scene.model_dump(mode='python'),
                'permitted_claims': ('importer_regression',),
            }
        )


def test_manifest_seal_detects_tamper() -> None:
    with pytest.raises(ValueError, match='manifest hash mismatch'):
        ExternalCorpusManifest(
            **{
                **MANIFEST.model_dump(mode='python'),
                'corpus_id': 'forged-corpus',
            }
        )


def test_dataset_pin_requires_sha256() -> None:
    with pytest.raises(ValueError, match='pin its sha256'):
        build_corpus_scene(
            scene_id='X4',
            dataset_ref=AuthorityRef(
                kind='external_asset_admission', ref_id='bras-v3'
            ),
            title='unpinned scene',
            phenomenon_ids=('x',),
            reference_strength='direct_reference',
            permitted_claims=('importer_regression',),
            asset_file_names=('Documentation.pdf',),
        )


def test_fetch_plan_is_deterministic_and_complete() -> None:
    plan = corpus_fetch_plan(MANIFEST)
    assert plan == corpus_fetch_plan(MANIFEST)
    planned = [(s.admission_id, s.file_name) for s in plan]
    expected = [
        (dataset.admission_id, f.file_name)
        for dataset in MANIFEST.datasets
        for f in dataset.files
    ]
    assert planned == expected
    # Restricting to one dataset yields only that dataset's steps.
    rs8_only = corpus_fetch_plan(MANIFEST, ('bras-rs8',))
    assert {s.admission_id for s in rs8_only} == {'bras-rs8'}
    assert len(rs8_only) == len(BRAS_RS8_ADMISSION.files)
    for step in plan:
        assert step.target_relpath.endswith('/' + step.file_name)
        assert step.uri.startswith('https://')
        assert step.md5 is not None or step.sha256 is not None


def _step_for(**over) -> CorpusFetchStep:
    kwargs = dict(
        admission_id='ds',
        file_name='f.bin',
        uri='https://example.invalid/f.bin',
        target_relpath='ds/f.bin',
        size_bytes=5,
        md5=hashlib.md5(b'hello').hexdigest(),
        sha256=hashlib.sha256(b'hello').hexdigest(),
        checksum_source='computed',
    )
    kwargs.update(over)
    return CorpusFetchStep(**kwargs)


def test_verify_fetched_file(tmp_path) -> None:
    target = tmp_path / 'f.bin'
    target.write_bytes(b'hello')
    receipt = verify_fetched_file(_step_for(), target)
    assert receipt.verdict == 'verified'
    assert receipt.computed_sha256 == hashlib.sha256(b'hello').hexdigest()


def test_verify_fetched_file_fail_closed(tmp_path) -> None:
    missing = verify_fetched_file(_step_for(), tmp_path / 'absent.bin')
    assert missing.verdict == 'missing'

    wrong_size = tmp_path / 'short.bin'
    wrong_size.write_bytes(b'hell')
    receipt = verify_fetched_file(_step_for(), wrong_size)
    assert receipt.verdict == 'size_mismatch'

    same_size_wrong_md5 = tmp_path / 'same.bin'
    same_size_wrong_md5.write_bytes(b'HELLO')
    receipt = verify_fetched_file(_step_for(), same_size_wrong_md5)
    assert receipt.verdict == 'md5_mismatch'

    sha_only = _step_for(md5=None)
    receipt = verify_fetched_file(sha_only, same_size_wrong_md5)
    assert receipt.verdict == 'sha256_mismatch'

    no_checksum = _step_for(
        md5=None, sha256=None, checksum_source='none'
    )
    ok_file = tmp_path / 'ok.bin'
    ok_file.write_bytes(b'hello')
    receipt = verify_fetched_file(no_checksum, ok_file)
    assert receipt.verdict == 'unverifiable'


def test_no_aggregate_or_anonymous_truth() -> None:
    # Every scene names phenomena, claims and assets explicitly — nothing
    # is a bare dataset-level "validated" flag.
    for scene in MANIFEST.scenes:
        assert scene.phenomenon_ids
        assert scene.permitted_claims
        assert scene.asset_file_names
        assert scene.dataset_ref.ref_sha256 is not None
