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

Issue #952 adds the rights layer for the binary families:
``CLF_LICENSED_FIXTURES`` seals the real manufacturer-distributed
fixtures we located during the rights investigation — each record pins
the original file's SHA256, size, manufacturer/model, and the verbatim
permission text (or its documented absence). ``evaluate_clf_payload``
turns payload bytes + a fixture record into an honest compatibility
verdict (``licensed_ok`` / ``decode_blocked`` / ``rights_unknown`` /
``no_licensed_fixture`` / ``solver_input_insufficient``) plus the #935
sufficiency-audit column states — all fail-closed, and ``declared``
surface metadata is never inferred past what the file itself states.

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
    binary_variant: ClfBinaryVariant = ''
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
# (.CF1) or 0x41 (.CF2), followed by ``BD 0A 00`` and a generation byte —
# 0x01 for the v1 binary layout and 0x02 for the v2 layout (CLF2 files
# carrying the extra v2 sections; confirmed on the CLF viewer kit's own
# ``clf2_v2_*`` samples, lead ``41 BD 0A 00 02`` + ``v2.0``). The ASCII
# marker ``v{1,2}.0`` sits at offset 20 after a 15-byte gap.
# Signature-only detection — the payload itself stays opaque.
_CLF_BINARY_LEAD = re.compile(b'^[\x40\x41]\xbd\x0a\x00([\x01\x02])')
_CLF_BINARY_VERSION_OFFSET = 20
_CLF_BINARY_VERSION_RE = re.compile(rb'^v([12])\.0')

ClfBinaryVariant = Literal['cf1_v1', 'cf2_v1', 'cf2_v2', '']


def detect_clf_binary_variant(source: bytes) -> ClfBinaryVariant:
    """Return the signature variant when the payload is a CLF binary.

    The generation byte and the offset-20 marker must agree — a
    ``BD 0A 00 02`` lead must carry ``v2.0``; anything inconsistent is
    not a recognized CLF binary. ``.CF1`` only exists in the v1 layout.
    """
    m = _CLF_BINARY_LEAD.match(source[:5])
    if not m:
        return ''
    generation = m.group(1)[0]
    v = _CLF_BINARY_VERSION_RE.match(
        source[_CLF_BINARY_VERSION_OFFSET:_CLF_BINARY_VERSION_OFFSET + 4]
    )
    if not v:
        return ''
    marker_version = int(v.group(1))
    if generation != marker_version:
        return ''
    if source[0] == 0x40:
        return 'cf1_v1' if generation == 1 else ''
    return 'cf2_v1' if generation == 1 else 'cf2_v2'


