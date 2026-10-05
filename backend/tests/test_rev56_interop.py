"""REV56-INTEROP regression tests (issue #578 openBIM IFC 4.3 interop,
issue #586 CEDIA RP1 performance-facts ingestion).

Fixture map — issue #578 §16:
  BIM10  units + axis fidelity            -> test_bim10_*
  BIM20  nested placement                 -> test_bim20_*
  BIM30  concave room + openings          -> test_bim30_*
  BIM40  build-up without acoustic truth  -> test_bim40_*
  BIM50  source revision delta            -> test_bim50_*
  BIM60  mapping ambiguity                -> test_bim60_*
  BIM70  export proposal                  -> test_bim70_*
  BIM80  IDS-style intake validation      -> test_bim80_*

Fixture map — issue #586 §15:
  RP1F10 underspecified marketing fact    -> test_rp1f10_*
  RP1F20 condition-complete amp fact      -> test_rp1f20_*
  RP1F30 FR condition mismatch            -> test_rp1f30_*
  RP1F40 source conflict                  -> test_rp1f40_*
  RP1F50 review-to-final revision         -> test_rp1f50_*
  RP1F60 RP22 consumption                 -> test_rp1f60_*
"""

from __future__ import annotations

from hashlib import sha256

import pytest

from htdt.cad_ifc_interop import (
    build_ifc_export_package,
    build_ifc_import,
    build_ifc_intake_profile,
    compute_ifc_revision_delta,
    default_htdt_intake_requirements,
    evaluate_ifc_intake,
    resolve_ifc_model,
)
from htdt.cad_ifc_repository import (
    CadIfcInteropRepository,
    IfcInteropIntegrityError,
)
from htdt.cad_performance_facts import (
    PerformanceFactConditions,
    PerformanceFactsFieldSpec,
    PerformanceFieldDispositionRecord,
    PerformanceRequirement,
    build_performance_fact,
    build_performance_facts_import,
    build_product_identity,
    build_profile_rebind,
    evaluate_fact_sufficiency,
    evaluate_product_suitability,
    reconcile_conflicting_facts,
    register_performance_facts_profile,
    rp22_consumption_view,
)
from htdt.cad_performance_facts_repository import (
    CadPerformanceFactsRepository,
    PerformanceFactsIntegrityError,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.ifc_step import parse_ifc_step

_TS = '2026-10-05T12:00:00+00:00'


# ---------------------------------------------------------------------------
# IFC fixture builders
# ---------------------------------------------------------------------------


def _header(schema: str = 'IFC4X3_ADD2') -> str:
    return (
        'ISO-10303-21;\n'
        'HEADER;\n'
        "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');\n"
        "FILE_NAME('f.ifc','2026-10-05T10:00:00',('a'),('o'),'app','v','');\n"
        f"FILE_SCHEMA(('{schema}'));\n"
        'ENDSEC;\n'
        'DATA;\n'
    )


_TAIL = 'ENDSEC;\nEND-ISO-10303-21;\n'


def _context(units: str, north: str = '') -> str:
    tn = f',#12' if north else ''
    return (
        '#1=IFCPROJECT(\'gid-proj\',$,\'P\',$,$,$,$,(#20),#40);\n'
        '#10=IFCAXIS2PLACEMENT3D(#11,$,$);\n'
        '#11=IFCCARTESIANPOINT((0.,0.,0.));\n'
        + north
        + '#20=IFCGEOMETRICREPRESENTATIONCONTEXT($,\'Model\',3,1.E-05,#10'
        + tn + ');\n'
        + units
        + '#40=IFCUNITASSIGNMENT((#30));\n'
    )


MM_UNITS = "#30=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);\n"
M_UNITS = "#30=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);\n"
INCH_UNITS = (
    '#30=IFCCONVERSIONBASEDUNIT(#31,.LENGTHUNIT.,\'INCH\',#32);\n'
    '#31=IFCDIMENSIONALEXPONENTS(1,0,0,0,0,0,0);\n'
    '#32=IFCMEASUREWITHUNIT(IFCLENGTHMEASURE(0.0254),#33);\n'
    '#33=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);\n'
)
NORTH_45 = '#12=IFCDIRECTION((0.70710678118655,0.70710678118655,0.));\n'

_STOREY_CHAIN = (
    '#45=IFCAXIS2PLACEMENT3D(#46,$,$);\n'
    '#46=IFCCARTESIANPOINT((0.,0.,0.));\n'
    '#47=IFCLOCALPLACEMENT($,#45);\n'
    '#50=IFCSITE(\'gid-site\',$,\'Site\',$,$,#47,$,$,.ELEMENT.,$,$,$,$,$);\n'
    '#51=IFCBUILDING(\'gid-bldg\',$,\'B\',$,$,#47,$,$,.ELEMENT.,$,$,$);\n'
    '#52=IFCBUILDINGSTOREY(\'gid-storey\',$,\'L1\',$,$,#53,$,$,.ELEMENT.,$,$,3000.);\n'
    '#53=IFCLOCALPLACEMENT(#47,#54);\n'
    '#54=IFCAXIS2PLACEMENT3D(#55,$,$);\n'
    '#55=IFCCARTESIANPOINT((0.,0.,3000.));\n'
    '#60=IFCRELAGGREGATES(\'a1\',$,$,$,#1,(#50));\n'
    '#61=IFCRELAGGREGATES(\'a2\',$,$,$,#50,(#51));\n'
    '#62=IFCRELAGGREGATES(\'a3\',$,$,$,#51,(#52));\n'
)

_L_SHAPED_SPACE = (
    '#70=IFCLOCALPLACEMENT(#53,#71);\n'
    '#71=IFCAXIS2PLACEMENT3D(#72,$,$);\n'
    '#72=IFCCARTESIANPOINT((1000.,2000.,0.));\n'
    '#80=IFCARBITRARYCLOSEDPROFILEDEF(.AREA.,\'L\',#81);\n'
    '#81=IFCPOLYLINE((#82,#83,#84,#85,#86,#87));\n'
    '#82=IFCCARTESIANPOINT((0.,0.));\n'
    '#83=IFCCARTESIANPOINT((5000.,0.));\n'
    '#84=IFCCARTESIANPOINT((5000.,7000.));\n'
    '#85=IFCCARTESIANPOINT((3000.,7000.));\n'
    '#86=IFCCARTESIANPOINT((3000.,4000.));\n'
    '#87=IFCCARTESIANPOINT((0.,4000.));\n'
    '#90=IFCEXTRUDEDAREASOLID(#80,$,#91,2800.);\n'
    '#91=IFCDIRECTION((0.,0.,1.));\n'
    '#92=IFCSHAPEREPRESENTATION(#20,\'Body\',\'SweptSolid\',(#90));\n'
    '#93=IFCPRODUCTDEFINITIONSHAPE($,$,(#92));\n'
    '#100=IFCSPACE(\'gid-space\',$,\'Theater\',$,$,#70,#93,$,.ELEMENT.,.INTERNAL.,$);\n'
)

_WALL_WITH_DOOR = (
    '#110=IFCWALL(\'gid-wall\',$,\'W1\',$,$,#111,#115);\n'
    '#111=IFCLOCALPLACEMENT(#53,#112);\n'
    '#112=IFCAXIS2PLACEMENT3D(#113,$,$);\n'
    '#113=IFCCARTESIANPOINT((0.,0.,0.));\n'
    '#114=IFCMATERIALLAYER(#200,19.,$,\'gypsum\');\n'
    '#115=IFCPRODUCTDEFINITIONSHAPE($,$,(#116));\n'
    '#116=IFCSHAPEREPRESENTATION(#20,\'Axis\',\'Curve2D\',(#117));\n'
    '#117=IFCPOLYLINE((#118,#119));\n'
    '#118=IFCCARTESIANPOINT((0.,0.));\n'
    '#119=IFCCARTESIANPOINT((5000.,0.));\n'
    '#200=IFCMATERIAL(\'Gypsum Board\');\n'
    '#201=IFCMATERIALLAYERSET((#114),\'stud wall\');\n'
    '#202=IFCMATERIALLAYERSETUSAGE(#201,.AXIS2.,.NEGATIVE.,0.,150.);\n'
    '#203=IFCRELASSOCIATESMATERIAL(\'mr\',$,$,$,(#110),#202);\n'
    '#210=IFCOPENINGELEMENT(\'gid-open\',$,\'O1\',$,$,#211,$);\n'
    '#211=IFCLOCALPLACEMENT(#111,#212);\n'
    '#212=IFCAXIS2PLACEMENT3D(#213,$,$);\n'
    '#213=IFCCARTESIANPOINT((1200.,0.,0.));\n'
    '#220=IFCDOOR(\'gid-door\',$,\'D1\',$,$,#221,$,\'door\',900.,2100.);\n'
    '#221=IFCLOCALPLACEMENT(#211,#222);\n'
    '#222=IFCAXIS2PLACEMENT3D(#223,$,$);\n'
    '#223=IFCCARTESIANPOINT((0.,0.,0.));\n'
    '#230=IFCRELVOIDSELEMENT(\'vr\',$,$,$,#110,#210);\n'
    '#231=IFCRELFILLSELEMENT(\'fr\',$,$,$,#210,#220);\n'
    '#240=IFCRELCONTAINEDINSPATIALSTRUCTURE(\'c1\',$,$,$,(#100,#110,#220),#52);\n'
)


def _model(
    *,
    units: str = MM_UNITS,
    north: str = '',
    storey_chain: str = _STOREY_CHAIN,
    body: str = '',
    header: str | None = None,
) -> str:
    return (header if header is not None else _header()) + _context(
        units, north
    ) + storey_chain + body + _TAIL


# ---------------------------------------------------------------------------
# BIM10 — units + axis fidelity (issue #578 §16)
# ---------------------------------------------------------------------------


def test_bim10_metric_mm_units_scale_world_transform() -> None:
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='m.ifc',
        source=_model(body=_L_SHAPED_SPACE),
        imported_at_utc=_TS,
    )
    space = next(m for m in mappings if m.htdt_role == 'room_candidate')
    # placement (1000,2000,0)mm under storey (0,0,3000)mm -> metres
    t = space.world_transform_m
    assert t is not None
    assert (t[3], t[7], t[11]) == (1.0, 2.0, 3.0)
    assert artifact.coordinate.length_scale_to_meter == 0.001
    assert artifact.coverage.unhandled_entity_types == {}


