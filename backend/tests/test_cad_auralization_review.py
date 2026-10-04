"""#538: capability records, routing declarations, review packages and
measured-vs-predicted listening validation.

The render authority (#515) and player/session authorities (#784/#518)
already exist; these tests cover the evidence/shareability layer:
capability-bounded confidence, multi-source routing with no silent
defaults, the deterministic fail-closed review package, and the per-band
measured-vs-predicted agreement record.
"""
from __future__ import annotations

from hashlib import sha256
import io
import zipfile

import numpy as np
import pytest
from pydantic import ValidationError

from htdt.cad_auralization import (
    AuralizationReceiverRef,
    DryProgramAssetRef,
    ImpulseAuthorityRef,
    build_auralization_artifact,
    build_auralization_render_spec,
    render_auralization,
)
from htdt.cad_auralization_repository import CadAuralizationRepository
from htdt.cad_auralization_review import (
    AuralizationStemRoute,
    ValidatedBand,
    build_auralization_capability,
    build_auralization_routing,
    build_listening_validation,
    build_review_package,
    verify_review_package,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene


def _hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8')
    return sha256(data).hexdigest()


def _samples_hash(samples) -> str:
    return sha256(np.asarray(samples, dtype='<f8').tobytes()).hexdigest()


def _impulse(kind: str = 'predicted', tag: str = 'ir', samples=None):
    if samples is None:
        samples = (1.0, 0.5, 0.25)
    return ImpulseAuthorityRef(
        kind=kind,
        artifact_id=f'{kind}:{tag}',
        artifact_sha256=_hash(f'{kind}-{tag}-bytes'),
        decoded_pcm_sha256=_samples_hash(samples),
        sample_rate_hz=48000,
        sample_count=len(samples),
        absolute_amplitude_authority=False,
    )


def _dry(tag: str = 'dry', samples=(0.1, -0.2, 0.3, -0.1)):
    return DryProgramAssetRef(
        asset_sha256=_hash(f'{tag}-bytes'),
        decoded_pcm_sha256=_samples_hash(samples),
        sample_rate_hz=48000,
        sample_count=len(samples),
        program_level_authority='unknown',
    )


def _stem(stem_id: str, source_id: str, **overrides):
    kwargs = dict(
        stem_id=stem_id,
        source_id=source_id,
        dry_source=_dry(f'{stem_id}-dry'),
        impulse_authority=_impulse(tag=f'{stem_id}-ir'),
        output_channel=0,
        gain_db=-3.0,
        delay_ms=0.0,
        filter_identity='none',
    )
    kwargs.update(overrides)
    return AuralizationStemRoute(**kwargs)


def _routing(**overrides):
    kwargs = dict(
        document_id='doc-1',
        output_sample_rate_hz=48000,
        output_layout='mono_mix',
        stems=(_stem('stem-fl', 'speaker-fl'),),
    )
    kwargs.update(overrides)
    return build_auralization_routing(**kwargs)


def _spec(kind: str = 'predicted', tag: str = 'ir', impulse_samples=None,
          dry_samples=(0.1, -0.2, 0.3, -0.1), **overrides):
    kwargs = dict(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        source_scenario_id='scenario-1',
        source_scenario_sha256=_hash('scenario'),
        receiver=AuralizationReceiverRef(
            receiver_id='seat-1', receiver_entity_id='entity-1'
        ),
        impulse_authority=_impulse(kind, tag, impulse_samples),
        dry_source=_dry(f'{tag}-dry', dry_samples),
        output_sample_rate_hz=48000,
        gain_policy='unity',
    )
    kwargs.update(overrides)
    return build_auralization_render_spec(**kwargs)


def _render(spec, impulse_samples, dry_samples=(0.1, -0.2, 0.3, -0.1)):
    """Render + seal: returns (pcm, artifact, wav_bytes)."""
    pcm = render_auralization(
        spec,
        dry_samples=dry_samples,
        impulse_samples=impulse_samples,
        dry_asset_sha256=spec.dry_source.asset_sha256,
        impulse_artifact_sha256=spec.impulse_authority.artifact_sha256,
    )
    artifact, wav = build_auralization_artifact(spec, pcm)
    return pcm, artifact, wav


def _capability(spec, artifact, routing=None, **overrides):
    kwargs = dict(
        spec=spec,
        artifact=artifact,
        routing=routing or _routing(),
        ir_origin=spec.impulse_authority.kind,
        prediction_measurement_identity='run-1',
        ir_producer_id='htdt.fixture_ir',
        ir_producer_version='1',
        hrtf_processing='none',
        confidence_state='unvalidated',
    )
    kwargs.update(overrides)
    return build_auralization_capability(**kwargs)


def _decaying_ir(seed: int, n: int = 2048, decay: float = 6.0,
                 rate: int = 48000):
    rng = np.random.default_rng(seed)
    t = np.arange(n) / rate
    ir = rng.standard_normal(n) * np.exp(-decay * t)
    return tuple(float(v) for v in ir)


# ---------------------------------------------------------------------------
# Routing declaration
# ---------------------------------------------------------------------------


def test_routing_declaration_seals_explicit_matrix() -> None:
    routing = _routing(
        output_layout='stereo',
        stems=(
            _stem('stem-fl', 'speaker-fl', output_channel=0, gain_db=-1.5,
                  delay_ms=2.0, filter_identity='hp-80hz'),
            _stem('stem-fr', 'speaker-fr', output_channel=1, gain_db=-2.0,
                  delay_ms=0.0, filter_identity='none'),
        ),
    )
    assert routing.routing_id.startswith('auralization-routing:')
    assert routing.stems[0].gain_db == -1.5
    # Same semantic content rebuilds to the same identity.
    again = _routing(
        output_layout='stereo',
        stems=(
            _stem('stem-fl', 'speaker-fl', output_channel=0, gain_db=-1.5,
                  delay_ms=2.0, filter_identity='hp-80hz'),
            _stem('stem-fr', 'speaker-fr', output_channel=1, gain_db=-2.0,
                  delay_ms=0.0, filter_identity='none'),
        ),
    )
    assert again.routing_id == routing.routing_id


def test_routing_rejects_duplicate_stems_and_layout_drift() -> None:
    with pytest.raises(ValueError, match='unique stem ids'):
        _routing(stems=(
            _stem('same', 'speaker-fl'),
            _stem('same', 'speaker-fr'),
        ))
    with pytest.raises(ValueError, match='single output channel'):
        _routing(stems=(_stem('stem-fl', 'speaker-fl', output_channel=1),))
    with pytest.raises(ValueError, match='channels 0/1'):
        _routing(
            output_layout='stereo',
            stems=(_stem('stem-fl', 'speaker-fl', output_channel=2),),
        )


def test_routing_requires_explicit_gain_delay_filter() -> None:
    # No silent defaults: omitting gain/delay/filter must not construct.
    with pytest.raises(ValidationError):
        AuralizationStemRoute(
            stem_id='s', source_id='src',
            dry_source=_dry(), impulse_authority=_impulse(),
            output_channel=0, delay_ms=0.0, filter_identity='none',
        )
    with pytest.raises(ValidationError):
        AuralizationStemRoute(
            stem_id='s', source_id='src',
            dry_source=_dry(), impulse_authority=_impulse(),
            output_channel=0, gain_db=0.0, filter_identity='none',
        )


# ---------------------------------------------------------------------------
# AuralizationCapability
# ---------------------------------------------------------------------------


def test_capability_seals_spec_artifact_and_routing() -> None:
    impulse = _decaying_ir(1)
    spec = _spec(impulse_samples=impulse)
    _pcm, artifact, _wav = _render(spec, impulse)
    routing = _routing()
    capability = _capability(spec, artifact, routing)
    assert capability.capability_id.startswith('auralization-capability:')
    assert capability.spec_id == spec.spec_id
    assert capability.impulse_authority.artifact_sha256 == (
        spec.impulse_authority.artifact_sha256
    )
    assert capability.routing_sha256 == routing.routing_sha256
    assert capability.level_semantics == artifact.level_semantics
    assert capability.confidence_state == 'unvalidated'


def test_capability_confidence_bounded_by_evidence() -> None:
    impulse = _decaying_ir(2)
    spec = _spec(impulse_samples=impulse)
    _pcm, artifact, _wav = _render(spec, impulse)
    # A predicted render cannot claim measured_reference.
    with pytest.raises(ValueError, match='measured IR origin'):
        _capability(spec, artifact, confidence_state='measured_reference')
    # Validated-domain requires evidence-backed bands.
    with pytest.raises(ValueError, match='validated band'):
        _capability(
            spec, artifact,
            confidence_state='predicted_validated_for_declared_domain',
        )
    # Validated bands on an unvalidated/demo record are rejected.
    with pytest.raises(ValueError, match='validated bands'):
        _capability(
            spec, artifact,
            validated_bands=(ValidatedBand(
                band_center_hz=1000.0,
                band_fraction='octave',
                evidence_id='validation-1',
            ),),
            confidence_state='demonstration_only',
        )
    # predicted_with_limitations must state the limitations.
    with pytest.raises(ValueError, match='limitations'):
        _capability(
            spec, artifact,
            confidence_state='predicted_with_limitations',
        )
    ok = _capability(
        spec, artifact,
        confidence_state='predicted_with_limitations',
        known_limitations=('late-field below measurement noise floor',),
    )
    assert ok.confidence_state == 'predicted_with_limitations'


def test_capability_ir_origin_matches_impulse_authority() -> None:
    impulse = _decaying_ir(3)
    spec = _spec(kind='measured', impulse_samples=impulse)
    _pcm, artifact, _wav = _render(spec, impulse)
    with pytest.raises(ValueError, match='predicted impulse authority'):
        _capability(spec, artifact, ir_origin='predicted')
    capability = _capability(
        spec, artifact,
        ir_origin='measured',
        confidence_state='measured_reference',
        validated_bands=(ValidatedBand(
            band_center_hz=1000.0,
            band_fraction='octave',
            evidence_id='measurement-1',
        ),),
    )
    assert capability.ir_origin == 'measured'
    # A hybrid claim must declare composition limitations.
    with pytest.raises(ValueError, match='known limitations'):
        _capability(
            spec, artifact,
            ir_origin='hybrid',
            confidence_state='unvalidated',
        )


def test_capability_hrtf_group_and_artifact_binding() -> None:
    impulse = _decaying_ir(4)
    spec = _spec(impulse_samples=impulse)
    _pcm, artifact, _wav = _render(spec, impulse)
    # Declared HRTF requires dataset+license provenance.
    with pytest.raises(ValueError, match='dataset id and hash'):
        _capability(spec, artifact, hrtf_processing='generic')
    with pytest.raises(ValueError, match='subject identity'):
        _capability(
            spec, artifact,
            hrtf_processing='individualized',
            hrtf_dataset_id='sofa-db',
            hrtf_dataset_sha256='a' * 64,
            hrtf_license_id='cc-by-4',
        )
    # Capability cannot bind a foreign artifact/spec.
    other_spec = _spec(tag='other', impulse_samples=impulse)
    with pytest.raises(ValueError, match='does not belong'):
        _capability(other_spec, artifact)


# ---------------------------------------------------------------------------
# Measured-vs-predicted listening validation
# ---------------------------------------------------------------------------


def _measured_predicted_pair(seed_measured: int = 11,
                             seed_predicted: int = 11):
    """A measured render and a predicted render on the same binding."""
    dry = (0.05, -0.1, 0.2, -0.05, 0.1)
    measured_ir = _decaying_ir(seed_measured)
    predicted_ir = _decaying_ir(seed_predicted)
    measured_spec = _spec(
        kind='measured', tag='m', impulse_samples=measured_ir,
        dry_samples=dry,
    )
    predicted_spec = _spec(
        kind='predicted', tag='p', impulse_samples=predicted_ir,
        dry_samples=dry,
    )
    m_pcm, m_art, _m_wav = _render(measured_spec, measured_ir, dry)
    p_pcm, p_art, _p_wav = _render(predicted_spec, predicted_ir, dry)
    return m_art, m_pcm, p_art, p_pcm


def test_listening_validation_computes_band_agreement() -> None:
    m_art, m_pcm, p_art, p_pcm = _measured_predicted_pair()
    record = build_listening_validation(
        document_id='doc-1',
        source_scenario_id='scenario-1',
        receiver_id='seat-1',
        measured_artifact=m_art,
        measured_pcm=m_pcm,
        predicted_artifact=p_art,
        predicted_pcm=p_pcm,
        band_centers_hz=(250.0, 1000.0, 4000.0),
        alignment_semantics='direct_arrival_aligned',
        level_matching_method='unity_passthrough',
    )
    assert record.comparison_state == 'computed'
    assert len(record.bands) == 3
    # Identical IR draws → near-zero level difference, correlation ~1.
    for band in record.bands:
        assert band.state == 'computed'
        assert band.level_difference_db == pytest.approx(0.0, abs=1e-6)
        assert band.waveform_correlation == pytest.approx(1.0, abs=1e-6)
    assert record.validation_id.startswith(
        'auralization-listening-validation:'
    )


def test_listening_validation_reports_divergence() -> None:
    m_art, m_pcm, p_art, p_pcm = _measured_predicted_pair(
        seed_measured=21, seed_predicted=22
    )
    record = build_listening_validation(
        document_id='doc-1',
        source_scenario_id='scenario-1',
        receiver_id='seat-1',
        measured_artifact=m_art,
        measured_pcm=m_pcm,
        predicted_artifact=p_art,
        predicted_pcm=p_pcm,
        band_centers_hz=(1000.0,),
        alignment_semantics='unaligned',
        level_matching_method='unity_passthrough',
        audible_limitations=('unrelated IR draws decorrelate',),
    )
    assert record.comparison_state == 'computed'
    band = record.bands[0]
    assert band.waveform_correlation < 0.5
    assert record.audible_limitations


def test_listening_validation_honest_states() -> None:
    m_art, m_pcm, p_art, p_pcm = _measured_predicted_pair()
    # No measured leg → honest no_measured_reference, no band metrics.
    record = build_listening_validation(
        document_id='doc-1',
        source_scenario_id='scenario-1',
        receiver_id='seat-1',
        predicted_artifact=p_art,
        predicted_pcm=p_pcm,
        alignment_semantics='declared',
        level_matching_method='unity_passthrough',
    )
    assert record.comparison_state == 'no_measured_reference'
    assert record.bands == ()
    assert record.measured_artifact_id is None
    # A band whose band energy floors out reports insufficient_signal.
    quiet = tuple(v * 1e-9 for v in _decaying_ir(31))
    p2_spec = _spec(kind='predicted', tag='quiet', impulse_samples=quiet)
    p2_pcm, p2_art, _wav = _render(p2_spec, quiet)
    record = build_listening_validation(
        document_id='doc-1',
        source_scenario_id='scenario-1',
        receiver_id='seat-1',
        measured_artifact=m_art,
        measured_pcm=m_pcm,
        predicted_artifact=p2_art,
        predicted_pcm=p2_pcm,
        band_centers_hz=(1000.0,),
        alignment_semantics='declared',
        level_matching_method='unity_passthrough',
    )
    assert record.comparison_state == 'partial'
    assert record.bands[0].state == 'insufficient_signal'
    assert record.bands[0].level_difference_db is None


def test_listening_validation_fails_closed_on_mismatched_legs() -> None:
    m_art, m_pcm, p_art, p_pcm = _measured_predicted_pair(
        seed_measured=21, seed_predicted=22
    )
    with pytest.raises(ValueError, match='predicted-IR artifact'):
        build_listening_validation(
            document_id='doc-1',
            source_scenario_id='s', receiver_id='r',
            predicted_artifact=m_art,
            predicted_pcm=m_pcm,
            alignment_semantics='x', level_matching_method='y',
        )
    with pytest.raises(ValueError, match='measured PCM'):
        build_listening_validation(
            document_id='doc-1',
            source_scenario_id='s', receiver_id='r',
            measured_artifact=m_art, measured_pcm=p_pcm,
            predicted_artifact=p_art, predicted_pcm=p_pcm,
            band_centers_hz=(1000.0,),
            alignment_semantics='x', level_matching_method='y',
        )


# ---------------------------------------------------------------------------
# Review package
# ---------------------------------------------------------------------------


def _comparison_row(tag: str, kind: str = 'predicted', label: str = 'A',
                    seed: int = 41):
    dry = (0.05, -0.1, 0.2, -0.05, 0.1)
    ir = _decaying_ir(seed)
    spec = _spec(kind=kind, tag=tag, impulse_samples=ir, dry_samples=dry)
    _pcm, artifact, wav = _render(spec, ir, dry)
    routing = _routing()
    capability = _capability(spec, artifact, routing)
    return dict(
        label=label, artifact=artifact, spec=spec, wav_bytes=wav,
        capability=capability, routing=routing,
    )


def _member(artifact) -> str:
    return 'audio/' + artifact.artifact_id.replace(':', '_') + '.wav'


def test_review_package_deterministic_and_verifiable() -> None:
    row_a = _comparison_row('a', label='A', seed=41)
    row_b = _comparison_row('b', label='B', seed=42)
    package, data = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(row_a, row_b),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    assert package.package_id.startswith('auralization-review-package:')
    assert package.package_asset_sha256 == _hash(data)
    # Byte-for-byte deterministic for identical inputs.
    again, again_data = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(
            _comparison_row('a', label='A', seed=41),
            _comparison_row('b', label='B', seed=42),
        ),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    assert again.package_id == package.package_id
    assert again_data == data
    # Members: manifest + viewer + one audio per comparison.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
    assert names == {
        'manifest.json',
        'index.html',
        _member(row_a['artifact']),
        _member(row_b['artifact']),
    }
    verified = verify_review_package(data)
    assert verified.package_id == package.package_id
    assert verified.semantic_sha256 == package.semantic_sha256


def test_review_package_fails_closed() -> None:
    row = _comparison_row('a', label='A', seed=41)
    # Missing WAV bytes → no package.
    missing_wav = dict(row)
    missing_wav.pop('wav_bytes')
    with pytest.raises(ValueError, match='WAV bytes'):
        build_review_package(
            document_id='doc-1',
            scene_revision_id='rev-1',
            scene_content_hash=_hash('scene'),
            comparisons=(missing_wav,),
            created_at_utc='2026-10-04T00:00:00+00:00',
        )
    # WAV bytes from a different render → digest mismatch.
    other = _comparison_row('b', label='B', seed=42)
    wrong = dict(row)
    wrong['wav_bytes'] = other['wav_bytes']
    with pytest.raises(ValueError, match='digest'):
        build_review_package(
            document_id='doc-1',
            scene_revision_id='rev-1',
            scene_content_hash=_hash('scene'),
            comparisons=(wrong,),
            created_at_utc='2026-10-04T00:00:00+00:00',
        )
    # A clipped render is never packaged.
    loud = (10.0, -10.0, 10.0)
    loud_spec = _spec(
        tag='loud', impulse_samples=(1.0, 0.5), dry_samples=loud,
    )
    loud_pcm, loud_artifact, loud_wav = _render(loud_spec, (1.0, 0.5), loud)
    assert loud_artifact.clipped_sample_count > 0
    clipped_row = dict(
        label='C', artifact=loud_artifact, spec=loud_spec,
        wav_bytes=loud_wav,
        capability=_capability(loud_spec, loud_artifact),
        routing=_routing(),
    )
    with pytest.raises(ValueError, match='clipped'):
        build_review_package(
            document_id='doc-1',
            scene_revision_id='rev-1',
            scene_content_hash=_hash('scene'),
            comparisons=(clipped_row,),
            created_at_utc='2026-10-04T00:00:00+00:00',
        )


def test_review_package_verifier_detects_tampering() -> None:
    package, data = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(_comparison_row('a', label='A', seed=41),),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    # Flip a byte inside the audio member → member hash check fails.
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(
        buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for name in source.namelist():
            raw = source.read(name)
            if name.startswith('audio/'):
                raw = raw[:100] + bytes([raw[100] ^ 0xFF]) + raw[101:]
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            target.writestr(info, raw)
    with pytest.raises(ValueError, match='hash mismatch'):
        verify_review_package(buffer.getvalue())
    # An extra member not in the manifest fails closed.
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(
        buffer, 'w', compression=zipfile.ZIP_DEFLATED
    ) as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr('extra/evil.txt', b'smuggled')
    with pytest.raises(ValueError, match='declared asset manifest'):
        verify_review_package(buffer.getvalue())


def test_review_package_manifest_security_rows() -> None:
    package, _data = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(_comparison_row('a', label='A', seed=41),),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    assert 'no_local_paths' in package.security_assertions
    assert 'rendered_audio_only_no_source_program_assets' in (
        package.security_assertions
    )
    audio = [
        asset for asset in package.assets
        if asset.kind == 'rendered_audio'
    ]
    assert len(audio) == 1
    assert audio[0].rights == 'derived_render_internal_review'


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_repository_round_trips_all_authorities(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_f1_scene(), parent_revision_id=None)
    repository = CadAuralizationRepository(scene_repository)

    routing = _routing()
    repository.save_routing(routing)
    assert repository.get_routing(routing.routing_id) == routing
    assert repository.find_routing_by_sha(routing.routing_sha256) == routing
    with pytest.raises(ValueError, match='append-only'):
        repository.save_routing(routing)

    row = _comparison_row('a', label='A', seed=41)
    repository.save_render_spec(row['spec'])
    repository.save_artifact(row['artifact'], row['wav_bytes'])
    repository.save_capability(row['capability'])
    assert repository.get_capability(
        row['capability'].capability_id
    ) == row['capability']
    assert repository.capabilities_for_spec(row['spec'].spec_id) == (
        row['capability'],
    )

    package, package_bytes = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(row,),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    repository.save_review_package(package, package_bytes)
    assert repository.get_review_package(package.package_id) == package
    stored = repository.read_review_package_bytes(package)
    assert sha256(stored).hexdigest() == package.package_asset_sha256
    assert verify_review_package(stored).package_id == package.package_id
    # A package whose capability was never persisted fails closed.
    orphan_row = _comparison_row('orphan', label='B', seed=55)
    orphan_package, orphan_bytes = build_review_package(
        document_id='doc-1',
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        comparisons=(orphan_row,),
        created_at_utc='2026-10-04T00:00:00+00:00',
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_review_package(orphan_package, orphan_bytes)

    m_art, m_pcm, p_art, p_pcm = _measured_predicted_pair()
    validation = build_listening_validation(
        document_id='doc-1',
        source_scenario_id='scenario-1',
        receiver_id='seat-1',
        measured_artifact=m_art,
        measured_pcm=m_pcm,
        predicted_artifact=p_art,
        predicted_pcm=p_pcm,
        band_centers_hz=(1000.0,),
        alignment_semantics='declared',
        level_matching_method='unity_passthrough',
    )
    repository.save_listening_validation(validation)
    assert repository.get_listening_validation(
        validation.validation_id
    ) == validation
    assert repository.listening_validations_for_document('doc-1') == (
        validation,
    )
