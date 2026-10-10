"""Versioned Reference Theater self-test fixture + deterministic lane (#891).

The Reference Theater is a bounded, real-shaped synthetic project — room
geometry, speaker/listener/screen layout, equipment declarations, material
and synthetic-measurement evidence, example optimization candidates and a
simulated device deploy target — shipped as a versioned, content-addressed
payload pinned by a sealed manifest. It opens on a fresh install without
any external proprietary assets and carries no customer/private data.

Pieces:

- :class:`ReferenceTheaterManifest` — sealed, versioned fixture authority:
  payload relpath + payload sha256, expected scene content sha, expected
  export semantic sha, provenance/licence labels (mirroring #836 corpus
  conventions), the declared self-test lane, and the #867 benchmark
  manifest entry.
- ``reference_theater/reference_theater_v1.json`` — the content-addressed
  payload. ``load_reference_theater_payload`` verifies the file's sha256
  against the manifest pin before parsing; any drift fails closed with
  :class:`ReferenceTheaterDriftError` — a fixture bump requires an explicit
  version + expected-result update, never a silent edit.
- :class:`CadReferenceTheaterRun` — sealed evidence for one self-test lane
  run, persisted by ``CadReferenceTheaterRepository`` in the
  ``cad_reference_theater_runs`` table (native schema v111). The lane is
  ``project open → authority validation → analysis → compare → simulated
  deploy → verify → export``; every step's observed result is compared to
  the documented expected outcome/tolerance and any mismatch, unverifiable
  state or drift verdicts the whole run negatively.

Honesty: the deploy lane runs against ``FakeAvrLanTransport`` — it is
*simulated* evidence only, marked ``deploy_is_simulated`` on the run
record, and never claims real device deployment (reuses the #878
evidence-strength vocabulary).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_json, canonical_sha256
from .cad_authority_resolver import AuthorityRef
from .cad_delegated_provider import _require_iso8601, _seal
from .cad_scene import SceneDocument, scene_content_hash


# ---------------------------------------------------------------------------
# Contract vocabulary
# ---------------------------------------------------------------------------

REFERENCE_THEATER_AUTHORITY_VERSION = 'reference-theater-1'
REFERENCE_THEATER_FIXTURE_VERSION = 'rt-v1'
REFERENCE_THEATER_PAYLOAD_RELPATH = 'reference_theater/reference_theater_v1.json'
REFERENCE_THEATER_PAYLOAD_FORMAT = 'htdt-reference-theater-1'
REFERENCE_THEATER_REPORT_FORMAT = 'htdt-reference-theater-selftest-1'

#: The deterministic self-test lane, in order (#891).
REFERENCE_THEATER_STEPS: tuple[str, ...] = (
    'open',
    'authority',
    'analysis',
    'compare',
    'deploy',
    'verify',
    'export',
)

ReferenceTheaterStep = Literal[
    'open', 'authority', 'analysis', 'compare', 'deploy', 'verify', 'export',
]

ReferenceTheaterStepVerdict = Literal[
    'verified',    # observed outcome matches the declared expectation
    'diverged',    # observed outcome contradicts it — fixture/behaviour drift
    'unverified',  # the step ran but its evidence is not checkable
    'unauthorized',  # the step required operator authorization it did not get
    'blocked',     # the step could not reach a verdict
    'skipped',     # an earlier unrecoverable step verdict made it moot
]

ReferenceTheaterOutcome = Literal[
    'passed',          # every step verified
    'failed',          # at least one step diverged
    'unauthorized',    # the deploy step lacked --authorize-apply
    'blocked',         # the lane could not complete
    'fixture_drift',   # payload/manifest/content-sha drift detected
]

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


class ReferenceTheaterError(RuntimeError):
    """Base failure for the Reference Theater lane."""


class ReferenceTheaterDriftError(ReferenceTheaterError):
    """The packaged fixture payload no longer matches its manifest pin."""


# ---------------------------------------------------------------------------
# Payload model — what ships inside reference_theater_v1.json
# ---------------------------------------------------------------------------

class ReferenceTheaterProvenance(BaseModel):
    """Provenance/licence labels mirroring the #836 corpus conventions."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authored_by: str = 'htdt-engineering'
    license: str = 'htdt-internal'
    private_data: Literal['none'] = 'none'
    proprietary_assets: tuple[str, ...] = ()
    synthetic_evidence: Literal[True] = True
    rights_note: str = Field(min_length=1)