def test_bim10_imperial_conversion_unit_resolves_scale() -> None:
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='i.ifc',
        source=_model(units=INCH_UNITS, body=_L_SHAPED_SPACE),
        imported_at_utc=_TS,
    )
    assert artifact.coordinate.length_unit_name == 'INCH'
    assert artifact.coordinate.length_scale_to_meter == pytest.approx(0.0254)
    space = next(m for m in mappings if m.htdt_role == 'room_candidate')
    t = space.world_transform_m
    assert t is not None
    assert t[3] == pytest.approx(1000.0 * 0.0254)


def test_bim10_undeclared_unit_withholds_metre_transform() -> None:
    # No unit assignment at all -> honest withholding, never a guess.
    doc = (
        _header()
        + '#1=IFCPROJECT(\'gid-proj\',$,\'P\',$,$,$,$,(#20),$);\n'
        + '#10=IFCAXIS2PLACEMENT3D(#11,$,$);\n'
        + '#11=IFCCARTESIANPOINT((0.,0.,0.));\n'
        + '#20=IFCGEOMETRICREPRESENTATIONCONTEXT($,\'Model\',3,1.E-05,#10,$);\n'
        + _STOREY_CHAIN
        + _L_SHAPED_SPACE
        + _TAIL
    )
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='nu.ifc',
        source=doc,
        imported_at_utc=_TS,
    )
    assert artifact.coordinate.length_unit_name is None
    assert artifact.coordinate.length_scale_to_meter is None
    assert artifact.coordinate.length_unit_state == 'undeclared'
    space = next(m for m in mappings if m.htdt_role == 'room_candidate')
    assert space.world_transform_m is None
    assert any('length unit' in w for w in artifact.warnings)


