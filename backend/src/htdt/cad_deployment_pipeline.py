"""#878 deployment pipeline authority — staged transaction records,
strongest-path evaluation and the assisted fallback contract.

One deployment run against one bound target advances through a pinned
stage ladder::

    baseline_captured -> compiled -> previewed -> authorized -> applied
      -> readback_matched/readback_diverged -> rollback_verified/failed

Every stage emits a new sealed :class:`DeploymentPipelineRecord` that
carries the full evidence accumulated so far — baseline snapshot pin,
compiled materialization sha, the semantic diff preview shown to the
operator, the one-shot operator authorization consumed immediately
before mutation, the apply ack with partial-write counts, the read-back
snapshot pin and the rollback outcome. ``evidence_strength`` is derived
structurally: machine read-back > applied ack > file-verified >
assisted attestation > unverified. The field can never exceed what the
pinned mechanism/verdict fields justify — a mismatched claim fails the
seal validator.

Fail-closed everywhere: unknown capability is unavailable, undeclared
control surfaces are never claimed, reverse-engineered/private
protocols and GUI automation are not production authority, and
machine-read-back evidence is never claimable from file writes.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_calibration import CadCalibrationExportSnapshot
from .cad_deployment_target import (
    DSPReadbackMechanism,
    DSPTargetProfile,
    get_dsp_target_profile,
)
from .cad_device_adapter import (
    AdapterCapabilityReport,
    AdapterCapabilityError,
    AdapterDeviceBinding,
    CalibrationDeviceAdapter,
    DeviceApplyAck,
    EffectiveAppliedSettingsSnapshot,
    MaterializedCalibrationSettings,
    build_observation,
    validate_materialization_result,
)
from .cad_equalizer_apo_export import (
    ApoExportChannel,
    verify_exported_apo_config,
)
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary

PipelineStage = Literal[
    'opened',
    'baseline_captured',
    'baseline_unavailable',
    'compiled',
    'previewed',
    'authorized',
    'applied',
    'partial_write',
    'readback_matched',
    'readback_diverged',
    'rollback_verified',
    'rollback_failed',
    'failed',
    'aborted',
]

#: Deployment evidence ladder — strictly ordered, never a single score.
DeploymentEvidenceStrength = Literal[
    'machine_readback',
    'applied_ack',
    'file_verified',
    'assisted_attestation',
    'unverified',
    'none',
]

_DEPLOYMENT_STRENGTH_RANK: dict[str, int] = {
    'machine_readback': 5,
    'applied_ack': 4,
    'file_verified': 3,
    'assisted_attestation': 2,
    'unverified': 1,
    'none': 0,
}

ReadbackVerdict = Literal['matched', 'diverged', 'not_attempted', 'unavailable']
PartialWriteState = Literal[
    'not_attempted', 'none', 'complete', 'partial', 'failed',
]
PipelineRollbackOutcome = Literal[
    'verified', 'failed', 'not_attempted', 'unavailable',
]
BaselineSource = Literal[
    'machine_capture', 'machine_readback', 'unavailable', 'not_attempted',
]
PathAvailability = Literal['available', 'conditional', 'unavailable']
MinidspDeployClass = Literal['machine_deployable', 'assisted_only']
AssistedStepAction = Literal[
    'produce_artifact',
    'write_file',
    'import_file',
    'enter_setting',
    'connect_device',
    'power_sequence',
    'verify_presence',
    'confirm',
]
AssistedOutcome = Literal['attested', 'partial', 'rejected']
ApoVerification = Literal[
    'content_sha_matched', 'content_sha_mismatch', 'install_failed',
    'not_verified',
]

#: supported_features vocabulary marking installed-file identity
#: verification (content sha of the *installed* file, not "file written").
FILE_IDENTITY_VERIFICATION_FEATURE = 'file_identity_verification'

_PRODUCTION_PROTOCOL_AUTHORITIES = frozenset({'documented', 'open_source'})


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal_id(
    model_cls: type[BaseModel],
    prefix: str,
    id_field: str,
    sha_field: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    probe = model_cls.model_construct(
        **canonicalize_payload(model_cls, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return _semantic_id(prefix, digest), digest


class PipelineError(Exception):
    """Base for pipeline-layer failures (fail-closed surfaces)."""


class PipelineStageError(PipelineError):
    """A stage was requested out of order or without its prerequisites."""


class PipelineAuthorizationError(PipelineError):
    """Operator authorization was missing, scoped wrong or consumed."""


class UnknownMinidspModelError(PipelineError):
    """miniDSP deployability was asked for an unregistered model."""


# ---------------------------------------------------------------------------
# Semantic diff preview — what the operator sees BEFORE authorizing.

class SemanticDiffEntry(BaseModel):
    """One channel/field change the apply will make.

    ``before_repr`` is the observed/baseline value, ``after_repr`` the
    compiled target value. ``kind`` records which side was observable —
    'unobserved_before' keeps a missing baseline visibly weaker.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    channel_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    before_repr: str
    after_repr: str
    kind: Literal['change', 'unobserved_before'] = 'change'


# ---------------------------------------------------------------------------
# Operator authorization — sealed, scoped, one-shot.

AuthorizationScope = Literal['apply', 'rollback']


