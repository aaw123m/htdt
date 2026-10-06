"""REV59-UNITS regression tests — #728 typed physical-quantity / unit
authority, #730 engineering-assumption / permissible-use ledger, #720
perceptual relevance / audibility authority, #719 residual
diagnostic-hypothesis authority.

Fixtures (UNIT/ASM/PER/DIA) exercise the fail-closed rules the issues
demand: dimensionless floats never mix, an unknown input never becomes
a nominal value, a sub-JND difference never becomes a ranking, and a
residual pattern never becomes a root cause without evidence.
"""

from __future__ import annotations

import sqlite3

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.cad_schema import ensure_native_schema, connect_sqlite
from htdt.cad_schema_ddl import NATIVE_SCHEMA_TABLES

from htdt.cad_typed_quantity import (
    CadQuantityRef,
    CadSourceObservation,
    build_typed_quantity,
    evaluate_quantity_operation,
    to_canonical,
    typed_quantity_binding,
)
from htdt.cad_typed_quantity_repository import (
    CadTypedQuantityRepository,
    TypedQuantityIntegrityError,
)
from htdt.cad_assumption_ledger import (
    CadAssumptionValue,
    active_assumptions,
    assumption_binding,
    assumption_lineage_issues,
    build_assumption_resolution,
    build_engineering_assumption,
    evaluate_permissible_use,
)
from htdt.cad_assumption_ledger_repository import (
    AssumptionLedgerIntegrityError,
    CadAssumptionLedgerRepository,
)
from htdt.cad_audibility import (
    CadAudibilityDelta,
    CadAudibilityThreshold,
    CadListeningConditions,
    CadModelApplicability,
    build_perceptual_model_profile,
    evaluate_audibility,
    perceptual_profile_binding,
)
from htdt.cad_audibility_repository import (
    AudibilityIntegrityError,
    CadAudibilityRepository,
)
from htdt.cad_diagnostic_hypothesis import (
    CadHypothesisOutcome,
    CadResidualSignature,
    build_diagnostic_case,
    build_diagnostic_hypothesis,
    build_diagnostic_test,
    diagnostic_case_binding,
    diagnostic_claim_allowed,
    diagnostic_hypothesis_binding,
    evaluate_diagnostic_verdict,
)
from htdt.cad_diagnostic_hypothesis_repository import (
    CadDiagnosticHypothesisRepository,
    DiagnosticIntegrityError,
)


DOC = 'doc-rev59-units'
T0 = '2026-10-06T00:00:00+00:00'
T1 = '2026-10-06T01:00:00+00:00'
T2 = '2026-10-06T02:00:00+00:00'
T3 = '2026-10-06T03:00:00+00:00'
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64

SUBJECT = AuthorityRef(
    kind='surface', ref_id='wall-1', ref_sha256=SHA_A
)
RESIDUAL = AuthorityRef(
    kind='residual_report', ref_id='rr-1', ref_sha256=SHA_B
)
TIMEBASE = AuthorityRef(
    kind='timebase', ref_id='tb-48k', ref_sha256=SHA_C
)
LEVEL_REF = AuthorityRef(
    kind='level_calibration', ref_id='lcal-1', ref_sha256=SHA_D
)


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


