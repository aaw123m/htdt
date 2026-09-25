from __future__ import annotations

from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from htdt.cad_drawing_set import (
    DrawingDatum,
    InstallationDrawingSet,
    build_drawing_set_spec,
    generate_drawing_set,
)
from htdt.report import (
    InstallationAuthorityBinding,
    InstallationDimensionSheet,
    InstallationEntityOutput,
    InstallationOutput,
    InstallationViewOutline,
)


NOW = '2026-09-24T00:00:00+00:00'
HASH = 'a' * 64


def _entity(entity_id: str, kind: str, x: float, y: float, z: float,
            **kw) -> InstallationEntityOutput:
    payload = dict(
        entity_id=entity_id,
        entity_kind=kind,
        name=kw.pop('name', entity_id),
        x_m=x, y_m=y, z_m=z,
        body_yaw_deg=0.0, body_pitch_deg=0.0, body_roll_deg=0.0,
    )
    payload.update(kw)
    return InstallationEntityOutput(**payload)


def _point(sheet: InstallationDimensionSheet):
    return sheet


def _output() -> InstallationOutput:
    entities = (
        _entity('spk-fl', 'speaker', 1.0, 0.5, 1.2,
                name='Front left', body_geometry_kind='box'),
        _entity('sub-1', 'subwoofer', 4.0, 0.5, 0.3, name='Sub'),
        _entity('scr-1', 'screen', 2.5, 0.1, 1.5, name='Screen',
                body_geometry_kind='box'),
        _entity('seat-1', 'seat', 2.5, 3.5, 0.9, name='Seat A'),
        _entity('atmos-1', 'speaker', 2.0, 2.0, 3.0,
                name='Atmos L', body_geometry_kind='box'),
    )
    dims = (
        InstallationDimensionSheet(
            view='top', horizontal_axis='x', vertical_axis='y',
            horizontal_min_m=0.0, horizontal_max_m=5.0,
            vertical_min_m=0.0, vertical_max_m=5.0,
            points=(),
        ),
        InstallationDimensionSheet(
            view='front', horizontal_axis='x', vertical_axis='z',
            horizontal_min_m=0.0, horizontal_max_m=5.0,
            vertical_min_m=0.0, vertical_max_m=3.2,
            points=(),
        ),
        InstallationDimensionSheet(
            view='side', horizontal_axis='y', vertical_axis='z',
            horizontal_min_m=0.0, horizontal_max_m=5.0,
            vertical_min_m=0.0, vertical_max_m=3.2,
            points=(),
        ),
    )
    authority = InstallationAuthorityBinding(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=HASH,
        effective_scene_content_hash=HASH,
    )
    payload = {
        'schema_version': 1,
        'authority_version': 'installation-output-1',
        'coordinate_system': 'htdt-x-right-y-rear-z-up-m',
        'authority': authority.model_dump(mode='json'),
        'evidence': [],
        'entities': [e.model_dump(mode='json') for e in entities],
        'dimensions': [d.model_dump(mode='json') for d in dims],
        'sections': [],
    }
    semantic = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()
    return InstallationOutput(
        schema_version=1,
        authority_version='installation-output-1',
        authority=authority,
        evidence=(),
        entities=entities,
        dimensions=dims,
        sections=(),
        semantic_sha256=semantic,
    )


def _datums():
    return (
        DrawingDatum(datum_id='ffl', kind='finished_floor', axis='z',
                     offset_m=0.0, label='FFL +0.000'),
        DrawingDatum(datum_id='fw', kind='front_wall', axis='y',
                     offset_m=0.0, label='Front wall'),
        DrawingDatum(datum_id='cl', kind='room_centerline', axis='x',
                     offset_m=2.5, label='Room CL'),
    )


def _spec(**kw):
    payload = dict(
        spec_id='ds-1',
        spec_version='1',
        sheets=('floor_plan', 'front_elevation', 'side_elevation', 'rcp'),
        page_size='a4',
        orientation='landscape',
        scale_policy='fit',
    )
    payload.update(kw)
    return build_drawing_set_spec(**payload)


