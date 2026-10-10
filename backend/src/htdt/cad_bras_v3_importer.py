"""BRAS v3 ``GeneralFIR`` scene importer (#836 Action 2).

The #948 fixture harness reads ``SingleRoomSRIR`` SOFA payloads (RS8).
The v3 reference scenes RS1–RS7 (and CR1–CR4) publish ``GeneralFIR``
SOFA files instead — same AES69 container, different conventions: the
``M`` axis enumerates measured source/measurement-point combinations and
``ReceiverPosition``/``EmitterPosition`` carry per-measurement channel
positions. This module imports them verbatim:

- ``read_sofa_generalfir`` — lazy-h5py reader preserving the payload's
  own semantics (units, conventions, per-measurement positions, declared
  delays). Fails closed on non-GeneralFIR payloads, ambiguous units,
  non-metre positions, or malformed axes — nothing is inferred.
- ``BrasMaterialTable`` / ``read_bras_material_csv`` — sealed third-octave
  absorption/scattering tables from the corpus ``mat_*.csv`` files,
  pinned by content hash; ``estimate_kind`` keeps initial vs fitted
  (calibrated-inference) estimates honest.
- ``import_bras_v3_scene`` — verifies the scene zip against the sealed
  admission (#836 Action 1), imports every ``*.sofa`` member, and emits
  a normalized :class:`~htdt.cad_benchmark.BenchmarkCase` with measured
  source/receiver points. Room geometry is NOT invented: the corpus
  publishes it only as descriptive sketches (skp/png/pdf), so
  ``geometry`` stays ``None`` and ``limitations`` says so.
- ``BrasV3SceneImport`` — the sealed import record binding case,
  measurements and per-file receipts.

Payloads are never vendored: the case binds positions, sample rate and
content hashes — observable extraction is left to the metric layer
(#836 Action 3).
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import zipfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _hash
from .cad_benchmark import BenchmarkCase, BenchmarkPoint, BenchmarkSourceAsset
from .cad_equipment import EquipmentDataProvenance
from .cad_external_corpus_manifest import (
    CorpusScene,
    ExternalAssetAdmission,
)


class BrasImportError(RuntimeError):
    """Raised on any integrity or semantics failure — fail closed."""


_SHA256 = r'^[0-9a-f]{64}$'

_SOFA_METRE_UNITS = frozenset({'metre', 'meter', 'metres', 'meters', 'm'})
_SOFA_PRESSURE_UNITS = frozenset({'pascal', 'pa', 'pascals'})
_SOFA_CARTESIAN_TYPES = frozenset({'cartesian', 'cartesian_xyz'})
_SOFA_SPHERICAL_TYPES = frozenset({'spherical'})

GENERALFIR_IMPORTER_ID = 'bras-v3-generalfir-importer'
GENERALFIR_IMPORTER_VERSION = '1'

MaterialEstimateKind = Literal['initial_estimates', 'fitted_estimates']


class GeneralFirMeasurement(BaseModel):
    """One imported ``GeneralFIR`` SOFA measurement, semantics verbatim.

    ``Data.IR`` is ``M x R x N``: ``M`` enumerates measured
    source/measurement-point combinations, ``R`` receiver channels. In
    the v3 corpus ``ReceiverPosition``/``EmitterPosition`` carry
    per-measurement channel positions (shape ``channels x 3 x M``);
    ``SourcePosition``/``ListenerPosition`` describe the scene axes.
    The record binds the content hash of the exact IR samples — the
    samples themselves are never vendored into the case.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    sofa_conventions: str
    data_ir_units: str
    sample_rate_hz: float = Field(gt=0.0)
    measurement_count: int = Field(ge=1)
    channel_count: int = Field(ge=1)
    ir_length_samples: int = Field(ge=1)
    source_positions_m: tuple[tuple[float, float, float], ...] = ()
    listener_positions_m: tuple[tuple[float, float, float], ...] = ()
    emitter_positions_m: tuple[tuple[float, float, float], ...] = ()
    receiver_positions_m: tuple[tuple[float, float, float], ...] = ()
    delays_s: tuple[tuple[float, ...], ...] = ()
    coordinate_system: str
    measurement_sha256: str = Field(pattern=_SHA256)


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
    import numpy as np  # noqa: PLC0415

    if isinstance(value, np.ndarray):
        if value.size != 1:
            return None
        value = value.flat[0]
    return str(value)


