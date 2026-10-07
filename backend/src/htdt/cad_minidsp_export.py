"""miniDSP biquad text-export adapter (#838 slice B).

``MiniDSPBiquadExportAdapter`` implements the
:class:`CalibrationDeviceAdapter` contract for miniDSP targets that
consume the documented per-channel biquad text file::

    biquad1,
    b0=0.999994731209322,
    b1=-1.999926191015139,
    b2=0.999932679693677,
    a1=1.999926191015139,
    a2=-0.999927410902999

The miniDSP denominator convention is ``1-a1*z^-1-a2*z^-2`` where HTDT
biquads carry ``1+a1*z^-1+a2*z^-2`` — coefficients are sign-inverted on
write, governed by the pinned target profile's
``biquad_coefficient_convention`` (anything unexpected fails closed).

Evidence boundaries, per the issue:

- an exact :class:`DSPTargetProfile` is required — there is no generic
  "miniDSP" target; Dirac and non-Dirac firmwares of the same unit are
  separate profiles;
- ``apply`` and machine ``read_back`` are declared unsupported — no
  public machine read-back/control API is claimed; the honest path is
  the file handoff plus :func:`record_operator_snapshot`, which lands
  at ``deployment_attested`` and can never reach
  ``deployment_verified``;
- the biquad file carries per-output PEQ only — delay, polarity, gain
  and crossovers are surfaced as ``unsupported_items`` so the #806
  deployment gate fails closed rather than silently dropping them.
"""

from __future__ import annotations

from typing import Any, Literal

from .cad_calibration import (
    CadCalibrationExportSnapshot,
    CadExportedChannelSettings,
)
from .cad_calibration_deployment import (
    CalibrationDeployment,
    DeploymentEffectivenessReport,
    DeploymentState,
)
from .cad_device_adapter import (
    AdapterCapabilityError,
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    DeviceApplyAck,
    MaterializedCalibrationSettings,
    _hash,
)
from .cad_deployment_target import (
    DSPTargetProfile,
    evaluate_export_target_fit,
)
from .canonical_json import canonical_json


MINIDSP_EXPORT_ADAPTER_ID = 'htdt-minidsp-biquad-export'
MINIDSP_EXPORT_ADAPTER_VERSION = '1'
MINIDSP_EXPORT_FORMAT_ID = 'minidsp-biquad-text-1'

#: Issue-vocabulary handoff stages for miniDSP-class targets. These map
#: onto #806 deployment states — a separate authority record is not
#: needed; the mapping keeps the UI honest about which evidence strength
#: was actually reached.
MinidspHandoffStage = Literal[
    'exported_for_minidsp',
    'operator_import_attested',
    'device_state_unknown',
    'post_measurement_verified',
]

MINIDSP_HANDOFF_LABELS: dict[str, str] = {
    'exported_for_minidsp': 'miniDSP 向けエクスポート済み（未検証）',
    'operator_import_attested': 'オペレータによる取り込み申告済み',
    'device_state_unknown': 'デバイス状態不明（機器読み戻しなし）',
    'post_measurement_verified': 'デプロイ後測定で検証済み',
}


def _render_channel_biquads(
    channel: CadExportedChannelSettings,
    profile: DSPTargetProfile,
) -> str:
    """One channel's PEQ as the documented miniDSP biquad file text."""
    invert = (
        profile.biquad_coefficient_convention
        == 'denominator=1-a1*z^-1-a2*z^-2'
    )
    lines: list[str] = []
    for index, biquad in enumerate(channel.peq, start=1):
        b0, b1, b2, a1, a2 = biquad.coefficients
        if invert:
            a1 = -a1
            a2 = -a2
        lines.extend(
            [
                f'biquad{index},',
                f'b0={b0!r},',
                f'b1={b1!r},',
                f'b2={b2!r},',
                f'a1={a1!r},',
                f'a2={a2!r}',
            ]
        )
    return '\n'.join(lines) + ('\n' if lines else '')


