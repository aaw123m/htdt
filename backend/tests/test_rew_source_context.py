"""#1042: REW beta-135 source-container provenance + privacy-safe export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htdt.rew_source_context import (
    extract_rew_source_context,
    RewMeasurementSourceContext,
)
from htdt.rew_api import (
    decode_frequency_response,
    normalize_measurement_summaries,
    RewFrequencyResponseSnapshot,
)
from htdt.cad_measurements import normalize_rew_api_snapshot
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene

FIXTURES = Path(__file__).parent / 'fixtures'
UUID = '01628624-ee2a-4a0f-99bb-9cf9e1b9c859'
ASSET_SHA = 'b' * 64
OTHER_ASSET_SHA = 'c' * 64


def _summary(**overrides) -> dict:
    payload = {
        'title': 'M1',
        'uuid': UUID,
        'rewVersion': 'V5.40 beta 135',
        'date': '2026-Sep-16 12:11:10',
    }
    payload.update(overrides)
    return payload


# 1. beta-135 summary with all new fields parses/persists
def test_beta135_fields_recognized_exactly():
    context = extract_rew_source_context(
        _summary(
            containingFileName='theater-session.mdat',
            containingFilePath='C:\\Users\\alice\\REW\\theater-session.mdat',
            containingFileNotes='baseline after traps',
            micCalFilePath='C:\\Cal\\umik-1_90deg.txt',
            soundcardCalFilePath='C:\\Cal\\scarlett.cal',
        ),
        measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
        mic_cal_asset_sha256=ASSET_SHA,
        soundcard_cal_asset_sha256=OTHER_ASSET_SHA,
        soundcard_cal_sample_rate_hz=48000,
    )
    assert context.containing_file_name == 'theater-session.mdat'
    assert context.containing_file_notes == 'baseline after traps'
    assert context.containing_file_path.endswith('theater-session.mdat')
    assert context.rew_version == 'V5.40 beta 135'
    assert context.mic_cal.resolved
    assert context.mic_cal.basename == 'umik-1_90deg.txt'
    assert context.soundcard_cal.applicable_sample_rate_hz == 48000
    assert context.context_sha256 != '0' * 64


# 2. older summary without the fields remains compatible
def test_older_summary_yields_explicitly_unavailable_context():
    context = extract_rew_source_context(
        _summary(), measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
    )
    assert context.containing_file_name is None
    assert context.containing_file_path is None
    assert context.mic_cal is None
    assert context.soundcard_cal is None


# 3. two different local paths to identical admitted bytes -> same identity
def test_path_change_on_identical_bytes_is_not_a_new_identity():
    first = extract_rew_source_context(
        _summary(micCalFilePath='C:\\Users\\alice\\cal.txt'),
        measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
        mic_cal_asset_sha256=ASSET_SHA,
        context_id='ctx-1',
    )
    second = extract_rew_source_context(
        _summary(micCalFilePath='D:\\work\\cal.txt'),
        measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
        mic_cal_asset_sha256=ASSET_SHA,
        context_id='ctx-1',
    )
    assert first.context_sha256 == second.context_sha256


# 4. a path string without admitted bytes never authorizes calibration
def test_unresolved_path_is_pointer_not_proof():
    context = extract_rew_source_context(
        _summary(micCalFilePath='C:\\Cal\\umik.txt'),
        measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
    )
    assert context.mic_cal is not None
    assert not context.mic_cal.resolved
    assert context.mic_cal.asset_sha256 is None


# 7. portable export redacts absolute paths by default
def test_portable_export_redacts_local_paths():
    context = extract_rew_source_context(
        _summary(
            containingFilePath='C:\\Users\\alice\\secret-client\\run.mdat',
            micCalFilePath='C:\\Cal\\umik.txt',
        ),
        measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
        mic_cal_asset_sha256=ASSET_SHA,
    )
    portable = context.portable_dict()
    serialized = json.dumps(portable)
    assert 'alice' not in serialized
    assert 'C:' not in serialized
    assert portable['containing_file_path'] == '<redacted>'
    assert portable['mic_cal']['file_name'] == 'umik.txt'
    assert portable['mic_cal']['asset_sha256'] == ASSET_SHA


def test_import_snapshot_records_portable_source_context(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    summaries = normalize_measurement_summaries(
        json.loads(
            (FIXTURES / 'rew_5_40_beta135_measurements.json').read_text(
                encoding='utf-8'
            )
        )
    )
    summary = dict(summaries[0])
    summary['containingFileName'] = 'theater-session.mdat'
    summary['containingFilePath'] = 'C:\\Users\\alice\\theater-session.mdat'
    summary['micCalFilePath'] = 'C:\\Cal\\umik.txt'
    raw_fr = json.loads(
        (FIXTURES / 'rew_5_40_beta135_frequency_response.json').read_text(
            encoding='utf-8'
        )
    )
    decoded = decode_frequency_response(
        UUID, raw_fr, requested_unit='SPL', requested_ppo=96
    )
    snapshot = RewFrequencyResponseSnapshot(
        summary, {'unit': 'SPL', 'ppo': 96}, raw_fr, decoded
    )
    record, dataset, raw_name, raw_bytes = normalize_rew_api_snapshot(
        revision,
        'point-mlp',
        snapshot,
        imported_at='2026-09-20T00:00:00+00:00',
    )
    provenance = json.loads(record.provenance_json)
    source_context = provenance['source_context']
    assert source_context['containing_file_name'] == 'theater-session.mdat'
    # Portable view in project truth: basename/hash only — the absolute
    # path survives only inside the private raw wrapper.
    assert source_context['containing_file_path'] == '<redacted>'
    assert 'alice' not in json.dumps(provenance)
    assert b'alice' in raw_bytes  # raw local evidence keeps it


def test_context_hash_pinned_stable():
    context = extract_rew_source_context(
        _summary(containingFileName='a.mdat'),
        measurement_uuid=UUID,
        observed_at='2026-09-20T00:00:00+00:00',
        context_id='ctx-fixed',
    )
    RewMeasurementSourceContext.model_validate(
        json.loads(context.model_dump_json())
    )
    with pytest.raises(ValueError, match='hash mismatch'):
        RewMeasurementSourceContext(
            **{
                **json.loads(context.model_dump_json()),
                'containing_file_name': 'b.mdat',
            }
        )
