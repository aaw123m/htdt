"""Luminaire photometric data interoperability (#1073).

Independent importers for the two standard photometric text formats so
theater-lighting placement and screen-ambient prediction consume real
luminaire evidence instead of typed-in guesses:

- IES LM-63 (``.ies``) — keyword header + TILT block + photometric
  counts line + candela table;
- EULUMDAT (``.ldt``) — fixed-position line format with C-plane /
  gamma-angle candela grids.

Rules:

- parsing is an independent implementation of the published formats —
  no photometric toolbox dependency;
- the artifact stores the verbatim payload and its SHA-256; the parsed
  candela mesh is a derived view, never a rewrite of the file;
- importers are fail-closed: a malformed header, inconsistent angle
  counts or truncated candela table returns ``invalid``/raises —
  nothing is interpolated into existence;
- a photometric artifact states candela per direction and (when
  published) total lumens — it never claims a lux, luminance or
  screen-ambient contribution; :func:`incident_lux_at_point` produces a
  bounded incident estimate only when the caller supplies mounting
  geometry, and never claims screen luminance (that needs reflectance).
"""

from __future__ import annotations

import hashlib
from math import isfinite
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance


LUMINAIRE_AUTHORITY_VERSION = 'luminaire-photometric-1'


PhotometricFormat = Literal['ies_lm63', 'eulumdat_ldt']

PhotometricUnits = Literal['candela_per_klm', 'candela']
"""LM-63 multiplies candela values by a ballast/photometric factor and
expresses them per delivered lumen unless the file says otherwise; LDT
values are cd/klm by definition of the format."""

ParseVerdict = Literal['valid', 'invalid', 'unqualified']
"""Fail-closed parse verdicts."""


class PhotometricArtifact(BaseModel):
    """One imported photometric distribution, verbatim-pinned."""

    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(min_length=1)
    source_format: PhotometricFormat
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    manufacturer: str | None = None
    luminaire_label: str | None = None
    catalog_number: str | None = None
    lumens_per_lamp: float | None = Field(default=None, ge=0.0)
    lamp_count: int | None = Field(default=None, ge=1)
    units: PhotometricUnits
    vertical_angles_deg: tuple[float, ...] = Field(min_length=1)
    horizontal_angles_deg: tuple[float, ...] = Field(min_length=1)
    candela: tuple[tuple[float, ...], ...] = Field(min_length=1)
    """``candela[h][v]`` — candela at horizontal angle ``h`` and
    vertical angle ``v`` (LM-63 convention; LDT C-planes mapped to
    horizontal)."""
    symmetry: str | None = None
    notes: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'PhotometricArtifact':
        nv = len(self.vertical_angles_deg)
        nh = len(self.horizontal_angles_deg)
        if len(self.candela) != nh:
            raise ValueError(
                'candela table must have one row per horizontal angle'
            )
        for row in self.candela:
            if len(row) != nv:
                raise ValueError(
                    'each candela row must cover every vertical angle'
                )
            for value in row:
                if not isfinite(value) or value < 0.0:
                    raise ValueError('candela values must be >= 0 finite')
        prev = -1.0
        for a in self.vertical_angles_deg:
            if not isfinite(a) or a <= prev:
                raise ValueError(
                    'vertical angles must be strictly increasing'
                )
            prev = a
        prev = -1.0
        for a in self.horizontal_angles_deg:
            if not isfinite(a) or a <= prev:
                raise ValueError(
                    'horizontal angles must be strictly increasing'
                )
            prev = a
        return self

    def candela_at(self, h_deg: float, v_deg: float) -> float | None:
        """Exact-grid lookup — no interpolation. ``None`` when either
        angle is not on the published grid."""
        try:
            hi = list(self.horizontal_angles_deg).index(h_deg)
            vi = list(self.vertical_angles_deg).index(v_deg)
        except ValueError:
            return None
        return self.candela[hi][vi]


class ParsedPhotometricFile(NamedTuple):
    verdict: ParseVerdict
    artifact: PhotometricArtifact | None
    detail: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --- IES LM-63 (``.ies``) ------------------------------------------------

