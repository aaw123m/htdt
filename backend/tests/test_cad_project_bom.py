from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_project_bom import (
    BOMLineItem,
    CableTakeoffRequirement,
    DesignAuthorityRef,
    OwnedAllocation,
    PurchaseRecord,
    SubstitutionRecord,
    TreatmentTakeoffRequirement,
    build_project_bom,
    build_readiness_summary,
    cable_takeoff_lines,
    diff_boms,
    reconcile_bom,
    treatment_takeoff_lines,
)


NOW = '2026-09-24T00:00:00+00:00'
SCENE = DesignAuthorityRef(
    authority_kind='scene_revision',
    ref_id='rev-9',
    ref_sha256='c' * 64,
)


def _line(line_id: str, **overrides) -> BOMLineItem:
    payload = dict(
        line_id=line_id,
        category='loudspeaker',
        name=line_id,
        quantity=1.0,
        unit='each',
        design_refs=(SCENE,),
        requirement=f'req-{line_id}',
    )
    payload.update(overrides)
    return BOMLineItem(**payload)


def _bom(lines, **overrides):
    payload = dict(
        bom_id='bom-1',
        document_id='doc-1',
        version='1',
        design_refs=(SCENE,),
        line_items=lines,
        generation_policy='manual-v1',
        generated_at_utc=NOW,
    )
    payload.update(overrides)
    return build_project_bom(**payload)


def test_bom_is_reproducible_and_versioned():
    a = _bom((_line('spk-1'),))
    b = _bom((_line('spk-1'),))
    assert a.bom_semantic_hash == b.bom_semantic_hash
    c = _bom((_line('spk-1'), _line('spk-2')), version='2')
    assert c.bom_semantic_hash != a.bom_semantic_hash
    assert c.supersedes_bom_id is None or c.supersedes_bom_id == 'bom-1'


def test_quantity_unit_semantics_keep_cable_in_meters():
    cable = _line('cab-1', category='cable', quantity=64.0, unit='meter')
    assert cable.unit == 'meter'
    with pytest.raises(ValidationError):
        _line('bad', unit='furlong')
    with pytest.raises(ValidationError):
        _line('neg', quantity=0)


def test_cable_takeoff_uses_length_and_declared_waste_only():
    lines = cable_takeoff_lines(
        (
            CableTakeoffRequirement(
                cable_type='14 AWG speaker',
                total_length_m=64.0,
                waste_allowance_m=2.0,
            ),
            CableTakeoffRequirement(
                cable_type='HDMI fiber',
                total_length_m=12.0,
                pre_terminated_runs=1,
            ),
        ),
        design_ref=DesignAuthorityRef(authority_kind='cable_plan', ref_id='cp-1'),
    )
    bulk, hdmi, preterm = lines[0], lines[1], lines[2]
    assert bulk.quantity == 66.0 and bulk.unit == 'meter'
    assert 'waste allowance' in (bulk.note or '')
    assert hdmi.quantity == 12.0
    assert preterm.quantity == 1.0 and preterm.unit == 'each'


def test_treatment_takeoff_preserves_exact_dimensions():
    lines = treatment_takeoff_lines((
        TreatmentTakeoffRequirement(
            treatment_definition_id='absorber-60',
            label='Side absorber',
            count=8,
            panel_dimensions_m=(1.2, 0.6, 0.075),
        ),
        TreatmentTakeoffRequirement(
            treatment_definition_id='absorber-60',
            label='Rear absorber thick',
            count=2,
            panel_dimensions_m=(1.2, 0.6, 0.15),
        ),
    ))
    assert lines[0].quantity == 8.0
    assert '0.075' in (lines[0].note or '')
    assert lines[1].requirement == 'absorber-60'
    # distinct air-gap/thickness configs remain separate lines
    assert len(lines) == 2


def test_owned_reconciliation_classification():
    bom = _bom((
        _line('spk-front', quantity=2.0, requirement='spk-model-x'),
        _line('spk-height', quantity=2.0, requirement='spk-model-y'),
        _line('avr', quantity=1.0, requirement='avr-x'),
        _line('opt', quantity=1.0),
        _line('ghost', quantity=1.0, design_refs=(), resolved_sku=None),
    ))
    rec = {item.line_id: item for item in reconcile_bom(
        bom,
        (
            OwnedAllocation(line_id='spk-front', instance_id='inst-fl', quantity=1),
            OwnedAllocation(line_id='spk-front', instance_id='inst-fr', quantity=1),
            OwnedAllocation(line_id='avr', instance_id='avr-01', quantity=1),
        ),
        optional_line_ids=('opt',),
    )}
    assert rec['spk-front'].classification == 'owned_reusable'
    assert rec['spk-front'].remaining_quantity == 0.0
    assert rec['avr'].classification == 'owned_reusable'
    assert rec['spk-height'].classification == 'newly_required'
    assert rec['opt'].classification == 'optional'
    assert rec['ghost'].classification == 'unresolved'


def test_diff_flags_ordered_no_longer_required():
    old = _bom((
        _line('keep', quantity=1.0),
        _line('qty', quantity=2.0),
        _line('spec', requirement='old-req'),
        _line('gone', procurement_state='ordered'),
    ))
    new = _bom(
        (
            _line('keep', quantity=1.0),
            _line('qty', quantity=3.0),
            _line('spec', requirement='new-req'),
            _line('fresh'),
        ),
        version='2',
        supersedes_bom_id='bom-1',
    )
    diff = diff_boms(old, new)
    assert diff.added == ('fresh',)
    assert diff.removed == ('gone',)
    assert diff.quantity_changed == ('qty',)
    assert diff.spec_changed == ('spec',)
    assert diff.ordered_no_longer_required == ('gone',)


def test_readiness_summary_counts_without_score():
    bom = _bom((
        _line('a', procurement_state='ordered'),
        _line('b', procurement_state='received'),
        _line('c'),
    ))
    rec = reconcile_bom(
        bom, (OwnedAllocation(line_id='b', instance_id='i-1', quantity=1),)
    )
    summary = build_readiness_summary(bom, rec)
    assert summary.required_lines == 3
    assert summary.owned_lines == 1
    assert summary.ordered_lines == 1
    assert summary.received_lines == 1
    assert not hasattr(summary, 'score')


def test_substitution_preserves_original_requirement():
    sub = SubstitutionRecord(
        substitution_id='s-1',
        line_id='spk-1',
        original_requirement='req-spk-1',
        substitute='sku-alternative-b',
        reason='A unavailable',
        revalidation='pending',
        created_at_utc=NOW,
    )
    assert sub.original_requirement == 'req-spk-1'
    assert sub.revalidation == 'pending'


def test_price_never_enters_identity_and_currency_pairs():
    line_a = _line('x', unit_price=100.0, currency='USD')
    line_b = _line('x', unit_price=200.0, currency='JPY')
    # different volatile prices do not change the requirement identity
    assert line_a.requirement_signature() == line_b.requirement_signature()
    with pytest.raises(ValidationError):
        _line('bad', unit_price=10.0)
    with pytest.raises(ValidationError):
        PurchaseRecord(
            record_id='p-1', vendor='v', ordered_at_utc=NOW, total=99.0,
        )