def detect_clf_family(source: bytes) -> ClfFamily:
    """Classify a payload into the CLF payload families.

    ``binary_cf1``/``binary_cf2`` = recognized secured distribution
    binary (signature match only — contents are never parsed).
    ``authoring_text`` = decodes as UTF-8(-SIG) text, the only family
    this module qualifies structurally. ``unknown`` = neither.
    """
    variant = detect_clf_binary_variant(source)
    if variant:
        return 'binary_cf1' if variant == 'cf1_v1' else 'binary_cf2'
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
        variant = detect_clf_binary_variant(source)
        detail = (
            'recognized CLF distribution binary (.%s, %s layout) — '
            'secured payload HTDT does not decode; see '
            'evaluate_clf_payload for the rights/sufficiency verdict '
            'and CLF_LICENSED_FIXTURES for pinned manufacturer fixtures'
            % (family.split('_')[1].upper(), variant)
        )
        return ClfQualification(
            verdict='unsupported',
            family=family,
            source_sha256=_sha256_bytes(source),
            binary_variant=variant,
            detail=detail,
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


# --- #952: declared-surface inspection ---------------------------------------
#
# A .CF1/.CF2 carries a small plaintext *declared* header — author name,
# model, manufacturer, measurement notes — that the free CLF viewer
# displays to every end user and that the CLF spec (§14) embeds for
# author-identity traceability. Reading that bounded surface is NOT a
# decode: the secured balloon/impedance data arrays are never touched.
# HTDT uses it only to pin provenance honestly — a download page could
# mislabel a file, but the embedded author tag is the file's own
# declaration. Everything reported is *declared*, never *verified*.

_CLF_DECLARED_SURFACE_BYTES = 4096
_CLF_DECLARED_SURFACE_MAX_STRINGS = 32
_CLF_DECLARED_SURFACE_MAX_CHARS = 256
_CLF_PRINTABLE_RUN_RE = re.compile(b'[\x20-\x7e]{4,}')


class ClfDeclaredSurface(BaseModel):
    """Bounded plaintext-declaration surface of a CLF binary."""

    model_config = ConfigDict(frozen=True)

    family: ClfFamily = 'unknown'
    binary_variant: ClfBinaryVariant = ''
    size_bytes: int = 0
    declared_strings: tuple[str, ...] = ()


def inspect_clf_declared_surface(source: bytes) -> ClfDeclaredSurface:
    """Extract the viewer-visible declared surface of a CLF binary.

    Reads at most the first ``_CLF_DECLARED_SURFACE_BYTES`` bytes —
    manufacturer files place the whole author/model/info surface there —
    and returns printable-ASCII runs as the file's own *declarations*.
    Data arrays are never parsed; for non-binary payloads the surface
    is empty.
    """
    family = detect_clf_family(source)
    if family not in ('binary_cf1', 'binary_cf2'):
        return ClfDeclaredSurface(family=family, size_bytes=len(source))
    region = source[:_CLF_DECLARED_SURFACE_BYTES]
    strings = tuple(
        s.decode('ascii')[:_CLF_DECLARED_SURFACE_MAX_CHARS]
        for s in _CLF_PRINTABLE_RUN_RE.findall(region)[
            :_CLF_DECLARED_SURFACE_MAX_STRINGS
        ]
    )
    return ClfDeclaredSurface(
        family=family,
        binary_variant=detect_clf_binary_variant(source),
        size_bytes=len(source),
        declared_strings=strings,
    )


# --- #952: licensed-fixture registry + compatibility verdict -----------------

ClfRightsGrant = Literal['granted', 'not_stated', 'not_granted']
"""Per-permission state of a fixture: ``granted`` only when affirmative
permission text exists; ``not_stated`` when nothing was found (the
honest default — absence of terms is not a grant); ``not_granted``
when terms explicitly deny the use."""

ClfFixtureSourceKind = Literal[
    'clf_group_distribution',
    'manufacturer_direct',
    'clf_group_sample_kit',
]

ClfOutcome = Literal[
    'licensed_ok',
    'decode_blocked',
    'rights_unknown',
    'no_licensed_fixture',
    'solver_input_insufficient',
    'unsupported',
    'not_clf',
]
"""#952 compatibility-verdict vocabulary for one payload evaluation:

- ``licensed_ok`` — rights established and a legitimate import path
  exists for the payload form (authoring text with a declared license,
  or a record whose import route is a permitted derivative);
- ``decode_blocked`` — rights established (licensed fixture), but no
  licensed decoder exists for the secured binary layout — the honest
  state of every real manufacturer .CF1/.CF2 today;
- ``rights_unknown`` — payload recognizable but rights could not be
  established (no manifest record, or record grants ``not_stated``);
- ``no_licensed_fixture`` — a record exists but explicitly denies use;
  also the lane-level report when no licensed fixture exists at all;
- ``solver_input_insufficient`` — rights + schema pass but the declared
  quantities cannot feed the solver input contract;
- ``unsupported`` — recognized family outside this evaluator's route;
- ``not_clf`` — no CLF signature of any family.
"""

ClfAuditColumn = Literal[
    'PASS',
    'PARTIAL',
    'UNSUPPORTED',
    'UNKNOWN',
    'BLOCKED',
]
"""#935 sufficiency-audit column states."""


class ClfSufficiencyColumns(BaseModel):
    """The #935 six-column sufficiency-audit row for one payload."""

    model_config = ConfigDict(frozen=True)

    available: ClfAuditColumn = 'UNKNOWN'
    rights_admissible: ClfAuditColumn = 'UNKNOWN'
    schema_admitted: ClfAuditColumn = 'UNKNOWN'
    solver_input_sufficient: ClfAuditColumn = 'UNKNOWN'
    reproducibly_predicted: ClfAuditColumn = 'UNKNOWN'
    physically_applicable: ClfAuditColumn = 'UNKNOWN'
    reasons: tuple[str, ...] = ()


class ClfFieldDeclaration(NamedTuple):
    """One documented CLF data-model field and HTDT's honesty state."""

    field: str
    clf_definition: str
    htdt_state: Literal[
        'documented_spec_only',
        'declared_surface_readable',
    ]
    per_file_state: Literal['not_decoded', 'declared_only', 'absent']


# Field-level honesty table — sourced from the CLF data-model spec (the
# publicly shipped CLF_spec.pdf) and the ODEON 19 manual, never inferred
# from payloads. Per-file values stay ``not_decoded``: knowing a field
# exists is not license to read it.
CLF_FIELD_DECLARATIONS: tuple[ClfFieldDeclaration, ...] = (
    ClfFieldDeclaration(
        field='coordinate_convention',
        clf_definition=(
            'spherical polar balloon on a fixed-degree azimuth/'
            'elevation grid (BALLOON-SYMMETRY none/vertical/horizontal/'
            'polar; BALLOON-ARC-ORDER default/reversed + ROTATE)'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='angular_resolution',
        clf_definition=(
            'CLF1 balloon on a 10-degree grid; CLF2 on a 5-degree grid'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='frequency_resolution',
        clf_definition=(
            'CLF1 1/1-octave band rows; CLF2 1/3-octave band rows'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='units',
        clf_definition=(
            'SI units throughout the data model (metres, volts, ohms, '
            'dB SPL re 20 µPa)'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='reference_distance',
        clf_definition=(
            'MEASUREMENT-DISTANCE record (mandatory when BALLOON-REF is '
            'absolute); balloon magnitudes are otherwise normalized '
            'relative SPL'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='absolute_spl_vs_sensitivity',
        clf_definition=(
            'BALLOON-REF selects absolute SPL, relative level, or '
            'arbitrary scaling; SENSITIVITY band block carries the '
            'on-axis sensitivity reference'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='phase_presence',
        clf_definition=(
            'no phase rows in CLF1 or CLF2 v1; CLF2 v2 optionally adds '
            'phase, filter, and multipart sections'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='bandwidth_validity',
        clf_definition=(
            'MINBAND..MAXBAND window bounds the declared band range '
            '(CLF2 MINBAND ≥ 50 Hz)'
        ),
        htdt_state='documented_spec_only',
        per_file_state='not_decoded',
    ),
    ClfFieldDeclaration(
        field='origin_authorship',
        clf_definition=(
            'embedded author identity + license type + modification '
            'checksums (spec §14); the same surface the free viewer '
            'displays'
        ),
        htdt_state='declared_surface_readable',
        per_file_state='declared_only',
    ),
)


class ClfFixtureRecord(BaseModel):
    """Sealed rights/provenance record for one real fixture (#952).

    The bytes are NEVER vendored — redistribution is not granted by any
    source we found, so evidence is the SHA256 + size + verbatim
    permission context. ``verify_clf_fixture`` re-checks a supplied
    payload against the pin.
    """

    model_config = ConfigDict(frozen=True)

    fixture_id: str = Field(pattern=r'^clfx-[0-9a-z-]+$')
    file_name: str = Field(min_length=1)
    family: Literal['binary_cf1', 'binary_cf2']
    binary_variant: Literal['cf1_v1', 'cf2_v1', 'cf2_v2']
    content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=1)
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    source_kind: ClfFixtureSourceKind
    source_url: str = Field(min_length=1)
    source_page: str = Field(min_length=1)
    licence_verbatim: str = Field(min_length=1)
    use_grant: ClfRightsGrant
    analysis_grant: ClfRightsGrant
    redistribution_grant: ClfRightsGrant
    import_route: Literal['binary_decode', 'tabular_derivative', 'none']
    declared_quantities: tuple[str, ...] = ()
    notes: str = ''


# The #952 rights investigation (docs/issues/issue-952-clf-binary-import.md)
# found real manufacturer-distributed .CF1/.CF2 files and pinned them here.
# No explicit redistribution grant exists anywhere, so all bytes stay
# external; the verbatim permission context is recorded per record.
_CLF_GROUP_FAQ_VERBATIM = (
    'End users can use the free CLF viewer to view the contents of a CLF '
    'file. To use the data for a design you need software that can '
    'import the binary CLF format data (CF1 and CF2).'
)
_GENELEC_PAGE_VERBATIM = (
    'Speakers data are available on for GLL, EASE 3, EASE 4 and CLF '
    'softwares.'
)

CLF_LICENSED_FIXTURES: tuple[ClfFixtureRecord, ...] = (
    ClfFixtureRecord(
        fixture_id='clfx-ces-cls3100-cf1',
        file_name='cls-3100.CF1',
        family='binary_cf1',
        binary_variant='cf1_v1',
        content_sha256=(
            'a73f232de176e7a59bfa3ca2899e97045bb68d2f920a563fa111eac45cece85b'
        ),
        size_bytes=34476,
        manufacturer='CES Audio',
        model='CLS-3100',
        source_kind='clf_group_distribution',
        source_url='http://clfgroup.org/files/dls/181/cls-3100.CF1',
        source_page='http://clfgroup.org/files/index.php',
        licence_verbatim=_CLF_GROUP_FAQ_VERBATIM,
        use_grant='granted',
        analysis_grant='granted',
        redistribution_grant='not_stated',
        import_route='binary_decode',
        declared_quantities=(
            'balloon_spl_magnitude_1oct_10deg',
            'sensitivity_band',
            'impedance_band',
            'measurement_distance',
            'author_metadata',
        ),
        notes=(
            'CLF Group manufacturer file section; payload declared as '
            '"2-way Loudspeaker, Free-field, Anechoic to 40ms, '
            'Normalized to 1 meter". No license text on the listing '
            'itself — grant derives from the consortium FAQ purpose '
            'statement; redistribution never addressed.'
        ),
    ),
    ClfFixtureRecord(
        fixture_id='clfx-ces-cls3100-cf2',
        file_name='cls-3100.CF2',
        family='binary_cf2',
        binary_variant='cf2_v1',
        content_sha256=(
            '30490c90f3a6d503bac2768ee32e4d631214785985bb54d3a87da218ef15b489'
        ),
        size_bytes=334716,
        manufacturer='CES Audio',
        model='CLS-3100',
        source_kind='clf_group_distribution',
        source_url='http://clfgroup.org/files/dls/182/cls-3100.CF2',
        source_page='http://clfgroup.org/files/index.php',
        licence_verbatim=_CLF_GROUP_FAQ_VERBATIM,
        use_grant='granted',
        analysis_grant='granted',
        redistribution_grant='not_stated',
        import_route='binary_decode',
        declared_quantities=(
            'balloon_spl_magnitude_1third_oct_5deg',
            'sensitivity_band',
            'impedance_band',
            'measurement_distance',
            'author_metadata',
        ),
        notes=(
            'CLF2 v1 layout (no phase rows per spec). Same declared '
            'measurement provenance as the .CF1 twin.'
        ),
    ),
    ClfFixtureRecord(
        fixture_id='clfx-genelec-4020a-cf2',
        file_name='Genelec Oy-4020A.CF2',
        family='binary_cf2',
        binary_variant='cf2_v1',
        content_sha256=(
            '820ea8d787b01498626c9c64c5252b4ad319f918982c3e0b94703170081dfef5'
        ),
        size_bytes=334040,
        manufacturer='Genelec',
        model='4020A',
        source_kind='manufacturer_direct',
        source_url=(
            'https://assets.ctfassets.net/4zjnzn055a4v/1fpUoMVTmsmIPlrahHG1G3/'
            '09922a50379b63a078f82cd3dc829e1d/genelec_clf_4020a.cf2.zip'
        ),
        source_page='https://www.genelec.com/simulation-files',
        licence_verbatim=_GENELEC_PAGE_VERBATIM,
        use_grant='granted',
        analysis_grant='granted',
        redistribution_grant='not_stated',
        import_route='binary_decode',
        declared_quantities=(
            'balloon_spl_magnitude_1third_oct_5deg',
            'sensitivity_band',
            'impedance_band',
            'measurement_distance',
            'author_metadata',
        ),
        notes=(
            'Manufacturer-hosted .CF2 inside a .zip. Page grants use in '
            'CLF-class simulation software; no terms-of-use or '
            'redistribution text was found on genelec.com (the '
            'terms-of-use URL 404s).'
        ),
    ),
    ClfFixtureRecord(
        fixture_id='clfx-clfgroup-v2-sample-cf2',
        file_name='clf2_v2_passive_broadband_nophase_nofilter.CF2',
        family='binary_cf2',
        binary_variant='cf2_v2',
        content_sha256=(
            '87c4742f0fd465e7b15a3d8d375fc41ae6e52493c99e62db2cc801ae56fb7309'
        ),
        size_bytes=334572,
        manufacturer='CLF Group',
        model='Speaker Maker Inc reference sample (v2)',
        source_kind='clf_group_sample_kit',
        source_url='http://clfgroup.org/CLF_Viewer_v2.1b.exe',
        source_page='http://clfgroup.org/download.php',
        licence_verbatim=_CLF_GROUP_FAQ_VERBATIM,
        use_grant='granted',
        analysis_grant='granted',
        redistribution_grant='not_stated',
        import_route='binary_decode',
        declared_quantities=(
            'balloon_spl_magnitude_1third_oct_5deg',
            'sensitivity_band',
            'impedance_band',
            'measurement_distance',
            'author_metadata',
        ),
        notes=(
            'Ships inside the free CLF viewer installer — the only '
            'public v2-generation (.02 signature, v2.0 marker) sample '
            'located. Fictional "Speaker Maker Inc" author.'
        ),
    ),
)


def clf_fixture_for_sha256(sha256: str) -> ClfFixtureRecord | None:
    """Return the licensed-fixture record matching *sha256*, if any."""
    for record in CLF_LICENSED_FIXTURES:
        if record.content_sha256 == sha256:
            return record
    return None


def verify_clf_fixture(
    record: ClfFixtureRecord, source: bytes
) -> Literal['matches', 'mismatch']:
    """Re-check a payload against a sealed fixture pin.

    ``matches`` requires identical SHA256, size, AND a signature variant
    consistent with the record — hash collision alone is not enough.
    """
    if len(source) != record.size_bytes:
        return 'mismatch'
    if _sha256_bytes(source) != record.content_sha256:
        return 'mismatch'
    if detect_clf_binary_variant(source) != record.binary_variant:
        return 'mismatch'
    return 'matches'


class ClfImportEvaluation(BaseModel):
    """One payload's rights + compatibility verdict (#952)."""

    model_config = ConfigDict(frozen=True)

    outcome: ClfOutcome
    family: ClfFamily = 'unknown'
    binary_variant: ClfBinaryVariant = ''
    source_sha256: str | None = None
    fixture_id: str | None = None
    fixture_match: Literal['matches', 'mismatch', 'not_applicable'] = (
        'not_applicable'
    )
    columns: ClfSufficiencyColumns = ClfSufficiencyColumns()
    declared_strings: tuple[str, ...] = ()
    detail: str = ''


def _rights_state(
    record: ClfFixtureRecord | None,
) -> Literal['licensed', 'unknown', 'denied']:
    if record is None:
        return 'unknown'
    if 'not_granted' in (record.use_grant, record.analysis_grant):
        return 'denied'
    if 'not_stated' in (record.use_grant, record.analysis_grant):
        return 'unknown'
    return 'licensed'


def evaluate_clf_payload(
    source: bytes, fixture: ClfFixtureRecord | None = None
) -> ClfImportEvaluation:
    """Evaluate one payload through the rights + sufficiency pipeline.

    Fail-closed at every column: a payload without established rights is
    never decoded-adjacent; a licensed binary is still ``decode_blocked``
    because no licensed decoder exists. ``fixture`` pins the expected
    record — a supplied payload that does not match the pin is reported
    as tampered/drifted evidence, not as the fixture.
    """
    qualification = qualify_clf(source)
    sha = _sha256_bytes(source)
    family = qualification.family
    variant = qualification.binary_variant
    if family == 'unknown':
        return ClfImportEvaluation(
            outcome='not_clf',
            family='unknown',
            source_sha256=sha,
            columns=ClfSufficiencyColumns(
                available='BLOCKED',
                reasons=('available=BLOCKED: no CLF signature',),
            ),
            detail='no CLF signature of any family',
        )
    if family == 'authoring_text':
        licensed = qualification.declares_license
        qualified = qualification.verdict == 'qualified'
        if qualified and licensed:
            outcome: ClfOutcome = 'licensed_ok'
            solver: ClfAuditColumn = 'PARTIAL'
            schema: ClfAuditColumn = 'PASS'
        elif qualified:
            outcome = 'rights_unknown'
            solver = 'UNKNOWN'
            schema = 'PASS'
        elif licensed:
            outcome = 'solver_input_insufficient'
            solver = 'BLOCKED'
            schema = 'BLOCKED'
        else:
            outcome = 'rights_unknown'
            solver = 'UNKNOWN'
            schema = 'BLOCKED'
        return ClfImportEvaluation(
            outcome=outcome,
            family=family,
            source_sha256=sha,
            columns=ClfSufficiencyColumns(
                available='PASS',
                rights_admissible=(
                    'PASS' if licensed else 'UNKNOWN'
                ),
                schema_admitted=schema,
                solver_input_sufficient=solver,
                reproducibly_predicted=(
                    'UNKNOWN' if qualified else 'BLOCKED'
                ),
                physically_applicable='UNKNOWN',
                reasons=(
                    'rights_admissible=%s: %s'
                    % (
                        'PASS' if licensed else 'UNKNOWN',
                        'declared LICENSE section'
                        if licensed
                        else 'no license declaration in file',
                    ),
                    'schema_admitted=%s: %s'
                    % (
                        schema,
                        'authoring-text qualifier PASS'
                        if qualified
                        else 'missing mandatory sections or numeric data',
                    ),
                ),
            ),
            detail=qualification.detail,
        )
    # binary family — the rights lane
    surface = inspect_clf_declared_surface(source)
    record = fixture or clf_fixture_for_sha256(sha)
    fixture_match: Literal['matches', 'mismatch', 'not_applicable']
    if record is not None:
        fixture_match = verify_clf_fixture(record, source)
    else:
        fixture_match = 'not_applicable'
    if record is not None and fixture_match == 'mismatch':
        # Hash/variant drift on a pinned fixture is tampering evidence —
        # never treated as the fixture itself.
        return ClfImportEvaluation(
            outcome='rights_unknown',
            family=family,
            binary_variant=variant,
            source_sha256=sha,
            fixture_id=record.fixture_id,
            fixture_match='mismatch',
            columns=ClfSufficiencyColumns(
                available='PASS',
                rights_admissible='UNKNOWN',
                schema_admitted='BLOCKED',
                reasons=(
                    'rights_admissible=UNKNOWN: payload does not match '
                    'the pinned fixture (sha/size/variant drift) — '
                    'possible tampering, rights of THIS payload unknown',
                ),
            ),
            declared_strings=surface.declared_strings,
            detail=(
                'payload drifted from sealed fixture %s' % record.fixture_id
            ),
        )
    rights = _rights_state(record)
    if rights == 'denied':
        return ClfImportEvaluation(
            outcome='no_licensed_fixture',
            family=family,
            binary_variant=variant,
            source_sha256=sha,
            fixture_id=record.fixture_id if record else None,
            fixture_match=fixture_match,
            columns=ClfSufficiencyColumns(
                available='PASS',
                rights_admissible='BLOCKED',
                schema_admitted='BLOCKED',
                reasons=(
                    'rights_admissible=BLOCKED: fixture terms deny '
                    'use/analysis',
                ),
            ),
            declared_strings=surface.declared_strings,
            detail='fixture record denies use — do not decode',
        )
    if rights == 'unknown':
        return ClfImportEvaluation(
            outcome='rights_unknown',
            family=family,
            binary_variant=variant,
            source_sha256=sha,
            fixture_id=record.fixture_id if record else None,
            fixture_match=fixture_match,
            columns=ClfSufficiencyColumns(
                available='PASS',
                rights_admissible='UNKNOWN',
                schema_admitted='BLOCKED',
                reasons=(
                    'rights_admissible=UNKNOWN: no licensed-fixture '
                    'record matches this payload, or its grants are '
                    'not_stated',
                ),
            ),
            declared_strings=surface.declared_strings,
            detail=(
                'recognized %s binary; no licensed-fixture record '
                'establishes rights for this payload' % variant
            ),
        )
    # licensed fixture, but no licensed decoder exists — the honest
    # real-fixture state. solver_input_sufficient stays UNKNOWN because
    # per-file values were never decoded (declared quantities only).
    redistribution_reason = (
        'redistribution %s — bytes stay external, evidence is '
        'sha256+size' % record.redistribution_grant
    )
    return ClfImportEvaluation(
        outcome='decode_blocked',
        family=family,
        binary_variant=variant,
        source_sha256=sha,
        fixture_id=record.fixture_id,
        fixture_match=fixture_match,
        columns=ClfSufficiencyColumns(
            available='PASS',
            rights_admissible='PARTIAL',
            schema_admitted='UNSUPPORTED',
            solver_input_sufficient='UNKNOWN',
            reproducibly_predicted='BLOCKED',
            physically_applicable='UNKNOWN',
            reasons=(
                'rights_admissible=PARTIAL: use/analysis granted via '
                'verbatim license context; ' + redistribution_reason,
                'schema_admitted=UNSUPPORTED: no licensed decoder for '
                'the secured %s layout (binary spec is by-request '
                'per CLF spec §14)' % variant,
                'solver_input_sufficient=UNKNOWN: declared quantities '
                '%s, per-file values never decoded'
                % (', '.join(record.declared_quantities),),
            ),
        ),
        declared_strings=surface.declared_strings,
        detail=(
            'licensed fixture %s (%s %s); decode_blocked — the binary '
            'spec is by-request only and no licensed decoder was found '
            '(CFxLib absent; viewer exports the tabular derivative '
            'route instead)'
            % (record.fixture_id, record.manufacturer, record.model)
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
            '(v1 layout); licensed fixtures pinned in '
            'CLF_LICENSED_FIXTURES but evaluate_clf_payload reports '
            'decode_blocked — only manufacturer/user tabular '
            'derivatives are import candidates.'
        ),
    ),
    InterchangeQualification(
        format_name='binary_cf2',
        state='user_import_candidate',
        verdict_basis=(
            'Secured distribution binary (.CF2) — signature-detected '
            '(v1 + v2 layouts); licensed fixtures pinned in '
            'CLF_LICENSED_FIXTURES but evaluate_clf_payload reports '
            'decode_blocked — only manufacturer/user tabular '
            'derivatives are import candidates.'
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
    'CLF_FIELD_DECLARATIONS',
    'CLF_LICENSED_FIXTURES',
    'ClfAuditColumn',
    'ClfBinaryVariant',
    'ClfDeclaredSurface',
    'ClfFamily',
    'ClfFieldDeclaration',
    'ClfFixtureRecord',
    'ClfFixtureSourceKind',
    'ClfImportEvaluation',
    'ClfOutcome',
    'ClfQualification',
    'ClfRightsGrant',
    'ClfSufficiencyColumns',
    'ClfVerdict',
    'ClfVersion',
    'clf_fixture_for_sha256',
    'detect_clf_binary_variant',
    'detect_clf_family',
    'evaluate_clf_payload',
    'GLL_BOUNDARY',
    'GllBoundaryPolicy',
    'GllParsePolicy',
    'inspect_clf_declared_surface',
    'INTERCHANGE_MATRIX',
    'InterchangeAdmissionState',
    'InterchangeQualification',
    'LOUDSPEAKER_INTERCHANGE_AUTHORITY_VERSION',
    'VENDOR_DATABASE_POLICIES',
    'VendorDatabasePolicy',
    'qualify_clf',
    'verify_clf_fixture',
]
