from __future__ import annotations

import hashlib
from dataclasses import asdict
import logging
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .acoustics import analyze_rectangular_context
from .comparison import ComparisonError, compare_frequency_responses
from .conditions import classify_differences, context_differences
from .database import SCHEMA_VERSION, DatasetIntegrityError, Store
from .features import FeatureDetectionError, detect_frequency_features, match_geometry_candidates
from .geometry import room_geometry_payload
from .models import AttachmentCreate, AttachmentKind, ComparisonCreate, ContextCreate, ImportPreviewRequest, MeasurementImportRequest, ProjectCreate, RewApiSnapshotImportRequest, SessionCreate
from .placement_constraints import ConstraintSetCreate, PlacementEvaluationRequest, evaluate_constraint_set, validate_constraint_set_for_context
from .readiness import evaluate_measurement_readiness
from .search_space import (
    MAX_SEARCH_PAGE_SIZE,
    SearchSpecCreate,
    generate_search_space,
    validate_search_spec,
)
from .report import build_report_payload, render_report_html
from .rew_api import DEFAULT_REW_API_URL, RewApiClient, RewApiError, RewApiNotFound, RewApiUnavailable
from .rew_parser import RewParseError, parse_rew_frequency_response


logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str
    version: str
    platform_target: str
    rew_required: bool
    measurement_hardware_required: bool
    schema_version: int


# The legacy browser backup/restore contract is retired (issue #332): it was a
# second, weaker backup authority beside the native .htdt-backup format, lacked
# archive/member extraction bounds and asset-hash verification, and its GET
# endpoint created state-changing archives outside the unsafe-method Origin
# policy. The routes stay registered so callers receive an explicit 410 Gone
# pointing at the supported authority instead of a bare 404.
BROWSER_BACKUP_RETIRED_DETAIL = (
    'The legacy browser backup/restore endpoints are retired. The supported '
    'backup authority is the native .htdt-backup archive: use the HTDT native '
    "application's data management or `HTDT.exe --backup/--restore` "
    '(`python -m htdt.native_cad --backup/--restore`).'
)


def _default_data_dir() -> Path:
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'HomeTheaterDigitalTwin'
    return Path.home() / '.home-theater-digital-twin'


def _small_file_sha256(path_value: str | None, *, max_bytes: int = 1024 * 1024) -> str | None:
    if not path_value:
        return None
    try:
        path = Path(path_value)
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(65536), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _comparison_eligibility(descriptor: dict) -> str | None:
    """Why a dataset is ineligible for an A/B comparison, or None.

    Same contract as feature-candidate matching: only measured evidence
    that is not flagged invalid may be compared.
    """
    if not descriptor['integrity_valid']:
        return 'dataset failed integrity verification'
    if descriptor['quality_status'] == 'invalid':
        return 'quality_status is invalid'
    if descriptor['evidence_type'] != 'measured':
        return f"evidence_type is {descriptor['evidence_type']}, not measured"
    return None


def _comparison_level_compatibility(
    descriptor_a: dict,
    descriptor_b: dict,
    reference_band: tuple[float, float] | None,
    forced: bool,
) -> str:
    """Verdict persisted verbatim into the comparison result.

    Values mirror ``cad_comparison_semantics.LevelCompatibility`` so reports
    read the same taxonomy on both surfaces. The web store holds no
    absolute-level reference authority (dataset metadata only carries the
    parser's ``level_reference`` token), so 'absolute_level_comparable' is
    never emitted here — an eligible pair is at best normalized-shape
    comparable. A forced pair stays diagnostic_only.
    """
    if forced:
        return 'diagnostic_only'
    reference_a = descriptor_a['dataset_metadata'].get('level_reference')
    reference_b = descriptor_b['dataset_metadata'].get('level_reference')
    if reference_a == reference_b or reference_band is not None:
        return 'normalized_shape_comparable'
    return 'diagnostic_only'


def _comparison_warnings(a: dict, b: dict, confounder_count: int) -> list[str]:
    warnings: list[str] = []
    for label, item in (('A', a), ('B', b)):
        status = item['quality_status']
        if status == 'invalid':
            warnings.append(f'{label} is marked invalid; keep only as reference, not evidence of improvement')
        elif status == 'warning':
            warnings.append(f'{label} has measurement quality warnings')
        elif status == 'unknown':
            warnings.append(f'{label} measurement quality is unknown')
        if item['evidence_type'] != 'measured':
            warnings.append(f'{label} evidence_type is {item["evidence_type"]}, not measured')
    if confounder_count:
        warnings.append(f'{confounder_count} unclassified context difference(s) may confound the A/B comparison')
    return warnings


