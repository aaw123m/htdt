"""Lifecycle/authority integrity contracts from the deep-review issues.

Covers: #867 (measurement/inbox disposition eligibility), #868 (design
decision single-head supersession + fail-closed reads), #869 (assumption
decision same-subject lineage + temporal validity), #870 (design brief
explicit head), #871 (canonical resolver kind routing), #873
(reconciliation input pins).
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from htdt.cad_analysis_study import build_analysis_study
from htdt.cad_analysis_study_repository import CadAnalysisStudyRepository
from htdt.cad_authority_resolver import AuthorityRef, ResolvedAuthority
from htdt.cad_assumption_decision import (
    AssumptionDecisionIntegrityError,
    AssumptionSubjectRef,
    active_assumption_decisions,
    build_assumption_decision,
)
from htdt.cad_assumption_decision_repository import (
    AssumptionDecisionStaleHeadError,
    CadAssumptionDecisionRepository,
)
from htdt.cad_authority_refs import CanonicalAuthorityRefResolver
from htdt.cad_design_brief import (
    BriefGoalRef,
    DesignBriefIntegrityError,
    build_design_brief,
    current_brief,
    revise_design_brief,
)
from htdt.cad_design_brief_repository import (
    CadDesignBriefRepository,
    DesignBriefStaleHeadError,
)
from htdt.cad_design_comparison import (
    build_alternative,
    build_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_design_decision import (
    DecisionAuthorityRef,
    DesignDecisionIntegrityError,
    build_decision_record,
    current_decisions,
    decision_lineage_issues,
)
from htdt.cad_design_decision_repository import (
    CadDesignDecisionRepository,
    DesignDecisionStaleHeadError,
)
from htdt.cad_evidence_reconciliation import (
    AlignmentRef,
    ReconciliationDecision,
    _hash,
    build_evidence_subject,
    build_observation,
    evidence_observation_sha256,
    evidence_subject_sha256,
    reconcile_subject,
)
from htdt.cad_evidence_reconciliation_repository import (
    CadEvidenceReconciliationRepository,
    ReconciliationIntegrityError,
)
from htdt.cad_evidence_register import ProjectEvidenceGapRegister
from htdt.cad_intervention_study import (
    InterventionFinding,
    build_intervention_study_spec,
)
from htdt.optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveValidDomain,
)
from htdt.cad_intervention_study_repository import (
    CadInterventionStudyRepository,
)
from htdt.cad_measurement_quality import measurement_sha256
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_empty_scene, make_f1_scene
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository

NOW = '2026-09-24T00:00:00+00:00'
LATER = '2026-09-24T01:00:00+00:00'
LATEST = '2026-09-24T02:00:00+00:00'
DOC = 'doc-1'


# -- shared seeds -------------------------------------------------------------


def _scene_repo(tmp_path: Path) -> SceneRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene(DOC), parent_revision_id=None)
    return scene_repository


def _comparison_authorities(scene_repository: SceneRepository):
    revision = scene_repository.latest(DOC)
    repository = CadDesignComparisonRepository(scene_repository)
    alt_a = build_alternative(
        label='案A', scene_revision=revision,
        created_at_utc=NOW, alternative_id='alt-a',
    )
    alt_b = build_alternative(
        label='案B', scene_revision=revision,
        created_at_utc=NOW, alternative_id='alt-b',
    )
    comparison_set = build_comparison_set(
        document_id=DOC,
        name='speaker layout options',
        alternatives=(alt_a, alt_b),
        created_at_utc=NOW,
        set_id='set-1',
    )
    repository.save_set(comparison_set)
    return comparison_set, (alt_a, alt_b)


def _alt_ref(alternative) -> DecisionAuthorityRef:
    return DecisionAuthorityRef(
        kind='comparison_alternative',
        ref_id=alternative.alternative_id,
        ref_sha256=alternative.alternative_sha256,
    )


def _decision(comparison_set, alternatives, **overrides):
    options = dict(
        document_id=DOC,
        title='5.1.4 採用',
        decision_scope='design_direction',
        lifecycle_intent='choose_for_design',
        selected_ref=_alt_ref(alternatives[1]),
        considered_refs=tuple(_alt_ref(a) for a in alternatives),
        comparison_set_ref=DecisionAuthorityRef(
            kind='design_comparison_set',
            ref_id=comparison_set.set_id,
            ref_sha256=comparison_set.set_sha256,
        ),
        created_at_utc=NOW,
        rationale_note='案B',
    )
    options.update(overrides)
    return build_decision_record(**options)


def _assumption(**overrides):
    options = dict(
        document_id=DOC,
        subject=AssumptionSubjectRef(kind='document', ref_id=DOC),
        attested_classification='missing_evidence',
        assumed_value='暫定',
        decision_scope='project',
        rationale='未入手',
        created_at_utc=NOW,
    )
    options.update(overrides)
    return build_assumption_decision(**options)


def _brief(**overrides):
    options = dict(
        document_id=DOC,
        title='Theater goals',
        goal_refs=(
            BriefGoalRef(
                goal_id='goal-note',
                kind='free_text',
                requirement='informational',
                label='Quiet HVAC preferred',
            ),
        ),
        created_at_utc=NOW,
    )
    options.update(overrides)
    return build_design_brief(**options)


def _insert_raw(db_path, table: str, record, columns: dict[str, object]) -> None:
    """Write a persisted row bypassing the repository's guards."""
    names = list(columns)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            f'INSERT INTO {table} ({", ".join(names)}) '
            f'VALUES ({", ".join("?" for _ in names)})',
            [columns[name] for name in names],
        )


