from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_repository import SceneRepository
from htdt.cad_standards import (
    CriterionDefinition,
    CriterionRule,
    CriterionSource,
    CriterionSourceExtraction,
    StandardsEvaluationTarget,
    StandardsProfile,
    StandardsSourceAuthority,
    build_standards_profile,
    build_standards_source_authority,
    build_user_standards_profile,
    evaluate_standards_profile,
    validate_criterion_source_authority,
)
from htdt.cad_standards_authorities import (
    builtin_standards_source_authorities,
    rp22_spatial_source_authority,
)
from htdt.cad_standards_profiles import (
    builtin_standards_profiles,
    rp22_spatial_profile,
)
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


NOW = '2026-09-21T00:00:00+00:00'


def _authority() -> StandardsSourceAuthority:
    return rp22_spatial_source_authority(2)


def _repository(tmp_path: Path, **kwargs) -> CadStandardsRepository:
    return CadStandardsRepository(
        SceneRepository(tmp_path / 'cad.sqlite3'),
        **kwargs,
    )


def _published_criterion(
    authority: StandardsSourceAuthority,
    extraction_id: str,
    **overrides,
) -> CriterionDefinition:
    extraction = authority.extraction(extraction_id)
    assert extraction is not None
    source = CriterionSource(
        publisher=authority.publisher,
        document_title=authority.document_title,
        document_version=authority.document_version,
        reference=extraction.reference,
        source_uri=authority.source_uri,
        content_kind=extraction.content_kind,
        authority_ref=authority.ref(),
        extraction_id=extraction.extraction_id,
    )
    values = {
        'criterion_id': extraction.extraction_id,
        'name': extraction.extraction_id,
        'source': source,
        'quantity': extraction.quantity,
        'unit': extraction.unit,
        'applicable_domains': ('seat',),
        'rule': extraction.rule,
    }
    values.update(overrides)
    return CriterionDefinition(**values)


def _published_profile(
    criteria,
    *,
    profile_id: str = 'fixture-published',
) -> StandardsProfile:
    return build_standards_profile(
        profile_id=profile_id,
        version='1.0',
        name='Fixture published profile',
        profile_kind='published',
        criteria=criteria,
    )


def test_published_profile_cannot_rely_on_free_form_citation(tmp_path) -> None:
    repository = _repository(tmp_path)
    authority = _authority()
    repository.save_source_authority(authority)

    criterion = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    bare_source = criterion.source.model_copy(
        update={
            'authority_ref': None,
            'extraction_id': None,
            'content_kind': None,
        }
    )
    profile = _published_profile(
        (criterion.model_copy(update={'source': bare_source}),)
    )
    with pytest.raises(ValueError, match='exact source authority'):
        repository.save_profile(profile)


def test_published_profile_rejects_dangling_authority_ref(tmp_path) -> None:
    repository = _repository(tmp_path)
    authority = _authority()
    criterion = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    profile = _published_profile((criterion,))
    with pytest.raises(ValueError, match='source authority does not exist'):
        repository.save_profile(profile)


def test_published_profile_rejects_fabricated_threshold(tmp_path) -> None:
    repository = _repository(tmp_path)
    authority = _authority()
    repository.save_source_authority(authority)

    # Real-looking RP22 citation and a valid authority ref, but an altered
    # threshold: the retained extraction does not back the fabricated rule.
    criterion = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
        rule=CriterionRule(operator='min', minimum=9.9, lower_inclusive=False),
    )
    profile = _published_profile((criterion,))
    with pytest.raises(ValueError, match='not backed by the extraction'):
        repository.save_profile(profile)
    assert repository.get_profile(profile.profile_id, profile.version) is None


def test_published_profile_rejects_other_document_citation(tmp_path) -> None:
    repository = _repository(tmp_path)
    authority = _authority()
    repository.save_source_authority(authority)

    criterion = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    other_document = criterion.source.model_copy(
        update={'document_version': 'v9.9, fabricated edition'}
    )
    profile = _published_profile(
        (criterion.model_copy(update={'source': other_document}),)
    )
    with pytest.raises(ValueError, match='does not match the authority document'):
        repository.save_profile(profile)


