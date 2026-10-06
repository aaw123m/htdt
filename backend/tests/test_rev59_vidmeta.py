"""REV59-VIDMETA regression tests — projector dynamic light (#759),
low-luminance metrology (#756), display acoustic boundary (#760),
codec/transcode fidelity (#753/#747)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_codec_fidelity import (
    CodecChainProfile,
    CodecFidelityObservation,
    QualityMethodProfile,
    evaluate_fidelity_claim,
)
from htdt.cad_display_acoustic_boundary import (
    DisplayAcousticBoundaryProfile,
    FrontStageVariantRecord,
    evaluate_frontstage_claim,
)
from htdt.cad_low_luminance import (
    DisplayLightMeasurementCapability,
    LowLuminanceObservation,
    evaluate_black_claim,
    evaluate_contrast_claim,
)
from htdt.cad_media_fidelity_repository import (
    CadMediaFidelityRepository,
    MediaFidelityIntegrityError,
)
from htdt.cad_projector_dynamic_light import (
    DynamicContrastQualification,
    ProjectorDynamicLightProfile,
    TemporalContrastMeasurement,
    compare_contrast_claims,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.canonical_json import canonical_sha256


DOC = 'doc-vidmeta'
_SHA = canonical_sha256({'fixture': 'sha'})


def _ref(kind: str, rid: str = 'x1', sha: str = _SHA) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=rid, ref_sha256=sha)


def _light_profile(**kw) -> ProjectorDynamicLightProfile:
    payload = dict(
        document_id=DOC,
        light_modes=('laser_dynamic',),
        device_ref=_ref('device', 'proj-1'),
    )
    payload.update(kw)
    return ProjectorDynamicLightProfile.create(**payload)


def _contrast(**kw) -> TemporalContrastMeasurement:
    payload = dict(
        document_id=DOC,
        measurand='sequential_dynamic',
        stimulus='full_white_black_fields',
        light_mode='laser_dynamic',
        value=5000.0,
    )
    payload.update(kw)
    return TemporalContrastMeasurement.create(**payload)


def _capability(**kw) -> DisplayLightMeasurementCapability:
    payload = dict(
        document_id=DOC,
        min_measurable_cd_m2=0.001,
        stray_light_control='stray_light_elimination_tube',
        instrument_ref=_ref('instrument', 'k10'),
    )
    payload.update(kw)
    return DisplayLightMeasurementCapability.create(**payload)


def _llo(**kw) -> LowLuminanceObservation:
    cap = _capability()
    payload = dict(
        document_id=DOC,
        capability_ref=_ref(
            'light_capability', cap.capability_id,
            cap.capability_sha256),
        reading_cd_m2=0.002,
    )
    payload.update(kw)
    return LowLuminanceObservation.create(**payload)


def _boundary(**kw) -> DisplayAcousticBoundaryProfile:
    payload = dict(
        document_id=DOC,
        surface_area_m2=8.0,
        transmission='opaque',
    )
    payload.update(kw)
    return DisplayAcousticBoundaryProfile.create(**payload)


def _variant(**kw) -> FrontStageVariantRecord:
    b = _boundary()
    payload = dict(
        document_id=DOC,
        boundary_ref=_ref(
            'display_boundary', b.profile_id, b.profile_sha256),
        strategy='behind_screen_lcr',
        speaker_refs=(_ref('speaker', 'sp-l'), _ref('speaker', 'sp-c')),
        verdict='placement_blocked',
    )
    payload.update(kw)
    return FrontStageVariantRecord.create(**payload)


def _chain(**kw) -> CodecChainProfile:
    payload = dict(
        document_id=DOC,
        media_kind='video',
        codec_steps=('lossy',),
    )
    payload.update(kw)
    return CodecChainProfile.create(**payload)


def _method(**kw) -> QualityMethodProfile:
    payload = dict(
        document_id=DOC,
        method_kind='j247_full_reference',
        media_kind='video',
        reference_required=True,
    )
    payload.update(kw)
    return QualityMethodProfile.create(**payload)


def _cfo(**kw) -> CodecFidelityObservation:
    c = _chain()
    m = _method()
    payload = dict(
        document_id=DOC,
        chain_ref=_ref('codec_chain', c.profile_id, c.profile_sha256),
        method_ref=_ref('quality_method', m.method_id, m.method_sha256),
        impairment_detected=False,
    )
    payload.update(kw)
    return CodecFidelityObservation.create(**payload)


class TestDynamicLight:
    def test_sealed_create(self) -> None:
        p = _light_profile()
        assert p.profile_id.startswith('pdl-')

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _light_profile(light_modes=('unknown',))
        with pytest.raises(ValidationError):
            _light_profile(light_modes=())

    def test_measurand_and_stimulus_required(self) -> None:
        with pytest.raises(ValidationError):
            _contrast(measurand='unknown')
        with pytest.raises(ValidationError):
            _contrast(stimulus='unknown')

    def test_sequential_needs_dynamic_mode(self) -> None:
        with pytest.raises(ValidationError):
            _contrast(light_mode='native_fixed')

    def test_measurand_mismatch_incomparable(self) -> None:
        a = _contrast()
        b = _contrast(measurand='native_static',
                      light_mode='native_fixed', value=2000.0)
        verdict, reason = compare_contrast_claims(a, b)
        assert verdict == 'incomparable'
        assert reason == 'measurand_mismatch'

    def test_same_measurand_comparable(self) -> None:
        verdict, _ = compare_contrast_claims(_contrast(), _contrast())
        assert verdict == 'comparable'

    def test_qualification_comparable_needs_same_measurand(self) -> None:
        with pytest.raises(ValidationError):
            DynamicContrastQualification.create(
                document_id=DOC,
                claimed_measurand='native_static',
                measured_measurand='sequential_dynamic',
                measurement_ref=_ref('measurement', 'm-1'),
                verdict='comparable',
            )


class TestLowLuminance:
    def test_sealed_create(self) -> None:
        c = _capability()
        assert c.capability_id.startswith('lmc-')

    def test_zero_floor_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _capability(min_measurable_cd_m2=0.0)

    def test_observation_requires_capability_ref(self) -> None:
        with pytest.raises(ValidationError):
            _llo(capability_ref=AuthorityRef(
                kind='light_capability', ref_id='x', ref_sha256=None))

    def test_below_floor_not_zero(self) -> None:
        obs = _llo(reading_cd_m2=0.0005,
                   visible_luminance_present=True)
        verdict, reason = evaluate_black_claim(obs, _capability())
        assert verdict == 'below_capability'
        assert reason == 'visible_below_floor_not_zero'

    def test_uncontrolled_stray_contaminated(self) -> None:
        verdict, _ = evaluate_black_claim(
            _llo(), _capability(stray_light_control='uncontrolled'))
        assert verdict == 'contaminated'

    def test_no_evidence_insufficient(self) -> None:
        verdict, _ = evaluate_black_claim(None, _capability())
        assert verdict == 'insufficient_evidence'

    def test_contrast_lower_bound_only(self) -> None:
        verdict, _ = evaluate_contrast_claim(100.0, 'below_capability')
        assert verdict == 'lower_bound_only'

    def test_contrast_finite_when_measured(self) -> None:
        verdict, _ = evaluate_contrast_claim(100.0, 'measured_black')
        assert verdict == 'finite_contrast'


class TestDisplayBoundary:
    def test_sealed_create(self) -> None:
        b = _boundary()
        assert b.profile_id.startswith('dab-')

    def test_transparent_needs_evidence(self) -> None:
        with pytest.raises(ValidationError):
            _boundary(transmission='acoustically_transparent')
        b = _boundary(
            transmission='acoustically_transparent',
            transmission_evidence_ref=_ref('evidence', 'e-1'))
        assert b.transmission == 'acoustically_transparent'

    def test_opaque_blocks_behind_lcr(self) -> None:
        verdict, reason = evaluate_frontstage_claim(
            _boundary(), 'behind_screen_lcr')
        assert verdict == 'placement_blocked'
        assert reason == 'opaque_wall_blocks_behind'

    def test_transparent_supports_behind(self) -> None:
        b = _boundary(
            transmission='acoustically_transparent',
            transmission_evidence_ref=_ref('evidence', 'e-1'))
        verdict, _ = evaluate_frontstage_claim(b, 'behind_screen_lcr')
        assert verdict == 'placement_supported'

    def test_unknown_transmission_needs_evidence(self) -> None:
        verdict, _ = evaluate_frontstage_claim(
            _boundary(transmission='unknown'), 'behind_screen_lcr')
        assert verdict == 'transmission_evidence_required'

    def test_supported_variant_needs_speakers(self) -> None:
        with pytest.raises(ValidationError):
            _variant(verdict='placement_supported', speaker_refs=())


class TestCodecFidelity:
    def test_sealed_create(self) -> None:
        c = _chain()
        assert c.profile_id.startswith('cfp-')

    def test_unknown_step_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _chain(codec_steps=('unknown',))

    def test_lossless_chain_no_hidden(self) -> None:
        with pytest.raises(ValidationError):
            _chain(
                codec_steps=('lossless',),
                hidden_processing_detected=True)

    def test_full_reference_requires_reference(self) -> None:
        with pytest.raises(ValidationError):
            _method(reference_required=False)

    def test_bit_transparent_fails_on_lossy(self) -> None:
        verdict, _ = evaluate_fidelity_claim(
            'bit_transparent', _chain(), ())
        assert verdict == 'not_assessed'

    def test_bit_transparent_lossless(self) -> None:
        c = _chain(codec_steps=('lossless', 'passthrough'))
        verdict, _ = evaluate_fidelity_claim(
            'bit_transparent', c, ())
        assert verdict == 'bit_transparent'

    def test_perceptual_needs_evidence(self) -> None:
        verdict, _ = evaluate_fidelity_claim(
            'perceptually_transparent', _chain(), ())
        assert verdict == 'insufficient_evidence'
        verdict, _ = evaluate_fidelity_claim(
            'perceptually_transparent', _chain(), (_cfo(),))
        assert verdict == 'perceptually_transparent'

    def test_impairment_downgrades(self) -> None:
        obs = _cfo(impairment_detected=True)
        verdict, _ = evaluate_fidelity_claim(
            'perceptually_transparent', _chain(), (obs,))
        assert verdict == 'impaired'


def _repo(tmp_path: Path) -> CadMediaFidelityRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    return CadMediaFidelityRepository(scene)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pdl = _light_profile()
    tcnt = _contrast()
    dcq = DynamicContrastQualification.create(
        document_id=DOC,
        claimed_measurand='sequential_dynamic',
        measured_measurand='sequential_dynamic',
        measurement_ref=_ref(
            'contrast_measure', tcnt.measurement_id,
            tcnt.measurement_sha256),
        verdict='comparable',
    )
    lmc = _capability()
    llo = _llo()
    dab = _boundary()
    fsv = _variant()
    cfp = _chain()
    qmp = _method()
    cfo = _cfo()

    repo.save_light_profile(pdl)
    repo.save_contrast_measure(tcnt)
    repo.save_contrast_qualification(dcq)
    repo.save_light_capability(lmc)
    repo.save_luminance_observation(llo)
    repo.save_boundary_profile(dab)
    repo.save_front_stage_variant(fsv)
    repo.save_codec_chain(cfp)
    repo.save_quality_method(qmp)
    repo.save_fidelity_observation(cfo)

    assert repo.get_light_profile(pdl.profile_id) == pdl
    assert repo.get_contrast_measure(tcnt.measurement_id) == tcnt
    assert repo.get_contrast_qualification(
        dcq.qualification_id) == dcq
    assert repo.get_light_capability(lmc.capability_id) == lmc
    assert repo.get_luminance_observation(
        llo.observation_id) == llo
    assert repo.get_boundary_profile(dab.profile_id) == dab
    assert repo.get_front_stage_variant(fsv.variant_id) == fsv
    assert repo.get_codec_chain(cfp.profile_id) == cfp
    assert repo.get_quality_method(qmp.method_id) == qmp
    assert repo.get_fidelity_observation(
        cfo.observation_id) == cfo


def test_repository_idempotent_resave(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    p = _light_profile()
    repo.save_light_profile(p)
    repo.save_light_profile(p)


def test_repository_detects_column_tamper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tcnt = _contrast()
    repo.save_contrast_measure(tcnt)
    with connect_sqlite(repo.path) as connection:
        connection.execute(
            'UPDATE cad_temporal_contrast_measures '
            "SET measurand='native_static' WHERE measurement_id=?",
            (tcnt.measurement_id,),
        )
        connection.commit()
    with pytest.raises(MediaFidelityIntegrityError):
        repo.get_contrast_measure(tcnt.measurement_id)


def test_vidmeta_tables_exist_after_fresh_migrate(tmp_path: Path) -> None:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    with connect_sqlite(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {
        'cad_projector_light_profiles',
        'cad_temporal_contrast_measures',
        'cad_dynamic_contrast_qualifications',
        'cad_light_measurement_capabilities',
        'cad_low_luminance_observations',
        'cad_display_boundary_profiles',
        'cad_front_stage_variants',
        'cad_codec_chain_profiles',
        'cad_quality_method_profiles',
        'cad_codec_fidelity_observations',
    }
    assert expected <= tables