# -- #867: measurement/inbox disposition eligibility ---------------------------


class _MeasurementRecord:
    def __init__(self, measurement_id: str, label: str = 'MLP') -> None:
        self.measurement_id = measurement_id
        self.label = label


class _MeasurementSourceStub:
    """Read adapter exposing the canonical #509 disposition lookup."""

    def __init__(self, measurements, datasets, dispositions=None) -> None:
        self._measurements = measurements
        self._datasets = datasets
        self._dispositions = dispositions or {}

    def list_measurements(self, document_id):
        return tuple(self._measurements)

    def dataset_for_measurement(self, measurement_id):
        return self._datasets.get(measurement_id)

    def latest_disposition(self, measurement_id):
        return self._dispositions.get(measurement_id)


class _InboxItemStub:
    def __init__(
        self, item_id: str, disposition: str, lineage: str = 'digest-1'
    ) -> None:
        self.inbox_item_id = item_id
        self.scope = DOC
        self.title = item_id
        self.disposition = disposition
        self.lineage_digest = lineage


class _InboxSourceStub:
    def __init__(self, items, inspection=None) -> None:
        self._items = items
        self._inspection = inspection

    def list_items(self):
        return tuple(self._items)

    def inspect(self, lineage_digest):
        return self._inspection


def _inbox_gaps(scene_repository, disposition, inspection=None):
    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        inbox_source=_InboxSourceStub(
            [_InboxItemStub('inbox-1', disposition)], inspection
        ),
    )
    return register.gaps(DOC, domain='measurement')


def test_ineligible_measurement_stays_missing_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    for state in (
        'test_only',
        'excluded_from_normal_use',
        'misassigned',
        'duplicate_import',
    ):
        register = ProjectEvidenceGapRegister(
            scene_source=scene_repository,
            measurement_source=_MeasurementSourceStub(
                [_MeasurementRecord('m-1')],
                {},
                {'m-1': SimpleNamespace(
                    disposition=state, correction_id=None,
                )},
            ),
        )
        gaps = register.gaps(DOC, domain='measurement')
        # Neither suppresses the project gap nor emits repair gaps.
        assert [gap.classification for gap in gaps] == [
            'missing_evidence'
        ], state