class DeploymentOperatorAuthorization(BaseModel):
    """Explicit operator authorization immediately before mutation.

    Scoped ('apply' or 'rollback'), pinned to the exact candidate
    materialization sha (apply) or pipeline id (rollback), and consumed
    one-shot — a second mutation needs a second authorization record.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authorization_id: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    pipeline_id: str = Field(min_length=1)
    scope: AuthorizationScope
    #: apply: the materialization being authorized; rollback: None.
    candidate_materialization_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$',
    )
    operator_id: str = Field(min_length=1)
    authorized_at_utc: str = Field(min_length=1)
    expires_at_utc: str | None = None
    consumed: bool = False
    consumed_by_record_id: str | None = None

    @model_validator(mode='after')
    def valid_authorization(self) -> 'DeploymentOperatorAuthorization':
        if self.scope == 'apply' and self.candidate_materialization_sha256 is None:
            raise ValueError('apply authorization must pin a materialization')
        if self.consumed and self.consumed_by_record_id is None:
            raise ValueError('consumed authorization must name the record')
        if not self.consumed and self.consumed_by_record_id is not None:
            raise ValueError('unconsumed authorization cannot name a record')
        if self.expires_at_utc is not None \
                and self.expires_at_utc <= self.authorized_at_utc:
            raise ValueError('expiry must be after authorized_at_utc')
        if self.authorization_sha256 != _hash(self.identity_payload()):
            raise ValueError('DeploymentOperatorAuthorization hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'authorization_id', 'authorization_sha256'},
        )


def build_operator_authorization(
    *,
    document_id: str,
    pipeline_id: str,
    scope: AuthorizationScope,
    operator_id: str,
    authorized_at_utc: str,
    candidate_materialization_sha256: str | None = None,
    expires_at_utc: str | None = None,
    consumed: bool = False,
    consumed_by_record_id: str | None = None,
) -> DeploymentOperatorAuthorization:
    payload: dict[str, Any] = {
        'document_id': document_id,
        'pipeline_id': pipeline_id,
        'scope': scope,
        'candidate_materialization_sha256': candidate_materialization_sha256,
        'operator_id': operator_id,
        'authorized_at_utc': authorized_at_utc,
        'expires_at_utc': expires_at_utc,
        'consumed': consumed,
        'consumed_by_record_id': consumed_by_record_id,
    }
    rid, digest = _seal_id(
        DeploymentOperatorAuthorization, 'doa',
        'authorization_id', 'authorization_sha256', payload,
    )
    return DeploymentOperatorAuthorization(
        authorization_id=rid, authorization_sha256=digest, **payload,
    )


def consume_authorization(
    authorization: DeploymentOperatorAuthorization,
    record_id: str,
) -> DeploymentOperatorAuthorization:
    """Return the consumed copy of a one-shot authorization."""
    if authorization.consumed:
        raise PipelineAuthorizationError('authorization already consumed')
    return build_operator_authorization(
        document_id=authorization.document_id,
        pipeline_id=authorization.pipeline_id,
        scope=authorization.scope,
        operator_id=authorization.operator_id,
        authorized_at_utc=authorization.authorized_at_utc,
        candidate_materialization_sha256=(
            authorization.candidate_materialization_sha256
        ),
        expires_at_utc=authorization.expires_at_utc,
        consumed=True,
        consumed_by_record_id=record_id,
    )


# ---------------------------------------------------------------------------
# Pipeline record — one sealed row per stage, carrying all evidence so far.

class DeploymentPipelineRecord(BaseModel):
    """Sealed staged-transaction record for one deployment run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    record_id: str = Field(min_length=1)
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    pipeline_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    adapter_id: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    stage: PipelineStage
    created_at_utc: str = Field(min_length=1)
    baseline_source: BaselineSource = 'not_attempted'
    baseline_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$',
    )
    materialization_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$',
    )
    preview_entries: tuple[SemanticDiffEntry, ...] = ()
    authorization_id: str | None = None
    ack_id: str | None = None
    applied_units: int | None = Field(default=None, ge=0)
    total_units: int | None = Field(default=None, ge=0)
    partial_write: PartialWriteState = 'not_attempted'
    #: Mechanism the read-back pin came through — gates evidence_strength.
    readback_mechanism: DSPReadbackMechanism = 'none'
    readback_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$',
    )
    readback_verdict: ReadbackVerdict = 'not_attempted'
    rollback_outcome: PipelineRollbackOutcome = 'not_attempted'
    assisted_attestation_id: str | None = None
    evidence_strength: DeploymentEvidenceStrength = 'none'
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_record(self) -> 'DeploymentPipelineRecord':
        expected = derive_pipeline_evidence_strength(self)
        if self.evidence_strength != expected:
            raise ValueError(
                f'evidence_strength {self.evidence_strength} exceeds derived '
                f'{expected} — machine-readback evidence is never claimable '
                'below its mechanism'
            )
        if self.baseline_sha256 is not None and self.baseline_source in (
            'unavailable', 'not_attempted',
        ):
            raise ValueError('baseline pin requires a real baseline source')
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('DeploymentPipelineRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'},
        )


def derive_pipeline_evidence_strength(
    record: 'DeploymentPipelineRecord | dict[str, Any]',
) -> DeploymentEvidenceStrength:
    """Evidence strength the pinned fields justify — structural ceiling."""
    get = (
        record.get if isinstance(record, dict)
        else lambda key, default=None: getattr(record, key, default)
    )
    readback_verdict = get('readback_verdict')
    readback_mechanism = get('readback_mechanism')
    if readback_verdict == 'matched':
        if readback_mechanism == 'machine_exact':
            return 'machine_readback'
        if readback_mechanism == 'operator_captured_file':
            return 'file_verified'
    if get('ack_id') or get('partial_write') == 'complete':
        return 'applied_ack'
    if get('assisted_attestation_id'):
        return 'assisted_attestation'
    if get('stage') in ('opened', 'baseline_captured', 'baseline_unavailable',
                        'compiled', 'previewed', 'authorized'):
        return 'none'
    return 'unverified'


