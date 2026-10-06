import io

# --- native_row_integrity.py: append my bindings before final '}' of the dict ---
p = 'backend/src/htdt/native_row_integrity.py'
s = io.open(p, encoding='utf-8').read()
add = '''    # REV59-UNITS: #728 typed physical quantity.
    'cad_typed_quantities': (
        'payload_json',
        (
            _b('quantity_id', 'quantity_id'),
            _b('quantity_sha256', 'quantity_sha256'),
            _b('document_id', 'document_id'),
            _b('quantity_kind', 'quantity_kind'),
            _b('value_kind', 'value_kind'),
            _b('canonical_value', 'canonical_value'),
            _b('canonical_unit', 'canonical_unit'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_quantity_operations': (
        'payload_json',
        (
            _b('operation_id', 'operation_id'),
            _b('operation_sha256', 'operation_sha256'),
            _b('document_id', 'document_id'),
            _b('operation', 'operation'),
            _b('state', 'state'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-UNITS: #730 engineering-assumption ledger.
    'cad_engineering_assumptions': (
        'payload_json',
        (
            _b('assumption_id', 'assumption_id'),
            _b('assumption_sha256', 'assumption_sha256'),
            _b('document_id', 'document_id'),
            _b('subject_ref_id', 'subject_ref', 'ref_id'),
            _b('assumption_kind', 'assumption_kind'),
            _b('evidence_state', 'evidence_state'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_assumption_resolutions': (
        'payload_json',
        (
            _b('resolution_id', 'resolution_id'),
            _b('resolution_sha256', 'resolution_sha256'),
            _b('document_id', 'document_id'),
            _b('assumption_ref_id', 'assumption_ref', 'ref_id'),
            _b('resolution_state', 'resolution_state'),
            _b('resolved_at_utc', 'resolved_at_utc'),
        ),
        (),
    ),
    'cad_permissible_use_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('intended_use', 'intended_use'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-UNITS: #720 perceptual relevance / audibility.
    'cad_perceptual_model_profiles': (
        'payload_json',
        (
            _b('profile_id', 'profile_id'),
            _b('profile_sha256', 'profile_sha256'),
            _b('document_id', 'document_id'),
            _b('model_kind', 'model_kind'),
            _b('scope_class', 'applicability', 'scope_class'),
            _b('literature_ref', 'literature_ref'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_audibility_assessments': (
        'payload_json',
        (
            _b('assessment_id', 'assessment_id'),
            _b('assessment_sha256', 'assessment_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
            _b('difference_ref_id', 'difference_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
    # REV59-UNITS: #719 residual diagnostic-hypothesis.
    'cad_diagnostic_cases': (
        'payload_json',
        (
            _b('case_id', 'case_id'),
            _b('case_sha256', 'case_sha256'),
            _b('document_id', 'document_id'),
            _b('symptom_ref_id', 'symptom_ref', 'ref_id'),
            _b('status', 'status'),
            _b('opened_at_utc', 'opened_at_utc'),
        ),
        (),
    ),
    'cad_diagnostic_hypotheses': (
        'payload_json',
        (
            _b('hypothesis_id', 'hypothesis_id'),
            _b('hypothesis_sha256', 'hypothesis_sha256'),
            _b('document_id', 'document_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('cause_family', 'cause_family'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_diagnostic_tests': (
        'payload_json',
        (
            _b('test_id', 'test_id'),
            _b('test_sha256', 'test_sha256'),
            _b('document_id', 'document_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('test_kind', 'test_kind'),
            _b('verdict', 'verdict'),
            _b('declared_at_utc', 'declared_at_utc'),
        ),
        (),
    ),
    'cad_diagnostic_verdicts': (
        'payload_json',
        (
            _b('verdict_id', 'verdict_id'),
            _b('verdict_sha256', 'verdict_sha256'),
            _b('document_id', 'document_id'),
            _b('case_ref_id', 'case_ref', 'ref_id'),
            _b('verdict', 'verdict'),
            _b('evaluation_version', 'evaluation_version'),
            _b('evaluated_at_utc', 'evaluated_at_utc'),
        ),
        (),
    ),
'''
old = """    'cad_caption_render_observations': (
        'payload_json',
        (
            _b('observation_id', 'observation_id'),
            _b('observation_sha256', 'observation_sha256'),
            _b('document_id', 'document_id'),
            _b('profile_ref_id', 'profile_ref', 'ref_id'),
        ),
        (),
    ),
}
"""
assert old in s and 'cad_typed_quantities' not in s
s = s.replace(old, old[:-2] + add + '}\n')
io.open(p, 'w', encoding='utf-8').write(s)

