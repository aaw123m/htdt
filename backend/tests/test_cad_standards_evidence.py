from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionEvidenceRef,
    CriterionObservation,
    CriterionRule,
    CriterionSource,
    StandardsEvaluationTarget,
    _digest,
    build_user_standards_profile,
    evaluate_standards_profile,
)
from htdt.cad_standards_evidence import (
    STANDARDS_MANUAL_OBSERVATION_KIND,
    ResolvedCriterionEvidence,
    StandardsObservationAuthority,
    build_standards_observation_authority,
)
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import ChannelRoleBinding, build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-10-01T00:00:00+00:00'
SOURCE = CriterionSource(
    publisher='Fixture publisher',
    document_title='Fixture criteria',
    document_version='1.0',
    reference='Fixture §1',
)
CAPABILITY = 'fixture-capability-v1'


def _criterion(
    criterion_id: str = 'distance',
    *,
    evidence_requirement: str = 'predicted_or_measured',
    maximum: float = 1.0,
) -> CriterionDefinition:
    return CriterionDefinition(
        criterion_id=criterion_id,
        name=criterion_id,
        source=SOURCE,
        quantity=f'{criterion_id}_quantity',
        unit='m',
        applicable_domains=('room',),
        required_inputs=(f'{criterion_id}_input',),
        required_capabilities=(CAPABILITY,),
        evidence_requirement=evidence_requirement,
        rule=CriterionRule(operator='max', maximum=maximum),
    )


def _target(revision, *, variant=None, entity_ids=('speaker-fl',)):
    return StandardsEvaluationTarget(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if variant is None else variant.variant_id,
        system_variant_sha256=None if variant is None else variant.variant_sha256,
        entity_ids=entity_ids,
        applicable_domains=('room',),
    )


def _authority(
    target: StandardsEvaluationTarget,
    *,
    criterion_id: str = 'distance',
    value: float = 0.8,
    basis: str = 'predicted',
    entity_ids=('speaker-fl',),
    provided_inputs=None,
    capabilities=(CAPABILITY,),
) -> StandardsObservationAuthority:
    return build_standards_observation_authority(
        document_id=target.document_id,
        scene_revision_id=target.scene_revision_id,
        scene_content_hash=target.scene_content_hash,
        system_variant_id=target.system_variant_id,
        system_variant_sha256=target.system_variant_sha256,
        quantity=f'{criterion_id}_quantity',
        unit='m',
        observed_value=value,
        evidence_basis=basis,
        entity_ids=entity_ids,
        provided_inputs=(
            (f'{criterion_id}_input',) if provided_inputs is None else provided_inputs
        ),
        capabilities=capabilities,
        observed_at_utc=NOW,
    )


def _observation(
    authority: StandardsObservationAuthority,
    *,
    criterion_id: str = 'distance',
    value: float = 0.8,
    basis: str = 'predicted',
    entity_ids=('speaker-fl',),
    provided_inputs=None,
    capabilities=(CAPABILITY,),
) -> CriterionObservation:
    return CriterionObservation(
        criterion_id=criterion_id,
        entity_ids=entity_ids,
        observed_value=value,
        unit='m',
        evidence_basis=basis,
        evidence_refs=(authority.ref(),),
        provided_inputs=(
            (f'{criterion_id}_input',)
            if provided_inputs is None
            else provided_inputs
        ),
        capabilities=capabilities,
    )


def _seed(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    repository = CadStandardsRepository(scene_repository, variant_repository)
    return scene_repository, revision, variant_repository, repository


def _profile(repository, criterion: CriterionDefinition):
    profile = build_user_standards_profile(
        profile_id='fixture-evidence-profile',
        version='1.0',
        name='Evidence fixture',
        criteria=(criterion,),
    )
    repository.save_profile(profile)
    return profile


def _save_valid(repository, profile, target, observation, *, at=NOW):
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(observation,),
        created_at_utc=at,
    )
    repository.save_evaluation(evaluation)
    return evaluation