def _to_triplets(
    raw: Any, *, what: str,
) -> tuple[tuple[float, float, float], ...]:
    rows: list[tuple[float, float, float]] = []
    for row in raw:
        triple = tuple(float(v) for v in row)
        if len(triple) != 3 or not all(math.isfinite(v) for v in triple):
            raise BrasImportError(f'{what} has a non-finite/malformed row')
        rows.append(triple)
    return tuple(rows)


def read_sofa_generalfir(
    source: Path | str,
    *,
    member_path: str | None = None,
    required_units: str | None = None,
    expected_coordinate_system: str | None = None,
) -> GeneralFirMeasurement:
    """Read one SOFA ``GeneralFIR`` payload, preserving semantics.

    ``source`` is a ``.sofa`` file or a zip archive with ``member_path``
    selecting the member. Fails closed (``BrasImportError``) on:
    missing/unreadable payload, non-GeneralFIR conventions, ambiguous
    ``Data.IR.Units``, non-metre/mistyped positions, non-3D IR data,
    non-finite samples, or a position axis incompatible with ``M``.
    """
    try:
        import h5py  # noqa: PLC0415 - lazy: only needed for .sofa payloads
    except ImportError as exc:  # pragma: no cover - environment guard
        raise BrasImportError(
            'h5py is required to import SOFA payloads (backend extra)'
        ) from exc

    source = Path(source)
    if member_path is not None:
        try:
            with zipfile.ZipFile(source) as archive:
                if member_path not in set(archive.namelist()):
                    raise BrasImportError(
                        f'sofa member {member_path} absent from {source.name}'
                    )
                payload = archive.read(member_path)
        except zipfile.BadZipFile as exc:
            raise BrasImportError(
                f'{source.name} is not a readable zip archive'
            ) from exc
        opener: Any = io.BytesIO(payload)
    else:
        if not source.is_file():
            raise BrasImportError(f'sofa payload missing: {source}')
        opener = source

    with h5py.File(opener, 'r') as sofa:
        conventions = _scalar_attr(sofa, 'SOFAConventions')
        if conventions is None or 'GeneralFIR' not in conventions:
            raise BrasImportError(
                f'not a GeneralFIR SOFA payload '
                f'(SOFAConventions={conventions!r})'
            )

        def _var(name: str) -> Any:
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
            raise BrasImportError('SOFA payload lacks Data.IR')
        units = _dataset_attr(ir_node, 'Units')
        if units is None or not units.strip():
            import re

            comment = _scalar_attr(sofa, 'Comment') or ''
            m = re.search(
                r'unit\s+of\s+Data\.IR\s+is\s+([^\.\[]+)', comment,
                flags=re.IGNORECASE,
            )
            units = m.group(1).strip() if m else None
        if units is None or not units.strip():
            raise BrasImportError(
                'Data.IR declares no Units — amplitude semantics unknown; '
                'refusing to guess calibration'
            )
        units = units.strip().rstrip('.').strip()
        if required_units == 'pascal_calibrated' and (
            units.lower() not in _SOFA_PRESSURE_UNITS
        ):
            raise BrasImportError(
                f'required pascal_calibrated units but payload '
                f'Data.IR.Units={units!r}'
            )

        rate_node = _var('Data.SamplingRate')
        if rate_node is None:
            raise BrasImportError('SOFA payload lacks Data.SamplingRate')
        import numpy as np  # noqa: PLC0415

        rate = float(np.asarray(rate_node[()]).reshape(-1)[0])
        if not (math.isfinite(rate) and rate > 0):
            raise BrasImportError('Data.SamplingRate is not a positive scalar')
        ir = np.asarray(ir_node[()])
        if ir.ndim != 3:
            raise BrasImportError(
                f'Data.IR must be MxRxN, got shape {ir.shape}'
            )
        if not bool(np.isfinite(ir).all()):
            raise BrasImportError('Data.IR contains non-finite samples')
        ir_sha = hashlib.sha256(ir.astype('<f8').tobytes()).hexdigest()
        measurement_count, channel_count, ir_length = ir.shape

        coordinate_system = 'cartesian'

        def _positions(
            group: str, what: str, *, required: bool,
        ) -> tuple[tuple[float, float, float], ...]:
            node = sofa.get(group)
            if node is None:
                if required:
                    raise BrasImportError(f'SOFA payload lacks {group}')
                return ()
            pos_type = (_dataset_attr(node, 'Type') or '').strip().lower()
            pos_units = (_dataset_attr(node, 'Units') or '').strip().lower()
            if expected_coordinate_system is not None:
                want = expected_coordinate_system.strip().lower()
                if 'cartesian' in want and pos_type not in _SOFA_CARTESIAN_TYPES:
                    raise BrasImportError(
                        f'{what}: convention {want!r} but payload '
                        f'{group}.Type={pos_type!r}'
                    )
                if 'spherical' in want and pos_type not in _SOFA_SPHERICAL_TYPES:
                    raise BrasImportError(
                        f'{what}: convention {want!r} but payload '
                        f'{group}.Type={pos_type!r}'
                    )
            if pos_units:
                tokens = [
                    t.strip() for t in pos_units.split(',') if t.strip()
                ]
                if not tokens or not all(
                    t in _SOFA_METRE_UNITS for t in tokens
                ):
                    raise BrasImportError(
                        f'{what}: {group}.Units={pos_units!r} is not metres'
                    )
            if pos_type:
                coordinate = pos_type
            raw = np.asarray(node[()]).reshape(-1, 3)
            return _to_triplets(raw.tolist(), what=what)

        # GeneralFIR carries scene axes + per-measurement channel axes.
        # SourcePosition/ListenerPosition may be scalar-per-scene or per-M;
        # ReceiverPosition/EmitterPosition may be channel x 3 x M.
        source_pos = _positions('SourcePosition', 'source positions',
                                required=False)
        listener_pos = _positions('ListenerPosition', 'listener positions',
                                  required=False)
        emitter_pos = _positions('EmitterPosition', 'emitter positions',
                                 required=False)
        receiver_pos = _positions('ReceiverPosition', 'receiver positions',
                                  required=False)
        # GeneralFIR needs at least one resolvable position axis per side.
        if not emitter_pos and not source_pos:
            raise BrasImportError(
                'SOFA payload carries neither EmitterPosition nor '
                'SourcePosition — source geometry unresolvable'
            )
        if not receiver_pos and not listener_pos:
            raise BrasImportError(
                'SOFA payload carries neither ReceiverPosition nor '
                'ListenerPosition — receiver geometry unresolvable'
            )
        for label, positions in (
            ('SourcePosition', source_pos),
            ('ListenerPosition', listener_pos),
            ('EmitterPosition', emitter_pos),
            ('ReceiverPosition', receiver_pos),
        ):
            if positions and len(positions) not in (1, measurement_count):
                raise BrasImportError(
                    f'{label} count {len(positions)} incompatible with '
                    f'Data.IR measurement axis {measurement_count}'
                )

        delays: tuple[tuple[float, ...], ...] = ()
        delay_node = _var('Data.Delay')
        if delay_node is not None:
            raw = np.asarray(delay_node[()])
            matrix = (
                raw.reshape(1, -1) if raw.ndim <= 1
                else raw.reshape(raw.shape[0], -1)
            )
            delays = tuple(
                tuple(float(v) / rate for v in row) for row in matrix.tolist()
            )

        node = sofa.get('ReceiverPosition') or sofa.get('ListenerPosition')
        if node is not None:
            coordinate_system = (
                _dataset_attr(node, 'Type') or 'cartesian'
            ).strip().lower()

        return GeneralFirMeasurement(
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
            coordinate_system=coordinate_system,
            measurement_sha256=ir_sha,
        )


