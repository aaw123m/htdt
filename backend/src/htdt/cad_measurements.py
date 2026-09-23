from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_repository import SceneRevision
from .cad_scene import Direction3, acoustic_reference_position
from .rew_api import (
    RewApiError,
    RewFrequencyResponseSnapshot,
    decode_frequency_response,
)
from .rew_parser import PARSER_VERSION, parse_rew_frequency_response


CAD_REW_API_SNAPSHOT_FORMAT = 'htdt-rew-api-frequency-response-snapshot-1'
CAD_REW_API_ADAPTER_VERSION = 'rew-api-snapshot-1'

# Self-declaring normalized evidence: the raw asset literally declares the
# dataset samples and interpretation metadata in canonical JSON, so the
# "import transformation" is a canonical decode. This is the honest
# persistence path for normalized FR data that does not come from a REW
# parser/adapter (provided data, fixtures, synthetic evidence).
HTDT_DECLARED_FR_FORMAT = 'htdt-declared-frequency-response-1'
HTDT_DECLARED_IMPORTER_VERSION = 'htdt-declared-fr-1'

# Version of the persisted transformation seal (see
# ``import_transformation_sha256``); part of the sealed payload so a future
# seal format can never be confused with this one.
IMPORT_TRANSFORMATION_VERSION = 'fr-import-transformation-1'


class CadMeasurementError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalize_rew_capture_timestamp(
    raw_date: str | None,
    *,
    host_timezone: tzinfo | None = None,
) -> tuple[str | None, str]:
    """Normalize a REW measurement date to offset-aware ISO 8601."""

    if raw_date is None or not raw_date.strip():
        return None, 'missing'
    raw = raw_date.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        try:
            date_part, time_part = raw.split(' ', 1)
            year_text, month_text, day_text = date_part.split('-', 2)
            hour_text, minute_text, second_text = time_part.split(':', 2)
            month = {
                'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
                'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
                'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
            }[month_text.lower()]
            parsed = datetime(
                int(year_text),
                month,
                int(day_text),
                int(hour_text),
                int(minute_text),
                int(second_text),
            )
        except (KeyError, TypeError, ValueError):
            return None, 'unparsed'

    if parsed.tzinfo is not None:
        return parsed.isoformat(), 'source_timezone'

    if host_timezone is None:
        try:
            return parsed.astimezone().isoformat(), 'host_local_timezone'
        except (OSError, ValueError):
            return None, 'host_timezone_unavailable'
    return parsed.replace(tzinfo=host_timezone).isoformat(), 'host_local_timezone'

def measurement_record_for_revision(
    revision: SceneRevision,
    measurement_entity_id: str,
    *,
    measurement_id: str | None = None,
    measurement_direction: Direction3 | None = None,
    evidence_type: str = 'unknown',
    channel_role: str = 'unknown',
    source_speaker_ids: tuple[str, ...] = (),
    radiation_scope: str = 'unknown',
    routing_evidence: str = 'unknown',
    captured_at: str | None = None,
    imported_at: str | None = None,
    source_kind: str,
    external_source_id: str | None = None,
    quality_status: str = 'unknown',
    quality_reasons: tuple[str, ...] = (),
    quality_source: str = 'unknown',
    provenance: dict[str, Any] | None = None,
) -> CadMeasurementRecord:
    try:
        entity = revision.document.entity(measurement_entity_id)
    except KeyError as exc:
        raise CadMeasurementError(f'unknown measurement entity: {measurement_entity_id}') from exc
    position = acoustic_reference_position(entity)
    if position is None:
        raise CadMeasurementError(
            f'entity {measurement_entity_id} does not expose an acoustic reference position'
        )

    for source_id in source_speaker_ids:
        try:
            source = revision.document.entity(source_id)
        except KeyError as exc:
            raise CadMeasurementError(f'unknown source speaker: {source_id}') from exc
        if source.kind != 'speaker':
            raise CadMeasurementError(f'source entity is not a speaker: {source_id}')

    return CadMeasurementRecord(
        measurement_id=measurement_id or str(uuid4()),
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        measurement_entity_id=measurement_entity_id,
        measurement_position=position,
        measurement_direction=measurement_direction,
        evidence_type=evidence_type,
        channel_role=channel_role,
        source_speaker_ids=source_speaker_ids,
        radiation_scope=radiation_scope,
        routing_evidence=routing_evidence,
        captured_at=captured_at,
        imported_at=imported_at or utc_now(),
        source_kind=source_kind,
        external_source_id=external_source_id,
        quality_status=quality_status,
        quality_reasons=quality_reasons,
        quality_source=quality_source,
        provenance_json=canonical_json(provenance or {}),
    )