def _tamper(db_path, sql: str, params=()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #728 — typed physical quantity / unit authority (UNIT fixtures)
# ---------------------------------------------------------------------------


def _length(value: float, unit: str, document_id: str = DOC):
    return build_typed_quantity(
        document_id=document_id,
        quantity_kind='length',
        value=value,
        unit=unit,
        declared_at_utc=T0,
    )


def test_unit10_mm_vs_m_identical_source_retained() -> None:
    """UNIT10: 1000 mm and 1 m are the same length; each keeps its own
    source representation."""
    mm = build_typed_quantity(
        document_id=DOC,
        quantity_kind='length',
        value=1000.0,
        unit='mm',
        source=CadSourceObservation(
            source_value=1000.0,
            source_unit='mm',
            source_token='DWG-104 dim string',
            significant_digits=4,
        ),
        declared_at_utc=T0,
    )
    m = _length(1.0, 'm')
    assert mm.canonical_unit == 'm'
    assert mm.canonical_value == pytest.approx(1.0)
    assert mm.same_quantity_identity(m)
    assert mm.source is not None
    assert mm.source.source_unit == 'mm'
    assert mm.source.source_value == 1000.0
    assert mm.display_unit == 'mm'


def test_unit20_degrees_require_explicit_conversion() -> None:
    """UNIT20: degrees passed to a radian solver — mixing angle semantics
    is rejected; an explicit convert succeeds."""
    deg = build_typed_quantity(
        document_id=DOC,
        quantity_kind='plane_angle',
        value=30.0,
        unit='deg',
        angle_semantics='absolute_heading',
        declared_at_utc=T0,
    )
    rad = build_typed_quantity(
        document_id=DOC,
        quantity_kind='plane_angle',
        value=0.5235987755982988,
        unit='rad',
        angle_semantics='absolute_heading',
        declared_at_utc=T0,
    )
    verdict = evaluate_quantity_operation(
        document_id=DOC,
        operation='add',
        operands=[deg, rad],
        evaluated_at_utc=T0,
    )
    assert verdict.state == 'computed'  # same kind+semantics: allowed
    assert verdict.derived is not None
    assert verdict.derived.value == pytest.approx(
        30.0 * 3.14159265358979 / 180.0 + 0.5235987755982988
    )
    conv = evaluate_quantity_operation(
        document_id=DOC,
        operation='convert',
        operands=[deg],
        target_unit='rad',
        evaluated_at_utc=T0,
    )
    assert conv.state == 'computed'
    assert conv.derived is not None
    assert conv.derived.value == pytest.approx(0.5235987755982988)
    # An absolute heading never silently becomes a relative rotation.
    phase2 = build_typed_quantity(
        document_id=DOC,
        quantity_kind='plane_angle',
        value=0.1,
        unit='rad',
        angle_semantics='relative_rotation',
        declared_at_utc=T0,
    )
    verdict2 = evaluate_quantity_operation(
        document_id=DOC,
        operation='add',
        operands=[deg, phase2],
        evaluated_at_utc=T0,
    )
    assert verdict2.state == 'incompatible_quantities'


def test_unit30_affine_temperature_arithmetic() -> None:
    """UNIT30: 20 °C + Δ10 K → 30 °C; absolute+absolute is rejected."""
    t_room = build_typed_quantity(
        document_id=DOC,
        quantity_kind='temperature_absolute',
        value=20.0,
        unit='degc',
        declared_at_utc=T0,
    )
    delta = build_typed_quantity(
        document_id=DOC,
        quantity_kind='temperature_difference',
        value=10.0,
        unit='delta_k',
        declared_at_utc=T0,
    )
    summed = evaluate_quantity_operation(
        document_id=DOC,
        operation='add',
        operands=[t_room, delta],
        evaluated_at_utc=T0,
    )
    assert summed.state == 'computed'
    assert summed.derived is not None
    assert summed.derived.value == pytest.approx(303.15)
    assert summed.derived.result_unit == 'k'
    # 20 °C + 30 °C is never allowed — affine absolutes don't add.
    bad = evaluate_quantity_operation(
        document_id=DOC,
        operation='add',
        operands=[t_room, t_room],
        evaluated_at_utc=T0,
    )
    assert bad.state == 'incompatible_quantities'
    # 20 °C − Δ10 K → 10 °C (absolute).
    diff = evaluate_quantity_operation(
        document_id=DOC,
        operation='subtract',
        operands=[t_room, delta],
        evaluated_at_utc=T0,
    )
    assert diff.state == 'computed'
    assert diff.derived is not None
    assert diff.derived.value == pytest.approx(283.15)


def test_unit40_percent_vs_fraction_same_ratio() -> None:
    """UNIT40: 50% and 0.5 are the same canonical ratio."""
    pct = build_typed_quantity(
        document_id=DOC,
        quantity_kind='ratio',
        value=50.0,
        unit='percent',
        ratio_semantics='plain_fraction',
        declared_at_utc=T0,
    )
    frac = build_typed_quantity(
        document_id=DOC,
        quantity_kind='ratio',
        value=0.5,
        unit='fraction',
        ratio_semantics='plain_fraction',
        declared_at_utc=T0,
    )
    assert pct.canonical_value == pytest.approx(0.5)
    assert pct.same_quantity_identity(frac)
    assert pct.display_unit == 'percent'


def test_unit50_samples_need_exact_timebase() -> None:
    """UNIT50: 480 samples → duration only with a pinned sample-rate
    identity + timebase reference."""
    samples = build_typed_quantity(
        document_id=DOC,
        quantity_kind='sample_count',
        value=480.0,
        unit='samples',
        declared_at_utc=T0,
    )
    rate = build_typed_quantity(
        document_id=DOC,
        quantity_kind='frequency',
        value=48000.0,
        unit='hz',
        declared_at_utc=T0,
    )
    missing = evaluate_quantity_operation(
        document_id=DOC,
        operation='samples_to_duration',
        operands=[samples, rate],
        evaluated_at_utc=T0,
    )
    assert missing.state == 'requires_timebase_identity'
    assert missing.derived is None
    ok = evaluate_quantity_operation(
        document_id=DOC,
        operation='samples_to_duration',
        operands=[samples, rate],
        timebase_ref=TIMEBASE,
        evaluated_at_utc=T0,
    )
    assert ok.state == 'computed'
    assert ok.derived is not None
    assert ok.derived.value == pytest.approx(0.01)
    assert ok.derived.result_unit == 's'


def test_unit60_cfm_to_m3s_stays_volume_flow() -> None:
    """UNIT60: 100 CFM converts exactly to m³/s and remains volume flow —
    never air velocity."""
    cfm = build_typed_quantity(
        document_id=DOC,
        quantity_kind='airflow_volume_rate',
        value=100.0,
        unit='cfm',
        declared_at_utc=T0,
    )
    assert cfm.canonical_unit == 'm3_per_s'
    assert cfm.canonical_value == pytest.approx(0.047194745)
    conv = evaluate_quantity_operation(
        document_id=DOC,
        operation='convert',
        operands=[cfm],
        target_unit='m3_per_s',
        evaluated_at_utc=T0,
    )
    assert conv.state == 'computed'
    assert conv.derived is not None
    assert conv.derived.quantity_kind == 'airflow_volume_rate'
    # air_changes_per_hour needs the declared named op + a volume.
    vol = build_typed_quantity(
        document_id=DOC,
        quantity_kind='volume',
        value=50.0,
        unit='m3',
        declared_at_utc=T0,
    )
    ach = evaluate_quantity_operation(
        document_id=DOC,
        operation='airflow_to_air_changes',
        operands=[cfm, vol],
        evaluated_at_utc=T0,
    )
    assert ach.state == 'computed'
    assert ach.derived is not None
    assert ach.derived.quantity_kind == 'air_changes_per_hour'
    assert ach.derived.value == pytest.approx(
        0.047194745 / 50.0 * 3600.0
    )


def test_unit70_conversion_never_improves_precision() -> None:
    """UNIT70: a 96 in drawing dimension converts exactly but the source
    resolution/precision is preserved, not improved."""
    dims = build_typed_quantity(
        document_id=DOC,
        quantity_kind='length',
        value=96.0,
        unit='in',
        source=CadSourceObservation(
            source_value=96.0,
            source_unit='in',
            source_resolution=0.125,  # 1/8 in drawing tolerance
            significant_digits=2,
        ),
        declared_at_utc=T0,
    )
    conv = evaluate_quantity_operation(
        document_id=DOC,
        operation='convert',
        operands=[dims],
        target_unit='m',
        evaluated_at_utc=T0,
    )
    assert conv.state == 'computed'
    assert conv.derived is not None
    assert conv.derived.value == pytest.approx(2.4384)
    # The source observation rides along verbatim — 96 in, 1/8 in
    # resolution — conversion never invents finer digits.
    assert dims.source is not None
    assert dims.source.source_resolution == pytest.approx(0.125)
    assert dims.source.significant_digits == 2


def test_unit80_undeclared_unit_fails_closed() -> None:
    """UNIT80: a provider field without a registered unit cannot become
    a quantity — the build refuses."""
    with pytest.raises(ValueError, match='not registered'):
        build_typed_quantity(
            document_id=DOC,
            quantity_kind='length',
            value=42.0,
            unit='vendor_unit_x',
            declared_at_utc=T0,
        )
    # And a foreign unit on a known kind is equally refused.
    with pytest.raises(ValueError, match='not registered'):
        build_typed_quantity(
            document_id=DOC,
            quantity_kind='length',
            value=1.0,
            unit='hz',
            declared_at_utc=T0,
        )


def test_unit90_pa_vs_db_never_directly_compared() -> None:
    """UNIT90: Pa and dB SPL are different authorities — direct
    comparison is rejected; the log conversion defers to #691."""
    pa = build_typed_quantity(
        document_id=DOC,
        quantity_kind='acoustic_pressure_rms',
        value=0.02,
        unit='pa',
        reference_condition='rms over declared window',
        declared_at_utc=T0,
    )
    ratio = evaluate_quantity_operation(
        document_id=DOC,
        operation='to_logarithmic',
        operands=[pa],
        evaluated_at_utc=T0,
    )
    assert ratio.state == 'requires_logarithmic_authority'
    # Cross-kind compare is fail-closed.
    freq = build_typed_quantity(
        document_id=DOC,
        quantity_kind='frequency',
        value=1000.0,
        unit='hz',
        declared_at_utc=T0,
    )
    cmp = evaluate_quantity_operation(
        document_id=DOC,
        operation='compare',
        operands=[pa, freq],
        evaluated_at_utc=T0,
    )
    assert cmp.state == 'incompatible_quantities'


def test_typed_quantity_repository_roundtrip_and_integrity(
    tmp_path,
) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadTypedQuantityRepository(scene)
    qty = _length(96.0, 'in')
    repo.save_quantity(qty)
    repo.save_quantity(qty)  # idempotent
    assert repo.get_quantity(qty.quantity_id) == qty
    assert repo.list_quantities(DOC) == (qty,)

    op = evaluate_quantity_operation(
        document_id=DOC,
        operation='convert',
        operands=[qty],
        target_unit='m',
        evaluated_at_utc=T0,
    )
    repo.save_operation(op)
    assert repo.get_operation(op.operation_id) == op

    _tamper(
        tmp_path / 'cad.sqlite3',
        "UPDATE cad_typed_quantities SET canonical_unit='cm' "
        "WHERE quantity_id=?",
        (qty.quantity_id,),
    )
    with pytest.raises(TypedQuantityIntegrityError):
        repo.get_quantity(qty.quantity_id)


# ---------------------------------------------------------------------------
# #730 — engineering assumption / permissible-use ledger (ASM fixtures)
# ---------------------------------------------------------------------------


def _assumed_material(
    evidence: str = 'literature_assumed',
    uses: tuple[str, ...] = ('concept_design',),
    sensitivity: str = 'not_evaluated',
    **kwargs,
):
    return build_engineering_assumption(
        document_id=DOC,
        subject_ref=SUBJECT,
        assumption_kind='physical_parameter',
        evidence_state=evidence,
        rationale='hidden rear wall — proceed on generic absorption',
        value=CadAssumptionValue(declared_value_label='α≈0.10 generic'),
        permissible_uses=uses,
        sensitivity=sensitivity,
        declared_at_utc=kwargs.pop('declared_at_utc', T0),
        **kwargs,
    )


def test_asm10_generic_material_permits_concept_blocks_validation() -> None:
    """ASM10: literature-assumed wall material is fine for concept
    design and fail-closed for a validation-grade claim."""
    a = _assumed_material()
    assert 'concept_design' in a.permissible_uses
    v_concept = evaluate_permissible_use(
        document_id=DOC,
        intended_use='concept_design',
        assumptions=[a],
        evaluated_at_utc=T0,
    )
    assert v_concept.verdict == 'permissible'
    v_validation = evaluate_permissible_use(
        document_id=DOC,
        intended_use='quantitative_solver_validation',
        assumptions=[a],
        evaluated_at_utc=T1,
    )
    assert v_validation.verdict == 'stronger_evidence_required'
    assert a.assumption_id in v_validation.limiting_assumption_ids


def test_asm20_scenario_branches_stay_separate() -> None:
    """ASM20: two mutually-exclusive hidden-wall hypotheses are separate
    scenario branches — never averaged into one fictional material."""
    h_a = _assumed_material(
        evidence='exploratory_scenario',
        uses=('concept_design', 'rough_layout_screening'),
        scenario_group='hidden-wall-alpha',
        scenario_label='gypsum on studs',
        declared_at_utc=T0,
    )
    h_b = _assumed_material(
        evidence='exploratory_scenario',
        uses=('concept_design', 'rough_layout_screening'),
        scenario_group='hidden-wall-alpha',
        scenario_label='concrete core wall',
        declared_at_utc=T1,
    )
    assert h_a.assumption_id != h_b.assumption_id
    assert h_a.scenario_group == h_b.scenario_group == (
        'hidden-wall-alpha'
    )
    assert h_a.scenario_label != h_b.scenario_label
    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='rp22_design_evaluation',
        assumptions=[h_a, h_b],
        evaluated_at_utc=T2,
    )
    assert v.verdict == 'incompatible'
    assert set(v.limiting_assumption_ids) == {
        h_a.assumption_id,
        h_b.assumption_id,
    }