def generalfir_ir(
    source: Path | str,
    *,
    member_path: str | None = None,
) -> Any:
    """Return the raw ``Data.IR`` array for reference extraction."""
    try:
        import h5py  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise BrasImportError('h5py/numpy required for IR access') from exc
    source = Path(source)
    if member_path is not None:
        with zipfile.ZipFile(source) as archive:
            if member_path not in set(archive.namelist()):
                raise BrasImportError(
                    f'sofa member {member_path} absent from {source.name}'
                )
            payload = archive.read(member_path)
        opener: Any = io.BytesIO(payload)
    else:
        opener = source
    with h5py.File(opener, 'r') as sofa:
        node = sofa.get('Data.IR')
        if node is None and 'Data' in sofa:
            node = sofa['Data'].get('IR')
        if node is None:
            raise BrasImportError('SOFA payload lacks Data.IR')
        return np.asarray(node[()])


# ---------------------------------------------------------------------------
# Material tables — ``3 Surface descriptions/_csv/...`` third-octave rows


class BrasMaterialTable(BaseModel):
    """One sealed third-octave material table from the corpus CSVs.

    ``estimate_kind`` keeps the corpus's own honesty distinction:
    ``initial_estimates`` are published first-pass values;
    ``fitted_estimates`` are calibrated-inference fits — inputs to an
    ``informed_calibrated`` run, never unfitted reference truth.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    material_id: str = Field(min_length=1)
    estimate_kind: MaterialEstimateKind
    source_file_name: str = Field(min_length=1)
    member_path: str | None = Field(default=None, min_length=1)
    band_hz: tuple[float, ...] = Field(min_length=1)
    absorption: tuple[float, ...] = Field(min_length=1)
    # ``None`` when the published table carries no scattering row — the
    # structured-geometry MDF variants ship bands + absorption only.
    scattering: tuple[float, ...] | None = None
    table_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode='after')
    def _check(self) -> 'BrasMaterialTable':
        if len(self.band_hz) != len(self.absorption):
            raise ValueError(
                'band/absorption rows must have equal length'
            )
        if self.scattering is not None and len(self.scattering) != len(self.band_hz):
            raise ValueError(
                'band/scattering rows must have equal length'
            )
        if any(b <= 0 or not math.isfinite(b) for b in self.band_hz):
            raise ValueError('band_hz must be positive finite')
        if sorted(self.band_hz) != list(self.band_hz):
            raise ValueError('band_hz must be ascending')
        rows = (('absorption', self.absorption),)
        if self.scattering is not None:
            rows = rows + (('scattering', self.scattering),)
        for label, row in rows:
            if any(not math.isfinite(v) or v < 0.0 or v > 1.0 for v in row):
                raise ValueError(f'{label} coefficients must be in [0, 1]')
        if self.table_sha256 != _hash(self.identity_payload()):
            raise ValueError('BrasMaterialTable hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'table_sha256'})


def read_bras_material_csv(
    source: Path | str,
    *,
    member_path: str | None = None,
    estimate_kind: MaterialEstimateKind | None = None,
) -> BrasMaterialTable:
    """Parse one corpus ``mat_*.csv``. Published tables carry either 3
    rows (band_hz/absorption/scattering) or — for the structured-geometry
    MDF variants — 2 rows (band_hz/absorption; ``scattering`` stays
    ``None``). Fails closed on non-numeric rows, ragged lengths,
    descending bands or out-of-range coefficients.

    ``estimate_kind`` defaults to the member's path segment
    (``initial_estimates``/``fitted_estimates``); a kind that cannot be
    resolved from either is a hard error — never guessed.
    """
    source = Path(source)
    if member_path is not None:
        try:
            with zipfile.ZipFile(source) as archive:
                if member_path not in set(archive.namelist()):
                    raise BrasImportError(
                        f'material member {member_path} absent from '
                        f'{source.name}'
                    )
                text = archive.read(member_path).decode('utf-8')
        except zipfile.BadZipFile as exc:
            raise BrasImportError(
                f'{source.name} is not a readable zip archive'
            ) from exc
        display_name = member_path
    else:
        if not source.is_file():
            raise BrasImportError(f'material csv missing: {source}')
        text = source.read_text(encoding='utf-8')
        display_name = source.name

    if estimate_kind is None:
        probe = member_path or str(source)
        if 'initial_estimates' in probe:
            estimate_kind = 'initial_estimates'
        elif 'fitted_estimates' in probe:
            estimate_kind = 'fitted_estimates'
        else:
            raise BrasImportError(
                f'cannot resolve estimate kind for {display_name} — '
                'declare it explicitly'
            )

    rows = [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text))
        if row and any(cell.strip() for cell in row)
    ]
    if len(rows) not in (2, 3):
        raise BrasImportError(
            f'{display_name}: expected 2–3 rows '
            '(bands/absorption[/scattering]), got '
            f'{len(rows)}'
        )

    def _numbers(row: list[str], what: str) -> tuple[float, ...]:
        values: list[float] = []
        for cell in row:
            try:
                values.append(float(cell))
            except ValueError as exc:
                raise BrasImportError(
                    f'{display_name}: non-numeric {what} value {cell!r}'
                ) from exc
        return tuple(values)

    band_hz = _numbers(rows[0], 'band')
    absorption = _numbers(rows[1], 'absorption')
    scattering = (
        _numbers(rows[2], 'scattering') if len(rows) == 3 else None
    )
    material_id = Path(display_name).stem

    probe = BrasMaterialTable.model_construct(
        material_id=material_id,
        estimate_kind=estimate_kind,
        source_file_name=source.name,
        member_path=member_path,
        band_hz=band_hz,
        absorption=absorption,
        scattering=scattering,
        table_sha256='0' * 64,
    )
    try:
        return BrasMaterialTable(
            **{**probe.model_dump(mode='python'),
               'table_sha256': _hash(probe.identity_payload())}
        )
    except Exception as exc:
        raise BrasImportError(
            f'{display_name}: invalid material table — {exc}'
        ) from exc


# ---------------------------------------------------------------------------
# Scene import — verify zip pins, enumerate SOFA members, emit BenchmarkCase


class BrasV3SofaMember(BaseModel):
    """One SOFA member inside a scene zip and its import disposition.

    ``imported`` members are ``GeneralFIR`` and carry a bound
    ``measurement_sha256``; other conventions (e.g.
    ``MultiSpeakerBRIR``) are honestly recorded as ``skipped`` with the
    declared convention — never imported under a wrong-typed reader.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    member_path: str = Field(min_length=1)
    role: Literal['rir', 'brir']
    variant: str = Field(min_length=1)
    disposition: Literal['imported', 'skipped']
    sofa_conventions: str | None = None
    skip_reason: str | None = None
    measurement_sha256: str | None = Field(default=None, pattern=_SHA256)