class MiniDSPBiquadExportAdapter:
    """File-export adapter bound to one exact miniDSP target profile."""

    def __init__(self, profile: DSPTargetProfile) -> None:
        if profile.target_family != 'minidsp_biquad_export':
            raise ValueError(
                'MiniDSPBiquadExportAdapter requires a '
                "'minidsp_biquad_export' target profile — there is no "
                'generic miniDSP target')
        self._profile = profile

    @property
    def profile(self) -> DSPTargetProfile:
        return self._profile

    def capability(self) -> AdapterCapabilityReport:
        return AdapterCapabilityReport(
            adapter_id=MINIDSP_EXPORT_ADAPTER_ID,
            adapter_version=MINIDSP_EXPORT_ADAPTER_VERSION,
            adapter_kind='offline_file',
            device_family='minidsp',
            supports_apply=False,
            supports_read_back=False,
            supports_materialization=True,
            notes=(
                f'Exact profile {self._profile.profile_id} '
                f'({self._profile.device_model}'
                + (
                    f'/{self._profile.profile_variant}'
                    if self._profile.profile_variant else ''
                )
                + f'): {self._profile.supported_sample_rates_hz[0]} Hz, '
                f'{self._profile.topology.peq_slots_per_output} PEQ per '
                'output channel.',
                'File handoff only — import via Device Console; no '
                'public machine read-back/control API is claimed.',
                'Operator attestation is the strongest evidence this '
                'target reaches; post-deployment measurement remains '
                'the final verification.',
            ),
        )

    def materialize(
        self,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        *,
        created_at_utc: str,
    ) -> MaterializedCalibrationSettings:
        profile = self._profile
        unsupported: list[str] = []
        _verdict, reasons = evaluate_export_target_fit(export, profile)
        unsupported.extend(reasons)

        routed = {entry[0] for entry in binding.routing}
        files: dict[str, str] = {}
        for channel in export.channels:
            if channel.channel_id not in routed:
                unsupported.append(
                    f'{channel.channel_id}: no routing on bound device')
                continue
            # The biquad text file carries PEQ coefficients only —
            # everything else the export claims is surfaced, not dropped.
            if channel.delay_s > 0.0:
                unsupported.append(
                    f'{channel.channel_id}: delay '
                    f'{channel.delay_s}s is not representable in the '
                    'miniDSP biquad file (device delay section is '
                    'separate)')
            if channel.polarity == 'inverted':
                unsupported.append(
                    f'{channel.channel_id}: polarity inversion is not '
                    'representable in the miniDSP biquad file')
            if abs(channel.gain_db) > 0.0:
                unsupported.append(
                    f'{channel.channel_id}: channel gain '
                    f'{channel.gain_db}dB is not representable in the '
                    'miniDSP biquad file')
            if channel.crossovers:
                unsupported.append(
                    f'{channel.channel_id}: crossovers are not part of '
                    'the miniDSP PEQ biquad file (device crossover '
                    'section is separate)')
            files[channel.channel_id] = _render_channel_biquads(
                channel, profile)

        payload_text = canonical_json(
            {
                'format_id': MINIDSP_EXPORT_FORMAT_ID,
                'adapter_id': MINIDSP_EXPORT_ADAPTER_ID,
                'adapter_version': MINIDSP_EXPORT_ADAPTER_VERSION,
                'target_profile_id': profile.profile_id,
                'target_profile_sha256': profile.profile_sha256,
                'binding': binding.semantic_payload(),
                'export_id': export.export_id,
                'exported_settings_semantic_sha256': (
                    export.exported_settings_semantic_sha256
                ),
                'sample_rate_hz': export.sample_rate_hz,
                'coefficient_convention': (
                    profile.biquad_coefficient_convention
                ),
                'files': files,
                'import_notes': [
                    'Import each file into the matching output '
                    "channel's PEQ block via Device Console.",
                    'Set the device/plugin sample rate to '
                    f'{export.sample_rate_hz} Hz before loading.',
                    'Verify device state by post-deployment '
                    'measurement — this export is not machine-verified.',
                ],
            }
        ) + '\n'
        materialization_id = 'mat:' + _hash(
            {
                'adapter_id': MINIDSP_EXPORT_ADAPTER_ID,
                'binding_sha256': binding.binding_sha256,
                'export_sha256': export.exported_settings_semantic_sha256,
                'target_profile_sha256': profile.profile_sha256,
            }
        )[:32]
        payload: dict[str, Any] = {
            'materialization_id': materialization_id,
            'created_at_utc': created_at_utc,
            'adapter_id': MINIDSP_EXPORT_ADAPTER_ID,
            'adapter_version': MINIDSP_EXPORT_ADAPTER_VERSION,
            'export_id': export.export_id,
            'exported_settings_semantic_sha256': (
                export.exported_settings_semantic_sha256
            ),
            'binding_id': binding.binding_id,
            'binding_sha256': binding.binding_sha256,
            'payload_text': payload_text,
            'channel_settings': export.channels,
            'quantization_applied': export.quantization_applied,
            'quantization_notes': export.quantization_notes,
            'unsupported_items': tuple(sorted(set(unsupported))),
        }
        provisional = MaterializedCalibrationSettings.model_construct(
            **payload, materialization_sha256='0' * 64
        )
        return MaterializedCalibrationSettings(
            **payload,
            materialization_sha256=_hash(provisional.semantic_payload()),
        )

    def apply(
        self,
        materialization: MaterializedCalibrationSettings,
        binding: AdapterDeviceBinding,
        *,
        operator_confirmed: bool,
        applied_at_utc: str,
    ) -> DeviceApplyAck:
        raise AdapterCapabilityError(
            f'{MINIDSP_EXPORT_ADAPTER_ID} cannot apply settings: the '
            'biquad file is a manual handoff imported via Device '
            'Console')

    def read_back(
        self,
        binding: AdapterDeviceBinding,
        *,
        observed_at_utc: str,
    ) -> tuple[CadExportedChannelSettings, ...]:
        raise AdapterCapabilityError(
            f'{MINIDSP_EXPORT_ADAPTER_ID} claims no machine read-back '
            'for this target; use record_operator_snapshot for '
            'operator attestation')


