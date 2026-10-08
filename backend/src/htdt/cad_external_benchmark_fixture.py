"""External measured-fixture admission + deterministic replay harness (#948).

This module is the common foundation issue #939's BRAS RS8 execution calls:

- ``ExternalBenchmarkFixture`` — a sealed admission record binding license,
  attribution, redistribution permission, origin URI, payload checksums,
  scene configuration, unit semantics, coordinate convention, source and
  receiver points, and boundary-material references to the #836 corpus
  authority (``cad_external_corpus_manifest``) on top of the #834 admission
  ledger (``cad_external_admission``).
- ``verify_fixture_payloads`` — fail-closed verification of every consumed
  payload byte against the pinned archive checksums (and optionally deeper
  per-member pins for files inside zip archives).
- ``read_sofa_measurement`` — a lazy-h5py reader for the licensed BRAS
  SOFA ``SingleRoomSRIR`` payloads that imports measured RIRs (Pa), the
  observation axes (emitter/receiver grids), the valid band, and the
  reference/normalization semantics *verbatim* — unlicensed or
  semantically ambiguous payloads are never silently admitted.
- ``FixtureRunSpec`` / ``run_fixture`` / ``replay_fixture`` — a deterministic
  replay harness pinning solver/provider identity + revision, mesh
  resolution, seed, runtime descriptor, evaluation profile and run mode
  (``preregistered_unfitted`` vs ``informed_calibrated``), issuing sealed,
  immutable ``FixtureRunEvidence`` verdicts.

Honesty rules (fail closed, never conflate):

- ``pascal_calibrated`` unit semantics requires the payload to declare
  pressure units (SOFA ``Data.IR.Units`` == 'pascal'); a ``normalized`` or
  ``unknown`` fixture can never feed an absolute-level observable
  (``unsupported``).
- ``coherent_phase`` phase authority is required for observables that
  compare phase-bearing data (``complex_transfer``, ``impulse_window``);
  ``magnitude_only``/``unknown`` authority makes them ``unsupported`` —
  wave phase is never claimed for a phase-unknown source.
- an observable whose declared band is disjoint from the fixture's valid
  band is ``unobservable``; data the fixture does not contain is
  ``missing``. Neither is a numeric fail.
- this harness never asserts room-scale/production validation: evidence
  class stays ``external_measured`` and the qualification layer (#809)
  decides level attainment separately.
"""

from __future__ import annotations

import hashlib
import io
import math
import platform
import re
import zipfile
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_benchmark import (
    BenchmarkCase,
    BenchmarkEvidenceClass,
    BenchmarkObservable,
    BenchmarkPoint,
    BenchmarkSourceAsset,
    EvaluationProfile,
    ObservableKind,
    PredictionProvider,
    evaluate_observable,
)
from .cad_equipment import EquipmentDataProvenance
from .cad_external_admission import ExternalAssetAdmission
from .cad_external_corpus_manifest import CorpusScene
from .canonical_json import canonical_sha256 as _hash

EXTERNAL_FIXTURE_SCHEMA_VERSION = 1
EXTERNAL_FIXTURE_AUTHORITY_VERSION = 'external-fixture-1'
EXTERNAL_FIXTURE_RUN_SCHEMA_VERSION = 1
EXTERNAL_FIXTURE_EVIDENCE_SCHEMA_VERSION = 1

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

FixtureUnitSemantics = Literal['pascal_calibrated', 'normalized', 'unknown']
"""How the measured amplitude axis must be interpreted.

``pascal_calibrated`` — payload values are absolute calibrated pressure in
pascal (BRAS ``Data.IR`` calibrated-Pa convention). ``normalized`` — values
are a ratio/normalized amplitude with no absolute reference. ``unknown`` —
the payload does not declare units; amplitude-sensitive observables are all
``unsupported`` (never guess calibration).
"""

FixturePhaseAuthority = Literal['coherent_phase', 'magnitude_only', 'unknown']
"""Whether the fixture's measured data carries trustworthy phase.

``coherent_phase`` — waveform/complex comparisons are meaningful.
``magnitude_only`` — only magnitude-level observables may be evaluated;
``complex_transfer``/``impulse_window`` are ``unsupported`` rather than a
fake phase claim. ``unknown`` behaves as ``magnitude_only`` for gating but
is recorded verbatim.
"""

FixtureRedistribution = Literal['permitted', 'forbidden', 'requires_review']
"""Whether fixture payloads/derived artifacts may be redistributed.

``permitted`` — the license allows sharing derived data (still requires
attribution; CC BY-SA additionally requires share-alike). ``forbidden`` —
payload bytes and derived excerpts must not leave the local machine;
verification receipts and verdicts (no payload data) may still be
reported. ``requires_review`` — redistribution policy unresolved; treat as
``forbidden`` everywhere.
"""

FixtureFileRole = Literal[
    'measured_rir',
    'geometry',
    'boundary_material',
    'source_directivity',
    'scene_metadata',
    'documentation',
    'other',
]

FixturePointRole = Literal['source', 'receiver']

FixtureObservableVerdict = Literal[
    'pass',
    'fail',
    'missing',
    'unobservable',
    'unsupported',
]
"""Per-observable verdict vocabulary (#948): ``missing`` = data the fixture
or provider does not contain; ``unobservable`` = the fixture physically
cannot observe it (disjoint band, no reference axis); ``unsupported`` = the
metric is inapplicable under the fixture's declared authority (normalized
units cannot feed an absolute-level metric; magnitude-only data cannot
feed a phase-bearing comparison)."""

FixtureRunStatus = Literal['pass', 'fail', 'incomplete', 'blocked']

FixtureRunMode = Literal['preregistered_unfitted', 'informed_calibrated']
"""Mirrors #809: ``informed_calibrated`` runs must name the tuned
parameters and pin the prior unfitted evidence they build on."""

FixtureReplayVerdict = Literal['reproduced', 'diverged', 'blocked']

FixtureFileVerdict = Literal[
    'verified',
    'missing',
    'size_mismatch',
    'md5_mismatch',
    'sha256_mismatch',
    'member_missing',
    'member_sha256_mismatch',
    'member_size_mismatch',
    'unverifiable',
    'outside_admission',
]
"""Fail-closed per-file verdicts; ``verified`` requires every pin to match.
``outside_admission`` = fixture names a file the admission never listed."""

_REDISTRIBUTABLE_LICENSE_FAMILIES = frozenset(
    {'cc0', 'cc_by', 'cc_by_sa', 'apache_2_0', 'mit'}
)
"""License families where ``redistribution='permitted'`` may be declared.
Everything else (``research_only``, ``proprietary``, ``unknown``) cannot
carry a permitted redistribution claim."""

_AMPLITUDE_SENSITIVE_KINDS = frozenset(
    {'magnitude_fr', 'complex_transfer', 'impulse_window'}
)
"""Kinds whose numeric result is meaningless when the fixture's amplitude
axis is ``unknown``."""

_PHASE_BEARING_KINDS = frozenset({'complex_transfer', 'impulse_window'})
"""Kinds that compare phase-bearing data; require coherent phase authority."""

_ABSOLUTE_LEVEL_TOLERANCE_UNITS = frozenset(
    {
        'pa',
        'pa_rms',
        'pa·s',
        'pa*s',
        'db_spl',
        'db spl',
        'spl',
    }
)
"""Tolerance units that express an absolute physical level. Require
``pascal_calibrated`` unit semantics — a normalized-amplitude fixture can
never meet them without conflating normalized amplitude with Pa."""

_SOFA_METRE_UNITS = frozenset({'metre', 'meter', 'metres', 'meters', 'm'})
_SOFA_PRESSURE_UNITS = frozenset({'pascal', 'pa', 'pascals'})
_SOFA_CARTESIAN_TYPES = frozenset({'cartesian', 'cartesian_xyz'})
_SOFA_SPHERICAL_TYPES = frozenset({'spherical'})


class FixtureImportError(RuntimeError):
    """Fail-closed fixture import/verification failure."""


class FixtureIntegrityError(FixtureImportError):
    """Checksum/pin or declared-vs-measured mismatch — tamper evidence."""


# ---------------------------------------------------------------------------
# Fixture declaration
# ---------------------------------------------------------------------------


