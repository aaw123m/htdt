"""Spectral lighting & bias-light verification evidence (#1076).

Importers and typed evidence records for spectral luminaire/light data:

- **ANSI/IES TM-27** — XML ``.spdx`` spectral-data interchange
  (SpectralData rows of wavelength vs. quantity);
- **IES TM-33** — XML luminaire photometric+chromaticity document
  (spectral power distribution embedded alongside photometry).

Hard rules — four quantities stay separate:

- ``cct_k`` — correlated colour temperature, a scalar;
- ``chromaticity_xy`` — a CIE 1931 (x, y) pair;
- ``spectral_power`` — the SPD vector over wavelength;
- ``illuminance_lux`` — an illuminance reading.

An importer never derives one from another. The only sanctioned
derivation is :func:`chromaticity_from_spd` — a labelled computation
(``derived_from_spd=True``) for bias-light checks, never an overwrite
of a published field. ``None`` means "not published", not "computed".
"""

from __future__ import annotations

import hashlib
from math import isfinite
from typing import Literal, NamedTuple
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance


SPECTRAL_LIGHTING_AUTHORITY_VERSION = 'spectral-lighting-1'


SpectralDocFormat = Literal['tm27_spdx', 'tm33_xml']
SpectralEvidenceKind = Literal[
    'manufacturer_declared',
    'lab_measured',
    'user_measured',
]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --- spectral evidence record ------------------------------------------

class SpectralSample(BaseModel):
    """One (wavelength_nm, power) point of an SPD."""

    model_config = ConfigDict(frozen=True)

    wavelength_nm: float = Field(gt=0.0)
    power: float

    @model_validator(mode='after')
    def _check(self) -> 'SpectralSample':
        if not isfinite(self.wavelength_nm) or not isfinite(self.power):
            raise ValueError('spectral sample must be finite')
        return self


