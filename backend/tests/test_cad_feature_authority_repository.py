"""#886: the feature-authority batch persists append-only and resolves
through the canonical authority registry.

Covers the six feature groups — acoustic performance targets, sound
isolation, rack/BOM, drawing sets and field labels — end to end: save,
reopen through the model's own validators (tampered rows fail closed),
idempotent re-save, collision rejection, document scoping, and
``CanonicalAuthorityRegistry`` resolution.
"""

from __future__ import annotations

from hashlib import sha256

import pytest

from htdt.cad_acoustic_target import (
    AcousticTargetBand,
    AcousticTargetCriterion,
    build_acoustic_target_profile,
)
from htdt.cad_authority_registry import build_canonical_authority_registry
from htdt.cad_drawing_set import InstallationDrawingSet, build_drawing_set_spec
from htdt.cad_equipment import EquipmentDataProvenance, FrequencyDomain
from htdt.cad_feature_authority_repository import (
    CadFeatureAuthorityRepository,
    FeatureAuthorityConflictError,
)
from htdt.cad_field_labels import (
    generate_label_sheet,
    mint_label,
    mint_reissued_label_for_same_target,
)
from htdt.cad_project_bom import (
    BOMLineItem,
    DesignAuthorityRef,
    build_project_bom,
)
from htdt.cad_rack_infrastructure import (
    RackLayout,
    RackPlacement,
    build_rack_definition,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.cad_sound_isolation import (
    IsolationMeasurement,
    IsolationPath,
    IsolationScenario,
    TransmissionLossBand,
    build_isolation_assembly,
    estimate_isolation,
)
from htdt.cad_standards import CriterionRule, CriterionSource


NOW = '2026-09-24T00:00:00+00:00'


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _repo(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision, CadFeatureAuthorityRepository(
        scene_repository
    )


def _prov() -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name='Fixture Lab',
        source_version='1',
        source_reference='report-1',
        source_sha256='a' * 64,
    )


def _target_profile(document_id: str, version: str = 'v1'):
    criterion = AcousticTargetCriterion(
        criterion_id='t30-main',
        name='T30',
        metric='decay_t30',
        metric_version='iso3382-2:2008-method',
        origin='standard',
        source=CriterionSource(
            publisher='Fixture publisher',
            document_title='Fixture acoustic guidance',
            document_version='1.0',
            reference='Fixture §2',
        ),
        bands=(
            AcousticTargetBand(
                band_id='125-250',
                frequency=FrequencyDomain(
                    minimum_hz=125.0, maximum_hz=250.0
                ),
            ),
        ),
        rule=CriterionRule(operator='range', minimum=0.2, maximum=0.5),
        unit='s',
        aggregation='spatial_mean',
        population_entity_ids=('seat-1',),
        required_capability='predicted_t30',
    )
    return build_acoustic_target_profile(
        profile_id='target-main',
        profile_version=version,
        name='Main acoustic target',
        document_id=document_id,
        criteria=(criterion,),
        created_at_utc=NOW,
    )


def _assembly():
    return build_isolation_assembly(
        assembly_id='asm-wall',
        name='Double stud wall',
        evidence_tier='lab_tl_spectrum',
        provenance=(_prov(),),
        tl_bands=(
            TransmissionLossBand(
                band_id='125',
                frequency=FrequencyDomain(
                    minimum_hz=100.0, maximum_hz=160.0
                ),
                tl_db=30.0,
            ),
        ),
        valid_frequency=FrequencyDomain(minimum_hz=100.0, maximum_hz=160.0),
    )


def _scenario() -> IsolationScenario:
    return IsolationScenario(
        scenario_id='sc-1',
        source_region_id='theater',
        receiving_region_id='bedroom',
        paths=(
            IsolationPath(
                path_id='p-wall',
                kind='partition',
                source_region_id='theater',
                receiving_region_id='bedroom',
                assembly_id='asm-wall',
                area_m2=8.0,
            ),
        ),
        evaluation_bands=(
            FrequencyDomain(minimum_hz=100.0, maximum_hz=160.0),
        ),
    )


def _rack():
    return build_rack_definition(
        rack_id='rack-1',
        name='Main rack',
        width_m=0.6,
        height_m=1.2,
        usable_depth_m=0.55,
        ru_capacity=12,
        provenance=(_prov(),),
    )


def _bom(document_id: str, version: str = 'v1'):
    return build_project_bom(
        bom_id='bom-1',
        document_id=document_id,
        version=version,
        design_refs=(
            DesignAuthorityRef(
                authority_kind='scene_revision',
                ref_id='rev-1',
                ref_sha256=_hash('rev-1'),
            ),
        ),
        line_items=(
            BOMLineItem(
                line_id='line-1',
                category='amplifier',
                name='Amplifier',
                quantity=1,
                unit='each',
                requirement='Amplifier requirement',
            ),
        ),
        generation_policy='fixture-policy',
        generated_at_utc=NOW,
    )


def test_target_profile_save_reopen_and_append_only(tmp_path) -> None:
    _, revision, repo = _repo(tmp_path)
    document_id = revision.document_id
    profile = _target_profile(document_id)

    repo.save_acoustic_target_profile(profile)
    repo.save_acoustic_target_profile(profile)  # idempotent re-save
    loaded = repo.get_acoustic_target_profile(
        'target-main', 'v1'
    )
    assert loaded == profile
    assert repo.list_acoustic_target_profiles(document_id) == (profile,)
    assert repo.list_acoustic_target_profiles('other-doc') == ()

    other = _target_profile(document_id, version='v2')
    repo.save_acoustic_target_profile(other)
    # A different payload under a claimed (id, version) is a collision.
    tampered = build_acoustic_target_profile(
        profile_id='target-main',
        profile_version='v1',
        name='Tampered target',
        document_id=document_id,
        criteria=profile.criteria,
        created_at_utc=NOW,
    )
    assert tampered.target_semantic_hash != profile.target_semantic_hash
    with pytest.raises(FeatureAuthorityConflictError):
        repo.save_acoustic_target_profile(tampered)


def test_isolation_batch_persists_with_scope(tmp_path) -> None:
    _, revision, repo = _repo(tmp_path)
    document_id = revision.document_id
    assembly = _assembly()
    scenario = _scenario()

    repo.save_isolation_assembly(assembly, document_id=document_id)
    assert repo.get_isolation_assembly('asm-wall') == assembly
    assert assembly in repo.list_isolation_assemblies(document_id)

    digest = repo.save_isolation_scenario(scenario, document_id=document_id)
    assert len(digest) == 64
    assert repo.get_isolation_scenario('sc-1') == scenario

    estimate = estimate_isolation(
        scenario=scenario, assemblies=(assembly,)
    )
    repo.save_isolation_estimate(estimate, document_id=document_id)
    assert repo.get_isolation_estimate(estimate.estimate_sha256) == estimate

    measurement = IsolationMeasurement(
        measurement_id='meas-iso-1',
        method_profile='astm_e336',
        source_region_id='theater',
        receiving_region_id='bedroom',
        measured_at_utc=NOW,
        provenance=(_prov(),),
    )
    repo.save_isolation_measurement(measurement, document_id=document_id)
    assert repo.get_isolation_measurement('meas-iso-1') == measurement

    # Estimates cannot be orphaned from their scenario.
    orphan = estimate_isolation(
        scenario=scenario.model_copy(update={'scenario_id': 'sc-missing'}),
        assemblies=(assembly,),
    )
    with pytest.raises(FeatureAuthorityConflictError):
        repo.save_isolation_estimate(orphan, document_id=document_id)


def test_rack_definition_and_layout_persist(tmp_path) -> None:
    _, revision, repo = _repo(tmp_path)
    rack = _rack()
    repo.save_rack_definition(rack, document_id=revision.document_id)
    assert repo.get_rack_definition('rack-1') == rack

    layout = RackLayout(
        rack_id='rack-1',
        placements=(RackPlacement(device_id='dev-1', ru_position=1),),
    )
    layout_id = repo.save_rack_layout(
        layout, document_id=revision.document_id
    )
    assert layout_id.startswith('rack-layout:')
    assert repo.get_rack_layout(layout_id) == layout

    # A layout can never reference an unpersisted rack.
    with pytest.raises(FeatureAuthorityConflictError):
        repo.save_rack_layout(
            RackLayout(
                rack_id='rack-missing',
                placements=(
                    RackPlacement(device_id='dev-1', ru_position=1),
                ),
            )
        )


def test_project_bom_versions_append_only(tmp_path) -> None:
    _, revision, repo = _repo(tmp_path)
    document_id = revision.document_id
    bom = _bom(document_id)
    repo.save_project_bom(bom)
    repo.save_project_bom(_bom(document_id, version='v2'))
    assert repo.get_project_bom('bom-1', 'v1') == bom
    assert len(repo.list_project_boms(document_id)) == 2

    divergent = build_project_bom(
        bom_id='bom-1',
        document_id=document_id,
        version='v1',
        design_refs=bom.design_refs,
        line_items=bom.line_items,
        generation_policy='other-policy',
        generated_at_utc=NOW,
    )
    assert divergent.bom_semantic_hash != bom.bom_semantic_hash
    with pytest.raises(FeatureAuthorityConflictError):
        repo.save_project_bom(divergent)


def test_drawing_set_spec_and_rendered_set_persist(tmp_path) -> None:
    _, revision, repo = _repo(tmp_path)
    spec = build_drawing_set_spec(
        spec_id='ds-1',
        spec_version='v1',
        sheets=('floor_plan', 'front_elevation'),
        datum_ids=(),
    )
    repo.save_drawing_set_spec(spec, document_id=revision.document_id)
    assert repo.get_drawing_set_spec('ds-1', 'v1') == spec

    drawing_set = InstallationDrawingSet(
        drawing_set_id='ds-1-rendered',
        installation_output_sha256=_hash('output'),
        spec_sha256=spec.spec_semantic_hash,
        sheets=(),
        generated_at_utc=NOW,
    )
    repo.save_installation_drawing_set(
        drawing_set, document_id=revision.document_id
    )
    assert (
        repo.get_installation_drawing_set('ds-1-rendered') == drawing_set
    )


def test_field_labels_and_sheets_persist(tmp_path) -> None:
    _, revision, repo = _repo(tmp_path)
    project_id = revision.document_id
    label = mint_label(
        project_id=project_id,
        target_kind='installed_equipment',
        target_id='dev-1',
        generation=1,
        created_at_utc=NOW,
    )
    repo.save_field_label(label)
    assert repo.get_field_label(label.label_id) == label
    assert repo.list_field_labels(project_id) == (label,)

    reissued = mint_reissued_label_for_same_target(
        label, created_at_utc=NOW
    )
    repo.save_field_label(reissued)
    assert len(repo.list_field_labels(project_id, target_id='dev-1')) == 2

    # Re-minting the same (target, generation) at a different timestamp
    # still produces the identical content-addressed label — saving it is
    # an idempotent no-op, not a collision.
    dupe = mint_label(
        project_id=project_id,
        target_kind='installed_equipment',
        target_id='dev-1',
        generation=2,
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    repo.save_field_label(dupe)
    assert len(repo.list_field_labels(project_id, target_id='dev-1')) == 2

    sheet = generate_label_sheet(
        (label, reissued),
        label_kind='device',
        sheet_id='sheet-1',
        page=(210.0, 297.0),
    )
    repo.save_label_sheet(sheet, project_id=project_id)
    assert repo.get_label_sheet('sheet-1') == sheet


def test_registry_resolves_feature_kinds(tmp_path) -> None:
    scene_repository, revision, repo = _repo(tmp_path)
    document_id = revision.document_id
    profile = _target_profile(document_id)
    repo.save_acoustic_target_profile(profile)
    assembly = _assembly()  # library authority — unbound scope
    repo.save_isolation_assembly(assembly)
    bom = _bom(document_id)
    repo.save_project_bom(bom)

    registry = build_canonical_authority_registry(
        scene_repository, feature_authority_repository=repo
    )

    resolved = registry.resolve(
        'acoustic_target_profile', 'target-main', document_id
    )
    assert resolved is not None
    assert resolved.semantic_sha256 == profile.target_semantic_hash
    assert resolved.document_id == document_id

    # Cross-document refs fail closed.
    assert (
        registry.resolve(
            'acoustic_target_profile', 'target-main', 'other-doc'
        )
        is None
    )

    # Library-scope assemblies resolve for any document.
    resolved = registry.resolve('isolation_assembly', 'asm-wall', document_id)
    assert resolved is not None
    assert resolved.document_id is None
    assert resolved.semantic_sha256 == assembly.semantic_sha256

    resolved = registry.resolve('project_bom', 'bom-1', document_id)
    assert resolved is not None
    assert resolved.semantic_sha256 == bom.bom_semantic_hash

    assert registry.resolve('project_bom', 'bom-1', 'other-doc') is None
    assert registry.resolve('rack_definition', 'nope', document_id) is None
    assert registry.adapter('project_bom').owner == (
        'CadFeatureAuthorityRepository'
    )