class FixturePin(BaseModel):
    """One payload the fixture consumes or binds.

    ``file_name`` must be a file listed by the pinned dataset admission;
    the archive-level checksum comes from that admission. ``member_path``
    selects a member inside a zip archive — archive bytes are hash-pinned
    so member identity is transitively bound; ``member_sha256`` adds an
    explicit per-member pin when the importer has recorded one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    file_name: str = Field(min_length=1)
    member_path: str | None = Field(default=None, min_length=1)
    role: FixtureFileRole = 'other'
    member_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    member_size_bytes: int | None = Field(default=None, ge=0)


class FixturePoint(BaseModel):
    """A declared source or receiver point on the measurement axis.

    ``index`` selects the row of the payload's per-measurement position
    grid (``SourcePosition[i]`` for sources, ``ListenerPosition[i]`` for
    receivers); ``None`` assigns the next free index in declaration order
    per role. ``position_m=None`` means the position is carried only
    inside the licensed payload and is filled at import time; when
    declared, the reader verifies the measured position matches within
    1e-9 m and fails closed on drift.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    point_id: str = Field(min_length=1)
    role: FixturePointRole
    index: int | None = Field(default=None, ge=0)
    position_m: tuple[float, float, float] | None = None
    orientation_deg: tuple[float, float, float] | None = None
    semantics: str = Field(
        default='',
        description='verbatim semantic note (e.g. head orientation, array axis)',
    )


class FixtureMaterialBinding(BaseModel):
    """A boundary-material declaration bound to a payload file."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    material_id: str = Field(min_length=1)
    source_file_name: str = Field(min_length=1)
    declaration: str = Field(
        min_length=1,
        description=(
            'verbatim declaration of what the payload provides, e.g. '
            '"third-octave absorption/scattering + complex impedance, '
            '100 Hz–4 kHz" — no derived values stored here'
        ),
    )


class ExternalBenchmarkFixture(BaseModel):
    """Sealed admission binding for one external measured benchmark fixture.

    The record pins *what may be consumed* (file names/members), *what the
    data means* (units, coordinates, phase authority, valid band), and
    *under which license* — it never carries the measured payload itself.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EXTERNAL_FIXTURE_SCHEMA_VERSION
    authority_version: Literal['external-fixture-1'] = (
        EXTERNAL_FIXTURE_AUTHORITY_VERSION
    )
    fixture_id: str = Field(min_length=1)
    dataset_ref: AuthorityRef
    scene_ref: AuthorityRef
    scene_config_id: str = Field(min_length=1)
    coordinate_convention: str = Field(min_length=1)
    unit_semantics: FixtureUnitSemantics
    phase_authority: FixturePhaseAuthority
    valid_band_hz: tuple[float, float] | None = None
    validity_band_basis: str = ''
    points: tuple[FixturePoint, ...] = Field(min_length=1)
    boundary_materials: tuple[FixtureMaterialBinding, ...] = ()
    consumed_files: tuple[FixturePin, ...] = Field(min_length=1)
    bound_files: tuple[FixturePin, ...] = ()
    license_id: str = Field(min_length=1)
    license_family: str = Field(min_length=1)
    attribution_note: str = Field(min_length=1)
    redistribution: FixtureRedistribution
    origin_uri: str = Field(min_length=1)
    evidence_class: BenchmarkEvidenceClass = 'external_measured'
    environment: dict[str, Any] = Field(default_factory=dict)
    applicability_notes: str = ''
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'fixture_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'ExternalBenchmarkFixture':
        if self.dataset_ref.ref_sha256 is None:
            raise ValueError('dataset_ref must pin the admission sha256')
        if self.scene_ref.ref_sha256 is None:
            raise ValueError('scene_ref must pin the corpus scene sha256')
        if self.license_family == 'unknown':
            raise ValueError(
                'fixture admission requires a resolved license family — '
                'license-unclear data is never admitted'
            )
        if (
            self.redistribution == 'permitted'
            and self.license_family
            not in _REDISTRIBUTABLE_LICENSE_FAMILIES
        ):
            raise ValueError(
                f'redistribution=permitted is incompatible with license '
                f'family {self.license_family}'
            )
        roles = [p.role for p in self.points]
        if 'source' not in roles or 'receiver' not in roles:
            raise ValueError(
                'fixture must declare at least one source and one receiver'
            )
        point_ids = [p.point_id for p in self.points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError('duplicate fixture point ids')
        names = [p.file_name for p in self.consumed_files]
        if len(names) != len(set(names)):
            raise ValueError(
                'consumed_files may not list the same file twice'
            )
        if not any(p.role == 'measured_rir' for p in self.consumed_files):
            raise ValueError(
                'fixture requires at least one consumed measured_rir pin'
            )
        bound = {m.source_file_name for m in self.boundary_materials}
        declared = {
            p.file_name for p in (*self.consumed_files, *self.bound_files)
        }
        missing = bound - declared
        if missing:
            raise ValueError(
                f'boundary materials reference undeclared files: '
                f'{sorted(missing)}'
            )
        if self.valid_band_hz is not None:
            low, high = self.valid_band_hz
            if not (low > 0.0 and high > low):
                raise ValueError('valid_band_hz must satisfy 0 < low < high')
            if not self.validity_band_basis:
                raise ValueError(
                    'a declared valid band must carry its basis'
                )
        if self.fixture_sha256 != _hash(self.identity_payload()):
            raise ValueError('external benchmark fixture hash mismatch')
        return self


def build_external_benchmark_fixture(
    *,
    fixture_id: str,
    dataset: ExternalAssetAdmission,
    scene: CorpusScene,
    coordinate_convention: str,
    unit_semantics: FixtureUnitSemantics,
    phase_authority: FixturePhaseAuthority,
    points: Sequence[FixturePoint | dict],
    consumed_files: Sequence[FixturePin | dict],
    bound_files: Sequence[FixturePin | dict] = (),
    boundary_materials: Sequence[FixtureMaterialBinding | dict] = (),
    valid_band_hz: tuple[float, float] | None = None,
    validity_band_basis: str | None = None,
    redistribution: FixtureRedistribution | None = None,
    attribution_note: str | None = None,
    evidence_class: BenchmarkEvidenceClass = 'external_measured',
    environment: dict[str, Any] | None = None,
    applicability_notes: str = '',
    scene_config_id: str | None = None,
) -> ExternalBenchmarkFixture:
    """Build a sealed fixture — license/attribution defaults are copied
    verbatim from the pinned admission so the record stands alone.

    ``valid_band_hz`` defaults to the scene's declared
    ``expected_validity_band_hz`` (with its basis) — never invent a wider
    band than the corpus declares.
    """
    dataset_ref = AuthorityRef(
        kind='external_asset_admission',
        ref_id=dataset.admission_id,
        ref_sha256=dataset.semantic_sha256,
    )
    scene_ref = AuthorityRef(
        kind='external_corpus_scene',
        ref_id=scene.scene_id,
        ref_sha256=scene.scene_sha256,
    )
    band = valid_band_hz
    basis = validity_band_basis or ''
    if band is None:
        band = scene.expected_validity_band_hz
        basis = basis or scene.validity_band_basis
    if redistribution is None:
        redistribution = (
            'permitted'
            if dataset.license_family in _REDISTRIBUTABLE_LICENSE_FAMILIES
            else 'requires_review'
        )
    fields: dict[str, Any] = {
        'fixture_id': fixture_id,
        'dataset_ref': dataset_ref,
        'scene_ref': scene_ref,
        'scene_config_id': scene_config_id or scene.scene_id,
        'coordinate_convention': coordinate_convention,
        'unit_semantics': unit_semantics,
        'phase_authority': phase_authority,
        'valid_band_hz': band,
        'validity_band_basis': basis,
        'points': tuple(
            p if isinstance(p, FixturePoint) else FixturePoint(**p)
            for p in points
        ),
        'boundary_materials': tuple(
            m
            if isinstance(m, FixtureMaterialBinding)
            else FixtureMaterialBinding(**m)
            for m in boundary_materials
        ),
        'consumed_files': tuple(
            f if isinstance(f, FixturePin) else FixturePin(**f)
            for f in consumed_files
        ),
        'bound_files': tuple(
            f if isinstance(f, FixturePin) else FixturePin(**f)
            for f in bound_files
        ),
        'license_id': dataset.license_id,
        'license_family': dataset.license_family,
        'attribution_note': (
            attribution_note
            if attribution_note is not None
            else (
                f'{dataset.dataset_title} — {dataset.publisher}, '
                f'license {dataset.license_id}'
                + (
                    f' ({dataset.license_uri})'
                    if dataset.license_uri
                    else ''
                )
            )
        ),
        'redistribution': redistribution,
        'origin_uri': dataset.record_uri,
        'evidence_class': evidence_class,
        'environment': environment or {},
        'applicability_notes': applicability_notes,
    }
    probe = ExternalBenchmarkFixture.model_construct(
        **fields, fixture_sha256='0' * 64
    )
    digest = _hash(probe.identity_payload())
    return ExternalBenchmarkFixture(
        **fields, fixture_sha256=digest
    )


# ---------------------------------------------------------------------------
# Payload verification — fail closed
# ---------------------------------------------------------------------------


class FixtureFileReceipt(BaseModel):
    """Verification outcome for one pinned file/member."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    file_name: str
    member_path: str | None = None
    role: FixtureFileRole = 'other'
    verdict: FixtureFileVerdict
    size_bytes: int | None = None
    computed_md5: str | None = None
    computed_sha256: str | None = None


def _hash_file(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _verify_member(
    zip_path: Path,
    pin: FixturePin,
) -> FixtureFileReceipt:
    base = {
        'file_name': pin.file_name,
        'member_path': pin.member_path,
        'role': pin.role,
    }
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            if pin.member_path not in names:
                return FixtureFileReceipt(**base, verdict='member_missing')
            info = archive.getinfo(pin.member_path)
            if (
                pin.member_size_bytes is not None
                and info.file_size != pin.member_size_bytes
            ):
                return FixtureFileReceipt(
                    **base,
                    verdict='member_size_mismatch',
                    size_bytes=info.file_size,
                )
            if pin.member_sha256 is not None:
                digest = hashlib.sha256()
                with archive.open(pin.member_path) as member:
                    for chunk in iter(lambda: member.read(1 << 20), b''):
                        digest.update(chunk)
                computed = digest.hexdigest()
                if computed != pin.member_sha256:
                    return FixtureFileReceipt(
                        **base,
                        verdict='member_sha256_mismatch',
                        size_bytes=info.file_size,
                        computed_sha256=computed,
                    )
            return FixtureFileReceipt(
                **base, verdict='verified', size_bytes=info.file_size
            )
    except zipfile.BadZipFile:
        return FixtureFileReceipt(**base, verdict='unverifiable')


def verify_fixture_payloads(
    root: Path | str,
    fixture: ExternalBenchmarkFixture,
    dataset: ExternalAssetAdmission,
) -> tuple[FixtureFileReceipt, ...]:
    """Verify every consumed/bound pin against the admission checksums.

    ``root`` is the corpus target directory used by
    ``scripts/fetch_external_corpus.py`` — payloads live under
    ``<root>/<dataset_name>/<file_name>``. A pin naming a file the
    admission never listed is ``outside_admission``; a file with no
    publisher/computed checksum is ``unverifiable``; every mismatch is
    reported, never silently passed.
    """
    root = Path(root)
    admitted = {f.file_name: f for f in dataset.files}
    receipts: list[FixtureFileReceipt] = []
    for pin in (*fixture.consumed_files, *fixture.bound_files):
        base = {
            'file_name': pin.file_name,
            'member_path': pin.member_path,
            'role': pin.role,
        }
        admitted_file = admitted.get(pin.file_name)
        if admitted_file is None:
            receipts.append(
                FixtureFileReceipt(**base, verdict='outside_admission')
            )
            continue
        path = root / dataset.dataset_name / pin.file_name
        if not path.is_file():
            receipts.append(
                FixtureFileReceipt(**base, verdict='missing')
            )
            continue
        size = path.stat().st_size
        if size != admitted_file.size_bytes:
            receipts.append(
                FixtureFileReceipt(
                    **base, verdict='size_mismatch', size_bytes=size
                )
            )
            continue
        if admitted_file.md5 is None and admitted_file.sha256 is None:
            receipts.append(
                FixtureFileReceipt(
                    **base, verdict='unverifiable', size_bytes=size
                )
            )
            continue
        computed_md5, computed_sha256 = _hash_file(path)
        if admitted_file.md5 is not None and computed_md5 != admitted_file.md5:
            receipts.append(
                FixtureFileReceipt(
                    **base,
                    verdict='md5_mismatch',
                    size_bytes=size,
                    computed_md5=computed_md5,
                    computed_sha256=computed_sha256,
                )
            )
            continue
        if (
            admitted_file.sha256 is not None
            and computed_sha256 != admitted_file.sha256
        ):
            receipts.append(
                FixtureFileReceipt(
                    **base,
                    verdict='sha256_mismatch',
                    size_bytes=size,
                    computed_md5=computed_md5,
                    computed_sha256=computed_sha256,
                )
            )
            continue
        if pin.member_path is not None:
            receipts.append(_verify_member(path, pin))
            continue
        receipts.append(
            FixtureFileReceipt(
                **base,
                verdict='verified',
                size_bytes=size,
                computed_md5=computed_md5,
                computed_sha256=computed_sha256,
            )
        )
    return tuple(receipts)


def assert_fixture_payloads_verified(
    fixture: ExternalBenchmarkFixture,
    receipts: Sequence[FixtureFileReceipt],
) -> None:
    """Raise ``FixtureIntegrityError`` unless every consumed pin verified.

    ``bound_files`` receipts are advisory provenance context (they may be
    absent locally); ``consumed_files`` are the payloads the importer
    opens and must all verify.
    """
    consumed = {(p.file_name, p.member_path) for p in fixture.consumed_files}
    failures = [
        r
        for r in receipts
        if (r.file_name, r.member_path) in consumed
        and r.verdict != 'verified'
    ]
    if failures:
        detail = '; '.join(
            f'{r.file_name}'
            + (f'::{r.member_path}' if r.member_path else '')
            + f'={r.verdict}'
            for r in failures
        )
        raise FixtureIntegrityError(f'fixture payloads not verified: {detail}')


# ---------------------------------------------------------------------------
# SOFA SingleRoomSRIR reader — lazy h5py, semantics preserved verbatim
# ---------------------------------------------------------------------------


class FixtureMeasurement(BaseModel):
    """Measured payload imported from a licensed SOFA RIR.

    SOFA ``SingleRoomSRIR`` semantics: ``Data.IR`` is ``M x R x N`` where
    ``M`` is the per-measurement axis (each row has its own
    ``SourcePosition``/``ListenerPosition``) and ``R`` the receiver-channel
    axis. The record carries the payload's own semantics verbatim (units
    string, conventions string, positions, declared delays) plus a content
    hash of the IR samples — fixture evidence binds the exact imported
    measurement without vendoring it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    sofa_conventions: str
    data_ir_units: str
    sample_rate_hz: float = Field(gt=0.0)
    measurement_count: int = Field(ge=1)
    channel_count: int = Field(ge=1)
    ir_length_samples: int = Field(ge=1)
    source_positions_m: tuple[tuple[float, float, float], ...]
    listener_positions_m: tuple[tuple[float, float, float], ...]
    emitter_positions_m: tuple[tuple[float, float, float], ...] = ()
    receiver_positions_m: tuple[tuple[float, float, float], ...] = ()
    delays_s: tuple[tuple[float, ...], ...] = ()
    temperature_c: float | None = None
    room_type: str | None = None
    coordinate_system: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)