def normalize_rew_api_snapshot(
    revision: SceneRevision,
    measurement_entity_id: str,
    snapshot: RewFrequencyResponseSnapshot,
    *,
    measurement_direction: Direction3 | None = None,
    evidence_type: str = 'unknown',
    channel_role: str = 'unknown',
    source_speaker_ids: tuple[str, ...] = (),
    radiation_scope: str = 'unknown',
    routing_evidence: str = 'unknown',
    routing_profile: dict[str, Any] | None = None,
    acquisition_context: dict[str, Any] | None = None,
    engine_session: dict[str, Any] | None = None,
    imported_at: str | None = None,
    validation_scope: Literal['owned_room'] | None = None,
    validation_campaign_id: str | None = None,
    captured_timezone: tzinfo | None = None,
) -> tuple[CadMeasurementRecord, CadFrequencyResponseDataset, str, bytes]:
    decoded = snapshot.decoded
    wrapper = {
        'format': CAD_REW_API_SNAPSHOT_FORMAT,
        'measurement_uuid': decoded.measurement_id,
        'query': snapshot.query,
        'measurement_summary': snapshot.measurement_summary,
        'frequency_response': snapshot.raw_frequency_response,
    }
    raw = canonical_json(wrapper).encode('utf-8')
    digest = sha256(raw).hexdigest()
    summary = snapshot.measurement_summary
    raw_captured_at = (
        summary.get('date')
        if isinstance(summary.get('date'), str) and summary.get('date')
        else None
    )
    captured_at, captured_at_source = normalize_rew_capture_timestamp(
        raw_captured_at,
        host_timezone=captured_timezone,
    )
    if validation_scope == 'owned_room' and not validation_campaign_id:
        raise CadMeasurementError(
            'owned-room REW import requires validation_campaign_id'
        )
    if validation_scope is None and validation_campaign_id is not None:
        raise CadMeasurementError(
            'validation_campaign_id requires validation_scope=owned_room'
        )
    phase_status = 'unknown' if decoded.phase_deg is not None else 'absent'
    warnings = []
    if decoded.phase_deg is not None and all(value == 0 for value in decoded.phase_deg):
        warnings.append('phase_all_zero_unverified')

    provenance: dict[str, Any] = {
        'adapter_version': CAD_REW_API_ADAPTER_VERSION,
        'rew_version': summary.get('rewVersion') if isinstance(summary.get('rewVersion'), str) else None,
        'captured_at_raw': raw_captured_at,
        'captured_at_source': captured_at_source,
        'validation_scope': validation_scope,
        'validation_campaign_id': validation_campaign_id,
        'requested': {
            'unit': decoded.requested_unit,
            'ppo': decoded.requested_ppo,
            'smoothing': decoded.requested_smoothing,
        },
        'returned': {
            'unit': decoded.unit,
            'ppo': decoded.points_per_octave,
            'freq_step_hz': decoded.frequency_step_hz,
            'smoothing': decoded.smoothing,
            'start_frequency_hz': decoded.start_frequency_hz,
        },
        'warnings': warnings,
    }
    if engine_session is not None:
        # The exact external measurement-engine session this import ran
        # under: producer version + capability snapshot + adapter identity
        # (#599). The session is caller-supplied provenance, merged under a
        # fixed key so reads can always find it.
        provenance['engine_session'] = engine_session
    if routing_profile is not None:
        provenance['routing_profile'] = routing_profile
    if acquisition_context is not None:
        provenance['acquisition_context'] = acquisition_context
    record = measurement_record_for_revision(
        revision,
        measurement_entity_id,
        measurement_direction=measurement_direction,
        evidence_type=evidence_type,
        channel_role=channel_role,
        source_speaker_ids=source_speaker_ids,
        radiation_scope=radiation_scope,
        routing_evidence=routing_evidence,
        captured_at=captured_at,
        imported_at=imported_at,
        source_kind='rew_api',
        external_source_id=decoded.measurement_id,
        provenance=provenance,
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=str(uuid4()),
        measurement_id=record.measurement_id,
        frequency_hz=decoded.frequency_hz,
        level_db=decoded.magnitude,
        phase_deg=decoded.phase_deg,
        phase_status=phase_status,
        level_reference='unknown',
        smoothing=decoded.smoothing,
        processing_json=canonical_json({
            'requested_unit': decoded.requested_unit,
            'requested_ppo': decoded.requested_ppo,
            'requested_smoothing': decoded.requested_smoothing,
            'returned_unit': decoded.unit,
            'returned_ppo': decoded.points_per_octave,
            'returned_frequency_step_hz': decoded.frequency_step_hz,
        }),
        source_sha256=digest,
        importer_version=CAD_REW_API_ADAPTER_VERSION,
    )
    return record, dataset, f'rew-api-{decoded.measurement_id}.json', raw


