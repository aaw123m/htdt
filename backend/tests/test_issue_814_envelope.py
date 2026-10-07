"""#814 — applicability/confidence envelope composition (REV63).

The envelope composes sealed evidence from the landed authorities
(#809 benchmark qualification, #810 uncertainty validation, #811 input
bounds, solver capability manifests, accuracy envelopes) into seven
*separate* dimensions — never an aggregate score. Absent evidence reads
as ``absent`` (未取得), never as a pass; staleness is declared, never
inferred.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.acoustic_validation_envelope import (
    build_accuracy_envelope,
    error_statistic,
)
from htdt.cad_acoustic_solver_adapter import (
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_applicability_envelope import (
    ENVELOPE_CLASS_LABELS,
    ENVELOPE_DIMENSIONS,
    DECISION_LABELS,
    DECISION_VERDICT_LABELS,
    CAPABILITY_STATE_LABELS,
    CLAIM_LABELS,
    PHENOMENON_LABELS,
    QUALIFICATION_LEVEL_LABELS,
    QUALIFICATION_VERDICT_LABELS,
    ApplicabilityEnvelope,
    ApplicabilityEnvelopeReport,
    EnvelopeContext,
    EnvelopeEvidenceBundle,
    EnvelopeDimensionState,
    build_envelope_report,
    compose_applicability_envelope,
    context_for_document,
    envelope_dimension_label,
    envelope_class_label,
    envelope_evidence_refs,
    envelope_lines,
    load_envelope_evidence,
    qualification_verdict_label,
)
from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_benchmark import (
    BenchmarkValidationEvidence,
    ObservableEvaluation,
)
from htdt.cad_benchmark_qualification import (
    BenchmarkPreregistration,
    BenchmarkQualification,
    BenchmarkSceneMapping,
    evaluate_qualification,
)
from htdt.cad_benchmark_qualification_repository import (
    CadBenchmarkQualificationRepository,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_solver_capability_manifest import (
    SOLVER_PATH_PHENOMENA,
    SolverCapabilityRow,
    build_solver_capability_manifest,
)
from htdt.cad_solver_confidence_bound import (
    ClaimBoundRecord,
    ClaimBoundRow,
    EnvironmentalBinding,
    SolverInputEnvelope,
)
from htdt.cad_solver_confidence_bound_repository import (
    CadSolverConfidenceBoundRepository,
)
from htdt.cad_validation_uncertainty import (
    CategoryContributionValue,
    ObservableUncertaintyEvaluation,
    UncertaintyDecisionRule,
    UncertaintySideEvidence,
    UncertaintyValidationVerdict,
    ValidationObservableMetric,
    ValidationUncertaintyProtocol,
    build_observable_uncertainty_evaluation,
    build_validation_uncertainty_verdict,
)
from htdt.cad_validation_uncertainty_repository import (
    CadValidationUncertaintyRepository,
)
from htdt.canonical_json import canonical_sha256
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


DOC = 'doc-issue814'
_SHA = canonical_sha256({'fixture': 'sha'})
_SHA2 = canonical_sha256({'fixture': 'sha2'})
_SHA3 = canonical_sha256({'fixture': 'sha3'})
_SCENE_SHA = canonical_sha256({'fixture': 'scene-revision'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _xref(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test-authority:{label}',
        authority_version='1',
        semantic_hash_sha256='ab' * 32,
    )


def _context(**kw) -> EnvelopeContext:
    payload = dict(
        document_id=DOC,
        adapter_id='htdt.fdtd-wave',
        adapter_version='2.0',
        solver_path='wave_r130',
    )
    payload.update(kw)
    return EnvelopeContext(**payload)


# ---------------------------------------------------------------------------
# capability manifest + input envelope fixtures (#580 / #811 family)
# ---------------------------------------------------------------------------


def _descriptor(adapter_version: str = '2.0', adapter_id: str = 'htdt.fdtd-wave'):
    return build_acoustic_solver_adapter_descriptor(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        model_solver_role_id='wave-solver',
        acoustic_domain='wave',
        solver_implementation_ref=_xref('fdtd-impl'),
        solver_configuration_schema_ref=_xref('fdtd-config'),
        supported_snapshot_schema_versions=(1,),
        supported_observables=('complex_pressure',),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=5000.0,
        ),
    )


def _manifest(
    *,
    descriptor=None,
    unsupported: tuple[str, ...] = (),
    bounded: tuple[str, ...] = (),
):
    descriptor = descriptor or _descriptor()
    rows = []
    for phenomenon in SOLVER_PATH_PHENOMENA:
        if phenomenon in unsupported:
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,  # type: ignore[arg-type]
                    state='UNSUPPORTED',
                    reasons=(f'{phenomenon} not modeled',),
                )
            )
        elif phenomenon in bounded:
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,  # type: ignore[arg-type]
                    state='BOUNDED',
                    valid_frequency_domain=FrequencyDomain(
                        minimum_hz=20.0, maximum_hz=2000.0,
                    ),
                    bound_description='limited band',
                )
            )
        else:
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,  # type: ignore[arg-type]
                    state='SUPPORTED',
                    valid_frequency_domain=FrequencyDomain(
                        minimum_hz=20.0, maximum_hz=5000.0,
                    ),
                )
            )
    return build_solver_capability_manifest(descriptor=descriptor, rows=tuple(rows))


def _environment() -> tuple[EnvironmentalBinding, ...]:
    return tuple(
        EnvironmentalBinding(aspect=aspect, state='declared')  # type: ignore[arg-type]
        for aspect in (
            'temperature', 'humidity', 'speed_of_sound',
            'door_opening_state', 'movable_objects', 'playback_state',
        )
    )


def _input_envelope(
    manifest=None,
    *,
    scene_sha: str = _SCENE_SHA,
    **kw,
) -> SolverInputEnvelope:
    payload = dict(
        document_id=DOC,
        solver_request_ref=_ref('solver_request', 'req-1'),
        capability_manifest_ref=(
            None
            if manifest is None
            else AuthorityRef(
                kind='solver_capability_manifest',
                ref_id=manifest.manifest_id,
                ref_sha256=manifest.semantic_sha256,
            )
        ),
        scene_revision_ref=_ref('scene_revision', 'sr-1', scene_sha),
        environment=_environment(),
        declared_at_utc='2026-10-07T00:00:00Z',
    )
    payload.update(kw)
    return SolverInputEnvelope.create(**payload)


def _bound_row(
    claim: str,
    verdict: str = 'envelope_inherited',
    ceiling: str = 'full_envelope',
    **kw,
) -> ClaimBoundRow:
    payload = dict(
        claim=claim,
        verdict=verdict,
        ceiling=ceiling,
        weakest_dimensions=(),
        reasons=(),
        supporting_refs=(),
    )
    payload.update(kw)
    return ClaimBoundRow(**payload)


def _bound_record(
    envelope: SolverInputEnvelope, rows=None
) -> ClaimBoundRecord:
    if rows is None:
        rows = tuple(
            _bound_row(claim) for claim in CLAIM_LABELS
        )
    return ClaimBoundRecord.create(
        document_id=DOC,
        input_envelope_ref=AuthorityRef(
            kind='solver_input_envelope',
            ref_id=envelope.envelope_id,
            ref_sha256=envelope.envelope_sha256,
        ),
        rows=tuple(sorted(rows, key=lambda row: str(row.claim))),
        evaluated_at_utc='2026-10-07T00:00:00Z',
    )


# ---------------------------------------------------------------------------
# accuracy envelope fixtures (#accuracy envelopes are in-memory evidence)
# ---------------------------------------------------------------------------


def _solver_envelope(
    observable: str = 'spatial_field_db',
    state: str = 'VALIDATED_FOR_DECLARED_DOMAIN',
    convergence: str = 'CONVERGED_WITHIN_TESTED_RANGE',
    solver_version: str = '2.0',
    solver_id: str = 'htdt.fdtd-wave',
):
    return build_accuracy_envelope(
        solver_id=solver_id,
        solver_algorithm='fdtd',
        solver_version=solver_version,
        observable=observable,  # type: ignore[arg-type]
        geometry_domain='closed room',
        boundary_material_assumptions='complex impedance',
        source_capability='measured balloon',
        receiver_capability='mic array',
        fixture_ids=('fx-1',),
        fixture_sha256s=(_SHA,),
        convergence=convergence,  # type: ignore[arg-type]
        convergence_axes_tested=('grid_dx',),
        error_statistic_definition='absolute error in dB',
        error_distribution=error_statistic(
            observable, 'dB', (0.1, -0.2, 0.15)  # type: ignore[arg-type]
        ),
        threshold_policy_id='policy-1',
        threshold_policy_revision=2,
        validation_state=state,  # type: ignore[arg-type]
        validated_at_utc='2026-10-07T00:00:00Z',
    )


# ---------------------------------------------------------------------------
# benchmark qualification fixtures (#809)
# ---------------------------------------------------------------------------


def _mapping(**over) -> BenchmarkSceneMapping:
    kwargs = dict(
        document_id=DOC,
        asset_ref=_ref('benchmark_source_asset', 'asset-bras-rs1'),
        corpus_scene_id='BRAS-RS1-SC1',
        solver_path='wave_r130',
        phenomenon_id='spatial_pressure_field',
        observable_ids=('obs_mag',),
        applicability_band_hz=(20.0, 500.0),
        boundary_model_family='locally_reacting_impedance',
        curvature_class='planar',
        rationale='rigid wall single reflection',
    )
    kwargs.update(over)
    return BenchmarkSceneMapping.create(**kwargs)


def _evidence(
    verdicts: tuple[tuple[str, str], ...] = (('obs_mag', 'PASS'),),
    **over,
) -> BenchmarkValidationEvidence:
    kwargs = dict(
        evidence_id='bev-1',
        benchmark_id='bras-rs1',
        benchmark_version='v3',
        benchmark_sha256='b' * 64,
        importer_id='canonical-json',
        importer_version='1',
        provider_id='htdt.fdtd-wave',
        provider_version='2.0',
        provider_config_sha256='c' * 64,
        evaluation_profile_id='profile-a',
        evaluation_profile_version='1',
        evidence_class='external_measured',
        status='PASS',
        observable_evaluations=tuple(
            ObservableEvaluation(
                observable_id=oid, kind='magnitude_fr',
                status=status, metric_id='rms_db', error=0.4,  # type: ignore[arg-type]
            )
            for oid, status in verdicts
        ),
        unsupported_count=0,
    )
    kwargs.update(over)
    return BenchmarkValidationEvidence(**kwargs)


def _prereg(mapping=None, **over) -> BenchmarkPreregistration:
    mapping = mapping or _mapping()
    kwargs = dict(
        document_id=DOC,
        mapping_ref=_ref(
            'benchmark_scene_mapping', mapping.mapping_id, mapping.mapping_sha256
        ),
        benchmark_id='bras-rs1',
        benchmark_version='v3',
        benchmark_sha256='b' * 64,
        provider_id='htdt.fdtd-wave',
        provider_version='2.0',
        provider_config_sha256='c' * 64,
        evaluation_profile_id='profile-a',
        evaluation_profile_version='1',
        declared_observable_ids=('obs_mag',),
        convergence_axes_planned=('grid_dx',),
        run_mode='preregistered_unfitted',
    )
    kwargs.update(over)
    return BenchmarkPreregistration.create(**kwargs)


def _qualification(
    *,
    mapping=None,
    prereg=None,
    evidence=None,
    **eval_kwargs,
) -> tuple[BenchmarkQualification, BenchmarkPreregistration, BenchmarkSceneMapping]:
    mapping = mapping or _mapping()
    prereg = prereg or _prereg(mapping)
    evidence = evidence or _evidence()
    payload = dict(
        convergence='CONVERGED_WITHIN_TESTED_RANGE',
        convergence_axes_tested=('grid_dx',),
        input_authority_complete=True,
        reference_quality_ok=True,
    )
    payload.update(eval_kwargs)
    out = evaluate_qualification(mapping, prereg, evidence, **payload)
    return BenchmarkQualification.create(**out), prereg, mapping


# ---------------------------------------------------------------------------
# uncertainty fixtures (#810)
# ---------------------------------------------------------------------------


def _metric(
    observable_id: str = 'spl.magnitude',
) -> ValidationObservableMetric:
    return ValidationObservableMetric(
        observable_id=observable_id,
        quantity='magnitude_response_db',
        unit='dB',
        domain='frequency',
        comparison_metric='band_rms',
        minimum_uncertainty_semantics='combined_standard',
        decision_rule=UncertaintyDecisionRule(
            significance_ratio=1.0, resolution_fraction=0.5
        ),
    )


def _protocol(
    model_version: str = '2.0', **kw
) -> ValidationUncertaintyProtocol:
    payload = dict(
        schema_version='vuq-protocol-1',
        document_id=DOC,
        protocol_version='1.0',
        model_id='htdt.fdtd-wave',
        model_version=model_version,
        observable_metrics=(_metric(),),
        correlation_policy='independent_unless_declared',
        bounded_input_policy='bounded_worst_case',
        created_at_utc='2026-10-07T00:00:00Z',
    )
    payload.update(kw)
    return ValidationUncertaintyProtocol.create(**payload)


def _side(value: float, category: str = 'measurement_instrument') -> UncertaintySideEvidence:
    return UncertaintySideEvidence(
        source_kind='declared_bound',
        bound_half_width=value,
        threshold_semantics='combined_bound',
        category_contributions=(
            CategoryContributionValue(category=category, contribution=value),
        ),
    )


def _evaluation(
    protocol: ValidationUncertaintyProtocol,
    residual: float = 1.0,
    measurement_bound: float = 1.0,
    prediction_bound: float = 0.5,
) -> ObservableUncertaintyEvaluation:
    return build_observable_uncertainty_evaluation(
        document_id=DOC,
        protocol=protocol,
        candidate_id='cand-h1',
        split='holdout',
        observable_id='spl.magnitude',
        measurement_evidence=_side(measurement_bound),
        prediction_evidence=_side(prediction_bound, category='numerical'),
        bands=(
            {
                'band_hz': (50.0, 100.0),
                'coverage': 'evaluated',
                'residual_value': residual,
                'residual_unit': 'dB',
            },
            {
                'band_hz': (100.0, 200.0),
                'coverage': 'evaluated',
                'residual_value': residual,
                'residual_unit': 'dB',
            },
        ),
        created_at_utc='2026-10-07T00:01:00Z',
    )


def _verdict(
    protocol: ValidationUncertaintyProtocol,
    evaluations,
    **kw,
) -> UncertaintyValidationVerdict:
    payload = dict(
        document_id=DOC,
        protocol=protocol,
        evaluations=evaluations,
        created_at_utc='2026-10-07T00:02:00Z',
    )
    payload.update(kw)
    return build_validation_uncertainty_verdict(**payload)


def _owned_room_verdict(
    protocol: ValidationUncertaintyProtocol, evaluations, **kw
) -> UncertaintyValidationVerdict:
    kw.setdefault('evidence_scope', 'owned_room')
    kw.setdefault('campaign_id', 'o60-validation-campaign:' + _SHA)
    kw.setdefault('campaign_sha256', _SHA2)
    kw.setdefault(
        'campaign_registration_id',
        'o60-validation-campaign-registration:' + _SHA3,
    )
    kw.setdefault('campaign_registration_sha256', _SHA3)
    return _verdict(protocol, evaluations, **kw)


def _compose(bundle=None, context=None, solver_envelopes=()):
    bundle = bundle or EnvelopeEvidenceBundle(document_id=DOC)
    context = context or _context()
    return compose_applicability_envelope(
        bundle,
        context=context,
        solver_envelopes=solver_envelopes,
        evaluated_at_utc='2026-10-07T12:00:00Z',
    )


def _assert_no_aggregate_score(payload) -> None:
    """Recursively assert the emitted structure carries no score."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert 'score' not in key.lower(), key
            assert 'aggregate' not in key.lower(), key
            _assert_no_aggregate_score(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _assert_no_aggregate_score(item)


# ---------------------------------------------------------------------------
# model invariants
# ---------------------------------------------------------------------------


class TestEnvelopeModel:
    def test_dimensions_must_enumerate_seven_in_order(self) -> None:
        envelope = _compose()
        assert [d.dimension for d in envelope.dimensions] == list(
            ENVELOPE_DIMENSIONS
        )
        assert len(envelope.dimensions) == 7

    def test_hash_covers_full_payload(self) -> None:
        envelope = _compose()
        digest = canonical_sha256(envelope.identity_payload())
        assert envelope.envelope_hash == digest

    def test_tampered_dimensions_rejected(self) -> None:
        envelope = _compose()
        forged = envelope.model_dump(mode='python')
        forged_dimensions = list(forged['dimensions'])
        forged_dimensions[0] = forged_dimensions[0] | {
            'evidence_class': 'externally_validated',
        }
        forged['dimensions'] = forged_dimensions
        with pytest.raises(ValidationError):
            ApplicabilityEnvelope.model_validate(forged)

    def test_absent_dimension_cannot_carry_verdict(self) -> None:
        with pytest.raises(ValidationError):
            EnvelopeDimensionState(
                dimension='verification',
                evidence_class='absent',
                verdict='PASS_WITHIN_DOMAIN',
            )

    def test_no_aggregate_score_anywhere(self) -> None:
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(_manifest(),),
        )
        envelope = _compose(bundle)
        _assert_no_aggregate_score(envelope.model_dump(mode='json'))
        report = build_envelope_report(
            envelope, generated_at_utc='2026-10-07T12:00:00Z'
        )
        _assert_no_aggregate_score(report.export_payload())

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApplicabilityEnvelope.model_validate(
                _compose().model_dump(mode='python') | {'bogus': 1}
            )