def build_pipeline_record(
    *,
    pipeline_id: str,
    document_id: str,
    binding_sha256: str,
    adapter_id: str,
    target_ref: str,
    stage: PipelineStage,
    created_at_utc: str,
    baseline_source: BaselineSource = 'not_attempted',
    baseline_sha256: str | None = None,
    materialization_sha256: str | None = None,
    preview_entries: tuple[SemanticDiffEntry, ...] = (),
    authorization_id: str | None = None,
    ack_id: str | None = None,
    applied_units: int | None = None,
    total_units: int | None = None,
    partial_write: PartialWriteState = 'not_attempted',
    readback_mechanism: DSPReadbackMechanism = 'none',
    readback_sha256: str | None = None,
    readback_verdict: ReadbackVerdict = 'not_attempted',
    rollback_outcome: PipelineRollbackOutcome = 'not_attempted',
    assisted_attestation_id: str | None = None,
    notes: tuple[str, ...] = (),
) -> DeploymentPipelineRecord:
    payload: dict[str, Any] = {
        'pipeline_id': pipeline_id,
        'document_id': document_id,
        'binding_sha256': binding_sha256,
        'adapter_id': adapter_id,
        'target_ref': target_ref,
        'stage': stage,
        'created_at_utc': created_at_utc,
        'baseline_source': baseline_source,
        'baseline_sha256': baseline_sha256,
        'materialization_sha256': materialization_sha256,
        'preview_entries': tuple(preview_entries),
        'authorization_id': authorization_id,
        'ack_id': ack_id,
        'applied_units': applied_units,
        'total_units': total_units,
        'partial_write': partial_write,
        'readback_mechanism': readback_mechanism,
        'readback_sha256': readback_sha256,
        'readback_verdict': readback_verdict,
        'rollback_outcome': rollback_outcome,
        'assisted_attestation_id': assisted_attestation_id,
        'notes': tuple(notes),
    }
    payload['evidence_strength'] = derive_pipeline_evidence_strength(payload)
    rid, digest = _seal_id(
        DeploymentPipelineRecord, 'dplr',
        'record_id', 'record_sha256', payload,
    )
    return DeploymentPipelineRecord(
        record_id=rid, record_sha256=digest, **payload,
    )


def derive_pipeline_stage(
    records: tuple[DeploymentPipelineRecord, ...],
) -> PipelineStage:
    """Latest stage of one pipeline run from its sealed record chain."""
    if not records:
        return 'opened'
    return records[-1].stage


# ---------------------------------------------------------------------------
# Assisted fallback contract — typed attestation, visibly weaker evidence.

class AssistedStepManifest(BaseModel):
    """One numbered human step in an assisted deployment."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    step_index: int = Field(ge=1)
    action: AssistedStepAction
    instruction: str = Field(min_length=1)
    artifact_ref: AuthorityRef | None = None


class AssistedInstructionManifest(BaseModel):
    """Step-specific instruction manifest for assisted deployment lanes."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    manifest_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    title: str = Field(min_length=1)
    steps: tuple[AssistedStepManifest, ...]
    #: True when the deployment can only be confirmed by a post-change
    #: acoustic measurement — the assisted lane never claims applied
    #: state as effective state.
    post_measurement_required: bool = True
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_manifest(self) -> 'AssistedInstructionManifest':
        indexes = [step.step_index for step in self.steps]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise ValueError('steps must be unique and ordered')
        if self.manifest_sha256 != _hash(self.identity_payload()):
            raise ValueError('AssistedInstructionManifest hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'manifest_id', 'manifest_sha256'},
        )


def build_assisted_manifest(
    *,
    document_id: str,
    target_ref: str,
    title: str,
    steps: tuple[AssistedStepManifest, ...],
    post_measurement_required: bool = True,
    notes: tuple[str, ...] = (),
) -> AssistedInstructionManifest:
    payload: dict[str, Any] = {
        'document_id': document_id,
        'target_ref': target_ref,
        'title': title,
        'steps': tuple(steps),
        'post_measurement_required': post_measurement_required,
        'notes': tuple(notes),
    }
    rid, digest = _seal_id(
        AssistedInstructionManifest, 'aim',
        'manifest_id', 'manifest_sha256', payload,
    )
    return AssistedInstructionManifest(
        manifest_id=rid, manifest_sha256=digest, **payload,
    )


class AssistedDeploymentAttestation(BaseModel):
    """Typed human attestation that assisted steps were performed.

    ``evidence_strength`` is structurally fixed at 'assisted_attestation'
    — visibly weaker than machine read-back in every downstream view.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    attestation_id: str = Field(min_length=1)
    attestation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    manifest_ref: AuthorityRef
    pipeline_id: str | None = None
    operator_attestor: str = Field(min_length=1)
    attested_at_utc: str = Field(min_length=1)
    completed_step_indexes: tuple[int, ...]
    outcome: AssistedOutcome
    post_measurement_required: bool = True
    artifact_refs: tuple[AuthorityRef, ...] = ()
    evidence_strength: Literal['assisted_attestation'] = (
        'assisted_attestation'
    )
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_attestation(self) -> 'AssistedDeploymentAttestation':
        if self.outcome == 'attested' and not self.completed_step_indexes:
            raise ValueError('attested outcome requires completed steps')
        if self.attestation_sha256 != _hash(self.identity_payload()):
            raise ValueError('AssistedDeploymentAttestation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'attestation_id', 'attestation_sha256'},
        )


def build_assisted_attestation(
    *,
    document_id: str,
    manifest: AssistedInstructionManifest,
    operator_attestor: str,
    attested_at_utc: str,
    completed_step_indexes: tuple[int, ...],
    outcome: AssistedOutcome,
    pipeline_id: str | None = None,
    artifact_refs: tuple[AuthorityRef, ...] = (),
    notes: tuple[str, ...] = (),
) -> AssistedDeploymentAttestation:
    declared = {step.step_index for step in manifest.steps}
    unknown = [i for i in completed_step_indexes if i not in declared]
    if unknown:
        raise PipelineError(
            f'attestation claims undeclared manifest steps: {unknown}'
        )
    if outcome == 'attested' and set(completed_step_indexes) != declared:
        raise PipelineError(
            'attested outcome requires every declared manifest step'
        )
    payload: dict[str, Any] = {
        'document_id': document_id,
        'manifest_ref': AuthorityRef(
            kind='assisted_instruction_manifest',
            ref_id=manifest.manifest_id,
            ref_sha256=manifest.manifest_sha256,
        ),
        'pipeline_id': pipeline_id,
        'operator_attestor': operator_attestor,
        'attested_at_utc': attested_at_utc,
        'completed_step_indexes': tuple(completed_step_indexes),
        'outcome': outcome,
        'post_measurement_required': manifest.post_measurement_required,
        'artifact_refs': tuple(artifact_refs),
        'notes': tuple(notes),
    }
    rid, digest = _seal_id(
        AssistedDeploymentAttestation, 'ada',
        'attestation_id', 'attestation_sha256', payload,
    )
    return AssistedDeploymentAttestation(
        attestation_id=rid, attestation_sha256=digest, **payload,
    )


# ---------------------------------------------------------------------------
# Equalizer APO bounded install — validates exact installed file identity.

class ApoInstallRecord(BaseModel):
    """Evidence for one Equalizer APO config install.

    ``verification`` is 'content_sha_matched' only when the bytes read
    back from the installed path hash identically to the rendered config
    — installed-file identity, distinct from "file written".
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    record_id: str = Field(min_length=1)
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    target_path_repr: str = Field(min_length=1)
    rendered_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    installed_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$',
    )
    semantic_verdict: Literal['matched', 'mismatch', 'not_checked'] = (
        'not_checked'
    )
    verification: ApoVerification
    evidence_strength: Literal['file_verified', 'none']
    created_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_install(self) -> 'ApoInstallRecord':
        if self.verification == 'content_sha_matched':
            if self.installed_sha256 is None:
                raise ValueError('matched verification needs installed sha')
            if self.installed_sha256 != self.rendered_sha256:
                raise ValueError('matched verification needs identical shas')
            if self.evidence_strength != 'file_verified':
                raise ValueError('matched install must be file_verified')
        elif self.evidence_strength != 'none':
            raise ValueError('unmatched install carries no file evidence')
        if self.record_sha256 != _hash(self.identity_payload()):
            raise ValueError('ApoInstallRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'record_id', 'record_sha256'},
        )