def _variant(baseline, *, name='Fixture variant', at='2026-10-01T00:05:00+00:00'):
    return build_system_variant(
        baseline=baseline,
        name=name,
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        created_at_utc=at,
    )


def test_observation_authority_round_trips_and_is_immutable(tmp_path: Path) -> None:
    scene_repository, revision, _variants, repository = _seed(tmp_path)
    target = _target(revision)
    authority = _authority(target)

    persisted = repository.save_observation_authority(authority)
    assert persisted == authority
    assert repository.save_observation_authority(authority) == authority
    assert repository.get_observation_authority(authority.authority_id) == authority
    assert repository.list_observation_authorities() == (authority,)
    assert authority.ref() == CriterionEvidenceRef(
        kind=STANDARDS_MANUAL_OBSERVATION_KIND,
        evidence_id=authority.authority_id,
        evidence_sha256=authority.semantic_hash_sha256,
    )

    tampered = authority.model_copy(update={'observed_value': 0.1})
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        repository.save_observation_authority(tampered)

    # A different revision of the same document is a different exact binding;
    # the authority is retained for that revision only.
    changed_document = revision.document.model_copy(
        update={
            'room': revision.document.room.model_copy(update={'width_m': 7.0})
        }
    )
    newer = scene_repository.save(
        changed_document,
        parent_revision_id=revision.revision_id,
    ).revision
    foreign = build_standards_observation_authority(
        document_id=newer.document_id,
        scene_revision_id=newer.revision_id,
        scene_content_hash=newer.content_hash,
        quantity='distance_quantity',
        unit='m',
        observed_value=0.8,
        evidence_basis='predicted',
        observed_at_utc=NOW,
    )
    repository.save_observation_authority(foreign)
    assert repository.get_observation_authority(foreign.authority_id) == foreign

    bogus_revision = build_standards_observation_authority(
        document_id=revision.document_id,
        scene_revision_id='missing-revision',
        scene_content_hash='0' * 64,
        quantity='distance_quantity',
        unit='m',
        observed_value=0.8,
        evidence_basis='predicted',
        observed_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='SceneRevision does not exist'):
        repository.save_observation_authority(bogus_revision)

    bogus_hash = build_standards_observation_authority(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash='1' * 64,
        quantity='distance_quantity',
        unit='m',
        observed_value=0.8,
        evidence_basis='predicted',
        observed_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='SceneRevision authority mismatch'):
        repository.save_observation_authority(bogus_hash)

    bogus_entity = _authority(target, entity_ids=('missing-entity',))
    with pytest.raises(ValueError, match='missing target entities'):
        repository.save_observation_authority(bogus_entity)


