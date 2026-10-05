"""REV56-TARGETS (#579/#588): RP22 21-parameter standards profile +
response-target / spectral-balance authority — sealed identities, honest
UNKNOWN/insufficient-evidence, fail-closed evidence classes, independent
target-deviation vs seat-spread metrics, append-only persistence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from htdt.cad_calibration import CadTargetCurve, CadTargetCurvePoint
from htdt.cad_repository import SceneRepository
from htdt.cad_response_target import (
    ResponseTargetDerivation,
    ResponseTargetExternalBinding,
    ResponseTargetProviderRef,
    SeatResponseObservation,
    TargetComparisonSemantics,
    build_response_target_profile,
    evaluate_response_target,
    target_identity_differences,
)
from htdt.cad_response_target_repository import (
    CadResponseTargetRepository,
    ResponseTargetConflictError,
)
from htdt.cad_rp22_profile import (
    RP22ParameterObservation,
    RP22ParameterSpec,
    derive_rp22_design_observations,
    evaluate_rp22_parameter,
    evaluate_rp22_profile,
    rp22_report_rows,
    rp22_v1_2_parameter_specs,
    rp22_v1_2_profile,
    rp22_verification_plan,
)
from htdt.cad_rp22_profile_repository import (
    CadRP22ProfileRepository,
    RP22ProfileConflictError,
)
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_empty_scene,
    quaternion_from_euler_deg,
)
from htdt.standards_workspace import StandardsWorkspaceModel
import math
from htdt.measurement_evidence_display import (
    response_target_kind_label,
    rp22_conformance_label,
    rp22_evaluation_line,
    rp22_mapping_status_label,
    seat_coverage_label,
    spectral_balance_line,
    target_binding_state_label,
)


DOC = 'doc-targets'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        make_empty_scene(doc_id), parent_revision_id=None
    )
    return scene_repository


def _spec(parameter_id: str):
    profile = rp22_v1_2_profile(created_at_utc='2026-10-05T00:00:00+00:00')
    spec = profile.parameter(parameter_id)
    assert spec is not None
    return spec


def _obs(
    parameter_id: str,
    value,
    *,
    evidence_class: str = 'measured_commissioning',
    seat_scope=(),
    basis=None,
    context_tag=None,
) -> RP22ParameterObservation:
    return RP22ParameterObservation(
        parameter_id=parameter_id,
        value=value,
        evidence_class=evidence_class,
        seat_scope=tuple(seat_scope),
        basis=basis,
        context_tag=context_tag,
    )


# ---------------------------------------------------------------------------
# #579 RP22 standards profile (RP22-10 … RP22-70)
# ---------------------------------------------------------------------------


def test_rp22_10_profile_sealed_exact_21_parameters():
    profile = rp22_v1_2_profile(created_at_utc='2026-10-05T00:00:00+00:00')
    assert len(profile.parameters) == 21
    assert [p.parameter_index for p in profile.parameters] == list(
        range(1, 22)
    )
    assert profile.registry_key == 'cedia-cta-rp22@v1.2'
    assert profile.standard_id == 'cedia-cta-rp22'
    assert profile.edition == 'v1.2'
    # sealed: identical declaration → identical hash
    again = rp22_v1_2_profile(created_at_utc='2026-10-05T00:00:00+00:00')
    assert again.profile_sha256 == profile.profile_sha256


def test_rp22_11_exact_level_limit_values():
    """Spot-check the Appendix A transcription (RP22 v1.2)."""
    specs = {s.parameter_id: s for s in rp22_v1_2_parameter_specs()}
    assert specs['rp22.p01'].limits == (0.5, 0.8, 1.2, 1.5)
    assert specs['rp22.p01'].strict_boundary is True
    assert specs['rp22.p02'].limits == (5.0, 11.0, 15.0, 15.0)
    alt = specs['rp22.p02'].alternative_limits[0]
    assert alt.condition_tag == 'auro3d_room_design'
    assert alt.minimum == 13.0
    assert alt.levels == (3, 4)
    assert specs['rp22.p05'].limits == (None, 80.0, 60.0, 50.0)
    assert specs['rp22.p08'].limits == (True, True, False, False)
    assert specs['rp22.p12'].limits == (99.0, 102.0, 105.0, 108.0)
    assert specs['rp22.p12'].recommended == (102.0, 105.0, 108.0, 111.0)
    assert specs['rp22.p14'].limits == (109.0, 112.0, 115.0, 118.0)
    assert specs['rp22.p15'].limits == (35.0, 26.0, 22.0, 18.0)
    assert specs['rp22.p15'].recommended == (26.0, 22.0, 18.0, 15.0)
    assert specs['rp22.p16'].limits == (5.0, 3.0, 1.5, 1.5)
    assert specs['rp22.p17'].limits == (None, None, 3.0, 1.5)
    assert specs['rp22.p18'].limits == (35.0, 30.0, 20.0, 18.0)
    assert specs['rp22.p20'].limits == (None, 4.0, 3.0, 2.0)
    assert specs['rp22.p21'].limits == (None, -8.0, -10.0, -12.0)
    assert specs['rp22.p21'].limit_kind == 'maximum'


def test_rp22_12_na_cells_are_not_applicable_not_pass():
    spec = _spec('rp22.p05')
    result = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p05', 10.0),
        requested_level=1,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert result.verdict == 'not_applicable'
    assert 'parameter_not_required_at_requested_level' in result.limitations


def test_rp22_15_no_observation_is_insufficient_evidence():
    spec = _spec('rp22.p03')
    result = evaluate_rp22_parameter(
        spec,
        None,
        requested_level=3,
        evaluation_kind='design_evaluation',
    )
    assert result.verdict == 'insufficient_evidence'
    assert 'no_observation' in result.limitations


def test_rp22_16_design_prediction_never_credits_commissioning():
    spec = _spec('rp22.p01')
    result = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p01', 2.0, evidence_class='design_prediction',
             seat_scope=('seat-a',)),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert result.verdict == 'insufficient_evidence'
    assert any('below_measured_commissioning' in n for n in result.limitations)
    # Same value credits a design evaluation.
    design = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p01', 2.0, evidence_class='design_prediction',
             seat_scope=('seat-a',)),
        requested_level=4,
        evaluation_kind='design_evaluation',
    )
    assert design.verdict == 'met'
    assert 'design_prediction_not_commissioning_proof' in design.limitations


def test_rp22_17_strict_boundary_minimum_distance():
    spec = _spec('rp22.p01')
    at_limit = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p01', 1.5, seat_scope=('s1',)),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert at_limit.verdict == 'not_met'  # boundary is strict '>'
    over = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p01', 1.51, seat_scope=('s1',)),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert over.verdict == 'met'


def test_rp22_18_boolean_allowed_upfiring_prohibition():
    spec = _spec('rp22.p08')
    used = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p08', True),
        requested_level=4,
        evaluation_kind='design_evaluation',
    )
    assert used.verdict == 'not_met'
    unused = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p08', False),
        requested_level=4,
        evaluation_kind='design_evaluation',
    )
    assert unused.verdict == 'met'
    # At Level 1 upfiring is allowed — always met, never 'required'.
    level_one = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p08', False),
        requested_level=1,
        evaluation_kind='design_evaluation',
    )
    assert level_one.verdict == 'met'


def test_rp22_20_auro3d_alternative_limit_requires_context_tag():
    spec = _spec('rp22.p02')
    without_tag = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p02', 13.0),
        requested_level=3,
        evaluation_kind='design_evaluation',
    )
    assert without_tag.verdict == 'not_met'
    auro = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p02', 13.0, context_tag='auro3d_room_design'),
        requested_level=3,
        evaluation_kind='design_evaluation',
    )
    assert auro.verdict == 'met'
    assert auro.achieved_by_alternative is True
    # The alternative does not relax levels it does not cover.
    auro_l2 = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p02', 13.0, context_tag='auro3d_room_design'),
        requested_level=4,
        evaluation_kind='design_evaluation',
    )
    assert auro_l2.verdict == 'met'  # 13 >= alt 13 at level 4 too
    auro_l1 = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p02', 13.0, context_tag='auro3d_room_design'),
        requested_level=1,
        evaluation_kind='design_evaluation',
    )
    assert auro_l1.verdict == 'met'  # 13 >= 5 regardless


def test_rp22_25_dynamics_requires_capability_basis():
    spec = _spec('rp22.p12')
    missing = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p12', 110.0,
             evidence_class='measured_commissioning'),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert missing.verdict == 'insufficient_evidence'
    assert 'capability_basis_missing' in missing.limitations
    weak = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p12', 110.0,
             evidence_class='measured_commissioning',
             basis='nominal_spec_only'),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert weak.verdict == 'insufficient_evidence'
    assert any('below_measured_commissioning' in n for n in weak.limitations)
    strong = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p12', 108.0,
             evidence_class='measured_commissioning',
             basis='in_room_measured_capability'),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert strong.verdict == 'met'


def test_rp22_26_basis_on_non_dynamics_parameter_is_rejected():
    spec = _spec('rp22.p01')
    result = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p01', 2.0, basis='commissioned_verified',
             seat_scope=('s1',)),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert result.verdict == 'insufficient_evidence'
    assert 'capability_basis_on_non_dynamics_parameter' in result.limitations


def test_rp22_30_achieved_level_separate_from_requested():
    """A Level-4 request failing the 2 dB surround limit still reports the
    Level-3 achievement it earned — requested ≠ achieved."""
    spec = _spec('rp22.p06')
    result = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p06', 3.0, seat_scope=('s1', 's2')),
        requested_level=4,
        evaluation_kind='design_evaluation',
    )
    assert result.verdict == 'not_met'
    assert result.achieved_level == 3
    assert result.target_at_requested_level == 2.0


def test_rp22_35_measurement_required_flags_even_when_met():
    spec = _spec('rp22.p15')
    result = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p15', 17.0),
        requested_level=4,
        evaluation_kind='measured_commissioning_evaluation',
    )
    assert result.verdict == 'met'
    assert 'measurement_evidence_required_for_claim' in result.limitations


def test_rp22_40_seat_metric_without_seat_scope_is_limitation():
    spec = _spec('rp22.p04')
    result = evaluate_rp22_parameter(
        spec,
        _obs('rp22.p04', 1.0, evidence_class='design_prediction'),
        requested_level=4,
        evaluation_kind='design_evaluation',
    )
    assert result.verdict == 'met'
    assert 'seat_coverage_undeclared' in result.limitations


def test_rp22_45_profile_evaluation_conformance_states():
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    # Only one failing parameter → not_met, and the failure is named.
    observations = (
        _obs('rp22.p01', 0.4, seat_scope=('s1',),
             evidence_class='design_prediction'),
    )
    evaluation = evaluate_rp22_profile(
        profile,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=observations,
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    assert evaluation.strict_conformance == 'not_met'
    assert evaluation.limiting_parameter_ids == ('rp22.p01',)
    # Every other parameter carries insufficient evidence — they are
    # reported, never silently folded into the verdict.
    assert len(evaluation.insufficient_parameter_ids) > 0

    # Only unknowns → indeterminate.
    unknown_only = evaluate_rp22_profile(
        profile,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=(),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    assert unknown_only.strict_conformance == 'indeterminate'


def test_rp22_46_duplicate_and_undeclared_observations_rejected():
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    with pytest.raises(ValueError, match='duplicate observation'):
        evaluate_rp22_profile(
            profile,
            document_id=DOC,
            requested_level=1,
            evaluation_kind='design_evaluation',
            observations=(
                _obs('rp22.p01', 1.0),
                _obs('rp22.p01', 1.1),
            ),
        )
    with pytest.raises(ValueError, match='undeclared parameter'):
        evaluate_rp22_profile(
            profile,
            document_id=DOC,
            requested_level=1,
            evaluation_kind='design_evaluation',
            observations=(_obs('rp22.p99', 1.0),),
        )


def test_rp22_50_verification_plan_and_report_rows():
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    evaluation = evaluate_rp22_profile(
        profile,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=(_obs('rp22.p01', 1.0,
                           evidence_class='design_prediction',
                           seat_scope=('s1',)),),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    plan = rp22_verification_plan(profile, evaluation)
    plan_ids = {item.parameter_id for item in plan}
    assert 'rp22.p01' not in plan_ids  # met at level 1
    assert 'rp22.p15' in plan_ids  # measurement_required stays on the plan
    rows = rp22_report_rows(profile, evaluation)
    assert len(rows) == 21
    p1 = next(r for r in rows if r.parameter_id == 'rp22.p01')
    assert p1.verdict == 'met'
    assert p1.requested_level_target == 0.5


def test_rp22_55_verification_plan_rejects_foreign_evaluation():
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    other = rp22_v1_2_profile(
        created_at_utc='2026-10-06T00:00:00+00:00'
    )
    evaluation = evaluate_rp22_profile(
        other,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=(),
    )
    with pytest.raises(ValueError, match='evaluation of this profile'):
        rp22_verification_plan(profile, evaluation)


def test_rp22_60_repository_roundtrip_and_append_only(tmp_path):
    repository = CadRP22ProfileRepository(_scene_repo(tmp_path))
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    repository.save_profile(profile)
    loaded = repository.get_profile(
        profile.profile_id, profile.edition
    )
    assert loaded == profile
    assert repository.get_profile_by_hash(profile.profile_sha256) == profile
    with pytest.raises(RP22ProfileConflictError):
        repository.save_profile(profile)


def _bridge_seat(entity_id, *, x, y) -> SceneEntity:
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


def _bridge_speaker_at_azimuth(
    entity_id, role, *, ear_x, ear_y, azimuth_deg, distance=1.5
) -> SceneEntity:
    azimuth = math.radians(azimuth_deg)
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        speaker_role=role,
        position=Position3(
            x_m=ear_x + distance * math.sin(azimuth),
            y_m=ear_y - distance * math.cos(azimuth),
            z_m=1.0,
        ),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


def test_rp22_62_layout_bridge_derives_spatial_observations(tmp_path):
    """The criterion-layout lane feeds the 21-parameter profile exactly:
    derivable spatial parameters become design-prediction observations;
    nothing else is fabricated."""
    document = SceneDocument(
        document_id=DOC,
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=2.4),
        entities=(
            _bridge_seat('seat-a', x=3.0, y=3.6),
            _bridge_speaker_at_azimuth(
                'sl', 'SL', ear_x=3.0, ear_y=3.6, azimuth_deg=-100.0
            ),
            _bridge_speaker_at_azimuth(
                'sbl', 'SBL', ear_x=3.0, ear_y=3.6, azimuth_deg=-150.0
            ),
            _bridge_speaker_at_azimuth(
                'sr', 'SR', ear_x=3.0, ear_y=3.6, azimuth_deg=100.0
            ),
            _bridge_speaker_at_azimuth(
                'sbr', 'SBR', ear_x=3.0, ear_y=3.6, azimuth_deg=150.0
            ),
        ),
    )
    scene_repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    scene_repository.save(document, parent_revision_id=None)
    workspace = StandardsWorkspaceModel(scene_repository, DOC)
    target = workspace.target_view(None).target

    observations = derive_rp22_design_observations(
        repository=workspace.repository,
        target=target,
        document=document,
        observed_at_utc='2026-10-05T01:00:00+00:00',
    )
    by_id = {obs.parameter_id: obs for obs in observations}
    # The lane derives boundary distance and the adjacent-surround angle.
    assert by_id['rp22.p01'].value == pytest.approx(1.4)
    assert by_id['rp22.p01'].evidence_class == 'design_prediction'
    assert by_id['rp22.p01'].seat_scope == ('seat-a',)
    assert 'rp22.p05' in by_id
    # Nothing underivable is fabricated.
    assert 'rp22.p12' not in by_id and 'rp22.p15' not in by_id

    # End-to-end: the derived observations feed a real Level-1 design
    # evaluation.
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    evaluation = evaluate_rp22_profile(
        profile,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=observations,
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    p1 = evaluation.result('rp22.p01')
    assert p1 is not None and p1.verdict == 'met'  # 1.4 > 0.5


def test_rp22_61_evaluation_persistence_requires_profile(tmp_path):
    repository = CadRP22ProfileRepository(_scene_repo(tmp_path))
    profile = rp22_v1_2_profile(
        created_at_utc='2026-10-05T00:00:00+00:00'
    )
    evaluation = evaluate_rp22_profile(
        profile,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=(),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    with pytest.raises(ValueError, match='persisted profile'):
        repository.save_evaluation(evaluation)
    repository.save_profile(profile)
    repository.save_evaluation(evaluation)
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations(DOC) == (evaluation,)
    with pytest.raises(RP22ProfileConflictError):
        repository.save_evaluation(evaluation)


def test_rp22_70_spec_requires_exactly_21_parameters():
    specs = rp22_v1_2_parameter_specs()
    with pytest.raises(ValidationError):
        from htdt.cad_rp22_profile import build_rp22_profile

        build_rp22_profile(
            profile_id='x',
            standard_id='cedia-cta-rp22',
            edition='v1.2',
            publisher='CEDIA / CTA',
            document_title='RP22',
            document_version='v1.2',
            source_uri='https://example.test/rp22.pdf',
            parameters=specs[:20],
        )


# ---------------------------------------------------------------------------
# #588 response-target / spectral-balance authority (SBT10 … SBT70)
# ---------------------------------------------------------------------------


def _curve(
    points=((20.0, 75.0), (100.0, 75.0), (1000.0, 70.0), (10000.0, 65.0)),
) -> CadTargetCurve:
    return CadTargetCurve(
        points=tuple(
            CadTargetCurvePoint(frequency_hz=f, level_db=l)
            for f, l in points
        ),
        normalization={
            'method': 'reference_frequency',
            'reference_frequency_hz': 1000.0,
        },
    )


def _target(**overrides):
    kwargs = dict(
        document_id=DOC,
        version='v1',
        name='Test target',
        kind='project_defined',
        curve=_curve(),
        created_at_utc='2026-10-05T00:00:00+00:00',
    )
    kwargs.update(overrides)
    return build_response_target_profile(**kwargs)


def _seat(seat_id, *, offset_db=0.0, role='evaluation',
          points=((20.0, 70.0), (100.0, 70.0), (1000.0, 70.0),
                  (10000.0, 70.0)), channel_group='system_sum'):
    return SeatResponseObservation(
        seat_id=seat_id,
        role=role,
        channel_group=channel_group,
        points=tuple((f, l + offset_db) for f, l in points),
    )


def test_sbt10_target_profile_sealed_and_deterministic():
    profile = _target()
    assert profile.profile_id == 'rtgt-' + profile.target_sha256[:24]
    assert profile.kind == 'project_defined'
    assert _target() == profile  # same inputs → same identity


def test_sbt11_kind_provenance_required():
    with pytest.raises(ValidationError, match='derivation'):
        _target(kind='measured_reference_derived')
    with pytest.raises(ValidationError, match='external binding'):
        _target(kind='external_standard_profile')
    with pytest.raises(ValidationError, match='provider'):
        _target(kind='provider_device_profile')
    with pytest.raises(ValidationError, match='unknown'):
        _target(kind='unknown')  # needs a rationale


def test_sbt12_external_binding_ambiguous_state():
    profile = _target(
        kind='external_standard_profile',
        external_binding=ResponseTargetExternalBinding(
            standard_id='avixa-a103.01',
            binding_state='source_version_ambiguous',
            source_note=(
                'Catalogue lists A103.01:2023; store page labels '
                'ANSI/AVIXA A103.01:2022 — exact edition unresolved.'
            ),
        ),
    )
    assert profile.external_binding.binding_state == (
        'source_version_ambiguous'
    )


def test_sbt13_measured_derived_keeps_provenance():
    profile = _target(
        kind='measured_reference_derived',
        derivation=ResponseTargetDerivation(
            source_measurement_ids=('meas-a', 'meas-b'),
            smoothing_or_fit='1/3-oct smoothing + logistic fit',
            derivation_algorithm='htdt-target-fit',
            derivation_version='1.0.0',
        ),
    )
    assert profile.derivation.source_measurement_ids == (
        'meas-a', 'meas-b'
    )


def test_sbt15_semantics_declared_or_rejected():
    with pytest.raises(ValidationError, match='explicit_grid_hz'):
        _target(
            semantics=TargetComparisonSemantics(grid_rule='explicit_grid')
        )
    with pytest.raises(ValidationError, match='aggregation_percentile'):
        _target(
            semantics=TargetComparisonSemantics(aggregation='percentile')
        )
    with pytest.raises(ValidationError, match='seat_weight_map'):
        _target(
            semantics=TargetComparisonSemantics(
                aggregation='weighted_mean'
            )
        )
    with pytest.raises(ValidationError, match='description'):
        _target(
            semantics=TargetComparisonSemantics(smoothing='custom')
        )


def test_sbt20_target_deviation_and_spread_are_independent():
    """Two seats shifted +5 dB together: deviation is 5 dB RMS, seat-to-
    seat spread is exactly 0 — uniform but off-target stays visible."""
    profile = _target()
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(
            _seat('s1', offset_db=5.0),
            _seat('s2', offset_db=5.0),
        ),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    # Normalization at 1 kHz removes the constant shift: seats normalize
    # to the target at the anchor — so shape error, not level offset,
    # drives deviation. With a flat -5 response vs a sloped target the
    # deviation is the shape error.
    assert evaluation.seats_evaluated == 2
    assert evaluation.seat_to_seat_spread_max_db == 0.0
    assert evaluation.mean_rms_target_deviation_db is not None
    s1 = evaluation.seat('s1')
    assert s1 is not None and s1.coverage_status == 'evaluated'
    assert s1.normalization_offset_db == -5.0  # target 70 - response 75


def test_sbt21_spread_detects_divergent_seats():
    profile = _target()
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(
            _seat('s1'),
            _seat(
                's2',
                points=((20.0, 80.0), (100.0, 70.0), (1000.0, 70.0),
                        (10000.0, 70.0)),
            ),
        ),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    # s2 is +10 dB at 20 Hz only → spread appears there and at the
    # interpolated points; the mean target deviation stays modest.
    assert evaluation.seat_to_seat_spread_max_db == pytest.approx(10.0)
    assert evaluation.seat_to_seat_spread_rms_db is not None
    assert evaluation.seat_to_seat_spread_rms_db < (
        evaluation.seat_to_seat_spread_max_db
    )


def test_sbt25_holdout_seats_reported_separately():
    profile = _target()
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(
            _seat('control-a', role='control'),
            _seat('control-b', role='control'),
            _seat('hold-1', role='holdout',
                  points=((20.0, 85.0), (100.0, 85.0), (1000.0, 70.0),
                          (10000.0, 70.0))),
        ),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    assert evaluation.control_group is not None
    assert evaluation.holdout_group is not None
    assert evaluation.control_group.seat_ids == ('control-a', 'control-b')
    assert evaluation.holdout_group.seat_ids == ('hold-1',)
    assert evaluation.holdout_vs_control_delta_db is not None
    assert evaluation.holdout_vs_control_delta_db > 0.0


def test_sbt30_channel_scope_mismatch_is_rejected():
    profile = _target(channel_scope=('screen',))
    with pytest.raises(ValueError, match='outside target channel scope'):
        evaluate_response_target(
            profile,
            document_id=DOC,
            responses=(_seat('s1', channel_group='lfe'),),
        )


def test_sbt35_validity_range_limits_comparison():
    profile = _target(frequency_validity_hz=(100.0, 2000.0))
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(_seat('s1'),),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    assert all(
        100.0 <= f <= 2000.0 for f in evaluation.comparison_grid_hz
    )


def test_sbt40_no_coverage_and_normalization_failure_are_honest():
    profile = _target()
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(
            _seat('s1'),
            # Response entirely outside the validity window covered by the
            # grid vs target — actually outside interpolation range.
            _seat('s2', points=((30000.0, 70.0), (40000.0, 70.0))),
        ),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    s2 = evaluation.seat('s2')
    assert s2 is not None
    assert s2.coverage_status == 'normalization_failed'
    assert evaluation.seats_without_coverage == 1


def test_sbt45_weighted_mean_aggregation():
    profile = _target(
        semantics=TargetComparisonSemantics(
            aggregation='weighted_mean',
            seat_weight_map={'s1': 1.0, 's2': 3.0},
        )
    )
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(_seat('s1'), _seat('s2')),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    assert evaluation.mean_rms_target_deviation_db is not None


def test_sbt46_weighted_mean_missing_weight_fails_closed():
    profile = _target(
        semantics=TargetComparisonSemantics(
            aggregation='weighted_mean',
            seat_weight_map={'s1': 1.0},
        )
    )
    with pytest.raises(ValueError, match='lacks weights'):
        evaluate_response_target(
            profile,
            document_id=DOC,
            responses=(_seat('s1'), _seat('s2')),
        )


def test_sbt50_identity_differences_report_semantic_fields():
    first = _target()
    same = _target()
    assert target_identity_differences(first, same) == ()
    different = _target(kind='user_preference')
    assert 'kind' in target_identity_differences(first, different)
    other_semantics = _target(
        semantics=TargetComparisonSemantics(smoothing='1/3_oct')
    )
    assert 'semantics' in target_identity_differences(
        first, other_semantics
    )


def test_sbt60_repository_roundtrip_and_append_only(tmp_path):
    repository = CadResponseTargetRepository(_scene_repo(tmp_path))
    profile = _target()
    repository.save_profile(profile)
    assert repository.get_profile(profile.profile_id, 'v1') == profile
    assert (
        repository.get_profile_by_hash(profile.target_sha256) == profile
    )
    with pytest.raises(ResponseTargetConflictError):
        repository.save_profile(profile)


def test_sbt61_evaluation_persistence_pin_enforced(tmp_path):
    repository = CadResponseTargetRepository(_scene_repo(tmp_path))
    profile = _target()
    evaluation = evaluate_response_target(
        profile,
        document_id=DOC,
        responses=(_seat('s1'),),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    with pytest.raises(ValueError, match='persisted target profile'):
        repository.save_evaluation(evaluation)
    repository.save_profile(profile)
    repository.save_evaluation(evaluation)
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations(DOC) == (evaluation,)


def test_sbt70_ja_labels_and_lines():
    profile = _target(
        kind='external_standard_profile',
        external_binding=ResponseTargetExternalBinding(
            standard_id='avixa-a103.01',
            binding_state='source_version_ambiguous',
            source_note='catalogue/store revision conflict',
        ),
    )
    assert '曖昧' in target_binding_state_label('source_version_ambiguous')
    assert '外部規格' in response_target_kind_label(profile.kind)
    assert seat_coverage_label('no_coverage') == 'カバレッジなし'
    rp22 = rp22_v1_2_profile(created_at_utc='2026-10-05T00:00:00+00:00')
    evaluation = evaluate_rp22_profile(
        rp22,
        document_id=DOC,
        requested_level=1,
        evaluation_kind='design_evaluation',
        observations=(),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    assert rp22_conformance_label('indeterminate') == '判定不能'
    assert '設計評価' in rp22_evaluation_line(evaluation)
    assert '未マップ' in rp22_mapping_status_label('unsupported')
    target_eval = evaluate_response_target(
        _target(),
        document_id=DOC,
        responses=(_seat('s1'), _seat('s2')),
        evaluated_at_utc='2026-10-05T01:00:00+00:00',
    )
    line = spectral_balance_line(target_eval)
    assert '目標乖離RMS' in line and '座席間ばらつき' in line


def test_new_tables_join_fail_closed_registries(tmp_path):
    """The four v26 tables must be covered by every fail-closed
    registry: authority-audit replay probes and row-integrity payload
    bindings. Missing coverage quarantines migrations and breaks
    backups (found by e2e testing)."""
    from htdt.native_authority_audit import _REPLAY_TABLES, audit_table_modes
    from htdt.native_row_integrity import (
        _ROW_BINDINGS,
        _UNBOUND_PAYLOAD_TABLES,
        scan_native_row_integrity,
    )
    import sqlite3

    tables = {
        'cad_rp22_profiles',
        'cad_rp22_evaluations',
        'cad_response_targets',
        'cad_spectral_balance_evaluations',
    }
    assert tables <= _REPLAY_TABLES
    assert tables <= set(audit_table_modes())
    for table in tables:
        assert (
            table in _ROW_BINDINGS or table in _UNBOUND_PAYLOAD_TABLES
        ), table

    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    with sqlite3.connect(scene.path) as connection:
        assert scan_native_row_integrity(connection) == ()