class BrasV3FileReceipt(BaseModel):
    """Verdict for one admitted archive the importer touched."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    file_name: str = Field(min_length=1)
    verdict: Literal['verified', 'unverifiable', 'size_mismatch',
                     'md5_mismatch', 'sha256_mismatch', 'missing']
    size_bytes: int | None = None
    detail: str | None = None


class BrasV3SceneImport(BaseModel):
    """Sealed import record: case + measurements + receipts."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    case: BenchmarkCase
    scene_id: str = Field(min_length=1)
    sofa_members: tuple[BrasV3SofaMember, ...]
    measurements: tuple[GeneralFirMeasurement, ...]
    materials: tuple[BrasMaterialTable, ...] = ()
    receipts: tuple[BrasV3FileReceipt, ...]


def _verify_zip_against_admission(
    root: Path,
    dataset: ExternalAssetAdmission,
    file_name: str,
) -> BrasV3FileReceipt:
    admitted = {f.file_name: f for f in dataset.files}
    entry = admitted.get(file_name)
    if entry is None:
        raise BrasImportError(
            f'{file_name} is not an admitted file of {dataset.admission_id}'
        )
    path = root / dataset.dataset_name / file_name
    if not path.is_file():
        return BrasV3FileReceipt(
            file_name=file_name, verdict='missing')
    size = path.stat().st_size
    if size != entry.size_bytes:
        return BrasV3FileReceipt(
            file_name=file_name, verdict='size_mismatch',
            size_bytes=size)
    md5 = hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - publisher pin
    if entry.md5 and md5 != entry.md5:
        return BrasV3FileReceipt(
            file_name=file_name, verdict='md5_mismatch',
            size_bytes=size)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if entry.sha256 is not None and sha != entry.sha256:
        return BrasV3FileReceipt(
            file_name=file_name, verdict='sha256_mismatch',
            size_bytes=size)
    if entry.sha256 is None:
        return BrasV3FileReceipt(
            file_name=file_name, verdict='unverifiable',
            size_bytes=size,
            detail='no sha256 pin — publisher md5 matched')
    return BrasV3FileReceipt(
        file_name=file_name, verdict='verified', size_bytes=size)


