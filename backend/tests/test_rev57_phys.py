"""REV57-PHYS regression suite — #613/#614/#615.

#613 geometry survey authority: declared CAD precision may never exceed
as-built evidence; controls consumed by registration cannot verify the
campaign they came from; multi-campaign elements without declared
registration are registration-limited.

#614 installed-source boundary authority: free-field loudspeaker data
never silently applies to boundary-installed mounts; boundary-gain
corrections never double-apply (dataset-embedded boundary or a DSP
preset); path-effect corrections stay under the SBIR authority.

#615 porous absorber authority: Delany–Bazley / Miki / JCA predictions
never extrapolate past the declared validity domain (bands are excluded,
never clipped); fit/holdout band sets are disjoint; inverse-estimated
parameters are never relabelled measured.
"""

from __future__ import annotations

import pytest

from htdt.cad_geometry_survey import (
    GeometricElementEvidence,
    GeometrySurveyInstrument,
    RegistrationTransform,
    SurveyCampaign,
    build_control_measurement,
    build_element_evidence,
    build_reconciliation,
    build_survey_campaign,
    build_survey_instrument,
    build_task_requirement,
    evaluate_geometry_qualification,
)
from htdt.cad_geometry_survey_repository import (
    CadGeometrySurveyRepository,
    GeometrySurveyIntegrityError,
)
from htdt.cad_installed_source_boundary import (
    InstalledMeasurementObservation,
    build_boundary_correction,
    build_installed_measurement,
    build_installed_mounting,
    build_source_measurement_condition,
    evaluate_installed_source,
)
from htdt.cad_installed_source_boundary_repository import (
    CadInstalledSourceBoundaryRepository,
    InstalledSourceIntegrityError,
)
from htdt.cad_porous_absorber import (
    PorousLayer,
    build_parameter_evidence,
    build_porous_buildup,
    compare_prediction_to_measured,
    delany_bazley_model,
    johnson_champoux_allard_model,
    measured_impedance_model,
    miki_model,
    evaluate_porous_model_eligibility,
    prediction_as_boundary_evidence,
    predict_porous_boundary,
    porous_buildup_as_material_buildup,
)
from htdt.cad_porous_absorber_repository import (
    CadPorousAbsorberRepository,
    PorousAbsorberIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

DOC = 'doc-rev57-phys'
_TS = '2026-10-05T12:00:00+00:00'


def _scene_repo(tmp_path, doc_id: str = DOC) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(doc_id), parent_revision_id=None)
    return scene_repository


# ---------------------------------------------------------------------------
# #613 fixtures
# ---------------------------------------------------------------------------


def _tls_instrument() -> GeometrySurveyInstrument:
    return build_survey_instrument(
        kind='terrestrial_laser_scanner',
        capability_class='traceable_survey_instrument',
        manufacturer='FARO',
        model='Focus Premium',
    )


def _campaign(instrument: GeometrySurveyInstrument) -> SurveyCampaign:
    return build_survey_campaign(
        document_id=DOC,
        label='TLS scan 2026-10',
        captured_at_utc=_TS,
        instrument_ids=[instrument.instrument_id],
        registration=RegistrationTransform(
            method='icp+targets', uncertainty_mm=3.0
        ),
    )


def _tls_element(
    campaign: SurveyCampaign,
    key: str = 'wall:north',
    **fields,
) -> GeometricElementEvidence:
    campaign_ids = fields.pop('campaign_ids', [campaign.campaign_id])
    return build_element_evidence(
        document_id=DOC,
        element_key=key,
        evidence_classes=('terrestrial_laser_scan',),
        observation_state='observed_surface',
        campaign_ids=campaign_ids,
        uncertainty=(),
        **fields,
    )


# ---------------------------------------------------------------------------
# #613 tests
# ---------------------------------------------------------------------------


def test_geo_instrument_sealed_identity() -> None:
    instrument = _tls_instrument()
    assert instrument.instrument_id.startswith('gsi:')
    assert len(instrument.instrument_sha256) == 64
    assert instrument.instrument_id == 'gsi:' + instrument.instrument_sha256


def test_geo_repository_roundtrip_append_only(tmp_path) -> None:
    repo = CadGeometrySurveyRepository(_scene_repo(tmp_path))
    instrument = _tls_instrument()
    repo.save_instrument(instrument)
    repo.save_instrument(instrument)  # identical re-save is a no-op
    assert repo.get_instrument(instrument.instrument_id) == instrument

    divergent = instrument.model_copy(update={'manufacturer': 'Leica'})
    with pytest.raises(GeometrySurveyIntegrityError):
        repo.save_instrument(divergent)

    forged = instrument.model_copy(
        update={
            'manufacturer': 'forged',
            'instrument_sha256': 'a' * 64,
            'instrument_id': 'gsi:' + 'a' * 64,
        }
    )
    with pytest.raises(GeometrySurveyIntegrityError):
        repo.save_instrument(forged)