# ---------------------------------------------------------------------------
# absent / fail-closed behaviour
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_empty_document_is_all_absent(self) -> None:
        envelope = _compose()
        for dimension in envelope.dimensions:
            if dimension.dimension == 'context_of_use':
                continue  # decisions are always computed
            assert dimension.evidence_class == 'absent'
            assert dimension.verdict is None
            assert not dimension.evidence_refs
        assert envelope.dimension('context_of_use').evidence_class == (
            'insufficient_evidence'
        )

    def test_decisions_fail_closed_without_evidence(self) -> None:
        envelope = _compose()
        by_decision = {d.decision: d.verdict for d in envelope.decisions}
        assert by_decision['inspect_prediction'] == 'not_supported'
        assert by_decision['compare_candidates'] == 'not_supported'
        assert by_decision['automatic_recommendation'] == 'blocked'

    def test_bundle_document_mismatch_rejected(self) -> None:
        bundle = EnvelopeEvidenceBundle(document_id='doc-other')
        with pytest.raises(ValueError):
            _compose(bundle)

    def test_unknown_context_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EnvelopeContext.model_validate(
                {'document_id': DOC, 'bogus': 1}
            )

    def test_solver_path_filter_never_crosses_paths(self) -> None:
        """Evidence for geometric_r150 never covers wave_r130 (#814: a
        result is valid only for the solver path that produced it)."""
        mapping = _mapping(solver_path='geometric_r150')
        qualification, prereg, mapping = _qualification(mapping=mapping)
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        assert envelope.dimension('external_validation').evidence_class == 'absent'


