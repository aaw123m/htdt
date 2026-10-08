"""Loudspeaker manufacturer-data interchange qualification (#1074, #905).

Two standards govern off-the-shelf loudspeaker data exchange:

- **CLF (Common Loudspeaker Format)** — an interchange published by the
  CLF consortium with two distinct payload families. The *authoring*
  text format (v1: frequency/magnitude/phase polar tables; v2 adds
  structured sections) is what this module *qualifies*. The form
  manufacturers actually distribute to end users is the **binary
  ``.CF1`` / ``.CF2``** (ODEON 19 manual §3.3, CATT directivity
  support, PRONOM fmt/1944+fmt/1945): a secured binary carrying author
  identity and modification protection. HTDT has no licensed binary
  decoder and does not parse those payloads — they qualify
  ``unsupported`` with the exact reason, and only manufacturer- or
  user-legitimately-obtained tabular derivatives may proceed to
  ``cad_directivity_import``. A text fixture PASS therefore never
  implies distribution-binary compatibility;
- **GLL (Generic Loudspeaker Library)** — EASE/AFMG proprietary binary
  container. HTDT never parses ``.gll``: the rights-safe boundary is
  ``user_import_candidate`` — only manufacturer-published derivatives
  (CLF exports, spec sheets) or the user's own measurements enter the
  twin; GLL contents stay opaque to us.

Rules:

- CLF parsing is structural qualification only — it validates the
  published section layout and reports what it found; it does not
  convert to a normalized dataset (that is a ``cad_directivity_import``
  adapter's job);
- a file failing qualification is reported ``unqualified`` with the
  exact missing pieces — never partially imported;
- the GLL boundary is explicit and hashable: admission state
  ``user_import_candidate``, contents enumerated from public
  documentation, parse policy ``opaque`` — no code path ever opens a
  ``.gll`` payload.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ingress import strict_ascii_number


LOUDSPEAKER_INTERCHANGE_AUTHORITY_VERSION = (
    'loudspeaker-interchange-1'
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


ClfVersion = Literal['clf1', 'clf2']
ClfFamily = Literal['authoring_text', 'binary_cf1', 'binary_cf2', 'unknown']
ClfVerdict = Literal['qualified', 'unqualified', 'unsupported', 'not_clf']
GllParsePolicy = Literal['opaque', 'import_allowed']
InterchangeAdmissionState = Literal[
    'ready_for_admission_review',
    'user_import_candidate',
    'ineligible',
]


# --- CLF qualification ----------------------------------------------------

# Section headers published in the CLF spec (clfdata.org). CLF1 requires
# the HEADER block and FREQUENCY data; CLF2 adds structured groups.
_CLF_REQUIRED_SECTIONS_CLF1 = {
    'HEADER',
    'FREQUENCY',
}
_CLF_KNOWN_SECTIONS = {
    'HEADER',
    'FREQUENCY',
    'ROTATION',
    'POLAR',
    'BALLOON',
    'LICENSE',
    'DISTRIBUTION',
    'MEASUREMENT',
}


class ClfQualification(BaseModel):
    """Result of qualifying one CLF payload (any family)."""

    model_config = ConfigDict(frozen=True)

    verdict: ClfVerdict
    family: ClfFamily = 'unknown'
    source_sha256: str | None = None
    detected_version: ClfVersion | None = None
    sections: tuple[str, ...] = ()
    missing_sections: tuple[str, ...] = ()
    frequency_rows: int = 0
    rotation_count: int = 0
    declares_license: bool = False
    detail: str = ''


_CLF_SECTION_RE = re.compile(r'^\s*\[([A-Za-z_][A-Za-z0-9_]*)\]\s*$')
_CLF2_MARKER_RE = re.compile(r'^\s*CLF\s*format\s*version\s*[:=]\s*2', re.I)

# Binary distribution signatures (PRONOM fmt/1944 / fmt/1945, corroborated
# by the ODEON 19 manual §3.3 and CATT's CF1/CF2 support): byte 0 is 0x40
# (.CF1) or 0x41 (.CF2), followed by ``BD 0A 00 01``; the ASCII marker
# ``v{1,2}.0`` sits at offset 20 after a 15-byte gap. Signature-only
# detection — the payload itself stays opaque.
_CLF_BINARY_LEAD = re.compile(b'^[\x40\x41]\xbd\x0a\x00\x01')
_CLF_BINARY_VERSION_OFFSET = 20
_CLF_BINARY_VERSION_RE = re.compile(b'^v([12])\.0')


def detect_clf_family(source: bytes) -> ClfFamily:
    """Classify a payload into the CLF payload families.

    ``binary_cf1``/``binary_cf2`` = recognized secured distribution
    binary (signature match only — contents are never parsed).
    ``authoring_text`` = decodes as UTF-8(-SIG) text, the only family
    this module qualifies structurally. ``unknown`` = neither.
    """
    m = _CLF_BINARY_LEAD.match(source[:5])
    if m:
        v = _CLF_BINARY_VERSION_RE.match(
            source[_CLF_BINARY_VERSION_OFFSET:_CLF_BINARY_VERSION_OFFSET + 4]
        )
        if v:
            if source[0] == 0x40:
                return 'binary_cf1'
            return 'binary_cf2'
    try:
        source.decode('utf-8-sig', errors='strict')
    except UnicodeDecodeError:
        return 'unknown'
    return 'authoring_text'


def qualify_clf(source: bytes) -> ClfQualification:
    """Structurally qualify a CLF payload.

    ``qualified`` = a CLF1/CLF2 authoring-text file containing every
    mandatory section for its version plus at least one numeric polar
    row; the file's own declared version wins over heuristics.
    ``unsupported`` = a recognized .CF1/.CF2 distribution binary — HTDT
    holds no licensed decoder, so the payload is reported honestly
    instead of failing as ``not_clf``. ``not_clf`` = no CLF signature
    in either family.
    """
    family = detect_clf_family(source)
    if family in ('binary_cf1', 'binary_cf2'):
        return ClfQualification(
            verdict='unsupported',
            family=family,
            source_sha256=_sha256_bytes(source),
            detail=(
                'recognized CLF distribution binary (.%s) — secured '
                'payload HTDT does not decode; import manufacturer- or '
                'user-legitimately-obtained tabular derivatives instead'
                % family.split('_')[1].upper()
            ),
        )
    if family == 'unknown':
        return ClfQualification(
            verdict='not_clf',
            detail=(
                'not CLF — no authoring-text sections decodable as '
                'UTF-8 and no .CF1/.CF2 binary signature'
            ),
        )
    text = source.decode('utf-8-sig', errors='strict')
    lines = text.splitlines()
    sections: list[str] = []
    freq_rows = 0
    rotation_count = 0
    declares_license = False
    declared_v2 = False
    in_frequency = False
    for line in lines:
        if _CLF2_MARKER_RE.match(line):
            declared_v2 = True
        m = _CLF_SECTION_RE.match(line)
        if m:
            name = m.group(1).upper()
            sections.append(name)
            in_frequency = name == 'FREQUENCY'
            continue
        stripped = line.strip()
        if in_frequency and stripped:
            parts = re.split(r'[\s,;]+', stripped)
            try:
                # ASCII dialect only — float() would count a full-width
                # digit row ('１２０ …') as numeric, inflating coverage.
                strict_ascii_number(parts[0], field_name='frequency')
                freq_rows += 1
            except ValueError:
                pass
        if re.match(r'^\s*R\s*\(?\s*[0-9]', line):
            rotation_count += 1
        if re.match(
            r'^\s*(license|licence|distribution)\s*[:=]',
            line,
            re.I,
        ):
            declares_license = True
    if not sections:
        return ClfQualification(
            verdict='not_clf',
            family='authoring_text',
            source_sha256=_sha256_bytes(source),
            detail='no CLF [SECTION] markers found',
        )
    required = set(_CLF_REQUIRED_SECTIONS_CLF1)
    if declared_v2:
        required |= {'LICENSE'}
    missing = tuple(sorted(required - set(sections)))
    version: ClfVersion = 'clf2' if declared_v2 else 'clf1'
    qualified = not missing and freq_rows > 0
    return ClfQualification(
        verdict='qualified' if qualified else 'unqualified',
        family='authoring_text',
        source_sha256=_sha256_bytes(source),
        detected_version=version,
        sections=tuple(dict.fromkeys(sections)),
        missing_sections=missing,
        frequency_rows=freq_rows,
        rotation_count=rotation_count,
        declares_license=declares_license or 'LICENSE' in sections,
        detail=(
            'meets mandatory section set'
            if qualified
            else 'missing mandatory sections or numeric data'
        ),
    )


# --- GLL boundary -----------------------------------------------------------

class GllBoundaryPolicy(BaseModel):
    """The rights-safe boundary around proprietary .gll containers."""

    model_config = ConfigDict(frozen=True)

    format_name: Literal['gll'] = 'gll'
    parse_policy: GllParsePolicy = 'opaque'
    admission_state: InterchangeAdmissionState = 'user_import_candidate'
    allowed_derivatives: tuple[str, ...] = (
        'clf_export',
        'manufacturer_spec_sheet',
        'user_measured',
        'normalized_json',
        'polar_table',
    )
    known_contents: tuple[str, ...] = (
        'balloon_spl_magnitude_phase',
        'box_parameters',
        'crossover_configuration',
        'complex_directivity',
        'electrical_input_impedance',
        'maximum_input_voltage',
        'manufacturer_metadata',
    )
    """Contents enumerated from public EASE documentation — every item
    stays opaque to HTDT; we list them so the boundary is explicit."""
    rationale: str = (
        'GLL is an AFMG proprietary binary container. HTDT never opens '
        'it; loudspeaker evidence enters via the CLF interchange path, '
        'manufacturer-published tables, or the user\'s own measurement '
        'records — each with its own provenance kind.'
    )


GLL_BOUNDARY: GllBoundaryPolicy = GllBoundaryPolicy()


class InterchangeQualification(BaseModel):
    """A vendor-format admission decision for one file kind."""

    model_config = ConfigDict(frozen=True)

    format_name: str = Field(min_length=1)
    state: InterchangeAdmissionState
    verdict_basis: str = Field(min_length=1)


INTERCHANGE_MATRIX: tuple[InterchangeQualification, ...] = (
    InterchangeQualification(
        format_name='clf1',
        state='ready_for_admission_review',
        verdict_basis=(
            'Authoring-text interchange (clfdata.org); structural '
            'qualifier implemented in this module.'
        ),
    ),
    InterchangeQualification(
        format_name='clf2',
        state='ready_for_admission_review',
        verdict_basis=(
            'CLF v2 authoring text adds structured sections + mandatory '
            'license metadata; same qualifier covers it.'
        ),
    ),
    InterchangeQualification(
        format_name='binary_cf1',
        state='user_import_candidate',
        verdict_basis=(
            'Secured distribution binary (.CF1) — signature-detected '
            'but never decoded (no licensed decoder); only '
            'manufacturer/user tabular derivatives are import '
            'candidates.'
        ),
    ),
    InterchangeQualification(
        format_name='binary_cf2',
        state='user_import_candidate',
        verdict_basis=(
            'Secured distribution binary (.CF2) — signature-detected '
            'but never decoded (no licensed decoder); only '
            'manufacturer/user tabular derivatives are import '
            'candidates.'
        ),
    ),
    InterchangeQualification(
        format_name='gll',
        state='user_import_candidate',
        verdict_basis=(
            'Proprietary binary — opaque boundary, never parsed; '
            'see GLL_BOUNDARY.'
        ),
    ),
)


class VendorDatabasePolicy(NamedTuple):
    """Admission posture for a vendor-specific loudspeaker database."""

    vendor: str
    database: str
    state: InterchangeAdmissionState
    note: str


VENDOR_DATABASE_POLICIES: tuple[VendorDatabasePolicy, ...] = (
    VendorDatabasePolicy(
        vendor='AFMG',
        database='EASE GLL library',
        state='user_import_candidate',
        note=(
            'Proprietary container; use manufacturer-published CLF '
            'exports instead.'
        ),
    ),
    VendorDatabasePolicy(
        vendor='CLF consortium',
        database='clfdata.org distributions',
        state='ready_for_admission_review',
        note='Free text format with explicit license fields.',
    ),
    VendorDatabasePolicy(
        vendor='generic manufacturer',
        database='proprietary databases (non-CLF)',
        state='ineligible',
        note=(
            'Vendor databases without a published interchange format '
            'are out of scope until a documented export exists.'
        ),
    ),
)


__all__ = [
    'ClfFamily',
    'ClfQualification',
    'ClfVerdict',
    'ClfVersion',
    'detect_clf_family',
    'GLL_BOUNDARY',
    'GllBoundaryPolicy',
    'GllParsePolicy',
    'INTERCHANGE_MATRIX',
    'InterchangeAdmissionState',
    'InterchangeQualification',
    'LOUDSPEAKER_INTERCHANGE_AUTHORITY_VERSION',
    'VENDOR_DATABASE_POLICIES',
    'VendorDatabasePolicy',
    'qualify_clf',
]