class SpectralEvidence(BaseModel):
    """Lighting evidence with strictly separated quantity fields."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    evidence_kind: SpectralEvidenceKind
    cct_k: float | None = Field(default=None, gt=0.0)
    chromaticity_xy: tuple[float, float] | None = None
    spectral_power: tuple[SpectralSample, ...] | None = None
    illuminance_lux: float | None = Field(default=None, ge=0.0)
    derived_from_spd: bool = False
    """True only when chromaticity/cct were *computed* from this
    record's own SPD — published values are always stored with
    ``derived_from_spd=False``."""
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _check(self) -> 'SpectralEvidence':
        if self.chromaticity_xy is not None:
            x, y = self.chromaticity_xy
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError('chromaticity out of the unit square')
        if self.derived_from_spd and self.spectral_power is None:
            raise ValueError(
                'derived chromaticity requires spectral_power'
            )
        if self.spectral_power is not None:
            wl = [s.wavelength_nm for s in self.spectral_power]
            if len(wl) < 2 or wl != sorted(wl):
                raise ValueError(
                    'spectral wavelengths must be strictly increasing'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('spectral evidence hash mismatch')
        return self

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )


def _hash(payload: dict) -> str:
    import json

    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    ).hexdigest()


def build_spectral_evidence(
    *,
    evidence_id: str,
    source_name: str,
    evidence_kind: SpectralEvidenceKind,
    source_sha256: str,
    cct_k: float | None = None,
    chromaticity_xy: tuple[float, float] | None = None,
    spectral_power: tuple[SpectralSample, ...] | None = None,
    illuminance_lux: float | None = None,
    derived_from_spd: bool = False,
) -> SpectralEvidence:
    probe = SpectralEvidence.model_construct(
        evidence_id=evidence_id,
        source_name=source_name,
        evidence_kind=evidence_kind,
        cct_k=cct_k,
        chromaticity_xy=chromaticity_xy,
        spectral_power=spectral_power,
        illuminance_lux=illuminance_lux,
        derived_from_spd=derived_from_spd,
        source_sha256=source_sha256,
        semantic_sha256='',
    )
    return SpectralEvidence(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# --- TM-27 / TM-33 XML importers ----------------------------------------

class ParsedSpectralDoc(NamedTuple):
    verdict: Literal['valid', 'invalid']
    evidence: SpectralEvidence | None
    detail: str


def _strip_ns(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _collect_spectral_columns(root) -> tuple[SpectralSample, ...] | None:
    """Walk the document for wavelength/power pairs.

    Supports the TM-27/TM-33 row forms ``<SpectralData
    wavelength=".." power=".."/>`` and the flat
    ``<SpectralValue><Wavelength/><Value/></SpectralValue>`` shape.
    """
    samples: list[SpectralSample] = []
    for el in root.iter():
        name = _strip_ns(el.tag)
        if name in ('SpectralData', 'SpectralValue'):
            wl = el.get('wavelength')
            pw = el.get('power') or el.get('value')
            if wl is None:
                wnode = next(
                    (
                        c
                        for c in el.iter()
                        if _strip_ns(c.tag) == 'Wavelength'
                    ),
                    None,
                )
                wl = wnode.text.strip() if wnode is not None and wnode.text else None
            if pw is None:
                vnode = next(
                    (
                        c
                        for c in el.iter()
                        if _strip_ns(c.tag) in ('Value', 'Power')
                    ),
                    None,
                )
                pw = vnode.text.strip() if vnode is not None and vnode.text else None
            if wl is not None and pw is not None:
                try:
                    samples.append(
                        SpectralSample(
                            wavelength_nm=float(wl), power=float(pw)
                        )
                    )
                except ValueError:
                    return None
    if not samples:
        return None
    samples.sort(key=lambda s: s.wavelength_nm)
    return tuple(samples)


def _text_at(root, *names: str) -> str | None:
    for el in root.iter():
        if _strip_ns(el.tag) in names and el.text and el.text.strip():
            return el.text.strip()
    return None


def parse_spectral_xml(
    text: str,
    *,
    evidence_id: str,
    evidence_kind: SpectralEvidenceKind = 'manufacturer_declared',
    source_name: str = 'tm27-tm33-import',
) -> ParsedSpectralDoc:
    """Import a TM-27 ``.spdx`` or TM-33 XML document.

    Extracts the SPD table plus any *published* CCT/chromaticity/lux
    fields; nothing is computed during import.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        return ParsedSpectralDoc('invalid', None, f'XML parse error: {exc}')
    samples = _collect_spectral_columns(root)
    cct_raw = _text_at(
        root,
        'CCT',
        'CorrelatedColorTemperature',
        'CorrelatedColourTemperature',
    )
    x_raw = _text_at(root, 'CIEx', 'ChromaticityX', 'x')
    y_raw = _text_at(root, 'CIEy', 'ChromaticityY', 'y')
    lux_raw = _text_at(root, 'Illuminance', 'Lux')
    cct = None
    if cct_raw is not None:
        try:
            cct = float(cct_raw)
        except ValueError:
            return ParsedSpectralDoc(
                'invalid', None, 'non-numeric published CCT'
            )
    xy = None
    if x_raw is not None and y_raw is not None:
        try:
            xy = (float(x_raw), float(y_raw))
        except ValueError:
            return ParsedSpectralDoc(
                'invalid', None, 'non-numeric published chromaticity'
            )
    lux = None
    if lux_raw is not None:
        try:
            lux = float(lux_raw)
        except ValueError:
            return ParsedSpectralDoc(
                'invalid', None, 'non-numeric published lux'
            )
    if samples is None and cct is None and xy is None and lux is None:
        return ParsedSpectralDoc(
            'invalid',
            None,
            'document carries no spectral or chromaticity data',
        )
    root_name = _strip_ns(root.tag)
    doc_format: SpectralDocFormat = (
        'tm27_spdx'
        if root_name.lower().startswith('spdx')
        or _text_at(root, 'SpectralDistribution') is not None
        else 'tm33_xml'
    )
    evidence = build_spectral_evidence(
        evidence_id=evidence_id,
        source_name=f'{source_name}:{doc_format}',
        evidence_kind=evidence_kind,
        source_sha256=_sha256_text(text),
        cct_k=cct,
        chromaticity_xy=xy,
        spectral_power=samples,
        illuminance_lux=lux,
        derived_from_spd=False,
    )
    return ParsedSpectralDoc('valid', evidence, f'parsed {doc_format}')


# --- labelled SPD derivations --------------------------------------------