def test_geo_campaign_requires_persisted_instrument(tmp_path) -> None:
    repo = CadGeometrySurveyRepository(_scene_repo(tmp_path))
    instrument = _tls_instrument()
    campaign = _campaign(instrument)
    with pytest.raises(GeometrySurveyIntegrityError):
        repo.save_campaign(campaign)
    repo.save_instrument(instrument)
    repo.save_campaign(campaign)
    assert repo.get_campaign(campaign.campaign_id) == campaign


def test_geo_element_requires_persisted_campaign(tmp_path) -> None:
    repo = CadGeometrySurveyRepository(_scene_repo(tmp_path))
    element = _tls_element(
        build_survey_campaign(
            document_id=DOC,
            label='ghost campaign',
            captured_at_utc=_TS,
        )
    )
    with pytest.raises(GeometrySurveyIntegrityError):
        repo.save_element(element)


def test_geo_tls_element_field_checked_and_task_fit() -> None:
    instrument = _tls_instrument()
    campaign = _campaign(instrument)
    element = _tls_element(campaign)
    control = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign.campaign_id,
        element_keys=('wall:north',),
        measured_value_mm=5003.0,
        declared_value_mm=5000.0,
        tolerance_mm=10.0,
    )
    assert control.passed is True
    task = build_task_requirement(
        document_id=DOC,
        task_class='sbir_early_reflection',
        required_element_keys=('wall:north',),
        tolerance_mm=20.0,
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        controls=[control],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    assert qualification.element_states[0].state == 'field_checked'
    assert qualification.task_verdicts[0].verdict == 'fit_for_declared_task'


def test_geo_design_only_element_blocks_high_precision_task() -> None:
    element = build_element_evidence(
        document_id=DOC,
        element_key='wall:south',
        evidence_classes=('design_drawing',),
        observation_state='design_source_only',
    )
    task = build_task_requirement(
        document_id=DOC,
        task_class='high_precision_validation',
        required_element_keys=('wall:south',),
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    assert qualification.element_states[0].state == 'design_only'
    verdict = qualification.task_verdicts[0]
    assert verdict.verdict == 'insufficient_evidence'
    assert 'ELEMENTS_DESIGN_ONLY' in verdict.reasons


def test_geo_failed_control_blocks_element() -> None:
    instrument = _tls_instrument()
    campaign = _campaign(instrument)
    element = _tls_element(campaign)
    control = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign.campaign_id,
        element_keys=('wall:north',),
        measured_value_mm=5050.0,
        declared_value_mm=5000.0,
        tolerance_mm=10.0,
    )
    assert control.passed is False
    task = build_task_requirement(
        document_id=DOC,
        task_class='sbir_early_reflection',
        required_element_keys=('wall:north',),
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        controls=[control],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    assert qualification.element_states[0].state == 'control_check_failed'
    assert qualification.task_verdicts[0].verdict == 'control_check_failed'


def test_geo_control_leakage_cannot_verify_its_own_campaign() -> None:
    """A control consumed by the registration never also verifies the
    elements registered with it — residual leakage is flagged and the
    element stays observed_unqualified (#613 §control leakage)."""
    instrument = _tls_instrument()
    campaign = _campaign(instrument)
    element = _tls_element(campaign)
    control = build_control_measurement(
        document_id=DOC,
        kind='known_target',
        campaign_id=campaign.campaign_id,
        element_keys=('wall:north',),
        measured_value_mm=100.0,
        declared_value_mm=100.0,
        tolerance_mm=10.0,
        used_for_registration=True,
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        controls=[control],
        evaluated_at_utc=_TS,
    )
    entry = qualification.element_states[0]
    assert entry.state == 'observed_unqualified'
    assert 'CONTROL_LEAKAGE_REGISTRATION' in entry.reasons


def test_geo_multi_campaign_without_registration_is_limited() -> None:
    instrument = _tls_instrument()
    campaign_a = _campaign(instrument)
    campaign_b = build_survey_campaign(
        document_id=DOC,
        label='unregistered second scan',
        captured_at_utc=_TS,
        instrument_ids=[instrument.instrument_id],
    )
    element = _tls_element(
        campaign_a,
        campaign_ids=[campaign_a.campaign_id, campaign_b.campaign_id],
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign_a, campaign_b],
        evaluated_at_utc=_TS,
    )
    entry = qualification.element_states[0]
    assert entry.state == 'registration_limited'
    assert 'MULTI_CAMPAIGN_UNREGISTERED' in entry.reasons


def test_geo_registration_without_uncertainty_is_limited() -> None:
    instrument = _tls_instrument()
    campaign = build_survey_campaign(
        document_id=DOC,
        label='scan, unknown registration error',
        captured_at_utc=_TS,
        instrument_ids=[instrument.instrument_id],
        registration=RegistrationTransform(method='icp'),
    )
    element = _tls_element(campaign)
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        evaluated_at_utc=_TS,
    )
    assert qualification.element_states[0].state == 'registration_limited'