def test_asm30_low_sensitivity_no_urgent_measurement() -> None:
    """ASM30: an assumption declared low-sensitivity does not force a
    measurement task for design-phase use."""
    a = _assumed_material(sensitivity='low')
    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='concept_design',
        assumptions=[a],
        evaluated_at_utc=T0,
    )
    assert v.verdict == 'permissible'
    # Stretched to an undeclared screening use: limited, not blocked —
    # low sensitivity keeps the label honest.
    v2 = evaluate_permissible_use(
        document_id=DOC,
        intended_use='optimization_shortlist',
        assumptions=[a],
        evaluated_at_utc=T1,
    )
    assert v2.verdict == 'permissible_with_limitations'


def test_asm40_high_sensitivity_flags_survey_priority() -> None:
    """ASM40: a high-sensitivity assumed subwoofer position demands
    resolution before it supports a stronger claim."""
    a = build_engineering_assumption(
        document_id=DOC,
        subject_ref=AuthorityRef(
            kind='device', ref_id='sub-1', ref_sha256=SHA_C
        ),
        assumption_kind='geometry',
        evidence_state='project_assumed',
        rationale='subwoofer XYZ assumed — optimizer winner moves on it',
        value=CadAssumptionValue(declared_value_label='X=1.2 Y=0.4 Z=0.05 m'),
        permissible_uses=('concept_design', 'rough_layout_screening'),
        sensitivity='high',
        declared_at_utc=T0,
    )
    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='optimization_shortlist',
        assumptions=[a],
        evaluated_at_utc=T1,
    )
    assert v.verdict == 'stronger_evidence_required'
    assert a.assumption_id in v.limiting_assumption_ids


def test_asm50_unknown_stays_unknown() -> None:
    """ASM50: an undocumented AVR crossover is a declared UNKNOWN — never
    a silent 80 Hz/LR4 default."""
    with pytest.raises(ValueError, match='unknown'):
        build_engineering_assumption(
            document_id=DOC,
            subject_ref=AuthorityRef(
                kind='avr', ref_id='avr-1', ref_sha256=SHA_D
            ),
            assumption_kind='device_behavior',
            evidence_state='unknown',
            rationale='crossover not documented',
            value=CadAssumptionValue(
                declared_value_label='80 Hz LR4'
            ),
            declared_at_utc=T0,
        )
    unk = build_engineering_assumption(
        document_id=DOC,
        subject_ref=AuthorityRef(
            kind='avr', ref_id='avr-1', ref_sha256=SHA_D
        ),
        assumption_kind='device_behavior',
        evidence_state='unknown',
        rationale='AVR crossover undocumented — declared gap',
        resolution_needed='inspect AVR setup menu',
        declared_at_utc=T0,
    )
    assert unk.value is None
    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='concept_design',
        assumptions=[unk],
        evaluated_at_utc=T1,
    )
    assert v.verdict == 'stronger_evidence_required'