def test_published_profile_rejects_dangling_extraction_and_kind(tmp_path) -> None:
    repository = _repository(tmp_path)
    authority = _authority()
    repository.save_source_authority(authority)

    criterion = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    missing_extraction = criterion.source.model_copy(
        update={'extraction_id': 'rp22.p99.no-such-extraction'}
    )
    profile = _published_profile(
        (criterion.model_copy(update={'source': missing_extraction}),)
    )
    with pytest.raises(ValueError, match='extraction is not in the authority'):
        repository.save_profile(profile)

    wrong_kind = criterion.source.model_copy(
        update={'content_kind': 'guidance'}
    )
    profile = _published_profile(
        (criterion.model_copy(update={'source': wrong_kind}),)
    )
    with pytest.raises(ValueError, match='content kind'):
        repository.save_profile(profile)

    wrong_reference = criterion.source.model_copy(
        update={'reference': 'Appendix A, Parameter 99'}
    )
    profile = _published_profile(
        (criterion.model_copy(update={'source': wrong_reference}),)
    )
    with pytest.raises(ValueError, match='does not match the extraction'):
        repository.save_profile(profile)


def test_published_profile_persists_with_retained_or_resolved_authority(
    tmp_path,
) -> None:
    authority = _authority()
    profile = _published_profile(
        (_published_criterion(authority, 'rp22.p01.listener-boundary-distance'),)
    )

    # Injected resolver path: resolution at the persistence boundary also pins
    # the authority into the retained store for later audit.
    store = {authority.authority_id: authority}
    repository = _repository(
        tmp_path,
        source_authority_resolver=lambda ref: store.get(ref.authority_id),
    )
    saved = repository.save_profile(profile)
    assert saved == profile
    assert repository.get_source_authority(authority.authority_id) == authority

    # A reopened repository without the resolver still resolves the retained
    # authority, so historical published versions remain auditable.
    reopened = CadStandardsRepository(repository.scene_repository)
    assert reopened.get_profile(profile.profile_id, profile.version) == profile
    assert reopened.resolve_source_authority(authority.ref()) == authority
    assert reopened.save_profile(profile) == profile

    # A ref pointing at the same authority id but a different version/hash is a
    # mismatch, not a resolution.
    mismatched = ExactExternalAuthorityRef(
        authority_id=authority.authority_id,
        authority_version='2',
        semantic_hash_sha256='f' * 64,
    )
    bad_source = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    ).source.model_copy(update={'authority_ref': mismatched})
    bad_profile = _published_profile(
        (
            _published_criterion(
                authority,
                'rp22.p01.listener-boundary-distance',
            ).model_copy(update={'source': bad_source}),
        ),
        profile_id='fixture-published-mismatched',
    )
    with pytest.raises(ValueError, match='source authority mismatch'):
        reopened.save_profile(bad_profile)


def test_user_defined_profiles_stay_functional(tmp_path) -> None:
    repository = _repository(tmp_path)
    authority = _authority()
    repository.save_source_authority(authority)

    plain = build_user_standards_profile(
        profile_id='fixture-user-plain',
        version='1.0',
        name='User profile without authority',
        criteria=(
            CriterionDefinition(
                criterion_id='user.max',
                name='User max',
                source=CriterionSource(
                    publisher='User',
                    document_title='Personal criteria',
                    document_version='1.0',
                    reference='user choice',
                ),
                quantity='user_quantity',
                unit='m',
                applicable_domains=('room',),
                rule=CriterionRule(operator='max', maximum=3.0),
            ),
        ),
    )
    assert repository.save_profile(plain) == plain

    # A user-defined criterion may bind an exact extraction too; the binding is
    # then held to the same exact-match contract.
    bound = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    user_bound = build_user_standards_profile(
        profile_id='fixture-user-bound',
        version='1.0',
        name='User profile bound to an extraction',
        criteria=(bound,),
    )
    assert repository.save_profile(user_bound) == user_bound

    # Dangling refs are rejected for user-defined profiles as well.
    dangling = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    dangling_source = dangling.source.model_copy(
        update={
            'authority_ref': ExactExternalAuthorityRef(
                authority_id='standards-source-authority:' + 'e' * 64,
                authority_version='1',
                semantic_hash_sha256='e' * 64,
            )
        }
    )
    user_dangling = build_user_standards_profile(
        profile_id='fixture-user-dangling',
        version='1.0',
        name='User profile with dangling ref',
        criteria=(dangling.model_copy(update={'source': dangling_source}),),
    )
    with pytest.raises(ValueError, match='source authority does not exist'):
        repository.save_profile(user_dangling)

    assert plain.profile_kind == 'user_defined'
    assert rp22_spatial_profile(2).profile_kind == 'published'