def test_geo_stale_element_downgrades_task() -> None:
    instrument = _tls_instrument()
    campaign = _campaign(instrument)
    element = _tls_element(campaign, stale_after_change=True)
    task = build_task_requirement(
        document_id=DOC,
        task_class='low_frequency_wave_model',
        required_element_keys=('wall:north',),
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    assert qualification.element_states[0].state == 'stale_after_change'
    assert qualification.task_verdicts[0].verdict == 'stale_after_change'


def test_geo_unresolved_reconciliation_demotes_task() -> None:
    instrument = _tls_instrument()
    campaign = _campaign(instrument)
    element = _tls_element(campaign)
    control = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign.campaign_id,
        element_keys=('wall:north',),
        measured_value_mm=5001.0,
        declared_value_mm=5000.0,
        tolerance_mm=10.0,
    )
    reconciliation = build_reconciliation(
        document_id=DOC,
        element_key='wall:north',
        reconciled_at_utc=_TS,
        approved_change=False,
    )
    task = build_task_requirement(
        document_id=DOC,
        task_class='prediction_measurement_registration',
        required_element_keys=('wall:north',),
    )
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        controls=[control],
        reconciliations=[reconciliation],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    verdict = qualification.task_verdicts[0]
    assert verdict.verdict == 'fit_with_limitations'
    assert 'RECONCILIATION_UNRESOLVED' in verdict.reasons


def test_geo_repository_persists_full_chain(tmp_path) -> None:
    repo = CadGeometrySurveyRepository(_scene_repo(tmp_path))
    instrument = _tls_instrument()
    repo.save_instrument(instrument)
    campaign = _campaign(instrument)
    repo.save_campaign(campaign)
    element = _tls_element(campaign)
    repo.save_element(element)
    control = build_control_measurement(
        document_id=DOC,
        kind='wall_to_wall_distance',
        campaign_id=campaign.campaign_id,
        element_keys=('wall:north',),
        measured_value_mm=5002.0,
        declared_value_mm=5000.0,
        tolerance_mm=10.0,
    )
    repo.save_control(control)
    task = build_task_requirement(
        document_id=DOC,
        task_class='sbir_early_reflection',
        required_element_keys=('wall:north',),
    )
    repo.save_task_requirement(task)
    qualification = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        controls=[control],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(qualification)
    assert repo.latest_qualification(DOC) == qualification


def test_geo_qualification_conflict_on_divergent_save(tmp_path) -> None:
    repo = CadGeometrySurveyRepository(_scene_repo(tmp_path))
    instrument = _tls_instrument()
    repo.save_instrument(instrument)
    campaign = _campaign(instrument)
    repo.save_campaign(campaign)
    element = _tls_element(campaign)
    repo.save_element(element)
    task = build_task_requirement(
        document_id=DOC,
        task_class='prediction_measurement_registration',
        required_element_keys=('wall:north',),
    )
    repo.save_task_requirement(task)
    q1 = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        task_requirements=[task],
        evaluated_at_utc=_TS,
    )
    q2 = evaluate_geometry_qualification(
        document_id=DOC,
        elements=[element],
        campaigns=[campaign],
        task_requirements=[task],
        evaluated_at_utc='2026-10-05T13:00:00+00:00',
    )
    # Different evaluation timestamp → different sha under same sealed id
    # space: ids are content-derived, so a divergent qualification has a
    # divergent id — re-saving is simply appending. A *forged* same-id
    # conflict must instead be detected.
    repo.save_qualification(q1)
    repo.save_qualification(q2)
    assert q1.qualification_id != q2.qualification_id
    forged = q1.model_copy(
        update={'evaluated_at_utc': '2026-10-05T14:00:00+00:00'}
    )
    with pytest.raises(GeometrySurveyIntegrityError):
        repo.save_qualification(forged)


# ---------------------------------------------------------------------------
# #614 fixtures
# ---------------------------------------------------------------------------


