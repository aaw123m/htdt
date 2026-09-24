from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_assumption_decision import (
    AssumptionDecision,
    AssumptionSubjectRef,
    active_assumption_decisions,
    build_assumption_decision,
)
from htdt.cad_assumption_decision_repository import (
    AssumptionDecisionConflictError,
    CadAssumptionDecisionRepository,
)
from htdt.cad_evidence_register import (
    ProjectEvidenceGapRegister,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_empty_scene,
)

NOW = '2026-09-24T00:00:00+00:00'


def _scene(document_id: str = 'doc-1', speaker_role: str | None = 'FL') -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='spk-1',
                kind='speaker',
                name='FL',
                speaker_role=speaker_role,
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _repository(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(_scene(), parent_revision_id=None)
    decisions = CadAssumptionDecisionRepository(scene_repository)
    return scene_repository, decisions


def _decision(
    *,
    document_id: str = 'doc-1',
    subject_kind: str = 'scene_entity',
    subject_ref_id: str = 'spk-1',
    attested: str = 'missing_evidence',
    **overrides,
) -> AssumptionDecision:
    options = dict(
        document_id=document_id,
        subject=AssumptionSubjectRef(kind=subject_kind, ref_id=subject_ref_id),
        attested_classification=attested,
        assumed_value='汎用2wayスピーカーとして扱う',
        decision_scope='project',
        rationale='実機が未入手のため暫定',
        created_at_utc=NOW,
    )
    options.update(overrides)
    return build_assumption_decision(**options)


# -- AssumptionDecision ----------------------------------------------------


def test_assumption_decision_hash_and_payload() -> None:
    decision = _decision()
    assert len(decision.decision_sha256) == 64
    assert decision.attested_classification == 'missing_evidence'
    assert decision.decision_scope == 'project'


def test_assumption_rejects_non_attestable_classification() -> None:
    with pytest.raises(ValidationError, match='attestable'):
        _decision(attested='unsupported')


def test_custom_scope_requires_label() -> None:
    with pytest.raises(ValidationError, match='custom_scope_label'):
        _decision(decision_scope='custom')
    decision = _decision(decision_scope='custom', custom_scope_label='設置前')
    assert decision.custom_scope_label == '設置前'


def test_supersede_requires_same_subject_and_document() -> None:
    first = _decision()
    other_subject = _decision(subject_kind='document', subject_ref_id='doc-1')
    with pytest.raises(ValueError, match='same subject'):
        _decision(supersedes=other_subject)
    other_doc = _decision(document_id='doc-2')
    with pytest.raises(ValueError, match='another document'):
        _decision(supersedes=other_doc)
    second = _decision(supersedes=first, assumed_value='実機判明: 3way')
    active = active_assumption_decisions((first, second))
    assert [item.decision_id for item in active] == [second.decision_id]


def test_expired_decisions_drop_out() -> None:
    stale = _decision(expires_at_utc='2026-01-01T00:00:00+00:00')
    live = _decision()
    active = active_assumption_decisions(
        (stale, live), as_of_utc='2026-06-01T00:00:00+00:00'
    )
    assert [item.decision_id for item in active] == [live.decision_id]


def test_assumption_repository_round_trip(tmp_path: Path) -> None:
    _scenes, repository = _repository(tmp_path)
    decision = _decision()
    repository.save_decision(decision)
    repository.save_decision(decision)
    assert repository.get_decision(decision.decision_id) == decision
    conflict = _decision(assumed_value='別の仮定', decision_id=decision.decision_id)
    with pytest.raises(AssumptionDecisionConflictError):
        repository.save_decision(conflict)


# -- Register ---------------------------------------------------------------


def test_register_no_document(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    register = ProjectEvidenceGapRegister(scene_source=scene_repository)
    gaps = register.gaps('doc-1')
    assert len(gaps) == 1
    assert gaps[0].classification == 'missing_evidence'
    assert gaps[0].domain == 'room'
    assert 'prediction' in gaps[0].affected_capabilities
    assert gaps[0].resolutions[0].deep_link is not None


def test_register_scene_gaps_derived_deterministically(tmp_path: Path) -> None:
    scene_repository, _ = _repository(tmp_path)
    register = ProjectEvidenceGapRegister(scene_source=scene_repository)
    first = register.gaps('doc-1')
    second = register.gaps('doc-1')
    assert [item.gap_id for item in first] == [item.gap_id for item in second]
    subjects = {(item.domain, item.classification) for item in first}
    # floor + ceiling assumed assemblies, speaker role ok, no binding source.
    assert ('room', 'assumed') in subjects
    # unbound speaker with no equipment source -> no equipment gap registered
    assert ('equipment', 'missing_evidence') not in subjects


def test_register_filters(tmp_path: Path) -> None:
    scene_repository, _ = _repository(tmp_path)
    register = ProjectEvidenceGapRegister(scene_source=scene_repository)
    room_gaps = register.gaps('doc-1', domain='room')
    assert all(item.domain == 'room' for item in room_gaps)
    assumed = register.gaps('doc-1', classification='assumed')
    assert all(item.classification == 'assumed' for item in assumed)
    floor = register.gaps('doc-1', subject_ref_id='floor:floor')
    assert all(
        item.subject is not None and item.subject.ref_id == 'floor:floor'
        for item in floor
    )


def test_register_attestation_reclassifies_but_keeps_gap(tmp_path: Path) -> None:
    scene_repository, decisions = _repository(tmp_path)
    decision = _decision(
        subject_kind='room_surface',
        subject_ref_id='floor:floor',
        attested='assumed',
        assumed_value='コンクリート床として扱う',
    )
    decisions.save_decision(decision)
    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        assumption_source=decisions,
    )
    gaps = register.gaps('doc-1', subject_ref_id='floor:floor')
    assert len(gaps) == 1
    assert gaps[0].classification == 'user_attested'
    assert gaps[0].assumption_decision_ids == (decision.decision_id,)
    # The limitation stays attached — attestation never fabricates evidence.
    assert gaps[0].limitation is not None


def test_register_unassigned_role_is_unknown(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        _scene(speaker_role='UNASSIGNED-1'), parent_revision_id=None
    )
    register = ProjectEvidenceGapRegister(scene_source=scene_repository)
    gaps = register.gaps('doc-1', domain='equipment')
    assert any(
        item.classification == 'unknown' and item.subject.ref_id == 'spk-1'
        for item in gaps
    )


def test_register_equipment_binding_missing_and_inferred(tmp_path: Path) -> None:
    scene_repository, _ = _repository(tmp_path)

    class _EquipmentSource:
        def __init__(self, binding):
            self._binding = binding

        def get_binding_for_entity(self, document_id, entity_id):
            return self._binding if entity_id == 'spk-1' else None

    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        equipment_source=_EquipmentSource(None),
    )
    gaps = register.gaps('doc-1', domain='equipment')
    assert any(item.classification == 'missing_evidence' for item in gaps)

    class _Provenance:
        evidence_kind = 'inferred'

    class _Binding:
        provenance = (_Provenance(),)

    inferred = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        equipment_source=_EquipmentSource(_Binding()),
    )
    gaps = inferred.gaps('doc-1', domain='equipment')
    assert any(item.classification == 'inferred' for item in gaps)