def test_source_authority_self_hash_and_extraction_identity() -> None:
    authority = _authority()
    assert authority.authority_id == (
        'standards-source-authority:' + authority.semantic_hash_sha256
    )
    assert authority.ref().semantic_hash_sha256 == authority.semantic_hash_sha256

    with pytest.raises(ValidationError):
        StandardsSourceAuthority.model_validate(
            {
                **authority.model_dump(mode='python'),
                'semantic_hash_sha256': 'f' * 64,
            }
        )

    duplicated = build_standards_source_authority(
        publisher='Fixture publisher',
        document_title='Fixture document',
        document_version='1.0',
        extractions=(
            CriterionSourceExtraction(
                extraction_id='one',
                reference='§1',
                content_kind='normative',
                quantity='q',
                unit='m',
                rule=CriterionRule(operator='max', maximum=1.0),
            ),
        ),
    )
    assert duplicated.extraction('missing') is None
    with pytest.raises(ValidationError):
        StandardsSourceAuthority.model_validate(
            {
                **duplicated.model_dump(mode='python'),
                'extractions': [
                    item.model_dump(mode='python')
                    for item in (*duplicated.extractions, *duplicated.extractions)
                ],
            }
        )

    # authority_ref and extraction_id must be declared together.
    with pytest.raises(ValidationError):
        CriterionSource(
            publisher='p',
            document_title='t',
            document_version='v',
            reference='r',
            extraction_id='orphan',
        )


def test_validate_criterion_source_authority_direct_binding() -> None:
    authority = _authority()
    criterion = _published_criterion(
        authority,
        'rp22.p01.listener-boundary-distance',
    )
    extraction = validate_criterion_source_authority(criterion, authority)
    assert extraction.extraction_id == 'rp22.p01.listener-boundary-distance'

    level3 = rp22_spatial_source_authority(3)
    with pytest.raises(ValueError, match='authority reference mismatch'):
        validate_criterion_source_authority(criterion, level3)


def test_builtin_profiles_carry_exact_provenance(tmp_path) -> None:
    repository = _repository(tmp_path)
    for authority in builtin_standards_source_authorities():
        repository.save_source_authority(authority)
    for profile in builtin_standards_profiles():
        assert repository.save_profile(profile) == profile
        for criterion in profile.criteria:
            assert criterion.source.authority_ref is not None
            assert criterion.source.extraction_id is not None
            assert criterion.source.content_kind is not None
    # REV63 #805 — the emitted set now spans both profile families and
    # every retained revision (spatial + performance + Dolby prov1/prov2
    # + AURO); the count derives from the builder, not a literal.
    assert len(repository.list_source_authorities()) == len(
        builtin_standards_source_authorities()
    )

    # Published profile persistence still feeds the #170/#410 evaluation path.
    profile = rp22_spatial_profile(2)
    evaluation = evaluate_standards_profile(
        profile=profile,
        target=StandardsEvaluationTarget(
            document_id='doc',
            scene_revision_id='rev',
            scene_content_hash='0' * 64,
            applicable_domains=('seat',),
        ),
        observations=(),
        created_at_utc=NOW,
    )
    assert {r.criterion_id for r in evaluation.results} == {
        c.criterion_id for c in profile.criteria
    }