def _dataset_attr(node: Any, name: str) -> str | None:
    try:
        value = node.attrs.get(name)
    except Exception:  # noqa: BLE001 - attribute reads must not guess
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    import numpy as np  # noqa: PLC0415 - h5py payloads imply numpy

    if isinstance(value, np.ndarray):
        if value.size != 1:
            return None
        value = value.flat[0]
    return str(value)


def _scalar_attr(handle: Any, name: str) -> str | None:
    try:
        value = handle.attrs.get(name)
    except Exception:  # noqa: BLE001
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    import numpy as np  # noqa: PLC0415 - h5py payloads imply numpy

    if isinstance(value, np.ndarray):
        if value.size != 1:
            return None
        value = value.flat[0]
    return str(value)


def _to_triplets(raw: Any, *, what: str) -> tuple[tuple[float, float, float], ...]:
    rows: list[tuple[float, float, float]] = []
    for row in raw:
        triple = tuple(float(v) for v in row)
        if len(triple) != 3 or not all(math.isfinite(v) for v in triple):
            raise FixtureImportError(f'{what} has a non-finite/malformed row')
        rows.append(triple)
    return tuple(rows)


def read_sofa_measurement(
    source: Path | str,
    *,
    member_path: str | None = None,
    required_units: FixtureUnitSemantics | None = None,
    expected_coordinate_system: str | None = None,
) -> FixtureMeasurement:
    """Read one SOFA ``SingleRoomSRIR`` payload, preserving semantics.

    ``source`` is a ``.sofa`` file or a zip archive with ``member_path``
    selecting the member. Fails closed (``FixtureImportError``) on:

    - missing/unreadable payload, non-SOFA or non-SingleRoomSRIR
      conventions;
    - ambiguous/absent ``Data.IR.Units`` (never inferred);
    - ``required_units='pascal_calibrated'`` when the payload does not
      declare pascal units;
    - coordinate-system mismatch against
      ``expected_coordinate_system`` ('cartesian'/'spherical' keyword in
      the fixture's declared convention) or non-metre position units;
    - non-finite IR samples or position rows.
    """
    try:
        import h5py  # noqa: PLC0415 - lazy: only needed for .sofa payloads
    except ImportError as exc:  # pragma: no cover - environment guard
        raise FixtureImportError(
            'h5py is required to import SOFA fixtures (backend extra)'
        ) from exc

    source = Path(source)
    if member_path is not None:
        try:
            with zipfile.ZipFile(source) as archive:
                if member_path not in set(archive.namelist()):
                    raise FixtureImportError(
                        f'sofa member {member_path} absent from {source.name}'
                    )
                payload = archive.read(member_path)
        except zipfile.BadZipFile as exc:
            raise FixtureImportError(
                f'{source.name} is not a readable zip archive'
            ) from exc
        opener: Any = io.BytesIO(payload)
    else:
        if not source.is_file():
            raise FixtureImportError(f'sofa payload missing: {source}')
        opener = source

    with h5py.File(opener, 'r') as sofa:
        conventions = _scalar_attr(sofa, 'SOFAConventions')
        if conventions is None or 'SingleRoomSRIR' not in conventions:
            raise FixtureImportError(
                f'not a SingleRoomSRIR SOFA payload '
                f'(SOFAConventions={conventions!r})'
            )

        def _var(name: str) -> Any:
            # SOFA stores Data.* either as flat variable names ('Data.IR')
            # or under a 'Data' group — accept both, prefer flat.
            if name in sofa:
                return sofa[name]
            if 'Data' in sofa:
                subgroup = sofa['Data']
                short = name.split('.', 1)[-1]
                if short in subgroup:
                    return subgroup[short]
            return None

        ir_node = _var('Data.IR')
        if ir_node is None:
            raise FixtureImportError('SOFA payload lacks Data.IR')
        units = _dataset_attr(ir_node, 'Units')
        if units is None or not units.strip():
            # Some datasets (e.g. BRAS RS8) declare IR units only in the
            # file-level Comment attr — read what the publisher declared,
            # never guess. Fail closed when neither declares units.
            comment = _scalar_attr(sofa, 'Comment') or ''
            m = re.search(
                r'unit\s+of\s+Data\.IR\s+is\s+([^\.\[]+)', comment,
                flags=re.IGNORECASE,
            )
            units = m.group(1).strip() if m else None
        if units is None or not units.strip():
            raise FixtureImportError(
                'Data.IR declares no Units — amplitude semantics unknown; '
                'refusing to guess calibration'
            )
        units = units.strip().rstrip('.').strip()
        if required_units == 'pascal_calibrated' and (
            units.lower() not in _SOFA_PRESSURE_UNITS
        ):
            raise FixtureImportError(
                f'fixture declares pascal_calibrated units but payload '
                f'Data.IR.Units={units!r}'
            )
        rate_node = _var('Data.SamplingRate')
        if rate_node is None:
            raise FixtureImportError('SOFA payload lacks Data.SamplingRate')
        import numpy as np  # noqa: PLC0415 - h5py already requires numpy

        rate = float(np.asarray(rate_node[()]).reshape(-1)[0])
        if not (math.isfinite(rate) and rate > 0):
            raise FixtureImportError('Data.SamplingRate is not a positive scalar')
        ir = np.asarray(ir_node[()])
        if ir.ndim != 3:
            raise FixtureImportError(
                f'Data.IR must be MxRxN (measurements x receivers x '
                f'samples), got shape {ir.shape}'
            )
        if not bool(np.isfinite(ir).all()):
            raise FixtureImportError('Data.IR contains non-finite samples')
        ir_sha = hashlib.sha256(ir.astype('<f8').tobytes()).hexdigest()

        measurement_count, channel_count, ir_length = ir.shape

        def _positions(group: str, what: str) -> tuple[tuple[float, float, float], ...]:
            node = sofa.get(group)
            if node is None:
                raise FixtureImportError(f'SOFA payload lacks {group}')
            pos_type = (_dataset_attr(node, 'Type') or '').strip().lower()
            pos_units = (_dataset_attr(node, 'Units') or '').strip().lower()
            if expected_coordinate_system is not None:
                want = expected_coordinate_system.strip().lower()
                if 'cartesian' in want and pos_type not in _SOFA_CARTESIAN_TYPES:
                    raise FixtureImportError(
                        f'{what}: fixture convention {want!r} but payload '
                        f'{group}.Type={pos_type!r}'
                    )
                if 'spherical' in want and pos_type not in _SOFA_SPHERICAL_TYPES:
                    raise FixtureImportError(
                        f'{what}: fixture convention {want!r} but payload '
                        f'{group}.Type={pos_type!r}'
                    )
            tokens = [t.strip() for t in pos_units.split(',') if t.strip()]
            if not tokens or not all(t in _SOFA_METRE_UNITS for t in tokens):
                raise FixtureImportError(
                    f'{what}: {group}.Units={pos_units!r} is not metres'
                )
            raw = np.asarray(node[()]).reshape(-1, 3)
            return _to_triplets(raw.tolist(), what=what)

        source_pos = _positions('SourcePosition', 'source positions')
        listener_pos = _positions('ListenerPosition', 'listener positions')
        if len(source_pos) not in (1, measurement_count):
            raise FixtureImportError(
                f'SourcePosition count {len(source_pos)} incompatible '
                f'with Data.IR measurement axis {measurement_count}'
            )
        if len(listener_pos) not in (1, measurement_count):
            raise FixtureImportError(
                f'ListenerPosition count {len(listener_pos)} incompatible '
                f'with Data.IR measurement axis {measurement_count}'
            )
        if len(source_pos) == 1 and measurement_count > 1:
            source_pos = source_pos * measurement_count
        if len(listener_pos) == 1 and measurement_count > 1:
            listener_pos = listener_pos * measurement_count

        emitter_pos: tuple[tuple[float, float, float], ...] = ()
        if sofa.get('EmitterPosition') is not None:
            emitter_pos = _to_triplets(
                np.asarray(sofa['EmitterPosition'][()]).reshape(-1, 3).tolist(),
                what='emitter positions',
            )
        receiver_pos: tuple[tuple[float, float, float], ...] = ()
        if sofa.get('ReceiverPosition') is not None:
            receiver_pos = _to_triplets(
                np.asarray(sofa['ReceiverPosition'][()]).reshape(-1, 3).tolist(),
                what='receiver positions',
            )

        delays: tuple[tuple[float, ...], ...] = ()
        delay_node = _var('Data.Delay')
        if delay_node is not None:
            raw = np.asarray(delay_node[()])
            matrix = raw.reshape(1, -1) if raw.ndim <= 1 else raw.reshape(raw.shape[0], -1)
            delays = tuple(
                tuple(float(v) / rate for v in row) for row in matrix.tolist()
            )

        temperature_raw = _scalar_attr(sofa, 'Temperature')
        temperature_c = None
        if temperature_raw is not None:
            try:
                candidate = float(temperature_raw)
                if math.isfinite(candidate):
                    temperature_c = candidate
            except (TypeError, ValueError):
                temperature_c = None
        return FixtureMeasurement(
            sofa_conventions=conventions,
            data_ir_units=units,
            sample_rate_hz=rate,
            measurement_count=measurement_count,
            channel_count=channel_count,
            ir_length_samples=ir_length,
            source_positions_m=source_pos,
            listener_positions_m=listener_pos,
            emitter_positions_m=emitter_pos,
            receiver_positions_m=receiver_pos,
            delays_s=delays,
            temperature_c=temperature_c,
            room_type=_scalar_attr(sofa, 'RoomType'),
            coordinate_system=(
                _dataset_attr(sofa['ListenerPosition'], 'Type') or 'cartesian'
            ).strip().lower(),
            measurement_sha256=ir_sha,
        )