def test_measured_criterion_requires_retained_exact_evidence(tmp_path: Path) -> None:
    _scene_repository, revision, _variants, repository = _seed(tmp_path)
    criterion = _criterion('distance', evidence_requirement='measured')
    profile = _profile(repository, criterion)
    target = _target(revision)

    authority = _authority(target, basis='measured')
    repository.save_observation_authority(authority)
    evaluation = _save_valid(
        repository,
        profile,
        target,
        _observation(authority, basis='measured'),
    )
    assert evaluation.results[0].status == 'PASS'
    assert evaluation.results[0].evidence_basis == 'measured'

    # Canonical valid evaluations reopen unchanged through the replaying reads.
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations_for_scene(revision.revision_id) == (
        evaluation,
    )

    # A fabricated ref to a nonexistent authority can never satisfy a
    # measured criterion: the save fails closed and nothing is persisted.
    fabricated = CriterionObservation(
        criterion_id='distance',
        entity_ids=('speaker-fl',),
        observed_value=0.5,
        unit='m',
        evidence_basis='measured',
        evidence_refs=(
            CriterionEvidenceRef(
                kind=STANDARDS_MANUAL_OBSERVATION_KIND,
                evidence_id='standards-observation:' + '0' * 64,
                evidence_sha256='0' * 64,
            ),
        ),
        provided_inputs=('distance_input',),
        capabilities=(CAPABILITY,),
    )
    fabricated_evaluation = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(fabricated,),
        created_at_utc='2026-10-01T00:10:00+00:00',
    )
    assert fabricated_evaluation.results[0].status == 'PASS'
    with pytest.raises(ValueError, match='does not resolve'):
        repository.save_evaluation(fabricated_evaluation)
    assert repository.get_evaluation(fabricated_evaluation.evaluation_id) is None

    # An unregistered evidence kind fails closed even though the id/sha look
    # plausible.
    unknown_kind = fabricated_evaluation.observations[0].model_copy(
        update={
            'evidence_refs': (
                CriterionEvidenceRef(
                    kind='fabricated-kind',
                    evidence_id='fabricated:1',
                    evidence_sha256='1' * 64,
                ),
            )
        }
    )
    with pytest.raises(ValueError, match='has no resolver'):
        repository.save_evaluation(
            evaluate_standards_profile(
                profile=profile,
                target=target,
                observations=(unknown_kind,),
                created_at_utc='2026-10-01T00:11:00+00:00',
            )
        )

    # Predicted evidence cannot back a measured claim.
    predicted_authority = _authority(
        target,
        criterion_id='distance',
        value=0.8,
        basis='predicted',
    )
    repository.save_observation_authority(predicted_authority)
    with pytest.raises(ValueError, match='is not supported'):
        repository.save_evaluation(
            evaluate_standards_profile(
                profile=profile,
                target=target,
                observations=(
                    _observation(predicted_authority, basis='measured'),
                ),
                created_at_utc='2026-10-01T00:12:00+00:00',
            )
        )


def test_observed_value_and_labels_must_match_resolved_evidence(
    tmp_path: Path,
) -> None:
    _scene_repository, revision, _variants, repository = _seed(tmp_path)
    profile = _profile(repository, _criterion('distance'))
    target = _target(revision)
    authority = _authority(target, value=0.8)
    repository.save_observation_authority(authority)

    # A favorable claimed value inconsistent with the exact evidence is
    # rejected even though the ref itself is real.
    favorable = _observation(authority, value=0.2)
    with pytest.raises(ValueError, match='inconsistent with resolved evidence'):
        _save_valid(repository, profile, target, favorable, at='2026-10-01T00:20:00+00:00')

    # Claimed inputs/capabilities cannot exceed what the evidence supports.
    inflated_capabilities = _observation(
        authority,
        capabilities=(CAPABILITY, 'forged-capability-v9'),
    )
    with pytest.raises(ValueError, match='beyond resolved evidence'):
        _save_valid(repository, profile, target, inflated_capabilities, at='2026-10-01T00:21:00+00:00')

    inflated_inputs = _observation(
        authority,
        provided_inputs=('distance_input', 'forged_input'),
    )
    with pytest.raises(ValueError, match='beyond resolved evidence'):
        _save_valid(repository, profile, target, inflated_inputs, at='2026-10-01T00:22:00+00:00')

    # A claimed entity binding must be covered by the value-attesting
    # evidence, even when the entity is inside the evaluation target.
    wider_target = _target(revision, entity_ids=('speaker-fl', 'speaker-c'))
    uncovered_entity = _observation(authority, entity_ids=('speaker-c',))
    with pytest.raises(ValueError, match='entity binding is not covered'):
        _save_valid(repository, profile, wider_target, uncovered_entity, at='2026-10-01T00:23:00+00:00')

    # An observation claiming a measured quantity with no evidence at all is
    # caller authority and is rejected outright.
    bare = CriterionObservation(
        criterion_id='distance',
        entity_ids=('speaker-fl',),
        observed_value=0.8,
        unit='m',
        provided_inputs=('distance_input',),
        capabilities=(CAPABILITY,),
    )
    with pytest.raises(ValueError, match='does not attest the claimed observed value'):
        _save_valid(repository, profile, target, bare, at='2026-10-01T00:24:00+00:00')