def normalize_rew_text(
    revision: SceneRevision,
    measurement_entity_id: str,
    raw: bytes,
    *,
    filename: str,
    measurement_direction: Direction3 | None = None,
    evidence_type: str = 'unknown',
    channel_role: str = 'unknown',
    source_speaker_ids: tuple[str, ...] = (),
    radiation_scope: str = 'unknown',
    routing_evidence: str = 'unknown',
    routing_profile: dict[str, Any] | None = None,
    acquisition_context: dict[str, Any] | None = None,
    engine_session: dict[str, Any] | None = None,
    imported_at: str | None = None,
) -> tuple[CadMeasurementRecord, CadFrequencyResponseDataset, str, bytes]:
    parsed = parse_rew_frequency_response(raw)
    provenance: dict[str, Any] = {
        'filename': filename,
        'header_lines': parsed.header_lines,
        'warnings': parsed.warnings,
    }
    if engine_session is not None:
        provenance['engine_session'] = engine_session
    if routing_profile is not None:
        provenance['routing_profile'] = routing_profile
    if acquisition_context is not None:
        provenance['acquisition_context'] = acquisition_context
    record = measurement_record_for_revision(
        revision,
        measurement_entity_id,
        measurement_direction=measurement_direction,
        evidence_type=evidence_type,
        channel_role=channel_role,
        source_speaker_ids=source_speaker_ids,
        radiation_scope=radiation_scope,
        routing_evidence=routing_evidence,
        captured_at=None,
        imported_at=imported_at,
        source_kind='rew_text',
        external_source_id=None,
        provenance=provenance,
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=str(uuid4()),
        measurement_id=record.measurement_id,
        frequency_hz=parsed.frequency_hz,
        level_db=parsed.level_db,
        phase_deg=parsed.phase_deg,
        phase_status=parsed.phase_status,
        level_reference=parsed.level_reference,
        smoothing=None,
        processing_json=canonical_json({'warnings': parsed.warnings}),
        source_sha256=parsed.source_sha256,
        importer_version=parsed.parser_version,
    )
    return record, dataset, filename, raw


def declared_fr_raw(
    *,
    frequency_hz: tuple[float, ...] | list[float],
    level_db: tuple[float, ...] | list[float],
    phase_deg: tuple[float, ...] | list[float] | None = None,
    phase_status: str = 'absent',
    level_reference: str = 'unknown',
    smoothing: str | None = None,
    processing: dict[str, Any] | None = None,
) -> bytes:
    """Canonical raw asset for a self-declared normalized FR dataset.

    The persisted payload literally declares the samples and interpretation
    metadata, so the pinned import transformation (see
    ``IMPORT_TRANSFORMATION_AUTHORITIES``) is a canonical decode: what the
    raw asset states is exactly what gets persisted.
    """
    payload = {
        'format': HTDT_DECLARED_FR_FORMAT,
        'frequency_hz': [float(value) for value in frequency_hz],
        'level_db': [float(value) for value in level_db],
        'phase_deg': None if phase_deg is None else [float(value) for value in phase_deg],
        'phase_status': phase_status,
        'level_reference': level_reference,
        'smoothing': smoothing,
        'processing': processing or {},
    }
    return canonical_json(payload).encode('utf-8')


def _canonical_payload(raw: bytes, *, format_value: str, label: str) -> dict[str, Any]:
    """Decode a canonical-JSON raw asset and pin its declared format."""
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{label} raw asset is not valid JSON') from exc
    if not isinstance(payload, dict) or payload.get('format') != format_value:
        raise ValueError(f'raw asset is not a {label} payload')
    if canonical_json(payload).encode('utf-8') != raw:
        raise ValueError(f'{label} raw asset is not in canonical form')
    return payload


def _declared_floats(payload: dict[str, Any], name: str, label: str) -> tuple[float, ...]:
    values = payload.get(name)
    if not isinstance(values, list):
        raise ValueError(f'{label} payload is missing the {name} array')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError(f'{label} payload {name} must contain only numbers')
    return tuple(float(value) for value in values)