class ReferenceTheaterExpectations(BaseModel):
    """Documented expected outcomes + tolerances for the lane steps."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    # analysis: derived scene facts that must match exactly.
    analysis: dict[str, Any] = Field(default_factory=dict)
    # compare: which declared optimization candidate must win.
    compare: dict[str, Any] = Field(default_factory=dict)
    # deploy: expected pipeline stage + maximum evidence strength.
    deploy: dict[str, Any] = Field(default_factory=dict)
    # verify: expected deployed channel gains (dB) + tolerance.
    verify: dict[str, Any] = Field(default_factory=dict)
    # export: required report fields + minimum artifact bytes.
    export: dict[str, Any] = Field(default_factory=dict)


class ReferenceTheaterPayload(BaseModel):
    """The versioned fixture payload (``reference_theater_v1.json``)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    format: Literal['htdt-reference-theater-1'] = (
        REFERENCE_THEATER_PAYLOAD_FORMAT
    )
    fixture_version: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = ''
    # The real-shaped project content (SceneDocument payload).
    scene: dict[str, Any]
    # Declared (non-authority) fixture sections — equipment, material and
    # synthetic measurement evidence stay payload-level declarations; the
    # lane never promotes them into sealed measurement authority.
    equipment: tuple[dict[str, Any], ...] = ()
    materials: tuple[dict[str, Any], ...] = ()
    measurement_evidence: tuple[dict[str, Any], ...] = ()
    optimization_candidates: tuple[dict[str, Any], ...] = ()
    # Simulated deploy target: a HeadlessDeploymentSpec-shaped document.
    deploy: dict[str, Any] = Field(default_factory=dict)
    expected: ReferenceTheaterExpectations = Field(
        default_factory=ReferenceTheaterExpectations)
    provenance: ReferenceTheaterProvenance | None = None


def reference_theater_payload_path() -> Path:
    """Filesystem path of the shipped fixture payload."""

    return Path(__file__).resolve().parent / REFERENCE_THEATER_PAYLOAD_RELPATH


def reference_theater_payload_sha256() -> str:
    """Canonical sha256 of the payload's parsed content — the content
    address. Canonical hashing (the corpus-manifest convention) pins
    semantic content and is independent of checkout line endings."""

    import json

    return canonical_sha256(json.loads(
        reference_theater_payload_path().read_bytes().decode('utf-8')))


# ---------------------------------------------------------------------------
# Sealed manifest — the versioned fixture authority
# ---------------------------------------------------------------------------