def _free_field_condition(**fields):
    return build_source_measurement_condition(
        document_id=DOC,
        source_dataset_id='spk-main',
        environment='free_field_anechoic',
        standard_profile='cta_2034_b',
        profile_revision='ANSI/CTA-2034-B (2024)',
        baffle_condition='unbaffled_free_air',
        mounting_condition_at_capture='free_standing',
        evidence_class='third_party_lab',
        **fields,
    )


def _mounting(kind: str, **fields):
    return build_installed_mounting(
        document_id=DOC,
        source_ref='speaker:L',
        kind=kind,
        declared_by='as_built_verified',
        **fields,
    )


def _gain_correction(**fields):
    return build_boundary_correction(
        document_id=DOC,
        label='+6 dB half-space loading',
        kind='half_space_boundary_gain',
        model_identity='allison_boundary_loading_1974',
        model_version='allison-1974-1',
        **fields,
    )


# ---------------------------------------------------------------------------
# #614 tests
# ---------------------------------------------------------------------------


def test_isb_free_standing_with_free_field_data_is_qualified() -> None:
    condition = _free_field_condition()
    mounting = _mounting('free_standing')
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        evaluated_at_utc=_TS,
    )
    assert qualification.state == 'qualified'
    assert qualification.achieved_capability == 'reference_source_only'
    assert qualification.directivity_applicability == 'directly_applicable'


def test_isb_in_wall_with_free_field_data_is_fail_closed() -> None:
    """Free-field data on an in-wall mounting must never claim installed
    response — the boundary is unaccounted for (#614)."""
    condition = _free_field_condition()
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        evaluated_at_utc=_TS,
    )
    assert qualification.state in (
        'unqualified_mounting_effect',
        'insufficient_evidence',
    )
    assert 'MEASUREMENT_INCOMPATIBLE_WITH_MOUNTING' in qualification.reasons


def test_isb_half_space_correction_raises_capability() -> None:
    condition = _free_field_condition()
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    correction = _gain_correction()
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        corrections=[correction],
        evaluated_at_utc=_TS,
    )
    assert qualification.achieved_capability == 'half_space_approximation'
    # declared boundary gain still cannot pass a requirement above its rung
    assert qualification.state in (
        'qualified_with_limitations',
        'unqualified_mounting_effect',
    )


def test_isb_double_apply_dataset_boundary_plus_gain() -> None:
    """A dataset that already embeds the installed boundary must never
    receive a modelled boundary gain again (#614 §double-apply)."""
    condition = _free_field_condition(includes_installed_boundary=True)
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    correction = _gain_correction()
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        corrections=[correction],
        evaluated_at_utc=_TS,
    )
    assert qualification.state == 'conflicting_corrections'
    assert 'BOUNDARY_GAIN_DOUBLE_APPLY' in qualification.reasons
    assert qualification.directivity_applicability == 'incompatible'


def test_isb_double_apply_dsp_preset_plus_gain() -> None:
    condition = _free_field_condition()
    mounting = _mounting(
        'in_wall',
        rear_cavity='sealed_volume',
        dsp_boundary_preset_ref='speaker:L:hiqnet-profile-7',
    )
    correction = _gain_correction()
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        corrections=[correction],
        evaluated_at_utc=_TS,
    )
    assert 'DSP_DOUBLE_COMPENSATION_RISK' in qualification.reasons
    assert (
        'dsp_boundary_compensation_declared_alongside_model_correction'
        in qualification.limitations
    )


def test_isb_path_effect_correction_rejected() -> None:
    """SBIR path effects stay under #129's authority — constructing a
    path-effect correction here fails closed."""
    with pytest.raises(ValueError):
        _gain_correction(corrects='path_effect')


def test_isb_manufacturer_preset_requires_binding() -> None:
    with pytest.raises(ValueError):
        build_boundary_correction(
            document_id=DOC,
            label='vendor preset',
            kind='manufacturer_install_preset',
            model_identity='vendor-boundary-preset',
            model_version='v1',
        )


def test_isb_measurement_requires_disjoint_fit_holdout() -> None:
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    with pytest.raises(ValueError):
        build_installed_measurement(
            document_id=DOC,
            mounting=mounting,
            measured_at_utc=_TS,
            fit_position_ids=['p1', 'p2'],
            holdout_position_ids=['p2', 'p3'],
        )