def test_all_four_sheets_render_deterministically():
    out = _output()
    ds = generate_drawing_set(
        output=out, spec=_spec(), datums=_datums(),
        project_label='HTDT demo', generated_at_utc=NOW,
    )
    assert isinstance(ds, InstallationDrawingSet)
    assert {s.kind for s in ds.sheets} == {
        'floor_plan', 'front_elevation', 'side_elevation', 'rcp'
    }
    again = generate_drawing_set(
        output=out, spec=_spec(), datums=_datums(),
        project_label='HTDT demo', generated_at_utc=NOW,
    )
    assert [s.sheet_content_sha256 for s in ds.sheets] == [
        s.sheet_content_sha256 for s in again.sheets
    ]
    floor = ds.sheet('floor_plan')
    svg = floor.to_svg()
    assert svg.startswith('<svg')
    assert 'NTS' in svg  # fit policy never labelled 1:50
    assert 'title-block' in svg


def test_rcp_only_shows_ceiling_plane_entities():
    ds = generate_drawing_set(
        output=_output(), spec=_spec(sheets=('rcp',), rcp_ceiling_inset_m=0.6),
        datums=_datums(), project_label='p', generated_at_utc=NOW,
    )
    rcp = ds.sheet('rcp')
    texts = [p.text for p in rcp.primitives if p.kind == 'text']
    # only the atmos speaker at z=3.0 is within 0.6 m of the 3.2 m ceiling
    assert any('Atmos' in t for t in texts if t)
    assert not any('Front left' in t for t in texts if t)


def test_dimensions_name_datum_and_referenced_point():
    ds = generate_drawing_set(
        output=_output(), spec=_spec(sheets=('floor_plan',)),
        datums=_datums(), project_label='p', generated_at_utc=NOW,
    )
    dims = [p.text for p in ds.sheet('floor_plan').primitives
            if p.kind == 'dimension']
    assert dims
    joined = ' | '.join(dims)
    assert 'Front wall' in joined and 'Room CL' in joined
    assert 'acoustic reference' in joined  # speaker entities dimensioned to ref
    assert 'image center' in joined       # screen entity


def test_mm_sheets_convert_offsets_to_millimeters():
    # spk-fl sits at y=0.5 — 500 mm off the front-wall datum at y=0
    ds_m = generate_drawing_set(
        output=_output(), spec=_spec(sheets=('floor_plan',), unit='m'),
        datums=_datums(), project_label='p', generated_at_utc=NOW,
    )
    text_m = ' | '.join(
        p.text for p in ds_m.sheet('floor_plan').primitives
        if p.kind == 'dimension'
    )
    assert '0.5 m from Front wall' in text_m

    ds_mm = generate_drawing_set(
        output=_output(), spec=_spec(sheets=('floor_plan',), unit='mm'),
        datums=_datums(), project_label='p', generated_at_utc=NOW,
    )
    text_mm = ' | '.join(
        p.text for p in ds_mm.sheet('floor_plan').primitives
        if p.kind == 'dimension'
    )
    assert '500 mm from Front wall' in text_mm
    assert '0.5 mm' not in text_mm


def _outlined_output() -> InstallationOutput:
    out = _output()
    outlines = (
        InstallationViewOutline(
            view='top',
            basis='exact_body_geometry',
            polygons_m=(((0.75, 0.3), (1.25, 0.3), (1.0, 0.8)),),
        ),
        InstallationViewOutline(
            view='front',
            basis='exact_body_geometry',
            polygons_m=(((0.75, 1.0), (1.25, 1.0), (1.25, 1.4), (0.75, 1.4)),),
        ),
        InstallationViewOutline(
            view='side',
            basis='bounding_envelope',
            polygons_m=(((0.25, 1.0), (0.75, 1.0), (0.75, 1.4), (0.25, 1.4)),),
        ),
    )
    entities = tuple(
        item.model_copy(update={'outlines': outlines})
        if item.entity_id == 'spk-fl'
        else item
        for item in out.entities
    )
    provisional = out.model_copy(update={'entities': entities})
    payload = provisional.identity_payload()
    semantic = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()
    return InstallationOutput(
        **{
            **out.model_dump(mode='python'),
            'entities': entities,
            'semantic_sha256': semantic,
        }
    )


