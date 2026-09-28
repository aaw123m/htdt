"""#1063 — video meter-correction interoperability (CCMX/CCSS).

Imports Argyll/DisplayCAL ``.ccmx`` (colorimeter correction matrix) and
``.ccss`` (colorimeter calibration spectral set) files as immutable,
provenance-pinned artifacts, evaluates their compatibility with a specific
colorimeter + display pairing, and binds them to the #647
``ColorimeterCorrectionProfile`` shell before any color evaluation consumes
them.

Contract carried by this module:

- the original file bytes are preserved verbatim (SHA-256 recorded; the raw
  CGATS keyword table keeps every field, including ones this importer does
  not understand);
- parsing is an independent CGATS.17-style text parser — no dependency on
  Argyll, DisplayCAL, or the corrections web database;
- a correction is a *meter correction* (device-level), never a display LUT —
  it is never merged into the meter identity and never stacked: a
  ``VideoColorMeasurementSet`` holds exactly one ``meter_correction``;
- compatibility evaluation is fail-closed: a correction whose target
  instrument or display does not match reports ``incompatible``, and only
  ``ready``/``limited`` artifacts may be bound;
- the DisplayCAL online corrections database is treated as a *discovery*
  channel only — a file that was discovered there is imported by its own
  bytes and hash, so the provenance chain names the artifact, not the site.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_colorimetry import ColorimeterCorrectionProfile
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash


METER_CORRECTION_PARSER_ID = 'htdt-cgats-ccxx-1'

MeterCorrectionKind = Literal['ccmx_matrix', 'ccss_spectral']

CorrectionReadiness = Literal['ready', 'limited', 'incompatible']

CorrectionApplication = Literal['external', 'htdt_offline', 'unknown']


class CgatsParseError(ValueError):
    """Raised when the text does not satisfy the CGATS.17-style grammar."""






# ---------------------------------------------------------------------------
# Minimal CGATS.17-style lexer/parser (Argyll-flavoured CGATS subset).
# ---------------------------------------------------------------------------

_KEYWORD_RE = re.compile(r'^[A-Za-z0-9_\-\.]+$')


def _unquote(token: str) -> str:
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return token


def _line_tokens(line: str) -> list[str]:
    """Split one logical line into whitespace/quoted tokens."""
    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        if line[i] == '"':
            j = line.find('"', i + 1)
            if j < 0:
                raise CgatsParseError('unterminated quoted string')
            tokens.append(line[i : j + 1])
            i = j + 1
            continue
        j = i
        while j < n and not line[j].isspace():
            j += 1
        tokens.append(line[i:j])
        i = j
    return tokens


class CgatsDocument(BaseModel):
    """Parsed representation of one CGATS.17-style text file.

    ``keywords`` preserves every KEY VALUE pair in file order, including
    keywords this importer does not interpret.
    """

    model_config = ConfigDict(frozen=True)

    format_id: str = Field(min_length=1)
    keywords: tuple[tuple[str, str], ...] = ()
    data_format: tuple[str, ...] = ()
    data_rows: tuple[tuple[str, ...], ...] = ()
    number_of_fields: int = Field(ge=0)
    number_of_sets: int = Field(ge=0)


def parse_cgats_document(text: str) -> CgatsDocument:
    """Parse a CGATS/CCMX/CCSS text file. Fail-closed on malformed input."""
    # Normalise newlines; CGATS files may arrive CRLF or LF.
    raw_lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    lines = [ln.strip() for ln in raw_lines]
    lines = [ln for ln in lines if ln]

    if not lines:
        raise CgatsParseError('empty document')

    format_id = lines[0].split()[0]
    if not _KEYWORD_RE.match(format_id):
        raise CgatsParseError(f'invalid format id {format_id!r}')

    keywords: list[tuple[str, str]] = []
    data_format: list[str] = []
    data_rows: list[tuple[str, ...]] = []
    number_of_fields = 0
    number_of_sets = 0

    state = 'header'
    i = 1
    while i < len(lines):
        line = lines[i]
        upper = line.upper()
        if upper == 'BEGIN_DATA_FORMAT':
            state = 'format'
            i += 1
            continue
        if upper == 'END_DATA_FORMAT':
            if state != 'format':
                raise CgatsParseError('END_DATA_FORMAT without BEGIN')
            state = 'header'
            i += 1
            continue
        if upper == 'BEGIN_DATA':
            state = 'data'
            i += 1
            continue
        if upper == 'END_DATA':
            if state != 'data':
                raise CgatsParseError('END_DATA without BEGIN')
            state = 'header'
            i += 1
            continue

        if state == 'format':
            data_format.extend(_line_tokens(line))
            i += 1
            continue
        if state == 'data':
            data_rows.append(tuple(_line_tokens(line)))
            i += 1
            continue

        # header keywords: KEY VALUE (value may be quoted or bare)
        tokens = _line_tokens(line)
        if len(tokens) >= 2:
            key = tokens[0]
            value = ' '.join(_unquote(t) for t in tokens[1:])
            if key == 'NUMBER_OF_FIELDS':
                number_of_fields = int(value)
            elif key == 'NUMBER_OF_SETS':
                number_of_sets = int(value)
            else:
                keywords.append((key, value))
            i += 1
            continue
        # single bare keyword line — keep it for the raw table
        keywords.append((tokens[0], ''))
        i += 1

    if state != 'header':
        raise CgatsParseError(f'unterminated {state} block')
    if number_of_fields and len(data_format) != number_of_fields:
        raise CgatsParseError(
            f'NUMBER_OF_FIELDS={number_of_fields} but data format has '
            f'{len(data_format)} fields'
        )
    if number_of_sets and len(data_rows) != number_of_sets:
        raise CgatsParseError(
            f'NUMBER_OF_SETS={number_of_sets} but data has '
            f'{len(data_rows)} rows'
        )
    for row in data_rows:
        if data_format and len(row) != len(data_format):
            raise CgatsParseError(
                f'data row has {len(row)} fields, expected '
                f'{len(data_format)}'
            )

    return CgatsDocument(
        format_id=format_id,
        keywords=tuple(keywords),
        data_format=tuple(data_format),
        data_rows=tuple(data_rows),
        number_of_fields=number_of_fields,
        number_of_sets=number_of_sets,
    )


def _keyword_table(doc: CgatsDocument) -> dict[str, str]:
    table: dict[str, str] = {}
    for key, value in doc.keywords:
        # keep the LAST occurrence; CGATS files may repeat keywords
        table[key] = value
    return table


def _float_rows(doc: CgatsDocument) -> tuple[tuple[float, ...], ...]:
    rows: list[tuple[float, ...]] = []
    for row in doc.data_rows:
        parsed: list[float] = []
        for cell in row:
            try:
                parsed.append(float(cell))
            except ValueError:
                raise CgatsParseError(
                    f'non-numeric data cell {cell!r}'
                ) from None
        rows.append(tuple(parsed))
    return tuple(rows)


# ---------------------------------------------------------------------------
# Typed artifact
# ---------------------------------------------------------------------------


class MeterCorrectionArtifact(BaseModel):
    """An immutable, hash-pinned meter-correction artifact (CCMX or CCSS)."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['meter-correction-1'] = 'meter-correction-1'
    artifact_id: str = Field(min_length=1)
    source_file_name: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_bytes: int = Field(gt=0)
    parser_id: str = Field(min_length=1)
    correction_kind: MeterCorrectionKind
    format_id: str = Field(min_length=1)
    descriptor: str | None = None
    originator: str | None = None
    created: str | None = None
    instrument: str | None = None
    reference_instrument: str | None = None
    display: str | None = None
    technology: str | None = None
    observer: str | None = None
    manufacturer: str | None = None
    matrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ] | None = None
    spectral_start_nm: float | None = None
    spectral_end_nm: float | None = None
    spectral_bands: int | None = Field(default=None, gt=0)
    spectral_samples: tuple[tuple[float, ...], ...] | None = None
    keyword_table: dict[str, str] = Field(default_factory=dict)
    data_format_fields: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'MeterCorrectionArtifact':
        if self.correction_kind == 'ccmx_matrix':
            if self.matrix is None:
                raise ValueError('ccmx artifact requires the 3x3 matrix')
        else:
            if self.spectral_samples is None:
                raise ValueError(
                    'ccss artifact requires spectral samples'
                )
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'meter correction artifact semantic hash mismatch'
            )
        return self