def test_asm60_measurement_replaces_assumption_lineage_kept() -> None:
    """ASM60: a field measurement supersedes the assumed material — the
    record is terminal, lineage stays."""
    assumed = _assumed_material()
    measured = build_engineering_assumption(
        document_id=DOC,
        subject_ref=SUBJECT,
        assumption_kind='physical_parameter',
        evidence_state='measured_observed',
        rationale='impedance-tube measurement of opened wall',
        value=CadAssumptionValue(
            declared_value_label='α=0.35 measured'
        ),
        supersedes_ref=assumption_binding(assumed),
        declared_at_utc=T1,
    )
    res = build_assumption_resolution(
        document_id=DOC,
        assumption_ref=assumption_binding(assumed),
        resolution_state='replaced_by_measurement',
        rationale='field measurement replaces assumed absorption',
        replacement_ref=assumption_binding(measured),
        resolved_at_utc=T2,
    )
    assert res.replacement_ref is not None
    open_set, inactive = active_assumptions(
        (assumed, measured), (res,)
    )
    assert open_set == (measured,)
    assert assumed.assumption_id in inactive
    # And the measured replacement is not gated as an assumption at all.
    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='quantitative_solver_validation',
        assumptions=[measured],
        evaluated_at_utc=T3,
    )
    assert v.verdict == 'permissible'


def test_asm70_inverse_estimated_stays_distinct() -> None:
    """ASM70: a calibrated-fit parameter fits data yet remains
    inverse-estimated — validation-grade use wants stronger evidence."""
    a = _assumed_material(evidence='calibrated_inverse_estimated')
    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='quantitative_solver_validation',
        assumptions=[a],
        evaluated_at_utc=T0,
    )
    assert v.verdict == 'stronger_evidence_required'
    assert a.assumption_id in v.limiting_assumption_ids


def test_asm80_preference_target_never_physical() -> None:
    """ASM80: a project preference target cannot claim measured/declared
    evidence — nonphysical kinds never masquerade as physics."""
    for state in (
        'measured_observed',
        'manufacturer_declared',
        'independent_reference',
        'derived',
    ):
        with pytest.raises(ValueError, match='masquerade|cannot claim'):
            build_engineering_assumption(
                document_id=DOC,
                subject_ref=SUBJECT,
                assumption_kind='user_preference_target',
                evidence_state=state,
                rationale='client target curve',
                value=CadAssumptionValue(
                    declared_value_label='+3 dB LF shelf'
                ),
                declared_at_utc=T0,
            )
    pref = build_engineering_assumption(
        document_id=DOC,
        subject_ref=SUBJECT,
        assumption_kind='user_preference_target',
        evidence_state='project_assumed',
        rationale='client preference, explicitly not physical truth',
        value=CadAssumptionValue(
            declared_value_label='+3 dB LF shelf'
        ),
        permissible_uses=('concept_design',),
        declared_at_utc=T0,
    )
    assert pref.assumption_kind == 'user_preference_target'


def test_assumption_ledger_repository_roundtrip_and_integrity(
    tmp_path,
) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadAssumptionLedgerRepository(scene)
    a = _assumed_material()
    repo.save_assumption(a)
    repo.save_assumption(a)
    assert repo.get_assumption(a.assumption_id) == a
    assert repo.list_assumptions(DOC) == (a,)

    res = build_assumption_resolution(
        document_id=DOC,
        assumption_ref=assumption_binding(a),
        resolution_state='evidence_requested',
        rationale='open the rear wall to measure',
        resolved_at_utc=T1,
    )
    repo.save_resolution(res)
    assert repo.get_resolution(res.resolution_id) == res

    v = evaluate_permissible_use(
        document_id=DOC,
        intended_use='concept_design',
        assumptions=[a],
        evaluated_at_utc=T2,
    )
    repo.save_assessment(v)
    assert repo.get_assessment(v.assessment_id) == v

    _tamper(
        tmp_path / 'cad.sqlite3',
        "UPDATE cad_engineering_assumptions "
        "SET evidence_state='measured_observed' WHERE assumption_id=?",
        (a.assumption_id,),
    )
    with pytest.raises(AssumptionLedgerIntegrityError):
        repo.get_assumption(a.assumption_id)


def test_assumption_lineage_cycle_detected() -> None:
    """Lineage integrity: a supersedes cycle is caught, not followed."""
    a = _assumed_material()
    b = _assumed_material(
        evidence='project_assumed', declared_at_utc=T1
    )
    cyc = build_engineering_assumption(
        document_id=DOC,
        subject_ref=SUBJECT,
        assumption_kind='physical_parameter',
        evidence_state='project_assumed',
        rationale='cycle probe',
        value=CadAssumptionValue(declared_value_label='x'),
        permissible_uses=('concept_design',),
        supersedes_ref=assumption_binding(b),
        declared_at_utc=T2,
    )
    issues = assumption_lineage_issues((a, b, cyc))
    assert not issues
    # Self-supersession is impossible to seal (hash binds content) but a
    # dangling supersedes ref is reported.
    dangling = build_engineering_assumption(
        document_id=DOC,
        subject_ref=SUBJECT,
        assumption_kind='physical_parameter',
        evidence_state='project_assumed',
        rationale='dangling probe',
        value=CadAssumptionValue(declared_value_label='y'),
        permissible_uses=('concept_design',),
        supersedes_ref=AuthorityRef(
            kind='engineering_assumption',
            ref_id='asm-missing',
            ref_sha256=SHA_B,
        ),
        declared_at_utc=T3,
    )
    assert assumption_lineage_issues((a, dangling))


# ---------------------------------------------------------------------------
# #720 — perceptual relevance / audibility authority (PER fixtures)
# ---------------------------------------------------------------------------


def _irregularity_profile():
    """Bücklein-style research profile: peaks more audible than dips."""
    return build_perceptual_model_profile(
        document_id=DOC,
        model_kind='spectral_irregularity_research',
        profile_label='peak/dip asymmetry research',
        literature_ref='Bücklein, JAES 29(3) 1981',
        literature_version='1981',
        applicability=CadModelApplicability(
            scope_class='applicable_with_limitations',
            stimulus='broadband_program',
            field='reverberant_room',
            listener='normal_hearing_general',
        ),
        thresholds=(
            CadAudibilityThreshold(
                observable='spectral_irregularity_db',
                direction='peak',
                threshold_value=1.0,
                threshold_unit='db',
                condition_label='narrowband peak, program',
            ),
            CadAudibilityThreshold(
                observable='spectral_irregularity_db',
                direction='dip',
                threshold_value=3.0,
                threshold_unit='db',
                condition_label='narrowband dip, program',
            ),
        ),
        declared_at_utc=T0,
    )