def test_register_prediction_states(tmp_path: Path) -> None:
    scene_repository, _ = _repository(tmp_path)

    class _PredictionSource:
        def __init__(self, results):
            self._results = results

        def list_results(self, document_id):
            return self._results

    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        prediction_source=_PredictionSource(()),
    )
    gaps = register.gaps('doc-1', domain='prediction')
    assert any(item.classification == 'unknown' for item in gaps)

    class _Result:
        status = 'completed'
        scene_revision_id = 'other'
        scene_content_hash = 'h'
        geometry_compatibility = None

    stale = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        prediction_source=_PredictionSource((_Result(),)),
    )
    gaps = stale.gaps('doc-1', domain='prediction')
    assert any(item.classification == 'stale' for item in gaps)


def test_register_measurement_states(tmp_path: Path) -> None:
    scene_repository, _ = _repository(tmp_path)

    class _MeasurementSource:
        def __init__(self, items, datasets):
            self._items = items
            self._datasets = datasets

        def list_measurements(self, document_id):
            return self._items

        def dataset_for_measurement(self, measurement_id):
            return self._datasets.get(measurement_id)

    register = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        measurement_source=_MeasurementSource((), {}),
    )
    gaps = register.gaps('doc-1', domain='measurement')
    assert any(item.classification == 'missing_evidence' for item in gaps)

    class _Measurement:
        measurement_id = 'm-1'
        label = 'MLP'

    missing_dataset = ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        measurement_source=_MeasurementSource((_Measurement(),), {}),
    )
    gaps = missing_dataset.gaps('doc-1', domain='measurement')
    assert any(
        item.classification == 'unresolved_dependency' for item in gaps
    )


def test_gap_id_rejects_tampering(tmp_path: Path) -> None:
    scene_repository, _ = _repository(tmp_path)
    register = ProjectEvidenceGapRegister(scene_source=scene_repository)
    gap = register.gaps('doc-1')[0]
    with pytest.raises(ValidationError, match='id mismatch'):
        type(gap)(**{**gap.model_dump(mode='json'), 'gap_id': 'gap:bad'})


# -- #798: scope, expiry, annotation, inbox scoping, ref resolution ----------

from htdt.cad_evidence_register import EvidenceGapContext


def _register(scene_repository, decisions=None, inbox=None):
    return ProjectEvidenceGapRegister(
        scene_source=scene_repository,
        assumption_source=decisions,
        inbox_source=inbox,
    )


class _InboxSource:
    def __init__(self, items):
        self._items = items

    def list_items(self, scope=None, disposition=None):
        return self._items


class _InboxItem:
    def __init__(self, inbox_item_id, scope):
        self.inbox_item_id = inbox_item_id
        self.scope = scope
        self.title = inbox_item_id


def _floor_gap_subject():
    return dict(subject_kind='room_surface', subject_ref_id='floor:floor')