def _matrix_3x3(rows: tuple[tuple[float, ...], ...]):
    if len(rows) != 3 or any(len(r) != 3 for r in rows):
        raise CgatsParseError('ccmx data must be a 3x3 matrix')
    return (
        (rows[0][0], rows[0][1], rows[0][2]),
        (rows[1][0], rows[1][1], rows[1][2]),
        (rows[2][0], rows[2][1], rows[2][2]),
    )


def import_meter_correction(
    *,
    file_name: str,
    data: bytes,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> MeterCorrectionArtifact:
    """Import one .ccmx/.ccss file as an immutable artifact.

    ``data`` is the original file bytes; they are hashed and preserved so
    the artifact can be re-derived and re-checked independently of this
    parser. Non-numeric or malformed content raises ``CgatsParseError``.
    """
    source_sha256 = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise CgatsParseError(
            f'file is not UTF-8 text: {exc.reason}'
        ) from exc
    doc = parse_cgats_document(text)
    table = _keyword_table(doc)
    kind: MeterCorrectionKind
    matrix = None
    spectral_samples: tuple[tuple[float, ...], ...] | None = None
    spectral_start = spectral_end = None
    spectral_bands = None

    if doc.format_id == 'CCMX':
        kind = 'ccmx_matrix'
        matrix = _matrix_3x3(_float_rows(doc))
    elif doc.format_id == 'CCSS':
        kind = 'ccss_spectral'
        spectral_samples = _float_rows(doc)
        for key, attr in (
            ('SPECTRAL_START_NM', 'start'),
            ('SPECTRAL_END_NM', 'end'),
        ):
            raw = table.get(key)
            if raw is not None:
                try:
                    value = float(raw)
                except ValueError:
                    raise CgatsParseError(
                        f'{key} is not numeric: {raw!r}'
                    ) from None
                if attr == 'start':
                    spectral_start = value
                else:
                    spectral_end = value
        raw_bands = table.get('SPECTRAL_BANDS')
        if raw_bands is not None:
            try:
                spectral_bands = int(float(raw_bands))
            except ValueError:
                raise CgatsParseError(
                    f'SPECTRAL_BANDS is not an integer: {raw_bands!r}'
                ) from None
    else:
        raise CgatsParseError(
            f'unsupported meter-correction format {doc.format_id!r} '
            '(expected CCMX or CCSS)'
        )

    artifact_id = f'meter-correction/{source_sha256[:16]}'
    probe = MeterCorrectionArtifact.model_construct(
        schema_version=1,
        authority_version='meter-correction-1',
        artifact_id=artifact_id,
        source_file_name=file_name,
        source_sha256=source_sha256,
        source_bytes=len(data),
        parser_id=METER_CORRECTION_PARSER_ID,
        correction_kind=kind,
        format_id=doc.format_id,
        descriptor=table.get('DESCRIPTOR'),
        originator=table.get('ORIGINATOR'),
        created=table.get('CREATED'),
        instrument=table.get('INSTRUMENT'),
        reference_instrument=table.get('REFERENCE'),
        display=table.get('DISPLAY'),
        technology=table.get('TECHNOLOGY'),
        observer=table.get('OBSERVER'),
        manufacturer=table.get('MANUFACTURER'),
        matrix=matrix,
        spectral_start_nm=spectral_start,
        spectral_end_nm=spectral_end,
        spectral_bands=spectral_bands,
        spectral_samples=spectral_samples,
        keyword_table=table,
        data_format_fields=doc.data_format,
        provenance=tuple(provenance),
        semantic_sha256='',
    )
    return MeterCorrectionArtifact(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


# ---------------------------------------------------------------------------
# Compatibility evaluation
# ---------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


def _matches(needle: str | None, haystack: str | None) -> bool | None:
    """Return True/False for a claimed match, None when unknowable."""
    if not needle or not haystack:
        return None
    n, h = _norm(needle), _norm(haystack)
    if not n or not h:
        return None
    return n in h or h in n


class CorrectionCompatibility(BaseModel):
    """Readiness verdict for one artifact against one intended pairing."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    authority_version: Literal['correction-compatibility-1'] = (
        'correction-compatibility-1'
    )
    artifact_id: str = Field(min_length=1)
    readiness: CorrectionReadiness
    reasons: tuple[str, ...] = ()
    semantic_sha256: str = Field(min_length=16)

    def semantic_payload(self) -> dict:
        return self.model_dump(
            mode='python', exclude={'semantic_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'CorrectionCompatibility':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError(
                'correction compatibility semantic hash mismatch'
            )
        return self


def evaluate_correction_compatibility(
    artifact: MeterCorrectionArtifact,
    *,
    target_meter: str,
    display: str | None = None,
    display_technology: str | None = None,
    meter_supports_spectral: bool = False,
) -> CorrectionCompatibility:
    """Decide whether ``artifact`` may correct ``target_meter`` on a display.

    ``ready`` — the artifact names this instrument and this display (or the
    correction is display-technology generic). ``limited`` — the artifact is
    instrument-correct but display identity is unverifiable or only loosely
    matches (e.g. technology matches but the exact display name does not).
    ``incompatible`` — wrong instrument family, a display/technology the
    artifact explicitly contradicts, or a spectral correction offered to a
    colorimeter that cannot consume CCSS data.
    """
    reasons: list[str] = []
    readiness: CorrectionReadiness = 'ready'

    meter_match = _matches(target_meter, artifact.instrument)
    if meter_match is False:
        readiness = 'incompatible'
        reasons.append(
            f'correction targets instrument {artifact.instrument!r}, '
            f'not {target_meter!r}'
        )
    elif meter_match is None and artifact.correction_kind == 'ccmx_matrix':
        # A CCMX names its target instrument; one that doesn't can't be
        # confirmed for this meter. CCSS is meter-generic by design — it
        # describes the display's spectra, not a specific instrument.
        readiness = 'limited'
        reasons.append('artifact does not name a target instrument')

    if artifact.correction_kind == 'ccss_spectral':
        if not meter_supports_spectral:
            readiness = 'incompatible'
            reasons.append(
                'target meter cannot consume spectral correction sets '
                '(CCSS is for instruments with known sensor curves)'
            )
        if display_technology is not None:
            tech_match = _matches(
                display_technology, artifact.technology
            )
            if tech_match is False:
                readiness = 'incompatible'
                reasons.append(
                    f'display technology {display_technology!r} '
                    f'contradicts artifact technology '
                    f'{artifact.technology!r}'
                )
            elif tech_match is None and readiness == 'ready':
                readiness = 'limited'
                reasons.append(
                    'artifact does not name a display technology'
                )

    if display is not None:
        display_match = _matches(display, artifact.display)
        if display_match is False:
            if readiness == 'ready':
                readiness = 'limited'
            reasons.append(
                f'display {display!r} does not match artifact display '
                f'{artifact.display!r}'
            )
        elif display_match is None and readiness == 'ready':
            readiness = 'limited'
            reasons.append('artifact does not name a display')

    if artifact.format_id not in ('CCMX', 'CCSS'):
        readiness = 'incompatible'
        reasons.append(
            f'unrecognised format {artifact.format_id!r}'
        )

    probe = CorrectionCompatibility.model_construct(
        schema_version=1,
        authority_version='correction-compatibility-1',
        artifact_id=artifact.artifact_id,
        readiness=readiness,
        reasons=tuple(reasons),
        semantic_sha256='',
    )
    return CorrectionCompatibility(
        **probe.model_dump(mode='python', exclude={'semantic_sha256'}),
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def correction_profile_for_binding(
    artifact: MeterCorrectionArtifact,
    compatibility: CorrectionCompatibility,
    *,
    application: CorrectionApplication,
    note: str | None = None,
    version: str = '1',
) -> ColorimeterCorrectionProfile:
    """Produce the #647 profile object for a single-correction binding.

    ``application`` records where the correction is applied: ``external``
    (applied outside HTDT by the measurement software), ``htdt_offline``
    (HTDT re-corrects raw meter data), or ``unknown``. The profile is a
    meter correction only — never a display LUT and never stacked with a
    second correction (a measurement set holds exactly one).
    """
    if compatibility.readiness == 'incompatible':
        raise ValueError(
            'cannot bind an incompatible meter correction '
            f'({"; ".join(compatibility.reasons) or "no reason recorded"})'
        )
    applies_to = artifact.technology or artifact.display
    return ColorimeterCorrectionProfile(
        correction_id=artifact.artifact_id,
        version=version,
        base_meter=artifact.instrument or 'unknown',
        correction_kind=(
            f'{artifact.correction_kind};application={application}'
        ),
        applies_to_display_class=applies_to,
        source_reference=(
            f'{artifact.source_file_name}'
            + (f' ({artifact.originator})' if artifact.originator else '')
        ),
        source_sha256=artifact.source_sha256,
        provenance=artifact.provenance,
    )


__all__ = [
    'CgatsDocument',
    'CgatsParseError',
    'CorrectionCompatibility',
    'CorrectionApplication',
    'CorrectionReadiness',
    'METER_CORRECTION_PARSER_ID',
    'MeterCorrectionArtifact',
    'MeterCorrectionKind',
    'correction_profile_for_binding',
    'evaluate_correction_compatibility',
    'import_meter_correction',
    'parse_cgats_document',
]