def measurement_ir(
    source: Path | str,
    *,
    member_path: str | None = None,
) -> Any:
    """Return the raw ``Data.IR`` array (numpy) for reference extraction.

    Separate from ``read_sofa_measurement`` so the sealed measurement
    record stays hashable; callers use this only to derive observable
    references (arrival time, windowed IR, spectra).
    """
    try:
        import h5py  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise FixtureImportError('h5py is required for SOFA payloads') from exc
    source = Path(source)
    if member_path is not None:
        with zipfile.ZipFile(source) as archive:
            payload = archive.read(member_path)
        opener: Any = io.BytesIO(payload)
    else:
        opener = source
    with h5py.File(opener, 'r') as sofa:
        if 'Data.IR' in sofa:
            return sofa['Data.IR'][()]
        if 'Data' in sofa and 'IR' in sofa['Data']:
            return sofa['Data']['IR'][()]
        raise FixtureImportError('SOFA payload lacks Data.IR')


def measured_arrival_s(
    measurement: FixtureMeasurement,
    *,
    measurement_index: int,
    channel_index: int = 0,
    ir: Any | None = None,
    derivation: str = 'data_delay',
) -> tuple[float, str]:
    """Measured direct-arrival time + how it was derived — always labelled.

    ``data_delay`` uses the payload's declared ``Data.Delay`` (the
    dataset's own time reference — note some datasets declare 0, which
    is then faithfully reported). ``peak`` uses argmax|IR|/sr (the
    dominant arrival — for BRAS anechoic RIRs the direct peak).
    ``threshold`` uses the first sample ≥10% of the channel peak —
    noisier; the returned label records the derivation honestly.
    """
    if derivation == 'data_delay':
        if not measurement.delays_s:
            raise FixtureImportError(
                'no Data.Delay in payload — choose derivation="peak" or '
                '"threshold" with the IR array, or declare the observable '
                'missing'
            )
        try:
            return (
                measurement.delays_s[measurement_index][channel_index],
                'data_delay',
            )
        except IndexError as exc:
            raise FixtureImportError(
                'Data.Delay grid incompatible with measurement/channel '
                'indices'
            ) from exc
    if ir is None:
        raise FixtureImportError(
            f'derivation={derivation} requires the IR array'
        )
    channel = ir[measurement_index][channel_index]
    peak = max(abs(float(v)) for v in channel)
    if peak == 0.0:
        raise FixtureImportError('measured channel is identically zero')
    if derivation == 'peak':
        index = max(
            range(len(channel)), key=lambda i: abs(float(channel[i]))
        )
        return index / measurement.sample_rate_hz, 'peak'
    if derivation == 'threshold':
        floor = 0.1 * peak
        for index, value in enumerate(channel):
            if abs(float(value)) >= floor:
                return index / measurement.sample_rate_hz, 'threshold'
        raise FixtureImportError('measured channel never crosses threshold')
    raise FixtureImportError(f'unknown arrival derivation {derivation!r}')