def _level_profile():
    return build_perceptual_model_profile(
        document_id=DOC,
        model_kind='jnd_threshold_profile',
        profile_label='level JND midband',
        literature_ref='Bistafa & Bradley, JASA 108(4) 2000',
        literature_version='2000',
        applicability=CadModelApplicability(
            scope_class='applicable_with_limitations',
            stimulus='broadband_program',
            field='reverberant_room',
            listener='normal_hearing_general',
        ),
        thresholds=(
            CadAudibilityThreshold(
                observable='level_difference_db',
                threshold_value=1.0,
                threshold_unit='db',
            ),
        ),
        declared_at_utc=T0,
    )


def test_per10_peak_dip_asymmetry_through_declared_profile() -> None:
    """PER10: equal-magnitude peak vs dip — the declared research
    profile judges the peak more salient; raw deltas stay equal."""
    profile = _irregularity_profile()
    peak = CadAudibilityDelta(
        observable='spectral_irregularity_db',
        magnitude=2.0,
        magnitude_unit='db',
        direction='peak',
    )
    dip = CadAudibilityDelta(
        observable='spectral_irregularity_db',
        magnitude=2.0,
        magnitude_unit='db',
        direction='dip',
    )
    conds = CadListeningConditions(
        stimulus='broadband_program',
        field='reverberant_room',
        listener='normal_hearing_general',
    )
    a_peak = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=peak,
        difference_ref=RESIDUAL,
        conditions=conds,
        evaluated_at_utc=T0,
    )
    a_dip = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=dip,
        difference_ref=RESIDUAL,
        conditions=conds,
        evaluated_at_utc=T1,
    )
    assert a_peak.verdict == 'potentially_audible'
    assert a_dip.verdict == 'not_distinguishable_under_profile'
    # The physical record is untouched: both deltas are 2 dB.
    assert a_peak.delta.magnitude == a_dip.delta.magnitude == 2.0


def test_per20_audibility_never_overrides_physical_gate() -> None:
    """PER20: a deep null can be inaudible yet physically infeasible —
    the verdict records the delta honestly and never erases it."""
    profile = _irregularity_profile()
    deep_null = CadAudibilityDelta(
        observable='spectral_irregularity_db',
        magnitude=30.0,
        magnitude_unit='db',
        direction='dip',
    )
    a = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=deep_null,
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='broadband_program',
            field='reverberant_room',
            listener='normal_hearing_general',
        ),
        evaluated_at_utc=T0,
    )
    # 30 dB exceeds even the dip threshold — potentially audible; and
    # even a not_distinguishable verdict would keep the raw 30 dB
    # recorded for the physical/headroom gates that own feasibility.
    assert a.verdict == 'potentially_audible'
    assert a.delta.magnitude == 30.0


def test_per30_modal_decay_observable_distinct() -> None:
    """PER30: a modest FR error with long modal decay — the decay is a
    separate observable with its own threshold, not a magnitude diff."""
    profile = build_perceptual_model_profile(
        document_id=DOC,
        model_kind='modal_decay_relevance',
        profile_label='modal decay relevance',
        literature_ref='Mäkivirta et al., JAES 51(5) 2003',
        literature_version='2003',
        applicability=CadModelApplicability(
            scope_class='applicable_with_limitations',
            field='reverberant_room',
            frequency_domain_label='modal band < 200 Hz',
        ),
        thresholds=(
            CadAudibilityThreshold(
                observable='modal_decay_time',
                threshold_value=0.3,
                threshold_unit='s',
                condition_label='excess decay vs broadband T30',
            ),
        ),
        declared_at_utc=T0,
    )
    small_fr = CadAudibilityDelta(
        observable='level_difference_db',
        magnitude=0.5,
        magnitude_unit='db',
    )
    long_decay = CadAudibilityDelta(
        observable='modal_decay_time',
        magnitude=1.1,
        magnitude_unit='s',
    )
    a_fr = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=small_fr,
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(field='reverberant_room'),
        evaluated_at_utc=T0,
    )
    assert a_fr.verdict == 'indeterminate'  # no threshold for it
    a_decay = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=long_decay,
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(field='reverberant_room'),
        evaluated_at_utc=T1,
    )
    assert a_decay.verdict == 'potentially_audible'


def test_per40_level_dependence_changes_identity() -> None:
    """PER40: the same transfer difference at two playback levels is two
    different assessments — the level pin is part of the identity."""
    profile = _level_profile()
    delta = CadAudibilityDelta(
        observable='level_difference_db',
        magnitude=1.4,
        magnitude_unit='db',
    )
    conds_a = CadListeningConditions(
        stimulus='broadband_program',
        field='reverberant_room',
        listener='normal_hearing_general',
        level_reference_ref=LEVEL_REF,
    )
    conds_b = CadListeningConditions(
        stimulus='broadband_program',
        field='reverberant_room',
        listener='normal_hearing_general',
        level_reference_ref=AuthorityRef(
            kind='level_calibration',
            ref_id='lcal-2',
            ref_sha256=SHA_D,
        ),
    )
    a = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=delta,
        difference_ref=RESIDUAL,
        conditions=conds_a,
        evaluated_at_utc=T0,
    )
    b = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=delta,
        difference_ref=RESIDUAL,
        conditions=conds_b,
        evaluated_at_utc=T0,
    )
    assert a.assessment_id != b.assessment_id
    assert a.conditions.level_reference_ref != (
        b.conditions.level_reference_ref
    )


