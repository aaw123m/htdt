"""CamillaDSP calibration deploy / read-back / rollback (#838 slice B).

``CamillaDSPCalibrationAdapter`` is the issue's reference machine-
verifiable deploy adapter: it implements the
:class:`CalibrationDeviceAdapter` contract (#609/#865) over the
documented CamillaDSP WebSocket JSON API via the injectable
:class:`CamillaDSPTransport` seam (#1072) — no websocket dependency is
added to the backend.

Evidence ladder, per the issue:

    designed → compiled_for_target → deploy_requested
    → deploy_acknowledged → config_readback_matched
    → runtime_observed → post_measurement_verified

Rules enforced here:

* ``materialize`` compiles the export into the device's own config,
  splicing an ``htdt_``-prefixed owned region (filters + pipeline steps)
  plus a machine-readable ``description`` binding. Device, mixer and
  processor sections are never touched; anything the export claims that
  cannot be represented becomes ``unsupported_items`` so the #806 gate
  fails closed.
* ``apply`` sends the compiled config through ``ValidateConfigJson``
  before ``SetConfigJson`` and requires explicit operator confirmation —
  the ack is never treated as deployed state.
* ``read_back`` fetches ``GetConfigJson`` and normalizes the htdt region
  back to ``CadExportedChannelSettings`` — the only path that can
  produce ``deployment_verified``.
* ``capture_baseline`` pins the pre-deploy config hash;
  ``rollback_previous`` restores it via ``GetPreviousConfig`` +
  read-back, emitting :class:`CamillaDSPRollbackEvidence`.
* ``observe_runtime`` seals CamillaDSP telemetry (state, clipping, RMS/
  peak, buffer/rate). Runtime telemetry is distinct evidence — it never
  upgrades a deployment to verified and never substitutes for
  post-deployment acoustic measurement.

Endpoint policy: ``binding.device_serial`` carries the operator-
configured endpoint URI ``camilladsp://host:port``. Loopback endpoints
are allowed by default; anything else requires the adapter to be
constructed with that exact endpoint in ``approved_remote_endpoints`` —
no silent LAN discovery.
"""

from __future__ import annotations

import copy
import json
from math import sqrt
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_calibration import (
    CadCalibrationExportSnapshot,
    CadCrossoverSetting,
    CadExportedChannelSettings,
    build_biquad_filter,
)
from .cad_calibration_deployment import _require_refs, _seal
from .cad_camilladsp import (
    CamillaDSPError,
    CamillaDSPTransport,
    _result_value,
)
from .cad_device_adapter import (
    AdapterCapabilityError,
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    DeviceApplyAck,
    MaterializedCalibrationSettings,
    _assert_binding,
    _hash,
)
from .canonical_json import canonical_json as _canonical


CAMILLADSP_DEPLOY_ADAPTER_ID = 'htdt-camilladsp-deploy'
CAMILLADSP_DEPLOY_ADAPTER_VERSION = '1'

#: Owned-region prefix inside the device config; anything under it is
#: managed by HTDT deploys and replaced atomically at compile time.
HTDT_REGION_PREFIX = 'htdt_'
HTDT_DESCRIPTION_KEY = 'htdt-deployment'

_LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}

#: Second-order crossovers compile to Butterworth-aligned biquads.
_BUTTERWORTH_Q = 1.0 / sqrt(2.0)

_BIQUAD_TYPE_TO_CAMILLA = {
    'peaking': 'Peaking',
    'low_pass': 'Lowpass',
    'high_pass': 'Highpass',
    'all_pass': 'Allpass',
}
_CAMILLA_TO_BIQUAD_TYPE = {
    'Peaking': 'peaking',
    'Lowpass': 'low_pass',
    'Highpass': 'high_pass',
    'Allpass': 'all_pass',
}

CamillaDSPDeploymentStage = Literal[
    'designed', 'compiled_for_target', 'deploy_requested',
    'deploy_acknowledged', 'config_readback_matched',
    'runtime_observed', 'post_measurement_verified',
]

CamillaDSPRollbackOutcome = Literal[
    'restored_verified', 'restored_previous_diverged',
    'restored_mismatch', 'failed', 'not_attempted',
]

CAMILLADSP_DEPLOY_LABELS: dict[str, str] = {
    'designed': '設計済み（エクスポート生成）',
    'compiled_for_target': 'ターゲット向けコンパイル済み',
    'deploy_requested': 'デプロイ要求済み',
    'deploy_acknowledged': '機器 ACK 済み（未検証）',
    'config_readback_matched': 'コンフィグ読み戻し一致',
    'runtime_observed': 'ランタイム観測済み（音響検証ではない）',
    'post_measurement_verified': 'デプロイ後測定で検証済み',
    'restored_verified': 'ロールバック復元確認（読み戻し一致）',
    'restored_previous_diverged': '直前コンフィグがベースラインと乖離',
    'restored_mismatch': 'ロールバック後読み戻し不一致',
    'failed': 'ロールバック失敗',
    'not_attempted': 'ロールバック未実施',
}