class ReferenceTheaterManifest(BaseModel):
    """Sealed version pin for the Reference Theater fixture.

    ``payload_sha256`` is the canonical content address of the shipped
    payload file; ``expected_scene_sha256`` and ``expected_export_sha256``
    pin the derived authorities the lane must reproduce — a change in
    either pins an explicit fixture version bump.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    manifest_id: str
    manifest_sha256: str
    fixture_id: str = Field(min_length=1)
    fixture_version: str = Field(min_length=1)
    payload_relpath: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_scene_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_export_sha256: str = Field(pattern=_SHA256_PATTERN)
    lane: tuple[str, ...] = REFERENCE_THEATER_STEPS
    provenance: ReferenceTheaterProvenance
    # #867 hook: the versioned fixture exposed as a stable benchmark
    # fixture entry (manifest entry, not a new benchmark).
    benchmark_fixture_id: str = Field(min_length=1)
    benchmark_size: Literal['small', 'medium', 'large'] = 'small'
    # #886 hook: whether the first-run wizard may offer the fixture.
    onboarding_offer: bool = True
    authority_version: str = REFERENCE_THEATER_AUTHORITY_VERSION

    @model_validator(mode='after')
    def valid_manifest(self) -> 'ReferenceTheaterManifest':
        if self.fixture_id != (
            f'reference-theater:{self.fixture_version}'):
            raise ValueError(
                'fixture_id must be reference-theater:<fixture_version>')
        if tuple(self.lane) != REFERENCE_THEATER_STEPS:
            raise ValueError('lane must match REFERENCE_THEATER_STEPS')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('manifest_id', None)
        payload.pop('manifest_sha256', None)
        return payload


def build_reference_theater_manifest(
    **payload: Any,
) -> ReferenceTheaterManifest:
    return _seal(
        ReferenceTheaterManifest,
        payload,
        'manifest_id', 'manifest_sha256', 'rtman',
    )


# Pinned at fixture authoring time (2026-10-08, rt-v1): payload canonical
# content sha, the derived SceneDocument content hash, and the derived
# calibration export semantic hash. Bumping any of them requires an
# explicit fixture version bump — see
# docs/issues/issue-891-reference-theater.md.
_RT_PAYLOAD_SHA256 = (
    '0f5cc4c608bc5489ad8fbffeaf82be04a643575f649b6bd04ce5b8d13acf2111'
)
_RT_SCENE_SHA256 = (
    '5ec13666222fb1e9c2e15d68e5c5f8292816e5f9958a95b00f9a03e1adb51150'
)
_RT_EXPORT_SHA256 = (
    'c0918150491bff593eea6636fa05c1c8d112f2e6255f00b46f808d67d0669d8e'
)

REFERENCE_THEATER_MANIFEST = build_reference_theater_manifest(
    fixture_id='reference-theater:rt-v1',
    fixture_version=REFERENCE_THEATER_FIXTURE_VERSION,
    payload_relpath=REFERENCE_THEATER_PAYLOAD_RELPATH,
    payload_sha256=_RT_PAYLOAD_SHA256,
    expected_scene_sha256=_RT_SCENE_SHA256,
    expected_export_sha256=_RT_EXPORT_SHA256,
    provenance={
        'authored_by': 'htdt-engineering',
        'license': 'htdt-internal',
        'rights_note': (
            'Fully synthetic fixture authored by the HTDT project; '
            'contains no customer, private or proprietary data and no '
            'external proprietary assets.'),
    },
    benchmark_fixture_id='htdt-reference-theater-v1',
    benchmark_size='small',
)


def reference_theater_manifest() -> ReferenceTheaterManifest:
    return REFERENCE_THEATER_MANIFEST


def load_reference_theater_payload() -> ReferenceTheaterPayload:
    """Load the shipped fixture payload, verifying its manifest pin first.

    Any drift — bytes changed, version mismatch, malformed content — fails
    closed with :class:`ReferenceTheaterDriftError` before the payload is
    ever usable.
    """

    manifest = REFERENCE_THEATER_MANIFEST
    path = reference_theater_payload_path()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReferenceTheaterDriftError(
            f'reference theater payload missing: {exc}') from exc
    import json

    try:
        document = json.loads(raw.decode('utf-8'))
    except Exception as exc:  # error-boundary: error translation — a JSON parse failure wraps as ReferenceTheaterDriftError with the original failure preserved via 'from exc' (noqa: BLE001)
        raise ReferenceTheaterDriftError(
            f'reference theater payload is not parseable JSON: {exc}'
        ) from exc
    observed = canonical_sha256(document)
    if observed != manifest.payload_sha256:
        raise ReferenceTheaterDriftError(
            'reference theater payload sha256 drift: '
            f'expected {manifest.payload_sha256}, got {observed}')
    try:
        payload = ReferenceTheaterPayload.model_validate(document)
    except Exception as exc:  # error-boundary: error translation — a payload-validation failure wraps as ReferenceTheaterDriftError with the original failure preserved via 'from exc' (noqa: BLE001)
        raise ReferenceTheaterDriftError(
            f'reference theater payload failed validation: {exc}') from exc
    if payload.fixture_version != manifest.fixture_version:
        raise ReferenceTheaterDriftError(
            'reference theater fixture version drift: '
            f'manifest {manifest.fixture_version}, '
            f'payload {payload.fixture_version}')
    if payload.provenance is None:
        raise ReferenceTheaterDriftError(
            'reference theater payload carries no provenance block')
    if payload.provenance.private_data != 'none':
        raise ReferenceTheaterDriftError(
            'reference theater payload declares private data')
    return payload


def reference_theater_available() -> bool:
    """Whether the fixture is present and verifies against the manifest."""

    try:
        load_reference_theater_payload()
    except ReferenceTheaterError:
        return False
    return True


def reference_theater_benchmark_entry() -> dict[str, Any]:
    """#867 hook: this fixture exposed as a stable benchmark manifest entry.

    The entry mirrors ``perf_fixtures.expected_fixture_identity`` shape so
    the perf harness can pin the versioned theater scene without a new
    benchmark definition.
    """

    manifest = REFERENCE_THEATER_MANIFEST
    payload = load_reference_theater_payload()
    document = SceneDocument.model_validate(payload.scene)
    return {
        'fixture_id': manifest.benchmark_fixture_id,
        'size': manifest.benchmark_size,
        'entity_count': len(document.entities),
        'revision_count': 1,
        'document_id': payload.document_id,
        'document_sha256': scene_content_hash(document),
        'generator_version': REFERENCE_THEATER_AUTHORITY_VERSION,
    }


# ---------------------------------------------------------------------------
# Materialization — open the fixture as a real project
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceTheaterOpenResult:
    document_id: str
    project_id: str
    revision_id: str
    content_sha256: str
    created: bool


def materialize_reference_theater(
    scene_repository: Any,
    library: Any,
) -> ReferenceTheaterOpenResult:
    """Materialize (or verify + reopen) the fixture project.

    First run saves the payload's SceneDocument as the document's head and
    registers the project entry. Later runs reuse the project only when
    the head still matches the manifest's expected scene sha — a diverged
    head (user-edited or corrupted fixture copy) fails closed as drift.
    """

    payload = load_reference_theater_payload()
    manifest = REFERENCE_THEATER_MANIFEST
    document = SceneDocument.model_validate(payload.scene)
    content_sha = scene_content_hash(document)
    if content_sha != manifest.expected_scene_sha256:
        raise ReferenceTheaterDriftError(
            'reference theater scene content drift: '
            f'expected {manifest.expected_scene_sha256}, got {content_sha}')
    head = scene_repository.current_head(document.document_id)
    created = False
    if head is None:
        result = scene_repository.save(document, parent_revision_id=None)
        head = result.revision
        created = True
    else:
        stored_sha = scene_content_hash(head.document)
        if stored_sha != manifest.expected_scene_sha256:
            raise ReferenceTheaterDriftError(
                'reference theater project head drift: '
                f'expected {manifest.expected_scene_sha256}, '
                f'got {stored_sha}')
    entry = library.ensure_document_registered(
        document.document_id, payload.display_name)
    return ReferenceTheaterOpenResult(
        document_id=document.document_id,
        project_id=entry.project_id,
        revision_id=head.revision_id,
        content_sha256=content_sha,
        created=created,
    )


# ---------------------------------------------------------------------------
# Self-test lane evidence — sealed run records
# ---------------------------------------------------------------------------


class ReferenceTheaterStepResult(BaseModel):
    """One lane step's observed result against its declared expectation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    step: ReferenceTheaterStep
    verdict: ReferenceTheaterStepVerdict
    expected: str = ''
    observed: str = ''
    note: str = ''


