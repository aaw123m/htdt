"""Provider-lane Room prediction runs (#938).

The R170A low-band wave lane and the R170B hybrid lane never re-simulate
inside the Room flow: a run binds the *exact persisted provider authority*
into the canonical request (the sealed provider payload is embedded verbatim
in ``input_snapshot_json``) and projects its stored response for the selected
receiver through ``provider.frequency_response`` /
``hybrid_provider_frequency_response``. Persistence replays both sides
deterministically — the input authority re-derives the canonical request from
the embedded provider, and the output authority re-projects the stored
frequency response — so a rewritten row still fails closed on save and read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import uuid4

from .cad_hybrid_prediction_provider import (
    HybridPredictionProvider,
    hybrid_provider_frequency_response,
)
from .cad_prediction_models import (
    CadPredictedProviderResponse,
    CadPredictionResult,
    canonical_prediction_json,
    prediction_input_hash,
    prediction_result_sha256,
)
from .cad_prediction_provider import LowBandPredictionProvider
from .cad_repository import SceneRevision


PROVIDER_RESPONSE_MODEL_ID = 'htdt.r170a_provider_response'
PROVIDER_RESPONSE_MODEL_VERSION = 'r170a-provider-response-1'
HYBRID_RESPONSE_MODEL_ID = 'htdt.r170b_hybrid_response'
HYBRID_RESPONSE_MODEL_VERSION = 'r170b-hybrid-response-1'

PROVIDER_RESULT_KIND = 'provider_frequency_response'
PROVIDER_GEOMETRY_COMPATIBILITY = 'persisted_provider_evidence'

_PROVIDER_RESPONSE_ASSUMPTIONS = (
    'provider_lane_persists_exact_stored_authority_output',
    'provider_lane_replays_persisted_evidence_not_a_solver_run',
)

ProviderLaneProvider = LowBandPredictionProvider | HybridPredictionProvider


@dataclass(frozen=True)
class ProviderResponseRequestIdentity:
    model_id: str
    model_version: str
    parameters_json: str
    input_snapshot_json: str
    input_hash: str
    geometry_compatibility: str


def _provider_kind(provider: ProviderLaneProvider) -> str:
    if isinstance(provider, LowBandPredictionProvider):
        return 'r170a'
    return 'r170b'


def _provider_authority(provider: ProviderLaneProvider):
    """The provider's revision-binding authority (named differently per lane)."""

    if isinstance(provider, LowBandPredictionProvider):
        return provider.current_authority
    return provider.base_current_authority


def _canonical_provider(provider: ProviderLaneProvider) -> ProviderLaneProvider:
    if isinstance(provider, LowBandPredictionProvider):
        return LowBandPredictionProvider.model_validate(
            provider.model_dump(mode='python')
        )
    return HybridPredictionProvider.model_validate(
        provider.model_dump(mode='python')
    )


def provider_response_request_identity(
    provider: ProviderLaneProvider,
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float,
) -> ProviderResponseRequestIdentity:
    """Canonical request binding one exact provider authority to one receiver."""

    provider = _canonical_provider(provider)
    authority = _provider_authority(provider)
    if (
        authority.document_id != revision.document_id
        or authority.scene_revision_id != revision.revision_id
        or authority.scene_content_hash != revision.content_hash
    ):
        raise ValueError(
            'provider current authority does not bind this SceneRevision'
        )

    if isinstance(provider, LowBandPredictionProvider):
        response = next(
            (
                item
                for item in provider.receiver_responses
                if item.receiver_entity_id == receiver_entity_id
            ),
            None,
        )
        if response is None:
            raise ValueError(
                'receiver entity is not part of the provider receiver set'
            )
        source_entity_id = authority.source_entity_id
    else:
        receiver_binding = provider.receiver_identity.receiver_binding
        if receiver_binding.entity_id != receiver_entity_id:
            raise ValueError(
                'receiver entity is not part of the hybrid provider receiver set'
            )
        source_entity_id = provider.source_entity_id

    provider.require_observable('frequency_response_magnitude')
    domain = provider.valid_frequency_domain
    if float(max_mode_hz) > float(domain.maximum_hz):
        raise ValueError(
            'requested band exceeds the provider valid frequency domain'
        )
    if float(max_mode_hz) < float(domain.minimum_hz):
        raise ValueError(
            'requested band is below the provider valid frequency domain'
        )

    parameters = canonical_prediction_json(
        {
            'provider_id': provider.provider_id,
            'provider_sha256': provider.semantic_sha256,
            'provider_kind': _provider_kind(provider),
            'receiver_entity_id': receiver_entity_id,
            'source_entity_id': source_entity_id,
            'max_mode_hz': float(max_mode_hz),
        }
    )
    snapshot = canonical_prediction_json(
        {
            'request_snapshot_version': 1,
            'document_id': revision.document_id,
            'scene_revision_id': revision.revision_id,
            'scene_content_hash': revision.content_hash,
            'receiver_entity_id': receiver_entity_id,
            'source_entity_id': source_entity_id,
            'provider_kind': _provider_kind(provider),
            'provider': provider.model_dump(mode='json'),
        }
    )
    if isinstance(provider, LowBandPredictionProvider):
        model_id = PROVIDER_RESPONSE_MODEL_ID
        model_version = PROVIDER_RESPONSE_MODEL_VERSION
    else:
        model_id = HYBRID_RESPONSE_MODEL_ID
        model_version = HYBRID_RESPONSE_MODEL_VERSION
    return ProviderResponseRequestIdentity(
        model_id=model_id,
        model_version=model_version,
        parameters_json=parameters,
        input_snapshot_json=snapshot,
        input_hash=prediction_input_hash(snapshot),
        geometry_compatibility=PROVIDER_GEOMETRY_COMPATIBILITY,
    )