def impulse_window_reference(
    source: Path | str,
    *,
    member_path: str | None,
    measurement_index: int,
    channel_index: int = 0,
    window_s: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Extract ``{time_s, pressure}`` for an ``impulse_window`` reference —
    calibrated pascal samples, in the declared window."""
    ir = measurement_ir(source, member_path=member_path)
    measurement = read_sofa_measurement(source, member_path=member_path)
    channel = [float(v) for v in ir[measurement_index][channel_index]]
    rate = measurement.sample_rate_hz
    times = [i / rate for i in range(len(channel))]
    if window_s is not None:
        low, high = window_s
        pairs = [(t, p) for t, p in zip(times, channel) if low <= t <= high]
        if not pairs:
            raise FixtureImportError(
                f'window {window_s} selects no samples'
            )
        times = [t for t, _ in pairs]
        channel = [p for _, p in pairs]
    return {'time_s': times, 'pressure': channel}


def magnitude_spectrum_reference(
    source: Path | str,
    *,
    member_path: str | None,
    measurement_index: int,
    channel_index: int = 0,
    frequencies_hz: Sequence[float],
) -> dict[str, Any]:
    """Single-sided amplitude spectrum at a declared frequency grid.

    Units follow the payload: calibrated IRs yield ``Pa·s`` spectral
    magnitude; the caller chooses a matching absolute or relative
    ``tolerance_unit`` — the harness never normalizes silently.
    """
    import numpy as np  # noqa: PLC0415

    ir = measurement_ir(source, member_path=member_path)
    measurement = read_sofa_measurement(source, member_path=member_path)
    channel = np.asarray(ir[measurement_index][channel_index], dtype=np.float64)
    spectrum = np.abs(np.fft.rfft(channel)) / measurement.sample_rate_hz
    freqs = np.fft.rfftfreq(len(channel), 1.0 / measurement.sample_rate_hz)
    requested = [float(f) for f in frequencies_hz]
    out_of_band = [f for f in requested if f < 0.0 or f > freqs[-1]]
    if out_of_band:
        raise FixtureImportError(
            f'frequency grid {out_of_band} exceeds the measured band '
            f'0–{freqs[-1]:.1f} Hz — refusing extrapolated references'
        )
    values = np.interp(requested, freqs, spectrum)
    return {
        'frequency_hz': requested,
        'magnitude': [float(v) for v in values],
        'units': measurement.data_ir_units,
    }


# ---------------------------------------------------------------------------
# Fixture → BenchmarkCase import
# ---------------------------------------------------------------------------


class ImportedFixtureCase(BaseModel):
    """The normalized case plus the measurement semantics it imports."""

    model_config = ConfigDict(frozen=True, extra='forbid', arbitrary_types_allowed=True)

    case: BenchmarkCase
    measurement: FixtureMeasurement
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    receipts: tuple[FixtureFileReceipt, ...]


def _positions_match(
    declared: tuple[float, float, float],
    measured: tuple[float, float, float],
    *,
    point_id: str,
) -> None:
    if any(abs(a - b) > 1e-9 for a, b in zip(declared, measured)):
        raise FixtureIntegrityError(
            f'fixture point {point_id} declares {declared} but payload '
            f'positions carry {measured} — refusing to merge divergent '
            'geometry'
        )


def import_fixture_case(
    fixture: ExternalBenchmarkFixture,
    dataset: ExternalAssetAdmission,
    scene: CorpusScene,
    root: Path | str,
    *,
    importer_version: str = '1',
    rir_member_path: str | None = None,
) -> ImportedFixtureCase:
    """Verify pins, read the measured SOFA RIR, emit a ``BenchmarkCase``.

    Fail closed on: stale dataset/scene pins, any unverified consumed
    payload, unit/coordinate ambiguity, or declared fixture geometry
    diverging from the measured positions.
    """
    if fixture.dataset_ref.ref_id != dataset.admission_id:
        raise FixtureIntegrityError(
            'fixture pins a different dataset admission'
        )
    if fixture.dataset_ref.ref_sha256 != dataset.semantic_sha256:
        raise FixtureIntegrityError(
            'dataset admission hash drifted from the fixture pin'
        )
    if fixture.scene_ref.ref_id != scene.scene_id:
        raise FixtureIntegrityError('fixture pins a different corpus scene')
    if fixture.scene_ref.ref_sha256 != scene.scene_sha256:
        raise FixtureIntegrityError(
            'corpus scene hash drifted from the fixture pin'
        )

    receipts = verify_fixture_payloads(root, fixture, dataset)
    consumed_failures = {
        r
        for r in receipts
        if r.verdict != 'verified'
        and any(
            p.file_name == r.file_name and p.member_path == r.member_path
            for p in fixture.consumed_files
        )
    }
    if consumed_failures:
        detail = '; '.join(
            f'{r.file_name}={r.verdict}' for r in consumed_failures
        )
        raise FixtureIntegrityError(
            f'consumed fixture payloads not verified: {detail}'
        )

    rir_pin = next(
        p for p in fixture.consumed_files if p.role == 'measured_rir'
    )
    rir_path = Path(root) / dataset.dataset_name / rir_pin.file_name
    member_path = rir_member_path or rir_pin.member_path
    measurement = read_sofa_measurement(
        rir_path,
        member_path=member_path,
        required_units=fixture.unit_semantics,
        expected_coordinate_system=fixture.coordinate_convention,
    )

    def _declared_axis(role: str) -> dict[int, FixturePoint]:
        declared: dict[int, FixturePoint] = {}
        next_index = 0
        for point in fixture.points:
            if point.role != role:
                continue
            index = point.index if point.index is not None else next_index
            if index in declared:
                raise FixtureIntegrityError(
                    f'duplicate declared index {index} for {role} points'
                )
            declared[index] = point
            next_index = index + 1
        return declared

    declared_sources = _declared_axis('source')
    declared_receivers = _declared_axis('receiver')
    if declared_sources and max(declared_sources) >= measurement.measurement_count:
        raise FixtureIntegrityError(
            'declared source index exceeds the measurement axis'
        )
    if declared_receivers and max(declared_receivers) >= measurement.measurement_count:
        raise FixtureIntegrityError(
            'declared receiver index exceeds the measurement axis'
        )

    def _axis_points(
        declared: dict[int, FixturePoint],
        measured: tuple[tuple[float, float, float], ...],
        prefix: str,
    ) -> list[BenchmarkPoint]:
        points: list[BenchmarkPoint] = []
        for index, position in enumerate(measured):
            point = declared.get(index)
            if point is not None and point.position_m is not None:
                _positions_match(
                    point.position_m, position, point_id=point.point_id
                )
            points.append(
                BenchmarkPoint(
                    point_id=(
                        point.point_id if point is not None
                        else f'{prefix}{index + 1}'
                    ),
                    position_m=position,
                    orientation_deg=(
                        point.orientation_deg if point is not None else None
                    ),
                    role=(
                        (point.semantics or None)
                        if point is not None
                        else None
                    ),
                )
            )
        return points

    sources = _axis_points(
        declared_sources, measurement.source_positions_m, 'S'
    )
    receivers = _axis_points(
        declared_receivers, measurement.listener_positions_m, 'R'
    )

    provenance = EquipmentDataProvenance(
        evidence_kind='measured',
        source_name=dataset.publisher,
        source_version=dataset.version_record_id
        or dataset.version_doi
        or 'unversioned',
        source_reference=dataset.record_uri,
        source_sha256=dataset.semantic_sha256,
    )
    source_asset_fields: dict[str, Any] = {
        'asset_id': f'fixture:{fixture.fixture_id}',
        'dataset_name': dataset.dataset_name,
        'dataset_version': dataset.version_doi
        or dataset.version_record_id
        or 'unversioned',
        'origin_uri': fixture.origin_uri,
        'content_sha256': None,
        'license_id': fixture.license_id,
        'admission_ref': dataset.admission_id,
        'evidence_class': fixture.evidence_class,
        'provenance': (provenance,),
    }
    source_asset_probe = BenchmarkSourceAsset.model_construct(
        **source_asset_fields, semantic_sha256='0' * 64
    )
    source_asset = BenchmarkSourceAsset(
        **source_asset_fields,
        semantic_sha256=_hash(source_asset_probe.semantic_payload()),
    )

    limitations_bits = [
        'licensed external measured data — payloads never vendored',
        f'redistribution={fixture.redistribution}',
        f'unit_semantics={fixture.unit_semantics}',
        f'phase_authority={fixture.phase_authority}',
    ]
    if fixture.valid_band_hz:
        limitations_bits.append(
            f'valid band {fixture.valid_band_hz[0]}–'
            f'{fixture.valid_band_hz[1]} Hz '
            f'({fixture.validity_band_basis})'
        )
    if fixture.applicability_notes:
        limitations_bits.append(fixture.applicability_notes)

    environment = dict(fixture.environment)
    if measurement.temperature_c is not None:
        environment.setdefault('temperature_c', measurement.temperature_c)
    if measurement.room_type is not None:
        environment.setdefault('room_type', measurement.room_type)
    environment['data_ir_units'] = measurement.data_ir_units

    case_fields: dict[str, Any] = {
        'benchmark_id': f'bras-fixture-{fixture.scene_config_id.lower()}',
        'version': importer_version,
        'title': f'{scene.title} — fixture {fixture.fixture_id}',
        'source_asset': source_asset,
        'coordinate_convention': fixture.coordinate_convention,
        'geometry': {
            'scene_config_id': fixture.scene_config_id,
            'boundary_materials': [
                {
                    'material_id': m.material_id,
                    'source_file_name': m.source_file_name,
                    'declaration': m.declaration,
                }
                for m in fixture.boundary_materials
            ],
        },
        'sources': tuple(sources),
        'receivers': tuple(receivers),
        'materials': (
            {
                'boundary_declarations': [
                    m.declaration for m in fixture.boundary_materials
                ]
            }
            if fixture.boundary_materials
            else None
        ),
        'environment': environment,
        'sample_rate_hz': measurement.sample_rate_hz,
        'time_origin_s': 0.0,
        'preprocessing': 'none — measured RIR imported verbatim',
        'limitations': '; '.join(limitations_bits),
        'observables': (),
        'importer_id': 'external-fixture-importer',
        'importer_version': importer_version,
    }
    case_probe = BenchmarkCase.model_construct(
        **case_fields, semantic_sha256='0' * 64
    )
    case = BenchmarkCase(
        **case_fields,
        semantic_sha256=_hash(case_probe.semantic_payload()),
    )
    return ImportedFixtureCase(
        case=case,
        measurement=measurement,
        fixture_sha256=fixture.fixture_sha256,
        receipts=receipts,
    )


# ---------------------------------------------------------------------------
# Eligibility-gated evaluation
# ---------------------------------------------------------------------------


class FixtureObservableEvaluation(BaseModel):
    """One observable's verdict inside a fixture run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable_id: str = Field(min_length=1)
    kind: ObservableKind | None = None
    verdict: FixtureObservableVerdict
    metric_id: str | None = None
    metric_version: str | None = None
    tolerance: float | None = None
    tolerance_unit: str | None = None
    evaluated_band_hz: tuple[float, float] | None = None
    error: float | None = None
    reason: str | None = None


def _intersect_band(
    fixture: ExternalBenchmarkFixture,
    observable: BenchmarkObservable,
) -> tuple[float, float] | None | Literal['unobservable']:
    """Band the fixture can actually observe; ``'unobservable'`` when the
    observable's declared band is disjoint from the fixture's valid band."""
    obs_band = observable.valid_band_hz
    fixture_band = fixture.valid_band_hz
    if obs_band is None or fixture_band is None:
        return obs_band if obs_band is not None else fixture_band
    low = max(obs_band[0], fixture_band[0])
    high = min(obs_band[1], fixture_band[1])
    if high <= low:
        return 'unobservable'
    return (low, high)


def fixture_observable_blocker(
    fixture: ExternalBenchmarkFixture,
    observable: BenchmarkObservable,
) -> tuple[FixtureObservableVerdict, str] | None:
    """Eligibility gate — returns ``(verdict, reason)`` when the fixture's
    declared authority cannot carry this observable, else ``None``."""
    if (
        observable.kind in _PHASE_BEARING_KINDS
        and fixture.phase_authority != 'coherent_phase'
    ):
        return (
            'unsupported',
            f'{observable.kind} embeds phase information but fixture '
            f'phase_authority={fixture.phase_authority} — refusing a '
            'phase claim for a phase-unknown source',
        )
    if (
        observable.kind in _AMPLITUDE_SENSITIVE_KINDS
        and fixture.unit_semantics == 'unknown'
    ):
        return (
            'unsupported',
            f'{observable.kind} is amplitude-sensitive but fixture '
            'unit_semantics=unknown',
        )
    unit = (observable.tolerance_unit or '').strip().lower()
    if (
        unit in _ABSOLUTE_LEVEL_TOLERANCE_UNITS
        and fixture.unit_semantics != 'pascal_calibrated'
    ):
        return (
            'unsupported',
            f'absolute-level tolerance unit {unit!r} requires '
            f'pascal_calibrated data; fixture is {fixture.unit_semantics} '
            '— normalized amplitude is not Pa',
        )
    return None


def _window_values(
    observable: BenchmarkObservable,
    payload: dict[str, Any],
) -> tuple[list[float], list[float], str | None]:
    times = payload.get('time_s')
    values = payload.get('pressure')
    if (
        not isinstance(times, list)
        or not isinstance(values, list)
        or len(times) != len(values)
        or not times
    ):
        return [], [], 'missing'
    pairs: list[tuple[float, float]] = []
    for raw_t, raw_v in zip(times, values):
        try:
            t_value, v_value = float(raw_t), float(raw_v)
        except (TypeError, ValueError):
            return [], [], 'missing'
        if not math.isfinite(t_value) or not math.isfinite(v_value):
            return [], [], 'missing'
        pairs.append((t_value, v_value))
    window = observable.window_s
    if window is not None:
        pairs = [p for p in pairs if window[0] <= p[0] <= window[1]]
    if not pairs:
        return [], [], 'missing'
    return (
        [t for t, _ in pairs],
        [v for _, v in pairs],
        None,
    )


def _evaluate_impulse_window(
    observable: BenchmarkObservable,
    prediction: dict[str, Any],
    tolerance: float,
) -> tuple[FixtureObservableVerdict, float | None, str | None]:
    ref_t, ref_p, failure = _window_values(observable, observable.reference)
    if failure is not None:
        return 'missing', None, 'reference impulse_window data missing'
    pred_t, pred_p, failure = _window_values(observable, prediction)
    if failure is not None or pred_t != ref_t:
        return 'missing', None, (
            'prediction impulse_window data missing or misaligned'
        )
    error = math.sqrt(
        sum((p - r) ** 2 for r, p in zip(ref_p, pred_p)) / len(ref_p)
    )
    return ('pass' if error <= tolerance else 'fail'), error, None


def _evaluate_decay_metric(
    observable: BenchmarkObservable,
    prediction: dict[str, Any],
    tolerance: float,
) -> tuple[FixtureObservableVerdict, float | None, str | None]:
    ref = observable.reference.get('value_s')
    pred = prediction.get('value_s')
    if ref is None or pred is None:
        return 'missing', None, 'decay value missing'
    error = abs(float(pred) - float(ref))
    return ('pass' if error <= tolerance else 'fail'), error, None


def evaluate_fixture_observable(
    fixture: ExternalBenchmarkFixture,
    observable: BenchmarkObservable,
    prediction: dict[str, Any] | None,
    profile: EvaluationProfile | None = None,
) -> FixtureObservableEvaluation:
    """Eligibility-gated per-observable evaluation — never conflates
    ``missing``/``unobservable``/``unsupported`` with a numeric fail."""
    band = _intersect_band(fixture, observable)
    tolerance = (
        profile.tolerance_overrides.get(observable.observable_id)
        if profile is not None
        else None
    )
    if tolerance is None:
        tolerance = observable.tolerance
    base = {
        'observable_id': observable.observable_id,
        'kind': observable.kind,
        'metric_id': observable.metric_id,
        'metric_version': observable.metric_version,
        'tolerance': tolerance,
        'tolerance_unit': observable.tolerance_unit,
        'evaluated_band_hz': (
            None if band == 'unobservable' else band
        ),
    }

    def _result(
        verdict: FixtureObservableVerdict,
        *,
        error: float | None = None,
        reason: str | None = None,
    ) -> FixtureObservableEvaluation:
        return FixtureObservableEvaluation(
            **base, verdict=verdict, error=error, reason=reason
        )

    if band == 'unobservable':
        return _result(
            'unobservable',
            reason=(
                'observable band is disjoint from the fixture valid band'
            ),
        )
    blocked = fixture_observable_blocker(fixture, observable)
    if blocked is not None:
        verdict, reason = blocked
        return _result(verdict, reason=reason)
    if prediction is None:
        return _result('missing', reason='no prediction supplied')
    tolerance = base['tolerance']
    if tolerance is None:
        return _result('missing', reason='no tolerance bound supplied')

    if observable.kind == 'impulse_window':
        verdict, error, reason = _evaluate_impulse_window(
            observable, prediction, float(tolerance)
        )
        return _result(verdict, error=error, reason=reason)
    if observable.kind == 'decay_metric':
        verdict, error, reason = _evaluate_decay_metric(
            observable, prediction, float(tolerance)
        )
        return _result(verdict, error=error, reason=reason)

    delegated = evaluate_observable(observable, prediction, profile)
    mapping: dict[str, FixtureObservableVerdict] = {
        'PASS': 'pass',
        'FAIL': 'fail',
        'UNKNOWN': 'missing',
        'NOT_APPLICABLE': 'unsupported',
    }
    return _result(
        mapping[delegated.status],
        error=delegated.error,
        reason=delegated.reason,
    )


# ---------------------------------------------------------------------------
# Run spec + sealed evidence + replay
# ---------------------------------------------------------------------------


def runtime_descriptor() -> dict[str, str]:
    """Runtime pinned into the run spec for deterministic replay."""
    descriptor = {
        'python_version': platform.python_version(),
        'python_implementation': platform.python_implementation(),
        'platform': platform.platform(),
    }
    try:
        import h5py  # noqa: PLC0415

        descriptor['h5py_version'] = h5py.__version__
    except ImportError:
        descriptor['h5py_version'] = 'absent'
    try:
        import numpy as np  # noqa: PLC0415

        descriptor['numpy_version'] = np.__version__
    except ImportError:
        descriptor['numpy_version'] = 'absent'
    return descriptor


class FixtureRuntime(BaseModel):
    """Runtime identity recorded inside the run spec."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    python_version: str = Field(min_length=1)
    python_implementation: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    h5py_version: str = Field(min_length=1)
    numpy_version: str = Field(min_length=1)


class FixtureRunSpec(BaseModel):
    """Sealed, deterministic run plan — everything needed to replay.

    Pins solver/provider identity + revision, mesh resolution, seed,
    runtime descriptor, evaluation profile and run mode. For
    ``informed_calibrated`` runs the tuned parameter names and the pinned
    prior unfitted evidence are mandatory (#809-style separation).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EXTERNAL_FIXTURE_RUN_SCHEMA_VERSION
    authority_version: Literal['external-fixture-1'] = (
        EXTERNAL_FIXTURE_AUTHORITY_VERSION
    )
    spec_id: str = Field(min_length=1)
    fixture_ref: AuthorityRef
    run_mode: FixtureRunMode = 'preregistered_unfitted'
    informed_parameters: tuple[str, ...] = ()
    prior_unfitted_evidence_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    provider_config_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    solver_path: str | None = Field(default=None, min_length=1)
    solver_revision: str | None = Field(default=None, min_length=1)
    mesh_resolution: str | None = Field(default=None, min_length=1)
    seed: str | None = Field(default=None, min_length=1)
    evaluation_profile_id: str = Field(min_length=1)
    evaluation_profile_version: str = Field(min_length=1)
    runtime: FixtureRuntime
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'spec_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'FixtureRunSpec':
        if self.fixture_ref.ref_sha256 is None:
            raise ValueError('fixture_ref must pin the fixture sha256')
        if self.run_mode == 'informed_calibrated':
            if not self.informed_parameters:
                raise ValueError(
                    'informed_calibrated runs must name the tuned '
                    'parameters'
                )
            if self.prior_unfitted_evidence_sha256 is None:
                raise ValueError(
                    'informed_calibrated runs must pin the prior '
                    'unfitted evidence they calibrate against'
                )
        else:
            if self.informed_parameters:
                raise ValueError(
                    'preregistered_unfitted runs must not carry informed '
                    'parameters'
                )
            if self.prior_unfitted_evidence_sha256 is not None:
                raise ValueError(
                    'preregistered_unfitted runs must not pin prior '
                    'fitted evidence'
                )
        if self.spec_sha256 != _hash(self.identity_payload()):
            raise ValueError('fixture run spec hash mismatch')
        return self


def build_fixture_run_spec(
    *,
    spec_id: str,
    fixture: ExternalBenchmarkFixture,
    provider: PredictionProvider,
    evaluation_profile: EvaluationProfile,
    run_mode: FixtureRunMode = 'preregistered_unfitted',
    informed_parameters: Sequence[str] = (),
    prior_unfitted_evidence_sha256: str | None = None,
    solver_path: str | None = None,
    solver_revision: str | None = None,
    mesh_resolution: str | None = None,
    seed: str | None = None,
    runtime: dict[str, str] | None = None,
) -> FixtureRunSpec:
    """Build a sealed run spec; runtime defaults to the current descriptor
    (replay then verifies the environment matches)."""
    fields: dict[str, Any] = {
        'spec_id': spec_id,
        'fixture_ref': AuthorityRef(
            kind='external_benchmark_fixture',
            ref_id=fixture.fixture_id,
            ref_sha256=fixture.fixture_sha256,
        ),
        'run_mode': run_mode,
        'informed_parameters': tuple(informed_parameters),
        'prior_unfitted_evidence_sha256': prior_unfitted_evidence_sha256,
        'provider_id': provider.provider_id,
        'provider_version': provider.provider_version,
        'provider_config_sha256': provider.config_sha256(),
        'solver_path': solver_path,
        'solver_revision': solver_revision,
        'mesh_resolution': mesh_resolution,
        'seed': seed,
        'evaluation_profile_id': evaluation_profile.profile_id,
        'evaluation_profile_version': evaluation_profile.version,
        'runtime': FixtureRuntime(**(runtime or runtime_descriptor())),
    }
    probe = FixtureRunSpec.model_construct(**fields, spec_sha256='0' * 64)
    return FixtureRunSpec(
        **fields, spec_sha256=_hash(probe.identity_payload())
    )


class FixtureRunEvidence(BaseModel):
    """Sealed, immutable verdict + evidence for one fixture run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = EXTERNAL_FIXTURE_EVIDENCE_SCHEMA_VERSION
    authority_version: Literal['external-fixture-1'] = (
        EXTERNAL_FIXTURE_AUTHORITY_VERSION
    )
    evidence_id: str = Field(min_length=1)
    spec_ref: AuthorityRef
    fixture_ref: AuthorityRef
    benchmark_sha256: str = Field(pattern=_SHA256_PATTERN)
    run_mode: FixtureRunMode
    status: FixtureRunStatus
    observable_evaluations: tuple[FixtureObservableEvaluation, ...]
    verdict_counts: dict[str, int]
    measured_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'evidence_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'FixtureRunEvidence':
        if self.spec_ref.ref_sha256 is None:
            raise ValueError('spec_ref must pin the spec sha256')
        if self.fixture_ref.ref_sha256 is None:
            raise ValueError('fixture_ref must pin the fixture sha256')
        if self.evidence_sha256 != _hash(self.identity_payload()):
            raise ValueError('fixture run evidence hash mismatch')
        return self


def _fold_fixture_verdicts(
    verdicts: Sequence[FixtureObservableVerdict],
) -> FixtureRunStatus:
    if not verdicts:
        return 'blocked'
    if 'fail' in verdicts:
        return 'fail'
    if all(v == 'pass' for v in verdicts):
        return 'pass'
    return 'incomplete'


def run_fixture(
    fixture: ExternalBenchmarkFixture,
    imported: ImportedFixtureCase,
    observables: Sequence[BenchmarkObservable],
    provider: PredictionProvider,
    spec: FixtureRunSpec,
    profile: EvaluationProfile | None = None,
) -> FixtureRunEvidence:
    """Execute the declared observables against the provider's predictions
    and seal the evidence.

    Frozen-config checks: the spec's fixture pin, provider identity and
    config hash must match exactly — any drift raises, never re-pins.
    """
    if spec.fixture_ref.ref_sha256 != fixture.fixture_sha256:
        raise FixtureIntegrityError(
            'run spec pins a different fixture revision'
        )
    if spec.provider_id != provider.provider_id:
        raise FixtureIntegrityError('provider identity drifted from spec')
    if spec.provider_version != provider.provider_version:
        raise FixtureIntegrityError('provider version drifted from spec')
    if spec.provider_config_sha256 != provider.config_sha256():
        raise FixtureIntegrityError(
            'provider config hash drifted from spec'
        )

    case_probe = imported.case.model_copy(
        update={'observables': tuple(observables)}
    )
    case_fields = case_probe.model_dump(mode='python')
    case_fields.pop('semantic_sha256', None)
    case = BenchmarkCase(
        **case_fields,
        semantic_sha256=_hash(case_probe.semantic_payload()),
    )
    predictions = provider.predict(case)
    evaluations = tuple(
        evaluate_fixture_observable(
            fixture, observable, predictions.get(observable.observable_id), profile
        )
        for observable in observables
    )
    counts: dict[str, int] = {}
    for evaluation in evaluations:
        counts[evaluation.verdict] = counts.get(evaluation.verdict, 0) + 1
    status = _fold_fixture_verdicts([e.verdict for e in evaluations])

    fields: dict[str, Any] = {
        'evidence_id': f'{spec.spec_id}-evidence',
        'spec_ref': AuthorityRef(
            kind='external_fixture_run_spec',
            ref_id=spec.spec_id,
            ref_sha256=spec.spec_sha256,
        ),
        'fixture_ref': spec.fixture_ref,
        'benchmark_sha256': case.semantic_sha256,
        'run_mode': spec.run_mode,
        'status': status,
        'observable_evaluations': evaluations,
        'verdict_counts': counts,
        'measured_payload_sha256': imported.measurement.measurement_sha256,
    }
    probe = FixtureRunEvidence.model_construct(
        **fields, evidence_sha256='0' * 64
    )
    return FixtureRunEvidence(
        **fields, evidence_sha256=_hash(probe.identity_payload())
    )


class FixtureReplayReport(BaseModel):
    """Replay verdict — identity comparison, never a fresh verdict."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    verdict: FixtureReplayVerdict
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_evidence_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    observed_evidence_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    reason: str | None = None


def replay_fixture(
    fixture: ExternalBenchmarkFixture,
    imported: ImportedFixtureCase,
    observables: Sequence[BenchmarkObservable],
    provider: PredictionProvider,
    spec: FixtureRunSpec,
    prior_evidence: FixtureRunEvidence | None,
    profile: EvaluationProfile | None = None,
) -> FixtureReplayReport:
    """Re-execute the spec and compare the sealed evidence identity.

    ``blocked`` when the current runtime differs from the spec's recorded
    descriptor — environment drift is surfaced, not absorbed.
    """
    current = runtime_descriptor()
    recorded = spec.runtime.model_dump()
    if current != recorded:
        drift = sorted(
            k
            for k in current
            if current[k] != recorded.get(k)
        )
        return FixtureReplayReport(
            verdict='blocked',
            spec_sha256=spec.spec_sha256,
            expected_evidence_sha256=(
                prior_evidence.evidence_sha256 if prior_evidence else None
            ),
            reason=f'runtime drift on {", ".join(drift)}',
        )
    evidence = run_fixture(
        fixture, imported, observables, provider, spec, profile
    )
    if prior_evidence is not None and (
        prior_evidence.spec_ref.ref_sha256 != spec.spec_sha256
    ):
        raise FixtureIntegrityError(
            'prior evidence was produced under a different spec'
        )
    expected = prior_evidence.evidence_sha256 if prior_evidence else None
    observed = evidence.evidence_sha256
    return FixtureReplayReport(
        verdict='reproduced' if expected == observed else 'diverged',
        spec_sha256=spec.spec_sha256,
        expected_evidence_sha256=expected,
        observed_evidence_sha256=observed,
        reason=None if expected == observed else (
            'evidence identity differs — nondeterministic provider or '
            'changed inputs'
        ),
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class StaticFixtureReplayProvider:
    """Provider bound to stored predictions — replay/unit-test anchor."""

    def __init__(
        self,
        predictions: dict[str, Any],
        *,
        provider_id: str = 'static-replay',
        provider_version: str = '1',
    ) -> None:
        self._predictions = dict(predictions)
        self.provider_id = provider_id
        self.provider_version = provider_version

    def predict(self, case: BenchmarkCase) -> dict[str, Any]:
        return dict(self._predictions)

    def config_sha256(self) -> str | None:
        return _hash(self._predictions)


class AnalyticDirectPathProvider:
    """Analytic specular-geometry provider (#939's analytic lane).

    Predicts ``arrival_timing`` observables as the free-field direct path
    ``distance / c`` from the measured source/receiver positions; the
    speed of sound is derived from the declared temperature
    (``c = 331.3 * sqrt(1 + T_C / 273.15)`` m/s, 343.0 m/s when no
    temperature is declared). Everything else stays absent → ``missing``.
    """

    provider_id = 'analytic-direct-path'
    provider_version = '1'

    def __init__(
        self,
        *,
        speed_of_sound_m_per_s: float | None = None,
    ) -> None:
        self._c = speed_of_sound_m_per_s

    def _speed_of_sound(self, case: BenchmarkCase) -> float:
        if self._c is not None:
            return self._c
        env = case.environment or {}
        temperature_c = env.get('temperature_c')
        if isinstance(temperature_c, (int, float)) and math.isfinite(
            float(temperature_c)
        ):
            return 331.3 * math.sqrt(1.0 + float(temperature_c) / 273.15)
        return 343.0

    def predict(self, case: BenchmarkCase) -> dict[str, Any]:
        c = self._speed_of_sound(case)
        sources = {p.point_id: p for p in case.sources}
        receivers = {p.point_id: p for p in case.receivers}
        predictions: dict[str, Any] = {}
        for observable in case.observables:
            if observable.kind != 'arrival_timing':
                continue
            source = sources.get(observable.source_id or '')
            receiver = receivers.get(observable.receiver_id or '')
            if (
                source is None
                or receiver is None
                or source.position_m is None
                or receiver.position_m is None
            ):
                continue
            distance = math.dist(source.position_m, receiver.position_m)
            predictions[observable.observable_id] = {
                'arrival_s': distance / c,
                'distance_m': distance,
                'speed_of_sound_m_per_s': c,
            }
        return predictions

    def config_sha256(self) -> str | None:
        return _hash(
            {
                'provider': self.provider_id,
                'model': 'free_field_direct_path',
                'speed_of_sound_m_per_s': self._c,
                'temperature_model': '331.3*sqrt(1+Tc/273.15)',
            }
        )


__all__ = [
    'EXTERNAL_FIXTURE_AUTHORITY_VERSION',
    'EXTERNAL_FIXTURE_SCHEMA_VERSION',
    'EXTERNAL_FIXTURE_RUN_SCHEMA_VERSION',
    'EXTERNAL_FIXTURE_EVIDENCE_SCHEMA_VERSION',
    'AnalyticDirectPathProvider',
    'ExternalBenchmarkFixture',
    'FixtureFileReceipt',
    'FixtureFileRole',
    'FixtureFileVerdict',
    'FixtureImportError',
    'FixtureIntegrityError',
    'FixtureMaterialBinding',
    'FixtureMeasurement',
    'FixtureObservableEvaluation',
    'FixtureObservableVerdict',
    'FixturePin',
    'FixturePoint',
    'FixtureReplayReport',
    'FixtureRunEvidence',
    'FixtureRunMode',
    'FixtureRunSpec',
    'FixtureRunStatus',
    'FixtureRuntime',
    'FixtureUnitSemantics',
    'FixturePhaseAuthority',
    'FixtureRedistribution',
    'ImportedFixtureCase',
    'StaticFixtureReplayProvider',
    'assert_fixture_payloads_verified',
    'build_external_benchmark_fixture',
    'build_fixture_run_spec',
    'evaluate_fixture_observable',
    'fixture_observable_blocker',
    'import_fixture_case',
    'impulse_window_reference',
    'magnitude_spectrum_reference',
    'measured_arrival_s',
    'measurement_ir',
    'read_sofa_measurement',
    'replay_fixture',
    'run_fixture',
    'runtime_descriptor',
    'verify_fixture_payloads',
]