def test_active_and_unset_dispositions_remain_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    # No disposition row at all: legacy storage presence stays eligible.
    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        measurement_source=_MeasurementSourceStub(
            [_MeasurementRecord('m-1')], {},
        ),
    )
    gaps = register.gaps(DOC, domain='measurement')
    assert not any(
        gap.classification == 'missing_evidence' for gap in gaps
    )
    assert any(
        gap.classification == 'unresolved_dependency' for gap in gaps
    )

    # Explicit 'active' also stays eligible and drives repair gaps.
    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        measurement_source=_MeasurementSourceStub(
            [_MeasurementRecord('m-1')],
            {},
            {'m-1': SimpleNamespace(
                disposition='active', correction_id=None,
            )},
        ),
    )
    gaps = register.gaps(DOC, domain='measurement')
    assert not any(
        gap.classification == 'missing_evidence' for gap in gaps
    )
    assert any(
        gap.classification == 'unresolved_dependency' for gap in gaps
    )


def test_corrected_measurement_keeps_correction_evidence(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        measurement_source=_MeasurementSourceStub(
            [_MeasurementRecord('m-1')],
            {},
            {'m-1': SimpleNamespace(
                disposition='corrected', correction_id='corr-1',
            )},
        ),
    )
    gaps = register.gaps(DOC, domain='measurement')
    assert not any(
        gap.classification == 'missing_evidence' for gap in gaps
    )
    repair = next(
        gap for gap in gaps
        if gap.classification == 'unresolved_dependency'
    )
    # The acquisition binding stays the subject; the correction record is
    # named as evidence so caveats ride with the gap.
    assert repair.subject.ref_id == 'm-1'
    assert any(
        ref.kind == 'measurement_correction' and ref.ref_id == 'corr-1'
        for ref in repair.source_refs
    )