def test_evidence_bound_to_other_scene_variant_or_entity_is_rejected(
    tmp_path: Path,
) -> None:
    scene_repository, revision, variant_repository, repository = _seed(tmp_path)
    profile = _profile(repository, _criterion('distance'))
    baseline_target = _target(revision)

    baseline_authority = _authority(baseline_target)
    repository.save_observation_authority(baseline_authority)

    # A later revision of the same document is a different evidence scope.
    renamed = revision.document.model_copy(
        update={
            'room': revision.document.room.model_copy(update={'width_m': 7.0})
        }
    )
    newer = scene_repository.save(
        renamed,
        parent_revision_id=revision.revision_id,
    ).revision
    newer_target = _target(newer)
    with pytest.raises(ValueError, match='bound to another SceneRevision'):
        _save_valid(
            repository,
            profile,
            newer_target,
            _observation(baseline_authority),
            at='2026-10-01T00:30:00+00:00',
        )

    variant = _variant(revision)
    variant_repository.save_variant(variant)
    variant_target = _target(revision, variant=variant)

    # Baseline-bound evidence does not cover a variant-bound target.
    with pytest.raises(ValueError, match='bound to another SystemVariant'):
        _save_valid(
            repository,
            profile,
            variant_target,
            _observation(baseline_authority),
            at='2026-10-01T00:31:00+00:00',
        )

    # Variant-bound evidence does not cover the baseline target either.
    variant_authority = _authority(variant_target)
    repository.save_observation_authority(variant_authority)
    variant_evaluation = _save_valid(
        repository,
        profile,
        variant_target,
        _observation(variant_authority),
        at='2026-10-01T00:32:00+00:00',
    )
    assert variant_evaluation.results[0].status == 'PASS'
    with pytest.raises(ValueError, match='bound to another SystemVariant'):
        _save_valid(
            repository,
            profile,
            baseline_target,
            _observation(variant_authority),
            at='2026-10-01T00:33:00+00:00',
        )

    # The variant-bound history replays cleanly on read.
    assert repository.list_evaluations_for_scene(revision.revision_id) == (
        variant_evaluation,
    )


def test_tampered_persisted_results_are_detected_on_read(tmp_path: Path) -> None:
    _scene_repository, revision, _variants, repository = _seed(tmp_path)
    profile = _profile(repository, _criterion('distance'))
    target = _target(revision)
    authority = _authority(target, value=2.0)
    repository.save_observation_authority(authority)
    evaluation = _save_valid(
        repository,
        profile,
        target,
        _observation(authority, value=2.0),
    )
    assert evaluation.results[0].status == 'FAIL'

    # Rewrite the persisted row with a favorable result and a recomputed
    # evaluation hash: the payload stays self-consistent but diverges from the
    # canonical replay.
    payload = evaluation.model_dump(mode='json')
    assert payload['results'][0]['criterion_id'] == 'distance'
    payload['results'][0]['status'] = 'PASS'
    payload['results'][0]['reason_code'] = 'comparison_pass'
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {'evaluation_sha256', 'created_at_utc'}
    }
    payload['evaluation_sha256'] = _digest(semantic)

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE cad_standards_evaluations SET payload_json=?, '
            'evaluation_sha256=? WHERE evaluation_id=?',
            (
                json.dumps(payload),
                payload['evaluation_sha256'],
                evaluation.evaluation_id,
            ),
        )
        connection.commit()

    with pytest.raises(ValueError, match='does not match evaluator authority'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='does not match evaluator authority'):
        repository.list_evaluations_for_scene(revision.revision_id)


def test_disappeared_evidence_fails_closed_on_read(tmp_path: Path) -> None:
    _scene_repository, revision, _variants, repository = _seed(tmp_path)
    profile = _profile(repository, _criterion('distance'))
    target = _target(revision)
    authority = _authority(target)
    repository.save_observation_authority(authority)
    evaluation = _save_valid(
        repository,
        profile,
        target,
        _observation(authority),
    )

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_standards_observation_authorities '
            'WHERE authority_id=?',
            (authority.authority_id,),
        )
        connection.commit()

    with pytest.raises(ValueError, match='does not resolve'):
        repository.get_evaluation(evaluation.evaluation_id)