def test_bim10_true_north_declared_not_applied() -> None:
    artifact, _ = build_ifc_import(
        document_id='d',
        file_name='n.ifc',
        source=_model(north=NORTH_45, body=_L_SHAPED_SPACE),
        imported_at_utc=_TS,
    )
    c = artifact.coordinate
    assert c.true_north_vector is not None
    assert c.true_north_angle_deg == pytest.approx(45.0)
    assert c.true_north_state == 'declared'


def test_bim10_unsupported_schema_is_rejected() -> None:
    doc = _model(header=_header('IFC2X3'), body=_L_SHAPED_SPACE)
    artifact, _ = build_ifc_import(
        document_id='d',
        file_name='old.ifc',
        source=doc,
        imported_at_utc=_TS,
    )
    assert artifact.schema_supported is False


# ---------------------------------------------------------------------------
# BIM20 — nested placement (issue #578 §16)
# ---------------------------------------------------------------------------


def test_bim20_nested_local_placement_composes() -> None:
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='np.ifc',
        source=_model(body=_L_SHAPED_SPACE + _WALL_WITH_DOOR),
        imported_at_utc=_TS,
    )
    by_gid = {m.ifc_global_id: m for m in mappings}
    door = by_gid['gid-door']
    # door(0,0,0) -> opening(1200,0,0) -> wall(0,0,0) -> storey(0,0,3000)
    # => (1200,0,3000)mm => (1.2, 0, 3.0)m
    t = door.world_transform_m
    assert t is not None
    assert (t[3], t[7], t[11]) == pytest.approx((1.2, 0.0, 3.0))
    assert door.parent_global_id == 'gid-storey'


def test_bim20_placement_cycle_is_unresolved_not_fatal() -> None:
    cyclic = (
        '#45=IFCAXIS2PLACEMENT3D(#46,$,$);\n'
        '#46=IFCCARTESIANPOINT((0.,0.,0.));\n'
        '#47=IFCLOCALPLACEMENT(#53,#45);\n'
        '#53=IFCLOCALPLACEMENT(#47,#54);\n'
        '#54=IFCAXIS2PLACEMENT3D(#55,$,$);\n'
        '#55=IFCCARTESIANPOINT((0.,0.,3000.));\n'
        '#50=IFCSITE(\'gid-site\',$,\'Site\',$,$,#47,$,$,.ELEMENT.,$,$,$,$,$);\n'
        '#51=IFCBUILDING(\'gid-bldg\',$,\'B\',$,$,#47,$,$,.ELEMENT.,$,$,$);\n'
        '#52=IFCBUILDINGSTOREY(\'gid-storey\',$,\'L1\',$,$,#53,$,$,.ELEMENT.,$,$,3000.);\n'
        '#60=IFCRELAGGREGATES(\'a1\',$,$,$,#1,(#50));\n'
        '#61=IFCRELAGGREGATES(\'a2\',$,$,$,#50,(#51));\n'
        '#62=IFCRELAGGREGATES(\'a3\',$,$,$,#51,(#52));\n'
    )
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='c.ifc',
        source=_model(storey_chain=cyclic, body=_L_SHAPED_SPACE),
        imported_at_utc=_TS,
    )
    space = next(m for m in mappings if m.htdt_role == 'room_candidate')
    assert space.world_transform_m is None
    assert space.world_transform_source_units is None
    assert artifact.coverage.unresolved_placement_count >= 3
    assert any('cyclic' in w for w in artifact.warnings)