# --- native_authority_audit.py ---
p = 'backend/src/htdt/native_authority_audit.py'
s = io.open(p, encoding='utf-8').read()

factory_old = """            return CadPresentationProfileRepository(scene)
        raise KeyError(name)"""
factory_new = """            return CadPresentationProfileRepository(scene)
        # REV59-UNITS authorities.
        if name == 'typed_quantity':
            from .cad_typed_quantity_repository import (
                CadTypedQuantityRepository,
            )

            return CadTypedQuantityRepository(scene)
        if name == 'assumption_ledger':
            from .cad_assumption_ledger_repository import (
                CadAssumptionLedgerRepository,
            )

            return CadAssumptionLedgerRepository(scene)
        if name == 'audibility':
            from .cad_audibility_repository import (
                CadAudibilityRepository,
            )

            return CadAudibilityRepository(scene)
        if name == 'diagnostic_hypothesis':
            from .cad_diagnostic_hypothesis_repository import (
                CadDiagnosticHypothesisRepository,
            )

            return CadDiagnosticHypothesisRepository(scene)
        raise KeyError(name)"""
assert factory_old in s
s = s.replace(factory_old, factory_new, 1)

probe_tail = """    _ReplayProbe(
        'caption_render_observation',
        'cad_caption_render_observations',
        ('observation_id',),
        _get('presentation_profile', 'get_caption_observation'),
    ),
)
"""
probe_add = """    _ReplayProbe(
        'caption_render_observation',
        'cad_caption_render_observations',
        ('observation_id',),
        _get('presentation_profile', 'get_caption_observation'),
    ),
    # REV59-UNITS: #728 typed physical quantity
    _ReplayProbe(
        'typed_quantity',
        'cad_typed_quantities',
        ('quantity_id',),
        _get('typed_quantity', 'get_quantity'),
    ),
    _ReplayProbe(
        'quantity_operation',
        'cad_quantity_operations',
        ('operation_id',),
        _get('typed_quantity', 'get_operation'),
    ),
    # REV59-UNITS: #730 engineering-assumption ledger
    _ReplayProbe(
        'engineering_assumption',
        'cad_engineering_assumptions',
        ('assumption_id',),
        _get('assumption_ledger', 'get_assumption'),
    ),
    _ReplayProbe(
        'assumption_resolution',
        'cad_assumption_resolutions',
        ('resolution_id',),
        _get('assumption_ledger', 'get_resolution'),
    ),
    _ReplayProbe(
        'permissible_use_assessment',
        'cad_permissible_use_assessments',
        ('assessment_id',),
        _get('assumption_ledger', 'get_assessment'),
    ),
    # REV59-UNITS: #720 perceptual relevance / audibility
    _ReplayProbe(
        'perceptual_model_profile',
        'cad_perceptual_model_profiles',
        ('profile_id',),
        _get('audibility', 'get_profile'),
    ),
    _ReplayProbe(
        'audibility_assessment',
        'cad_audibility_assessments',
        ('assessment_id',),
        _get('audibility', 'get_assessment'),
    ),
    # REV59-UNITS: #719 residual diagnostic-hypothesis
    _ReplayProbe(
        'diagnostic_case',
        'cad_diagnostic_cases',
        ('case_id',),
        _get('diagnostic_hypothesis', 'get_case'),
    ),
    _ReplayProbe(
        'diagnostic_hypothesis',
        'cad_diagnostic_hypotheses',
        ('hypothesis_id',),
        _get('diagnostic_hypothesis', 'get_hypothesis'),
    ),
    _ReplayProbe(
        'diagnostic_test',
        'cad_diagnostic_tests',
        ('test_id',),
        _get('diagnostic_hypothesis', 'get_test'),
    ),
    _ReplayProbe(
        'diagnostic_verdict',
        'cad_diagnostic_verdicts',
        ('verdict_id',),
        _get('diagnostic_hypothesis', 'get_verdict'),
    ),
)
"""
assert probe_tail in s
s = s.replace(probe_tail, probe_add, 1)
io.open(p, 'w', encoding='utf-8').write(s)
print('done')