class CamillaDSPEndpointError(AdapterCapabilityError):
    """Endpoint policy violation — remote targets need approval."""


def _endpoint_host(endpoint: str) -> str:
    try:
        parsed = urlparse(endpoint)
    except ValueError as error:
        raise CamillaDSPEndpointError(
            f'endpoint {endpoint!r} is not a camilladsp:// URI'
        ) from error
    if parsed.scheme != 'camilladsp' or not parsed.hostname:
        raise CamillaDSPEndpointError(
            f'endpoint {endpoint!r} must be camilladsp://host:port')
    return parsed.hostname


def _slug(channel_id: str) -> str:
    """Filesystem-safe filter-name component for one channel id."""
    return ''.join(
        character if character.isalnum() or character in '_-'
        else '_'
        for character in channel_id
    )


def _parse_config_document(raw: Any) -> dict[str, Any]:
    """Parse a GetConfigJson/GetPreviousConfig document (JSON or YAML)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise CamillaDSPError(
            'malformed_response', f'config payload type {type(raw)!r}')
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - dep pinned
            raise CamillaDSPError(
                'missing_dependency', 'YAML configs require PyYAML'
            ) from error
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise CamillaDSPError('malformed_config', str(error))
    if not isinstance(parsed, dict):
        raise CamillaDSPError(
            'malformed_config', 'config document is not a mapping')
    return parsed


def config_sha256(config: dict[str, Any]) -> str:
    """Semantic hash of a device config — canonical JSON of the mapping."""
    return _hash(config)


# ----------------------------------------------------------------------
# compile: export → CamillaDSP config


def _peq_filter(biquad: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        'type': _BIQUAD_TYPE_TO_CAMILLA[biquad.filter_type],
        'freq': biquad.frequency_hz,
        'q': biquad.q,
    }
    if biquad.filter_type == 'peaking':
        parameters['gain'] = biquad.gain_db
    return {'type': 'Biquad', 'parameters': parameters}


def _crossover_filter(
    crossover: CadCrossoverSetting,
) -> dict[str, Any] | str:
    """Order-1 → FO types; order-2 → Butterworth-aligned biquad; higher
    orders return an unsupported reason — never silently approximated."""
    subtype_base = (
        'Highpass' if crossover.crossover_type == 'high_pass'
        else 'Lowpass'
    )
    if crossover.filter_order == 1:
        return {
            'type': 'Biquad',
            'parameters': {
                'type': f'{subtype_base}FO',
                'freq': crossover.frequency_hz,
            },
        }
    if crossover.filter_order == 2:
        return {
            'type': 'Biquad',
            'parameters': {
                'type': subtype_base,
                'freq': crossover.frequency_hz,
                'q': _BUTTERWORTH_Q,
            },
        }
    return (
        f'crossover order {crossover.filter_order} at '
        f'{crossover.frequency_hz}Hz exceeds the deployable biquad '
        'cascade (max order 2)'
    )


def _strip_htdt_region(
    config: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Remove every htdt_-owned filter and pipeline reference.

    Pipeline steps that mix htdt and foreign filter names keep their
    foreign names; a step owned entirely by htdt is dropped.
    """
    dropped = 0
    filters = config.get('filters')
    if isinstance(filters, dict):
        for name in [n for n in filters if n.startswith(HTDT_REGION_PREFIX)]:
            del filters[name]
            dropped += 1
    pipeline = config.get('pipeline')
    if isinstance(pipeline, list):
        kept: list[Any] = []
        for step in pipeline:
            if (
                isinstance(step, dict)
                and isinstance(step.get('names'), list)
            ):
                foreign = [
                    name for name in step['names']
                    if not (
                        isinstance(name, str)
                        and name.startswith(HTDT_REGION_PREFIX)
                    )
                ]
                dropped += len(step['names']) - len(foreign)
                if foreign:
                    kept.append({**step, 'names': foreign})
                continue
            kept.append(step)
        config['pipeline'] = kept
    return config, dropped