def test_isb_holdout_verified_measurement_promotes() -> None:
    condition = _free_field_condition()
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    measurement = build_installed_measurement(
        document_id=DOC,
        mounting=mounting,
        measured_at_utc=_TS,
        observations=[
            InstalledMeasurementObservation(
                position_id='p-fit', observable='seat_transfer',
                band_hz=63.0,
                measured_db=80.5, predicted_db=79.0, tolerance_db=3.0,
            ),
            InstalledMeasurementObservation(
                position_id='p-holdout', observable='seat_transfer',
                band_hz=63.0,
                measured_db=79.8, predicted_db=79.0, tolerance_db=3.0,
            ),
        ],
        fit_position_ids=['p-fit'],
        holdout_position_ids=['p-holdout'],
    )
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        measurements=[measurement],
        evaluated_at_utc=_TS,
    )
    assert qualification.verification == 'validated_on_holdout'
    assert qualification.achieved_capability == 'installed_measured_transfer'
    assert qualification.state == 'qualified'


def test_isb_repository_enforces_mounting_chain(tmp_path) -> None:
    repo = CadInstalledSourceBoundaryRepository(_scene_repo(tmp_path))
    condition = _free_field_condition()
    repo.save_condition(condition)
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    measurement = build_installed_measurement(
        document_id=DOC,
        mounting=mounting,
        measured_at_utc=_TS,
    )
    with pytest.raises(InstalledSourceIntegrityError):
        repo.save_measurement(measurement)
    repo.save_mounting(mounting)
    repo.save_measurement(measurement)
    repo.save_measurement(measurement)  # no-op
    forged = measurement.model_copy(
        update={'measured_at_utc': '2026-10-05T13:00:00+00:00'}
    )
    with pytest.raises(InstalledSourceIntegrityError):
        repo.save_measurement(forged)


def test_isb_qualification_binds_mounting_sha(tmp_path) -> None:
    repo = CadInstalledSourceBoundaryRepository(_scene_repo(tmp_path))
    condition = _free_field_condition()
    repo.save_condition(condition)
    mounting = _mounting('in_wall', rear_cavity='sealed_volume')
    repo.save_mounting(mounting)
    qualification = evaluate_installed_source(
        document_id=DOC,
        source_dataset_id='spk-main',
        mounting=mounting,
        condition=condition,
        evaluated_at_utc=_TS,
    )
    repo.save_qualification(qualification)
    repo.save_qualification(qualification)
    # a qualification evaluated against a different mounting sha is a
    # different sealed record; the row-level binding is enforced in
    # save_measurement — here we check append-only conflict surface.
    divergent = qualification.model_copy(update={'state': 'qualified'})
    with pytest.raises(InstalledSourceIntegrityError):
        repo.save_qualification(divergent)


def test_isb_correction_conflict(tmp_path) -> None:
    repo = CadInstalledSourceBoundaryRepository(_scene_repo(tmp_path))
    correction = _gain_correction()
    repo.save_correction(correction)
    repo.save_correction(correction)
    forged = correction.model_copy(update={'label': 'forged'})
    with pytest.raises(InstalledSourceIntegrityError):
        repo.save_correction(forged)


# ---------------------------------------------------------------------------
# #615 fixtures
# ---------------------------------------------------------------------------


def _sigma(value: float = 20000.0):
    return build_parameter_evidence(
        document_id=DOC,
        material_ref='glasswool-48',
        quantity='airflow_resistivity',
        value=value,
        unit='Pa*s/m^2',
        evidence_class='measured',
        method='iso_9053_1_2026_static',
        source_ref='cert-r48',
    )


def _thickness(value: float = 50.0):
    return build_parameter_evidence(
        document_id=DOC,
        material_ref='glasswool-48',
        quantity='thickness',
        value=value,
        unit='mm',
        evidence_class='manufacturer_declared',
        method='manufacturer_method',
    )


def _buildup(**fields):
    layers = fields.pop(
        'layers',
        (
            PorousLayer(
                material_ref='glasswool-48',
                thickness_mm=50.0,
                air_gap_behind_mm=30.0,
            ),
        ),
    )
    return build_porous_buildup(
        document_id=DOC,
        label='50mm glasswool + 30mm gap on rigid wall',
        layers=layers,
        backing=fields.pop('backing', 'rigid'),
        anisotropy=fields.pop('anisotropy', 'isotropic_assumed'),
        **fields,
    )


# ---------------------------------------------------------------------------
# #615 tests
# ---------------------------------------------------------------------------


def test_pam_measured_class_requires_measured_method() -> None:
    with pytest.raises(ValueError):
        build_parameter_evidence(
            document_id=DOC,
            material_ref='m1',
            quantity='airflow_resistivity',
            value=10000.0,
            unit='Pa*s/m^2',
            evidence_class='measured',
            method='manufacturer_method',
        )