# ---------------------------------------------------------------------------
# BIM30 — concave room + openings (issue #578 §16)
# ---------------------------------------------------------------------------


def test_bim30_concave_space_extrusion_and_opening_linkage() -> None:
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='lr.ifc',
        source=_model(body=_L_SHAPED_SPACE + _WALL_WITH_DOOR),
        imported_at_utc=_TS,
    )
    by_gid = {m.ifc_global_id: m for m in mappings}
    space = by_gid['gid-space']
    assert space.ifc_type == 'IFCSPACE'
    geom = space.geometry_descriptors[0]
    assert geom.kind == 'extruded_area_solid'
    assert geom.profile_name == 'L'
    assert len(geom.profile_points) == 6  # concave L kept, never convexified
    assert geom.depth_source_units == pytest.approx(2800.0)
    # opening element -> filled by door, recorded on the host wall
    assert by_gid['gid-open'].htdt_role == 'opening'
    assert by_gid['gid-door'].htdt_role == 'opening'
    assert artifact.coverage.mapped_entity_count == 4


def test_bim30_opening_records_fill_chain_on_host() -> None:
    _artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='lr2.ifc',
        source=_model(body=_L_SHAPED_SPACE + _WALL_WITH_DOOR),
        imported_at_utc=_TS,
    )
    wall = next(m for m in mappings if m.ifc_global_id == 'gid-wall')
    assert len(wall.openings) == 1
    assert wall.openings[0].opening_global_id == 'gid-open'
    assert wall.openings[0].filling_global_id == 'gid-door'
    assert wall.openings[0].filling_type == 'IFCDOOR'


# ---------------------------------------------------------------------------
# BIM40 — build-up without acoustic property (issue #578 §16, §4)
# ---------------------------------------------------------------------------


def test_bim40_material_layers_are_hints_not_acoustic_truth() -> None:
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='bu.ifc',
        source=_model(body=_WALL_WITH_DOOR),
        imported_at_utc=_TS,
    )
    wall = next(m for m in mappings if m.ifc_global_id == 'gid-wall')
    assert [l.material_name for l in wall.material_layers] == [
        'Gypsum Board'
    ]
    assert wall.material_layers[0].layer_thickness_source_units == (
        pytest.approx(19.0)
    )
    # construction identity is NEVER an acoustic preset — the mapping
    # carries no absorption/scattering claim.
    assert 'absorption' not in wall.properties
    assert artifact.coverage.relation_counts['IFCRELASSOCIATESMATERIAL'] == 1
    # no acoustic property was silently synthesized from 'Gypsum Board'
    layer = wall.material_layers[0]
    assert not hasattr(layer, 'absorption')


# ---------------------------------------------------------------------------
# BIM50 — source revision delta (issue #578 §16, §6)
# ---------------------------------------------------------------------------


def _revision_doc(*, door_gid: str = 'gid-door', open_x: float = 1200.0) -> str:
    body = _WALL_WITH_DOOR.replace("'gid-door'", f"'{door_gid}'").replace(
        '(1200.,0.,0.)', f'({open_x},0.,0.)'
    )
    return _model(body=_L_SHAPED_SPACE + body)


def test_bim50_revision_delta_classifies_changes() -> None:
    a1, m1 = build_ifc_import(
        document_id='d',
        file_name='r1.ifc',
        source=_revision_doc(),
        imported_at_utc=_TS,
    )
    # v2: opening moved + door re-issued with a NEW GlobalId (ambiguous)
    a2, m2 = build_ifc_import(
        document_id='d',
        file_name='r2.ifc',
        source=_revision_doc(door_gid='gid-door-v2', open_x=1500.0),
        imported_at_utc='2026-10-05T13:00:00+00:00',
    )
    delta = compute_ifc_revision_delta(
        document_id='d',
        prior_artifact=a1,
        prior_mappings=m1,
        new_artifact=a2,
        new_mappings=m2,
        evaluated_at_utc='2026-10-05T13:05:00+00:00',
    )
    assert delta.reconciliation_state == 'needs_review'
    kinds = {i.ifc_global_id: i.change_kind for i in delta.items}
    assert kinds['gid-open'] == 'geometry_changed'
    assert kinds['gid-wall'] == 'semantics_changed'
    assert kinds['gid-space'] == 'unchanged'
    assert delta.ambiguous_count == 1
    ambiguous = next(
        i for i in delta.items if i.change_kind == 'ambiguous_rebind'
    )
    assert ambiguous.new_mapping_id is not None
    assert ambiguous.acoustically_relevant is True