def compile_camilladsp_config(
    export: CadCalibrationExportSnapshot,
    binding: AdapterDeviceBinding,
    base_config: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Splice the export into a copy of the device's current config.

    Returns ``(candidate_config, notes, unsupported_items)``. The device,
    mixer and processor sections pass through untouched; the htdt-owned
    region (``htdt_`` filters, their Filter pipeline steps, and the
    ``description`` binding blob) is fully replaced.
    """
    if not isinstance(base_config, dict):
        raise AdapterCapabilityError(
            'device base config is not a JSON object')
    config, dropped = _strip_htdt_region(copy.deepcopy(base_config))
    notes: list[str] = []
    unsupported: list[str] = []
    if dropped:
        notes.append(f'removed {dropped} stale htdt-owned entries')

    devices = config.get('devices')
    if not isinstance(devices, dict):
        devices = {}
        config['devices'] = devices
    rate = devices.get('samplerate')
    if rate is None:
        devices['samplerate'] = export.sample_rate_hz
        notes.append('devices.samplerate spliced from export')
    elif rate != export.sample_rate_hz:
        unsupported.append(
            f'devices.samplerate:{rate} != '
            f'export:{export.sample_rate_hz}')

    playback = devices.get('playback')
    playback_channels = (
        playback.get('channels')
        if isinstance(playback, dict) else None
    )
    routing = {channel_id: output for channel_id, output in binding.routing}
    tag = export.exported_settings_semantic_sha256[:12]

    blob_channels: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    filters = config.get('filters')
    if not isinstance(filters, dict):
        filters = {}
        config['filters'] = filters
    prefix_owner: dict[str, str] = {}

    for channel in export.channels:
        output = routing.get(channel.channel_id)
        if output is None:
            unsupported.append(
                f'{channel.channel_id}: no routing on bound device')
            continue
        try:
            output_index = int(output)
        except (TypeError, ValueError):
            unsupported.append(
                f'{channel.channel_id}: routed output {output!r} is '
                'not a playback channel index')
            continue
        if (
            playback_channels is not None
            and not 0 <= output_index < playback_channels
        ):
            unsupported.append(
                f'{channel.channel_id}: output index {output_index} '
                f'outside declared playback channels '
                f'{playback_channels}')
            continue

        prefix = f'{HTDT_REGION_PREFIX}{tag}_{_slug(channel.channel_id)}'
        owner = prefix_owner.get(prefix)
        if owner is not None:
            # _slug is not injective: 'a.b' and 'a_b' share a filter
            # region. Merging them would silently overwrite one
            # channel's calibration with the other's — fail closed.
            unsupported.append(
                f'{channel.channel_id}: filter-name region collides '
                f'with channel {owner!r} after name normalization')
            continue
        prefix_owner[prefix] = channel.channel_id
        names: list[str] = []
        if channel.gain_db != 0.0 or channel.polarity == 'inverted':
            name = f'{prefix}_gain'
            filters[name] = {
                'type': 'Gain',
                'parameters': {
                    'gain': channel.gain_db,
                    'inverted': channel.polarity == 'inverted',
                },
            }
            names.append(name)
        if channel.delay_s > 0.0:
            name = f'{prefix}_delay'
            filters[name] = {
                'type': 'Delay',
                'parameters': {
                    'delay': channel.delay_s * 1000.0,
                    'unit': 'ms',
                },
            }
            names.append(name)
        for index, crossover in enumerate(channel.crossovers, start=1):
            rendered = _crossover_filter(crossover)
            if isinstance(rendered, str):
                unsupported.append(
                    f'{channel.channel_id}: {rendered}')
                continue
            name = f'{prefix}_xo{index}'
            filters[name] = rendered
            names.append(name)
        for index, biquad in enumerate(channel.peq, start=1):
            name = f'{prefix}_peq{index}'
            filters[name] = _peq_filter(biquad)
            names.append(name)
        if names:
            steps.append({
                'type': 'Filter',
                'channels': [output_index],
                'names': names,
            })
        blob_channels.append({
            'channel_id': channel.channel_id,
            'role_id': channel.role_id,
            'source_entity_id': channel.source_entity_id,
            'physical_output_id': channel.physical_output_id,
            'output': output,
            # Original filter ids: the device knows filters by the
            # htdt_-owned names, but read-back must reconstruct the
            # exact exported CadBiquadFilter (filter_id included) or
            # field-level diffs would report drift that is not real.
            'peq_filter_ids': [
                biquad.filter_id for biquad in channel.peq],
        })

    pipeline = config.get('pipeline')
    if not isinstance(pipeline, list):
        pipeline = []
    pipeline.extend(steps)
    config['pipeline'] = pipeline

    config['description'] = _canonical({
        HTDT_DESCRIPTION_KEY: {
            'export_id': export.export_id,
            'exported_settings_semantic_sha256': (
                export.exported_settings_semantic_sha256),
            'sample_rate_hz': export.sample_rate_hz,
            'channels': blob_channels,
        },
    })
    return config, tuple(notes), tuple(unsupported)


# ----------------------------------------------------------------------
# normalize: device config → exported channel settings


def _delay_to_seconds(parameters: dict[str, Any]) -> float | None:
    delay = parameters.get('delay')
    unit = parameters.get('unit', 'ms')
    if not isinstance(delay, (int, float)):
        return None
    if unit == 'ms':
        return float(delay) / 1000.0
    if unit == 'us':
        return float(delay) / 1_000_000.0
    if unit == 'mm':
        # millimetres of path difference at CamillaDSP's 343 m/s.
        return float(delay) / 343000.0
    if unit == 'samples':
        return None  # rate-dependent; resolved by the caller
    return None


def normalize_camilladsp_config(
    config: dict[str, Any],
) -> tuple[tuple[CadExportedChannelSettings, ...], dict[str, Any]] | None:
    """Reconstruct exported channel settings from a device config.

    Returns ``None`` when the config carries no htdt deployment region —
    a config not written by this adapter has no calibration-channel
    identity. Returns ``(channels, description_blob)`` otherwise.
    """
    description = config.get('description')
    blob: dict[str, Any] | None = None
    if isinstance(description, str):
        try:
            parsed = json.loads(description)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            candidate = parsed.get(HTDT_DESCRIPTION_KEY)
            if isinstance(candidate, dict):
                blob = candidate
    if blob is None:
        return None
    sample_rate = blob.get('sample_rate_hz')
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        return None
    tag = str(blob.get('exported_settings_semantic_sha256', ''))[:12]
    if not tag:
        return None

    filters = config.get('filters')
    if not isinstance(filters, dict):
        filters = {}
    pipeline = config.get('pipeline')
    if not isinstance(pipeline, list):
        pipeline = []

    # output index each htdt filter group is bound to, from pipeline
    step_outputs: dict[str, list[str]] = {}
    for step in pipeline:
        if not isinstance(step, dict) or step.get('type') != 'Filter':
            continue
        names = step.get('names')
        channels = step.get('channels')
        if not isinstance(names, list) or not isinstance(channels, list):
            continue
        for name in names:
            if isinstance(name, str) and name.startswith(HTDT_REGION_PREFIX):
                step_outputs[name] = [str(index) for index in channels]

    channels: list[CadExportedChannelSettings] = []
    for entry in blob.get('channels') or []:
        if not isinstance(entry, dict):
            continue
        channel_id = entry.get('channel_id')
        if not isinstance(channel_id, str) or not channel_id:
            continue
        prefix = f'{HTDT_REGION_PREFIX}{tag}_{_slug(channel_id)}'
        gain_db = 0.0
        polarity: Literal['normal', 'inverted'] = 'normal'
        delay_s = 0.0
        crossovers: list[CadCrossoverSetting] = []
        peq: list[Any] = []
        observed_outputs: list[str] = []
        owned = {
            name: spec
            for name, spec in filters.items()
            if name.startswith(prefix)
        }
        for name in sorted(owned):
            spec = owned[name]
            if not isinstance(spec, dict):
                continue
            parameters = spec.get('parameters')
            if not isinstance(parameters, dict):
                parameters = {}
            observed_outputs.extend(step_outputs.get(name, []))
            suffix = name[len(prefix):]
            if suffix == '_gain' and spec.get('type') == 'Gain':
                value = parameters.get('gain')
                if isinstance(value, (int, float)):
                    gain_db = float(value)
                if parameters.get('inverted') is True:
                    polarity = 'inverted'
            elif suffix == '_delay' and spec.get('type') == 'Delay':
                seconds = _delay_to_seconds(parameters)
                if seconds is None and parameters.get('unit') == 'samples':
                    count = parameters.get('delay')
                    if isinstance(count, (int, float)):
                        seconds = float(count) / sample_rate
                if seconds is not None:
                    delay_s = seconds
            elif suffix.startswith('_xo') and spec.get('type') == 'Biquad':
                subtype = parameters.get('type')
                frequency = parameters.get('freq')
                if subtype in ('HighpassFO', 'LowpassFO') and isinstance(
                        frequency, (int, float)):
                    crossovers.append(CadCrossoverSetting(
                        crossover_type=(
                            'high_pass' if subtype.startswith('High')
                            else 'low_pass'),
                        frequency_hz=float(frequency),
                        filter_order=1,
                    ))
                elif subtype in ('Highpass', 'Lowpass') and isinstance(
                        frequency, (int, float)):
                    q = parameters.get('q')
                    if isinstance(q, (int, float)) and abs(
                            float(q) - _BUTTERWORTH_Q) <= 1e-6:
                        crossovers.append(CadCrossoverSetting(
                            crossover_type=(
                                'high_pass' if subtype.startswith('High')
                                else 'low_pass'),
                            frequency_hz=float(frequency),
                            filter_order=2,
                        ))
                    # an htdt_-owned order-2 crossover is only ever
                    # written as Butterworth — any other q means the
                    # device deviates, so leave it out and let the diff
                    # surface the drift.
                # other subtypes are not htdt-representable: left out of
                # the reconstruction so the diff surfaces the deviation.
            elif suffix.startswith('_peq') and spec.get('type') == 'Biquad':
                subtype = parameters.get('type')
                filter_type = _CAMILLA_TO_BIQUAD_TYPE.get(subtype)
                frequency = parameters.get('freq')
                q = parameters.get('q')
                gain = parameters.get('gain', 0.0)
                if (
                    filter_type is None
                    or not isinstance(frequency, (int, float))
                    or not isinstance(q, (int, float))
                    or not isinstance(gain, (int, float))
                ):
                    continue
                try:
                    index = int(suffix[len('_peq'):]) - 1
                except (TypeError, ValueError):
                    index = -1
                peq_ids = entry.get('peq_filter_ids')
                filter_id = (
                    peq_ids[index]
                    if isinstance(peq_ids, list)
                    and 0 <= index < len(peq_ids)
                    and isinstance(peq_ids[index], str)
                    else name
                )
                try:
                    peq.append(build_biquad_filter(
                        filter_id=filter_id,
                        filter_type=filter_type,
                        frequency_hz=float(frequency),
                        q=float(q),
                        gain_db=float(gain)
                        if filter_type == 'peaking' else 0.0,
                        sample_rate_hz=sample_rate,
                    ))
                except ValueError:
                    # Device-side values that fail device-neutral
                    # validation stay unrepresented — the divergence
                    # shows as a deviation, never a silent rewrite.
                    continue
        channels.append(CadExportedChannelSettings(
            channel_id=channel_id,
            role_id=str(entry.get('role_id') or 'unknown'),
            source_entity_id=str(entry.get('source_entity_id') or 'unknown'),
            physical_output_id=str(
                entry.get('physical_output_id') or 'unknown'),
            gain_db=gain_db,
            delay_s=delay_s,
            polarity=polarity,
            crossovers=tuple(crossovers),
            peq=tuple(peq),
            routing=tuple(sorted(set(observed_outputs))),
        ))
    return tuple(channels), blob


# ----------------------------------------------------------------------
# sealed deployment evidence


class CamillaDSPDeploymentSession(BaseModel):
    """Pinned evidence of one CamillaDSP deploy chain (cdsp-).

    Binds the #806 deployment authority to the concrete protocol
    artifacts: the candidate config hash that was sent, the pre-deploy
    baseline hash, the apply ack id and the config read-back verdict.
    Optional refs pin the runtime observation and post-deployment
    effectiveness report that advance the stage ladder.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    session_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str
    deployment_ref: AuthorityRef
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    materialization_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_config_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    previous_config_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$')
    deploy_ack_id: str | None = None
    deployed_at_utc: str = Field(min_length=1)
    readback_config_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$')
    #: None = no read-back taken; True/False = canonical config equality.
    readback_matched: bool | None = None
    runtime_observation_ref: AuthorityRef | None = None
    effectiveness_report_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CamillaDSPDeploymentSession':
        _require_refs(
            self.deployment_ref,
            self.runtime_observation_ref,
            self.effectiveness_report_ref,
        )
        if self.deployment_ref.kind != 'calibration_deployment':
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        if self.runtime_observation_ref is not None and (
            self.runtime_observation_ref.kind
            != 'camilladsp_runtime_observation'
        ):
            raise ValueError(
                "runtime_observation_ref kind must be "
                "'camilladsp_runtime_observation'")
        if self.effectiveness_report_ref is not None and (
            self.effectiveness_report_ref.kind
            != 'deployment_effectiveness_report'
        ):
            raise ValueError(
                "effectiveness_report_ref kind must be "
                "'deployment_effectiveness_report'")
        if self.readback_matched is not True and (
            self.runtime_observation_ref is not None
            or self.effectiveness_report_ref is not None
        ):
            raise ValueError(
                'runtime/effectiveness refs require a matched '
                'config read-back — the ladder cannot advance on a '
                'divergent or missing read-back')
        if (self.readback_matched is None) != (
                self.readback_config_sha256 is None):
            raise ValueError(
                'readback_matched and readback_config_sha256 must be '
                'recorded together — a read-back verdict must pin the '
                'config it compared')
        if self.session_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CamillaDSPDeploymentSession hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'session_id', 'session_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CamillaDSPDeploymentSession':
        return _seal(
            cls, payload, 'session_id', 'session_sha256', 'cdsp')


class CamillaDSPRuntimeObservation(BaseModel):
    """Device runtime telemetry (crun-) — runtime evidence, never
    acoustic verification.

    Clipping counters, processing state, signal levels and rate metrics
    bound to one binding (and optionally one deployment). Commands that
    fail are recorded in ``limitations`` — unknown is explicit, never a
    default value.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    deployment_ref: AuthorityRef | None = None
    observed_at_utc: str = Field(min_length=1)
    camilladsp_version: str | None = None
    processing_state: str | None = None
    stop_reason: str | None = None
    clipped_samples: int | None = Field(default=None, ge=0)
    capture_rate_hz: int | None = Field(default=None, gt=0)
    rate_adjust: float | None = None
    buffer_level: int | None = Field(default=None, ge=0)
    processing_load_pct: float | None = Field(default=None, ge=0.0)
    capture_peak_dbfs: tuple[float, ...] | None = None
    capture_rms_dbfs: tuple[float, ...] | None = None
    playback_peak_dbfs: tuple[float, ...] | None = None
    playback_rms_dbfs: tuple[float, ...] | None = None
    limitations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'CamillaDSPRuntimeObservation':
        _require_refs(self.deployment_ref)
        if self.deployment_ref is not None and (
            self.deployment_ref.kind != 'calibration_deployment'
        ):
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        if self.observation_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CamillaDSPRuntimeObservation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'observation_id', 'observation_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CamillaDSPRuntimeObservation':
        return _seal(
            cls, payload,
            'observation_id', 'observation_sha256', 'crun')


class CamillaDSPRollbackEvidence(BaseModel):
    """Evidence of a config rollback attempt (crbk-).

    ``restored_verified`` requires the post-rollback read-back to equal
    the config the rollback set, and — when a deployment session pinned
    a baseline — that the fetched previous config equals it. Any weaker
    outcome is recorded, never upgraded.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str
    deployment_ref: AuthorityRef
    session_ref: AuthorityRef | None = None
    previous_config_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$')
    requested_at_utc: str = Field(min_length=1)
    restored_config_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$')
    post_rollback_readback_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$')
    outcome: CamillaDSPRollbackOutcome
    error_detail: str | None = None
    #: Optional link to the #806 authority record that superseded the
    #: deployment this evidence rolls back.
    rollback_record_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CamillaDSPRollbackEvidence':
        _require_refs(
            self.deployment_ref,
            self.session_ref,
            self.rollback_record_ref,
        )
        if self.deployment_ref.kind != 'calibration_deployment':
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        if self.session_ref is not None and (
            self.session_ref.kind != 'camilladsp_deployment_session'
        ):
            raise ValueError(
                "session_ref kind must be "
                "'camilladsp_deployment_session'")
        if self.rollback_record_ref is not None and (
            self.rollback_record_ref.kind != 'deployment_rollback'
        ):
            raise ValueError(
                "rollback_record_ref kind must be 'deployment_rollback'")
        if self.outcome in (
            'restored_verified', 'restored_previous_diverged',
            'restored_mismatch',
        ) and self.post_rollback_readback_sha256 is None:
            raise ValueError(
                'a restored outcome requires post-rollback read-back')
        if self.evidence_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CamillaDSPRollbackEvidence hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CamillaDSPRollbackEvidence':
        return _seal(
            cls, payload, 'evidence_id', 'evidence_sha256', 'crbk')


def derive_camilladsp_stage(
    session: CamillaDSPDeploymentSession | None,
) -> CamillaDSPDeploymentStage:
    """Furthest honest stage the pinned session evidence supports.

    The ladder never advances on request alone: read-back match is
    required before ``config_readback_matched``, a bound runtime
    observation before ``runtime_observed``, and a bound post-deployment
    effectiveness report before ``post_measurement_verified``.
    """
    if session is None:
        return 'designed'
    if session.readback_matched is True:
        if session.runtime_observation_ref is not None:
            if session.effectiveness_report_ref is not None:
                return 'post_measurement_verified'
            return 'runtime_observed'
        return 'config_readback_matched'
    if session.deploy_ack_id is not None:
        return 'deploy_acknowledged'
    return 'compiled_for_target'


# ----------------------------------------------------------------------
# the adapter


class CamillaDSPCalibrationAdapter:
    """``CalibrationDeviceAdapter`` over the CamillaDSP WebSocket API.

    ``transports`` maps operator-configured endpoint URIs
    (``camilladsp://host:port``) to live transports — construction-time
    wiring, never discovery. ``binding.device_serial`` carries the
    endpoint URI: for a headless daemon, the endpoint *is* the device
    identity on the network.
    """

    def __init__(
        self,
        transports: dict[str, CamillaDSPTransport],
        *,
        approved_remote_endpoints: tuple[str, ...] = (),
    ) -> None:
        self._transports = dict(transports)
        self._approved_remote = frozenset(approved_remote_endpoints)

    def capability(self) -> AdapterCapabilityReport:
        return AdapterCapabilityReport(
            adapter_id=CAMILLADSP_DEPLOY_ADAPTER_ID,
            adapter_version=CAMILLADSP_DEPLOY_ADAPTER_VERSION,
            adapter_kind='network_api',
            device_family='camilladsp',
            supports_apply=True,
            supports_read_back=True,
            supports_materialization=True,
            deploy_mechanism='machine_write',
            readback_mechanism='machine_exact',
            rollback_mechanism='previous_config',
            runtime_observation='telemetry',
            supported_features=(
                'peq', 'gain', 'delay', 'polarity', 'fir', 'routing',
            ),
            limit_notes=(
                'Device-side resource limits (available filter slots, '
                'pipeline size) are surfaced as unsupported_items during '
                'materialization.',
            ),
            auth_requirements=(
                'operator_confirmation',
                'approved_remote_endpoint',
            ),
            applicability='camilladsp documented WebSocket JSON API',
            protocol_authority='open_source',
            notes=(
                'Deploy: ValidateConfigJson-gated SetConfigJson over '
                'the documented WebSocket API.',
                'Read-back: GetConfigJson normalized through the '
                'htdt_-owned config region.',
                'Rollback: GetPreviousConfig + post-rollback read-back '
                'evidence.',
                'Loopback endpoints by default; remote endpoints '
                'require explicit operator approval — no LAN discovery.',
                'SetConfigJson Ok is a device ack only — never acoustic '
                'verification.',
            ),
        )

    # -- transport plumbing ------------------------------------------

    def _transport(
        self, binding: AdapterDeviceBinding,
    ) -> CamillaDSPTransport:
        _assert_binding(CAMILLADSP_DEPLOY_ADAPTER_ID, binding)
        endpoint = binding.device_serial
        host = _endpoint_host(endpoint)
        if (
            host not in _LOOPBACK_HOSTS
            and endpoint not in self._approved_remote
        ):
            raise CamillaDSPEndpointError(
                f'remote endpoint {endpoint!r} requires explicit '
                'operator approval (approved_remote_endpoints)')
        transport = self._transports.get(endpoint)
        if transport is None:
            raise AdapterCapabilityError(
                f'endpoint {endpoint!r} is not bound to this adapter')
        return transport

    @staticmethod
    def _call(
        transport: CamillaDSPTransport,
        command: str,
        argument: Any = None,
    ) -> Any:
        response = transport.request({command: argument})
        ok, value = _result_value(response, command)
        if not ok:
            raise CamillaDSPError(
                'device_error', f'{command}: {value!r}')
        return value

    def _current_config(
        self, binding: AdapterDeviceBinding,
    ) -> dict[str, Any]:
        return _parse_config_document(
            self._call(self._transport(binding), 'GetConfigJson'))

    # -- CalibrationDeviceAdapter contract ----------------------------

    def materialize(
        self,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        *,
        created_at_utc: str,
    ) -> MaterializedCalibrationSettings:
        transport = self._transport(binding)
        base = _parse_config_document(
            self._call(transport, 'GetConfigJson'))
        candidate, notes, unsupported = compile_camilladsp_config(
            export, binding, base)
        payload_text = _canonical(candidate) + '\n'
        materialization_id = 'mat:' + _hash(
            {
                'adapter_id': CAMILLADSP_DEPLOY_ADAPTER_ID,
                'binding_sha256': binding.binding_sha256,
                'export_sha256': export.exported_settings_semantic_sha256,
                'candidate_config_sha256': config_sha256(candidate),
            }
        )[:32]
        payload: dict[str, Any] = {
            'materialization_id': materialization_id,
            'created_at_utc': created_at_utc,
            'adapter_id': CAMILLADSP_DEPLOY_ADAPTER_ID,
            'adapter_version': CAMILLADSP_DEPLOY_ADAPTER_VERSION,
            'export_id': export.export_id,
            'exported_settings_semantic_sha256': (
                export.exported_settings_semantic_sha256
            ),
            'binding_id': binding.binding_id,
            'binding_sha256': binding.binding_sha256,
            'payload_text': payload_text,
            'channel_settings': export.channels,
            'quantization_applied': export.quantization_applied,
            'quantization_notes': (
                tuple(export.quantization_notes) + tuple(notes)
            ),
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
        transport = self._transport(binding)
        if not operator_confirmed:
            raise PermissionError(
                'CamillaDSP config mutation requires explicit operator '
                'confirmation')
        candidate = materialization.payload_text.strip()
        # The device's own schema/deep validation must accept the
        # candidate before it is applied — a rejected validate never
        # reaches SetConfigJson.
        self._call(transport, 'ValidateConfigJson', candidate)
        self._call(transport, 'SetConfigJson', candidate)
        ack_id = 'ack:' + _hash(
            {
                'materialization_id': materialization.materialization_id,
                'binding_sha256': binding.binding_sha256,
                'applied_at_utc': applied_at_utc,
            }
        )[:32]
        return DeviceApplyAck(
            ack_id=ack_id,
            materialization_id=materialization.materialization_id,
            acked_at_utc=applied_at_utc,
            device_note=(
                'SetConfigJson accepted after ValidateConfigJson — an '
                'ack only, not installed-state evidence'),
        )

    def read_back(
        self,
        binding: AdapterDeviceBinding,
        *,
        observed_at_utc: str,
    ) -> tuple[CadExportedChannelSettings, ...]:
        config = self._current_config(binding)
        normalized = normalize_camilladsp_config(config)
        if normalized is None:
            raise AdapterCapabilityError(
                'device config carries no htdt deployment region — '
                'nothing this adapter wrote can be read back')
        channels, _blob = normalized
        return channels

    # -- evidence surface ----------------------------------------------

    def capture_baseline(
        self, binding: AdapterDeviceBinding,
    ) -> tuple[str, str]:
        """Canonical pre-deploy config and its semantic hash."""
        config = self._current_config(binding)
        canonical = _canonical(config)
        return canonical, config_sha256(config)

    def read_back_config(
        self, binding: AdapterDeviceBinding,
    ) -> tuple[dict[str, Any], str]:
        """The device's live config and its semantic hash — used to bind
        ``readback_config_sha256`` on a deployment session."""
        config = self._current_config(binding)
        return config, config_sha256(config)

    def observe_runtime(
        self,
        binding: AdapterDeviceBinding,
        *,
        document_id: str,
        observed_at_utc: str,
        deployment_ref: AuthorityRef | None = None,
    ) -> CamillaDSPRuntimeObservation:
        """Seal the device's runtime telemetry as evidence.

        Every command that fails lands in ``limitations`` — unknown is
        explicit. This record is runtime evidence only; it cannot
        produce or upgrade acoustic verification.
        """
        transport = self._transport(binding)
        limitations: list[str] = []

        def _try(command: str, field: str = '') -> Any:
            try:
                return self._call(transport, command)
            except CamillaDSPError as error:
                limitations.append(f'{command}:{error.kind}')
                return None

        version = _try('GetVersion')
        state = _try('GetState')
        stop_reason = _try('GetStopReason')
        clipped = _try('GetClippedSamples')
        capture_rate = _try('GetCaptureRate')
        rate_adjust = _try('GetRateAdjust')
        buffer_level = _try('GetBufferLevel')
        processing_load = _try('GetProcessingLoad')
        levels = _try('GetSignalLevels')
        playback_peak = playback_rms = capture_peak = capture_rms = None
        if isinstance(levels, dict):
            def _vector(name: str) -> tuple[float, ...] | None:
                value = levels.get(name)
                if not isinstance(value, list):
                    return None
                try:
                    return tuple(float(item) for item in value)
                except (TypeError, ValueError):
                    return None

            playback_peak = _vector('playback_peak')
            playback_rms = _vector('playback_rms')
            capture_peak = _vector('capture_peak')
            capture_rms = _vector('capture_rms')
        return CamillaDSPRuntimeObservation.create(
            document_id=document_id,
            binding_sha256=binding.binding_sha256,
            deployment_ref=deployment_ref,
            observed_at_utc=observed_at_utc,
            camilladsp_version=(
                str(version) if isinstance(version, str) else None),
            processing_state=(
                str(state) if isinstance(state, str) else None),
            stop_reason=(
                str(stop_reason)
                if isinstance(stop_reason, str) else None),
            clipped_samples=(
                int(clipped)
                if isinstance(clipped, (int, float)) else None),
            capture_rate_hz=(
                int(capture_rate)
                if isinstance(capture_rate, (int, float)) else None),
            rate_adjust=(
                float(rate_adjust)
                if isinstance(rate_adjust, (int, float)) else None),
            buffer_level=(
                int(buffer_level)
                if isinstance(buffer_level, (int, float)) else None),
            processing_load_pct=(
                float(processing_load)
                if isinstance(processing_load, (int, float)) else None),
            capture_peak_dbfs=capture_peak,
            capture_rms_dbfs=capture_rms,
            playback_peak_dbfs=playback_peak,
            playback_rms_dbfs=playback_rms,
            limitations=tuple(sorted(set(limitations))),
        )

    def rollback_previous(
        self,
        binding: AdapterDeviceBinding,
        *,
        document_id: str,
        deployment_ref: AuthorityRef,
        requested_at_utc: str,
        session: CamillaDSPDeploymentSession | None = None,
        rollback_record_ref: AuthorityRef | None = None,
    ) -> CamillaDSPRollbackEvidence:
        """Restore the previous config and seal the evidence.

        ``GetPreviousConfig`` → ``ValidateConfigJson`` →
        ``SetConfigJson`` → ``GetConfigJson`` read-back. Outcome is
        ``restored_verified`` only when the read-back equals what was
        set *and* the previous config equals the session's pinned
        baseline (a divergence means the device state changed since the
        deploy — still surfaced, never hidden).
        """
        transport = self._transport(binding)
        session_ref = None
        baseline = None
        if session is not None:
            session_ref = AuthorityRef(
                kind='camilladsp_deployment_session',
                ref_id=session.session_id,
                ref_sha256=session.session_sha256,
            )
            baseline = session.previous_config_sha256
        previous_sha = restored_sha = readback_sha = None
        error_detail = None
        outcome: CamillaDSPRollbackOutcome = 'failed'
        try:
            previous = _parse_config_document(
                self._call(transport, 'GetPreviousConfig'))
            previous_sha = config_sha256(previous)
            candidate = _canonical(previous)
            self._call(transport, 'ValidateConfigJson', candidate)
            self._call(transport, 'SetConfigJson', candidate)
            restored_sha = previous_sha
            readback = _parse_config_document(
                self._call(transport, 'GetConfigJson'))
            readback_sha = config_sha256(readback)
            if readback_sha != restored_sha:
                outcome = 'restored_mismatch'
            elif baseline is not None and previous_sha != baseline:
                outcome = 'restored_previous_diverged'
            else:
                outcome = 'restored_verified'
        except (CamillaDSPError, AdapterCapabilityError) as error:
            error_detail = str(error)
        return CamillaDSPRollbackEvidence.create(
            document_id=document_id,
            deployment_ref=deployment_ref,
            session_ref=session_ref,
            previous_config_sha256=previous_sha,
            requested_at_utc=requested_at_utc,
            restored_config_sha256=restored_sha,
            post_rollback_readback_sha256=readback_sha,
            outcome=outcome,
            error_detail=error_detail,
            rollback_record_ref=rollback_record_ref,
        )


__all__ = [
    'CAMILLADSP_DEPLOY_ADAPTER_ID',
    'CAMILLADSP_DEPLOY_ADAPTER_VERSION',
    'CAMILLADSP_DEPLOY_LABELS',
    'HTDT_DESCRIPTION_KEY',
    'HTDT_REGION_PREFIX',
    'CamillaDSPCalibrationAdapter',
    'CamillaDSPDeploymentSession',
    'CamillaDSPDeploymentStage',
    'CamillaDSPEndpointError',
    'CamillaDSPRollbackEvidence',
    'CamillaDSPRollbackOutcome',
    'CamillaDSPRuntimeObservation',
    'compile_camilladsp_config',
    'config_sha256',
    'derive_camilladsp_stage',
    'normalize_camilladsp_config',
]
