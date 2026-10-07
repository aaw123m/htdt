"""REV63 #805 — standards coverage gap matrix + extended built-in criteria.

The gap matrix is a sealed, versioned record that enumerates every
criterion HTDT intends to cover for each built-in standard and its exact
state: implemented (pinned per profile revision), evidence_missing, or
unsupported. These tests cover every criterion state, profile-version
immutability, fail-closed UNKNOWN semantics, mandatory source/revision
validation, predicted-vs-measured evidence separation, applicability
scoping, and sealed repository round-trip + tamper detection.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
    quaternion_from_euler_deg,
)
from htdt.cad_standards import (
    CriterionEvidenceRef,
    CriterionObservation,
    CriterionSource,
    StandardsEvaluationTarget,
    evaluate_standards_profile,
)
from htdt.cad_standards_gap_matrix import (
    BUILTIN_GAP_MATRIX_VERSION,
    GapMatrixCriterion,
    GapMatrixImplementedRef,
    GapMatrixStandard,
    StandardsGapMatrix,
    build_standards_gap_matrix,
    builtin_standards_gap_matrix,
    validate_gap_matrix,
)
from htdt.cad_standards_authorities import (
    builtin_standards_source_authorities,
)
from htdt.cad_standards_profiles import (
    builtin_standards_profiles,
    dolby_atmos_home_5_1_2_profile,
    dolby_atmos_home_5_1_2_profile_v2,
    rp22_performance_profile,
)
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.standards_workspace import StandardsWorkspaceModel


NOW = '2026-10-07T00:00:00+00:00'
PROFILES = builtin_standards_profiles()
MATRIX = builtin_standards_gap_matrix(PROFILES, created_at_utc=NOW)

SOURCE = CriterionSource(
    publisher='Fixture publisher',
    document_title='Fixture criteria',
    document_version='1.0',
    reference='Fixture §1',
)


def _matrix_entry(criterion_id: str) -> GapMatrixCriterion:
    for standard in MATRIX.standards:
        entry = standard.criterion(criterion_id)
        if entry is not None:
            return entry
    raise AssertionError(criterion_id)


def _target(
    *,
    revision,
    domains: tuple[str, ...] = ('room',),
) -> StandardsEvaluationTarget:
    return StandardsEvaluationTarget(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        applicable_domains=domains,
    )


def _seat(entity_id: str, *, x: float, y: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=0.55),
        orientation=quaternion_from_euler_deg(
            yaw_deg=180.0, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.1),
        acoustic_reference_offset_m=Offset3(z_m=0.65),
    )


def _speaker(
    entity_id: str,
    role: str,
    *,
    x: float,
    y: float,
    z: float,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        speaker_role=role,
        position=Position3(x_m=x, y_m=y, z_m=z),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


def _result(evaluation, criterion_id: str):
    for result in evaluation.results:
        if result.criterion_id == criterion_id:
            return result
    raise AssertionError(criterion_id)


# ---------------------------------------------------------------------------
# Gap-matrix coverage content
# ---------------------------------------------------------------------------


def test_gap_matrix_covers_every_rp22_parameter() -> None:
    standard = MATRIX.standard('cedia-cta-rp22-v1.2')
    assert standard is not None
    indexed = {
        criterion.criterion_id.split('.')[1]: criterion
        for criterion in standard.criteria
    }
    for index in range(1, 22):
        key = f'p{index:02d}'
        assert key in indexed, key

    # Parameter 2 has no honest single boundary — evidence_missing, and it
    # names the missing design-intent authority rather than hiding.
    p2 = indexed['p02']
    assert p2.state == 'evidence_missing'
    assert p2.missing_evidence == (
        'declared-immersive-format-design-intent-v1',
    )
    assert 'Auro' in p2.state_reason

    # The other twenty parameters are implemented with exact revision pins.
    implemented = [
        criterion
        for criterion in standard.criteria
        if criterion.state == 'implemented'
    ]
    assert len(implemented) == 20
    for entry in implemented:
        assert entry.implemented
        for ref in entry.implemented:
            assert ref.criterion_id == entry.criterion_id
            assert ref.profile_id.startswith('cedia-cta-rp22-')
            assert ref.criterion_sha256


def test_gap_matrix_declares_every_standard_family() -> None:
    ids = {standard.standard_id for standard in MATRIX.standards}
    assert ids == {
        'cedia-cta-rp22-v1.2',
        'dolby-atmos-home-r3.1',
        'auro-3d-home-rev12',
        'dts-x-home',
        'theater-viewing-screen',
    }

    states = {criterion.state for criterion in MATRIX.criteria()}
    assert states == {'implemented', 'evidence_missing', 'unsupported'}

    dtsx = MATRIX.standard('dts-x-home')
    assert dtsx is not None
    assert all(
        criterion.state == 'unsupported' for criterion in dtsx.criteria
    )

    # New Dolby prov2 criterion is in the matrix pinned to both
    # semantic content and the exact profile revision.
    elevation = _matrix_entry('dolby.5.1.2.top-middle-overhead-elevation')
    assert elevation.state == 'implemented'
    assert {
        ref.profile_version for ref in elevation.implemented
    } == {'r3.1-2018-12-13-prov2'}


def test_gap_matrix_validate_fail_closed() -> None:
    assert validate_gap_matrix(MATRIX, profiles=PROFILES) == ()

    # A matrix that omits an encoded criterion is rejected.
    rp22 = MATRIX.standard('cedia-cta-rp22-v1.2')
    assert rp22 is not None
    trimmed = GapMatrixStandard(
        standard_id=rp22.standard_id,
        name=rp22.name,
        intended_profile_ids=rp22.intended_profile_ids,
        criteria=tuple(
            criterion
            for criterion in rp22.criteria
            if criterion.criterion_id != 'rp22.p01.listener-boundary-distance'
        ),
    )
    broken = build_standards_gap_matrix(
        standards=(
            trimmed,
            *(
                standard
                for standard in MATRIX.standards
                if standard.standard_id != rp22.standard_id
            ),
        ),
        matrix_version='broken-1',
        created_at_utc=NOW,
    )
    errors = validate_gap_matrix(broken, profiles=PROFILES)
    assert any('absent from the gap matrix' in error for error in errors)

    # A ref pinning the wrong criterion hash is rejected.
    entry = _matrix_entry('rp22.p01.listener-boundary-distance')
    forged = entry.model_copy(
        update={
            'implemented': (
                GapMatrixImplementedRef(
                    profile_id=entry.implemented[0].profile_id,
                    profile_version=entry.implemented[0].profile_version,
                    criterion_id=entry.criterion_id,
                    criterion_sha256='0' * 64,
                ),
                *entry.implemented[1:],
            )
        }
    )
    forged_standard = GapMatrixStandard(
        standard_id=rp22.standard_id,
        name=rp22.name,
        intended_profile_ids=rp22.intended_profile_ids,
        criteria=tuple(
            forged if criterion.criterion_id == entry.criterion_id
            else criterion
            for criterion in rp22.criteria
        ),
    )
    forged_matrix = build_standards_gap_matrix(
        standards=(
            forged_standard,
            *(
                standard
                for standard in MATRIX.standards
                if standard.standard_id != rp22.standard_id
            ),
        ),
        matrix_version='broken-2',
        created_at_utc=NOW,
    )
    errors = validate_gap_matrix(forged_matrix, profiles=PROFILES)
    assert any('criterion sha256' in error for error in errors)


def test_gap_matrix_seal_integrity() -> None:
    payload = MATRIX.model_dump(mode='json')
    rebuilt = StandardsGapMatrix.model_validate(payload)
    assert rebuilt == MATRIX

    tampered = dict(payload)
    tampered['matrix_version'] = 'forged'
    with pytest.raises(ValidationError):
        StandardsGapMatrix.model_validate(tampered)

    tampered_id = dict(payload)
    tampered_id['matrix_id'] = 'sgm-' + 'f' * 24
    with pytest.raises(ValidationError):
        StandardsGapMatrix.model_validate(tampered_id)


def test_gap_matrix_source_and_state_mandatory() -> None:
    # A criterion without a source record cannot exist — pydantic rejects
    # the missing field outright.
    with pytest.raises(ValidationError):
        GapMatrixCriterion(
            criterion_id='un sourced',
            name='unsourced',
            applicable_domains=('room',),
            state='unsupported',
            state_reason='x',
        )

    # 'implemented' without a pinned revision is rejected.
    with pytest.raises(ValueError, match='at least one'):
        GapMatrixCriterion(
            criterion_id='fake-implemented',
            name='fake',
            source=SOURCE,
            applicable_domains=('room',),
            state='implemented',
            state_reason='x',
            quantity='q',
            unit='m',
        )

    # 'evidence_missing' must name the missing evidence authority.
    with pytest.raises(ValueError, match='name the evidence'):
        GapMatrixCriterion(
            criterion_id='fake-missing',
            name='fake',
            source=SOURCE,
            applicable_domains=('room',),
            state='evidence_missing',
            state_reason='x',
        )

    # A non-implemented entry carrying refs is rejected.
    with pytest.raises(ValueError, match='must not carry'):
        GapMatrixCriterion(
            criterion_id='fake-unsupported',
            name='fake',
            source=SOURCE,
            applicable_domains=('room',),
            state='unsupported',
            state_reason='x',
            implemented=(
                GapMatrixImplementedRef(
                    profile_id='p',
                    profile_version='v',
                    criterion_id='fake-unsupported',
                    criterion_sha256='0' * 64,
                ),
            ),
        )


def test_gap_matrix_repository_roundtrip_and_tamper(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    repository = CadStandardsRepository(scene_repository)

    saved = repository.save_gap_matrix(MATRIX)
    assert saved == MATRIX

    reopened = CadStandardsRepository(scene_repository)
    assert reopened.get_gap_matrix(MATRIX.matrix_id) == MATRIX
    assert (
        reopened.get_gap_matrix_version(BUILTIN_GAP_MATRIX_VERSION)
        == MATRIX
    )
    assert reopened.list_gap_matrices() == (MATRIX,)

    # Same record re-saved: idempotent no-op.
    assert reopened.save_gap_matrix(MATRIX) == MATRIX

    # Same version, different content: sealed conflict.
    forged = build_standards_gap_matrix(
        standards=MATRIX.standards,
        matrix_version=BUILTIN_GAP_MATRIX_VERSION,
        created_at_utc='2026-10-08T00:00:00+00:00',
    )
    with pytest.raises(ValueError, match='immutable'):
        reopened.save_gap_matrix(forged)

    # Byte-level tampering with the stored payload fails closed on read.
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            "UPDATE cad_standards_gap_matrices SET payload_json = "
            "replace(payload_json, 'builtin-1', 'builtin-9')"
        )
        connection.commit()
    with pytest.raises(ValidationError):
        reopened.get_gap_matrix(MATRIX.matrix_id)


# ---------------------------------------------------------------------------
# Profile versioning — historical revisions stay pinned
# ---------------------------------------------------------------------------


def test_prov1_profile_revision_unchanged_by_prov2(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    repository = CadStandardsRepository(scene_repository)

    prov1 = dolby_atmos_home_5_1_2_profile()
    prov2 = dolby_atmos_home_5_1_2_profile_v2()
    assert prov1.version == 'r3.1-2018-12-13-prov1'
    assert prov2.version == 'r3.1-2018-12-13-prov2'
    assert prov1.profile_id == prov2.profile_id
    assert prov1.profile_semantic_hash != prov2.profile_semantic_hash
    # prov2 adds exactly one criterion.
    added = {
        criterion.criterion_id for criterion in prov2.criteria
    } - {criterion.criterion_id for criterion in prov1.criteria}
    assert added == {'dolby.5.1.2.top-middle-overhead-elevation'}

    for authority in builtin_standards_source_authorities():
        repository.save_source_authority(authority)
    repository.save_profile(prov1)
    repository.save_profile(prov2)

    evaluation_v1 = evaluate_standards_profile(
        profile=prov1,
        target=_target(
            revision=baseline, domains=('speaker_layout',)
        ),
        observations=(),
        created_at_utc=NOW,
    )
    assert evaluation_v1.profile_version == 'r3.1-2018-12-13-prov1'
    assert {result.criterion_id for result in evaluation_v1.results} == {
        'dolby.5.1.2.front-left-azimuth',
        'dolby.5.1.2.front-right-azimuth',
        'dolby.5.1.2.surround-left-azimuth',
        'dolby.5.1.2.surround-right-azimuth',
    }

    evaluation_v2 = evaluate_standards_profile(
        profile=prov2,
        target=_target(
            revision=baseline, domains=('speaker_layout',)
        ),
        observations=(),
        created_at_utc=NOW,
    )
    assert evaluation_v2.profile_version == 'r3.1-2018-12-13-prov2'
    assert 'dolby.5.1.2.top-middle-overhead-elevation' in {
        result.criterion_id for result in evaluation_v2.results
    }

    repository.save_evaluation(evaluation_v1)
    repository.save_evaluation(evaluation_v2)

    reopened = CadStandardsRepository(scene_repository)
    assert reopened.get_evaluation(evaluation_v1.evaluation_id) == (
        evaluation_v1
    )
    versions = {
        profile.version
        for profile in reopened.list_profile_versions(prov1.profile_id)
    }
    assert versions == {'r3.1-2018-12-13-prov1', 'r3.1-2018-12-13-prov2'}


# ---------------------------------------------------------------------------
# Fail-closed evaluation semantics for the extended criteria
# ---------------------------------------------------------------------------


def test_performance_criteria_unknown_without_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    profile = rp22_performance_profile(4)

    # SPL capability requires a declared spl-capability evidence
    # capability; with no observation the verdict is UNKNOWN, never
    # inferred from geometry.
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=_target(
            revision=baseline,
            domains=('room', 'seat', 'speaker_layout'),
        ),
        observations=(),
        created_at_utc=NOW,
    )
    for result in evaluation.results:
        assert result.status == 'UNKNOWN'
        assert result.reason_code == 'missing_observation'

    # An observation that provides the input but not the capability still
    # fails closed.
    spl = next(
        criterion
        for criterion in profile.criteria
        if criterion.criterion_id == 'rp22.p12.screen-spl-capability'
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=_target(
            revision=baseline,
            domains=('room', 'seat', 'speaker_layout'),
        ),
        observations=(
            CriterionObservation(
                criterion_id='rp22.p12.screen-spl-capability',
                observed_value=110.0,
                unit='dB SPL (C)',
                evidence_basis='predicted',
                provided_inputs=('screen_speaker_spl_capability_db',),
            ),
        ),
        created_at_utc=NOW,
    )
    result = _result(evaluation, 'rp22.p12.screen-spl-capability')
    assert result.status == 'UNKNOWN'
    assert result.reason_code == 'missing_input_or_capability'
    assert 'spl-capability-evidence-v1' in result.missing_capabilities


def test_measured_only_criteria_reject_predicted_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    profile = rp22_performance_profile(4)

    def _observation(
        criterion_id: str,
        value: float,
        unit: str,
        input_name: str,
        capability: str,
        basis: str = 'predicted',
    ) -> CriterionObservation:
        return CriterionObservation(
            criterion_id=criterion_id,
            observed_value=value,
            unit=unit,
            evidence_basis=basis,
            evidence_refs=(
                CriterionEvidenceRef(
                    kind='fixture-evidence',
                    evidence_id=f'{criterion_id}-ev',
                ),
            ),
            provided_inputs=(input_name,),
            capabilities=(capability,),
        )

    observations = (
        _observation(
            'rp22.p15.background-noise-ncb',
            14.0,
            'NCB',
            'background_noise_ncb_rating',
            'operating-state-noise-measurement-v1',
        ),
        _observation(
            'rp22.p16.seat-to-seat-fr-variance-screen',
            1.0,
            'dB',
            'seat_to_seat_fr_variance_screen_db',
            'seat-response-measurement-v1',
        ),
        _observation(
            'rp22.p21.early-reflection-level',
            -14.0,
            'dB',
            'early_reflection_level_db',
            'reflection-window-measurement-v1',
        ),
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=_target(
            revision=baseline,
            domains=('room', 'seat', 'speaker_layout'),
        ),
        observations=observations,
        created_at_utc=NOW,
    )
    for result in evaluation.results:
        if result.criterion_id.startswith(('rp22.p15', 'rp22.p16', 'rp22.p21')):
            assert result.status == 'UNKNOWN'
            assert result.reason_code == 'measurement_evidence_required'

    # The same observation with a measured basis evaluates.
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=_target(
            revision=baseline,
            domains=('room', 'seat', 'speaker_layout'),
        ),
        observations=tuple(
            item.model_copy(update={'evidence_basis': 'measured'})
            for item in observations
        ),
        created_at_utc=NOW,
    )
    assert _result(
        evaluation, 'rp22.p15.background-noise-ncb'
    ).status == 'PASS'
    assert _result(
        evaluation, 'rp22.p16.seat-to-seat-fr-variance-screen'
    ).status == 'PASS'
    assert _result(
        evaluation, 'rp22.p21.early-reflection-level'
    ).status == 'PASS'


def test_applicability_scoping_stays_exact() -> None:
    profile = rp22_performance_profile(1)
    target = StandardsEvaluationTarget(
        document_id='doc',
        scene_revision_id='rev',
        scene_content_hash='0' * 64,
        applicable_domains=('other_domain',),
    )
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(),
        created_at_utc=NOW,
    )
    assert all(
        result.status == 'NOT_APPLICABLE'
        for result in evaluation.results
    )


# ---------------------------------------------------------------------------
# Layout-derived observation for the new Dolby elevation criterion
# ---------------------------------------------------------------------------


def _workspace(
    tmp_path: Path,
    document: SceneDocument,
    *,
    name: str = 'scenes.sqlite3',
) -> StandardsWorkspaceModel:
    repository = SceneRepository(tmp_path / name)
    repository.save(document, parent_revision_id=None)
    return StandardsWorkspaceModel(repository, document.document_id)


def _dolby_elevation_scene(*, overhead_z: float) -> SceneDocument:
    # Seat ear at (3.0, 3.6, 1.2) facing -Y. A TML/TMR pair placed nearly
    # overhead: at ear distance ~0.2 m horizontally, z=3.0 gives ~83.7
    # degrees elevation — inside the published 65-100 window.
    return SceneDocument(
        document_id='doc-805-dolby-elevation',
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=4.0),
        entities=(
            _seat('seat-a', x=3.0, y=3.6),
            _speaker('speaker-tml', 'TML', x=2.9, y=3.7, z=overhead_z),
            _speaker('speaker-tmr', 'TMR', x=3.1, y=3.7, z=overhead_z),
        ),
    )


def test_dolby_overhead_elevation_derived_from_layout(tmp_path) -> None:
    workspace = _workspace(tmp_path, _dolby_elevation_scene(overhead_z=3.0))
    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile_v2(), variant_id=None
    )
    result = _result(
        evaluation, 'dolby.5.1.2.top-middle-overhead-elevation'
    )
    assert result.status == 'PASS'
    assert result.observed_value == pytest.approx(
        math.degrees(math.atan2(1.8, math.hypot(-0.1, 0.1)))
    )
    assert result.evidence_basis == 'predicted'

    # Out-of-window elevation fails.
    workspace = _workspace(
        tmp_path, _dolby_elevation_scene(overhead_z=1.5), name='low.sqlite3'
    )
    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile_v2(), variant_id=None
    )
    result = _result(
        evaluation, 'dolby.5.1.2.top-middle-overhead-elevation'
    )
    assert result.status == 'FAIL'

    # No TML/TMR pair (but a speaker layout exists): UNKNOWN, never
    # inferred — the criterion needs an observed elevation and none can
    # be derived.
    scene = SceneDocument(
        document_id='doc-805-no-overhead',
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=4.0),
        entities=(
            _seat('seat-a', x=3.0, y=3.6),
            _speaker('speaker-fl', 'FL', x=2.0, y=1.8, z=1.2),
        ),
    )
    workspace = _workspace(tmp_path, scene, name='none.sqlite3')
    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile_v2(), variant_id=None
    )
    result = _result(
        evaluation, 'dolby.5.1.2.top-middle-overhead-elevation'
    )
    assert result.status == 'UNKNOWN'
    assert result.reason_code == 'missing_observation'

    # prov1 does not know the criterion at all.
    evaluation = workspace.evaluate(
        dolby_atmos_home_5_1_2_profile(), variant_id=None
    )
    assert 'dolby.5.1.2.top-middle-overhead-elevation' not in {
        result.criterion_id for result in evaluation.results
    }