def parse_ies_lm63(
    text: str, *, artifact_id: str = 'imported-ies'
) -> ParsedPhotometricFile:
    """Parse an IES LM-63 ``.ies`` text file.

    Supports the standard ``TILT=NONE`` form (independent TILT blocks
    are rejected rather than misparsed). Fail-closed: structural errors
    yield ``verdict='invalid'`` with no artifact.
    """
    lines = [ln.rstrip('\r') for ln in text.split('\n')]
    if not lines or not lines[0].startswith('IESNA'):
        return ParsedPhotometricFile(
            'invalid', None, 'missing IESNA header line'
        )
    keywords: dict[str, str] = {}
    i = 1
    tilt_mode: str | None = None
    # keyword section: KEYWORD=value or [KEYWORD] value
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith('[') and ']' in line:
            key, _, rest = line[1:].partition(']')
            val = rest.strip()
            j = i + 1
            while not val and j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith('[') or '=' in nxt:
                    break
                val = nxt
                j += 1
            keywords[key.strip().upper()] = val
            i += 1
            continue
        if '=' in line:
            key, _, val = line.partition('=')
            key = key.strip().upper()
            val = val.strip()
            if key == 'TILT':
                tilt_mode = val.upper()
            else:
                keywords[key] = val
            i += 1
            continue
        # first non-keyword line starts the counts block
        break
    if tilt_mode is not None and tilt_mode != 'NONE':
        return ParsedPhotometricFile(
            'unqualified',
            None,
            'TILT!=NONE luminaire geometry not supported; '
            'file is not misparsed',
        )
    # counts line: nlamp lumens multiplier nv nh photometric_type
    #              units_type width length height
    try:
        counts = lines[i].split()
        (
            lamp_count,
            lumens_per_lamp,
            multiplier,
            n_vert,
            n_horiz,
            photo_type,
            units_type,
            _w,
            _l,
            _h,
        ) = counts[:10]
        lamp_count_i = int(lamp_count)
        lumens_f = float(lumens_per_lamp)
        mult_f = float(multiplier)
        nv = int(n_vert)
        nh = int(n_horiz)
        i += 1
    except (IndexError, ValueError):
        return ParsedPhotometricFile(
            'invalid', None, 'malformed photometric counts line'
        )
    if nv < 1 or nh < 1:
        return ParsedPhotometricFile(
            'invalid', None, 'empty angle grid declared'
        )
    # ballast factor line, future-use line, input watts line
    for _ in range(3):
        if i >= len(lines):
            return ParsedPhotometricFile(
                'invalid', None, 'truncated numeric section'
            )
        i += 1
    rest = ' '.join(lines[i:]).split()

    def take(n: int) -> list[float]:
        nonlocal rest
        if len(rest) < n:
            raise ValueError('truncated angle/candela table')
        vals = [float(v) for v in rest[:n]]
        rest = rest[n:]
        return vals

    try:
        vertical = take(nv)
        horizontal = take(nh)
        raw = take(nv * nh)
    except ValueError:
        return ParsedPhotometricFile(
            'invalid', None, 'truncated angle/candela table'
        )
    candela: list[tuple[float, ...]] = []
    for h in range(nh):
        candela.append(
            tuple(v * mult_f for v in raw[h * nv:(h + 1) * nv])
        )
    units: PhotometricUnits = 'candela' if int(units_type) == 1 else (
        'candela_per_klm'
    )
    # LM-63: units_type 1 = feet, 2 = meters — candela units come from
    # photometric_type and lumens; we keep candela values verbatim and
    # record lumens separately.
    units = 'candela'
    artifact = PhotometricArtifact(
        artifact_id=artifact_id,
        source_format='ies_lm63',
        source_sha256=_sha256_text(text),
        manufacturer=keywords.get('MANUFAC'),
        luminaire_label=keywords.get('LUMINAIRE'),
        catalog_number=keywords.get('LUMCAT') or keywords.get('LUMCATNO'),
        lumens_per_lamp=lumens_f,
        lamp_count=lamp_count_i,
        units=units,
        vertical_angles_deg=tuple(vertical),
        horizontal_angles_deg=tuple(horizontal),
        candela=tuple(candela),
        symmetry=keywords.get('SYMMETRY'),
        notes=f'LM-63 photometric_type={photo_type}',
    )
    return ParsedPhotometricFile('valid', artifact, 'parsed LM-63 file')


# --- EULUMDAT (``.ldt``) ---------------------------------------------------

_LDT_DTYPE_TO_UNITS = {
    '1': 'candela_per_klm',  # cd/klm
    '2': 'candela',  # cd
}