# CIE 1931 2-degree colour-matching functions, coarse tabulation at
# 5 nm (380–780 nm) — published standard values (CIE 015:2018 tables).
_CMf_5NM: tuple[tuple[float, float, float, float], ...] = (
    (380.0, 0.001368, 0.000039, 0.006450),
    (385.0, 0.002236, 0.000064, 0.010550),
    (390.0, 0.004243, 0.000120, 0.020050),
    (395.0, 0.007650, 0.000217, 0.036210),
    (400.0, 0.014310, 0.000396, 0.067850),
    (405.0, 0.023190, 0.000640, 0.110200),
    (410.0, 0.043510, 0.001210, 0.207400),
    (415.0, 0.077630, 0.002180, 0.371300),
    (420.0, 0.134380, 0.004000, 0.645600),
    (425.0, 0.214770, 0.007300, 1.039050),
    (430.0, 0.283900, 0.011600, 1.385600),
    (435.0, 0.328500, 0.016840, 1.622960),
    (440.0, 0.348280, 0.023000, 1.747060),
    (445.0, 0.348060, 0.029800, 1.782600),
    (450.0, 0.336200, 0.038000, 1.772110),
    (455.0, 0.318700, 0.048000, 1.744100),
    (460.0, 0.290800, 0.060000, 1.669200),
    (465.0, 0.251100, 0.073900, 1.528100),
    (470.0, 0.195360, 0.090980, 1.287640),
    (475.0, 0.142100, 0.112600, 1.041900),
    (480.0, 0.095640, 0.139020, 0.812950),
    (485.0, 0.057950, 0.169300, 0.616200),
    (490.0, 0.032010, 0.208020, 0.465180),
    (495.0, 0.014700, 0.258600, 0.353300),
    (500.0, 0.004900, 0.323000, 0.272000),
    (505.0, 0.002400, 0.407300, 0.212300),
    (510.0, 0.009300, 0.503000, 0.158200),
    (515.0, 0.029100, 0.608200, 0.111700),
    (520.0, 0.063270, 0.710000, 0.078250),
    (525.0, 0.109600, 0.793200, 0.057250),
    (530.0, 0.165500, 0.862000, 0.042160),
    (535.0, 0.225750, 0.914850, 0.029840),
    (540.0, 0.290400, 0.954000, 0.020300),
    (545.0, 0.359700, 0.980300, 0.013400),
    (550.0, 0.433450, 0.994950, 0.008750),
    (555.0, 0.512050, 1.000000, 0.005750),
    (560.0, 0.594500, 0.995000, 0.003900),
    (565.0, 0.678400, 0.978600, 0.002750),
    (570.0, 0.762100, 0.952000, 0.002100),
    (575.0, 0.842500, 0.915400, 0.001800),
    (580.0, 0.916300, 0.870000, 0.001650),
    (585.0, 0.978600, 0.816300, 0.001400),
    (590.0, 1.026300, 0.757000, 0.001100),
    (595.0, 1.056700, 0.694900, 0.001000),
    (600.0, 1.062200, 0.631000, 0.000800),
    (605.0, 1.045600, 0.566800, 0.000600),
    (610.0, 1.002600, 0.503000, 0.000340),
    (615.0, 0.938400, 0.441200, 0.000240),
    (620.0, 0.854450, 0.381000, 0.000190),
    (625.0, 0.751400, 0.321000, 0.000100),
    (630.0, 0.642400, 0.265000, 0.000050),
    (635.0, 0.541900, 0.217000, 0.000030),
    (640.0, 0.447900, 0.175000, 0.000020),
    (645.0, 0.360800, 0.138200, 0.000010),
    (650.0, 0.283500, 0.107000, 0.000000),
    (655.0, 0.218700, 0.081600, 0.000000),
    (660.0, 0.164900, 0.061000, 0.000000),
    (665.0, 0.121200, 0.044580, 0.000000),
    (670.0, 0.087400, 0.032000, 0.000000),
    (675.0, 0.063600, 0.023200, 0.000000),
    (680.0, 0.046770, 0.017000, 0.000000),
    (685.0, 0.032900, 0.011920, 0.000000),
    (690.0, 0.022700, 0.008210, 0.000000),
    (695.0, 0.015840, 0.005723, 0.000000),
    (700.0, 0.011359, 0.004102, 0.000000),
    (705.0, 0.008111, 0.002929, 0.000000),
    (710.0, 0.005790, 0.002091, 0.000000),
    (715.0, 0.004109, 0.001484, 0.000000),
    (720.0, 0.002899, 0.001047, 0.000000),
    (725.0, 0.002049, 0.000740, 0.000000),
    (730.0, 0.001440, 0.000520, 0.000000),
    (735.0, 0.001000, 0.000361, 0.000000),
    (740.0, 0.000690, 0.000249, 0.000000),
    (745.0, 0.000476, 0.000172, 0.000000),
    (750.0, 0.000332, 0.000120, 0.000000),
    (755.0, 0.000235, 0.000085, 0.000000),
    (760.0, 0.000166, 0.000060, 0.000000),
    (765.0, 0.000117, 0.000042, 0.000000),
    (770.0, 0.000083, 0.000030, 0.000000),
    (775.0, 0.000059, 0.000021, 0.000000),
    (780.0, 0.000042, 0.000015, 0.000000),
)