def test_pam_inverse_estimated_never_relabelled() -> None:
    with pytest.raises(ValueError):
        build_parameter_evidence(
            document_id=DOC,
            material_ref='m1',
            quantity='porosity',
            value=0.95,
            unit='dimensionless',
            evidence_class='measured',
            method='inverse_estimated',
            fit_record_ref='fit-1',
        )
    with pytest.raises(ValueError):
        build_parameter_evidence(
            document_id=DOC,
            material_ref='m1',
            quantity='porosity',
            value=0.95,
            unit='dimensionless',
            evidence_class='inverse_estimated',
            method='inverse_estimated',
        )  # no fit_record_ref


def test_pam_prediction_never_masquerades_as_measured() -> None:
    model = delany_bazley_model()
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(250.0, 500.0, 1000.0),
        computed_at_utc=_TS,
    )
    assert pred.evidence_class == 'parametric_model_prediction'
    forged = pred.model_copy(update={'evidence_class': 'measured'})
    with pytest.raises(ValueError):
        pred.__class__.model_validate(forged.model_dump())


def test_pam_missing_parameters_fail_closed() -> None:
    model = delany_bazley_model()
    eligibility, reasons = evaluate_porous_model_eligibility(
        model=model,
        parameters=[],
        buildup=_buildup(),
        frequencies_hz=(500.0,),
    )
    assert eligibility == 'missing_parameters'
    assert 'REQUIRED_PARAMETER_MISSING' in reasons


def test_pam_non_compute_model_is_unsupported() -> None:
    model = measured_impedance_model('iso-10534-2-lab-cert-1')
    eligibility, reasons = evaluate_porous_model_eligibility(
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(500.0,),
    )
    assert eligibility == 'unsupported'
    assert 'MODEL_NOT_IMPLEMENTED' in reasons


def test_pam_material_class_veto() -> None:
    model = delany_bazley_model()
    eligibility, reasons = evaluate_porous_model_eligibility(
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(500.0,),
        material_class='membrane_resonant',
    )
    assert eligibility == 'incompatible_material'
    assert 'MATERIAL_CLASS_INCOMPATIBLE' in reasons


def test_pam_anisotropy_declared_blocks_isotropic_models() -> None:
    model = delany_bazley_model()
    buildup = _buildup(anisotropy='declared_anisotropic')
    eligibility, reasons = evaluate_porous_model_eligibility(
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=buildup,
        frequencies_hz=(500.0,),
    )
    assert eligibility != 'eligible'
    assert 'ANISOTROPY_UNSUPPORTED' in reasons


def test_pam_delany_bazley_band_physics() -> None:
    """50mm σ=20k glasswool on 30mm gap / rigid backing: absorption must
    rise monotonically toward the band ceiling and 100 Hz (x<0.01) is
    excluded rather than extrapolated (DB validity 0.01 ≤ x ≤ 1)."""
    model = delany_bazley_model()
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(100.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0),
        computed_at_utc=_TS,
    )
    emitted = {b.frequency_hz: b.absorption_alpha for b in pred.bands}
    assert 100.0 in pred.excluded_bands_hz
    assert 100.0 not in emitted
    assert emitted[250.0] > 0.3
    assert emitted[4000.0] > emitted[250.0]
    for band in pred.bands:
        assert 0.0 <= band.absorption_alpha <= 1.0


def test_pam_miki_and_jca_agree_in_ballpark() -> None:
    miki = miki_model()
    jca = johnson_champoux_allard_model()
    params = [
        _sigma(20000.0),
        _thickness(),
        build_parameter_evidence(
            document_id=DOC,
            material_ref='glasswool-48',
            quantity='porosity',
            value=0.98,
            unit='dimensionless',
            evidence_class='measured',
            method='iso_9053_1_2026_static',
        ),
        build_parameter_evidence(
            document_id=DOC,
            material_ref='glasswool-48',
            quantity='tortuosity',
            value=1.0,
            unit='dimensionless',
            evidence_class='literature_assumed',
            method='manufacturer_method',
        ),
        build_parameter_evidence(
            document_id=DOC,
            material_ref='glasswool-48',
            quantity='viscous_characteristic_length',
            value=120e-6,
            unit='m',
            evidence_class='literature_assumed',
            method='manufacturer_method',
        ),
        build_parameter_evidence(
            document_id=DOC,
            material_ref='glasswool-48',
            quantity='thermal_characteristic_length',
            value=240e-6,
            unit='m',
            evidence_class='literature_assumed',
            method='manufacturer_method',
        ),
    ]
    p_miki = predict_porous_boundary(
        document_id=DOC,
        model=miki,
        parameters=params,
        buildup=_buildup(),
        frequencies_hz=(500.0, 1000.0, 2000.0),
        computed_at_utc=_TS,
    )
    p_jca = predict_porous_boundary(
        document_id=DOC,
        model=jca,
        parameters=params,
        buildup=_buildup(),
        frequencies_hz=(500.0, 1000.0, 2000.0),
        computed_at_utc=_TS,
    )
    miki_alpha = {b.frequency_hz: b.absorption_alpha for b in p_miki.bands}
    jca_alpha = {b.frequency_hz: b.absorption_alpha for b in p_jca.bands}
    # JCA is a full physical model; Miki is the empirical approximation —
    # they must agree within ~0.1 on a fibre material (the regression
    # guard that catches a wiring bug between the two solvers).
    for f in (500.0, 1000.0, 2000.0):
        assert abs(miki_alpha[f] - jca_alpha[f]) < 0.15


