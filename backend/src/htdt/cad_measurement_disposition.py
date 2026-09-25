"""Append-only disposition and assignment-correction authority for measurements.

A persisted measurement and its raw dataset are immutable evidence. When the
human binding was wrong — wrong seat, wrong channel, a test capture, or a
duplicate import — we never rewrite or delete the record. Instead we append
lifecycle events:

* ``CadMeasurementDisposition`` — the current lifecycle state of the
  measurement (active, misassigned, excluded, test-only, duplicate import,
  corrected). The latest event wins; the full log stays inspectable.
* ``CadMeasurementCorrection`` — an explicit corrected evidence binding
  pointing at the exact bound dataset. Corrections never claim a new physical
  acquisition: they re-target the same immutable dataset to different
  entities/channels.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .r120_geometry_compiler import ExactExternalAuthorityRef


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


MeasurementDispositionState = Literal[
    'active',
    'corrected',
    'misassigned',
    'excluded_from_normal_use',
    'test_only',
    'duplicate_import',
]

MEASUREMENT_DISPOSITION_STATES = (
    'active',
    'corrected',
    'misassigned',
    'excluded_from_normal_use',
    'test_only',
    'duplicate_import',
)

# Dispositions under which a measurement stays normally eligible for downstream
# analysis. Anything else remains inspectable but must not silently feed
# selectors, comparisons, or optimization gates.
MEASUREMENT_ELIGIBLE_DISPOSITIONS = frozenset({'active', 'corrected'})


class CadMeasurementDisposition(BaseModel):
    """One lifecycle event for a measurement. Append-only; latest wins."""

    model_config = ConfigDict(frozen=True)

    disposition_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    disposition: MeasurementDispositionState
    reason: str = Field(min_length=1)
    correction_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    disposition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_disposition(self) -> 'CadMeasurementDisposition':
        if self.disposition == 'corrected' and self.correction_id is None:
            raise ValueError('corrected disposition must pin the correction record')
        if self.disposition != 'corrected' and self.correction_id is not None:
            raise ValueError('correction_id is only valid for corrected dispositions')
        if self.disposition_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement disposition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'disposition_id': self.disposition_id,
            'document_id': self.document_id,
            'measurement_id': self.measurement_id,
            'disposition': self.disposition,
            'reason': self.reason,
            'correction_id': self.correction_id,
            'created_at_utc': self.created_at_utc,
        }


# Explicit correction semantics (#863): relabeling a measurement onto another
# target is never allowed to silently change its physical-position claim.
# ``assignment_label_only`` is the entity-relabel case that is only valid when
# the corrected target's acoustic reference equals the immutable import
# position exactly. ``assignment_with_pose_evidence`` additionally pins an
# exact pose-observation authority when the corrected target's position
# differs. ``channel_routing_only`` never touches the target entity.
CorrectionKind = Literal[
    'assignment_label_only',
    'assignment_with_pose_evidence',
    'channel_routing_only',
]

CORRECTION_KINDS = (
    'assignment_label_only',
    'assignment_with_pose_evidence',
    'channel_routing_only',
)


class CadMeasurementCorrection(BaseModel):
    """Corrected evidence binding for a measurement.

    The record pins the exact bound dataset (``dataset_id`` +
    ``dataset_sha256``) so the corrected view can never drift onto different
    evidence. Each field that is not None replaces the original assignment;
    ``source_speaker_ids`` replaces the whole tuple when provided. At least one
    field must change.

    ``correction_kind`` is optional additive metadata (#863) — spatial truth is
    enforced by the repository regardless of the declared kind: a target-entity
    correction whose acoustic reference differs from the immutable measurement
    position is only persistable with ``pose_evidence_ref`` pinned to a
    resolved actual-pose observation authority (#732).
    """

    model_config = ConfigDict(frozen=True)

    correction_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_entity_id: str | None = Field(default=None, min_length=1)
    channel_role: str | None = Field(default=None, min_length=1)
    source_speaker_ids: tuple[str, ...] | None = None
    radiation_scope: str | None = Field(default=None, min_length=1)
    routing_evidence: str | None = Field(default=None, min_length=1)
    correction_kind: CorrectionKind | None = None
    pose_evidence_ref: ExactExternalAuthorityRef | None = None
    reason: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    correction_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_correction(self) -> 'CadMeasurementCorrection':
        if (
            self.measurement_entity_id is None
            and self.channel_role is None
            and self.source_speaker_ids is None
            and self.radiation_scope is None
            and self.routing_evidence is None
        ):
            raise ValueError('correction must change at least one assignment field')
        if (
            self.correction_kind == 'assignment_with_pose_evidence'
            and self.pose_evidence_ref is None
        ):
            raise ValueError(
                'assignment_with_pose_evidence correction must pin '
                'pose_evidence_ref'
            )
        if (
            self.correction_kind == 'channel_routing_only'
            and self.measurement_entity_id is not None
        ):
            raise ValueError(
                'channel_routing_only correction cannot replace the target '
                'entity'
            )
        if self.correction_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement correction hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'correction_id': self.correction_id,
            'document_id': self.document_id,
            'measurement_id': self.measurement_id,
            'dataset_id': self.dataset_id,
            'dataset_sha256': self.dataset_sha256,
            'measurement_entity_id': self.measurement_entity_id,
            'channel_role': self.channel_role,
            'source_speaker_ids': list(self.source_speaker_ids)
            if self.source_speaker_ids is not None
            else None,
            'radiation_scope': self.radiation_scope,
            'routing_evidence': self.routing_evidence,
            'reason': self.reason,
            'created_at_utc': self.created_at_utc,
        }
        # Optional #863 fields join identity only when present so pre-
        # correction-kind rows keep their hashes.
        if self.correction_kind is not None:
            payload['correction_kind'] = self.correction_kind
        if self.pose_evidence_ref is not None:
            payload['pose_evidence_ref'] = self.pose_evidence_ref.model_dump(
                mode='json'
            )
        return payload


def build_measurement_disposition(
    *,
    document_id: str,
    measurement_id: str,
    disposition: MeasurementDispositionState,
    reason: str,
    correction_id: str | None = None,
    disposition_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadMeasurementDisposition:
    payload = {
        'disposition_id': disposition_id or str(uuid4()),
        'document_id': document_id,
        'measurement_id': measurement_id,
        'disposition': disposition,
        'reason': reason,
        'correction_id': correction_id,
        'created_at_utc': created_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    return CadMeasurementDisposition(
        **payload,
        disposition_sha256=_hash(payload),
    )


def build_measurement_correction(
    *,
    document_id: str,
    measurement_id: str,
    dataset_id: str,
    dataset_sha256: str,
    reason: str,
    measurement_entity_id: str | None = None,
    channel_role: str | None = None,
    source_speaker_ids: tuple[str, ...] | None = None,
    radiation_scope: str | None = None,
    routing_evidence: str | None = None,
    correction_kind: CorrectionKind | None = None,
    pose_evidence_ref: ExactExternalAuthorityRef | None = None,
    correction_id: str | None = None,
    created_at_utc: str | None = None,
) -> CadMeasurementCorrection:
    payload = {
        'correction_id': correction_id or str(uuid4()),
        'document_id': document_id,
        'measurement_id': measurement_id,
        'dataset_id': dataset_id,
        'dataset_sha256': dataset_sha256,
        'measurement_entity_id': measurement_entity_id,
        'channel_role': channel_role,
        'source_speaker_ids': list(source_speaker_ids)
        if source_speaker_ids is not None
        else None,
        'radiation_scope': radiation_scope,
        'routing_evidence': routing_evidence,
        'reason': reason,
        'created_at_utc': created_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    provisional = CadMeasurementCorrection.model_construct(
        **payload,
        correction_kind=correction_kind,
        pose_evidence_ref=pose_evidence_ref,
        correction_sha256='0' * 64,
    )
    return CadMeasurementCorrection(
        **payload,
        correction_kind=correction_kind,
        pose_evidence_ref=pose_evidence_ref,
        correction_sha256=_hash(provisional.identity_payload()),
    )


__all__ = [
    'CORRECTION_KINDS',
    'CadMeasurementCorrection',
    'CadMeasurementDisposition',
    'CorrectionKind',
    'MEASUREMENT_DISPOSITION_STATES',
    'MEASUREMENT_ELIGIBLE_DISPOSITIONS',
    'MeasurementDispositionState',
    'build_measurement_correction',
    'build_measurement_disposition',
]