def test_pinned_semantic_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    _scene_repository, revision, _variants, repository = _seed(tmp_path)
    profile = _profile(repository, _criterion('distance'))
    target = _target(revision)
    authority = _authority(target)
    repository.save_observation_authority(authority)

    pinned_wrong = authority.ref().model_copy(
        update={'evidence_sha256': 'f' * 64}
    )
    observation = _observation(authority).model_copy(
        update={'evidence_refs': (pinned_wrong,)}
    )
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        _save_valid(
            repository,
            profile,
            target,
            observation,
            at='2026-10-01T00:40:00+00:00',
        )


def test_custom_resolver_integrates_scene_independent_authority(
    tmp_path: Path,
) -> None:
    _scene_repository, revision, _variants, _repository = _seed(tmp_path)
    resolved_calls: list[str] = []

    def fixture_resolver(context, ref) -> ResolvedCriterionEvidence:
        resolved_calls.append(ref.evidence_id)
        if ref.evidence_id != 'fixture-external:1':
            raise ValueError('unknown fixture authority')
        return ResolvedCriterionEvidence(
            ref=ref,
            source_sha256='c' * 64,
            evidence_basis='predicted',
            observed_value=0.8,
            unit='m',
            provided_inputs=('distance_input',),
            capabilities=(CAPABILITY,),
            entity_ids=('speaker-fl',),
        )

    repository = CadStandardsRepository(
        _scene_repository,
        _variants,
        evidence_resolvers={'fixture_external': fixture_resolver},
    )
    profile = _profile(repository, _criterion('distance'))
    target = _target(revision)
    ref = CriterionEvidenceRef(
        kind='fixture_external',
        evidence_id='fixture-external:1',
        evidence_sha256='c' * 64,
    )
    evaluation = _save_valid(
        repository,
        profile,
        target,
        CriterionObservation(
            criterion_id='distance',
            entity_ids=('speaker-fl',),
            observed_value=0.8,
            unit='m',
            evidence_basis='predicted',
            evidence_refs=(ref,),
            provided_inputs=('distance_input',),
            capabilities=(CAPABILITY,),
        ),
    )
    assert evaluation.results[0].status == 'PASS'
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert resolved_calls

    # A wrong pinned hash is rejected against the resolver's authority hash.
    bad_pin = CriterionObservation(
        criterion_id='distance',
        entity_ids=('speaker-fl',),
        observed_value=0.8,
        unit='m',
        evidence_basis='predicted',
        evidence_refs=(
            CriterionEvidenceRef(
                kind='fixture_external',
                evidence_id='fixture-external:1',
                evidence_sha256='d' * 64,
            ),
        ),
        provided_inputs=('distance_input',),
        capabilities=(CAPABILITY,),
    )
    with pytest.raises(ValueError, match='semantic hash mismatch'):
        _save_valid(
            repository,
            profile,
            target,
            bad_pin,
            at='2026-10-01T00:50:00+00:00',
        )

    # A repository without the registered kind fails closed on read.
    bare_repository = CadStandardsRepository(_scene_repository, _variants)
    with pytest.raises(ValueError, match='has no resolver'):
        bare_repository.get_evaluation(evaluation.evaluation_id)


def test_evaluation_identity_depends_on_exact_evidence(tmp_path: Path) -> None:
    _scene_repository, revision, _variants, repository = _seed(tmp_path)
    profile = _profile(repository, _criterion('distance'))
    target = _target(revision)
    authority = _authority(target)
    repository.save_observation_authority(authority)

    first = _save_valid(repository, profile, target, _observation(authority))
    repeated = evaluate_standards_profile(
        profile=profile,
        target=target,
        observations=(_observation(authority),),
        created_at_utc='2026-10-01T01:00:00+00:00',
    )
    assert repeated.evaluation_id == first.evaluation_id
    assert repository.save_evaluation(repeated) == first

    # Untyped/kind-less refs are not even representable.
    with pytest.raises(ValidationError):
        CriterionEvidenceRef(evidence_id='bare-id')
