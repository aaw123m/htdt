"""BRAS v3 metric manifests (#836 Action 3).

Turns imported ``GeneralFIR`` measurements into honest reference
observables for qualification runs (Action 4+). Every reference is
derived *verbatim* from the measured IR — the manifest records exactly
how (metric_id/metric_version, band, window, onset threshold, filter
method) so nothing about the derivation is implicit.

Observable honesty:
- ``impulse_window``, ``arrival_timing``, ``decay_metric``,
  ``magnitude_fr`` are derivable from measured RIRs.
- ``complex_transfer`` is NOT published: the v3 corpus does not certify
  coherent phase, so a phase-bearing reference would be invented truth.
- Tolerances are deliberately absent here — pass/fail bounds belong to
  the qualification config (Action 4), not to the measured reference.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _hash
from .cad_bras_v3_importer import (
    BrasImportError,
    generalfir_ir,
    read_sofa_generalfir,
)
from .cad_external_corpus_manifest import (
    CorpusScene,
    ExternalAssetAdmission,
)


_SHA256 = r'^[0-9a-f]{64}$'

MetricKind = Literal[
    'arrival_timing', 'impulse_window', 'decay_metric', 'magnitude_fr'
]

METRIC_MANIFEST_VERSION = '1'


class BrasMetricEntry(BaseModel):
    """One reference observable bound to a scene member + IR axis.

    ``band_hz`` is the ISO third-octave band for ``decay_metric`` /
    ``magnitude_fr`` references; ``window_s`` bounds an
    ``impulse_window``; ``onset_threshold`` documents the energy-onset
    fraction used for ``arrival_timing``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable_id: str = Field(min_length=1)
    kind: MetricKind
    metric_id: str = Field(min_length=1)
    metric_version: str = Field(min_length=1)
    measurement_index: int = Field(ge=0)
    channel_index: int = Field(default=0, ge=0)
    band_hz: tuple[float, float] | None = None
    window_s: tuple[float, float] | None = None
    onset_threshold: float | None = Field(
        default=None, gt=0.0, lt=1.0)
    derivation: str = Field(min_length=1)
    reference: dict[str, Any]

    @model_validator(mode='after')
    def _check(self) -> 'BrasMetricEntry':
        if self.kind == 'decay_metric' and self.band_hz is None:
            raise ValueError('decay_metric requires band_hz')
        if self.kind == 'arrival_timing' and self.onset_threshold is None:
            raise ValueError('arrival_timing requires onset_threshold')
        return self