def chromaticity_from_spd(
    evidence: SpectralEvidence,
) -> SpectralEvidence | None:
    """Compute CIE 1931 (x, y) from the record's own SPD.

    Labelled derivation — the returned record sets
    ``derived_from_spd=True`` and keeps the published fields intact.
    ``None`` when the record carries no SPD. Sample wavelengths outside
    the 5 nm CMF table range are skipped, not interpolated.
    """
    if evidence.spectral_power is None:
        return None
    cmf = {round(w): (x, y, z) for w, x, y, z in _CMf_5NM}
    X = Y = Z = 0.0
    for s in evidence.spectral_power:
        key = round(s.wavelength_nm)
        row = cmf.get(key)
        if row is None:
            continue
        X += s.power * row[0]
        Y += s.power * row[1]
        Z += s.power * row[2]
    total = X + Y + Z
    if total <= 0.0:
        return None
    return build_spectral_evidence(
        evidence_id=evidence.evidence_id + '-derived-xy',
        source_name=evidence.source_name,
        evidence_kind=evidence.evidence_kind,
        source_sha256=evidence.source_sha256,
        cct_k=None,
        chromaticity_xy=(X / total, Y / total),
        spectral_power=evidence.spectral_power,
        illuminance_lux=evidence.illuminance_lux,
        derived_from_spd=True,
    )


# --- bias-light verification ---------------------------------------------

BiasVerdict = Literal[
    'conforms', 'deviates', 'insufficient_data'
]


class BiasLightCheck(NamedTuple):
    verdict: BiasVerdict
    measured_xy: tuple[float, float] | None
    target_xy: tuple[float, float]
    delta_e_proxy: float | None
    detail: str


# Reference-white chromaticity target for video bias lighting.
D65_X = 0.31271
D65_Y = 0.32902
BIAS_LIGHT_TARGET_XY: tuple[float, float] = (D65_X, D65_Y)


def verify_bias_light(
    evidence: SpectralEvidence,
    *,
    target_xy: tuple[float, float] = BIAS_LIGHT_TARGET_XY,
    tolerance: float = 0.005,
) -> BiasLightCheck:
    """Verify a bias-light chromaticity against the D65 target.

    ``insufficient_data`` when no chromaticity is available — the check
    never substitutes CCT for chromaticity.
    """
    xy = evidence.chromaticity_xy
    if xy is None:
        derived = chromaticity_from_spd(evidence)
        xy = derived.chromaticity_xy if derived else None
    if xy is None:
        return BiasLightCheck(
            'insufficient_data', None, target_xy, None,
            'no chromaticity or usable SPD present',
        )
    dist = (
        (xy[0] - target_xy[0]) ** 2 + (xy[1] - target_xy[1]) ** 2
    ) ** 0.5
    verdict: BiasVerdict = (
        'conforms' if dist <= tolerance else 'deviates'
    )
    return BiasLightCheck(
        verdict,
        xy,
        target_xy,
        dist,
        f'chromaticity distance {dist:.5f} vs tolerance {tolerance}',
    )


def spectral_provenance(evidence: SpectralEvidence) -> (
    EquipmentDataProvenance
):
    kind_map = {
        'manufacturer_declared': 'manufacturer',
        'lab_measured': 'measured',
        'user_measured': 'measured',
    }
    return EquipmentDataProvenance(
        evidence_kind=kind_map[evidence.evidence_kind],  # type: ignore[arg-type]
        source_name=evidence.source_name,
        source_version=SPECTRAL_LIGHTING_AUTHORITY_VERSION,
        source_reference=evidence.evidence_id,
        source_sha256=evidence.source_sha256,
    )


__all__ = [
    'BIAS_LIGHT_TARGET_XY',
    'BiasLightCheck',
    'BiasVerdict',
    'D65_X',
    'D65_Y',
    'ParsedSpectralDoc',
    'SPECTRAL_LIGHTING_AUTHORITY_VERSION',
    'SpectralDocFormat',
    'SpectralEvidence',
    'SpectralEvidenceKind',
    'SpectralSample',
    'build_spectral_evidence',
    'chromaticity_from_spd',
    'parse_spectral_xml',
    'spectral_provenance',
    'verify_bias_light',
]
