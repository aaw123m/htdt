"""REV59-DISPLAY3 regression tests — #660 gradation, #688 colour
volume, #672 spatial resolution, #756 low-luminance, #759 dynamic
contrast, #760 display-wall boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_display_authority_repository import (
    CadDisplayAuthorityRepository,
    DisplayAuthorityIntegrityError,
)
from htdt.cad_display_boundary import (
    DisplayWallBoundary,
    WallAcousticImpact,
    evaluate_wall_claim,
)
from htdt.cad_display_fidelity import (
    ColourVolumeMeasurement,
    DisplayedGradationObservation,
    SpatialResolutionEvidence,
    evaluate_display_fidelity_claim,
)
from htdt.cad_display_metrology import (
    DynamicContrastMeasurement,
    LowLuminanceCapability,
    evaluate_metrology_claim,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    ensure_native_schema,
)


def _ref(rid: str = 'doc-1') -> AuthorityRef:
    return AuthorityRef(kind='doc', ref_id=rid, ref_sha256='a' * 64)


def _repo(tmp_path: Path) -> CadDisplayAuthorityRepository:
    db = tmp_path / 'scene.htdtscene'
    ensure_native_schema(db)
    return CadDisplayAuthorityRepository(SceneRepository(db))


def _dgo(**kw) -> DisplayedGradationObservation:
    payload = dict(
        document_id='doc-1',
        stimulus_ref=_ref('stim-1'),
        range_semantics='full',
        banding_observed=False,
    )
    payload.update(kw)
    return DisplayedGradationObservation.create(payload)


def _cvol(**kw) -> ColourVolumeMeasurement:
    payload = dict(
        document_id='doc-1',
        colour_space='ictcp',
        method='idms_v1_3_gamut_volume',
        measurement_ref=_ref('meas-1'),
    )
    payload.update(kw)
    return ColourVolumeMeasurement.create(payload)


def _sres(**kw) -> SpatialResolutionEvidence:
    payload = dict(
        document_id='doc-1',
        method='contrast_modulation',
        resolving_element='1lp/deg',
        measurement_ref=_ref('meas-2'),
    )
    payload.update(kw)
    return SpatialResolutionEvidence.create(payload)


def _llc(**kw) -> LowLuminanceCapability:
    payload = dict(
        document_id='doc-1',
        minimum_luminance_cd_m2=0.001,
        stray_light_control='elimination_tube',
    )
    payload.update(kw)
    return LowLuminanceCapability.create(payload)


def _dcm(**kw) -> DynamicContrastMeasurement:
    payload = dict(
        document_id='doc-1',
        contrast_kind='native_sequential',
        stimulus_ref=_ref('stim-2'),
        control_mode='iris_manual',
    )
    payload.update(kw)
    return DynamicContrastMeasurement.create(payload)


def _dwb(**kw) -> DisplayWallBoundary:
    payload = dict(
        document_id='doc-1',
        wall_kind='led_wall',
        area_m2=9.0,
        behind_speaker_placement=False,
        acoustic_transparency_claim='opaque',
    )
    payload.update(kw)
    return DisplayWallBoundary.create(payload)


def _wai(**kw) -> WallAcousticImpact:
    payload = dict(
        document_id='doc-1',
        boundary_ref=_ref('b-1'),
        reflection_added=True,
    )
    payload.update(kw)
    return WallAcousticImpact.create(payload)


def test_gradation_requires_stimulus_and_range():
    with pytest.raises(ValueError):
        _dgo(stimulus_ref=None)
    with pytest.raises(ValueError):
        _dgo(range_semantics='unknown')


def test_volume_requires_space_method_and_measurement():
    with pytest.raises(ValueError):
        _cvol(colour_space='unknown')
    with pytest.raises(ValueError):
        _cvol(measurement_ref=None)
    with pytest.raises(ValueError, match='reference'):
        _cvol(coverage_fraction=0.8)


def test_resolution_requires_method_and_measurement():
    with pytest.raises(ValueError):
        _sres(method='unknown')
    with pytest.raises(ValueError):
        _sres(measurement_ref=None)


def test_capability_requires_stray_light_control():
    with pytest.raises(ValueError):
        _llc(stray_light_control='unknown')
    with pytest.raises(ValueError, match='minimum'):
        _llc(stray_light_control='none',
             minimum_luminance_cd_m2=None)


def test_dynamic_contrast_requires_context():
    with pytest.raises(ValueError):
        _dcm(contrast_kind='unknown')
    with pytest.raises(ValueError, match='adaptation'):
        _dcm(contrast_kind='dynamic_advertised',
             adaptation_history=None)
    with pytest.raises(ValueError):
        _dcm(stimulus_ref=None)


def test_wall_requires_kind_and_transparency_evidence():
    with pytest.raises(ValueError):
        _dwb(wall_kind='unknown')
    with pytest.raises(ValueError, match='transmission'):
        _dwb(behind_speaker_placement=True,
             acoustic_transparency_claim='perforated')
    w = _dwb(behind_speaker_placement=True,
             acoustic_transparency_claim='perforated',
             transmission_evidence_ref=_ref('tl-1'))
    assert w.behind_speaker_placement is True


def test_impact_requires_boundary_and_measurement():
    with pytest.raises(ValueError):
        _wai(boundary_ref=None)
    with pytest.raises(ValueError, match='measurement'):
        _wai(transmission_loss_db=3.0)


def test_fidelity_claim_ladder():
    g, v, r = _dgo(), _cvol(), _sres()
    g_bad = _dgo(banding_observed=True)
    assert evaluate_display_fidelity_claim(
        g_bad, v, r)[0] == 'range_semantics_unqualified'
    assert evaluate_display_fidelity_claim(
        None, v, r, nominal_10bit_declared=True)[0] == (
        'nominal_spec_is_not_result')
    assert evaluate_display_fidelity_claim(
        g, None, r, gamut_coverage_2d_pct=95.0)[0] == (
        'triangle_is_not_volume')
    assert evaluate_display_fidelity_claim(
        g, v, None, nominal_4k_declared=True)[0] == (
        'raster_is_not_resolution')
    assert evaluate_display_fidelity_claim(g, v, r)[0] == (
        'qualified_fidelity')


def test_metrology_claim_ladder():
    c, m = _llc(), _dcm()
    assert evaluate_metrology_claim(
        None, m, meter_calibrated=True)[0] == (
        'calibrated_meter_is_not_black_evidence')
    assert evaluate_metrology_claim(c, None)[0] == (
        'capability_unqualified')
    m_dyn = _dcm(contrast_kind='dynamic_advertised',
                 control_mode='dynamic_light_control',
                 adaptation_history='prior white frame')
    assert evaluate_metrology_claim(c, m_dyn)[0] == (
        'dynamic_is_not_stable_quantity')
    assert evaluate_metrology_claim(c, m)[0] == (
        'qualified_metrology')


def test_wall_claim_ladder():
    b, i = _dwb(), _wai()
    assert evaluate_wall_claim(None, i)[0] == (
        'video_wall_is_also_acoustic')
    b_tr = _dwb(behind_speaker_placement=True,
                acoustic_transparency_claim='perforated',
                transmission_evidence_ref=_ref('tl-1'))
    assert evaluate_wall_claim(b, None)[0] == 'impact_unbounded'
    assert evaluate_wall_claim(b_tr, i)[0] == 'qualified_boundary'


def test_roundtrip_all_seven(tmp_path):
    repo = _repo(tmp_path)
    recs = (_dgo(), _cvol(), _sres(), _llc(), _dcm(), _dwb(), _wai())
    repo.save_gradation_observation(recs[0])
    repo.save_colour_volume(recs[1])
    repo.save_resolution_evidence(recs[2])
    repo.save_luminance_capability(recs[3])
    repo.save_contrast_measurement(recs[4])
    repo.save_wall_boundary(recs[5])
    repo.save_wall_impact(recs[6])
    assert repo.get_gradation_observation(
        recs[0].observation_id) == recs[0]
    assert repo.get_colour_volume(recs[1].volume_id) == recs[1]
    assert repo.get_resolution_evidence(
        recs[2].evidence_id) == recs[2]
    assert repo.get_luminance_capability(
        recs[3].capability_id) == recs[3]
    assert repo.get_contrast_measurement(
        recs[4].measurement_id) == recs[4]
    assert repo.get_wall_boundary(recs[5].boundary_id) == recs[5]
    assert repo.get_wall_impact(recs[6].impact_id) == recs[6]


def test_tamper_detected(tmp_path):
    repo = _repo(tmp_path)
    v = _cvol()
    repo.save_colour_volume(v)
    import sqlite3
    with sqlite3.connect(repo.path) as c:
        c.execute(
            'UPDATE cad_colour_volume_measurements SET '
            'colour_space=? WHERE volume_id=?',
            ('cie_lab', v.volume_id),
        )
    with pytest.raises(DisplayAuthorityIntegrityError):
        repo.get_colour_volume(v.volume_id)


def test_fresh_migrate(tmp_path):
    db = tmp_path / 'fresh.htdtscene'
    version = ensure_native_schema(db)
    assert version == NATIVE_SCHEMA_VERSION
    repo = CadDisplayAuthorityRepository(SceneRepository(db))
    g = _dgo()
    repo.save_gradation_observation(g)
    assert repo.get_gradation_observation(g.observation_id) == g