def _rederive_rew_text_dataset(raw: bytes) -> dict[str, Any]:
    """Re-run the pinned REW text parser against the exact raw bytes."""
    parsed = parse_rew_frequency_response(raw)
    return {
        'frequency_hz': parsed.frequency_hz,
        'level_db': parsed.level_db,
        'phase_deg': parsed.phase_deg,
        'phase_status': parsed.phase_status,
        'level_reference': parsed.level_reference,
        'smoothing': None,
        'processing_json': canonical_json({'warnings': parsed.warnings}),
        'source_sha256': parsed.source_sha256,
        'importer_version': parsed.parser_version,
    }


def _rederive_rew_api_snapshot_dataset(raw: bytes) -> dict[str, Any]:
    """Re-run the pinned REW API adapter against the canonical stored wrapper."""
    wrapper = _canonical_payload(
        raw,
        format_value=CAD_REW_API_SNAPSHOT_FORMAT,
        label='REW API frequency-response snapshot',
    )
    measurement_uuid = wrapper.get('measurement_uuid')
    if not isinstance(measurement_uuid, str) or not measurement_uuid:
        raise ValueError('REW API snapshot wrapper is missing the measurement UUID')
    if not isinstance(wrapper.get('measurement_summary'), dict):
        raise ValueError('REW API snapshot wrapper is missing the measurement summary')
    frequency_response = wrapper.get('frequency_response')
    if not isinstance(frequency_response, dict):
        raise ValueError('REW API snapshot wrapper is missing the frequency response')
    query = wrapper.get('query')
    if not isinstance(query, dict) or set(query) - {'unit', 'ppo', 'smoothing'}:
        raise ValueError('REW API snapshot wrapper carries an unsupported request query')
    unit = query.get('unit')
    if not isinstance(unit, str) or not unit:
        raise ValueError('REW API snapshot query is missing the requested unit')
    ppo = query.get('ppo')
    if ppo is not None and (isinstance(ppo, bool) or not isinstance(ppo, int)):
        raise ValueError('REW API snapshot query ppo must be an integer')
    smoothing = query.get('smoothing')
    if smoothing is not None and not isinstance(smoothing, str):
        raise ValueError('REW API snapshot query smoothing must be a string')
    try:
        decoded = decode_frequency_response(
            measurement_uuid,
            frequency_response,
            requested_unit=unit,
            requested_ppo=ppo,
            requested_smoothing=smoothing,
        )
    except RewApiError as exc:
        raise ValueError(
            'REW API snapshot does not decode through the pinned adapter: '
            f'{exc}'
        ) from exc
    return {
        'frequency_hz': decoded.frequency_hz,
        'level_db': decoded.magnitude,
        'phase_deg': decoded.phase_deg,
        'phase_status': 'unknown' if decoded.phase_deg is not None else 'absent',
        'level_reference': 'unknown',
        'smoothing': decoded.smoothing,
        'processing_json': canonical_json({
            'requested_unit': decoded.requested_unit,
            'requested_ppo': decoded.requested_ppo,
            'requested_smoothing': decoded.requested_smoothing,
            'returned_unit': decoded.unit,
            'returned_ppo': decoded.points_per_octave,
            'returned_frequency_step_hz': decoded.frequency_step_hz,
        }),
        'source_sha256': sha256(raw).hexdigest(),
        'importer_version': CAD_REW_API_ADAPTER_VERSION,
    }


def _rederive_declared_dataset(raw: bytes) -> dict[str, Any]:
    """Decode a canonical self-declared FR payload into dataset fields."""
    payload = _canonical_payload(
        raw,
        format_value=HTDT_DECLARED_FR_FORMAT,
        label='declared frequency-response',
    )
    if set(payload) != {
        'format',
        'frequency_hz',
        'level_db',
        'phase_deg',
        'phase_status',
        'level_reference',
        'smoothing',
        'processing',
    }:
        raise ValueError('declared frequency-response payload has unexpected fields')
    phase_deg = payload['phase_deg']
    if phase_deg is not None:
        phase_deg = _declared_floats(payload, 'phase_deg', 'declared frequency-response')
    phase_status = payload['phase_status']
    if phase_status not in ('valid', 'absent', 'unknown'):
        raise ValueError('declared frequency-response phase_status is invalid')
    level_reference = payload['level_reference']
    if not isinstance(level_reference, str) or not level_reference:
        raise ValueError('declared frequency-response level_reference must be a string')
    smoothing = payload['smoothing']
    if smoothing is not None and not isinstance(smoothing, str):
        raise ValueError('declared frequency-response smoothing must be a string or null')
    processing = payload['processing']
    if not isinstance(processing, dict):
        raise ValueError('declared frequency-response processing must be an object')
    return {
        'frequency_hz': _declared_floats(payload, 'frequency_hz', 'declared frequency-response'),
        'level_db': _declared_floats(payload, 'level_db', 'declared frequency-response'),
        'phase_deg': phase_deg,
        'phase_status': phase_status,
        'level_reference': level_reference,
        'smoothing': smoothing,
        'processing_json': canonical_json(processing),
        'source_sha256': sha256(raw).hexdigest(),
        'importer_version': HTDT_DECLARED_IMPORTER_VERSION,
    }