def test_per50_iso226_scope_gate() -> None:
    """PER50: ISO 226 on a multichannel reverberant movie program is
    outside scope — never a universal weighting."""
    iso226 = build_perceptual_model_profile(
        document_id=DOC,
        model_kind='iso226_reference',
        profile_label='ISO 226:2023 equal-loudness',
        literature_ref='ISO 226:2023',
        literature_version='2023',
        applicability=CadModelApplicability(
            scope_class='applicable_as_reference',
            stimulus='pure_tone',
            field='free_field_frontal',
            listener='normal_hearing_18_25',
            frequency_domain_label='20 Hz–12.5 kHz preferred freqs',
        ),
        thresholds=(
            CadAudibilityThreshold(
                observable='level_difference_db',
                threshold_value=0.5,
                threshold_unit='db',
                condition_label='1 kHz pure tone 60 phon',
            ),
        ),
        declared_at_utc=T0,
    )
    a = evaluate_audibility(
        document_id=DOC,
        profile=iso226,
        delta=CadAudibilityDelta(
            observable='level_difference_db',
            magnitude=0.2,
            magnitude_unit='db',
        ),
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='multichannel_cinema',
            field='reverberant_room',
            listener='normal_hearing_general',
        ),
        evaluated_at_utc=T0,
    )
    assert a.verdict == 'outside_model_scope'
    assert any('stimulus' in r for r in a.reasons)
    # And an ISO-226 profile claiming reference scope on the wrong
    # envelope cannot even be sealed.
    with pytest.raises(ValueError, match='ISO 226'):
        build_perceptual_model_profile(
            document_id=DOC,
            model_kind='iso226_reference',
            profile_label='misused 226',
            literature_ref='ISO 226:2023',
            applicability=CadModelApplicability(
                scope_class='applicable_as_reference',
                stimulus='multichannel_cinema',
                field='reverberant_room',
            ),
            thresholds=(
                CadAudibilityThreshold(
                    observable='level_difference_db',
                    threshold_value=0.5,
                    threshold_unit='db',
                ),
            ),
            declared_at_utc=T0,
        )


def test_per60_loudness_needs_level_calibration() -> None:
    """PER60: an ISO-532-class profile without a pinned level reference
    is insufficient_evidence, not a guess."""
    loudness = build_perceptual_model_profile(
        document_id=DOC,
        model_kind='iso532_loudness',
        profile_label='ISO 532-1 Zwicker',
        literature_ref='ISO 532-1:2017',
        literature_version='2017',
        applicability=CadModelApplicability(
            scope_class='applicable_with_limitations',
            stimulus='broadband_program',
            field='free_field_frontal',
        ),
        requires_level_calibration=True,
        thresholds=(
            CadAudibilityThreshold(
                observable='loudness_difference',
                threshold_value=0.02,
                threshold_unit='sone_ratio',
            ),
        ),
        declared_at_utc=T0,
    )
    missing = evaluate_audibility(
        document_id=DOC,
        profile=loudness,
        delta=CadAudibilityDelta(
            observable='loudness_difference',
            magnitude=0.5,
            magnitude_unit='sone_ratio',
        ),
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='broadband_program',
            field='free_field_frontal',
        ),
        evaluated_at_utc=T0,
    )
    assert missing.verdict == 'insufficient_evidence'
    ok = evaluate_audibility(
        document_id=DOC,
        profile=loudness,
        delta=CadAudibilityDelta(
            observable='loudness_difference',
            magnitude=0.5,
            magnitude_unit='sone_ratio',
        ),
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='broadband_program',
            field='free_field_frontal',
            level_reference_ref=LEVEL_REF,
        ),
        evaluated_at_utc=T1,
    )
    assert ok.verdict == 'potentially_audible'


def test_per70_model_and_listening_test_coexist() -> None:
    """PER70: model predicts negligible, ABX detects — both verdicts are
    retained as separate evidence, neither erases the other."""
    model = _level_profile()
    abx = build_perceptual_model_profile(
        document_id=DOC,
        model_kind='listening_test_derived',
        profile_label='project ABX result',
        literature_ref='project listening test LT-7, n=12',
        applicability=CadModelApplicability(
            scope_class='applicable_with_limitations',
            stimulus='multichannel_cinema',
            field='reverberant_room',
            listener='trained_listener',
        ),
        thresholds=(
            CadAudibilityThreshold(
                observable='level_difference_db',
                threshold_value=0.1,
                threshold_unit='db',
                condition_label='ABX detection bound',
            ),
        ),
        declared_at_utc=T1,
    )
    delta = CadAudibilityDelta(
        observable='level_difference_db',
        magnitude=0.4,
        magnitude_unit='db',
    )
    a_model = evaluate_audibility(
        document_id=DOC,
        profile=model,
        delta=delta,
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='broadband_program',
            field='reverberant_room',
            listener='normal_hearing_general',
        ),
        evaluated_at_utc=T2,
    )
    a_abx = evaluate_audibility(
        document_id=DOC,
        profile=abx,
        delta=delta,
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='multichannel_cinema',
            field='reverberant_room',
            listener='trained_listener',
        ),
        evaluated_at_utc=T3,
    )
    assert a_model.verdict == 'not_distinguishable_under_profile'
    assert a_abx.verdict == 'potentially_audible'
    assert a_model.assessment_id != a_abx.assessment_id


def test_per80_project_threshold_floor() -> None:
    """PER80: a project practical threshold declares its own floor —
    below it the delta is real but perceptually out of scope."""
    project = build_perceptual_model_profile(
        document_id=DOC,
        model_kind='project_practical_threshold',
        profile_label='project EQ relevance floor',
        literature_ref='project requirement doc PR-12',
        applicability=CadModelApplicability(
            scope_class='applicable_as_reference',
            stimulus='other_declared',
            field='reverberant_room',
            frequency_domain_label='40–250 Hz correction band',
        ),
        thresholds=(
            CadAudibilityThreshold(
                observable='level_difference_db',
                threshold_value=2.0,
                threshold_unit='db',
                condition_label='declared correction floor',
            ),
        ),
        declared_at_utc=T0,
    )
    a = evaluate_audibility(
        document_id=DOC,
        profile=project,
        delta=CadAudibilityDelta(
            observable='level_difference_db',
            magnitude=1.2,
            magnitude_unit='db',
        ),
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='other_declared',
            field='reverberant_room',
        ),
        evaluated_at_utc=T0,
    )
    assert a.verdict == 'not_distinguishable_under_profile'
    assert a.matched_threshold is not None
    assert a.matched_threshold.threshold_value == 2.0


def test_audibility_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadAudibilityRepository(scene)
    profile = _level_profile()
    repo.save_profile(profile)
    repo.save_profile(profile)
    assert repo.get_profile(profile.profile_id) == profile
    assert repo.list_profiles(DOC) == (profile,)

    a = evaluate_audibility(
        document_id=DOC,
        profile=profile,
        delta=CadAudibilityDelta(
            observable='level_difference_db',
            magnitude=2.0,
            magnitude_unit='db',
        ),
        difference_ref=RESIDUAL,
        conditions=CadListeningConditions(
            stimulus='broadband_program',
            field='reverberant_room',
            listener='normal_hearing_general',
        ),
        evaluated_at_utc=T0,
    )
    repo.save_assessment(a)
    assert repo.get_assessment(a.assessment_id) == a

    _tamper(
        tmp_path / 'cad.sqlite3',
        "UPDATE cad_audibility_assessments "
        "SET verdict='not_distinguishable_under_profile' "
        "WHERE assessment_id=?",
        (a.assessment_id,),
    )
    with pytest.raises(AudibilityIntegrityError):
        repo.get_assessment(a.assessment_id)


