"""#878 AVR/processor deployment adapter — documented Denon/Marantz-style
LAN telnet control (TCP port 23).

Only the vendor-documented command surface is claimed:

* ``CV<ch> <val>`` — per-channel trim, 38–62 in 0.5 dB steps, 50 = 0 dB
  (documented Denon AVR control protocol),
* ``CV<ch> ?`` — request command returning ``CV<ch> <val>`` — the machine
  read-back of the applied trim,
* ``MV`` / ``MU`` — master volume / mute status queries.

Everything the documented surface does not cover — per-channel distance/
delay, crossovers, PEQ/FIR, speaker size, bass management — stays
undeclared and fails closed into ``unsupported_items`` during
materialization (the community-observed HTTP AJAX endpoints are *not*
manufacturer-documented and are never production authority here).

Transports are injectable; a ``FakeAvrLanTransport`` simulates the
documented request/response surface for tests and is marked as such on
the capability report (``adapter_kind='simulated'``,
``protocol_authority='simulated'``).
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .cad_calibration import (
    CadCalibrationExportSnapshot,
    CadExportedChannelSettings,
)
from .cad_device_adapter import (
    AdapterCapabilityError,
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    DeviceApplyAck,
    MaterializedCalibrationSettings,
    _assert_binding,
)
from .canonical_json import canonical_sha256 as _hash


AVR_LAN_ADAPTER_ID = 'htdt-avr-lan'
AVR_LAN_ADAPTER_VERSION = '1'
AVR_LAN_DEVICE_FAMILY = 'avr-denon-marantz-telnet'

#: Documented channel codes on the Denon/Marantz telnet surface.
AVR_CHANNEL_CODES: dict[str, str] = {
    'fl': 'FL', 'front_l': 'FL', 'front-left': 'FL',
    'fr': 'FR', 'front_r': 'FR', 'front-right': 'FR',
    'c': 'C', 'center': 'C', 'centre': 'C',
    'sw': 'SW', 'sw1': 'SW', 'sub': 'SW', 'subwoofer': 'SW',
    'sw2': 'SW2', 'sub2': 'SW2', 'subwoofer2': 'SW2',
    'sw3': 'SW3', 'sw4': 'SW4',
    'sl': 'SL', 'surround_l': 'SL', 'surround-left': 'SL',
    'sr': 'SR', 'surround_r': 'SR', 'surround-right': 'SR',
    'sbl': 'SBL', 'surround_back_l': 'SBL',
    'sbr': 'SBR', 'surround_back_r': 'SBR',
    'sb': 'SB', 'surround_back': 'SB',
    'fhl': 'FHL', 'front_height_l': 'FHL',
    'fhr': 'FHR', 'front_height_r': 'FHR',
    'fwl': 'FWL', 'fwr': 'FWR',
    'tfl': 'TFL', 'tfr': 'TFR', 'tml': 'TML', 'tmr': 'TMR',
    'trl': 'TRL', 'trr': 'TRR', 'rhl': 'RHL', 'rhr': 'RHR',
    'fd': 'FD', 'sd': 'SD', 'bd': 'BD',
    'shl': 'SHL', 'shr': 'SHR', 'ts': 'TS', 'ch': 'CH',
}

#: Documented trim range — 38..62 in 0.5 dB steps, 50 = 0 dB.
AVR_CV_MIN, AVR_CV_MAX, AVR_CV_ZERO = 38, 62, 50

_LOOPBACK_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


class AvrLanTransport(Protocol):
    """Injectable transport seam — the only device-touching boundary."""

    def send(self, command: str) -> None: ...

    def query(self, command: str) -> tuple[str, ...]: ...


class AvrLanApplyError(AdapterCapabilityError):
    """Apply aborted mid-way — carries the partial-write counts."""

    def __init__(
        self,
        message: str,
        *,
        applied_units: int,
        total_units: int,
    ) -> None:
        super().__init__(message)
        self.applied_units = applied_units
        self.total_units = total_units


class AvrLanRollbackResult(BaseModel):
    """Outcome of a re-write-baseline rollback (in-memory evidence)."""

    model_config = ConfigDict(frozen=True)

    outcome: Literal[
        'restored_verified', 'restored_mismatch', 'failed', 'not_attempted',
    ]
    post_readback_sha256: str | None = None
    notes: tuple[str, ...] = ()


def _endpoint_host(endpoint: str) -> str:
    return endpoint.rsplit(':', 1)[0].strip('[]')


def _cv_command(code: str, value: int) -> str:
    return f'CV{code} {value}'


def _gain_to_cv(gain_db: float) -> int | None:
    raw = AVR_CV_ZERO + gain_db * 2.0
    if abs(raw - round(raw)) > 1e-9:
        return None
    value = int(round(raw))
    if not (AVR_CV_MIN <= value <= AVR_CV_MAX):
        return None
    return value


def _cv_to_gain(value: int) -> float:
    return (value - AVR_CV_ZERO) / 2.0


def _channel_code(channel_id: str) -> str | None:
    return AVR_CHANNEL_CODES.get(channel_id.strip().lower())


class AvrLanCalibrationAdapter:
    """Documented-LAN AVR adapter — per-channel trims only, fail closed.

    The device surface automates *applicable* settings: channel gain
    trims. Delay/crossover/PEQ/routing fields the documented protocol
    does not expose are surfaced as ``unsupported_items`` and block the
    deployment gate rather than being silently dropped.
    """

    def __init__(
        self,
        transports: Mapping[str, AvrLanTransport],
        *,
        approved_remote_endpoints: tuple[str, ...] = (),
        simulated: bool = False,
    ) -> None:
        self._transports = dict(transports)
        self._approved_remote = frozenset(approved_remote_endpoints)
        self._simulated = simulated
        self._baseline_trims: dict[str, float] | None = None

    def capability(self) -> AdapterCapabilityReport:
        return AdapterCapabilityReport(
            adapter_id=AVR_LAN_ADAPTER_ID,
            adapter_version=AVR_LAN_ADAPTER_VERSION,
            adapter_kind='simulated' if self._simulated else 'network_api',
            device_family=AVR_LAN_DEVICE_FAMILY,
            supports_apply=True,
            supports_read_back=True,
            supports_materialization=True,
            deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            rollback_mechanism='previous_config',
            runtime_observation='none',
            supported_features=('gain',),
            limit_notes=(
                'CV trim range is 38-62 (0.5 dB steps, 50 = 0 dB); '
                'values outside the documented range or off the 0.5 dB '
                'grid are unsupported.',
                'The documented telnet surface covers channel trims '
                'only — delay, crossovers, PEQ/FIR and routing are not '
                'claimable and stay assisted.',
            ),
            auth_requirements=(
                'operator_confirmation',
                'approved_remote_endpoint',
            ),
            applicability=(
                'Denon/Marantz documented AVR telnet control protocol '
                '(TCP port 23)'
            ),
            protocol_authority=(
                'simulated' if self._simulated else 'documented'
            ),
            notes=(
                'Deploy: CV<ch> <val> documented commands per channel '
                'trim.',
                'Read-back: CV<ch> ? request returning the stored trim '
                '(machine read-back, not file inference).',
                'Rollback: baseline CV re-write + post-rollback '
                'read-back comparison.',
                'Loopback endpoints by default; remote endpoints '
                'require explicit operator approval — no LAN discovery.',
                'Set ack is a device ack only — never acoustic '
                'verification.',
            ),
        )

    # -- transport plumbing ------------------------------------------

    def _transport(self, binding: AdapterDeviceBinding) -> AvrLanTransport:
        _assert_binding(AVR_LAN_ADAPTER_ID, binding)
        endpoint = binding.device_serial
        host = _endpoint_host(endpoint)
        transport = self._transports.get(endpoint)
        if transport is None:
            transport = self._transports.get(host)
        if transport is None:
            raise AdapterCapabilityError(
                f'no AVR transport registered for endpoint {endpoint}'
            )
        if (
            host not in _LOOPBACK_HOSTS
            and endpoint not in self._approved_remote
            and host not in self._approved_remote
        ):
            raise AdapterCapabilityError(
                f'remote AVR endpoint {endpoint} is not operator-approved'
            )
        return transport

    # -- contract -----------------------------------------------------

    def materialize(
        self,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        *,
        created_at_utc: str,
    ) -> MaterializedCalibrationSettings:
        _assert_binding(AVR_LAN_ADAPTER_ID, binding)
        routing = {channel_id: output for channel_id, output in binding.routing}
        commands: list[str] = []
        unsupported: list[str] = []
        landed: list[CadExportedChannelSettings] = []
        for channel in export.channels:
            code = _channel_code(channel.channel_id)
            if code is None:
                unsupported.append(
                    f'{channel.channel_id}: no documented AVR channel '
                    'code mapping'
                )
                continue
            if channel.channel_id not in routing:
                unsupported.append(
                    f'{channel.channel_id}: no routing on bound device'
                )
                continue
            if channel.delay_s != 0.0:
                unsupported.append(
                    f'{channel.channel_id}: delay is not on the '
                    'documented telnet surface'
                )
            if channel.polarity != 'normal':
                unsupported.append(
                    f'{channel.channel_id}: polarity is not on the '
                    'documented telnet surface'
                )
            if channel.crossovers:
                unsupported.append(
                    f'{channel.channel_id}: crossovers are not on the '
                    'documented telnet surface'
                )
            if channel.peq:
                unsupported.append(
                    f'{channel.channel_id}: PEQ/FIR is not on the '
                    'documented telnet surface'
                )
            value = _gain_to_cv(channel.gain_db)
            if value is None:
                unsupported.append(
                    f'{channel.channel_id}: gain {channel.gain_db} dB is '
                    'outside the documented CV range/grid'
                )
                continue
            commands.append(_cv_command(code, value))
            landed.append(channel.model_copy(update={
                'delay_s': 0.0,
                'polarity': 'normal',
                'crossovers': (),
                'peq': (),
            }))
        payload_text = '\r\n'.join(commands)
        payload: dict[str, Any] = {
            'schema_version': 1,
            'materialization_id': str(uuid4()),
            'created_at_utc': created_at_utc,
            'adapter_id': AVR_LAN_ADAPTER_ID,
            'adapter_version': AVR_LAN_ADAPTER_VERSION,
            'export_id': export.export_id,
            'exported_settings_semantic_sha256': (
                export.exported_settings_semantic_sha256
            ),
            'binding_id': binding.binding_id,
            'binding_sha256': binding.binding_sha256,
            'payload_text': payload_text,
            'channel_settings': tuple(landed),
            'quantization_applied': len(landed) != len(export.channels),
            'quantization_notes': (
                'CV trims quantize to 0.5 dB steps on 38-62 (50 = 0 dB).',
            ),
            'unsupported_items': tuple(unsupported),
        }
        provisional = MaterializedCalibrationSettings.model_construct(
            **payload, materialization_sha256='0' * 64,
        )
        return MaterializedCalibrationSettings(
            **payload,
            materialization_sha256=_hash(provisional.semantic_payload()),
        )

    def _commands(
        self, materialization: MaterializedCalibrationSettings,
    ) -> list[str]:
        return [
            line for line in materialization.payload_text.split('\r\n')
            if line.strip()
        ]

    def apply(
        self,
        materialization: MaterializedCalibrationSettings,
        binding: AdapterDeviceBinding,
        *,
        operator_confirmed: bool,
        applied_at_utc: str,
    ) -> DeviceApplyAck:
        _assert_binding(AVR_LAN_ADAPTER_ID, binding)
        if not operator_confirmed:
            raise AdapterCapabilityError(
                'AVR apply requires explicit operator confirmation'
            )
        transport = self._transport(binding)
        commands = self._commands(materialization)
        applied = 0
        try:
            for command in commands:
                transport.send(command)
                applied += 1
        except Exception as exc:
            raise AvrLanApplyError(
                f'AVR apply aborted after {applied}/{len(commands)} '
                f'commands: {exc}',
                applied_units=applied,
                total_units=len(commands),
            ) from exc
        return DeviceApplyAck(
            ack_id=str(uuid4()),
            materialization_id=materialization.materialization_id,
            acked_at_utc=applied_at_utc,
            device_note=(
                f'{applied}/{len(commands)} documented CV commands sent'
            ),
            applied_units=applied,
            total_units=len(commands),
        )

    def _read_trims(
        self,
        transport: AvrLanTransport,
        binding: AdapterDeviceBinding,
    ) -> dict[str, float]:
        trims: dict[str, float] = {}
        for channel_id, _output in binding.routing:
            code = _channel_code(channel_id)
            if code is None:
                raise AdapterCapabilityError(
                    f'{channel_id}: no documented AVR channel code'
                )
            try:
                responses = transport.query(f'CV{code} ?')
            except Exception as exc:
                raise AdapterCapabilityError(
                    f'AVR read-back of {code} failed: {exc}'
                ) from exc
            expected = f'CV{code}'
            value: int | None = None
            for response in responses:
                # Strict documented form: ``CV<ch> <int>`` — a prefix
                # collision ('CVFLX 50' is not 'CVFL 50'), a non-numeric
                # value or one outside the documented 38-62 range is a
                # malformed response, never an observed trim.
                parts = response.strip().upper().split()
                if len(parts) != 2 or parts[0] != expected:
                    continue
                try:
                    candidate = int(parts[1])
                except ValueError:
                    continue
                if not (AVR_CV_MIN <= candidate <= AVR_CV_MAX):
                    raise AdapterCapabilityError(
                        f'malformed CV response for {code}: '
                        f'{response!r} is outside the documented '
                        f'{AVR_CV_MIN}-{AVR_CV_MAX} range'
                    )
                value = candidate
                break
            if value is None:
                raise AdapterCapabilityError(
                    f'no documented CV response for {code} — '
                    'read-back failed closed'
                )
            trims[channel_id] = _cv_to_gain(value)
        return trims

    def read_back(
        self,
        binding: AdapterDeviceBinding,
        *,
        observed_at_utc: str,
    ) -> tuple[CadExportedChannelSettings, ...]:
        """Machine read-back of the applied trims via ``CV<ch> ?``."""
        transport = self._transport(binding)
        trims = self._read_trims(transport, binding)
        observed: list[CadExportedChannelSettings] = []
        for channel_id, output in binding.routing:
            observed.append(CadExportedChannelSettings(
                channel_id=channel_id,
                role_id='unobserved',
                source_entity_id='unobserved',
                physical_output_id=output,
                gain_db=trims[channel_id],
                delay_s=0.0,
                polarity='normal',
                crossovers=(),
                peq=(),
                routing=(output,),
            ))
        return tuple(observed)

    # -- baseline / rollback -------------------------------------------

    def capture_baseline(
        self, binding: AdapterDeviceBinding,
    ) -> tuple[dict[str, Any], str]:
        """Pre-mutation machine snapshot — pins observed trims."""
        transport = self._transport(binding)
        trims = self._read_trims(transport, binding)
        self._baseline_trims = dict(trims)
        payload = {'avr_cv_trims': trims}
        return payload, _hash(payload)

    def rollback_previous(
        self, binding: AdapterDeviceBinding,
    ) -> AvrLanRollbackResult:
        """Re-write the captured baseline trims and verify by read-back."""
        transport = self._transport(binding)
        baseline = self._baseline_trims
        if baseline is None:
            return AvrLanRollbackResult(
                outcome='not_attempted',
                notes=('no captured baseline on this adapter',),
            )
        try:
            for channel_id, gain_db in baseline.items():
                code = _channel_code(channel_id)
                value = _gain_to_cv(gain_db)
                if code is None or value is None:
                    return AvrLanRollbackResult(
                        outcome='failed',
                        notes=(
                            f'baseline trim for {channel_id} is not '
                            'representable on the documented surface',
                        ),
                    )
                transport.send(_cv_command(code, value))
            observed = self._read_trims(transport, binding)
        except Exception as exc:
            return AvrLanRollbackResult(
                outcome='failed', notes=(f'rollback failed: {exc}',),
            )
        post_sha = _hash({'avr_cv_trims': observed})
        if observed == baseline:
            return AvrLanRollbackResult(
                outcome='restored_verified',
                post_readback_sha256=post_sha,
            )
        return AvrLanRollbackResult(
            outcome='restored_mismatch',
            post_readback_sha256=post_sha,
            notes=('post-rollback read-back diverged from baseline',),
        )


class FakeAvrLanTransport:
    """Simulated documented-telnet surface for tests.

    Keeps the documented CV state in memory; ``send`` parses set
    commands, ``query`` answers ``CV<ch> ?`` with the documented
    ``CV<ch> <val>`` response form. Marked simulated through
    ``AvrLanCalibrationAdapter(simulated=True)`` so records never read
    it as production evidence.
    """

    def __init__(
        self,
        initial: Mapping[str, float] | None = None,
        *,
        fail_on_send_index: int | None = None,
    ) -> None:
        self.state: dict[str, int] = {
            code: AVR_CV_ZERO for code in set(AVR_CHANNEL_CODES.values())
        }
        for channel_id, gain in (initial or {}).items():
            code = _channel_code(channel_id)
            value = _gain_to_cv(gain)
            if code is not None and value is not None:
                self.state[code] = value
        self.fail_on_send_index = fail_on_send_index
        self.sent_commands: list[str] = []

    def send(self, command: str) -> None:
        index = len(self.sent_commands)
        if self.fail_on_send_index is not None and index == self.fail_on_send_index:
            raise OSError('fake transport: simulated send failure')
        self.sent_commands.append(command)
        parts = command.strip().split(' ', 1)
        head = parts[0]
        if head.startswith('CV') and len(parts) == 2:
            code = head[2:]
            self.state[code] = int(parts[1])

    def query(self, command: str) -> tuple[str, ...]:
        text = command.strip()
        if text.startswith('CV') and text.endswith('?'):
            code = text[2:-1].strip()
            value = self.state.get(code)
            if value is None:
                return ()
            return (f'CV{code} {value}',)
        return ()


__all__ = [
    'AVR_CHANNEL_CODES',
    'AVR_CV_MAX',
    'AVR_CV_MIN',
    'AVR_CV_ZERO',
    'AVR_LAN_ADAPTER_ID',
    'AVR_LAN_ADAPTER_VERSION',
    'AVR_LAN_DEVICE_FAMILY',
    'AvrLanApplyError',
    'AvrLanCalibrationAdapter',
    'AvrLanRollbackResult',
    'AvrLanTransport',
    'FakeAvrLanTransport',
]