# Methods the unknown-/api/* fallback answers 404 for. OPTIONS is excluded so
# preflights keep the router's default 405; TRACE/CONNECT likewise never
# reach a handler.
_API_FALLBACK_METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE']


# Store-level KeyError codes translated to the same operator-facing sentences
# sibling routes raise literally. str(KeyError) would leak the Python repr
# ("'project_not_found'") into the detail field instead.
_KEY_ERROR_DETAILS = {
    'project_not_found': 'Project not found',
    'parent_context_not_found': 'Parent context not found',
    'context_not_found': 'Context not found',
    'session_not_found': 'Session not found',
    'constraint_set_not_found': 'ConstraintSet not found',
    'dataset_not_found': 'Dataset not found',
    'measurement_not_found': 'Measurement not found',
}


def _key_error_detail(exc: KeyError) -> str:
    code = str(exc.args[0]) if exc.args else ''
    return _KEY_ERROR_DETAILS.get(code) or code or 'Not found'


def _is_api_path(path: str) -> bool:
    """True when a decoded SPA-fallback path is really an ``/api`` URL.

    ``{path:path}`` params arrive URL-decoded, so an encoded separator
    (``%2f``) is already unfolded here. The check is case-insensitive: a
    misspelled ``/API/...`` route must not silently serve ``index.html``
    either. Segment-exact so a legitimate ``/apix``-style SPA route still
    falls back.
    """
    lowered = path.lower()
    return lowered == 'api' or lowered.startswith('api/')


def _resolve_frontend_path(frontend_root: Path, path: str) -> Path | None:
    """Return the resolved file inside ``frontend_root`` for an SPA route path.

    ``frontend_root`` must already be resolved. The decoded ``path`` parameter
    may contain ``..`` segments, absolute paths, or Windows separators/drive
    prefixes that would escape the static root, so containment is proven on
    the resolved filesystem path before any file is served. Returns ``None``
    when the path cannot be served safely from the static root.
    """
    if not path:
        return None
    # Backslashes are separators on Windows; normalize them so that
    # traversal segments and drive/UNC prefixes cannot hide.
    normalized = path.replace('\\', '/')
    windows_path = PureWindowsPath(normalized)
    if windows_path.drive or windows_path.is_absolute() or normalized.startswith('/'):
        return None
    if '..' in PurePosixPath(normalized).parts:
        return None
    try:
        candidate = (frontend_root / normalized).resolve()
        if not candidate.is_relative_to(frontend_root) or not candidate.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


# #598: the legacy browser API writes ``htdt.sqlite3`` + ``assets/`` — a second
# data authority beside the native ``cad-scenes.sqlite3``.
# Retired: the installed product launches ``htdt.native_cad`` and never this
# app; this app survives only for tests/development against an explicitly
# isolated data root. Pointing it at the default data directory requires the
# HTDT_LEGACY_API opt-in so an everyday launch cannot silently recreate the
# legacy store in the live root.
LEGACY_API_ENV_VAR = 'HTDT_LEGACY_API'
#: Swagger UI + the OpenAPI document hand the whole route surface to anything
#: that reaches the loopback port, so they stay off unless explicitly opted
#: into for development/debugging.
LEGACY_API_DOCS_ENV_VAR = 'HTDT_LEGACY_API_DOCS'
LEGACY_API_DISABLED_DETAIL = (
    'The legacy browser API is retired and development-only. Its store '
    '(htdt.sqlite3 + assets/) is no longer the data authority; the native '
    'application (htdt.native_cad) owns cad-scenes.sqlite3. '
    'To exercise the legacy API for development, pass an explicit isolated '
    'data directory (e.g. `python -m htdt --data-dir <dir>`) or set '
    f'{LEGACY_API_ENV_VAR}=1 in the environment.'
)


class LegacyApiDisabledError(RuntimeError):
    """Raised when the legacy API would touch the default data directory."""