def test_bim50_coordinate_authority_change_forces_review() -> None:
    a1, m1 = build_ifc_import(
        document_id='d',
        file_name='r1.ifc',
        source=_revision_doc(),
        imported_at_utc=_TS,
    )
    # v2 identical except the project switched to metres
    a2, m2 = build_ifc_import(
        document_id='d',
        file_name='r2.ifc',
        source=_model(units=M_UNITS, body=_L_SHAPED_SPACE + _WALL_WITH_DOOR),
        imported_at_utc='2026-10-05T13:00:00+00:00',
    )
    delta = compute_ifc_revision_delta(
        document_id='d',
        prior_artifact=a1,
        prior_mappings=m1,
        new_artifact=a2,
        new_mappings=m2,
        evaluated_at_utc='2026-10-05T13:05:00+00:00',
    )
    assert delta.reconciliation_state == 'needs_review'
    assert any(
        'coordinate authority' in (i.detail or '') for i in delta.items
    )


# ---------------------------------------------------------------------------
# BIM60 — mapping ambiguity (issue #578 §16, §3)
# ---------------------------------------------------------------------------


def test_bim60_proxy_and_unknown_types_map_to_generic_honestly() -> None:
    body = (
        '#300=IFCBUILDINGELEMENTPROXY(\'gid-proxy\',$,\'Thing\',$,$,#301,$);\n'
        '#301=IFCLOCALPLACEMENT(#53,#302);\n'
        '#302=IFCAXIS2PLACEMENT3D(#303,$,$);\n'
        '#303=IFCCARTESIANPOINT((0.,0.,0.));\n'
        '#310=IFCCURTAINWALL(\'gid-cw\',$,\'CW\',$,$,#311,$);\n'
        '#311=IFCLOCALPLACEMENT(#53,#312);\n'
        '#312=IFCAXIS2PLACEMENT3D(#313,$,$);\n'
        '#313=IFCCARTESIANPOINT((0.,0.,0.));\n'
        '#320=IFCANNOTATION(\'gid-note\',$,\'note\',$,$,$,$);\n'
        '#240=IFCRELCONTAINEDINSPATIALSTRUCTURE(\'c1\',$,$,$,(#300,#310,#320),#52);\n'
    )
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='amb.ifc',
        source=_model(body=body),
        imported_at_utc=_TS,
    )
    by_gid = {m.ifc_global_id: m for m in mappings}
    assert by_gid['gid-proxy'].htdt_role == 'generic'
    # IFCANNOTATION is not a covered entity type — counted honestly.
    assert 'IFCANNOTATION' in artifact.coverage.unhandled_entity_types
    assert artifact.coverage.unhandled_entity_types['IFCANNOTATION'] == 1


# ---------------------------------------------------------------------------
# BIM70 — export proposal (issue #578 §16, §9)
# ---------------------------------------------------------------------------


def test_bim70_reference_export_emits_parseable_step_with_provenance() -> None:
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='src.ifc',
        source=_model(body=_L_SHAPED_SPACE),
        imported_at_utc=_TS,
    )
    package = build_ifc_export_package(
        document_id='d',
        mode='reference_export',
        label='coordination',
        spaces=[m for m in mappings if m.htdt_role == 'room_candidate'],
        source_artifact=artifact,
        created_at_utc='2026-10-05T14:00:00+00:00',
    )
    assert package.step_sha256 == sha256(
        package.step_text.encode('utf-8')
    ).hexdigest()
    header, entities = parse_ifc_step(package.step_text)
    assert header.schema_identifiers == ('IFC4X3_ADD2',)
    names = {e.name for e in entities}
    assert 'IFCSPACE' in names
    assert 'IFCRELCONTAINEDINSPATIALSTRUCTURE' in names
    assert 'IFCPROPERTYSET' in names
    # provenance carried, never silently rewritten
    assert 'Pset_HTDT_Provenance' in package.step_text
    assert 'HTDTExportMode' in package.step_text
    # declaration-symmetric: unresolved scientific data listed
    assert package.unexported_items


def test_bim70_export_requires_metre_resolved_transforms() -> None:
    # undeclared units -> no metre transform -> fail-closed listing
    doc = (
        _header()
        + '#1=IFCPROJECT(\'gid-proj\',$,\'P\',$,$,$,$,(#20),$);\n'
        + '#10=IFCAXIS2PLACEMENT3D(#11,$,$);\n'
        + '#11=IFCCARTESIANPOINT((0.,0.,0.));\n'
        + '#20=IFCGEOMETRICREPRESENTATIONCONTEXT($,\'Model\',3,1.E-05,#10,$);\n'
        + _STOREY_CHAIN
        + _L_SHAPED_SPACE
        + _TAIL
    )
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='nu.ifc',
        source=doc,
        imported_at_utc=_TS,
    )
    package = build_ifc_export_package(
        document_id='d',
        mode='reference_export',
        label='coordination',
        spaces=[m for m in mappings if m.htdt_role == 'room_candidate'],
        source_artifact=artifact,
        created_at_utc=_TS,
    )
    assert 'IFCSPACE' not in package.step_text
    assert any(
        'undeclared source unit' in item
        for item in package.unexported_items
    )


# ---------------------------------------------------------------------------
# BIM80 — IDS-style intake validation (issue #578 §16, §8)
# ---------------------------------------------------------------------------