class EqualizerApoInstaller:
    """Bounded Equalizer APO install + installed-identity read-back.

    Writes the rendered config to an explicit operator-supplied path,
    re-reads the installed file and pins its content sha. It never
    discovers devices and never claims more than file-level evidence —
    the strongest reachable class is 'file_verified'.
    """

    def install(
        self,
        *,
        document_id: str,
        rendered_text: str,
        target_path: str | Path,
        created_at_utc: str,
        channels: tuple[ApoExportChannel, ...] | None = None,
        global_preamp_db: float | None = None,
        device: str | None = None,
    ) -> ApoInstallRecord:
        target = Path(target_path)
        rendered_sha = hashlib.sha256(
            rendered_text.encode('utf-8'),
        ).hexdigest()
        verification: ApoVerification
        installed_sha: str | None = None
        semantic_verdict: Literal['matched', 'mismatch', 'not_checked']
        notes: list[str] = []
        try:
            # bytes-exact write — installed-file identity is content sha
            target.write_bytes(rendered_text.encode('utf-8'))
        except OSError as exc:
            verification = 'install_failed'
            semantic_verdict = 'not_checked'
            notes.append(f'install failed: {exc}')
        else:
            installed_bytes = target.read_bytes()
            installed_sha = hashlib.sha256(installed_bytes).hexdigest()
            if installed_sha == rendered_sha:
                verification = 'content_sha_matched'
            else:
                verification = 'content_sha_mismatch'
                notes.append(
                    'installed file content differs from rendered bytes'
                )
            if channels is None:
                semantic_verdict = 'not_checked'
            else:
                # Semantic round-trip: the *installed* bytes must parse
                # back to the requested settings through the #808
                # importer — never just "a file exists".
                try:
                    semantic_verdict = verify_exported_apo_config(
                        installed_bytes.decode('utf-8'),
                        channels=channels,
                        global_preamp_db=global_preamp_db,
                        device=device,
                        imported_at_utc=created_at_utc,
                    )[0]
                except Exception as exc:  # error-boundary: importer
                    semantic_verdict = 'mismatch'
                    notes.append(f'semantic verification failed: {exc}')
        payload: dict[str, Any] = {
            'document_id': document_id,
            'target_path_repr': str(target),
            'rendered_sha256': rendered_sha,
            'installed_sha256': installed_sha,
            'semantic_verdict': semantic_verdict,
            'verification': verification,
            'evidence_strength': (
                'file_verified'
                if verification == 'content_sha_matched' else 'none'
            ),
            'created_at_utc': created_at_utc,
            'notes': tuple(notes),
        }
        rid, digest = _seal_id(
            ApoInstallRecord, 'eap',
            'record_id', 'record_sha256', payload,
        )
        return ApoInstallRecord(
            record_id=rid, record_sha256=digest, **payload,
        )


# ---------------------------------------------------------------------------
# miniDSP per-model deployability registry — sealed, exact-model keyed.