def _decode_provider_request(
    parameters_json: str,
    input_snapshot_json: str,
) -> tuple[ProviderLaneProvider, str, float]:
    """Re-validate the exact provider authority embedded in a persisted run."""

    try:
        parameters = json.loads(parameters_json)
    except json.JSONDecodeError as exc:
        raise ValueError('provider parameters_json must contain JSON') from exc
    if not isinstance(parameters, dict):
        raise ValueError('provider parameters_json must be a JSON object')
    keys = {
        'provider_id',
        'provider_sha256',
        'provider_kind',
        'receiver_entity_id',
        'source_entity_id',
        'max_mode_hz',
    }
    if set(parameters) != keys:
        raise ValueError('provider parameters_json has an unknown shape')
    receiver_entity_id = parameters['receiver_entity_id']
    max_mode_hz = parameters['max_mode_hz']
    if not isinstance(receiver_entity_id, str) or not receiver_entity_id:
        raise ValueError('provider request must name a receiver entity')
    if isinstance(max_mode_hz, bool) or not isinstance(max_mode_hz, (int, float)):
        raise ValueError('provider request max_mode_hz must be numeric')

    try:
        snapshot = json.loads(input_snapshot_json)
    except json.JSONDecodeError as exc:
        raise ValueError('provider input_snapshot_json must contain JSON') from exc
    if not isinstance(snapshot, dict):
        raise ValueError('provider input_snapshot_json must be a JSON object')
    if snapshot.get('request_snapshot_version') != 1:
        raise ValueError('provider input snapshot version is unsupported')
    if snapshot.get('receiver_entity_id') != receiver_entity_id:
        raise ValueError('provider request receiver is inconsistent')
    provider_kind = snapshot.get('provider_kind')
    payload = snapshot.get('provider')
    if not isinstance(payload, dict):
        raise ValueError('provider input snapshot does not embed a provider')
    try:
        if provider_kind == 'r170a':
            provider: ProviderLaneProvider = (
                LowBandPredictionProvider.model_validate(payload)
            )
        elif provider_kind == 'r170b':
            provider = HybridPredictionProvider.model_validate(payload)
        else:
            raise ValueError('provider request kind is unsupported')
    except ValueError:
        raise
    except Exception as exc:  # error-boundary: error translation — a snapshot re-validation failure wraps as ValueError with the original failure preserved via 'from exc' (noqa: BLE001)
        raise ValueError(
            'provider input snapshot authority fails to re-validate'
        ) from exc
    if (
        provider.provider_id != parameters['provider_id']
        or provider.semantic_sha256 != parameters['provider_sha256']
    ):
        raise ValueError(
            'provider parameters do not match the embedded authority'
        )
    return provider, receiver_entity_id, float(max_mode_hz)


def replay_provider_response_request(
    revision: SceneRevision,
    parameters_json: str,
    input_snapshot_json: str,
) -> ProviderResponseRequestIdentity:
    """Input replayer: re-derive the canonical request for one provider run."""

    provider, receiver_entity_id, max_mode_hz = _decode_provider_request(
        parameters_json,
        input_snapshot_json,
    )
    authority = _provider_authority(provider)
    if (
        authority.document_id != revision.document_id
        or authority.scene_revision_id != revision.revision_id
        or authority.scene_content_hash != revision.content_hash
    ):
        raise ValueError(
            'provider authority does not bind the stored SceneRevision'
        )
    return provider_response_request_identity(
        provider,
        revision,
        receiver_entity_id,
        max_mode_hz=max_mode_hz,
    )