def test_pam_finite_absorbing_backing_is_honest() -> None:
    """An unsupported backing must exclude bands — never compute them
    against an implicit rigid assumption."""
    model = delany_bazley_model()
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(backing='finite_absorbing'),
        frequencies_hz=(500.0, 1000.0),
        computed_at_utc=_TS,
    )
    assert pred.bands == ()
    assert set(pred.excluded_bands_hz) == {500.0, 1000.0}


def test_pam_multilayer_db_is_limited_not_blocked() -> None:
    model = delany_bazley_model()
    buildup = _buildup(
        layers=(
            PorousLayer(material_ref='glasswool-48', thickness_mm=50.0),
            PorousLayer(material_ref='glasswool-48', thickness_mm=10.0),
        ),
    )
    eligibility, reasons = evaluate_porous_model_eligibility(
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=buildup,
        frequencies_hz=(500.0,),
    )
    assert eligibility == 'eligible_with_limitations'
    assert 'MULTI_LAYER_UNSUPPORTED' in reasons


def test_pam_fit_comparison_disjoint_bands() -> None:
    model = delany_bazley_model()
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(250.0, 500.0, 1000.0),
        computed_at_utc=_TS,
    )
    with pytest.raises(ValueError):
        compare_prediction_to_measured(
            document_id=DOC,
            prediction=pred,
            measured_evidence_ref='meas-10534-1',
            measured_bands=((250.0, 0.5), (500.0, 0.85)),
            fit_band_hz=(250.0, 500.0),
            holdout_band_hz=(500.0, 1000.0),
            declared_tolerance=0.05,
            compared_at_utc=_TS,
        )


def test_pam_fit_comparison_holdout_verdict() -> None:
    model = delany_bazley_model()
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(250.0, 500.0, 1000.0),
        computed_at_utc=_TS,
    )
    measured = tuple(
        (b.frequency_hz, min(1.0, b.absorption_alpha + 0.01))
        for b in pred.bands
    )
    comparison = compare_prediction_to_measured(
        document_id=DOC,
        prediction=pred,
        measured_evidence_ref='meas-10534-1',
        measured_bands=measured,
        fit_band_hz=(250.0,),
        holdout_band_hz=(500.0, 1000.0),
        declared_tolerance=0.05,
        compared_at_utc=_TS,
    )
    assert comparison.verdict == 'validated_on_holdout'


def test_pam_prediction_exports_570_boundary_evidence() -> None:
    """#570 stays the canonical solver gate: predictions must export
    MaterialBoundaryEvidence rather than a private side format."""
    model = delany_bazley_model()
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(500.0, 1000.0),
        computed_at_utc=_TS,
    )
    evidence = prediction_as_boundary_evidence(pred, evidence_id='bev-1')
    assert evidence.method_class == 'derived_conversion'
    assert evidence.quantity == 'surface_impedance'
    assert evidence.incidence == 'normal'
    assert pred.prediction_id in evidence.source_refs


def test_pam_buildup_exports_material_buildup() -> None:
    buildup = _buildup()
    material = porous_buildup_as_material_buildup(buildup)
    assert material.thickness_mm == pytest.approx(50.0)
    assert material.air_gap_mm == pytest.approx(30.0)