class MinidspModelDeployEntry(BaseModel):
    """Deployability classification for one exact miniDSP model/variant.

    ``machine_deployable`` is claimable only while a documented machine
    interface is pinned in ``deployability_refs``; every current model is
    'assisted_only' — file export plus operator-driven import is the
    strongest honest lane.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    entry_id: str = Field(min_length=1)
    entry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: DSPTargetProfile.profile_id this entry classifies.
    profile_id: str = Field(min_length=1)
    device_model: str = Field(min_length=1)
    profile_variant: str | None = None
    deploy_class: MinidspDeployClass
    #: 'none' until a documented machine interface is pinned.
    machine_interface: str = 'none'
    #: Pinned reviewed documentation — same strings as the profile's
    #: ``source_refs``.
    deployability_refs: tuple[str, ...] = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_entry(self) -> 'MinidspModelDeployEntry':
        if (
            self.deploy_class == 'machine_deployable'
            and self.machine_interface == 'none'
        ):
            raise ValueError(
                'machine_deployable requires a pinned machine interface'
            )
        if self.entry_sha256 != _hash(self.identity_payload()):
            raise ValueError('MinidspModelDeployEntry hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'entry_id', 'entry_sha256'},
        )


def _build_minidsp_entry(
    profile: DSPTargetProfile,
    *,
    deploy_class: MinidspDeployClass,
    machine_interface: str = 'none',
    notes: tuple[str, ...] = (),
) -> MinidspModelDeployEntry:
    payload: dict[str, Any] = {
        'profile_id': profile.profile_id,
        'device_model': profile.device_model,
        'profile_variant': profile.profile_variant,
        'deploy_class': deploy_class,
        'machine_interface': machine_interface,
        'deployability_refs': tuple(profile.source_refs),
        'notes': notes,
    }
    rid, digest = _seal_id(
        MinidspModelDeployEntry, 'mmr',
        'entry_id', 'entry_sha256', payload,
    )
    return MinidspModelDeployEntry(
        entry_id=rid, entry_sha256=digest, **payload,
    )


def _build_minidsp_registry() -> dict[str, MinidspModelDeployEntry]:
    from .cad_deployment_target import list_dsp_target_profiles

    registry: dict[str, MinidspModelDeployEntry] = {}
    for profile in list_dsp_target_profiles():
        if profile.target_family != 'minidsp_biquad_export':
            continue
        # Honest classification: no documented machine write/read API is
        # pinned for any current miniDSP profile — Device Console import
        # is operator-driven, so the strongest lane is assisted_only.
        entry = _build_minidsp_entry(
            profile,
            deploy_class='assisted_only',
            notes=(
                'File handoff only — import via miniDSP Device Console; '
                'no documented machine control interface is pinned.',
            ),
        )
        registry[profile.profile_id] = entry
    return registry


MINIDSP_DEPLOYABILITY: dict[str, MinidspModelDeployEntry] = (
    _build_minidsp_registry()
)


def minidsp_deployability(profile_id: str) -> MinidspModelDeployEntry:
    """Exact-model deployability entry — unknown models fail closed."""
    try:
        return MINIDSP_DEPLOYABILITY[profile_id]
    except KeyError:
        # Re-raise as the pipeline vocabulary's unknown-model error; the
        # profile registry error for a bad id stays distinct.
        get_dsp_target_profile(profile_id)
        raise UnknownMinidspModelError(
            f'no deployability classification for profile {profile_id}'
        ) from None


def minidsp_deploy_class(profile_id: str) -> MinidspDeployClass:
    return minidsp_deployability(profile_id).deploy_class


# ---------------------------------------------------------------------------
# Strongest-path evaluator — callable by the #868 orchestrator.

class DeploymentPathCandidate(BaseModel):
    """One deployable path under evaluation.

    ``capability`` is the adapter-published manifest; ``reachability``
    reflects any pre-flight contact actually made ('unknown' stays
    conditional — never silently upgraded).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    capability: AdapterCapabilityReport
    binding_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$',
    )
    assisted_manifest_ref: AuthorityRef | None = None
    reachability: Literal['confirmed', 'unconfirmed', 'unknown'] = 'unknown'