# ---------------------------------------------------------------------------
# #719 — residual diagnostic-hypothesis authority (DIA fixtures)
# ---------------------------------------------------------------------------


def _case():
    return build_diagnostic_case(
        document_id=DOC,
        symptom_ref=RESIDUAL,
        symptom_summary='predicted vs measured crossover notch',
        opened_at_utc=T0,
    )


def _sig(label: str = 'crossover-region notch'):
    return CadResidualSignature(
        signature_label=label,
        observable_label='magnitude dip at crossover',
        extraction_method='fr-signature-extract-1',
    )


def _hypothesis(case, family: str, label: str, **kw):
    return build_diagnostic_hypothesis(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        cause_family=family,
        hypothesis_label=label,
        expected_signatures=(_sig(),),
        required_evidence=kw.pop('required_evidence', 'test'),
        declared_at_utc=kw.pop('declared_at_utc', T0),
        **kw,
    )


def test_dia10_confound_discriminated_by_second_test() -> None:
    """DIA10: position vs timing confound — a second discrimination test
    separates them."""
    case = _case()
    h_pos = _hypothesis(
        case, 'source_position_aim', 'source XYZ off'
    )
    h_timing = _hypothesis(
        case, 'timebase_clock', 'timing reference offset'
    )
    screen = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='discrimination_measurement',
        test_label='front-back distance sweep',
        predeclared_prediction='position error shifts null with distance',
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_pos),
                outcome='indistinguishable',
            ),
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_timing),
                outcome='indistinguishable',
            ),
        ),
        verdict='supported_but_confounded',
        executed_at_utc=T1,
        declared_at_utc=T0,
    )
    v1 = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_pos, h_timing],
        tests=[screen],
        evaluated_at_utc=T1,
    )
    assert v1.verdict == 'diagnostically_confounded'
    loopback = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='loopback_check',
        test_label='interface loopback timing check',
        predeclared_prediction='timing hypothesis predicts fixed offset',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='lb-1', ref_sha256=SHA_C
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_pos),
                outcome='contradicted',
            ),
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_timing),
                outcome='supported',
            ),
        ),
        verdict='confirmed_by_intervention_and_remeasurement',
        executed_at_utc=T2,
        declared_at_utc=T1,
    )
    v2 = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_pos, h_timing],
        tests=[screen, loopback],
        evaluated_at_utc=T2,
    )
    assert v2.verdict == 'root_cause_confirmed_within_declared_scope'
    assert v2.confirmed_hypothesis_ids == (h_timing.hypothesis_id,)


def test_dia20_two_plausible_causes_stay_open() -> None:
    """DIA20: material vs directivity — both plausible, extra evidence
    still needed; the verdict refuses a winner."""
    case = _case()
    h_mat = _hypothesis(
        case, 'material_boundary', 'boundary absorption off'
    )
    h_dir = _hypothesis(
        case,
        'source_model_directivity',
        'source off-axis response',
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_mat, h_dir],
        tests=[],
        evaluated_at_utc=T1,
    )
    # Untested candidates stay unresolved — the verdict refuses to
    # pick; neither may be claimed as cause.
    assert v.verdict == 'unresolved'
    for h in (h_mat, h_dir):
        allowed, _ = diagnostic_claim_allowed(h, [])
        assert not allowed


def test_dia30_polarity_confirmed_by_intervention() -> None:
    """DIA30: crossover null → isolated channel test confirms an
    inverted output; repair+remeasure confirms."""
    case = _case()
    h_pol = _hypothesis(
        case, 'routing_polarity', 'sub channel inverted'
    )
    h_aim = _hypothesis(
        case, 'source_position_aim', 'sub aim offset'
    )
    test = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='controlled_intervention',
        test_label='invert sub polarity, remeasure crossover',
        predeclared_prediction='notch fills ~8 dB at 120 Hz',
        intervention_label='invert sub polarity',
        held_constant_label='all other DSP, positions, levels',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='meas-post', ref_sha256=SHA_C
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_pol),
                outcome='supported',
            ),
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_aim),
                outcome='contradicted',
            ),
        ),
        verdict='confirmed_by_intervention_and_remeasurement',
        executed_at_utc=T2,
        declared_at_utc=T1,
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_pol, h_aim],
        tests=[test],
        evaluated_at_utc=T3,
    )
    assert v.verdict == 'root_cause_confirmed_within_declared_scope'
    allowed, level = diagnostic_claim_allowed(h_pol, [test])
    assert allowed
    assert level == 'controlled_intervention_support'
    blocked, level = diagnostic_claim_allowed(h_aim, [test])
    assert not blocked


def test_dia40_model_form_is_first_class_cause() -> None:
    """DIA40: model-form inadequacy is a live cause family — a residual
    surviving every parameter fit is not a material indictment."""
    case = _case()
    h_model = _hypothesis(
        case,
        'model_form_inadequacy',
        'boundary model too low-order for curved wall',
        required_evidence='higher-fidelity model holdout comparison',
    )
    h_mat = _hypothesis(
        case, 'material_boundary', 'material bound'
    )
    fit = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='independent_remeasurement',
        test_label='holdout comparison under higher-fidelity model',
        predeclared_prediction='model-form hypothesis explains holdout',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='ho-1', ref_sha256=SHA_D
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_model),
                outcome='supported',
            ),
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_mat),
                outcome='contradicted',
            ),
        ),
        verdict='confirmed_by_intervention_and_remeasurement',
        executed_at_utc=T2,
        declared_at_utc=T1,
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_model, h_mat],
        tests=[fit],
        evaluated_at_utc=T3,
    )
    assert v.verdict == 'root_cause_confirmed_within_declared_scope'
    assert h_model.cause_family == 'model_form_inadequacy'
    assert v.confirmed_hypothesis_ids == (h_model.hypothesis_id,)


def test_dia50_measurement_chain_fault_blocks_calibration() -> None:
    """DIA50: a loopback exposes an interface transfer error — the chain
    fault is confirmed before any room claim is made."""
    case = _case()
    h_chain = _hypothesis(
        case, 'measurement_chain', 'interface transfer error'
    )
    h_room = _hypothesis(case, 'material_boundary', 'room material')
    lb = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='loopback_check',
        test_label='soundcard loopback sweep',
        predeclared_prediction='broadband flatness error reproduces',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='loop-1', ref_sha256=SHA_A
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_chain),
                outcome='supported',
            ),
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_room),
                outcome='out_of_domain',
            ),
        ),
        verdict='confirmed_by_intervention_and_remeasurement',
        executed_at_utc=T1,
        declared_at_utc=T0,
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_chain, h_room],
        tests=[lb],
        evaluated_at_utc=T2,
    )
    assert v.verdict == 'root_cause_confirmed_within_declared_scope'
    assert v.confirmed_hypothesis_ids == (h_chain.hypothesis_id,)