def _provider_response(
    provider: ProviderLaneProvider,
    receiver_entity_id: str,
    max_mode_hz: float,
) -> CadPredictedProviderResponse:
    """Project the provider's exact stored output for one receiver/band."""

    domain = provider.valid_frequency_domain
    low_hz = float(domain.minimum_hz)
    high_hz = min(float(domain.maximum_hz), float(max_mode_hz))
    if isinstance(provider, LowBandPredictionProvider):
        response = next(
            (
                item
                for item in provider.receiver_responses
                if item.receiver_entity_id == receiver_entity_id
            ),
            None,
        )
        if response is None:
            raise ValueError(
                'receiver entity is not part of the provider receiver set'
            )
        fr = provider.frequency_response(response.receiver_id)
        selected_hz = tuple(
            float(f) for f in fr.frequency_hz if low_hz <= float(f) <= high_hz
        )
        levels = {
            float(f): float(v)
            for f, v in zip(fr.frequency_hz, fr.level_db)
        }
        level_db = tuple(levels[f] for f in selected_hz)
    else:
        receiver_binding = provider.receiver_identity.receiver_binding
        if receiver_binding.entity_id != receiver_entity_id:
            raise ValueError(
                'receiver entity is not part of the hybrid provider receiver set'
            )
        fr = hybrid_provider_frequency_response(
            provider,
            source_entity_id=provider.source_entity_id,
            receiver_id=provider.receiver_id,
            low_hz=low_hz,
            high_hz=high_hz,
        )
        selected_hz = fr.frequency_hz
        level_db = fr.level_db
    if len(selected_hz) < 2:
        raise ValueError(
            'provider band selection yields fewer than two samples'
        )
    return CadPredictedProviderResponse(
        provider_id=provider.provider_id,
        provider_semantic_sha256=provider.semantic_sha256,
        provider_adapter_id=provider.adapter_id,
        provider_adapter_version=provider.adapter_version,
        provider_evidence_state=provider.evidence_state,
        provider_evidence_scope=provider.evidence_scope,
        receiver_entity_id=receiver_entity_id,
        frequency_hz=tuple(selected_hz),
        level_db=tuple(level_db),
    )


def analyze_provider_frequency_response(
    provider: ProviderLaneProvider,
    revision: SceneRevision,
    receiver_entity_id: str,
    *,
    max_mode_hz: float,
    constraint_workspace_hash: str | None = None,
) -> tuple[CadPredictionResult, ...]:
    """Run the provider lane: persist the exact stored response as the result."""

    identity = provider_response_request_identity(
        provider,
        revision,
        receiver_entity_id,
        max_mode_hz=max_mode_hz,
    )
    payload = _provider_response(provider, receiver_entity_id, max_mode_hz)
    run_id = str(uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()
    completed_at = datetime.now(timezone.utc).isoformat()
    fields = {
        'prediction_id': str(uuid4()),
        'run_id': run_id,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'constraint_workspace_hash': constraint_workspace_hash,
        'model_id': identity.model_id,
        'model_version': identity.model_version,
        'result_kind': PROVIDER_RESULT_KIND,
        'geometry_compatibility': identity.geometry_compatibility,
        'parameters_json': identity.parameters_json,
        'input_snapshot_json': identity.input_snapshot_json,
        'input_hash': identity.input_hash,
        'submitted_at_utc': submitted_at,
        'completed_at_utc': completed_at,
        'assumptions': _PROVIDER_RESPONSE_ASSUMPTIONS,
        'warnings': (),
        'provider_response': payload,
    }
    provisional = CadPredictionResult.model_construct(
        result_sha256='0' * 64,
        **fields,
    )
    result = CadPredictionResult(
        result_sha256=prediction_result_sha256(
            provisional.result_identity_payload()
        ),
        **fields,
    )
    return (result,)


def replay_provider_response_run(
    revision: SceneRevision,
    parameters_json: str,
    input_snapshot_json: str,
    constraint_workspace_hash: str | None,
) -> tuple[CadPredictionResult, ...]:
    """Output replayer: re-project the embedded provider's stored response."""

    provider, receiver_entity_id, max_mode_hz = _decode_provider_request(
        parameters_json,
        input_snapshot_json,
    )
    identity = provider_response_request_identity(
        provider,
        revision,
        receiver_entity_id,
        max_mode_hz=max_mode_hz,
    )
    payload = _provider_response(provider, receiver_entity_id, max_mode_hz)
    fields = {
        'prediction_id': 'replay',
        'run_id': 'replay',
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'constraint_workspace_hash': constraint_workspace_hash,
        'model_id': identity.model_id,
        'model_version': identity.model_version,
        'result_kind': PROVIDER_RESULT_KIND,
        'geometry_compatibility': identity.geometry_compatibility,
        'parameters_json': identity.parameters_json,
        'input_snapshot_json': identity.input_snapshot_json,
        'input_hash': identity.input_hash,
        'submitted_at_utc': 'replay',
        'completed_at_utc': 'replay',
        'assumptions': _PROVIDER_RESPONSE_ASSUMPTIONS,
        'warnings': (),
        'provider_response': payload,
    }
    provisional = CadPredictionResult.model_construct(
        result_sha256='0' * 64,
        **fields,
    )
    result = CadPredictionResult(
        result_sha256=prediction_result_sha256(
            provisional.result_identity_payload()
        ),
        **fields,
    )
    return (result,)


def embedded_run_provider(
    result: CadPredictionResult,
) -> ProviderLaneProvider | None:
    """The exact provider authority a provider-lane result was built from.

    Decoded from the canonical input snapshot so interpretation binds the same
    sealed authority the run persisted — never a repository lookup keyed by a
    mutable provider id.
    """

    if result.model_id not in (
        PROVIDER_RESPONSE_MODEL_ID,
        HYBRID_RESPONSE_MODEL_ID,
    ):
        return None
    provider, _entity_id, _band = _decode_provider_request(
        result.parameters_json,
        result.input_snapshot_json,
    )
    return provider