def parse_eulumdat(
    text: str, *, artifact_id: str = 'imported-ldt'
) -> ParsedPhotometricFile:
    """Parse an EULUMDAT ``.ldt`` file (DIN EN 13032-2 line format)."""
    lines = [ln.rstrip('\r') for ln in text.split('\n')]
    # tolerate trailing empty lines
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 26:
        return ParsedPhotometricFile(
            'invalid', None, 'LDT requires >= 26 fixed-position lines'
        )
    try:
        company = lines[0].strip()
        # lines[1]=Ityp, [2]=Symm, [3]=Mc (c-plane count)
        mc = int(lines[3].strip())
        dc_deg = float(lines[4].strip())
        ng = int(lines[5].strip())
        dg_deg = float(lines[6].strip())
        lumens_total = float(lines[9].strip() or 'nan')
        # line 10: dtype (1=cd/klm, 2=cd)
        dtype = lines[10].strip()
        # lamps line 7 descriptive; watt line 8
    except (ValueError, IndexError):
        return ParsedPhotometricFile(
            'invalid', None, 'malformed LDT header block'
        )
    if mc < 1 or ng < 1:
        return ParsedPhotometricFile(
            'invalid', None, 'empty LDT angle grid'
        )
    expected = 26 + mc * ng
    if len(lines) < expected:
        return ParsedPhotometricFile(
            'invalid', None, 'truncated LDT candela table'
        )
    horizontal = tuple(round(i * dc_deg, 6) for i in range(mc))
    vertical = tuple(round(i * dg_deg, 6) for i in range(ng))
    candela_rows: list[tuple[float, ...]] = []
    try:
        for c in range(mc):
            row = tuple(float(lines[26 + c * ng + g]) for g in range(ng))
            candela_rows.append(row)
    except ValueError:
        return ParsedPhotometricFile(
            'invalid', None, 'non-numeric LDT candela value'
        )
    artifact = PhotometricArtifact(
        artifact_id=artifact_id,
        source_format='eulumdat_ldt',
        source_sha256=_sha256_text(text),
        manufacturer=company or None,
        luminaire_label=lines[7].strip() or None,
        lumens_per_lamp=(
            lumens_total if isfinite(lumens_total) else None
        ),
        lamp_count=None,
        units=_LDT_DTYPE_TO_UNITS.get(dtype, 'candela_per_klm'),
        vertical_angles_deg=vertical,
        horizontal_angles_deg=horizontal,
        candela=tuple(candela_rows),
        symmetry=lines[2].strip() or None,
        notes='EULUMDAT import; dtype=' + dtype,
    )
    return ParsedPhotometricFile('valid', artifact, 'parsed LDT file')


def parse_photometric(
    text: str, *, artifact_id: str
) -> ParsedPhotometricFile:
    """Dispatch on content: IESNA header -> LM-63, else try EULUMDAT."""
    first = text.lstrip().split('\n', 1)[0]
    if first.startswith('IESNA'):
        return parse_ies_lm63(text, artifact_id=artifact_id)
    return parse_eulumdat(text, artifact_id=artifact_id)


# --- bounded incident-lux estimate --------------------------------------

def incident_lux_at_point(
    artifact: PhotometricArtifact,
    *,
    h_deg: float,
    v_deg: float,
    distance_m: float,
    luminous_flux_lm: float | None = None,
) -> float | None:
    """Incident illuminance E = I(theta)/d² for an exact-grid angle.

    Bounded estimate only: candela must be on the published grid (no
    interpolation) and the caller supplies mounting distance. When the
    artifact stores cd/klm the caller must pass the installed luminous
    flux in lumens. Returns ``None`` when the angle is off-grid — never
    an interpolated or assumed value. Screen *luminance* is out of
    scope (requires surface reflectance).
    """
    if distance_m <= 0 or not isfinite(distance_m):
        raise ValueError('distance_m must be positive')
    candela = artifact.candela_at(h_deg, v_deg)
    if candela is None:
        return None
    if artifact.units == 'candela_per_klm':
        if luminous_flux_lm is None or luminous_flux_lm <= 0:
            raise ValueError(
                'cd/klm artifact requires luminous_flux_lm'
            )
        candela = candela * luminous_flux_lm / 1000.0
    return candela / (distance_m * distance_m)


def luminaire_provenance(artifact: PhotometricArtifact) -> (
    EquipmentDataProvenance
):
    return EquipmentDataProvenance(
        evidence_kind='manufacturer',
        source_name=f'photometric:{artifact.source_format}',
        source_version=LUMINAIRE_AUTHORITY_VERSION,
        source_reference=artifact.artifact_id,
        source_sha256=artifact.source_sha256,
    )


__all__ = [
    'LUMINAIRE_AUTHORITY_VERSION',
    'ParseVerdict',
    'ParsedPhotometricFile',
    'PhotometricArtifact',
    'PhotometricFormat',
    'PhotometricUnits',
    'incident_lux_at_point',
    'luminaire_provenance',
    'parse_eulumdat',
    'parse_ies_lm63',
    'parse_photometric',
]