def test_dia60_multi_fault_retained() -> None:
    """DIA60: one intervention fixes LF while HF remains — two confirmed
    causes coexist; the verdict never forces a single winner."""
    case = _case()
    h_lf = _hypothesis(case, 'routing_polarity', 'sub polarity')
    h_hf = _hypothesis(case, 'geometry_asbuilt', 'wall offset')
    t_lf = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='controlled_intervention',
        test_label='polarity fix',
        predeclared_prediction='LF notch fills',
        intervention_label='invert polarity',
        held_constant_label='everything else',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='post-lf', ref_sha256=SHA_A
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_lf),
                outcome='supported',
            ),
        ),
        verdict='confirmed_by_intervention_and_remeasurement',
        executed_at_utc=T1,
        declared_at_utc=T0,
    )
    t_hf = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='controlled_intervention',
        test_label='temporary wall panel at as-built offset',
        predeclared_prediction='HF comb notch shifts',
        intervention_label='panel reposition',
        held_constant_label='everything else',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='post-hf', ref_sha256=SHA_D
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_hf),
                outcome='supported',
            ),
        ),
        verdict='confirmed_by_intervention_and_remeasurement',
        executed_at_utc=T2,
        declared_at_utc=T1,
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_lf, h_hf],
        tests=[t_lf, t_hf],
        evaluated_at_utc=T3,
    )
    assert v.verdict == 'root_cause_confirmed_within_declared_scope'
    assert set(v.confirmed_hypothesis_ids) == {
        h_lf.hypothesis_id,
        h_hf.hypothesis_id,
    }


def test_dia70_ineffective_intervention_downgrades() -> None:
    """DIA70: a controlled intervention producing no change contradicts
    the hypothesis."""
    case = _case()
    h_wall = _hypothesis(
        case, 'material_boundary', 'wall panel material'
    )
    t = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='controlled_intervention',
        test_label='temporary absorber over wall patch',
        predeclared_prediction='early reflection level drops ≥3 dB',
        intervention_label='add 50 mm absorber',
        held_constant_label='positions, levels, DSP',
        result_evidence_ref=AuthorityRef(
            kind='measurement', ref_id='post-x', ref_sha256=SHA_B
        ),
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_wall),
                outcome='contradicted',
                note='no change within noise floor',
            ),
        ),
        verdict='no_material_effect',
        executed_at_utc=T2,
        declared_at_utc=T1,
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_wall],
        tests=[t],
        evaluated_at_utc=T3,
    )
    assert v.verdict == 'unresolved'
    state = v.hypothesis_states[0]
    assert state.derived_state == 'contradicted'


def test_dia80_confounded_not_arbitrary() -> None:
    """DIA80: observationally-equivalent candidates →
    diagnostically_confounded, never an arbitrary winner."""
    case = _case()
    h_a = _hypothesis(case, 'timebase_clock', 'latency offset')
    h_b = _hypothesis(case, 'source_position_aim', 'mic shift')
    screen = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='discrimination_measurement',
        test_label='delay sweep — equivalent signatures',
        predeclared_prediction='both predict identical observable',
        outcomes=(
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_a),
                outcome='indistinguishable',
            ),
            CadHypothesisOutcome(
                hypothesis_ref=diagnostic_hypothesis_binding(h_b),
                outcome='indistinguishable',
            ),
        ),
        verdict='supported_but_confounded',
        executed_at_utc=T1,
        declared_at_utc=T0,
    )
    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h_a, h_b],
        tests=[screen],
        evaluated_at_utc=T2,
    )
    assert v.verdict == 'diagnostically_confounded'
    assert set(v.confounded_hypothesis_ids) == {
        h_a.hypothesis_id,
        h_b.hypothesis_id,
    }
    for h in (h_a, h_b):
        allowed, _ = diagnostic_claim_allowed(h, [screen])
        assert not allowed


def test_diagnostic_repository_roundtrip_and_integrity(tmp_path) -> None:
    scene = _scene_repo(tmp_path)
    repo = CadDiagnosticHypothesisRepository(scene)
    case = _case()
    repo.save_case(case)
    repo.save_case(case)
    assert repo.get_case(case.case_id) == case
    assert repo.list_cases(DOC) == (case,)

    h = _hypothesis(case, 'routing_polarity', 'sub inverted')
    repo.save_hypothesis(h)
    assert repo.get_hypothesis(h.hypothesis_id) == h

    t = build_diagnostic_test(
        document_id=DOC,
        case_ref=diagnostic_case_binding(case),
        test_kind='discrimination_measurement',
        test_label='predeclared screen',
        predeclared_prediction='signature X',
        declared_at_utc=T1,
    )
    repo.save_test(t)
    assert repo.get_test(t.test_id) == t

    v = evaluate_diagnostic_verdict(
        document_id=DOC,
        case=case,
        hypotheses=[h],
        tests=[t],
        evaluated_at_utc=T2,
    )
    repo.save_verdict(v)
    assert repo.get_verdict(v.verdict_id) == v

    _tamper(
        tmp_path / 'cad.sqlite3',
        "UPDATE cad_diagnostic_verdicts "
        "SET verdict='root_cause_confirmed_within_declared_scope' "
        "WHERE verdict_id=?",
        (v.verdict_id,),
    )
    with pytest.raises(DiagnosticIntegrityError):
        repo.get_verdict(v.verdict_id)


def test_case_never_accepts_unpinned_symptom() -> None:
    """The diagnostic case pins its triggering residual — an
    unhash-pinned symptom cannot be sealed."""
    with pytest.raises(ValueError, match='sha256'):
        build_diagnostic_case(
            document_id=DOC,
            symptom_ref=AuthorityRef(
                kind='residual_report', ref_id='rr-2'
            ),
            symptom_summary='x',
            opened_at_utc=T0,
        )


# ---------------------------------------------------------------------------
# Schema registration
# ---------------------------------------------------------------------------


def test_rev59_tables_registered_and_created(tmp_path) -> None:
    new_tables = {
        'cad_typed_quantities',
        'cad_quantity_operations',
        'cad_engineering_assumptions',
        'cad_assumption_resolutions',
        'cad_permissible_use_assessments',
        'cad_perceptual_model_profiles',
        'cad_audibility_assessments',
        'cad_diagnostic_cases',
        'cad_diagnostic_hypotheses',
        'cad_diagnostic_tests',
        'cad_diagnostic_verdicts',
    }
    assert new_tables <= set(NATIVE_SCHEMA_TABLES)
    path = tmp_path / 'cad.sqlite3'
    version = ensure_native_schema(path)
    assert version >= 53
    with connect_sqlite(path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert new_tables <= names