def derive_minidsp_handoff_stage(
    deployment: CalibrationDeployment | None,
    report: DeploymentEffectivenessReport | None = None,
) -> MinidspHandoffStage:
    """Issue-vocabulary stage for a miniDSP-class file handoff.

    Maps the #806 evidence ladder onto the issue's miniDSP stage names;
    ``post_measurement_verified`` requires a bound effectiveness report
    with a conclusive verdict — operator attestation alone stays at
    ``operator_import_attested`` forever.
    """
    if deployment is None:
        return 'device_state_unknown'
    if (
        report is not None
        and report.deployment_ref.ref_id == deployment.deployment_id
        and report.verdict
        in ('improvement_verified', 'no_measurable_change',
            'regression_observed')
    ):
        return 'post_measurement_verified'
    if deployment.deployment_state == 'deployment_attested':
        return 'operator_import_attested'
    if deployment.deployment_state == 'deployment_unverified':
        return 'exported_for_minidsp'
    # mismatch / blocked / verified-by-claim: device state cannot be
    # honestly asserted from file-export evidence.
    return 'device_state_unknown'


__all__ = [
    'MINIDSP_EXPORT_ADAPTER_ID',
    'MINIDSP_EXPORT_ADAPTER_VERSION',
    'MINIDSP_EXPORT_FORMAT_ID',
    'MINIDSP_HANDOFF_LABELS',
    'MiniDSPBiquadExportAdapter',
    'MinidspHandoffStage',
    'derive_minidsp_handoff_stage',
]