class CadReferenceTheaterRun(BaseModel):
    """Sealed evidence for one Reference Theater self-test lane run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    run_id: str
    run_sha256: str
    document_id: str
    fixture_version: str
    manifest_sha256: str
    outcome: ReferenceTheaterOutcome
    verdict: str
    steps: tuple[ReferenceTheaterStepResult, ...]
    deploy_is_simulated: bool = True
    deploy_evidence_strength: str = 'none'
    deploy_record_ref: AuthorityRef | None = None
    project_ref: AuthorityRef | None = None
    scene_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    started_at_utc: str
    finished_at_utc: str
    elapsed_ms: int = 0
    authority_version: str = REFERENCE_THEATER_AUTHORITY_VERSION

    @model_validator(mode='after')
    def valid_run(self) -> 'CadReferenceTheaterRun':
        for field in ('started_at_utc', 'finished_at_utc'):
            _require_iso8601(getattr(self, field), field)
        observed = tuple(step.step for step in self.steps)
        if observed != REFERENCE_THEATER_STEPS:
            raise ValueError(
                'run steps must cover the lane in declared order')
        if self.outcome == 'passed' and any(
                step.verdict != 'verified' for step in self.steps):
            raise ValueError(
                'passed runs require every step verified')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('run_id', None)
        payload.pop('run_sha256', None)
        return payload


def build_reference_theater_run(**payload: Any) -> CadReferenceTheaterRun:
    return _seal(
        CadReferenceTheaterRun, payload, 'run_id', 'run_sha256', 'rtrun')


def reference_theater_run_ref(run: CadReferenceTheaterRun) -> AuthorityRef:
    return AuthorityRef(
        kind='reference_theater_run',
        ref_id=run.run_id,
        ref_sha256=run.run_sha256)


# ---------------------------------------------------------------------------
# The self-test lane
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


@dataclass(frozen=True, slots=True)
class ReferenceTheaterLaneResult:
    run: CadReferenceTheaterRun
    refs: tuple[AuthorityRef, ...]


def _step(
    step: ReferenceTheaterStep,
    verdict: ReferenceTheaterStepVerdict,
    *,
    expected: str = '',
    observed: str = '',
    note: str = '',
) -> ReferenceTheaterStepResult:
    return ReferenceTheaterStepResult(
        step=step, verdict=verdict, expected=expected,
        observed=observed, note=note)


def _scene_facts(document: SceneDocument) -> dict[str, Any]:
    speakers = [e for e in document.entities if e.kind == 'speaker']
    return {
        'entity_count': len(document.entities),
        'speaker_count': len(speakers),
        'speaker_roles': sorted(
            e.speaker_role for e in speakers if e.speaker_role),
        'seat_count': sum(1 for e in document.entities if e.kind == 'seat'),
        'measurement_point_count': sum(
            1 for e in document.entities if e.kind == 'measurement_point'),
        'screen_count': sum(
            1 for e in document.entities if e.kind == 'screen'),
        'room': (
            None if document.room is None else {
                'width_m': document.room.width_m,
                'depth_m': document.room.depth_m,
                'height_m': document.room.height_m,
            }),
    }


def _compare_facts(observed: dict[str, Any], expected: dict[str, Any]) -> str | None:
    """Return the first diverging expected key, or None when all match."""

    for key, wanted in expected.items():
        if key not in observed:
            return f'{key} missing'
        if observed[key] != wanted:
            return (
                f'{key}: expected {wanted!r}, observed {observed[key]!r}')
    return None


def run_reference_theater_self_test(
    repos: Any,
    *,
    authorize_apply: str | None = None,
    out_path: Path | None = None,
    now: Callable[[], str] | None = None,
) -> ReferenceTheaterLaneResult:
    """Run the deterministic self-test lane and seal the run record.

    ``repos`` supplies ``scene``/``library``/``deployment``/
    ``reference_theater`` accessors (the headless CLI ``_Repositories``
    shape). ``authorize_apply`` is the session-scoped operator
    authorization for the simulated deploy step — device mutation is never
    implicit, so without it the deploy step reports ``unauthorized`` and
    the whole run verdicts ``unauthorized``.
    """

    clock = now or _utc_now
    import time as _time

    monotonic_start = _time.monotonic()
    started_at = clock()
    steps: dict[str, ReferenceTheaterStepResult] = {}
    refs: list[AuthorityRef] = []
    document_id = ''
    manifest_sha = REFERENCE_THEATER_MANIFEST.manifest_sha256
    scene_sha = REFERENCE_THEATER_MANIFEST.expected_scene_sha256
    deploy_evidence_strength = 'none'
    deploy_record_ref: AuthorityRef | None = None
    project_ref: AuthorityRef | None = None
    report_sha: str | None = None

    payload: ReferenceTheaterPayload | None = None
    document: SceneDocument | None = None
    head = None
    outcome: ReferenceTheaterOutcome = 'blocked'
    verdict = 'blocked'

    # ---- step 1: open -------------------------------------------------
    try:
        opened = materialize_reference_theater(repos.scene, repos.library)
        document_id = opened.document_id
        scene_sha = opened.content_sha256
        project_ref = AuthorityRef(
            kind='project',
            ref_id=opened.project_id,
            ref_sha256=canonical_sha256({
                'project_id': opened.project_id,
                'document_id': opened.document_id}))
        refs.append(project_ref)
        payload = load_reference_theater_payload()
        document = SceneDocument.model_validate(payload.scene)
        head = repos.scene.current_head(document_id)
        steps['open'] = _step(
            'open', 'verified',
            expected=f"scene sha {scene_sha[:16]}…",
            observed=(
                f"materialized rev {opened.revision_id}"
                if opened.created
                else f"existing head {opened.revision_id} verified"),
            note=f"project {opened.project_id}")
    except ReferenceTheaterDriftError as exc:
        steps['open'] = _step(
            'open', 'diverged', expected='manifest-pinned fixture',
            observed=str(exc), note='fixture drift fails closed')
        outcome = 'fixture_drift'
        verdict = 'fixture_drift'
    except Exception as exc:  # error-boundary: open failure blocks the lane
        steps['open'] = _step(
            'open', 'blocked', expected='project materializes',
            observed=f'{type(exc).__name__}: {exc}')
    if 'open' not in steps or steps['open'].verdict != 'verified':
        for rest in REFERENCE_THEATER_STEPS[1:]:
            steps.setdefault(rest, _step(
                rest, 'skipped',
                note='open did not verify — lane cannot proceed'))
        if steps['open'].verdict != 'diverged':
            outcome, verdict = 'blocked', 'open_failed'
    else:
        # ---- step 2: authority validation ------------------------------
        try:
            from .native_authority_audit import audit_native_authority_graph

            report = audit_native_authority_graph(repos.scene.path)
            observed = (
                f'{len(report.diagnostics)} diagnostics, '
                f'{len(report.unclassified_tables)} unclassified')
            steps['authority'] = _step(
                'authority',
                'verified' if report.ok else 'diverged',
                expected='authority audit clean', observed=observed,
                note=report.summary()[:200])
        except Exception as exc:  # error-boundary: self-test step — a crashing step yields an honest 'blocked' verdict with the exception identity, never a pass/diverge (noqa: BLE001)
            steps['authority'] = _step(
                'authority', 'blocked', expected='audit runs',
                observed=f'{type(exc).__name__}: {exc}')

        # ---- step 3: analysis ------------------------------------------
        try:
            assert document is not None and payload is not None
            facts = _scene_facts(document)
            expected = dict(payload.expected.analysis)
            diff = _compare_facts(facts, expected)
            steps['analysis'] = _step(
                'analysis',
                'verified' if diff is None else 'diverged',
                expected=canonical_json(expected)[:240],
                observed=canonical_json(facts)[:240],
                note='' if diff is None else f'diverged: {diff}')
        except Exception as exc:  # error-boundary: self-test step — a crashing step yields an honest 'blocked' verdict with the exception identity, never a pass/diverge (noqa: BLE001)
            steps['analysis'] = _step(
                'analysis', 'blocked', expected='scene facts derive',
                observed=f'{type(exc).__name__}: {exc}')

        # ---- step 4: compare -------------------------------------------
        try:
            expected_compare = dict(payload.expected.compare)
            candidates = list(payload.optimization_candidates)
            ranked = sorted(
                candidates,
                key=lambda c: float(c.get('objective_score', 0.0)),
                reverse=True)
            winner = ranked[0]['candidate_id'] if ranked else None
            wanted = expected_compare.get('preferred_candidate_id')
            tolerance = expected_compare.get('tolerance', {})
            diverged = winner != wanted
            tolerance_note = ''
            if not diverged and tolerance:
                scores = {
                    c['candidate_id']: float(c.get('objective_score', 0.0))
                    for c in candidates}
                wanted_score = scores.get(wanted)
                for cid, score in scores.items():
                    if cid == wanted:
                        continue
                    window = float(tolerance.get(cid, 0.0))
                    if wanted_score is not None and (
                            score > wanted_score + window):
                        diverged = True
                        tolerance_note = (
                            f'{cid} beats {wanted} beyond tolerance')
                        break
            steps['compare'] = _step(
                'compare',
                'diverged' if diverged else 'verified',
                expected=f'preferred={wanted}',
                observed=f'preferred={winner}',
                note=tolerance_note)
        except Exception as exc:  # error-boundary: self-test step — a crashing step yields an honest 'blocked' verdict with the exception identity, never a pass/diverge (noqa: BLE001)
            steps['compare'] = _step(
                'compare', 'blocked', expected='candidates compare',
                observed=f'{type(exc).__name__}: {exc}')

        # ---- step 5: simulated deploy ----------------------------------
        deploy_snapshot = None
        if not authorize_apply:
            steps['deploy'] = _step(
                'deploy', 'unauthorized',
                expected='--authorize-apply OPERATOR_ID',
                observed='no operator authorization supplied',
                note='device mutation is never implicit, even simulated')
            steps['verify'] = _step(
                'verify', 'skipped',
                note='deploy not authorized — nothing to verify')
        else:
            try:
                from .cad_avr_lan_adapter import (
                    AVR_LAN_ADAPTER_ID,
                    AvrLanCalibrationAdapter,
                    FakeAvrLanTransport,
                )
                from .cad_calibration import CadCalibrationExportSnapshot
                from .cad_device_adapter import build_device_binding
                from .cad_deployment_pipeline import DeploymentPipelineService
                from .cad_headless_cli import HeadlessDeploymentSpec

                spec = HeadlessDeploymentSpec.model_validate(payload.deploy)
                export = CadCalibrationExportSnapshot.model_validate(
                    spec.export.model_dump(mode='json'))
                export_sha = canonical_sha256(export.semantic_payload())
                if export_sha != REFERENCE_THEATER_MANIFEST.expected_export_sha256:
                    raise ReferenceTheaterDriftError(
                        'fixture deploy export semantic drift: expected '
                        f'{REFERENCE_THEATER_MANIFEST.expected_export_sha256}, '
                        f'got {export_sha}')
                transport = FakeAvrLanTransport(
                    initial=spec.adapter.initial_gains)
                adapter = AvrLanCalibrationAdapter(
                    {spec.binding.device_serial: transport},
                    approved_remote_endpoints=tuple(
                        spec.adapter.approved_remote_endpoints),
                    simulated=True)
                binding = build_device_binding(
                    adapter_id=AVR_LAN_ADAPTER_ID,
                    device_family=spec.binding.device_family,
                    device_model=spec.binding.device_model,
                    device_serial=spec.binding.device_serial,
                    firmware_version=spec.binding.firmware_version,
                    routing=tuple(
                        tuple(pair) for pair in spec.binding.routing),
                    bound_at_utc=clock(),
                )
                service = DeploymentPipelineService(
                    adapter, binding,
                    target_ref=spec.target_ref,
                    document_id=document_id,
                    repository=repos.deployment,
                )
                service.open(at=clock())
                service.compile(export, at=clock())
                service.preview(at=clock())
                authorization = service.authorize(
                    operator_id=authorize_apply, scope='apply', at=clock())
                service.apply(authorization, at=clock())
                deploy_snapshot = service.verify_readback(at=clock())
                latest = service.latest
                stage = latest.stage if latest is not None else 'unknown'
                if latest is not None:
                    deploy_evidence_strength = latest.evidence_strength
                    deploy_record_ref = AuthorityRef(
                        kind='deployment_pipeline_record',
                        ref_id=latest.record_id,
                        ref_sha256=latest.record_sha256)
                    refs.append(deploy_record_ref)
                expected_deploy = dict(payload.expected.deploy)
                wanted_stage = expected_deploy.get(
                    'stage', 'readback_matched')
                steps['deploy'] = _step(
                    'deploy',
                    'verified' if stage == wanted_stage else 'diverged',
                    expected=f'stage {wanted_stage}',
                    observed=f'stage {stage} '
                             f'evidence={deploy_evidence_strength} '
                             'simulated',
                    note='simulated adapter evidence only')
            except ReferenceTheaterDriftError as exc:
                steps['deploy'] = _step(
                    'deploy', 'diverged',
                    expected='manifest-pinned export',
                    observed=str(exc))
                steps['verify'] = _step(
                    'verify', 'skipped', note='deploy drifted')
            except Exception as exc:  # error-boundary: self-test step — a crashing step yields an honest 'blocked' verdict with the exception identity, never a pass/diverge (noqa: BLE001)
                steps['deploy'] = _step(
                    'deploy', 'blocked',
                    expected='readback_matched simulated deploy',
                    observed=f'{type(exc).__name__}: {exc}')
                steps['verify'] = _step(
                    'verify', 'skipped', note='deploy blocked')

            # ---- step 6: verify ----------------------------------------
            if deploy_snapshot is not None:
                try:
                    expected_verify = dict(payload.expected.verify)
                    wanted_gains = {
                        str(k): float(v)
                        for k, v in dict(
                            expected_verify.get('gains_db', {})).items()}
                    tolerance_db = float(
                        expected_verify.get('tolerance_db', 0.5))
                    observed_gains = {
                        channel.channel_id: float(channel.gain_db)
                        for channel in deploy_snapshot.observed_channels}
                    divergence = None
                    for channel_id, wanted in wanted_gains.items():
                        observed_gain = observed_gains.get(channel_id)
                        if observed_gain is None:
                            divergence = f'{channel_id} missing'
                            break
                        if abs(observed_gain - wanted) > tolerance_db:
                            divergence = (
                                f'{channel_id}: expected '
                                f'{wanted:g}±{tolerance_db:g}, '
                                f'observed {observed_gain:g}')
                            break
                    steps['verify'] = _step(
                        'verify',
                        'verified' if divergence is None else 'diverged',
                        expected=canonical_json(wanted_gains)[:240],
                        observed=canonical_json(observed_gains)[:240],
                        note=divergence or '')
                except Exception as exc:  # error-boundary: self-test step — a crashing step yields an honest 'blocked' verdict with the exception identity, never a pass/diverge (noqa: BLE001)
                    steps['verify'] = _step(
                        'verify', 'blocked',
                        expected='deployed gains match fixture',
                        observed=f'{type(exc).__name__}: {exc}')

        # ---- step 7: export ---------------------------------------------
        try:
            report = {
                'format': REFERENCE_THEATER_REPORT_FORMAT,
                'fixture_version': payload.fixture_version,
                'manifest_sha256': manifest_sha,
                'scene_sha256': scene_sha,
                'steps': [
                    {
                        'step': step,
                        'verdict': steps[step].verdict,
                        'expected': steps[step].expected,
                        'observed': steps[step].observed,
                    }
                    for step in REFERENCE_THEATER_STEPS
                    if step in steps
                ],
            }
            target = Path(out_path) if out_path else (
                repos.data_dir / 'exports' / 'reference-theater'
                / 'selftest-report.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            text = canonical_json(report) + '\n'
            target.write_text(text, encoding='utf-8')
            loaded = text.encode('utf-8')
            report_sha = hashlib.sha256(loaded).hexdigest()
            expected_export = dict(payload.expected.export)
            min_bytes = int(expected_export.get('min_bytes', 64))
            required = tuple(expected_export.get(
                'required_keys', ('format', 'fixture_version',
                                  'manifest_sha256', 'steps')))
            missing = [k for k in required if k not in report]
            ok = (
                not missing
                and len(loaded) >= min_bytes
                and report['format'] == REFERENCE_THEATER_REPORT_FORMAT)
            steps['export'] = _step(
                'export',
                'verified' if ok else 'diverged',
                expected=f'report artifact >= {min_bytes} B, '
                         f'keys {required}',
                observed=f'{len(loaded)} B at {target}',
                note='' if not missing else f'missing {missing}')
        except Exception as exc:  # error-boundary: self-test step — a crashing step yields an honest 'blocked' verdict with the exception identity, never a pass/diverge (noqa: BLE001)
            steps['export'] = _step(
                'export', 'blocked', expected='report artifact writes',
                observed=f'{type(exc).__name__}: {exc}')

        if outcome == 'blocked':
            verdicts = {step.verdict for step in steps.values()}
            if 'unauthorized' in verdicts:
                outcome, verdict = 'unauthorized', 'authorize_apply_required'
            elif 'diverged' in verdicts or 'unverified' in verdicts:
                bad = next(
                    s.step for s in steps.values()
                    if s.verdict in ('diverged', 'unverified'))
                outcome, verdict = 'failed', f'mismatch:{bad}'
            elif 'blocked' in verdicts:
                bad = next(
                    s.step for s in steps.values()
                    if s.verdict == 'blocked')
                outcome, verdict = 'blocked', f'blocked:{bad}'
            elif verdicts == {'verified'}:
                outcome, verdict = 'passed', 'all_verified'

    finished_at = clock()
    elapsed_ms = int((_time.monotonic() - monotonic_start) * 1000)
    run = build_reference_theater_run(
        document_id=document_id or 'unmaterialized',
        fixture_version=REFERENCE_THEATER_FIXTURE_VERSION,
        manifest_sha256=manifest_sha,
        outcome=outcome,
        verdict=verdict,
        steps=[steps[s] for s in REFERENCE_THEATER_STEPS],
        deploy_is_simulated=True,
        deploy_evidence_strength=deploy_evidence_strength,
        deploy_record_ref=(
            None if deploy_record_ref is None
            else deploy_record_ref.model_dump(mode='json')),
        project_ref=(
            None if project_ref is None
            else project_ref.model_dump(mode='json')),
        scene_sha256=scene_sha,
        report_sha256=report_sha,
        started_at_utc=started_at,
        finished_at_utc=finished_at,
        elapsed_ms=elapsed_ms,
    )
    repos.reference_theater.save_run(run)
    refs.append(reference_theater_run_ref(run))
    return ReferenceTheaterLaneResult(run=run, refs=tuple(refs))


__all__ = [
    'REFERENCE_THEATER_AUTHORITY_VERSION',
    'REFERENCE_THEATER_FIXTURE_VERSION',
    'REFERENCE_THEATER_MANIFEST',
    'REFERENCE_THEATER_PAYLOAD_FORMAT',
    'REFERENCE_THEATER_PAYLOAD_RELPATH',
    'REFERENCE_THEATER_REPORT_FORMAT',
    'REFERENCE_THEATER_STEPS',
    'CadReferenceTheaterRun',
    'ReferenceTheaterDriftError',
    'ReferenceTheaterError',
    'ReferenceTheaterExpectations',
    'ReferenceTheaterLaneResult',
    'ReferenceTheaterManifest',
    'ReferenceTheaterOpenResult',
    'ReferenceTheaterOutcome',
    'ReferenceTheaterPayload',
    'ReferenceTheaterProvenance',
    'ReferenceTheaterStep',
    'ReferenceTheaterStepResult',
    'ReferenceTheaterStepVerdict',
    'build_reference_theater_manifest',
    'build_reference_theater_run',
    'load_reference_theater_payload',
    'materialize_reference_theater',
    'reference_theater_available',
    'reference_theater_benchmark_entry',
    'reference_theater_manifest',
    'reference_theater_payload_path',
    'reference_theater_payload_sha256',
    'reference_theater_run_ref',
    'run_reference_theater_self_test',
]