class BrasV3MetricManifest(BaseModel):
    """Sealed metric manifest for one SOFA member of one scene."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    manifest_id: str = Field(min_length=1)
    manifest_version: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    dataset_admission_id: str = Field(min_length=1)
    member_path: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)
    observables: tuple[BrasMetricEntry, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def _check(self) -> 'BrasV3MetricManifest':
        ids = [o.observable_id for o in self.observables]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate observable_id')
        if self.manifest_sha256 != _hash(self.identity_payload()):
            raise ValueError('manifest sha mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'manifest_id', 'manifest_sha256'})


def _ir_channel(
    source: Path,
    member_path: str,
    m_index: int,
    r_index: int,
) -> Any:
    import numpy as np  # noqa: PLC0415

    ir = np.asarray(generalfir_ir(source, member_path=member_path))
    if ir.ndim != 3:
        raise BrasImportError(f'Data.IR must be MxRxN, got {ir.shape}')
    if not (0 <= m_index < ir.shape[0]):
        raise BrasImportError(
            f'measurement_index {m_index} outside M axis {ir.shape[0]}')
    if not (0 <= r_index < ir.shape[1]):
        raise BrasImportError(
            f'channel_index {r_index} outside R axis {ir.shape[1]}')
    channel = np.asarray(ir[m_index, r_index], dtype=np.float64)
    if not bool(np.isfinite(channel).all()):
        raise BrasImportError('IR channel has non-finite samples')
    return channel


def bras_impulse_window_reference(
    source: Path | str,
    *,
    member_path: str,
    measurement_index: int,
    channel_index: int = 0,
    window_s: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """``{time_s, pressure}`` in declared units — verbatim samples."""
    source = Path(source)
    ir = _ir_channel(source, member_path, measurement_index, channel_index)
    rate = read_sofa_generalfir(source, member_path=member_path).sample_rate_hz
    times = [i / rate for i in range(len(ir))]
    channel = [float(v) for v in ir]
    if window_s is not None:
        low, high = window_s
        pairs = [(t, p) for t, p in zip(times, channel) if low <= t <= high]
        if not pairs:
            raise BrasImportError(f'window {window_s} selects no samples')
        times = [t for t, _ in pairs]
        channel = [p for _, p in pairs]
    return {'time_s': times, 'pressure': channel}


def bras_magnitude_fr_reference(
    source: Path | str,
    *,
    member_path: str,
    measurement_index: int,
    channel_index: int = 0,
    frequencies_hz: tuple[float, ...] | list[float],
) -> dict[str, Any]:
    """Single-sided amplitude spectrum on a declared grid — no
    normalization, units follow the payload."""
    import numpy as np  # noqa: PLC0415

    source = Path(source)
    ir = _ir_channel(source, member_path, measurement_index, channel_index)
    measurement = read_sofa_generalfir(source, member_path=member_path)
    spectrum = np.abs(np.fft.rfft(ir)) / measurement.sample_rate_hz
    freqs = np.fft.rfftfreq(len(ir), 1.0 / measurement.sample_rate_hz)
    requested = [float(f) for f in frequencies_hz]
    out_of_band = [f for f in requested if f < 0.0 or f > freqs[-1]]
    if out_of_band:
        raise BrasImportError(
            f'frequency grid {out_of_band} exceeds measured band '
            f'0–{freqs[-1]:.1f} Hz — refusing extrapolated references')
    values = np.interp(requested, freqs, spectrum)
    return {
        'frequency_hz': requested,
        'magnitude': [float(v) for v in values],
        'units': measurement.data_ir_units,
    }


def bras_arrival_timing_reference(
    source: Path | str,
    *,
    member_path: str,
    measurement_index: int,
    channel_index: int = 0,
    onset_threshold: float,
) -> dict[str, Any]:
    """Energy-onset arrival time: first sample whose |pressure| reaches
    ``onset_threshold`` of the channel peak. The threshold is part of
    the manifest — it is never silently assumed."""
    import numpy as np  # noqa: PLC0415

    if not (0.0 < onset_threshold < 1.0):
        raise BrasImportError('onset_threshold must be in (0, 1)')
    source = Path(source)
    ir = _ir_channel(source, member_path, measurement_index, channel_index)
    rate = read_sofa_generalfir(source, member_path=member_path).sample_rate_hz
    peak = float(np.abs(ir).max())
    if not (math.isfinite(peak) and peak > 0.0):
        raise BrasImportError('IR channel has zero/non-finite peak')
    hits = np.flatnonzero(np.abs(ir) >= onset_threshold * peak)
    if hits.size == 0:
        raise BrasImportError('no onset sample found')
    return {
        'arrival_s': float(hits[0]) / rate,
        'onset_threshold': onset_threshold,
        'method': 'energy_onset_fraction',
    }


def _brickwall_bandpass(
    ir: Any, rate: float, band_hz: tuple[float, float]
) -> Any:
    """Deterministic brick-wall bandpass via rfft masking. The method is
    recorded in the manifest derivation — never implicit."""
    import numpy as np  # noqa: PLC0415

    low, high = band_hz
    freqs = np.fft.rfftfreq(len(ir), 1.0 / rate)
    mask = (freqs >= low) & (freqs < high)
    if not bool(mask.any()):
        raise BrasImportError(
            f'band {band_hz} selects no spectrum bins (Nyquist '
            f'{freqs[-1]:.1f} Hz)')
    spec = np.fft.rfft(ir) * mask
    return np.fft.irfft(spec, n=len(ir))


def _schroeder_t20(
    ir: Any, rate: float, band_hz: tuple[float, float] | None
) -> float:
    """T20 via backward-integrated energy decay, least-squares fit over
    the -5..-25 dB window, extrapolated to -60 dB."""
    import numpy as np  # noqa: PLC0415

    if band_hz is not None:
        ir = _brickwall_bandpass(ir, rate, band_hz)
    energy = np.asarray(ir, dtype=np.float64) ** 2
    schroeder = np.cumsum(energy[::-1])[::-1]
    if schroeder[0] <= 0.0:
        raise BrasImportError('zero channel energy')
    db = 10.0 * np.log10(schroeder / schroeder[0])
    times = np.arange(len(db)) / rate
    sel = (db <= -5.0) & (db >= -25.0)
    if int(sel.sum()) < 2:
        raise BrasImportError(
            'insufficient -5..-25 dB decay range for a T20 fit')
    slope, intercept = np.polyfit(times[sel], db[sel], 1)
    if slope >= 0.0:
        raise BrasImportError(
            'non-decaying Schroeder curve — cannot derive T20')
    return float(-60.0 / slope)


def bras_decay_metric_reference(
    source: Path | str,
    *,
    member_path: str,
    measurement_index: int,
    channel_index: int = 0,
    band_hz: tuple[float, float],
) -> dict[str, Any]:
    """T20 in seconds for one declared band (brick-wall FFT bandpass +
    Schroeder backward integration over -5..-25 dB)."""
    source = Path(source)
    ir = _ir_channel(source, member_path, measurement_index, channel_index)
    rate = read_sofa_generalfir(source, member_path=member_path).sample_rate_hz
    t20 = _schroeder_t20(ir, rate, band_hz)
    return {
        'value_s': t20,
        'band_hz': [float(band_hz[0]), float(band_hz[1])],
        'method': 'brickwall_fft_bandpass+schroeder_t20_-5..-25dB',
    }


def build_bras_v3_metric_manifest(
    dataset: ExternalAssetAdmission,
    scene: CorpusScene,
    root: Path | str,
    *,
    member_path: str,
    measurement_indexes: tuple[int, ...],
    bands_hz: tuple[tuple[float, float], ...] = (),
    onset_threshold: float = 0.1,
    manifest_version: str = METRIC_MANIFEST_VERSION,
) -> BrasV3MetricManifest:
    """Build a sealed metric manifest for one SOFA member.

    Emits per measurement index: an ``arrival_timing`` reference
    (declared ``onset_threshold``) and, per declared band, a
    ``decay_metric`` T20 reference. The member must exist in the scene
    zip and the IR axis indexes are bounds-checked — fail closed.
    """
    if scene.dataset_ref.ref_sha256 != dataset.semantic_sha256:
        raise BrasImportError('dataset admission hash drifted from the scene pin')
    if not measurement_indexes:
        raise BrasImportError('no measurement indexes declared')
    root = Path(root)
    scene_files = {f.file_name for f in dataset.files}
    candidates = [
        n for n in scene.asset_file_names
        if n.lower().endswith('.zip')
        and 'scene_descriptions' in n.lower()
        and n in scene_files
    ]
    if len(candidates) != 1:
        raise BrasImportError(
            f'scene {scene.scene_id}: cannot resolve its scene zip')
    zip_path = root / dataset.dataset_name / candidates[0]
    if not zip_path.is_file():
        raise BrasImportError(f'scene zip missing: {zip_path}')

    measurement = read_sofa_generalfir(zip_path, member_path=member_path)
    entries: list[BrasMetricEntry] = []
    for m_index in measurement_indexes:
        if not (0 <= m_index < measurement.measurement_count):
            raise BrasImportError(
                f'measurement_index {m_index} outside '
                f'{measurement.measurement_count}')
        entries.append(
            BrasMetricEntry(
                observable_id=f'arrival-m{m_index}',
                kind='arrival_timing',
                metric_id='bras-onset-arrival',
                metric_version='1',
                measurement_index=m_index,
                onset_threshold=onset_threshold,
                derivation=(
                    f'first |p| >= {onset_threshold}*peak of the measured IR'
                ),
                reference=bras_arrival_timing_reference(
                    zip_path, member_path=member_path,
                    measurement_index=m_index,
                    onset_threshold=onset_threshold,
                ),
            )
        )
        for band in bands_hz:
            entries.append(
                BrasMetricEntry(
                    observable_id=(
                        f't20-m{m_index}-'
                        f'{int(band[0])}-{int(band[1])}hz'
                    ),
                    kind='decay_metric',
                    metric_id='bras-t20-schroeder',
                    metric_version='1',
                    measurement_index=m_index,
                    band_hz=(float(band[0]), float(band[1])),
                    derivation=(
                        'brick-wall FFT bandpass; Schroeder backward '
                        'integration; least-squares T20 over -5..-25 dB'
                    ),
                    reference=bras_decay_metric_reference(
                        zip_path, member_path=member_path,
                        measurement_index=m_index,
                        band_hz=band,
                    ),
                )
            )

    probe = BrasV3MetricManifest.model_construct(
        manifest_id='pending',
        manifest_version=manifest_version,
        scene_id=scene.scene_id,
        dataset_admission_id=dataset.admission_id,
        member_path=member_path,
        measurement_sha256=measurement.measurement_sha256,
        observables=tuple(entries),
        manifest_sha256='0' * 64,
    )
    digest = _hash(probe.identity_payload())
    return BrasV3MetricManifest(
        **{**probe.model_dump(mode='python'),
           'manifest_id': f'bmm-{digest[:12]}',
           'manifest_sha256': digest}
    )


__all__ = [
    'BrasMetricEntry',
    'BrasV3MetricManifest',
    'METRIC_MANIFEST_VERSION',
    'MetricKind',
    'bras_arrival_timing_reference',
    'bras_decay_metric_reference',
    'bras_impulse_window_reference',
    'bras_magnitude_fr_reference',
    'build_bras_v3_metric_manifest',
]