def test_bim80_intake_evaluation_actionable_diagnostics() -> None:
    profile = build_ifc_intake_profile(
        document_id='d',
        name='HTDT minimum room intake',
        profile_version='1.0',
        requirements=default_htdt_intake_requirements(),
    )
    evaluation = evaluate_ifc_intake(
        document_id='d',
        profile=profile,
        source=_model(body=_L_SHAPED_SPACE + _WALL_WITH_DOOR),
        evaluated_at_utc=_TS,
    )
    by_req = {r.requirement_id: r for r in evaluation.results}
    assert by_req['schema'].verdict == 'satisfied'
    assert by_req['units'].verdict == 'satisfied'
    assert by_req['spaces'].verdict == 'satisfied'
    # adjacency data is absent -> optional requirement fails with an
    # actionable diagnostic, not a generic parse error
    assert by_req['adjacency'].verdict == 'unsatisfied'
    assert 'IfcRelSpaceBoundary' in by_req['adjacency'].diagnostics[0]
    assert evaluation.overall_state == 'satisfied_with_gaps'


def test_bim80_missing_unit_fails_required_requirement() -> None:
    doc = (
        _header()
        + '#1=IFCPROJECT(\'gid-proj\',$,\'P\',$,$,$,$,(#20),$);\n'
        + '#10=IFCAXIS2PLACEMENT3D(#11,$,$);\n'
        + '#11=IFCCARTESIANPOINT((0.,0.,0.));\n'
        + '#20=IFCGEOMETRICREPRESENTATIONCONTEXT($,\'Model\',3,1.E-05,#10,$);\n'
        + _STOREY_CHAIN
        + _L_SHAPED_SPACE
        + _TAIL
    )
    profile = build_ifc_intake_profile(
        document_id='d',
        name='HTDT minimum room intake',
        profile_version='1.0',
        requirements=default_htdt_intake_requirements(),
    )
    evaluation = evaluate_ifc_intake(
        document_id='d',
        profile=profile,
        source=doc,
        evaluated_at_utc=_TS,
    )
    units = next(r for r in evaluation.results if r.requirement_id == 'units')
    assert units.verdict == 'unsatisfied'
    assert 'missing_length_unit' in units.diagnostics[0]
    assert evaluation.overall_state == 'failed'


def test_bim80_unparseable_file_rejects_with_schema_diagnostic() -> None:
    profile = build_ifc_intake_profile(
        document_id='d',
        name='HTDT minimum room intake',
        profile_version='1.0',
        requirements=default_htdt_intake_requirements(),
    )
    evaluation = evaluate_ifc_intake(
        document_id='d',
        profile=profile,
        source='this is not STEP at all',
        evaluated_at_utc=_TS,
    )
    assert evaluation.overall_state == 'failed'
    schema = next(r for r in evaluation.results if r.requirement_id == 'schema')
    assert schema.verdict == 'unsatisfied'
    assert 'model_resolution_failed' in schema.diagnostics[0]


# ---------------------------------------------------------------------------
# Repository round-trip + integrity (issue #578 general)
# ---------------------------------------------------------------------------


def _scene_repo(tmp_path):
    from htdt.cad_repository import SceneRepository

    return SceneRepository(tmp_path / 'scene.sqlite3')


def test_ifc_repository_roundtrip_and_foreign_binding(tmp_path) -> None:
    repo = CadIfcInteropRepository(_scene_repo(tmp_path))
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='m.ifc',
        source=_model(body=_L_SHAPED_SPACE + _WALL_WITH_DOOR),
        imported_at_utc=_TS,
    )
    repo.save_import_artifact(artifact)
    repo.save_entity_mappings(mappings)
    loaded = repo.get_import_artifact(artifact.artifact_id)
    assert loaded is not None
    assert loaded.artifact_sha256 == artifact.artifact_sha256
    listed = repo.list_entity_mappings('d', artifact.artifact_id)
    assert len(listed) == len(mappings)
    # forgery rejected: tampered payload fails the row-level seal check
    forged = artifact.model_copy(update={'file_name': 'forged.ifc'})
    with pytest.raises(IfcInteropIntegrityError):
        repo.save_import_artifact(forged)


def test_ifc_mapping_requires_stored_artifact(tmp_path) -> None:
    repo = CadIfcInteropRepository(_scene_repo(tmp_path))
    artifact, mappings = build_ifc_import(
        document_id='d',
        file_name='m.ifc',
        source=_model(body=_L_SHAPED_SPACE),
        imported_at_utc=_TS,
    )
    # never saved the artifact -> mapping cannot float free
    with pytest.raises(IfcInteropIntegrityError):
        repo.save_entity_mappings(mappings)
    repo.save_import_artifact(artifact)
    repo.save_entity_mappings(mappings)
    assert repo.get_entity_mapping(mappings[0].mapping_id) is not None


# ---------------------------------------------------------------------------
# RP1 fixtures (issue #586 §15)
# ---------------------------------------------------------------------------