def _finite_safe_error_detail(value: object) -> object:
    """Make a pydantic error's echoed ``input`` JSON-serializable.

    Validation errors carry the offending input value. A rejected non-finite
    float (``Infinity``/``NaN``) cannot be encoded by the plain ``json``
    encoder FastAPI's default handler uses, so the 422 response itself would
    crash with ``ValueError`` and surface as a 500. Non-finite floats are
    rendered as their string form instead; everything else is untouched.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite_safe_error_detail(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_safe_error_detail(item) for item in value]
    return value


def create_app(data_dir: Path | None = None, rew_client: RewApiClient | None = None) -> FastAPI:
    if data_dir is None and os.environ.get(LEGACY_API_ENV_VAR) != '1':
        raise LegacyApiDisabledError(LEGACY_API_DISABLED_DETAIL)
    store = Store(data_dir or _default_data_dir())
    rew = rew_client or RewApiClient(os.environ.get('HTDT_REW_API_URL', DEFAULT_REW_API_URL))
    expose_docs = os.environ.get(LEGACY_API_DOCS_ENV_VAR) == '1'
    app = FastAPI(title='Home Theater Digital Twin', version=__version__,
                  docs_url='/api/docs' if expose_docs else None, redoc_url=None,
                  openapi_url='/api/openapi.json' if expose_docs else None)
    app.state.store = store
    app.state.rew = rew

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={'detail': _finite_safe_error_detail(jsonable_encoder(exc.errors()))},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Keep the traceback in the log (the custom handler replaces
        # ServerErrorMiddleware's own logging) while the body stays a
        # safe, uniform `{"detail": ...}` like every other error route.
        logger.exception('Unhandled error on %s %s', request.method, request.url.path)
        return JSONResponse(status_code=500, content={'detail': 'Internal Server Error'})

    @app.get('/api/health', response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status='ok', version=__version__, platform_target='Windows 11 x64', rew_required=False,
                              measurement_hardware_required=False, schema_version=SCHEMA_VERSION)

    @app.get('/api/integrity')
    def integrity() -> dict:
        problems = store.integrity_problems()
        return {'status': 'ok' if not problems else 'error', 'problems': problems}

    @app.get('/api/rew/status')
    def rew_status() -> dict:
        return rew.status()

    @app.get('/api/rew/audio-preflight')
    def rew_audio_preflight() -> dict:
        try:
            return rew.get_audio_preflight()
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'Unexpected REW API response: {exc}') from exc


    @app.get('/api/rew/roomsim/state')
    def rew_roomsim_state() -> dict:
        try:
            return RewApiClient.roomsim_snapshot_payload(rew.get_roomsim_snapshot())
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'Unexpected REW Room Simulator response: {exc}') from exc

    @app.get('/api/rew/roomsim/frequency-response')
    def rew_roomsim_frequency_response(
        mic_position: str = Query(default='Main', min_length=1, max_length=100),
        source: str | None = Query(default=None, max_length=100),
    ) -> dict:
        try:
            response = rew.get_roomsim_frequency_response(mic_position=mic_position, source_name=source)
            return RewApiClient.roomsim_response_payload(response)
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'Unexpected REW Room Simulator response: {exc}') from exc

    @app.get('/api/rew/measurements')
    def rew_measurements() -> list[dict]:
        try:
            return rew.list_measurements()
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'Unexpected REW API response: {exc}') from exc

    @app.get('/api/rew/measurements/{measurement_id}/frequency-response')
    def rew_frequency_response(
        measurement_id: str,
        ppo: int | None = Query(default=96, ge=1, le=384),
        unit: str = Query(default='SPL', min_length=1, max_length=32),
        smoothing: str | None = Query(default=None, max_length=32),
    ) -> dict:
        try:
            response = rew.get_frequency_response(measurement_id, ppo=ppo, unit=unit, smoothing=smoothing)
            return RewApiClient.response_payload(response)
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'Unexpected REW API response: {exc}') from exc

    @app.get('/api/projects')
    def list_projects() -> list[dict]:
        return store.list_projects()

    @app.post('/api/projects', status_code=201)
    def create_project(request: ProjectCreate) -> dict:
        return store.create_project(request.name)

    @app.get('/api/projects/{project_id}')
    def get_project(project_id: str) -> dict:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail='Project not found')
        return project

    @app.get('/api/projects/{project_id}/sessions')
    def list_sessions(project_id: str) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        return store.list_sessions(project_id)

    @app.post('/api/projects/{project_id}/sessions', status_code=201)
    def create_session(project_id: str, request: SessionCreate) -> dict:
        try:
            return store.create_session(project_id, request.purpose, request.started_at, request.notes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get('/api/projects/{project_id}/contexts')
    def list_contexts(project_id: str) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        return store.list_contexts(project_id)

    @app.get('/api/projects/{project_id}/contexts/{context_id}/geometry')
    def context_geometry(project_id: str, context_id: str) -> dict:
        context = store.get_context(project_id, context_id)
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            return room_geometry_payload(context['payload']['room'])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post('/api/projects/{project_id}/contexts', status_code=201)
    def create_context(project_id: str, request: ContextCreate) -> dict:
        try:
            return store.create_context(project_id, request.model_dump(mode='json'), request.parent_context_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get('/api/projects/{project_id}/constraint-sets')
    def list_constraint_sets(project_id: str, context_id: str | None = Query(default=None)) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        if context_id is not None and store.get_context(project_id, context_id) is None:
            raise HTTPException(status_code=404, detail='Context not found')
        return store.list_constraint_sets(project_id, context_id)

    @app.post('/api/projects/{project_id}/constraint-sets', status_code=201)
    def create_constraint_set(project_id: str, request: ConstraintSetCreate) -> dict:
        context = store.get_context(project_id, request.context_id)
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            spec = validate_constraint_set_for_context(request, context['payload'])
            return store.create_constraint_set(project_id, request.context_id, request.name, spec)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get('/api/projects/{project_id}/constraint-sets/{constraint_set_id}')
    def get_constraint_set(project_id: str, constraint_set_id: str) -> dict:
        record = store.get_constraint_set(project_id, constraint_set_id)
        if record is None:
            raise HTTPException(status_code=404, detail='ConstraintSet not found')
        return record

    @app.post('/api/projects/{project_id}/constraint-sets/{constraint_set_id}/evaluate')
    def evaluate_placement(project_id: str, constraint_set_id: str, request: PlacementEvaluationRequest) -> dict:
        record = store.get_constraint_set(project_id, constraint_set_id)
        if record is None:
            raise HTTPException(status_code=404, detail='ConstraintSet not found')
        if not record['integrity_valid']:
            raise HTTPException(status_code=409, detail='ConstraintSet integrity check failed')
        context = store.get_context(project_id, record['context_id'])
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            result = evaluate_constraint_set(context['payload'], record['spec'], request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**result, 'constraint_set_id': record['id'], 'constraint_set_spec_sha256': record['spec_sha256'],
                'context_id': record['context_id']}

    @app.get('/api/projects/{project_id}/search-specs')
    def list_search_specs(project_id: str, context_id: str | None = Query(default=None)) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        if context_id is not None and store.get_context(project_id, context_id) is None:
            raise HTTPException(status_code=404, detail='Context not found')
        return store.list_search_specs(project_id, context_id)

    @app.post('/api/projects/{project_id}/search-specs/preview')
    def preview_search_spec(project_id: str, request: SearchSpecCreate) -> dict:
        constraint_set = store.get_constraint_set(project_id, request.constraint_set_id)
        if constraint_set is None:
            raise HTTPException(status_code=404, detail='ConstraintSet not found')
        if not constraint_set['integrity_valid']:
            raise HTTPException(status_code=409, detail='ConstraintSet integrity check failed')
        context = store.get_context(project_id, constraint_set['context_id'])
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            spec, estimate = validate_search_spec(
                request, context['payload'], context_id=context['id'],
                constraint_set_id=constraint_set['id'], constraint_set_spec=constraint_set['spec'],
                constraint_set_spec_sha256=constraint_set['spec_sha256'],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {'classification': 'placement_search_space_preview', 'spec': spec, **estimate}

    @app.post('/api/projects/{project_id}/search-specs', status_code=201)
    def create_search_spec(project_id: str, request: SearchSpecCreate) -> dict:
        constraint_set = store.get_constraint_set(project_id, request.constraint_set_id)
        if constraint_set is None:
            raise HTTPException(status_code=404, detail='ConstraintSet not found')
        if not constraint_set['integrity_valid']:
            raise HTTPException(status_code=409, detail='ConstraintSet integrity check failed')
        context = store.get_context(project_id, constraint_set['context_id'])
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            spec, estimate = validate_search_spec(
                request, context['payload'], context_id=context['id'],
                constraint_set_id=constraint_set['id'], constraint_set_spec=constraint_set['spec'],
                constraint_set_spec_sha256=constraint_set['spec_sha256'],
            )
            record = store.create_search_spec(project_id, context['id'], constraint_set['id'], request.name, spec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc
        return {**record, 'estimate': estimate}

    @app.get('/api/projects/{project_id}/search-specs/{search_spec_id}')
    def get_search_spec(project_id: str, search_spec_id: str) -> dict:
        record = store.get_search_spec(project_id, search_spec_id)
        if record is None:
            raise HTTPException(status_code=404, detail='SearchSpec not found')
        return record

    @app.post('/api/projects/{project_id}/search-specs/{search_spec_id}/generate')
    def generate_search_candidates(
        project_id: str, search_spec_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=MAX_SEARCH_PAGE_SIZE),
    ) -> dict:
        record = store.get_search_spec(project_id, search_spec_id)
        if record is None:
            raise HTTPException(status_code=404, detail='SearchSpec not found')
        if not record['integrity_valid']:
            raise HTTPException(status_code=409, detail='SearchSpec integrity check failed')
        constraint_set = store.get_constraint_set(project_id, record['constraint_set_id'])
        if constraint_set is None:
            raise HTTPException(status_code=404, detail='ConstraintSet not found')
        if not constraint_set['integrity_valid']:
            raise HTTPException(status_code=409, detail='ConstraintSet integrity check failed')
        context = store.get_context(project_id, record['context_id'])
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            result = generate_search_space(
                context['payload'], record['spec'], search_spec_sha256=record['spec_sha256'],
                constraint_set_spec=constraint_set['spec'], constraint_set_spec_sha256=constraint_set['spec_sha256'],
                offset=offset, limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**result, 'search_spec_id': record['id'], 'search_spec_sha256': record['spec_sha256'],
                'constraint_set_id': constraint_set['id'], 'constraint_set_spec_sha256': constraint_set['spec_sha256'],
                'context_id': record['context_id']}

    @app.get('/api/projects/{project_id}/contexts/{context_id}/measurement-readiness')
    def measurement_readiness(project_id: str, context_id: str) -> dict:
        context = store.get_context(project_id, context_id)
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            preflight = rew.get_audio_preflight()
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'Unexpected REW API response: {exc}') from exc
        java = preflight.get('java') if isinstance(preflight.get('java'), dict) else None
        cal_path = java.get('input_cal_file') if java and isinstance(java.get('input_cal_file'), str) else None
        return evaluate_measurement_readiness(
            context['payload'],
            preflight,
            store.list_attachments(project_id),
            context_id=context_id,
            current_calibration_sha256=_small_file_sha256(cal_path),
        )

    @app.get('/api/projects/{project_id}/contexts/{context_id}/acoustics')
    def context_acoustics(project_id: str, context_id: str, max_hz: float = Query(default=300.0, gt=0, le=2000),
                          sound_speed_m_s: float = Query(default=343.0, gt=250, lt=400)) -> dict:
        context = store.get_context(project_id, context_id)
        if context is None:
            raise HTTPException(status_code=404, detail='Context not found')
        try:
            return analyze_rectangular_context(context['payload'], max_hz=max_hz, sound_speed_m_s=sound_speed_m_s)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post('/api/import/preview')
    def preview_import(request: ImportPreviewRequest) -> dict:
        try:
            raw = store.decode_base64(request.raw_base64)
            parsed = parse_rew_frequency_response(raw)
        except (ValueError, RewParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {'filename': request.filename, 'sha256': parsed.source_sha256, 'parser_version': parsed.parser_version,
                'points': len(parsed.frequency_hz), 'frequency_min_hz': parsed.frequency_hz[0], 'frequency_max_hz': parsed.frequency_hz[-1],
                'phase_status': parsed.phase_status, 'level_reference': parsed.level_reference, 'warnings': list(parsed.warnings),
                'header_lines': list(parsed.header_lines)}

    @app.post('/api/projects/{project_id}/rew-snapshots', status_code=201)
    def import_rew_snapshot(project_id: str, request: RewApiSnapshotImportRequest) -> dict:
        if store.get_context(project_id, request.context_id) is None:
            raise HTTPException(status_code=404, detail='Context not found')
        if request.session_id is not None and store.get_session(project_id, request.session_id) is None:
            raise HTTPException(status_code=404, detail='Session not found')
        try:
            snapshot = rew.get_frequency_response_snapshot(
                request.measurement_uuid, ppo=request.ppo, unit=request.unit, smoothing=request.smoothing
            )
            return store.import_rew_api_snapshot(
                project_id,
                request.context_id,
                snapshot,
                channel_role=request.channel_role,
                evidence_type=request.evidence_type,
                source_speaker_ids=request.source_speaker_ids,
                radiation_scope=request.radiation_scope,
                routing_evidence=request.routing_evidence,
                notes=request.notes,
                quality_status=request.quality_status,
                quality_reasons=request.quality_reasons,
                quality_source=request.quality_source,
                repeat_group=request.repeat_group,
                session_id=request.session_id,
                api_base_url=rew.base_url,
            )
        except RewApiUnavailable as exc:
            raise HTTPException(status_code=503, detail=f'REW API unavailable: {exc}') from exc
        except RewApiNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RewApiError as exc:
            raise HTTPException(status_code=502, detail=f'REW snapshot rejected: {exc}') from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get('/api/projects/{project_id}/measurements')
    def list_measurements(project_id: str, context_id: str | None = Query(default=None),
                          session_id: str | None = Query(default=None)) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        try:
            return store.list_measurements(project_id, context_id, session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.post('/api/projects/{project_id}/measurements', status_code=201)
    def import_measurement(project_id: str, request: MeasurementImportRequest) -> dict:
        try:
            raw = store.decode_base64(request.raw_base64)
            return store.import_measurement(project_id=project_id, context_id=request.context_id, filename=request.filename, raw=raw,
                                            channel_role=request.channel_role, evidence_type=request.evidence_type,
                                            source_speaker_ids=request.source_speaker_ids, radiation_scope=request.radiation_scope,
                                            captured_at=request.captured_at, notes=request.notes, routing_evidence=request.routing_evidence,
                                            quality_status=request.quality_status, quality_reasons=request.quality_reasons,
                                            quality_source=request.quality_source, repeat_group=request.repeat_group,
                                            session_id=request.session_id)
        except (ValueError, RewParseError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get('/api/projects/{project_id}/datasets/{dataset_id}/feature-candidates')
    def feature_candidates(
        project_id: str,
        dataset_id: str,
        low_hz: float = Query(default=20.0, gt=0, lt=2000),
        high_hz: float = Query(default=300.0, gt=0, le=2000),
        prominence_db: float = Query(default=3.0, gt=0, le=30),
        baseline_window_octaves: float = Query(default=1 / 3, gt=0, le=2),
        min_spacing_octaves: float = Query(default=1 / 12, ge=0, le=1),
        match_tolerance_octaves: float = Query(default=1 / 12, gt=0, le=1),
        sound_speed_m_s: float = Query(default=343.0, gt=250, lt=400),
    ) -> dict:
        if high_hz <= low_hz:
            raise HTTPException(status_code=422, detail='high_hz must be greater than low_hz')
        try:
            descriptor = store.get_dataset_descriptor(dataset_id)
            if descriptor['project_id'] != project_id:
                raise KeyError('dataset_not_found')
            if not descriptor['integrity_valid']:
                raise HTTPException(status_code=409, detail='Dataset failed integrity verification')
            response = store.get_frequency_response(dataset_id)
            detection = detect_frequency_features(
                response,
                low_hz=low_hz,
                high_hz=high_hz,
                prominence_db=prominence_db,
                baseline_window_octaves=baseline_window_octaves,
                min_spacing_octaves=min_spacing_octaves,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc
        except DatasetIntegrityError as exc:
            raise HTTPException(status_code=409, detail='Dataset failed integrity verification') from exc
        except FeatureDetectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        warnings: list[str] = []
        if descriptor['quality_status'] == 'invalid':
            warnings.append('Measurement is marked invalid; geometry candidate matching is disabled')
        elif descriptor['quality_status'] == 'warning':
            warnings.append('Measurement has quality warnings; candidate matches require manual review')
        elif descriptor['quality_status'] == 'unknown':
            warnings.append('Measurement quality is unknown; candidate matches are provisional')
        if descriptor['evidence_type'] != 'measured':
            warnings.append(f"evidence_type is {descriptor['evidence_type']}; automatic geometry candidate matching is disabled")

        eligible = descriptor['evidence_type'] == 'measured' and descriptor['quality_status'] != 'invalid'
        matches: list[dict] = []
        geometry_algorithm_version: str | None = None
        if eligible:
            try:
                geometry = analyze_rectangular_context(descriptor['context_payload'], max_hz=high_hz, sound_speed_m_s=sound_speed_m_s)
                geometry_algorithm_version = geometry['algorithm_version']
                matches = match_geometry_candidates(
                    detection['features'], geometry, tolerance_octaves=match_tolerance_octaves
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f'Geometry candidate matching unavailable: {exc}')

        return {
            'classification': 'candidate_association_not_causal_diagnosis',
            'dataset_id': dataset_id,
            'measurement': {
                'measurement_id': descriptor['measurement_id'],
                'context_id': descriptor['context_id'],
                'session_id': descriptor['session_id'],
                'channel_role': descriptor['channel_role'],
                'evidence_type': descriptor['evidence_type'],
                'quality_status': descriptor['quality_status'],
                'quality_reasons': descriptor['quality_reasons'],
                'quality_source': descriptor['quality_source'],
            },
            'feature_detection': detection,
            'eligible_for_candidate_matching': eligible,
            'candidate_matches': matches,
            'match_parameters': {
                'tolerance_octaves': match_tolerance_octaves,
                'sound_speed_m_s': sound_speed_m_s,
                'geometry_algorithm_version': geometry_algorithm_version,
            },
            'warnings': warnings,
        }

    @app.get('/api/projects/{project_id}/attachments')
    def list_attachments(project_id: str, context_id: str | None = Query(default=None),
                         measurement_id: str | None = Query(default=None),
                         kind: AttachmentKind | None = Query(default=None)) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        try:
            return store.list_attachments(project_id, context_id, measurement_id, kind)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.post('/api/projects/{project_id}/attachments', status_code=201)
    def create_attachment(project_id: str, request: AttachmentCreate) -> dict:
        try:
            raw = store.decode_base64(request.raw_base64)
            return store.attach_asset(project_id, request.filename, raw, request.kind, request.label, request.measurement_id, request.context_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc

    @app.get('/api/projects/{project_id}/comparisons')
    def list_comparisons(project_id: str) -> list[dict]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail='Project not found')
        return store.list_comparisons(project_id)

    def report_snapshot(project_id: str, comparison_id: str) -> tuple[dict, dict]:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail='Project not found')
        comparison = next((item for item in store.list_comparisons(project_id) if item['id'] == comparison_id), None)
        if comparison is None:
            raise HTTPException(status_code=404, detail='Comparison not found')
        return project, comparison

    @app.get('/api/projects/{project_id}/comparisons/{comparison_id}/report.json')
    def comparison_report_json(
        project_id: str,
        comparison_id: str,
        lang: Literal['en', 'ja'] = Query(default='en'),
    ) -> JSONResponse:
        project, comparison = report_snapshot(project_id, comparison_id)
        payload = build_report_payload(project, comparison, lang=lang)
        filename = f'htdt-comparison-{comparison_id[:8]}.json'
        return JSONResponse(payload, headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.get('/api/projects/{project_id}/comparisons/{comparison_id}/report.html')
    def comparison_report_html(
        project_id: str,
        comparison_id: str,
        lang: Literal['en', 'ja'] = Query(default='en'),
    ) -> HTMLResponse:
        project, comparison = report_snapshot(project_id, comparison_id)
        payload = build_report_payload(project, comparison, lang=lang)
        filename = f'htdt-comparison-{comparison_id[:8]}.html'
        return HTMLResponse(render_report_html(payload), headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.post('/api/projects/{project_id}/comparisons', status_code=201)
    def create_comparison(project_id: str, request: ComparisonCreate) -> dict:
        try:
            descriptor_a = store.get_dataset_descriptor(request.dataset_a_id)
            descriptor_b = store.get_dataset_descriptor(request.dataset_b_id)
            if descriptor_a['project_id'] != project_id or descriptor_b['project_id'] != project_id:
                raise KeyError('dataset_not_found')
            if not descriptor_a['integrity_valid'] or not descriptor_b['integrity_valid']:
                raise HTTPException(status_code=409, detail='Dataset failed integrity verification')
            eligibility = {
                'a': _comparison_eligibility(descriptor_a) or 'eligible',
                'b': _comparison_eligibility(descriptor_b) or 'eligible',
            }
            ineligible = [side for side, reason in eligibility.items() if reason != 'eligible']
            if ineligible and not request.force:
                detail = ', '.join(
                    f'dataset {side.upper()} ({reason})' for side, reason in eligibility.items() if reason != 'eligible'
                )
                raise HTTPException(
                    status_code=422,
                    detail=f'Ineligible for comparison: {detail}. '
                           'Resubmit with force=true for an explicitly diagnostic_only comparison.',
                )
            forced = bool(ineligible)
            a = store.get_frequency_response(request.dataset_a_id)
            b = store.get_frequency_response(request.dataset_b_id)
            reference_band = None
            if request.reference_low_hz is not None and request.reference_high_hz is not None:
                reference_band = (request.reference_low_hz, request.reference_high_hz)
            excluded = tuple((band.low_hz, band.high_hz) for band in request.excluded_bands)
            result = compare_frequency_responses(a, b, request.low_hz, request.high_hz,
                                                 reference_band_hz=reference_band, excluded_bands=excluded)
            differences = context_differences(descriptor_a['context_payload'], descriptor_b['context_payload'])
            classified = classify_differences(differences, request.expected_change_paths)
            same_repeat_group = bool(descriptor_a['repeat_group'] and descriptor_a['repeat_group'] == descriptor_b['repeat_group'])
            keys = ('measurement_id', 'context_id', 'session_id', 'channel_role', 'evidence_type', 'quality_status', 'quality_reasons', 'quality_source', 'repeat_group')
            result_payload = {**asdict(result), 'measurement_a': {key: descriptor_a[key] for key in keys},
                              'measurement_b': {key: descriptor_b[key] for key in keys},
                              'comparison_role': 'repeatability' if same_repeat_group else 'configuration_ab',
                              'level_compatibility': _comparison_level_compatibility(
                                  descriptor_a, descriptor_b, reference_band, forced
                              ),
                              'label_a': f"{descriptor_a['channel_role']} · {descriptor_a['evidence_type']} · R{descriptor_a['context_revision_number']}",
                              'label_b': f"{descriptor_b['channel_role']} · {descriptor_b['evidence_type']} · R{descriptor_b['context_revision_number']}",
                              'eligibility': eligibility,
                              'forced': forced,
                              'context_differences': differences, 'intended_changes': classified['intended'],
                              'confounders': classified['confounders'],
                              'interpretation_warnings': _comparison_warnings(descriptor_a, descriptor_b, len(classified['confounders']))}
            return store.save_comparison(project_id, request.dataset_a_id, request.dataset_b_id, request.model_dump(mode='json'), result_payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_key_error_detail(exc)) from exc
        except DatasetIntegrityError as exc:
            raise HTTPException(status_code=409, detail='Dataset failed integrity verification') from exc
        except ComparisonError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get('/api/backup', deprecated=True)
    def retired_browser_backup() -> None:
        raise HTTPException(status_code=410, detail=BROWSER_BACKUP_RETIRED_DETAIL)

    @app.post('/api/restore', deprecated=True)
    def retired_browser_restore() -> None:
        raise HTTPException(status_code=410, detail=BROWSER_BACKUP_RETIRED_DETAIL)

    frontend_dist = Path(__file__).resolve().parents[3] / 'frontend' / 'dist'
    if frontend_dist.is_dir():
        frontend_root = frontend_dist.resolve()
        assets_dir = frontend_root / 'assets'
        if assets_dir.is_dir():
            app.mount('/assets', StaticFiles(directory=assets_dir), name='assets')

        # Unknown /api/* paths must never fall through to the SPA fallback:
        # registered after every concrete /api/* route so only unmatched API
        # paths land here. Without this guard a misspelled API route is
        # answered 200 text/html (index.html) instead of a JSON 404, and the
        # gated-off /api/docs and /api/openapi.json would serve the app shell.
        @app.api_route('/api', methods=_API_FALLBACK_METHODS, include_in_schema=False)
        @app.api_route('/api/{path:path}', methods=_API_FALLBACK_METHODS, include_in_schema=False)
        def api_not_found(path: str = '') -> None:
            raise HTTPException(status_code=404, detail='Not Found')

        @app.get('/{path:path}', include_in_schema=False)
        def frontend(path: str) -> FileResponse:
            if _is_api_path(path):
                raise HTTPException(status_code=404, detail='Not Found')
            candidate = _resolve_frontend_path(frontend_root, path)
            if candidate is not None:
                return FileResponse(candidate)
            return FileResponse(frontend_root / 'index.html')
    else:
        @app.get('/', include_in_schema=False)
        def root() -> dict[str, str]:
            return {'name': 'Home Theater Digital Twin', 'status': 'backend-ready', 'ui': 'frontend/dist has not been built yet'}

    return app


# No module-level app instance: creating one eagerly would initialise the
# legacy store on import. ``htdt.server`` builds the ASGI app instead.