class DeploymentPathVerdict(BaseModel):
    """Ranked verdict for one candidate path."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    evidence_strength: DeploymentEvidenceStrength
    #: True only for production-eligible control surfaces — documented or
    #: open-source protocols on real (non-simulated) adapters.
    production_eligible: bool
    availability: PathAvailability
    reasons: tuple[str, ...] = ()


def evaluate_deployment_path(
    candidate: DeploymentPathCandidate,
) -> DeploymentPathVerdict:
    """Classify one path on the evidence ladder — fail-closed."""
    cap = candidate.capability
    reasons: list[str] = []
    simulated = (
        cap.adapter_kind == 'simulated'
        or cap.protocol_authority == 'simulated'
    )
    production_protocol = (
        cap.protocol_authority in _PRODUCTION_PROTOCOL_AUTHORITIES
        and not simulated
    )
    if not production_protocol:
        reasons.append(
            f'protocol_authority={cap.protocol_authority} '
            f'(adapter_kind={cap.adapter_kind}) is not production '
            'authority'
        )
    machine_write = (
        cap.supports_apply and cap.deploy_mechanism == 'machine_write'
    )
    machine_readback = (
        cap.supports_read_back and cap.readback_mechanism == 'machine_exact'
    )
    if cap.supports_apply and not machine_write:
        reasons.append(
            'supports_apply declared without a machine_write mechanism'
        )
    if cap.supports_read_back and not machine_readback:
        reasons.append(
            'supports_read_back declared without a machine_exact mechanism'
        )
    file_verified = (
        cap.deploy_mechanism == 'file_export'
        and (
            cap.readback_mechanism == 'operator_captured_file'
            or FILE_IDENTITY_VERIFICATION_FEATURE in cap.supported_features
        )
    )
    if machine_write and machine_readback:
        strength: DeploymentEvidenceStrength = 'machine_readback'
    elif machine_write:
        strength = 'applied_ack'
    elif file_verified:
        strength = 'file_verified'
    elif candidate.assisted_manifest_ref is not None:
        strength = 'assisted_attestation'
        reasons.append('assisted lane — human attestation is the evidence')
    else:
        strength = 'none'
        reasons.append('no declared deployment capability — unavailable')
    if simulated:
        # Simulated/fake transports keep their rank for tests but are
        # never production-eligible on any lane, machine or file/assisted.
        production_eligible = False
    elif strength in ('machine_readback', 'applied_ack'):
        production_eligible = production_protocol
    else:
        production_eligible = production_protocol or strength in (
            'file_verified', 'assisted_attestation',
        )
    if strength == 'none':
        availability: PathAvailability = 'unavailable'
    elif candidate.reachability != 'confirmed':
        availability = 'conditional'
        reasons.append('reachability unconfirmed')
    else:
        availability = 'available'
    return DeploymentPathVerdict(
        candidate_id=candidate.candidate_id,
        adapter_id=candidate.adapter_id,
        evidence_strength=strength,
        production_eligible=production_eligible,
        availability=availability,
        reasons=tuple(reasons),
    )


def rank_deployment_paths(
    candidates: tuple[DeploymentPathCandidate, ...],
) -> tuple[DeploymentPathVerdict, ...]:
    """Strongest-first ordering; unavailable paths sink to the bottom."""
    verdicts = [evaluate_deployment_path(c) for c in candidates]
    rank = {
        'available': 0, 'conditional': 1, 'unavailable': 2,
    }
    verdicts.sort(
        key=lambda v: (
            -_DEPLOYMENT_STRENGTH_RANK[v.evidence_strength],
            rank[v.availability],
            not v.production_eligible,
            v.candidate_id,
        ),
    )
    return tuple(verdicts)


def select_strongest_deployment_path(
    candidates: tuple[DeploymentPathCandidate, ...],
) -> DeploymentPathVerdict | None:
    """Strongest path overall — None (fail closed) when none exist."""
    ranked = rank_deployment_paths(candidates)
    for verdict in ranked:
        if verdict.availability != 'unavailable':
            return verdict
    return None


def select_strongest_production_path(
    candidates: tuple[DeploymentPathCandidate, ...],
) -> DeploymentPathVerdict | None:
    """Strongest production-eligible path — None when none exist."""
    ranked = rank_deployment_paths(candidates)
    for verdict in ranked:
        if verdict.availability != 'unavailable' and verdict.production_eligible:
            return verdict
    return None


# ---------------------------------------------------------------------------
# Pipeline service — drives one adapter through the staged transaction.

class DeploymentPipelineService:
    """Staged deployment transaction against one bound adapter.

    Stage order is enforced: compile needs a baseline attempt, preview
    needs compile, apply needs a fresh unconsumed 'apply'-scoped
    authorization pinning the exact materialization sha, read-back needs
    apply, rollback needs a 'rollback'-scoped authorization. Records are
    persisted through ``repository`` when given.
    """

    def __init__(
        self,
        adapter: CalibrationDeviceAdapter,
        binding: AdapterDeviceBinding,
        *,
        target_ref: str,
        document_id: str,
        repository: Any | None = None,
        capability: AdapterCapabilityReport | None = None,
    ) -> None:
        self._adapter = adapter
        self._binding = binding
        self._target_ref = target_ref
        self._document_id = document_id
        self._repository = repository
        self._capability = capability or adapter.capability()
        self._pipeline_id = f'dpl-{uuid4().hex[:24]}'
        self._records: list[DeploymentPipelineRecord] = []
        self._materialization: MaterializedCalibrationSettings | None = None
        self._export: CadCalibrationExportSnapshot | None = None
        self._baseline_payload: Any | None = None
        self._baseline_sha256: str | None = None

    @property
    def pipeline_id(self) -> str:
        return self._pipeline_id

    @property
    def records(self) -> tuple[DeploymentPipelineRecord, ...]:
        return tuple(self._records)

    @property
    def latest(self) -> DeploymentPipelineRecord | None:
        return self._records[-1] if self._records else None

    # -- helpers -----------------------------------------------------

    def _emit(self, stage: PipelineStage, at: str, **updates: Any):
        prev = self.latest
        base: dict[str, Any] = {}
        if prev is not None:
            base = {
                'baseline_source': prev.baseline_source,
                'baseline_sha256': prev.baseline_sha256,
                'materialization_sha256': prev.materialization_sha256,
                'preview_entries': prev.preview_entries,
                'authorization_id': prev.authorization_id,
                'ack_id': prev.ack_id,
                'applied_units': prev.applied_units,
                'total_units': prev.total_units,
                'partial_write': prev.partial_write,
                'readback_mechanism': prev.readback_mechanism,
                'readback_sha256': prev.readback_sha256,
                'readback_verdict': prev.readback_verdict,
                'rollback_outcome': prev.rollback_outcome,
                'assisted_attestation_id': prev.assisted_attestation_id,
            }
        base.update(updates)
        record = build_pipeline_record(
            pipeline_id=self._pipeline_id,
            document_id=self._document_id,
            binding_sha256=self._binding.binding_sha256,
            adapter_id=self._capability.adapter_id,
            target_ref=self._target_ref,
            stage=stage,
            created_at_utc=at,
            **base,
        )
        self._records.append(record)
        if self._repository is not None:
            self._repository.save_pipeline_record(record)
        return record

    def _require_stage(self, *allowed: PipelineStage) -> None:
        current = derive_pipeline_stage(self.records)
        if current not in allowed:
            raise PipelineStageError(
                f'pipeline stage {current} cannot advance; expected '
                f'one of {allowed}'
            )

    def _assert_authorization(
        self,
        authorization: DeploymentOperatorAuthorization,
        *,
        scope: AuthorizationScope,
        at: str,
    ) -> None:
        if authorization.pipeline_id != self._pipeline_id:
            raise PipelineAuthorizationError(
                'authorization belongs to a different pipeline'
            )
        if authorization.scope != scope:
            raise PipelineAuthorizationError(
                f'authorization scope {authorization.scope} != {scope}'
            )
        if authorization.consumed:
            raise PipelineAuthorizationError('authorization consumed')
        if authorization.expires_at_utc is not None \
                and authorization.expires_at_utc < at:
            raise PipelineAuthorizationError('authorization expired')

    # -- stages ------------------------------------------------------

    def open(self, *, at: str) -> DeploymentPipelineRecord:
        """Open the run — baseline attempt first, before any mutation."""
        self._require_stage('opened')
        capture = getattr(self._adapter, 'capture_baseline', None)
        baseline_source: BaselineSource = 'unavailable'
        baseline_sha: str | None = None
        if callable(capture):
            try:
                payload, sha = capture(self._binding)
                self._baseline_payload = payload
                baseline_sha = sha
                baseline_source = 'machine_capture'
            except Exception:  # error-boundary: baseline probe — any machine-capture failure degrades to an honest 'unavailable' baseline, never a forged one (noqa: BLE001)
                baseline_source = 'unavailable'
        elif self._capability.supports_read_back:
            try:
                observed = self._adapter.read_back(
                    self._binding, observed_at_utc=at,
                )
                self._baseline_payload = {
                    'channels': [
                        c.model_dump(mode='json') for c in observed
                    ],
                }
                baseline_sha = _hash(self._baseline_payload)
                baseline_source = 'machine_readback'
            except Exception:  # error-boundary: baseline probe — any readback failure degrades to an honest 'unavailable' baseline, never a forged one (noqa: BLE001)
                baseline_source = 'unavailable'
        self._baseline_sha256 = baseline_sha
        return self._emit(
            'baseline_captured' if baseline_sha else 'baseline_unavailable',
            at,
            baseline_source=baseline_source,
            baseline_sha256=baseline_sha,
        )

    def compile(
        self,
        export: CadCalibrationExportSnapshot,
        *,
        at: str,
    ) -> MaterializedCalibrationSettings:
        """Compile the pinned export into the device-native payload."""
        self._require_stage('baseline_captured', 'baseline_unavailable')
        materialization = self._adapter.materialize(
            export, self._binding, created_at_utc=at,
        )
        validate_materialization_result(
            self._capability, export, self._binding, materialization,
        )
        self._export = export
        self._materialization = materialization
        self._emit(
            'compiled', at,
            materialization_sha256=materialization.materialization_sha256,
        )
        return materialization

    def preview(self, *, at: str) -> tuple[SemanticDiffEntry, ...]:
        """Semantic diff preview — what the apply will change."""
        self._require_stage('compiled')
        assert self._export is not None
        entries: list[SemanticDiffEntry] = []
        baseline_channels = self._baseline_channel_map()
        for channel in self._export.channels:
            before = baseline_channels.get(channel.channel_id)
            for field_name in ('gain_db', 'delay_s', 'polarity'):
                after_repr = repr(getattr(channel, field_name))
                before_val = (
                    before.get(field_name) if before is not None else None
                )
                if before_val is None:
                    entries.append(SemanticDiffEntry(
                        channel_id=channel.channel_id,
                        field_name=field_name,
                        before_repr='unobserved',
                        after_repr=after_repr,
                        kind='unobserved_before',
                    ))
                elif repr(before_val) != after_repr:
                    entries.append(SemanticDiffEntry(
                        channel_id=channel.channel_id,
                        field_name=field_name,
                        before_repr=repr(before_val),
                        after_repr=after_repr,
                    ))
        result = tuple(entries)
        self._emit('previewed', at, preview_entries=result)
        return result

    def _baseline_channel_map(self) -> dict[str, dict[str, Any]]:
        """Observed per-channel baseline values, any capture shape."""
        payload = self._baseline_payload
        result: dict[str, dict[str, Any]] = {}
        if not isinstance(payload, dict):
            return result
        for channel in payload.get('channels', ()):
            result[channel['channel_id']] = dict(channel)
        for channel_id, gain in payload.get('avr_cv_trims', {}).items():
            result[channel_id] = {'gain_db': gain}
        return result

    def authorize(
        self,
        *,
        operator_id: str,
        scope: AuthorizationScope = 'apply',
        at: str,
        expires_at_utc: str | None = None,
    ) -> DeploymentOperatorAuthorization:
        """Mint a scoped one-shot authorization immediately before mutation."""
        if scope == 'apply':
            self._require_stage('previewed')
            assert self._materialization is not None
            pin = self._materialization.materialization_sha256
        else:
            self._require_stage(
                'applied', 'partial_write', 'readback_matched',
                'readback_diverged',
            )
            pin = None
        authorization = build_operator_authorization(
            document_id=self._document_id,
            pipeline_id=self._pipeline_id,
            scope=scope,
            operator_id=operator_id,
            authorized_at_utc=at,
            candidate_materialization_sha256=pin,
            expires_at_utc=expires_at_utc,
        )
        if self._repository is not None:
            self._repository.save_operator_authorization(authorization)
        if scope == 'apply':
            self._emit(
                'authorized', at,
                authorization_id=authorization.authorization_id,
            )
        return authorization

    def apply(
        self,
        authorization: DeploymentOperatorAuthorization,
        *,
        at: str,
    ) -> DeviceApplyAck:
        """Apply — consumes the authorization, records partial writes."""
        self._assert_authorization(authorization, scope='apply', at=at)
        assert self._materialization is not None
        if (
            authorization.candidate_materialization_sha256
            != self._materialization.materialization_sha256
        ):
            raise PipelineAuthorizationError(
                'authorization pins a different materialization'
            )
        self._require_stage('authorized')
        partial: PartialWriteState = 'none'
        ack: DeviceApplyAck | None = None
        try:
            ack = self._adapter.apply(
                self._materialization,
                self._binding,
                operator_confirmed=True,
                applied_at_utc=at,
            )
        except Exception as exc:  # error-boundary: apply boundary — any apply failure emits a failed/partial record with applied/total units preserved and re-raises; the identity is never masked (noqa: BLE001)
            applied = getattr(exc, 'applied_units', None)
            total = getattr(exc, 'total_units', None)
            partial = 'partial' if applied else 'failed'
            record = self._emit(
                'partial_write' if partial == 'partial' else 'failed',
                at,
                applied_units=applied,
                total_units=total,
                partial_write=partial,
                notes=(f'apply raised: {exc}',),
            )
            self._save_consumed(authorization, record.record_id)
            raise
        applied_units = ack.applied_units
        total_units = ack.total_units
        if total_units is not None and applied_units is not None:
            if applied_units < total_units:
                partial = 'partial'
            elif applied_units == total_units:
                partial = 'complete'
        record = self._emit(
            'partial_write' if partial == 'partial' else 'applied',
            at,
            authorization_id=authorization.authorization_id,
            ack_id=ack.ack_id,
            applied_units=applied_units,
            total_units=total_units,
            partial_write=partial,
        )
        self._save_consumed(authorization, record.record_id)
        return ack

    def _save_consumed(
        self,
        authorization: DeploymentOperatorAuthorization,
        record_id: str,
    ) -> None:
        if self._repository is None:
            return
        try:
            self._repository.save_operator_authorization(
                consume_authorization(authorization, record_id),
            )
        except Exception:  # error-boundary: best-effort bookkeeping — a consumption-persistence failure logs and leaves the authorization unconsumed (conservative direction); the apply record is already emitted (noqa: BLE001)
            _LOGGER.warning(
                'authorization-consumption persistence failed', exc_info=True
            )  # consumption persistence is best-effort bookkeeping

    def verify_readback(self, *, at: str) -> EffectiveAppliedSettingsSnapshot:
        """Read back the device state and diff against the pinned export."""
        self._require_stage('applied', 'partial_write')
        assert self._export is not None
        if not self._capability.supports_read_back:
            raise AdapterCapabilityError(
                f'adapter {self._capability.adapter_id} does not support '
                'read-back'
            )
        observed = self._adapter.read_back(
            self._binding, observed_at_utc=at,
        )
        snapshot = build_observation(
            document_id=self._document_id,
            binding=self._binding,
            export=self._export,
            observed_channels=observed,
            observed_at_utc=at,
            source='read_back',
        )
        verdict: ReadbackVerdict = (
            'matched' if not snapshot.deviations else 'diverged'
        )
        self._emit(
            'readback_matched' if verdict == 'matched' else 'readback_diverged',
            at,
            readback_mechanism=self._capability.readback_mechanism,
            readback_sha256=snapshot.snapshot_sha256,
            readback_verdict=verdict,
        )
        return snapshot

    def rollback(
        self,
        authorization: DeploymentOperatorAuthorization,
        *,
        at: str,
    ) -> DeploymentPipelineRecord:
        """Verified rollback to the pinned baseline — never assumed."""
        self._require_stage(
            'applied', 'partial_write', 'readback_matched',
            'readback_diverged',
        )
        self._assert_authorization(authorization, scope='rollback', at=at)
        outcome: PipelineRollbackOutcome = 'unavailable'
        rollback_previous = getattr(self._adapter, 'rollback_previous', None)
        if self._baseline_sha256 is None:
            outcome = 'unavailable'
        elif callable(rollback_previous):
            try:
                evidence = rollback_previous(self._binding)
                outcome = (
                    'verified'
                    if getattr(evidence, 'outcome', '') == 'restored_verified'
                    else 'failed'
                )
            except Exception:  # error-boundary: rollback probe — an evidence-inspection failure records the outcome as 'failed' honestly (noqa: BLE001)
                outcome = 'failed'
        else:
            outcome = 'unavailable'
        record = self._emit(
            'rollback_verified' if outcome == 'verified' else 'rollback_failed',
            at,
            rollback_outcome=outcome,
        )
        self._save_consumed(authorization, record.record_id)
        return record

    def abort(self, *, at: str, reason: str) -> DeploymentPipelineRecord:
        # Terminal stages are never masked — an abort after a verified
        # rollback or a failed run would rewrite the derived end-state.
        self._require_stage(
            'opened', 'baseline_captured', 'baseline_unavailable',
            'compiled', 'previewed', 'authorized', 'applied',
            'partial_write', 'readback_matched', 'readback_diverged',
        )
        return self._emit('aborted', at, notes=(reason,))


# ---------------------------------------------------------------------------
# JA labels

DEPLOYMENT_PIPELINE_LABELS: dict[str, str] = {
    # stages
    'opened': '開始',
    'baseline_captured': 'ベースライン取得済み',
    'baseline_unavailable': 'ベースライン取得不可',
    'compiled': 'コンパイル済み',
    'previewed': 'プレビュー済み',
    'authorized': '認可済み',
    'applied': '適用済み',
    'partial_write': '部分書き込み',
    'readback_matched': '読み戻し一致',
    'readback_diverged': '読み戻し不一致',
    'rollback_verified': 'ロールバック検証済み',
    'rollback_failed': 'ロールバック失敗',
    'failed': '失敗',
    'aborted': '中止',
    # evidence strengths
    'machine_readback': '機器読み戻し',
    'applied_ack': '適用ACK',
    'file_verified': 'ファイル検証済み',
    'assisted_attestation': '支援付き証明',
    'unverified': '未検証',
    'none': '証跡なし',
    # readback verdicts
    'matched': '一致',
    'diverged': '不一致',
    'not_attempted': '未実施',
    'unavailable': '利用不可',
    # partial write
    'complete': '完了',
    'partial': '部分',
    # rollback
    'verified': '検証済み',
    # availability
    'available': '利用可能',
    'conditional': '条件付き',
    # deploy classes
    'machine_deployable': '機器適用可能',
    'assisted_only': '支援付きのみ',
    # assisted outcomes
    'attested': '証明済み',
    'rejected': '却下',
    # apo verification
    'content_sha_matched': '内容ハッシュ一致',
    'content_sha_mismatch': '内容ハッシュ不一致',
    'install_failed': 'インストール失敗',
    'not_verified': '未検証',
}


__all__ = [
    'ApoInstallRecord',
    'ApoVerification',
    'AssistedDeploymentAttestation',
    'AssistedInstructionManifest',
    'AssistedOutcome',
    'AssistedStepAction',
    'AssistedStepManifest',
    'AuthorizationScope',
    'BaselineSource',
    'DeploymentEvidenceStrength',
    'DeploymentOperatorAuthorization',
    'DeploymentPathCandidate',
    'DeploymentPathVerdict',
    'DeploymentPipelineRecord',
    'DeploymentPipelineService',
    'DEPLOYMENT_PIPELINE_LABELS',
    'EqualizerApoInstaller',
    'FILE_IDENTITY_VERIFICATION_FEATURE',
    'MinidspDeployClass',
    'MinidspModelDeployEntry',
    'MINIDSP_DEPLOYABILITY',
    'PartialWriteState',
    'PathAvailability',
    'PipelineAuthorizationError',
    'PipelineError',
    'PipelineRollbackOutcome',
    'PipelineStage',
    'PipelineStageError',
    'ReadbackVerdict',
    'SemanticDiffEntry',
    'UnknownMinidspModelError',
    'build_assisted_attestation',
    'build_assisted_manifest',
    'build_operator_authorization',
    'build_pipeline_record',
    'consume_authorization',
    'derive_pipeline_evidence_strength',
    'derive_pipeline_stage',
    'evaluate_deployment_path',
    'minidsp_deploy_class',
    'minidsp_deployability',
    'rank_deployment_paths',
    'select_strongest_deployment_path',
    'select_strongest_production_path',
]