def test_inbox_settled_dispositions_emit_no_gaps(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    for disposition in ('promoted', 'rejected', 'superseded'):
        assert _inbox_gaps(scene_repository, disposition) == (), disposition


def test_inbox_pending_and_deferred_gaps(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    pending = _inbox_gaps(scene_repository, 'pending')
    assert [gap.classification for gap in pending] == [
        'unresolved_dependency'
    ]
    assert pending[0].subject.ref_id == 'inbox-1'

    deferred = _inbox_gaps(scene_repository, 'deferred')
    assert len(deferred) == 1
    # A documented park reads differently from fresh pending work.
    assert '保留' in deferred[0].summary


def test_inbox_partially_promoted_names_remaining_kinds(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    inspection = SimpleNamespace(
        available_authority_kinds=('rew_measurement', 'floor_plan'),
        promoted_authority_kinds=('floor_plan',),
        blocked_authority_kinds=(),
    )
    gaps = _inbox_gaps(scene_repository, 'partially_promoted', inspection)
    assert len(gaps) == 1
    assert gaps[0].classification == 'unresolved_dependency'
    assert 'rew_measurement' in gaps[0].limitation


# -- #868: design decision single-head lineage --------------------------------


def test_decision_supersession_is_single_head(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    comparison_set, alternatives = _comparison_authorities(scene_repository)
    repository = CadDesignDecisionRepository(scene_repository)

    first = _decision(comparison_set, alternatives)
    repository.save_decision(first)
    second = _decision(
        comparison_set, alternatives,
        supersedes=first, created_at_utc=LATER,
    )
    repository.save_decision(second)

    # A third writer cannot also supersede the stale head.
    competing = _decision(
        comparison_set, alternatives,
        supersedes=first, title='別決定', created_at_utc=LATEST,
    )
    with pytest.raises(DesignDecisionStaleHeadError):
        repository.save_decision(competing)

    # The losing writer re-aims at the current head and succeeds.
    third = _decision(
        comparison_set, alternatives,
        supersedes=second, title='別決定', created_at_utc=LATEST,
    )
    repository.save_decision(third)
    assert [d.decision_id for d in current_decisions(repository.list_decisions(DOC))] == [
        third.decision_id
    ]


def test_decision_independent_roots_coexist(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    comparison_set, alternatives = _comparison_authorities(scene_repository)
    repository = CadDesignDecisionRepository(scene_repository)
    repository.save_decision(_decision(comparison_set, alternatives))
    repository.save_decision(
        _decision(
            comparison_set, alternatives,
            title='設置案の妥協', decision_scope='installation_concession',
            created_at_utc=LATER,
        )
    )
    current = current_decisions(repository.list_decisions(DOC))
    assert len(current) == 2


def test_decision_persisted_fork_fails_closed(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    comparison_set, alternatives = _comparison_authorities(scene_repository)
    repository = CadDesignDecisionRepository(scene_repository)
    first = _decision(comparison_set, alternatives)
    repository.save_decision(first)
    repository.save_decision(
        _decision(
            comparison_set, alternatives,
            supersedes=first, created_at_utc=LATER,
        )
    )
    # A fork written outside the repository (restore/import corruption).
    fork = _decision(
        comparison_set, alternatives,
        supersedes=first, title='分岐', created_at_utc=LATEST,
    )
    _insert_raw(
        repository.path,
        'design_decisions',
        fork,
        {
            'decision_id': fork.decision_id,
            'document_id': fork.document_id,
            'decision_scope': fork.decision_scope,
            'selected_ref_id': fork.selected_ref.ref_id,
            'supersedes_decision_id': fork.supersedes_decision_id,
            'created_at_utc': fork.created_at_utc,
            'decision_sha256': fork.decision_sha256,
            'payload_json': json.dumps(
                fork.model_dump(mode='json'), ensure_ascii=False
            ),
        },
    )
    with pytest.raises(DesignDecisionIntegrityError):
        repository.list_decisions(DOC)
    with pytest.raises(DesignDecisionIntegrityError):
        current_decisions(repository.list_decisions(DOC))


def test_decision_row_payload_drift_fails_closed(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    comparison_set, alternatives = _comparison_authorities(scene_repository)
    repository = CadDesignDecisionRepository(scene_repository)
    record = _decision(comparison_set, alternatives)
    repository.save_decision(record)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE design_decisions SET decision_scope=? WHERE decision_id=?',
            ('installation_concession', record.decision_id),
        )
    with pytest.raises(DesignDecisionIntegrityError):
        repository.get_decision(record.decision_id)


def test_decision_lineage_issues_detect_cycles() -> None:
    """A supersession cycle is detected on in-memory topology too."""
    assert decision_lineage_issues(()) == ()
    a = _decision_lineage_node('a', supersedes='b')
    b = _decision_lineage_node('b', supersedes='a')
    assert decision_lineage_issues((a, b))
    head = _decision_lineage_node('c')
    assert decision_lineage_issues((a, b, head))


def _decision_lineage_node(decision_id: str, supersedes: str | None = None):
    """Minimal record shape for pure-topology lineage checks."""
    from htdt.cad_design_decision import DesignDecisionRecord

    return DesignDecisionRecord.model_construct(
        decision_id=decision_id,
        document_id=DOC,
        supersedes_decision_id=supersedes,
    )


# -- #869: assumption decision lifecycle ---------------------------------------


def test_assumption_supersession_same_subject_single_head(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    repository = CadAssumptionDecisionRepository(scene_repository)
    first = _assumption()
    repository.save_decision(first)
    second = _assumption(
        supersedes=first, assumed_value='実機判明', created_at_utc=LATER
    )
    repository.save_decision(second)

    stale = _assumption(
        supersedes=first, assumed_value='競合', created_at_utc=LATEST
    )
    with pytest.raises(AssumptionDecisionStaleHeadError):
        repository.save_decision(stale)
    third = _assumption(
        supersedes=second, assumed_value='確定', created_at_utc=LATEST
    )
    repository.save_decision(third)
    assert [
        d.decision_id
        for d in active_assumption_decisions(repository.list_decisions(DOC))
    ] == [third.decision_id]


def test_assumption_cross_subject_supersession_rejected() -> None:
    other_subject = _assumption(
        subject=AssumptionSubjectRef(kind='document', ref_id='doc-2')
    )
    with pytest.raises(ValueError, match='same subject'):
        _assumption(supersedes=other_subject)
    other_doc = _assumption(document_id='doc-2')
    with pytest.raises(ValueError, match='another document'):
        _assumption(supersedes=other_doc)


def test_assumption_timestamps_strict_and_ordered(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _assumption(created_at_utc='2026-09-24T00:00:00')  # naive
    with pytest.raises(ValidationError):
        _assumption(created_at_utc='not-a-date')
    with pytest.raises(ValidationError):
        _assumption(expires_at_utc='2026-09-24T00:00:00+09:00')  # non-UTC
    with pytest.raises(ValidationError, match='before created_at_utc'):
        _assumption(
            created_at_utc=NOW, expires_at_utc='2026-01-01T00:00:00+00:00'
        )

    scene_repository = _scene_repo(tmp_path)
    repository = CadAssumptionDecisionRepository(scene_repository)
    first = _assumption(created_at_utc=NOW)
    repository.save_decision(first)
    predated = _assumption(
        supersedes=first, created_at_utc='2020-01-01T00:00:00+00:00'
    )
    with pytest.raises(ValueError, match='predates'):
        repository.save_decision(predated)


def test_assumption_independent_roots_and_corrupt_row(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    repository = CadAssumptionDecisionRepository(scene_repository)
    # Two independent roots on the same subject coexist.
    repository.save_decision(_assumption())
    repository.save_decision(_assumption(assumed_value='別仮定'))
    assert len(repository.list_decisions(DOC)) == 2

    # Corrupt a row's denormalized column -> fails closed, never attests.
    record = repository.list_decisions(DOC)[0]
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE assumption_decisions SET subject_ref_id=? '
            'WHERE decision_id=?',
            ('doc-tampered', record.decision_id),
        )
    with pytest.raises(AssumptionDecisionIntegrityError):
        repository.list_decisions(DOC)

    # The register surfaces an explicit integrity gap instead of crashing.
    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        assumption_source=repository,
    )
    gaps = register.gaps(DOC)
    integrity = [
        gap for gap in gaps
        if gap.classification == 'unverified' and gap.domain == 'commissioning'
    ]
    assert len(integrity) == 1
    # The corrupt attestation was withheld, not applied.
    assert all(
        gap.classification != 'user_attested' for gap in gaps
    )


# -- #870: design brief explicit head ------------------------------------------


def test_brief_supersession_single_head_and_head_semantics(
    tmp_path: Path,
) -> None:
    scene_repository = _scene_repo(tmp_path)
    repository = CadDesignBriefRepository(scene_repository)
    first = _brief()
    repository.save_brief(first)
    second = revise_design_brief(first, title='v2', created_at_utc=LATER)
    repository.save_brief(second)

    competing = revise_design_brief(first, title='v3', created_at_utc=LATEST)
    with pytest.raises(DesignBriefStaleHeadError):
        repository.save_brief(competing)
    third = revise_design_brief(second, title='v3', created_at_utc=LATEST)
    repository.save_brief(third)
    assert repository.latest_brief(DOC) == third
    # The old head stays inspectable.
    assert repository.get_brief(first.brief_id) == first


def test_brief_rejects_malformed_and_predated_timestamps(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        _brief(created_at_utc='2026-09-24T00:00:00')  # naive
    with pytest.raises(ValidationError):
        _brief(created_at_utc='not-a-date')

    scene_repository = _scene_repo(tmp_path)
    repository = CadDesignBriefRepository(scene_repository)
    first = _brief(created_at_utc=LATER)
    repository.save_brief(first)
    predated = revise_design_brief(
        first, created_at_utc='2020-01-01T00:00:00+00:00'
    )
    with pytest.raises(ValueError, match='predates'):
        repository.save_brief(predated)


def test_brief_fork_never_silently_replaces_head(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repository = CadDesignBriefRepository(scene_repository)
    first = _brief()
    repository.save_brief(first)
    repository.save_brief(
        revise_design_brief(first, title='v2', created_at_utc=LATER)
    )
    # An independent later root is a topology fork: reads fail closed
    # rather than silently crowning the newest timestamp.
    repository.save_brief(_brief(title='unrelated', created_at_utc=LATEST))
    with pytest.raises(DesignBriefIntegrityError):
        repository.latest_brief(DOC)
    with pytest.raises(DesignBriefIntegrityError):
        current_brief(repository.list_briefs(DOC))


def test_brief_row_payload_drift_fails_closed(tmp_path: Path) -> None:
    scene_repository = _scene_repo(tmp_path)
    repository = CadDesignBriefRepository(scene_repository)
    brief = _brief()
    repository.save_brief(brief)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_design_briefs SET created_at_utc=? WHERE brief_id=?',
            ('1970-01-01T00:00:00+00:00', brief.brief_id),
        )
    with pytest.raises(DesignBriefIntegrityError):
        repository.get_brief(brief.brief_id)


# -- #871: canonical resolver kind routing -------------------------------------


def _study_seed(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=saved.revision,
        name='study variant',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return scene_repository, saved, variant


def test_resolver_routes_colliding_study_ids_to_own_family(
    tmp_path: Path,
) -> None:
    scene_repository, saved, variant = _study_seed(tmp_path)
    spec = build_intervention_study_spec(
        scene_revision=saved.revision,
        base_variant=variant,
        finding=InterventionFinding(
            source_kind='explicit_user_region',
            observable='target_response_error',
            target_band_hz=(55.0, 75.0),
            target_seat_entity_ids=('seat-1',),
            detail='elevated magnitude at MLP',
        ),
        allowed_families=('geometry',),
        metric_definitions=(
            ObjectiveDefinition(
                objective_id='obj-1',
                quantity='response_error',
                unit='dB',
                direction='minimize',
                valid_domain=ObjectiveValidDomain(
                    kind='bounded_real', minimum=0.0
                ),
                comparison_model_id='test-model',
                comparison_model_version='1',
            ),
        ),
        fidelity_label='native-model-v1',
        provider_id='room-simulation',
        provider_version='1.0.0',
        algorithm_version='1',
        candidate_budget=16,
        created_at_utc=NOW,
    )
    CadInterventionStudyRepository(
        scene_repository=scene_repository
    ).save_spec(spec)
    # Deliberate ID collision across families.
    study = build_analysis_study(
        document_id=saved.revision.document_id,
        title='colliding id',
        created_at_utc=NOW,
        study_id=spec.spec_id,
    )
    CadAnalysisStudyRepository(scene_repository).save_study(study)

    resolver = CanonicalAuthorityRefResolver(scene_repository)
    doc_id = saved.revision.document_id
    resolved_study = resolver.resolve(
        'analysis_study', spec.spec_id, doc_id
    )
    resolved_spec = resolver.resolve(
        'intervention_study_spec', spec.spec_id, doc_id
    )
    assert resolved_study is not None
    assert resolved_spec is not None
    assert resolved_study.semantic_sha256 == study.study_sha256
    assert resolved_spec.semantic_sha256 == spec.spec_sha256


def test_resolver_measurement_pins_semantic_hash(tmp_path: Path) -> None:
    scene_repository, saved, _variant = _study_seed(tmp_path)
    measurement_repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, raw = normalize_rew_text(
        saved.revision,
        'point-mlp',
        b'20 70\n40 71\n80 69\n',
        filename='mlp.txt',
        imported_at='2026-09-17T09:30:00+00:00',
    )
    measurement_repository.save(
        record, dataset, raw_filename=filename, raw_bytes=raw
    )
    resolver = CanonicalAuthorityRefResolver(scene_repository)
    doc_id = saved.revision.document_id
    resolved = resolver.resolve(
        'measurement', record.measurement_id, doc_id
    )
    assert resolved is not None
    assert resolved.document_id == doc_id
    assert resolved.semantic_sha256 == measurement_sha256(record)
    # Unknown and foreign measurements fail closed.
    assert resolver.resolve('measurement', 'ghost', doc_id) is None


def test_every_known_kind_has_one_owner_contract() -> None:
    """Every declared KNOWN_KIND resolves through exactly one family (#871).

    Kinds are unique type tags — no two semantic families share a kind
    string, and the resolver must never return a payload of the wrong
    family for a kind.
    """
    resolver = CanonicalAuthorityRefResolver(_NullScene())
    assert len(resolver.KNOWN_KINDS) == len(set(resolver.KNOWN_KINDS))
    assert 'intervention_study_spec' in resolver.KNOWN_KINDS
    assert 'intervention_alternative' in resolver.KNOWN_KINDS
    # The two study families use distinct kinds; no generic 'study' tag.
    assert 'study' not in resolver.KNOWN_KINDS
    for kind in resolver.KNOWN_KINDS:
        assert resolver.knows(kind)


# -- #873: reconciliation input pins -------------------------------------------


def _recon_seed(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    )

    def _external(kind: str, sha: str):
        def resolve(ref_id: str):
            return ResolvedAuthority(
                kind=kind,
                ref_id=ref_id,
                document_id=saved.revision.document_id,
                semantic_sha256=sha,
            )

        return resolve

    repository = CadEvidenceReconciliationRepository(
        scene_repository,
        kind_resolvers={
            'capture': _external('capture', 'a' * 64),
            'floor_plan': _external('floor_plan', 'b' * 64),
            # Manual dimensions carry operator provenance — ID-bearing
            # only, never a hash.
            'manual_dimension': lambda ref_id: ResolvedAuthority(
                kind='manual_dimension',
                ref_id=ref_id,
                document_id=saved.revision.document_id,
                semantic_sha256=None,
            ),
        },
    )
    return repository, saved


def _recon_subject(saved, document_id: str = F1_DOCUMENT_ID):
    return build_evidence_subject(
        document_id=document_id,
        subject_kind='dimension',
        target=AuthorityRef(
            kind='scene_revision',
            ref_id=saved.revision.revision_id,
            ref_sha256=saved.revision.content_hash,
        ),
        attribute='room_width_m',
        description='Room width at seat row',
    )


def test_subject_and_observation_have_semantic_identity(
    tmp_path: Path,
) -> None:
    repository, saved = _recon_seed(tmp_path)
    subject = _recon_subject(saved)
    assert subject.subject_sha256 == evidence_subject_sha256(subject)
    repository.save_subject(subject)
    observation = build_observation(
        subject_id=subject.subject_id,
        source='manual_dimension',
        source_ref='tape-1',
        value=4.5,
        unit='m',
        alignment=AlignmentRef(frame_kind='manual_frame', frame_id='d'),
        captured_at_utc=NOW,
    )
    assert observation.observation_sha256 == evidence_observation_sha256(
        observation
    )
    repository.save_observation(observation)

    # Rows must carry the stored semantic identity once written.
    loaded_subject = repository.get_subject(subject.subject_id)
    assert loaded_subject.subject_sha256 == subject.subject_sha256
    loaded = repository.get_observation(observation.observation_id)
    assert loaded.observation_sha256 == observation.observation_sha256


def test_unpinned_new_records_are_rejected(tmp_path: Path) -> None:
    repository, saved = _recon_seed(tmp_path)
    subject = _recon_subject(saved)
    legacy_subject = subject.model_copy(update={'subject_sha256': None})
    with pytest.raises(ReconciliationIntegrityError):
        repository.save_subject(legacy_subject)

    repository.save_subject(subject)
    observation = build_observation(
        subject_id=subject.subject_id,
        source='manual_dimension',
        source_ref='tape-1',
        value=4.5,
        unit='m',
        alignment=AlignmentRef(frame_kind='manual_frame', frame_id='d'),
    )
    legacy_observation = observation.model_copy(
        update={'observation_sha256': None}
    )
    with pytest.raises(ReconciliationIntegrityError):
        repository.save_observation(legacy_observation)


def test_decision_pins_inputs_and_tamper_fails_read(tmp_path: Path) -> None:
    repository, saved = _recon_seed(tmp_path)
    subject = _recon_subject(saved)
    repository.save_subject(subject)
    frame = AlignmentRef(
        frame_kind='capture_frame',
        frame_id='capture-A',
        frame_sha256='f' * 64,
    )
    observations = (
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            source_ref='tape-1',
            value=4.50,
            unit='m',
            uncertainty=0.01,
            alignment=frame,
            captured_at_utc=NOW,
        ),
        build_observation(
            subject_id=subject.subject_id,
            source='manual_dimension',
            source_ref='tape-2',
            value=4.51,
            unit='m',
            uncertainty=0.02,
            alignment=frame,
            captured_at_utc=LATER,
        ),
    )
    for observation in observations:
        repository.save_observation(observation)
    decision = reconcile_subject(
        subject,
        observations,
        tolerance=0.02,
        tolerance_unit='m',
        decided_at_utc=NOW,
    )
    assert decision.pins_exact_inputs
    assert decision.subject_sha256 == subject.subject_sha256
    assert tuple(
        r.observation_sha256 for r in decision.observation_refs
    ) == tuple(
        o.observation_sha256
        for o in sorted(observations, key=lambda o: o.observation_id)
    )
    repository.save_decision(decision)
    assert repository.get_decision(decision.decision_id) == decision

    # Tamper with an observation under the same id -> pinned read fails.
    drifted = observations[0].model_copy(update={'value': 9.99})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_evidence_observations SET payload_json=? '
            'WHERE observation_id=?',
            (drifted.model_dump_json(), observations[0].observation_id),
        )
    with pytest.raises(ReconciliationIntegrityError):
        repository.get_decision(decision.decision_id)


def test_decision_rejects_malformed_decided_at(tmp_path: Path) -> None:
    _repository, saved = _recon_seed(tmp_path)
    subject = _recon_subject(saved)
    with pytest.raises(ValidationError):
        reconcile_subject(subject, (), decided_at_utc='not-a-date')
    with pytest.raises(ValidationError):
        reconcile_subject(subject, (), decided_at_utc='2026-09-24T00:00:00')


def test_legacy_id_only_decision_is_classified_legacy(
    tmp_path: Path,
) -> None:
    """A pre-#873 decision without pins is readable but flagged non-exact."""
    repository, saved = _recon_seed(tmp_path)
    subject = _recon_subject(saved)
    repository.save_subject(subject)
    decision = reconcile_subject(subject, (), decided_at_utc=NOW)
    data = decision.model_dump(mode='json')
    del data['subject_sha256'], data['observation_refs'], data['decision_sha256']
    provisional = ReconciliationDecision.model_construct(
        **data, decision_sha256='0' * 64
    )
    legacy = ReconciliationDecision(
        **data,
        decision_sha256=_hash(provisional.semantic_payload()),
    )
    # Bypass save-time pin enforcement by writing the legacy row directly.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'INSERT INTO cad_reconciliation_decisions ('
            'decision_id, subject_id, document_id, outcome, '
            'decision_sha256, decided_at_utc, payload_json'
            ') VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                legacy.decision_id,
                legacy.subject_id,
                legacy.document_id,
                legacy.outcome,
                legacy.decision_sha256,
                legacy.decided_at_utc,
                legacy.model_dump_json(),
            ),
        )
    loaded = repository.get_decision(legacy.decision_id)
    assert loaded is not None
    assert not loaded.pins_exact_inputs
    assert repository.latest_decision(subject.subject_id) is not None


# -- helpers -------------------------------------------------------------------


class _NullScene:
    """SceneRepository stand-in for pure-registry tests (never touched)."""

    path = ':memory:'
