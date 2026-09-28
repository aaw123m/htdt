"""Immersive-audio renderer output topology authority (#1022).

Three layers stay separate:

- **Physical installed speaker** — Scene / InstalledEquipment authority
  (exact XYZ, wiring, equipment definition).
- **Logical renderer role** — format-specific roles (``Top Front L``,
  ``Surround Height L``, ``Top Surround``, ``Dolby Enabled Front L`` …);
  never normalized into a generic ``height_left``.
- **Renderer/output binding** — for one exact processor + firmware +
  speaker-layout setting + decoder/upmixer + operating preset, which
  logical role renders to which physical output and installed speaker.

An installed speaker is not automatically active: every installed speaker
referenced by a topology carries an explicit activity state, and an absent
logical role is either covered by a documented substitution/virtualization
or reported UNKNOWN — never silently relabeled or fabricated.

- :class:`RendererOutputTopology` — the immutable configured topology.
- :class:`LogicalRoleBinding` — one role → output/speaker binding.
- :class:`SpeakerActivity` — activity state of an installed speaker.
- :func:`evaluate_renderer_topology` — commissioning checks.
- :func:`active_source_entities` — the physical sources prediction /
  excitation may drive under this topology.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload




RendererFamily = Literal[
    'pcm_native_channels',
    'dolby_atmos',
    'dolby_surround_upmixer',
    'dts_x',
    'dts_neural_x',
    'auro_3d',
    'auro_matic',
    'user_manual_direct',
    'vendor_opaque',
    'unknown',
]
"""Renderer/decoder family. No family implies proprietary decoding is
implemented — this authority stores the configured routing, not the DSP."""

TopologyEvidence = Literal[
    'hardware_readback_verified',
    'user_confirmed_device_state',
    'documented_supported',
    'unknown',
]
"""Evidence tier for the *configured* topology, ordered strongest first."""

SpeakerActivityState = Literal[
    'active',
    'inactive',
    'virtualized',
    'substituted',
    'unsupported',
    'unknown',
]


class LogicalRoleBinding(BaseModel):
    """One logical renderer role bound to concrete outputs.

    ``requested_role_id`` is the role the renderer wants;
    ``configured_role_id`` differs only under a documented substitution
    (e.g. Rear Height serving as Auro Surround Height) — the physical
    speaker is never silently relabeled.
    """

    model_config = ConfigDict(frozen=True)

    requested_role_id: str = Field(min_length=1)
    configured_role_id: str = Field(min_length=1)
    output_port_id: str | None = None
    installed_equipment_id: str | None = None
    activity: SpeakerActivityState = 'unknown'
    substitution_rule: str | None = None
    virtualization_mode: str | None = None
    virtualization_source_speakers: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'LogicalRoleBinding':
        if self.requested_role_id != self.configured_role_id:
            if self.substitution_rule is None:
                raise ValueError(
                    'a configured role differing from the requested role '
                    'requires an explicit substitution_rule'
                )
            if self.activity not in ('substituted', 'unknown'):
                raise ValueError(
                    'a substituted binding must declare activity '
                    "'substituted' or 'unknown'"
                )
        if self.activity == 'virtualized':
            if self.installed_equipment_id is not None:
                raise ValueError(
                    'a virtualized role must not fabricate an installed '
                    'speaker binding'
                )
            if self.virtualization_mode is None:
                raise ValueError(
                    'a virtualized role requires an explicit '
                    'virtualization_mode'
                )
        if self.activity in ('active', 'substituted'):
            if self.installed_equipment_id is None:
                raise ValueError(
                    'an active/substituted role must bind an installed '
                    'speaker entity'
                )
        return self


class SpeakerActivity(BaseModel):
    """Explicit activity state of one installed speaker under this
    topology — the twin never assumes every installed speaker is used."""

    model_config = ConfigDict(frozen=True)

    installed_equipment_id: str = Field(min_length=1)
    activity: SpeakerActivityState
    via_role_id: str | None = None


class RendererOutputTopology(BaseModel):
    """The configured renderer → output → speaker topology for one exact
    processor state — immutable; changing firmware/setup creates a new
    topology."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['renderer-output-topology-1'] = (
        'renderer-output-topology-1'
    )
    topology_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    processor_equipment_id: str = Field(min_length=1)
    firmware_version: str | None = None
    operating_preset_id: str | None = None
    renderer_family: RendererFamily = 'unknown'
    renderer_mode: str | None = None
    configured_layout_label: str | None = None
    amp_assign_profile_id: str | None = None
    role_bindings: tuple[LogicalRoleBinding, ...] = ()
    speaker_activities: tuple[SpeakerActivity, ...] = ()
    evidence_tier: TopologyEvidence = 'unknown'
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    topology_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(mode='python', exclude={'topology_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'RendererOutputTopology':
        requested = [b.requested_role_id for b in self.role_bindings]
        if len(set(requested)) != len(requested):
            raise ValueError(
                'each requested logical role may bind at most once'
            )
        bound_speakers = {
            b.installed_equipment_id
            for b in self.role_bindings
            if b.installed_equipment_id is not None
        }
        for activity in self.speaker_activities:
            if (
                activity.activity == 'active'
                and activity.installed_equipment_id not in bound_speakers
            ):
                raise ValueError(
                    'a speaker cannot be declared active without a role '
                    'binding'
                )
        if self.topology_sha256 != _hash(self.semantic_payload()):
            raise ValueError('renderer topology semantic hash mismatch')
        return self


def build_renderer_output_topology(
    *,
    topology_id: str,
    version: str,
    processor_equipment_id: str,
    firmware_version: str | None = None,
    operating_preset_id: str | None = None,
    renderer_family: RendererFamily = 'unknown',
    renderer_mode: str | None = None,
    configured_layout_label: str | None = None,
    amp_assign_profile_id: str | None = None,
    role_bindings: tuple[LogicalRoleBinding, ...] = (),
    speaker_activities: tuple[SpeakerActivity, ...] = (),
    evidence_tier: TopologyEvidence = 'unknown',
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> RendererOutputTopology:
    probe = RendererOutputTopology.model_construct(**canonicalize_payload(RendererOutputTopology, dict(
        topology_id=topology_id,
        version=version,
        processor_equipment_id=processor_equipment_id,
        firmware_version=firmware_version,
        operating_preset_id=operating_preset_id,
        renderer_family=renderer_family,
        renderer_mode=renderer_mode,
        configured_layout_label=configured_layout_label,
        amp_assign_profile_id=amp_assign_profile_id,
        role_bindings=tuple(role_bindings),
        speaker_activities=tuple(speaker_activities),
        evidence_tier=evidence_tier,
        provenance=tuple(provenance),
        topology_sha256='',
    )))
    return RendererOutputTopology(
        **probe.model_dump(mode='python', exclude={'topology_sha256'}),
        topology_sha256=_hash(probe.semantic_payload()),
    )


def active_source_entities(
    topology: RendererOutputTopology,
) -> tuple[str, ...]:
    """Installed source entities a prediction/excitation path may drive
    under this topology — active or substituted bindings only."""
    return tuple(
        sorted(
            {
                b.installed_equipment_id
                for b in topology.role_bindings
                if b.installed_equipment_id is not None
                and b.activity in ('active', 'substituted')
            }
        )
    )


class TopologyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    reason: str | None = None


class RendererTopologyEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    topology: RendererOutputTopology
    checks: tuple[TopologyCheckResult, ...]
    evaluation_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RendererTopologyEvaluation':
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('renderer topology evaluation hash mismatch')
        if self.evaluation_id != 'rte-' + digest[:24]:
            raise ValueError('renderer topology evaluation id mismatch')
        return self


def evaluate_renderer_topology(
    *,
    topology: RendererOutputTopology,
    installed_equipment_ids: tuple[str, ...] | set[str],
) -> RendererTopologyEvaluation:
    """Commissioning checks for one configured topology.

    ``installed_equipment_ids`` is the physical inventory: bindings must
    resolve to it, and installed speakers with no binding must be carried
    as explicit inactive/unknown activities rather than silently dropped
    or assumed driven.
    """
    installed = set(installed_equipment_ids)
    checks: list[TopologyCheckResult] = []

    unresolved = [
        b.requested_role_id
        for b in topology.role_bindings
        if b.installed_equipment_id is not None
        and b.installed_equipment_id not in installed
    ]
    checks.append(
        TopologyCheckResult(
            check='bindings_resolve',
            status='FAIL' if unresolved else 'PASS',
            reason=(
                'bindings reference uninstalled equipment: '
                + ', '.join(unresolved)
                if unresolved
                else 'every bound role resolves to installed equipment'
            ),
        )
    )

    bound = {
        b.installed_equipment_id
        for b in topology.role_bindings
        if b.installed_equipment_id is not None
    }
    declared = {a.installed_equipment_id for a in topology.speaker_activities}
    undeclared_installed = installed - bound - declared
    checks.append(
        TopologyCheckResult(
            check='installed_speakers_accounted',
            status='FAIL' if undeclared_installed else 'PASS',
            reason=(
                'installed speakers with no binding and no activity '
                'record: ' + ', '.join(sorted(undeclared_installed))
                if undeclared_installed
                else 'every installed speaker is bound or explicitly '
                'inactive/unknown'
            ),
        )
    )

    if topology.evidence_tier == 'unknown':
        checks.append(
            TopologyCheckResult(
                check='topology_evidence',
                status='UNKNOWN',
                reason='configured topology has no readback/user-confirmed '
                'or documented evidence',
            )
        )
    else:
        checks.append(
            TopologyCheckResult(
                check='topology_evidence',
                status='PASS',
                reason=f'topology evidence tier: {topology.evidence_tier}',
            )
        )

    virtualized = [
        b.requested_role_id
        for b in topology.role_bindings
        if b.activity == 'virtualized'
    ]
    checks.append(
        TopologyCheckResult(
            check='virtualization_documented',
            status='PASS' if virtualized else 'NOT_APPLICABLE',
            reason=(
                'virtualized roles with explicit modes: '
                + ', '.join(virtualized)
                if virtualized
                else 'no virtualized roles'
            ),
        )
    )

    probe = RendererTopologyEvaluation.model_construct(**canonicalize_payload(RendererTopologyEvaluation, dict(
        evaluation_id='',
        topology=topology,
        checks=tuple(checks),
        evaluation_sha256='',
    )))
    digest = _hash(probe.semantic_payload())
    return RendererTopologyEvaluation(
        **probe.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        ),
        evaluation_id='rte-' + digest[:24],
        evaluation_sha256=digest,
    )


def renderer_topology_status(
    evaluation: RendererTopologyEvaluation,
) -> EvaluationStatus:
    return _combine_status(tuple(c.status for c in evaluation.checks))