def _rp1_profile(document_id: str = 'd', maturity='industry_review'):
    return register_performance_facts_profile(
        document_id=document_id,
        publisher='CEDIA',
        family='rp1_1_loudspeakers',
        document_reference='CEDIA RP1-1 Loudspeaker Performance Facts',
        revision_or_date='industry-review-2025-09',
        maturity_state=maturity,
        source_reference='https://cedia.org/rp1-1-review',
        field_mapping_version='htdt-rp1-map-1',
        field_definitions=(
            PerformanceFactsFieldSpec(
                field_name='max_output_db_spl_1m',
                quantity_kind='max_output',
                unit='dB SPL',
                required_conditions=(
                    'measurement_distance_m',
                    'axis_azimuth_deg',
                    'environment',
                ),
            ),
            PerformanceFactsFieldSpec(
                field_name='continuous_power_w_8ohm',
                quantity_kind='amplifier_output',
                unit='W',
                required_conditions=(
                    'load_impedance_ohm',
                    'channels_driven',
                    'duration_kind',
                ),
            ),
        ),
    )


def _product(document_id: str = 'd'):
    return build_product_identity(
        document_id=document_id,
        manufacturer='Acme',
        brand='Acme Audio',
        model='SUB-1',
        hardware_revision='mk2',
        variant='230V',
    )


def _amp_fact(product, **conditions):
    return build_performance_fact(
        document_id='d',
        product=product,
        quantity_kind='amplifier_output',
        value_kind='scalar',
        scalar_value=100.0,
        unit='W',
        evidence_class='rp1_profiled_manufacturer_data',
        source_field_name='continuous_power_w_8ohm',
        conditions=PerformanceFactConditions(**conditions),
    )


def test_rp1f10_underspecified_fact_is_insufficient() -> None:
    # marketing "100 W/ch" with zero conditions (issue #586 §4)
    product = _product()
    fact = _amp_fact(product)
    suff, missing = evaluate_fact_sufficiency(
        fact, _rp1_profile().field_spec('continuous_power_w_8ohm')
    )
    assert suff == 'insufficient'
    assert set(missing) == {
        'load_impedance_ohm',
        'channels_driven',
        'duration_kind',
    }
    evaluation = evaluate_product_suitability(
        document_id='d',
        product=product,
        facts=[fact],
        requirement=PerformanceRequirement(
            kind='amplifier_capability',
            params={'load_ohm': 8.0, 'required_w': 50.0},
        ),
        evaluated_at_utc=_TS,
    )
    assert evaluation.verdict == 'insufficient_data'
    assert 'amplifier_load_condition_missing' in evaluation.reason_codes


def test_rp1f20_condition_complete_fact_is_eligible() -> None:
    product = _product()
    fact = _amp_fact(
        product,
        load_impedance_ohm=8.0,
        channels_driven=2,
        duration_kind='continuous',
    )
    suff, missing = evaluate_fact_sufficiency(
        fact, _rp1_profile().field_spec('continuous_power_w_8ohm')
    )
    assert suff == 'engineering_grade'
    assert missing == ()
    evaluation = evaluate_product_suitability(
        document_id='d',
        product=product,
        facts=[fact],
        requirement=PerformanceRequirement(
            kind='amplifier_capability',
            params={'load_ohm': 8.0, 'required_w': 50.0},
        ),
        evaluated_at_utc=_TS,
    )
    assert evaluation.verdict == 'eligible'


def test_rp1f30_condition_mismatch_limits_evidence() -> None:
    # FR fact missing axis/distance declarations -> limited, not promoted
    product = _product()
    fact = build_performance_fact(
        document_id='d',
        product=product,
        quantity_kind='frequency_response',
        value_kind='range',
        range_min=28.0,
        range_max=20000.0,
        unit='Hz',
        evidence_class='manufacturer_datasheet',
        domain=FrequencyDomain(minimum_hz=28.0, maximum_hz=20000.0),
        conditions=PerformanceFactConditions(environment='anechoic'),
    )
    suff, missing = evaluate_fact_sufficiency(fact)
    assert suff in ('limited', 'insufficient')
    assert 'measurement_distance_m' in missing
    evaluation = evaluate_product_suitability(
        document_id='d',
        product=product,
        facts=[fact],
        requirement=PerformanceRequirement(
            kind='bandwidth',
            params={'required_low_hz': 30.0, 'required_high_hz': 18000.0},
        ),
        evaluated_at_utc=_TS,
    )
    assert evaluation.verdict == 'eligible_with_limitations'
    assert 'bandwidth_facts_limited_conditions' in evaluation.reason_codes


def test_rp1f40_conflicting_sources_coexist() -> None:
    product = _product()
    manufacturer = build_performance_fact(
        document_id='d',
        product=product,
        quantity_kind='max_output',
        value_kind='scalar',
        scalar_value=120.0,
        unit='dB SPL',
        evidence_class='manufacturer_datasheet',
        conditions=PerformanceFactConditions(measurement_distance_m=1.0),
    )
    independent = build_performance_fact(
        document_id='d',
        product=product,
        quantity_kind='max_output',
        value_kind='scalar',
        scalar_value=112.0,
        unit='dB SPL',
        evidence_class='independent_lab',
        conditions=PerformanceFactConditions(measurement_distance_m=1.0),
    )
    view = reconcile_conflicting_facts(
        product_id=product.product_id,
        quantity_kind='max_output',
        facts=[manufacturer, independent],
    )
    assert view['state'] == 'coexisting'
    assert view['fact_count'] == 2
    assert view['scalar_disagreement'] is True
    assert set(view['by_evidence_class']) == {
        'manufacturer_datasheet',
        'independent_lab',
    }