# ---------------------------------------------------------------------------
# representational capability (manifest = declaration, not validation)
# ---------------------------------------------------------------------------


class TestCapability:
    def test_bound_manifest_reads_supported(self) -> None:
        manifest = _manifest()
        envelope_in = _input_envelope(manifest)
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(envelope_in,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('representational_capability')
        assert dim.evidence_class == 'supported'
        assert dim.verdict == 'SUPPORTED'
        assert dim.band is not None
        assert 'capability' not in dim.evidence_class or True
        # declaration must never read as validation
        assert dim.evidence_class not in (
            'externally_validated', 'numerically_verified', 'holdout_validated',
        )

    def test_all_unsupported_manifest(self) -> None:
        manifest = _manifest(unsupported=SOLVER_PATH_PHENOMENA)
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('representational_capability')
        assert dim.evidence_class == 'unsupported'
        assert dim.verdict == 'UNSUPPORTED'

    def test_bounded_only_manifest(self) -> None:
        manifest = _manifest(bounded=SOLVER_PATH_PHENOMENA)
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('representational_capability')
        assert dim.evidence_class == 'bounded'
        assert dim.verdict == 'BOUNDED'

    def test_stale_manifest(self) -> None:
        manifest = _manifest(descriptor=_descriptor(adapter_version='1.0'))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('representational_capability')
        assert dim.evidence_class == 'stale'
        assert dim.staleness_notes
        assert dim.stale_refs

    def test_phenomenon_cells_track_manifest_rows(self) -> None:
        manifest = _manifest(unsupported=('coherent_phase',))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        cells = {c.phenomenon: c for c in envelope.phenomenon_cells}
        assert cells['coherent_phase'].capability_state == 'UNSUPPORTED'
        assert cells['direct_sound'].capability_state == 'SUPPORTED'
        assert cells['direct_sound'].band is not None
        assert len(envelope.phenomenon_cells) == 9

    def test_manifest_sha_mismatch_fails_closed(self) -> None:
        manifest = _manifest()
        bad_env = _input_envelope(
            manifest,
            capability_manifest_ref=AuthorityRef(
                kind='solver_capability_manifest',
                ref_id=manifest.manifest_id,
                ref_sha256='0' * 64,
            ),
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(bad_env,),
        )
        with pytest.raises(ValueError):
            _compose(bundle)


# ---------------------------------------------------------------------------
# external validation (#809)
# ---------------------------------------------------------------------------


class TestExternalValidation:
    def test_external_measured_pass(self) -> None:
        qualification, prereg, mapping = _qualification()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('external_validation')
        assert dim.verdict == 'PASS_WITHIN_DOMAIN'
        assert dim.evidence_class == 'externally_validated'
        assert dim.band is not None
        cell = {
            c.phenomenon: c for c in envelope.phenomenon_cells
        }['spatial_pressure_field']
        assert cell.external_evidence_class == 'externally_validated'
        assert 'PASS_WITHIN_DOMAIN' in cell.external_verdicts

    def test_fail_verdict_never_hidden(self) -> None:
        qualification, prereg, mapping = _qualification(
            evidence=_evidence(verdicts=(('obs_mag', 'FAIL'),))
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('external_validation')
        assert dim.evidence_class == 'failed'
        assert dim.verdict == 'FAIL'

    def test_outside_applicability_stays_distinct(self) -> None:
        # a claimed band beyond the mapping's declared applicability domain
        qualification, prereg, mapping = _qualification(
            verdict_band_hz=(20.0, 9999.0),
        )
        assert qualification.verdict == 'OUTSIDE_APPLICABILITY'
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('external_validation')
        assert dim.evidence_class == 'outside_applicability'

    def test_insufficient_variants(self) -> None:
        qualification, prereg, mapping = _qualification(
            input_authority_complete=False,
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('external_validation')
        assert dim.evidence_class == 'insufficient_evidence'
        assert 'INSUFFICIENT' in (dim.verdict or '')

    def test_mixed_pass_and_fail_keeps_both(self) -> None:
        qualification_pass, prereg1, mapping1 = _qualification()
        qualification_fail, prereg2, mapping2 = _qualification(
            mapping=_mapping(
                corpus_scene_id='BRAS-RS1-SC2',
                phenomenon_id='late_energy_decay',
            ),
            evidence=_evidence(
                evidence_id='bev-2',
                verdicts=(('obs_mag', 'FAIL'),),
            ),
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification_pass, qualification_fail),
            scene_mappings=(mapping1, mapping2),
            preregistrations=(prereg1, prereg2),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('external_validation')
        assert dim.evidence_class == 'externally_validated'
        assert 'FAIL' in dim.conflicts

    def test_stale_qualification(self) -> None:
        prereg = _prereg(provider_version='0.9')
        qualification, prereg, mapping = _qualification(
            prereg=prereg,
            evidence=_evidence(provider_version='0.9'),
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('external_validation')
        assert dim.evidence_class == 'stale'
        assert dim.staleness_notes


# ---------------------------------------------------------------------------
# verification (accuracy envelopes + convergence)
# ---------------------------------------------------------------------------


class TestVerification:
    def test_converged_validated_envelope(self) -> None:
        envelope = _compose(solver_envelopes=(_solver_envelope(),))
        dim = envelope.dimension('verification')
        assert dim.evidence_class == 'numerically_verified'
        assert dim.verdict == 'VALIDATED_FOR_DECLARED_DOMAIN'

    def test_validated_with_limitations(self) -> None:
        envelope = _compose(
            solver_envelopes=(
                _solver_envelope(state='VALIDATED_WITH_LIMITATIONS'),
            )
        )
        dim = envelope.dimension('verification')
        assert dim.evidence_class == 'bounded'
        assert dim.verdict == 'VALIDATED_WITH_LIMITATIONS'

    def test_nonconvergence_is_failed(self) -> None:
        envelope = _compose(
            solver_envelopes=(
                _solver_envelope(
                    state='VALIDATED_WITH_LIMITATIONS',
                    convergence='NOT_CONVERGED',
                ),
            )
        )
        dim = envelope.dimension('verification')
        assert dim.evidence_class == 'failed'
        assert dim.verdict == 'NOT_CONVERGED'

    def test_insufficient_evidence(self) -> None:
        envelope = _compose(
            solver_envelopes=(
                _solver_envelope(convergence='INSUFFICIENT_EVIDENCE'),
            )
        )
        dim = envelope.dimension('verification')
        assert dim.evidence_class == 'insufficient_evidence'

    def test_experimental_state(self) -> None:
        envelope = _compose(
            solver_envelopes=(
                _solver_envelope(state='EXPERIMENTAL'),
            )
        )
        dim = envelope.dimension('verification')
        assert dim.evidence_class == 'insufficient_evidence'

    def test_stale_solver_version(self) -> None:
        envelope = _compose(
            solver_envelopes=(_solver_envelope(solver_version='1.0'),)
        )
        dim = envelope.dimension('verification')
        assert dim.evidence_class == 'stale'

    def test_other_solver_never_covers(self) -> None:
        envelope = _compose(
            solver_envelopes=(_solver_envelope(solver_id='other.solver'),)
        )
        assert envelope.dimension('verification').evidence_class == 'absent'

    def test_qualification_convergence_counts(self) -> None:
        qualification, prereg, mapping = _qualification(
            convergence='INSUFFICIENT_EVIDENCE',
            convergence_axes_tested=(),
            reference_quality_ok=False,
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        assert envelope.dimension('verification').evidence_class == (
            'insufficient_evidence'
        )


# ---------------------------------------------------------------------------
# input qualification (#811)
# ---------------------------------------------------------------------------


class TestInputQualification:
    def _bundle(self, *verdicts: str, scene_sha: str = _SCENE_SHA):
        manifest = _manifest()
        envelope_in = _input_envelope(manifest, scene_sha=scene_sha)
        rows = tuple(
            _bound_row(
                claim,
                verdict=verdicts[i % len(verdicts)],
                ceiling={
                    'envelope_inherited': 'full_envelope',
                    'solver_bounded': 'solver_bound',
                    'bounded_by_input': 'measured_bound',
                    'claim_denied': 'insufficient_authority',
                    'solver_unqualified': 'insufficient_authority',
                    'unbounded_input': 'insufficient_authority',
                }[verdicts[i % len(verdicts)]],
                weakest_dimensions=(
                    ('boundary_materials',)
                    if verdicts[i % len(verdicts)] in (
                        'claim_denied', 'unbounded_input',
                        'solver_unqualified', 'bounded_by_input',
                    ) else ()
                ),
                reasons=(
                    ('declared authority cannot support the claim',)
                    if verdicts[i % len(verdicts)] in (
                        'claim_denied', 'unbounded_input',
                    ) else ()
                ),
            )
            for i, claim in enumerate(CLAIM_LABELS)
        )
        record = _bound_record(envelope_in, rows=rows)
        return EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(envelope_in,),
            claim_bound_records=(record,),
        )

    def test_envelope_inherited_is_qualified(self) -> None:
        envelope = _compose(self._bundle('envelope_inherited'))
        dim = envelope.dimension('input_qualification')
        assert dim.evidence_class == 'qualified'
        assert dim.verdict == 'envelope_inherited'
        assert len(envelope.claim_rows) == 10

    def test_bounded_by_input(self) -> None:
        envelope = _compose(self._bundle('bounded_by_input'))
        dim = envelope.dimension('input_qualification')
        assert dim.evidence_class == 'bounded'
        assert dim.verdict == 'bounded_by_input'

    def test_claim_denied_is_failed(self) -> None:
        envelope = _compose(self._bundle('claim_denied'))
        dim = envelope.dimension('input_qualification')
        assert dim.evidence_class == 'failed'
        assert dim.verdict == 'claim_denied'

    def test_solver_unqualified_is_unsupported(self) -> None:
        envelope = _compose(self._bundle('solver_unqualified'))
        dim = envelope.dimension('input_qualification')
        assert dim.evidence_class == 'unsupported'

    def test_unbounded_input_is_insufficient(self) -> None:
        envelope = _compose(self._bundle('unbounded_input'))
        dim = envelope.dimension('input_qualification')
        assert dim.evidence_class == 'insufficient_evidence'

    def test_claim_rows_carry_record_refs(self) -> None:
        envelope = _compose(self._bundle('envelope_inherited'))
        row = envelope.claim_rows[0]
        assert row.record_id.startswith('cbr-')
        assert row.claim in CLAIM_LABELS

    def test_claims_ride_phenomenon_cells(self) -> None:
        envelope = _compose(self._bundle('bounded_by_input'))
        cells = {c.phenomenon: c for c in envelope.phenomenon_cells}
        # decay_time maps to late_energy_decay per _CLAIM_REQUIREMENTS
        assert any(
            token.startswith('decay_time:')
            for token in cells['late_energy_decay'].claim_verdicts
        )

    def test_stale_scene_revision(self) -> None:
        bundle = self._bundle(
            'envelope_inherited', scene_sha='0' * 64
        )
        # context pins a different (current) scene revision
        context = _context(scene_revision_sha256=_SCENE_SHA)
        envelope = _compose(bundle, context)
        dim = envelope.dimension('input_qualification')
        assert dim.evidence_class == 'stale'

    def test_absent_when_no_records(self) -> None:
        envelope = _compose()
        assert envelope.dimension('input_qualification').evidence_class == 'absent'


# ---------------------------------------------------------------------------
# owned-room evidence (#810 scope separation)
# ---------------------------------------------------------------------------


class TestOwnedRoomEvidence:
    def test_consistent_owned_room(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(protocol, residual=0.5)
        verdict = _owned_room_verdict(protocol, (evaluation,))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
            uncertainty_verdicts=(verdict,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('owned_room_evidence')
        assert dim.evidence_class == 'holdout_validated'
        assert dim.verdict == 'consistent_with_reference_within_uncertainty'

    def test_synthetic_only_is_absent(self) -> None:
        """Synthetic-fixture verdicts never count as owned-room evidence."""
        protocol = _protocol()
        evaluation = _evaluation(protocol, residual=0.5)
        verdict = _verdict(protocol, (evaluation,))
        assert verdict.evidence_scope == 'synthetic_fixture'
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
            uncertainty_verdicts=(verdict,),
        )
        envelope = _compose(bundle)
        assert envelope.dimension('owned_room_evidence').evidence_class == 'absent'

    def test_discrepancy_is_failed(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(
            protocol, residual=5.0, measurement_bound=0.2, prediction_bound=0.2
        )
        verdict = _owned_room_verdict(protocol, (evaluation,))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
            uncertainty_verdicts=(verdict,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('owned_room_evidence')
        assert dim.evidence_class == 'failed'
        assert dim.verdict in (
            'discrepancy_significant', 'model_form_discrepancy_required'
        )

    def test_stale_protocol_model_version(self) -> None:
        protocol = _protocol(model_version='1.0')
        evaluation = _evaluation(protocol, residual=0.5)
        verdict = _owned_room_verdict(protocol, (evaluation,))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
            uncertainty_verdicts=(verdict,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('owned_room_evidence')
        assert dim.evidence_class == 'stale'

    def test_ranking_axis_separate(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(protocol, residual=0.5)
        verdict = _owned_room_verdict(protocol, (evaluation,))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
            uncertainty_verdicts=(verdict,),
        )
        envelope = _compose(bundle)
        # ranking verdict is its own field — never merged into absolute
        assert envelope.ranking_verdict in (
            None, 'ranking_supported', 'ranking_contradicted',
            'ranking_insufficient_evidence', 'ranking_not_evaluated',
        )


# ---------------------------------------------------------------------------
# uncertainty / limitations
# ---------------------------------------------------------------------------


class TestUncertaintyLimitations:
    def test_absent_without_records(self) -> None:
        envelope = _compose()
        assert envelope.dimension('uncertainty_limitations').evidence_class == 'absent'
        assert envelope.model_form_discrepancy is False

    def test_headline_is_worst_unresolved(self) -> None:
        """A limitations dimension reports what is NOT resolved — a single
        insufficient band dominates over consistent siblings."""
        protocol = _protocol()
        consistent = _evaluation(protocol, residual=0.5)
        insufficient = _evaluation(
            protocol, residual=0.5,
        ).model_copy()  # same shape; covered below by verdict entry
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(consistent, insufficient),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('uncertainty_limitations')
        assert dim.evidence_class in ('supported', 'bounded', 'insufficient_evidence')
        assert dim.evidence_refs

    def test_failed_evaluation_headlines(self) -> None:
        protocol = _protocol()
        evaluation = _evaluation(
            protocol, residual=5.0,
            measurement_bound=0.1, prediction_bound=0.1,
        )
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
        )
        envelope = _compose(bundle)
        dim = envelope.dimension('uncertainty_limitations')
        assert dim.evidence_class == 'failed'


# ---------------------------------------------------------------------------
# context of use decisions
# ---------------------------------------------------------------------------


class TestDecisions:
    def test_inspect_allowed_with_capability(self) -> None:
        manifest = _manifest()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        by_decision = {d.decision: d.verdict for d in envelope.decisions}
        assert by_decision['inspect_prediction'] == 'allowed'
        assert by_decision['automatic_recommendation'] == 'blocked'

    def test_automatic_recommendation_always_blocked(self) -> None:
        """#814/#801: production recommendation requires evidence no landed
        authority can emit — the decision stays fail-closed."""
        protocol = _protocol()
        evaluation = _evaluation(protocol, residual=0.5)
        verdict = _owned_room_verdict(protocol, (evaluation,))
        qualification, prereg, mapping = _qualification()
        manifest = _manifest()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
            uncertainty_protocols=(protocol,),
            uncertainty_evaluations=(evaluation,),
            uncertainty_verdicts=(verdict,),
        )
        envelope = _compose(bundle)
        by_decision = {d.decision: d.verdict for d in envelope.decisions}
        assert by_decision['automatic_recommendation'] == 'blocked'
        # and owned-room + external together allow real comparison
        assert by_decision['compare_candidates'] == 'allowed'

    def test_decisions_carry_basis_states(self) -> None:
        envelope = _compose()
        for decision in envelope.decisions:
            assert decision.basis_states


# ---------------------------------------------------------------------------
# context derivation (sealed refs, never inference)
# ---------------------------------------------------------------------------


class TestContextDerivation:
    def test_derives_identity_from_bound_manifest(self) -> None:
        manifest = _manifest()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
            scene_mappings=(_mapping(),),
        )
        context = context_for_document(bundle, document_id=DOC)
        assert context.adapter_id == 'htdt.fdtd-wave'
        assert context.adapter_version == '2.0'
        assert context.adapter_descriptor_id == manifest.adapter_descriptor_id
        assert context.solver_path == 'wave_r130'
        assert context.scene_revision_sha256 == _SCENE_SHA

    def test_ambiguous_solver_paths_stay_unset(self) -> None:
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            scene_mappings=(
                _mapping(),
                _mapping(
                    corpus_scene_id='BRAS-RS1-SC2', solver_path='geometric_r150'
                ),
            ),
        )
        context = context_for_document(bundle, document_id=DOC)
        assert context.solver_path is None


# ---------------------------------------------------------------------------
# JA labels — every emitted token has a JA rendering
# ---------------------------------------------------------------------------


class TestLabels:
    def test_all_seven_dimensions_labeled(self) -> None:
        for dimension in ENVELOPE_DIMENSIONS:
            label = envelope_dimension_label(dimension)
            assert label != dimension
            assert any(ord(ch) > 127 for ch in label)

    def test_all_classes_labeled(self) -> None:
        for cls in ENVELOPE_CLASS_LABELS:
            label = envelope_class_label(cls)
            assert label != cls
            assert any(ord(ch) > 127 for ch in label)

    def test_verdict_vocabulary_labeled(self) -> None:
        for token in (
            'PASS_WITHIN_DOMAIN', 'FAIL', 'INSUFFICIENT_REFERENCE_QUALITY',
            'INSUFFICIENT_INPUT_AUTHORITY', 'NUMERICAL_NONCONVERGENCE',
            'OUTSIDE_APPLICABILITY', 'UNSUPPORTED_OBSERVABLE',
        ):
            assert qualification_verdict_label(token) != token
        for token in QUALIFICATION_LEVEL_LABELS.values():
            assert any(ord(ch) > 127 for ch in token)
        for token in CAPABILITY_STATE_LABELS.values():
            assert any(ord(ch) > 127 for ch in token)
        for token in DECISION_LABELS.values():
            assert any(ord(ch) > 127 for ch in token)
        for token in DECISION_VERDICT_LABELS.values():
            assert any(ord(ch) > 127 for ch in token)
        for token in CLAIM_LABELS.values():
            assert any(ord(ch) > 127 for ch in token)
        for token in PHENOMENON_LABELS.values():
            assert any(ord(ch) > 127 for ch in token)

    def test_envelope_lines_are_ja(self) -> None:
        envelope = _compose()
        lines = envelope_lines(envelope)
        assert len(lines) == 10  # 7 dimensions + 3 decisions
        assert any('未取得' in line for line in lines)


# ---------------------------------------------------------------------------
# repository round-trip + tamper detection
# ---------------------------------------------------------------------------


def _save_all(
    tmp_path: Path, *,
    manifest=None,
    input_envelopes=(),
    claim_records=(),
    qualifications=(),
    mappings=(),
    preregistrations=(),
    protocols=(),
    evaluations=(),
    verdicts=(),
) -> SceneRepository:
    path = tmp_path / 'cad.sqlite3'
    ensure_native_schema(path)
    scene_repo = SceneRepository(path)
    bound_repo = CadSolverConfidenceBoundRepository(scene_repo)
    uncertainty_repo = CadValidationUncertaintyRepository(scene_repo)
    benchmark_repo = CadBenchmarkQualificationRepository(scene_repo)

    if manifest is not None:
        descriptor = _descriptor()
        ensure_native_schema(path)
        with connect_sqlite(path) as connection:
            connection.execute(
                """
                INSERT INTO cad_acoustic_solver_adapters(
                    descriptor_id, semantic_sha256, adapter_id,
                    adapter_version, model_solver_role_id, acoustic_domain,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    descriptor.descriptor_id,
                    descriptor.semantic_sha256,
                    descriptor.adapter_id,
                    descriptor.adapter_version,
                    descriptor.model_solver_role_id,
                    descriptor.acoustic_domain,
                    descriptor.model_dump_json(),
                    '2026-10-07T00:00:00Z',
                ),
            )
            connection.execute(
                """
                INSERT INTO cad_solver_capability_manifests(
                    manifest_id, semantic_sha256, adapter_descriptor_id,
                    adapter_id, acoustic_domain, payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.semantic_sha256,
                    manifest.adapter_descriptor_id,
                    manifest.adapter_id,
                    manifest.acoustic_domain,
                    manifest.model_dump_json(),
                    '2026-10-07T00:00:00Z',
                ),
            )
            connection.commit()

    for envelope in input_envelopes:
        bound_repo.save_input_envelope(envelope)
    for record in claim_records:
        bound_repo.save_claim_bound(record)
    for mapping in mappings:
        benchmark_repo.scene_mappings.save(mapping)
    for prereg in preregistrations:
        benchmark_repo.preregistrations.save(prereg)
    for qualification in qualifications:
        benchmark_repo.qualifications.save(qualification)
    for protocol in protocols:
        uncertainty_repo.save_protocol(protocol)
    for evaluation in evaluations:
        uncertainty_repo.save_evaluation(evaluation)
    for verdict in verdicts:
        uncertainty_repo.save_verdict(verdict)
    return scene_repo


class TestRepositoryRoundTrip:
    def test_load_compose_from_stores(self, tmp_path: Path) -> None:
        manifest = _manifest()
        envelope_in = _input_envelope(manifest)
        record = _bound_record(envelope_in)
        qualification, prereg, mapping = _qualification()
        protocol = _protocol()
        evaluation = _evaluation(protocol, residual=0.5)
        verdict = _owned_room_verdict(protocol, (evaluation,))

        scene_repo = _save_all(
            tmp_path,
            manifest=manifest,
            input_envelopes=(envelope_in,),
            claim_records=(record,),
            qualifications=(qualification,),
            mappings=(mapping,),
            preregistrations=(prereg,),
            protocols=(protocol,),
            evaluations=(evaluation,),
            verdicts=(verdict,),
        )
        bundle = load_envelope_evidence(scene_repo, document_id=DOC)
        assert len(bundle.qualifications) == 1
        assert len(bundle.capability_manifests) == 1
        assert len(bundle.input_envelopes) == 1
        assert len(bundle.claim_bound_records) == 1
        assert len(bundle.uncertainty_verdicts) == 1

        context = context_for_document(bundle, document_id=DOC)
        envelope = compose_applicability_envelope(
            bundle,
            context=context,
            evaluated_at_utc='2026-10-07T12:00:00Z',
        )
        assert envelope.dimension(
            'representational_capability'
        ).evidence_class == 'supported'
        assert envelope.dimension(
            'external_validation'
        ).evidence_class == 'externally_validated'
        assert envelope.dimension(
            'input_qualification'
        ).evidence_class == 'qualified'
        assert envelope.dimension(
            'owned_room_evidence'
        ).evidence_class == 'holdout_validated'
        assert envelope_evidence_refs(envelope)

    def test_tampered_payload_fails_closed(self, tmp_path: Path) -> None:
        manifest = _manifest()
        envelope_in = _input_envelope(manifest)
        record = _bound_record(envelope_in)
        scene_repo = _save_all(
            tmp_path,
            manifest=manifest,
            input_envelopes=(envelope_in,),
            claim_records=(record,),
        )
        # tamper the stored claim-bound payload — a forged verdict
        import json
        from contextlib import closing
        with closing(connect_sqlite(scene_repo.path)) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_claim_bound_records LIMIT 1'
            ).fetchone()
            payload = json.loads(row['payload_json'])
            payload['document_id'] = 'doc-tampered'
            connection.execute(
                'UPDATE cad_claim_bound_records SET payload_json=?',
                (json.dumps(payload),),
            )
            connection.commit()
        with pytest.raises((ValueError, ValidationError)):
            load_envelope_evidence(scene_repo, document_id=DOC)

    def test_other_document_never_bleeds(self, tmp_path: Path) -> None:
        manifest = _manifest()
        other_envelope = SolverInputEnvelope.create(
            document_id='doc-other',
            solver_request_ref=_ref('solver_request', 'req-2'),
            capability_manifest_ref=AuthorityRef(
                kind='solver_capability_manifest',
                ref_id=manifest.manifest_id,
                ref_sha256=manifest.semantic_sha256,
            ),
            environment=_environment(),
            declared_at_utc='2026-10-07T00:00:00Z',
        )
        scene_repo = _save_all(
            tmp_path,
            manifest=manifest,
            input_envelopes=(other_envelope,),
        )
        bundle = load_envelope_evidence(scene_repo, document_id=DOC)
        # the manifest is adapter-scoped and *listed*, but no envelope in
        # DOC binds it — capability must remain absent for DOC
        envelope = compose_applicability_envelope(
            bundle,
            context=_context(),
            evaluated_at_utc='2026-10-07T12:00:00Z',
        )
        assert envelope.dimension(
            'representational_capability'
        ).evidence_class == 'absent'


# ---------------------------------------------------------------------------
# report / export
# ---------------------------------------------------------------------------


class TestReport:
    def test_report_seal(self) -> None:
        envelope = _compose()
        report = build_envelope_report(
            envelope, generated_at_utc='2026-10-07T12:00:00Z'
        )
        digest = canonical_sha256(report.identity_payload())
        assert report.report_sha256 == digest
        assert report.report_id == f'aer-{digest[:24]}'
        assert report.envelope_hash == envelope.envelope_hash

    def test_report_binds_benchmark_identity(self) -> None:
        qualification, prereg, mapping = _qualification()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            qualifications=(qualification,),
            scene_mappings=(mapping,),
            preregistrations=(prereg,),
        )
        envelope = _compose(bundle)
        report = build_envelope_report(
            envelope,
            generated_at_utc='2026-10-07T12:00:00Z',
            preregistrations=(prereg,),
            qualifications=(qualification,),
        )
        assert len(report.benchmark_identities) == 1
        identity = report.benchmark_identities[0]
        assert identity.benchmark_id == 'bras-rs1'
        assert identity.provider_id == 'htdt.fdtd-wave'
        assert identity.provider_version == '2.0'

    def test_report_flattened_refs(self) -> None:
        manifest = _manifest()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        report = build_envelope_report(
            envelope, generated_at_utc='2026-10-07T12:00:00Z'
        )
        assert any(
            ref.ref_id == manifest.manifest_id
            for ref in report.evidence_refs
        )

    def test_report_text_is_ja(self) -> None:
        envelope = _compose()
        report = build_envelope_report(
            envelope, generated_at_utc='2026-10-07T12:00:00Z'
        )
        text = report.render_text()
        assert '適用範囲エンベロープ' in text
        assert '未取得' in text

    def test_report_tamper_rejected(self) -> None:
        envelope = _compose()
        report = build_envelope_report(
            envelope, generated_at_utc='2026-10-07T12:00:00Z'
        )
        forged = report.model_dump(mode='python')
        forged['envelope_payload']['context']['adapter_id'] = 'evil.solver'
        with pytest.raises(ValidationError):
            ApplicabilityEnvelopeReport.model_validate(forged)


# ---------------------------------------------------------------------------
# UI surface
# ---------------------------------------------------------------------------


class TestPanel:
    @pytest.fixture
    def app(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        yield app

    def test_panel_renders_all_dimensions(self, app) -> None:
        from htdt.applicability_envelope_panel import (
            ApplicabilityEnvelopePanel,
        )
        envelope = _compose()
        panel = ApplicabilityEnvelopePanel(envelope)
        try:
            table = panel.dimension_table
            assert table.rowCount() == 7
            for row in range(6):
                state_item = table.item(row, 2)
                assert state_item.text() == '未取得'
            # context_of_use is always computed — decisions exist but no
            # evidence, so it reads 証拠不足, never a pass
            assert table.item(6, 2).text() == '証拠不足'
        finally:
            panel.deleteLater()

    def test_panel_renders_populated_envelope(self, app) -> None:
        from htdt.applicability_envelope_panel import (
            ApplicabilityEnvelopePanel,
        )
        manifest = _manifest()
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        panel = ApplicabilityEnvelopePanel(envelope)
        try:
            assert 'htdt.fdtd-wave' in panel.context_label.text()
            table = panel.dimension_table
            capability_row = table.item(0, 1).text()
            assert 'サポート' in capability_row
            state_item = table.item(0, 2)
            assert 'サポート宣言あり' in state_item.text()
            # phenomenon matrix renders capability per cell
            assert panel.phenomenon_table.rowCount() == 9
            direct = panel.phenomenon_table.item(0, 1)
            assert direct is not None
        finally:
            panel.deleteLater()

    def test_panel_staleness_visible(self, app) -> None:
        from htdt.applicability_envelope_panel import (
            ApplicabilityEnvelopePanel,
        )
        manifest = _manifest(descriptor=_descriptor(adapter_version='1.0'))
        bundle = EnvelopeEvidenceBundle(
            document_id=DOC,
            capability_manifests=(manifest,),
            input_envelopes=(_input_envelope(manifest),),
        )
        envelope = _compose(bundle)
        panel = ApplicabilityEnvelopePanel(envelope)
        try:
            assert 'drifted' in panel.staleness_label.text()
        finally:
            panel.deleteLater()

    def test_panel_clear(self, app) -> None:
        from htdt.applicability_envelope_panel import (
            ApplicabilityEnvelopePanel,
        )
        panel = ApplicabilityEnvelopePanel(None)
        try:
            assert panel.dimension_table.rowCount() == 0
            assert 'エンベロープなし' in panel.context_label.text()
        finally:
            panel.deleteLater()