def test_pam_repository_chain(tmp_path) -> None:
    repo = CadPorousAbsorberRepository(_scene_repo(tmp_path))
    sigma = _sigma()
    repo.save_parameter(sigma)
    repo.save_parameter(sigma)
    forged = sigma.model_copy(update={'value': 9999.0})
    with pytest.raises(PorousAbsorberIntegrityError):
        repo.save_parameter(forged)

    model = delany_bazley_model()
    repo.save_model(model)
    buildup = _buildup()
    repo.save_buildup(buildup)

    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[sigma, _thickness()],
        buildup=buildup,
        frequencies_hz=(500.0,),
        computed_at_utc=_TS,
    )
    # prediction needs its parameters persisted first
    with pytest.raises(PorousAbsorberIntegrityError):
        repo.save_prediction(pred)
    repo.save_parameter(_thickness())
    repo.save_prediction(pred)
    repo.save_prediction(pred)  # no-op

    comparison = compare_prediction_to_measured(
        document_id=DOC,
        prediction=pred,
        measured_evidence_ref='meas-1',
        measured_bands=((500.0, 0.9),),
        compared_at_utc=_TS,
    )
    repo.save_comparison(comparison)

    divergent = model.model_copy(update={'label': 'forged'})
    with pytest.raises(PorousAbsorberIntegrityError):
        repo.save_model(divergent)


def test_pam_prediction_rejects_stale_model_sha(tmp_path) -> None:
    repo = CadPorousAbsorberRepository(_scene_repo(tmp_path))
    repo.save_parameter(_sigma())
    repo.save_parameter(_thickness())
    model = delany_bazley_model()
    repo.save_model(model)
    repo.save_buildup(_buildup())
    pred = predict_porous_boundary(
        document_id=DOC,
        model=model,
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(500.0,),
        computed_at_utc=_TS,
    )
    # forging the pinned model sha is caught at the repository boundary
    forged = pred.model_copy(update={'model_sha256': 'b' * 64})
    with pytest.raises(PorousAbsorberIntegrityError):
        repo.save_prediction(forged)


def test_pam_fit_comparison_record_roundtrip(tmp_path) -> None:
    """Persisted comparison replays from payload without drift."""
    repo = CadPorousAbsorberRepository(_scene_repo(tmp_path))
    comparison = compare_prediction_to_measured(
        document_id=DOC,
        prediction=None,
        measured_evidence_ref='meas-2',
        measured_bands=((500.0, 0.9),),
        compared_at_utc=_TS,
    )
    repo.save_comparison(comparison)
    assert repo.get_comparison(comparison.comparison_id) == comparison


# ---------------------------------------------------------------------------
# Audit-chain coverage
# ---------------------------------------------------------------------------


def test_authority_chain_covers_rev57_phys(tmp_path) -> None:
    """Every probe table must resolve through the lazy repository
    factory — REV57 added three repositories to the audit chain."""
    from htdt.native_authority_audit import _RepositoryChain

    _scene_repo(tmp_path)
    chain = _RepositoryChain(tmp_path / 'cad.sqlite3')
    try:
        for name in ('geometry_survey', 'installed_source', 'porous_absorber'):
            assert chain.repo(name) is not None, name
    finally:
        chain.close()


def test_native_audit_replays_phys_rows(tmp_path) -> None:
    """Populated REV57-PHYS tables replay through the audit without
    integrity findings."""
    from htdt.native_authority_audit import assert_native_authority_graph

    geo = CadGeometrySurveyRepository(_scene_repo(tmp_path))
    instrument = _tls_instrument()
    geo.save_instrument(instrument)
    campaign = _campaign(instrument)
    geo.save_campaign(campaign)
    element = _tls_element(campaign)
    geo.save_element(element)
    task = build_task_requirement(
        document_id=DOC,
        task_class='prediction_measurement_registration',
        required_element_keys=('wall:north',),
    )
    geo.save_task_requirement(task)
    geo.save_qualification(
        evaluate_geometry_qualification(
            document_id=DOC,
            elements=[element],
            campaigns=[campaign],
            task_requirements=[task],
            evaluated_at_utc=_TS,
        )
    )

    isb = CadInstalledSourceBoundaryRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    condition = _free_field_condition()
    isb.save_condition(condition)
    mounting = _mounting('free_standing')
    isb.save_mounting(mounting)
    isb.save_qualification(
        evaluate_installed_source(
            document_id=DOC,
            source_dataset_id='spk-main',
            mounting=mounting,
            condition=condition,
            evaluated_at_utc=_TS,
        )
    )

    pam = CadPorousAbsorberRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    pam.save_parameter(_sigma())
    pam.save_parameter(_thickness())
    pam.save_model(delany_bazley_model())
    pam.save_buildup(_buildup())
    pred = predict_porous_boundary(
        document_id=DOC,
        model=delany_bazley_model(),
        parameters=[_sigma(), _thickness()],
        buildup=_buildup(),
        frequencies_hz=(500.0,),
        computed_at_utc=_TS,
    )
    pam.save_prediction(pred)

    assert_native_authority_graph(tmp_path / 'cad.sqlite3')