def test_checkpoint_scoped_assumption_needs_context(tmp_path: Path) -> None:
    """A checkpoint-scoped assumption annotates only inside that context."""
    from htdt.cad_design_checkpoint import build_design_checkpoint
    from htdt.cad_design_checkpoint_repository import (
        CadDesignCheckpointRepository,
    )

    scene_repository, decisions = _repository(tmp_path)
    revision = scene_repository.latest('doc-1')
    checkpoint = build_design_checkpoint(
        document_id='doc-1',
        title='layout freeze',
        scene_revision=revision,
        created_at_utc=NOW,
        checkpoint_id='cp-1',
    )
    CadDesignCheckpointRepository(scene_repository).save_checkpoint(checkpoint)
    scope_ref = AssumptionSubjectRef(
        kind='design_checkpoint',
        ref_id='cp-1',
        ref_sha256=checkpoint.checkpoint_sha256,
    )
    decision = _decision(
        **_floor_gap_subject(),
        attested='assumed',
        decision_scope='design_checkpoint',
        scope_ref=scope_ref,
    )
    decisions.save_decision(decision)
    register = _register(scene_repository, decisions)

    project_gap = register.gaps('doc-1', subject_ref_id='floor:floor')[0]
    assert project_gap.classification == 'assumed'
    assert project_gap.assumption_decision_ids == ()

    scoped_gap = register.gaps(
        'doc-1',
        context=EvidenceGapContext(design_checkpoint_id='cp-1'),
        subject_ref_id='floor:floor',
    )[0]
    assert scoped_gap.classification == 'user_attested'
    assert scoped_gap.assumption_decision_ids == (decision.decision_id,)
    # Annotation keeps the underlying classification + scope visible.
    assert scoped_gap.underlying_classification == 'assumed'
    assert 'design_checkpoint' in (scoped_gap.attestation_detail or '')

    other_checkpoint = register.gaps(
        'doc-1',
        context=EvidenceGapContext(design_checkpoint_id='cp-other'),
        subject_ref_id='floor:floor',
    )[0]
    assert other_checkpoint.classification == 'assumed'


def test_expired_assumption_stops_applying(tmp_path: Path) -> None:
    """Expiry is evaluated at the context instant, not save time."""
    scene_repository, decisions = _repository(tmp_path)
    decision = _decision(
        **_floor_gap_subject(),
        attested='assumed',
        expires_at_utc='2026-01-01T00:00:00+00:00',
    )
    decisions.save_decision(decision)
    register = _register(scene_repository, decisions)

    current = register.gaps('doc-1', subject_ref_id='floor:floor')[0]
    assert current.classification == 'assumed'

    historical = register.gaps(
        'doc-1',
        context=EvidenceGapContext(as_of_utc='2025-01-01T00:00:00+00:00'),
        subject_ref_id='floor:floor',
    )[0]
    assert historical.classification == 'user_attested'


def test_attestation_annotation_keeps_underlying(tmp_path: Path) -> None:
    scene_repository, decisions = _repository(tmp_path)
    decisions.save_decision(
        _decision(
            **_floor_gap_subject(),
            attested='assumed',
            assumed_value='コンクリート床として扱う',
        )
    )
    register = _register(scene_repository, decisions)
    gap = register.gaps('doc-1', subject_ref_id='floor:floor')[0]
    assert gap.classification == 'user_attested'
    assert gap.underlying_classification == 'assumed'
    assert gap.attestation_detail == 'project'


def test_inbox_items_scoped_to_document(tmp_path: Path) -> None:
    """Unassigned/foreign-scoped inbox items never leak into a project."""
    scene_repository, _ = _repository(tmp_path)
    register = _register(
        scene_repository,
        inbox=_InboxSource(
            [
                _InboxItem('inbox-1', 'capture-inbox-unassigned'),
                _InboxItem('inbox-2', 'doc-other'),
                _InboxItem('inbox-3', 'doc-1'),
            ]
        ),
    )
    subjects = {
        item.subject.ref_id
        for item in register.gaps('doc-1')
        if item.subject is not None
        and item.subject.kind == 'capture_inbox_item'
    }
    assert subjects == {'inbox-3'}


def test_assumption_repo_resolves_subject_refs(tmp_path: Path) -> None:
    """#798 C: refs whose kind has a canonical resolver must exist."""
    from htdt.cad_assumption_decision_repository import (
        AssumptionDecisionRefError,
    )

    scene_repository, decisions = _repository(tmp_path)
    # scene_entity 'spk-ghost' does not exist in doc-1 -> rejected.
    with pytest.raises(AssumptionDecisionRefError, match='unknown'):
        decisions.save_decision(
            _decision(subject_kind='scene_entity', subject_ref_id='spk-ghost')
        )
    # scene_entity 'spk-1' exists in doc-1's head -> accepted.
    decisions.save_decision(_decision())
    # Derived kinds (no canonical resolver) pass through unchanged.
    decisions.save_decision(
        _decision(subject_kind='room_surface', subject_ref_id='floor:floor')
    )
