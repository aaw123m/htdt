"""#1062 — projection-screen evidence registry tests."""

import pytest

from htdt.cad_screen_evidence_registry import (
    PROJECTION_SCREEN_EVIDENCE_REGISTRY,
    build_screen_evidence_registry,
    registry_family_coverage,
    registry_records,
)

R = PROJECTION_SCREEN_EVIDENCE_REGISTRY


def test_registry_covers_three_named_materials():
    models = {r.screen_model for r in R.records}
    assert 'Harmony G3' in models
    assert 'MicroPerf X2 THX Ultra' in models
    assert 'Center Stage XD' in models


def test_families_are_separate():
    coverage = registry_family_coverage(R, 'Harmony G3')
    assert 'peak_gain' in coverage['optical']
    assert 'acoustic_transparency' in coverage['acoustic']
    optical_records = registry_records(R, family='optical')
    assert all(r.family == 'optical' for r in optical_records)
    acoustic_records = registry_records(R, family='acoustic')
    assert all(r.family == 'acoustic' for r in acoustic_records)


def test_manufacturer_claims_not_promoted():
    for record in R.records:
        if record.screen_model in ('Harmony G3', 'Center Stage XD'):
            assert record.evidence_class in (
                'manufacturer_declared',
                'manufacturer_measured',
            )
    independent = registry_records(R, subject='attenuation_db')
    assert any(
        r.evidence_class == 'independent_measured'
        and 'Sound & Vision' in r.locator
        for r in independent
    )


def test_prose_spacing_kept_as_prose():
    record = next(
        r
        for r in R.records
        if r.record_id == 'harmony-g3/acoustic/speaker-distance'
    )
    assert isinstance(record.parsed_value(), str)
    assert '1 inch' in record.parsed_value()
    # never digitised into a numeric distance claim
    assert record.unit is None


def test_records_have_source_and_locator():
    source_ids = {s.source_id for s in R.sources}
    for record in R.records:
        assert record.source_id in source_ids
        assert record.locator


def test_no_quality_scores():
    # the schema has no quality/confidence/score field at all
    fields = type(R.records[0]).model_fields
    assert 'score' not in fields
    assert 'quality' not in fields


def test_registry_integrity():
    probe = R.model_copy(update={'semantic_sha256': '0' * 64})
    with pytest.raises(ValueError):
        type(probe)(**probe.model_dump(mode='python'))


def test_duplicate_record_id_rejected():
    duplicate = R.records[0].model_copy()
    with pytest.raises(ValueError, match='duplicate record_id'):
        build_screen_evidence_registry(
            registry_id='test',
            records=(R.records[0], duplicate),
            sources=R.sources,
        )


def test_unknown_source_rejected():
    orphan = R.records[0].model_copy(
        update={'source_id': 'not-a-source'}
    )
    # recompute record hash so only the registry-level check fires
    import hashlib, json
    def h(payload):
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    probe = type(orphan).model_construct(
        **orphan.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256='',
    )
    fixed = type(orphan)(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=h(probe.semantic_payload()),
    )
    with pytest.raises(ValueError, match='unknown source_id'):
        build_screen_evidence_registry(
            registry_id='test',
            records=(fixed,),
            sources=R.sources,
        )