def _scene_zip_name(scene: CorpusScene, dataset: ExternalAssetAdmission) -> str:
    scene_files = {f.file_name for f in dataset.files}
    candidates = [
        name
        for name in scene.asset_file_names
        if name.lower().endswith('.zip')
        and 'scene_descriptions' in name.lower()
        and name in scene_files
    ]
    if len(candidates) != 1:
        raise BrasImportError(
            f'scene {scene.scene_id}: cannot resolve its scene zip '
            f'member (candidates={candidates!r})'
        )
    return candidates[0]


def _sofa_role(member_path: str) -> str:
    lowered = member_path.lower()
    if '/brirs/' in lowered or '_brirs' in lowered:
        return 'brir'
    return 'rir'


def import_bras_v3_scene(
    dataset: ExternalAssetAdmission,
    scene: CorpusScene,
    root: Path | str,
    *,
    importer_version: str = GENERALFIR_IMPORTER_VERSION,
    coordinate_convention: str = 'cartesian',
) -> BrasV3SceneImport:
    """Import one BRAS v3 scene into a normalized ``BenchmarkCase``.

    Verifies the scene zip against the sealed admission, imports every
    ``*.sofa`` member as a ``GeneralFirMeasurement``, and emits a case
    whose sources/receivers come from the *measured* positions of the
    first RIR member. Room geometry stays ``None``: the corpus publishes
    it only descriptively — the limitations text says so honestly.
    """
    if scene.dataset_ref.ref_id != dataset.admission_id:
        raise BrasImportError(
            f'scene {scene.scene_id} pins a different dataset admission'
        )
    if scene.dataset_ref.ref_sha256 != dataset.semantic_sha256:
        raise BrasImportError('dataset admission hash drifted from the scene pin')
    root = Path(root)
    zip_name = _scene_zip_name(scene, dataset)
    receipt = _verify_zip_against_admission(root, dataset, zip_name)
    if receipt.verdict not in ('verified', 'unverifiable'):
        raise BrasImportError(
            f'scene zip {zip_name} failed verification: {receipt.verdict}'
        )
    zip_path = root / dataset.dataset_name / zip_name
    with zipfile.ZipFile(zip_path) as archive:
        members = sorted(
            name for name in archive.namelist()
            if name.lower().endswith('.sofa') and not name.startswith('__MACOSX')
        )
        if not members:
            raise BrasImportError(
                f'{zip_name} contains no SOFA members to import'
            )
        wav_members = sorted(
            name for name in archive.namelist()
            if name.lower().endswith('.wav') and not name.startswith('__MACOSX')
        )

    measurements: list[GeneralFirMeasurement] = []
    sofa_members: list[BrasV3SofaMember] = []
    imported_pairs: list[tuple[BrasV3SofaMember, GeneralFirMeasurement]] = []
    for member in members:
        variant = Path(member).stem
        try:
            measurement = read_sofa_generalfir(
                zip_path, member_path=member,
                expected_coordinate_system=coordinate_convention,
            )
        except BrasImportError as exc:
            # A non-GeneralFIR convention (e.g. MultiSpeakerBRIR) is a
            # different observable class — record it honestly instead of
            # importing under the wrong-typed reader. Other failures
            # (corruption, missing axes) still propagate.
            if 'not a GeneralFIR SOFA payload' in str(exc):
                sofa_members.append(
                    BrasV3SofaMember(
                        member_path=member,
                        role=_sofa_role(member),
                        variant=variant,
                        disposition='skipped',
                        sofa_conventions=str(exc).rsplit('(', 1)[-1].rstrip(')'),
                        skip_reason=str(exc),
                    )
                )
                continue
            raise
        measurements.append(measurement)
        member_record = BrasV3SofaMember(
            member_path=member,
            role=_sofa_role(member),
            variant=variant,
            disposition='imported',
            measurement_sha256=measurement.measurement_sha256,
        )
        sofa_members.append(member_record)
        imported_pairs.append((member_record, measurement))

    if not imported_pairs:
        raise BrasImportError(
            f'{zip_name}: no GeneralFIR SOFA members could be imported'
        )
    primary_member, primary_measurement = next(
        (pair for pair in imported_pairs if pair[0].role == 'rir'),
        imported_pairs[0],
    )

    emitter = primary_measurement.emitter_positions_m or ()
    receiver = primary_measurement.receiver_positions_m or ()
    sources = tuple(
        BenchmarkPoint(
            point_id=f'S{i + 1}',
            position_m=pos,
            role='measured emitter position',
        )
        for i, pos in enumerate(emitter or primary_measurement.source_positions_m)
    )
    receivers = tuple(
        BenchmarkPoint(
            point_id=f'R{i + 1}',
            position_m=pos,
            role='measured receiver position',
        )
        for i, pos in enumerate(receiver or primary_measurement.listener_positions_m)
    )

    provenance = EquipmentDataProvenance(
        evidence_kind='measured',
        source_name=dataset.publisher,
        source_version=(
            dataset.version_doi or dataset.version_record_id or 'unversioned'
        ),
        source_reference=dataset.record_uri,
        source_sha256=dataset.semantic_sha256,
    )
    asset_probe = BenchmarkSourceAsset.model_construct(
        asset_id=f'corpus:{dataset.admission_id}:{scene.scene_id}',
        dataset_name=dataset.dataset_name,
        dataset_version=(
            dataset.version_doi or dataset.version_record_id or 'unversioned'
        ),
        origin_uri=dataset.record_uri,
        content_sha256=None,
        license_id=dataset.license_id,
        admission_ref=dataset.admission_id,
        evidence_class='external_measured',
        provenance=(provenance,),
        semantic_sha256='0' * 64,
    )
    source_asset = BenchmarkSourceAsset(
        **{**asset_probe.model_dump(mode='python', exclude={'semantic_sha256'}),
           'semantic_sha256': _hash(asset_probe.semantic_payload())}
    )

    limitations = '; '.join(
        bit for bit in (
            'licensed external measured data — payloads never vendored',
            f'license={dataset.license_id}',
            'room geometry is descriptive-only in the corpus '
            '(skp/png/pdf) — not machine-imported',
            scene.applicability_notes or '',
        ) if bit
    )
    case_probe = BenchmarkCase.model_construct(
        schema_version=1,
        benchmark_id=f'bras-{dataset.admission_id}-{scene.scene_id.lower()}',
        version=importer_version,
        title=f'{scene.title} ({dataset.dataset_name})',
        source_asset=source_asset,
        coordinate_convention=coordinate_convention,
        geometry=None,
        sources=sources,
        receivers=receivers,
        materials=None,
        environment={
            'sofa_conventions': primary_measurement.sofa_conventions,
            'data_ir_units': primary_measurement.data_ir_units,
            'sofa_member_count': len(sofa_members),
            'wav_member_count': len(wav_members),
        },
        sample_rate_hz=primary_measurement.sample_rate_hz,
        frequency_grid_hz=None,
        time_origin_s=0.0,
        preprocessing='none — measured RIR imported verbatim',
        limitations=limitations,
        observables=(),
        importer_id=GENERALFIR_IMPORTER_ID,
        importer_version=importer_version,
        semantic_sha256='0' * 64,
    )
    case = BenchmarkCase(
        **{**case_probe.model_dump(mode='python', exclude={'semantic_sha256'}),
           'semantic_sha256': _hash(case_probe.semantic_payload())}
    )
    return BrasV3SceneImport(
        case=case,
        scene_id=scene.scene_id,
        sofa_members=tuple(sofa_members),
        measurements=tuple(measurements),
        receipts=(receipt,),
    )


__all__ = [
    'BrasImportError',
    'BrasMaterialTable',
    'BrasV3FileReceipt',
    'BrasV3SceneImport',
    'BrasV3SofaMember',
    'GENERALFIR_IMPORTER_ID',
    'GENERALFIR_IMPORTER_VERSION',
    'GeneralFirMeasurement',
    'MaterialEstimateKind',
    'generalfir_ir',
    'import_bras_v3_scene',
    'read_bras_material_csv',
    'read_sofa_generalfir',
]