def test_exact_outlines_draw_polygons_envelope_draws_dashed():
    ds = generate_drawing_set(
        output=_outlined_output(), spec=_spec(sheets=('floor_plan', 'side_elevation')),
        datums=(), project_label='p', generated_at_utc=NOW,
    )
    floor = ds.sheet('floor_plan')
    polys = [p for p in floor.primitives if p.kind == 'polygon']
    assert len(polys) == 1
    assert not polys[0].dashed
    svg = floor.to_svg()
    assert '<polygon' in svg

    side = ds.sheet('side_elevation')
    side_polys = [p for p in side.primitives if p.kind == 'polygon']
    assert len(side_polys) == 1
    assert side_polys[0].dashed
    legend = [p.text for p in side.primitives
              if p.kind == 'text' and p.text and 'bounding-envelope' in p.text]
    assert legend  # the sheet states the envelope basis


def test_fixed_scale_label_and_overflow_flag():
    ok = generate_drawing_set(
        output=_output(),
        spec=_spec(sheets=('floor_plan',), scale_policy='fixed',
                   fixed_scale_denominator=50, page_size='a3'),
        datums=(), project_label='p', generated_at_utc=NOW,
    )
    assert ok.sheet('floor_plan').title_block.scale_label == '1:50'
    # a4 landscape usable width is 297-30 = 267 mm
    room = next(p for p in ok.sheet('floor_plan').primitives
                if p.kind == 'rect' and p.layer == 'room')
    assert room.data[2] == pytest.approx(100.0)  # 5 m at 1:50
    overflow = generate_drawing_set(
        output=_output(),
        spec=_spec(sheets=('floor_plan',), scale_policy='fixed',
                   fixed_scale_denominator=10, page_size='a4'),
        datums=(), project_label='p', generated_at_utc=NOW,
    )
    sheet = overflow.sheet('floor_plan')
    assert sheet.needs_review
    assert 'fixed 1:10' in ' '.join(sheet.review_reasons)
    # the sheet is flagged but NOT silently rescaled — geometry stays at
    # the declared 1:10 so the label is never a lie
    room = next(p for p in sheet.primitives
                if p.kind == 'rect' and p.layer == 'room')
    assert room.data[2] == pytest.approx(500.0)  # 5 m at 1:10


def test_spec_versioning_and_validation():
    spec = _spec()
    other = _spec(sheets=('floor_plan',), spec_version='2')
    assert spec.spec_semantic_hash != other.spec_semantic_hash
    with pytest.raises(ValidationError):
        _spec(scale_policy='fixed', fixed_scale_denominator=None)
    with pytest.raises(ValidationError):
        _spec(sheets=('floor_plan', 'floor_plan'))


def test_lifecycle_states_are_styled_distinctly():
    ds = generate_drawing_set(
        output=_output(), spec=_spec(sheets=('floor_plan',)),
        datums=(), project_label='p', generated_at_utc=NOW,
        entity_states={'spk-fl': 'proposed', 'seat-1': 'as_built'},
    )
    svg = ds.sheet('floor_plan').to_svg()
    assert 'stroke-dasharray="2,1.5"' in svg  # proposed dashed


def test_generated_timestamp_not_in_content_hash():
    out = _output()
    a = generate_drawing_set(output=out, spec=_spec(sheets=('floor_plan',)),
                             datums=(), project_label='p',
                             generated_at_utc='2026-01-01T00:00:00+00:00')
    b = generate_drawing_set(output=out, spec=_spec(sheets=('floor_plan',)),
                             datums=(), project_label='p',
                             generated_at_utc='2026-06-06T00:00:00+00:00')
    assert (a.sheet('floor_plan').sheet_content_sha256
            == b.sheet('floor_plan').sheet_content_sha256)
    # but the timestamp still renders on the sheet
    assert '2026-06-06' in b.sheet('floor_plan').to_svg()


def test_output_hash_pinned_to_drawing_set():
    ds = generate_drawing_set(
        output=_output(), spec=_spec(sheets=('floor_plan',)),
        datums=(), project_label='p', generated_at_utc=NOW,
    )
    out = _output()
    assert ds.installation_output_sha256 == out.semantic_sha256
    assert ds.spec_sha256 == _spec(sheets=('floor_plan',)).spec_semantic_hash


def test_output_with_outlines_binds_them_into_semantic_hash():
    assert (
        _outlined_output().semantic_sha256
        != _output().semantic_sha256
    )