@dataclass(frozen=True)
class ImportTransformationAuthority:
    """A versioned importer that can prove a dataset derives from raw bytes.

    ``rederive`` reruns the exact pinned transformation (parse, decode,
    normalization) against the persisted raw asset and returns every semantic
    ``CadFrequencyResponseDataset`` field it produces — samples,
    interpretation metadata, ``processing_json``, ``source_sha256`` and the
    importer version — so persistence can require exact equality instead of
    trusting caller-provided arrays paired with a matching raw SHA.
    """

    importer_version: str
    source_kind: str
    rederive: Callable[[bytes], dict[str, Any]]


# Versioned import-transformation authority registry. Every persisted
# importer_version must resolve to an authority that rederives the canonical
# dataset from the exact raw asset. Importer versions without a registered
# authority — including historical ones — are non-authoritative: persistence
# and reads fail closed instead of trusting the stored payload.
IMPORT_TRANSFORMATION_AUTHORITIES: dict[str, ImportTransformationAuthority] = {
    authority.importer_version: authority
    for authority in (
        ImportTransformationAuthority(
            importer_version=PARSER_VERSION,
            source_kind='rew_text',
            rederive=_rederive_rew_text_dataset,
        ),
        ImportTransformationAuthority(
            importer_version=CAD_REW_API_ADAPTER_VERSION,
            source_kind='rew_api',
            rederive=_rederive_rew_api_snapshot_dataset,
        ),
        ImportTransformationAuthority(
            importer_version=HTDT_DECLARED_IMPORTER_VERSION,
            source_kind='unknown',
            rederive=_rederive_declared_dataset,
        ),
    )
}


def import_transformation_authority(importer_version: str) -> ImportTransformationAuthority:
    """Resolve a persisted importer version to its registered authority."""
    authority = IMPORT_TRANSFORMATION_AUTHORITIES.get(importer_version)
    if authority is None:
        raise ValueError(
            'no registered import transformation authority: '
            f'{importer_version}'
        )
    return authority


def import_transformation_sha256(
    *,
    source_sha256: str,
    importer_version: str,
    dataset_sha256: str,
) -> str:
    """Seal exact raw source + pinned importer identity + dataset identity."""
    return sha256(
        canonical_json({
            'transformation_version': IMPORT_TRANSFORMATION_VERSION,
            'source_sha256': source_sha256,
            'importer_version': importer_version,
            'dataset_sha256': dataset_sha256,
        }).encode('utf-8')
    ).hexdigest()


def verify_imported_dataset(
    dataset: CadFrequencyResponseDataset,
    raw_bytes: bytes,
    *,
    source_kind: str | None = None,
) -> None:
    """Require ``dataset`` to be the canonical output of its pinned importer.

    Reruns the registered versioned transformation for
    ``dataset.importer_version`` against the exact raw bytes and demands the
    rederived dataset equal the submitted/persisted one in every semantic
    field. When ``source_kind`` is given (the measurement record's declared
    source kind), the authority's source kind must match it so a dataset
    cannot claim a different acquisition path than its pinned importer.

    A dataset that merely pairs valid arrays with a matching raw SHA — or a
    persisted row rewritten coherently, hash columns and all — fails closed
    here because the stored samples must literally rederive from the raw
    asset under the pinned importer.
    """
    authority = import_transformation_authority(dataset.importer_version)
    if source_kind is not None and authority.source_kind != source_kind:
        raise ValueError(
            'dataset importer does not match the measurement source kind'
        )
    derived = authority.rederive(raw_bytes)
    try:
        expected = CadFrequencyResponseDataset(
            dataset_id=dataset.dataset_id,
            measurement_id=dataset.measurement_id,
            **derived,
        )
    except ValidationError as exc:
        raise ValueError(
            'import transformation output is not a valid dataset'
        ) from exc
    if expected != dataset:
        raise ValueError(
            'dataset does not match the canonical import transformation '
            'output for its raw asset'
        )
