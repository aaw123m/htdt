from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from htdt.cad_standards_registry import (
    CriterionCoverageEntry,
    ProfileSourceRegistry,
    StandardsSourceRecord,
    evaluate_coverage,
)

NOW = '2026-09-24T00:00:00+00:00'


def _canonical(v) -> str:
    return json.dumps(
        v, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False,
    )


def _source(
    sid: str = 'standards-source:itu-bs1116-3',
    license_class: str = 'purchased_standard',
    domains: tuple[str, ...] = ('studio_mix_room',),
) -> StandardsSourceRecord:
    payload = {
        'source_id': sid,
        'title': 'ITU-R BS.1116-3 listening test methods',
        'publisher': 'ITU',
        'version_or_edition': 'BS.1116-3',
        'domains': domains,
        'authoritative_scope': 'subjective evaluation methodology',
        'license_class': license_class,
        'authoritative_reference': 'https://www.itu.int/rec/R-REC-BS.1116',
    }
    provisional = StandardsSourceRecord.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return StandardsSourceRecord.model_validate(
        {
            **payload,
            'semantic_sha256': hashlib.sha256(
                _canonical(provisional.identity_payload()).encode('utf-8')
            ).hexdigest(),
        }
    )


def _registry(**overrides) -> ProfileSourceRegistry:
    payload = {
        'registry_id': 'standards-registry:home-playback-1',
        'profile_id': 'home-playback',
        'profile_version': '1',
        'product_domain': 'home_playback',
        'declared_scope': 'home listening room playback calibration',
        'required_source_ids': (),
        'optional_source_ids': (),
        'excluded_source_ids': (),
        'criteria': (),
        **overrides,
    }
    provisional = ProfileSourceRegistry.model_construct(
        **payload, semantic_sha256='0' * 64
    )
    return ProfileSourceRegistry.model_validate(
        {
            **payload,
            'semantic_sha256': hashlib.sha256(
                _canonical(provisional.identity_payload()).encode('utf-8')
            ).hexdigest(),
        }
    )


def test_complete_for_declared_scope() -> None:
    free = _source(
        sid='standards-source:iec-61672-1',
        license_class='free_technical_reference',
        domains=('home_playback', 'generic_measurement'),
    )
    registry = _registry(
        required_source_ids=(free.source_id,),
        criteria=(
            CriterionCoverageEntry(
                criterion_id='weighting-a',
                source_id=free.source_id,
                coverage='IMPLEMENTED',
                implemented_by=('htdt.spl.weighting',),
            ),
        ),
    )
    report = evaluate_coverage(
        (free,), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    assert report.rows[0].status == 'COMPLETE_FOR_DECLARED_SCOPE'


def test_missing_source_is_source_unavailable() -> None:
    registry = _registry(
        required_source_ids=('standards-source:missing-one',),
    )
    report = evaluate_coverage(
        (), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    row = report.rows[0]
    assert row.status == 'SOURCE_UNAVAILABLE'
    assert row.missing_sources == ('standards-source:missing-one',)


def test_license_restricted_source_blocks_completeness() -> None:
    purchased = _source()
    registry = _registry(
        required_source_ids=(purchased.source_id,),
    )
    report = evaluate_coverage(
        (purchased,), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    row = report.rows[0]
    assert row.status == 'LICENSE_RESTRICTED'
    assert row.license_blocked_sources == (purchased.source_id,)


def test_domain_mismatch_needs_review() -> None:
    # a studio-only source claimed by a home-playback profile
    free_studio = _source(
        sid='standards-source:studio-only',
        license_class='free_technical_reference',
        domains=('studio_mix_room',),
    )
    registry = _registry(
        required_source_ids=(free_studio.source_id,),
    )
    report = evaluate_coverage(
        (free_studio,), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    row = report.rows[0]
    assert row.status == 'NEEDS_REVIEW'
    assert row.domain_violations == (free_studio.source_id,)


def test_unimplemented_criteria_make_partial() -> None:
    free = _source(
        sid='standards-source:open-1',
        license_class='free_technical_reference',
        domains=('home_playback',),
    )
    registry = _registry(
        required_source_ids=(free.source_id,),
        criteria=(
            CriterionCoverageEntry(
                criterion_id='criterion-x',
                source_id=free.source_id,
                coverage='NOT_YET_IMPLEMENTED',
                missing_behavior='no code path implements this yet',
            ),
        ),
    )
    report = evaluate_coverage(
        (free,), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    row = report.rows[0]
    assert row.status == 'PARTIAL'
    assert row.unimplemented_criteria == ('criterion-x',)


def test_criterion_must_reference_listed_source() -> None:
    with pytest.raises(ValidationError):
        _registry(
            criteria=(
                CriterionCoverageEntry(
                    criterion_id='orphan',
                    source_id='standards-source:not-listed',
                    coverage='NOT_APPLICABLE',
                ),
            ),
        )


def test_implemented_criterion_requires_artifacts() -> None:
    with pytest.raises(ValidationError):
        CriterionCoverageEntry(
            criterion_id='c1',
            source_id='standards-source:x',
            coverage='IMPLEMENTED',
        )


def test_required_and_excluded_overlap_rejected() -> None:
    with pytest.raises(ValidationError):
        _registry(
            required_source_ids=('standards-source:a',),
            excluded_source_ids=('standards-source:a',),
        )


def test_report_is_deterministic() -> None:
    free = _source(
        sid='standards-source:open-1',
        license_class='free_technical_reference',
        domains=('home_playback',),
    )
    registry = _registry(required_source_ids=(free.source_id,))
    first = evaluate_coverage(
        (free,), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    second = evaluate_coverage(
        (free,), (registry,), report_id='standards-report:r1',
        created_at_utc=NOW,
    )
    assert first.semantic_sha256 == second.semantic_sha256
