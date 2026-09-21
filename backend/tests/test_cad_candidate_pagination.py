"""Regression coverage for the 500-candidate generation-page contract.

``generate_search_space`` never returns more than ``MAX_SEARCH_PAGE_SIZE``
candidates per call, while ``SearchSpec.candidate_limit`` accepts up to
50,000 (the Optimize UI default is 10,000). Native O50/O60/O70 authorities
that resolve the full feasible set must paginate deterministically through
``iter_cad_candidate_pages`` rather than request an unbounded page; these
tests pin that a >500-candidate SearchSpec resolves completely and
identically to the canonical enumeration.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from htdt.cad_adaptive_repository import CadAdaptivePlanRepository
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_measurement_loop import build_measurement_plan
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_model_validation_repository import CadModelValidationRepository
from htdt.cad_model_validation_service import CadModelValidationService
from htdt.cad_objective_repository import CadObjectiveRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim_repository import CadRoomSimRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import (
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
    iter_cad_candidate_pages,
)
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.cad_validation_campaign import (
    CadValidationCampaignCandidate,
    CadValidationCampaignRepeatability,
    CadValidationCampaignSensitivity,
    CadValidationCampaignSeparation,
    CadValidationTargetResponse,
    build_validation_campaign,
)
from htdt.cad_validation_campaign_repository import CadValidationCampaignRepository
from htdt.search_space import MAX_SEARCH_PAGE_SIZE


DOCUMENT_ID = 'candidate-pagination-fixture'


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
            ),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _large_fixture(tmp_path, *, candidate_limit: int = 10_000):
    """A SearchSpec whose 501 feasible candidates span two contract pages."""
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    spec, estimate = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (
            CadSearchAxis(
                entity_id='fl',
                axis='x',
                min_m=0.5,
                max_m=5.0,
                step_m=0.009,
            ),
        ),
        candidate_limit=candidate_limit,
        name='over-500 sweep',
    )
    search_repository = CadSearchRepository(scene_repository)
    search_repository.save(spec)
    return scene_repository, search_repository, revision, spec, estimate


def _all_candidates(scene_repository, spec):
    return [
        candidate
        for page in iter_cad_candidate_pages(scene_repository, spec)
        for candidate in page.candidates
    ]


def _reference_enumeration(scene_repository, spec, *, limit: int):
    """Independent canonical enumeration through the single-page API."""
    candidates = []
    pages = []
    offset = 0
    while True:
        page = generate_cad_candidates(
            scene_repository,
            spec,
            offset=offset,
            limit=limit,
        )
        pages.append(page)
        candidates.extend(page.candidates)
        offset += len(page.candidates)
        if not page.candidates or offset >= page.feasible_candidate_count:
            break
    return pages, candidates


@pytest.mark.parametrize('candidate_limit', [10_000, 50_000])
def test_iter_candidate_pages_resolves_full_set_over_500(
    tmp_path,
    candidate_limit,
):
    scene_repository, _search, _revision, spec, estimate = _large_fixture(
        tmp_path,
        candidate_limit=candidate_limit,
    )
    assert estimate['raw_candidate_count'] > MAX_SEARCH_PAGE_SIZE

    pages = list(iter_cad_candidate_pages(scene_repository, spec))
    assert len(pages) == 2
    # Even a 50,000 candidate_limit never requests a page larger than the
    # generator contract.
    assert all(page.limit == MAX_SEARCH_PAGE_SIZE for page in pages)
    assert [len(page.candidates) for page in pages] == [500, 1]
    assert len({page.candidate_set_sha256 for page in pages}) == 1

    candidates = [candidate for page in pages for candidate in page.candidates]
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    assert len(candidate_ids) == len(set(candidate_ids))
    assert [candidate.feasible_index for candidate in candidates] == list(range(501))

    # The resolved set equals the canonical enumeration taken at an unrelated
    # page size: identical members, order, and set identity.
    reference_pages, reference = _reference_enumeration(
        scene_repository,
        spec,
        limit=250,
    )
    assert candidate_ids == [candidate.candidate_id for candidate in reference]
    assert all(
        page.candidate_set_sha256 == pages[0].candidate_set_sha256
        for page in reference_pages
    )

    # Re-running the iterator reproduces the identical set.
    again = _all_candidates(scene_repository, spec)
    assert [candidate.candidate_id for candidate in again] == candidate_ids


def test_iter_candidate_pages_matches_single_page_for_small_set(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    spec, _estimate = build_cad_search_spec(
        revision,
        CadConstraintSet(document_id=DOCUMENT_ID, constraints=()),
        (CadSearchAxis(entity_id='fl', axis='x', min_m=1.0, max_m=2.0, step_m=0.2),),
        candidate_limit=20,
    )

    direct = generate_cad_candidates(scene_repository, spec, limit=20)
    pages = list(iter_cad_candidate_pages(scene_repository, spec))
    assert len(pages) == 1
    assert pages[0] == direct


def test_measurement_plan_resolves_candidate_beyond_first_page(tmp_path):
    """O50: a UI-default 10,000 candidate_limit spec builds a Measurement Plan."""
    scene_repository, search_repository, revision, spec, _estimate = _large_fixture(
        tmp_path
    )
    canonical = generate_cad_candidates(scene_repository, spec, limit=500)
    last_page = generate_cad_candidates(
        scene_repository,
        spec,
        offset=canonical.feasible_candidate_count - 1,
        limit=1,
    )
    candidate = last_page.candidates[0]
    assert candidate.feasible_index == 500

    applied = scene_repository.save(
        candidate_preview_document(revision.document, candidate),
        parent_revision_id=revision.revision_id,
    ).revision
    plan = build_measurement_plan(
        scene_repository,
        search_repository,
        search_spec_id=spec.search_spec_id,
        candidate_id=candidate.candidate_id,
        applied_scene_revision_id=applied.revision_id,
    )
    assert plan.candidate_id == candidate.candidate_id
    assert plan.candidate_set_sha256 == canonical.candidate_set_sha256

    # Persisting revalidates membership through the same paginated replay.
    measurement_repository = CadMeasurementRepository(scene_repository)
    measurement_repository.save_measurement_plan(plan)


def test_validation_campaign_regenerates_candidate_set_across_pages(tmp_path):
    """O60 campaign: full-set regeneration succeeds for a >500 spec."""
    scene_repository, search_repository, _revision, spec, _estimate = (
        _large_fixture(tmp_path)
    )
    canonical = generate_cad_candidates(scene_repository, spec, limit=500)
    candidates = _all_candidates(scene_repository, spec)
    calibration = candidates[0]
    holdouts = (candidates[499], candidates[500])

    campaign = build_validation_campaign(
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=canonical.candidate_set_sha256,
        model_id='rew-roomsim',
        model_version='5.40',
        requested_band_hz=(20.0, 160.0),
        max_holdout_rms_db=4.0,
        candidates=(
            CadValidationCampaignCandidate(
                candidate_id=calibration.candidate_id,
                split='calibration',
            ),
            CadValidationCampaignCandidate(
                candidate_id=holdouts[0].candidate_id,
                split='holdout',
            ),
            CadValidationCampaignCandidate(
                candidate_id=holdouts[1].candidate_id,
                split='holdout',
            ),
        ),
        objective_ids=('response.rms_difference_db',),
        target_response=CadValidationTargetResponse(
            frequency_hz=(20.0, 40.0, 80.0, 160.0),
            level_db=(0.0, 0.0, 0.0, 0.0),
        ),
        sensitivity=(
            CadValidationCampaignSensitivity(
                objective_id='response.rms_difference_db',
                candidate_a_id=holdouts[0].candidate_id,
                candidate_b_id=holdouts[1].candidate_id,
                max_observed_sensitivity_per_m=20.0,
                max_model_error_per_m=10.0,
            ),
        ),
        repeatability=(
            CadValidationCampaignRepeatability(
                candidate_id=calibration.candidate_id,
            ),
        ),
        separation=(
            CadValidationCampaignSeparation(
                candidate_a_id=holdouts[0].candidate_id,
                candidate_b_id=holdouts[1].candidate_id,
                repeatability_candidate_id=calibration.candidate_id,
                min_repeatability_multiple=2.0,
            ),
        ),
        required_applicability_codes=('geometry',),
    )

    measurement_repository = CadMeasurementRepository(scene_repository)
    campaign_repository = CadValidationCampaignRepository(
        search_repository,
        measurement_repository,
    )
    registration = campaign_repository.save(campaign)
    assert campaign_repository.get(campaign.campaign_id) == campaign
    assert registration.campaign_sha256 == campaign.campaign_sha256


def _o60_repositories(scene_repository, search_repository):
    measurement_repository = CadMeasurementRepository(scene_repository)
    roomsim_repository = CadRoomSimRepository(scene_repository, search_repository)
    validation_repository = CadModelValidationRepository(
        search_repository,
        roomsim_repository,
        measurement_repository,
    )
    validation_service = CadModelValidationService(
        search_repository,
        roomsim_repository,
        measurement_repository,
        CadObjectiveRepository(scene_repository, search_repository),
    )
    return measurement_repository, validation_repository, validation_service


def test_model_validation_candidate_positions_span_pages(tmp_path):
    """O60 model validation: candidate lookups cross the 500-page boundary."""
    scene_repository, search_repository, _revision, spec, _estimate = (
        _large_fixture(tmp_path)
    )
    canonical = generate_cad_candidates(scene_repository, spec, limit=500)
    candidates = _all_candidates(scene_repository, spec)
    boundary_ids = {
        candidates[499].candidate_id,
        candidates[500].candidate_id,
    }

    _measurement, repository, service = _o60_repositories(
        scene_repository,
        search_repository,
    )
    for resolver in (repository._candidate_positions, service._candidate_positions):
        positions = resolver(
            spec,
            set(boundary_ids),
            canonical.candidate_set_sha256,
        )
        assert set(positions) == boundary_ids
        assert (
            positions[candidates[500].candidate_id]
            == candidates[500].positions
        )

    with pytest.raises(ValueError, match='candidate-set hash'):
        repository._candidate_positions(spec, set(boundary_ids), '0' * 64)
    with pytest.raises(ValueError, match='outside SearchSpec'):
        repository._candidate_positions(
            spec,
            {'not-a-candidate'},
            canonical.candidate_set_sha256,
        )


def test_adaptive_candidate_authority_spans_pages(tmp_path):
    """O70: adaptive candidate validation resolves members past page one."""
    scene_repository, search_repository, _revision, spec, _estimate = (
        _large_fixture(tmp_path)
    )
    canonical = generate_cad_candidates(scene_repository, spec, limit=500)
    candidates = _all_candidates(scene_repository, spec)
    wanted = [candidates[499].candidate_id, candidates[500].candidate_id]

    _measurement, validation_repository, _service = _o60_repositories(
        scene_repository,
        search_repository,
    )
    repository = CadAdaptivePlanRepository(search_repository, validation_repository)

    def plan(candidate_ids, candidate_set_sha256):
        return SimpleNamespace(
            candidate_set_sha256=candidate_set_sha256,
            proposals=[
                SimpleNamespace(candidate_id=candidate_id)
                for candidate_id in candidate_ids
            ],
        )

    repository._validate_candidate_authority(
        plan(wanted, canonical.candidate_set_sha256),
        spec,
    )
    with pytest.raises(ValueError, match='no longer matches SearchSpec'):
        repository._validate_candidate_authority(
            plan(wanted, '0' * 64),
            spec,
        )
    with pytest.raises(ValueError, match='outside SearchSpec'):
        repository._validate_candidate_authority(
            plan(['not-a-candidate'], canonical.candidate_set_sha256),
            spec,
        )


def test_objective_membership_replay_resolves_candidate_beyond_first_page(
    tmp_path,
):
    """O30 replay shares the same paginated candidate-set authority."""
    scene_repository, search_repository, _revision, spec, _estimate = (
        _large_fixture(tmp_path)
    )
    canonical = generate_cad_candidates(scene_repository, spec, limit=500)
    candidates = _all_candidates(scene_repository, spec)

    repository = CadObjectiveRepository(scene_repository, search_repository)
    candidate, candidate_set_sha256 = repository._require_candidate_membership(
        spec,
        candidates[500].candidate_id,
    )
    assert candidate == candidates[500]
    assert candidate_set_sha256 == canonical.candidate_set_sha256

    with pytest.raises(ValueError, match='not a member of the SearchSpec'):
        repository._require_candidate_membership(spec, 'not-a-candidate')