def test_rp1f50_review_to_final_rebind_preserves_history(tmp_path) -> None:
    review = _rp1_profile(maturity='industry_review')
    final = register_performance_facts_profile(
        document_id='d',
        publisher='CEDIA',
        family='rp1_1_loudspeakers',
        document_reference='CEDIA RP1-1 Loudspeaker Performance Facts',
        revision_or_date='1.0-published-2026-02',
        maturity_state='final_published',
        source_reference='https://cedia.org/rp1-1',
        field_mapping_version='htdt-rp1-map-1',
        field_definitions=review.field_definitions,
    )
    assert review.maturity_state == 'industry_review'
    assert final.maturity_state == 'final_published'
    # incomplete disposition set -> refused
    with pytest.raises(ValueError):
        build_profile_rebind(
            document_id='d',
            from_profile=review,
            to_profile=final,
            field_dispositions=(),
            rebound_import_ids=(),
            decided_at_utc=_TS,
        )
    rebind = build_profile_rebind(
        document_id='d',
        from_profile=review,
        to_profile=final,
        field_dispositions=(
            PerformanceFieldDispositionRecord(
                field_name='max_output_db_spl_1m',
                disposition='unchanged',
            ),
            PerformanceFieldDispositionRecord(
                field_name='continuous_power_w_8ohm',
                disposition='unchanged',
            ),
        ),
        rebound_import_ids=(),
        decided_at_utc=_TS,
    )
    repo = CadPerformanceFactsRepository(_scene_repo(tmp_path))
    repo.save_profile(review)
    repo.save_profile(final)
    repo.save_rebind(rebind)
    loaded = repo.get_rebind(rebind.rebind_id)
    assert loaded is not None
    assert loaded.from_profile_id == review.profile_id
    # historical profile record is untouched
    assert repo.get_profile(review.profile_id).maturity_state == (
        'industry_review'
    )


def test_rp1f60_rp22_consumption_never_promotes_manufacturer() -> None:
    product = _product()
    fact = _amp_fact(
        product,
        load_impedance_ohm=8.0,
        channels_driven=2,
        duration_kind='continuous',
    )
    obs = rp22_consumption_view([fact], 'parameter_12')
    assert obs.evidence_class == 'design_prediction'
    assert obs.basis == 'modelled_with_output_limits'
    assert obs.evidence_ref == fact.fact_id
    empty = rp22_consumption_view([], 'parameter_12')
    assert empty.value is None
    assert empty.evidence_class == 'unknown'


# ---------------------------------------------------------------------------
# RP1 persistence + integrity
# ---------------------------------------------------------------------------


def test_rp1_repository_roundtrip_and_bindings(tmp_path) -> None:
    repo = CadPerformanceFactsRepository(_scene_repo(tmp_path))
    profile = _rp1_profile()
    product = _product()
    fact = _amp_fact(
        product,
        load_impedance_ohm=8.0,
        channels_driven=2,
        duration_kind='continuous',
    )
    run = build_performance_facts_import(
        document_id='d',
        profile=profile,
        extraction_state='machine_readable',
        facts=[fact],
        source_asset_sha256=sha256(b'asset').hexdigest(),
        source_label='facts.json',
        unmapped_fields=('marketing_tagline',),
        unsupported_fields=('future_field_x',),
        imported_at_utc=_TS,
    )
    repo.save_profile(profile)
    repo.save_product(product)
    repo.save_fact(fact)
    repo.save_import(run)
    assert repo.get_profile(profile.profile_id) is not None
    assert repo.get_fact(fact.fact_id).product_id == product.product_id
    assert repo.get_import(run.import_id).unmapped_fields == (
        'marketing_tagline',
    )
    # a fact referencing an unstored product cannot persist
    ghost = build_product_identity(
        document_id='d', manufacturer='Ghost', model='X'
    )
    ghost_fact = _amp_fact(ghost)
    with pytest.raises(PerformanceFactsIntegrityError):
        repo.save_fact(ghost_fact)
    # sealed forgery rejected
    forged = product.model_copy(update={'model': 'SUB-1X'})
    with pytest.raises(PerformanceFactsIntegrityError):
        repo.save_product(forged)


def test_rp1_import_preserves_unknown_future_fields() -> None:
    profile = _rp1_profile()
    product = _product()
    fact = build_performance_fact(
        document_id='d',
        product=product,
        quantity_kind='other_declared',
        value_kind='text',
        text_value='future datum',
        evidence_class='rp1_profiled_manufacturer_data',
        source_field_name='rp1_1_future_field',
    )
    run = build_performance_facts_import(
        document_id='d',
        profile=profile,
        extraction_state='machine_readable',
        facts=[fact],
        imported_at_utc=_TS,
    )
    assert any('future_field' in w for w in run.warnings)
