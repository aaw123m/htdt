from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .cad_repository import SceneRepository
from .cad_roomsim import (
    CadRoomSimBinding,
    build_cad_roomsim_candidate_request,
)
from .cad_roomsim_results import (
    CadRoomSimBatchSpec,
    CadRoomSimCandidateAttempt,
    roomsim_result_response,
)
from .cad_search import iter_cad_candidate_pages
from .cad_search_models import CAD_SEARCH_ALGORITHM_VERSION, CadCandidate
from .cad_search_repository import CadSearchRepository


class CadRoomSimRepository:
    """Immutable O20 Room Simulator batch/attempt storage on native CAD authority."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        search_repository: CadSearchRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.search_repository = search_repository
        self.path = Path(scene_repository.path)
        if Path(search_repository.path) != self.path:
            raise ValueError('scene and search repositories must share one native CAD database')
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS cad_roomsim_batch_specs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_run_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    scene_content_hash TEXT NOT NULL,
                    search_spec_id TEXT NOT NULL,
                    search_spec_sha256 TEXT NOT NULL,
                    candidate_set_sha256 TEXT NOT NULL,
                    batch_spec_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id),
                    FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
                );
                CREATE INDEX IF NOT EXISTS idx_roomsim_batch_search_seq
                    ON cad_roomsim_batch_specs(search_spec_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_roomsim_candidate_attempts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL UNIQUE,
                    batch_run_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    attempt_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    completed_at_utc TEXT NOT NULL,
                    UNIQUE(batch_run_id, candidate_id, attempt_index),
                    FOREIGN KEY(batch_run_id) REFERENCES cad_roomsim_batch_specs(batch_run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_roomsim_attempt_batch_seq
                    ON cad_roomsim_candidate_attempts(batch_run_id, seq ASC);
                CREATE INDEX IF NOT EXISTS idx_roomsim_attempt_candidate_seq
                    ON cad_roomsim_candidate_attempts(batch_run_id, candidate_id, attempt_index ASC);
                '''
            )

    def _require_batch_authority(self, spec: CadRoomSimBatchSpec) -> None:
        """Replay the exact batch-input compiler over declared authority.

        ``CadRoomSimBatchSpec`` self-hashes its submitted requests, so a
        coherently rehashed payload is not proof of authority. The persisted
        batch must instead reproduce its candidate request set from the exact
        SearchSpec candidate enumeration: the exact SceneRevision and
        SearchSpec are re-resolved (the SearchSpec read already replays its
        own compiler authority), the pinned search algorithm regenerates the
        exact candidate set and ``candidate_set_sha256``, the persisted
        binding parses as a ``CadRoomSimBinding``, and every persisted
        request must equal ``build_cad_roomsim_candidate_request`` for the
        exact resolved candidate.

        The persisted ``requests`` tuple is the explicit ordered subset
        selection: every member must resolve to a canonical candidate with
        matching raw/feasible indices, and the sequence must follow canonical
        enumeration order (strictly increasing ``feasible_index``) rather
        than trusting whatever order was submitted.

        Replay policy for historical search algorithms: only the pinned
        ``deterministic_grid``/``search-space-grid-1`` enumeration is
        replayable. Persisted SearchSpec rows on an older schema version
        already fail closed inside ``CadSearchRepository``, so a batch
        anchored to them can never authorize attempts.

        This runs before every batch write and on every batch read, so a
        stale or tampered row cannot authorize candidate attempts.
        """

        revision = self.scene_repository.get(spec.scene_revision_id)
        if revision is None:
            raise ValueError('Room Simulator batch source revision does not exist')
        if revision.document_id != spec.document_id:
            raise ValueError('Room Simulator batch source revision belongs to another document')
        if revision.content_hash != spec.scene_content_hash:
            raise ValueError('Room Simulator batch source content hash mismatch')

        search_spec = self.search_repository.get(spec.search_spec_id)
        if search_spec is None:
            raise ValueError('Room Simulator batch SearchSpec does not exist')
        if search_spec.document_id != spec.document_id:
            raise ValueError('Room Simulator batch SearchSpec belongs to another document')
        if (
            search_spec.scene_revision_id != spec.scene_revision_id
            or search_spec.scene_content_hash != spec.scene_content_hash
        ):
            raise ValueError('Room Simulator batch SearchSpec source binding mismatch')
        if search_spec.search_spec_sha256 != spec.search_spec_sha256:
            raise ValueError('Room Simulator batch SearchSpec hash mismatch')
        if (
            search_spec.algorithm != 'deterministic_grid'
            or search_spec.algorithm_version != CAD_SEARCH_ALGORITHM_VERSION
        ):
            raise ValueError(
                'Room Simulator batch cannot replay the pinned search algorithm'
            )

        binding = CadRoomSimBinding.model_validate(json.loads(spec.binding_json))

        remaining = {item.candidate_id for item in spec.requests}
        resolved: dict[str, CadCandidate] = {}
        candidate_set_sha256: str | None = None
        with closing(
            iter_cad_candidate_pages(self.scene_repository, search_spec)
        ) as pages:
            for page in pages:
                candidate_set_sha256 = page.candidate_set_sha256
                for candidate in page.candidates:
                    if candidate.candidate_id in remaining:
                        resolved[candidate.candidate_id] = candidate
                        remaining.discard(candidate.candidate_id)
                if not remaining:
                    break
        if candidate_set_sha256 != spec.candidate_set_sha256:
            raise ValueError('Room Simulator batch candidate-set hash mismatch')

        last_feasible_index = -1
        for persisted in spec.requests:
            candidate = resolved.get(persisted.candidate_id)
            if candidate is None:
                raise ValueError(
                    'Room Simulator batch request candidate is not in the '
                    'regenerated SearchSpec candidate set'
                )
            if (
                candidate.raw_index != persisted.raw_index
                or candidate.feasible_index != persisted.feasible_index
            ):
                raise ValueError(
                    'Room Simulator batch request raw/feasible index mismatch'
                )
            if candidate.feasible_index <= last_feasible_index:
                raise ValueError(
                    'Room Simulator batch requests must follow canonical '
                    'candidate enumeration order'
                )
            last_feasible_index = candidate.feasible_index
            if (
                build_cad_roomsim_candidate_request(
                    revision,
                    search_spec,
                    candidate,
                    binding,
                )
                != persisted
            ):
                raise ValueError(
                    'Room Simulator batch request is not the canonical '
                    'candidate request'
                )

    def _validated_batch_spec(self, row: sqlite3.Row) -> CadRoomSimBatchSpec:
        """Deserialize one persisted batch row and replay its exact authority."""
        spec = CadRoomSimBatchSpec.model_validate_json(row['payload_json'])
        if (
            row['batch_run_id'] != spec.batch_run_id
            or row['document_id'] != spec.document_id
            or row['scene_revision_id'] != spec.scene_revision_id
            or row['scene_content_hash'] != spec.scene_content_hash
            or row['search_spec_id'] != spec.search_spec_id
            or row['search_spec_sha256'] != spec.search_spec_sha256
            or row['candidate_set_sha256'] != spec.candidate_set_sha256
            or row['batch_spec_sha256'] != spec.batch_spec_sha256
            or row['created_at_utc'] != spec.created_at_utc
        ):
            raise ValueError(
                'persisted Room Simulator batch row disagrees with its payload'
            )
        self._require_batch_authority(spec)
        return spec

    def save_batch_spec(self, spec: CadRoomSimBatchSpec) -> None:
        if not isinstance(spec, CadRoomSimBatchSpec):
            raise TypeError('spec must be CadRoomSimBatchSpec')
        spec = CadRoomSimBatchSpec.model_validate(spec.model_dump(mode='python'))
        self._require_batch_authority(spec)

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''INSERT INTO cad_roomsim_batch_specs(
                    batch_run_id, document_id, scene_revision_id, scene_content_hash,
                    search_spec_id, search_spec_sha256, candidate_set_sha256,
                    batch_spec_sha256, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    spec.batch_run_id,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.scene_content_hash,
                    spec.search_spec_id,
                    spec.search_spec_sha256,
                    spec.candidate_set_sha256,
                    spec.batch_spec_sha256,
                    spec.model_dump_json(),
                    spec.created_at_utc,
                ),
            )

    def get_batch_spec(self, batch_run_id: str) -> CadRoomSimBatchSpec | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM cad_roomsim_batch_specs WHERE batch_run_id=?',
                (batch_run_id,),
            ).fetchone()
        return None if row is None else self._validated_batch_spec(row)

    def list_batch_specs(self, search_spec_id: str) -> tuple[CadRoomSimBatchSpec, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM cad_roomsim_batch_specs WHERE search_spec_id=? ORDER BY seq ASC',
                (search_spec_id,),
            ).fetchall()
        return tuple(self._validated_batch_spec(row) for row in rows)

    def _verify_attempt_authority(
        self,
        attempt: CadRoomSimCandidateAttempt,
        batch: CadRoomSimBatchSpec | None,
    ) -> None:
        """Fail closed unless a completed attempt binds the exact persisted request.

        A self-consistent attempt hash alone cannot prove that the recorded
        response came from executing the persisted candidate request, so the
        exact request SHA, model/adapter identity and response selector are
        re-verified against the immutable batch on every save and read.
        """
        if batch is None or attempt.batch_run_id != batch.batch_run_id:
            raise ValueError('Room Simulator attempt batch authority is unavailable')
        if attempt.status != 'completed':
            return
        result = attempt.result
        if result is None:
            raise ValueError('completed Room Simulator attempt has no execution result')
        requests = {item.candidate_id: item for item in batch.requests}
        request = requests.get(attempt.candidate_id)
        if request is None:
            raise ValueError('Room Simulator attempt candidate is not part of the batch')
        if (
            result.candidate_id != attempt.candidate_id
            or result.request_sha256 != request.request_sha256
        ):
            raise ValueError(
                'Room Simulator attempt result is not bound to the exact candidate request'
            )
        if result.model_id != batch.model_id or result.adapter_version != batch.adapter_version:
            raise ValueError('Room Simulator attempt model/adapter authority mismatch')
        request_payload = json.loads(request.request_json)
        response = roomsim_result_response(result)
        if (
            response.mic_position != request_payload.get('mic_position', 'Main')
            or response.source_name != request_payload.get('source_name')
        ):
            raise ValueError(
                'Room Simulator attempt response does not match the exact candidate request'
            )

    def _decode_attempt(
        self,
        row: sqlite3.Row,
        batch: CadRoomSimBatchSpec | None,
    ) -> CadRoomSimCandidateAttempt:
        attempt = CadRoomSimCandidateAttempt.model_validate_json(row['payload_json'])
        if (
            attempt.attempt_id != row['attempt_id']
            or attempt.candidate_id != row['candidate_id']
            or attempt.attempt_index != row['attempt_index']
            or attempt.status != row['status']
            or attempt.attempt_sha256 != row['attempt_sha256']
        ):
            raise ValueError('Room Simulator attempt row authority mismatch')
        self._verify_attempt_authority(attempt, batch)
        return attempt

    def save_attempt(self, attempt: CadRoomSimCandidateAttempt) -> None:
        batch = self.get_batch_spec(attempt.batch_run_id)
        if batch is None:
            raise ValueError('Room Simulator attempt batch does not exist')
        candidate_ids = {item.candidate_id for item in batch.requests}
        if attempt.candidate_id not in candidate_ids:
            raise ValueError('Room Simulator attempt candidate is not part of the batch')
        self._verify_attempt_authority(attempt, batch)

        prior = self.list_candidate_attempts(attempt.batch_run_id, attempt.candidate_id)
        expected_index = len(prior) + 1
        if attempt.attempt_index != expected_index:
            raise ValueError(
                f'Room Simulator attempt_index must be the next immutable index: {expected_index}'
            )
        if any(item.status == 'completed' for item in prior):
            raise ValueError('Room Simulator candidate already has a completed attempt')

        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''INSERT INTO cad_roomsim_candidate_attempts(
                    attempt_id, batch_run_id, candidate_id, attempt_index, status,
                    attempt_sha256, payload_json, completed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    attempt.attempt_id,
                    attempt.batch_run_id,
                    attempt.candidate_id,
                    attempt.attempt_index,
                    attempt.status,
                    attempt.attempt_sha256,
                    attempt.model_dump_json(),
                    attempt.completed_at_utc,
                ),
            )

    def get_attempt(self, attempt_id: str) -> CadRoomSimCandidateAttempt | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_roomsim_candidate_attempts WHERE attempt_id=?',
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        attempt = CadRoomSimCandidateAttempt.model_validate_json(row['payload_json'])
        if attempt.attempt_id != attempt_id:
            raise ValueError('Room Simulator attempt identity authority mismatch')
        self._verify_attempt_authority(attempt, self.get_batch_spec(attempt.batch_run_id))
        return attempt

    def list_attempts(self, batch_run_id: str) -> tuple[CadRoomSimCandidateAttempt, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT attempt_id, candidate_id, attempt_index, status, attempt_sha256, '
                'payload_json FROM cad_roomsim_candidate_attempts '
                'WHERE batch_run_id=? ORDER BY seq ASC',
                (batch_run_id,),
            ).fetchall()
        if not rows:
            return ()
        batch = self.get_batch_spec(batch_run_id)
        return tuple(self._decode_attempt(row, batch) for row in rows)

    def list_candidate_attempts(
        self,
        batch_run_id: str,
        candidate_id: str,
    ) -> tuple[CadRoomSimCandidateAttempt, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT attempt_id, candidate_id, attempt_index, status, attempt_sha256, '
                'payload_json FROM cad_roomsim_candidate_attempts '
                'WHERE batch_run_id=? AND candidate_id=? ORDER BY attempt_index ASC',
                (batch_run_id, candidate_id),
            ).fetchall()
        if not rows:
            return ()
        batch = self.get_batch_spec(batch_run_id)
        return tuple(self._decode_attempt(row, batch) for row in rows)

    def completed_candidate_ids(self, batch_run_id: str) -> frozenset[str]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT attempt_id, candidate_id, attempt_index, status, attempt_sha256, "
                "payload_json FROM cad_roomsim_candidate_attempts "
                "WHERE batch_run_id=? AND status='completed'",
                (batch_run_id,),
            ).fetchall()
        if not rows:
            return frozenset()
        batch = self.get_batch_spec(batch_run_id)
        return frozenset(self._decode_attempt(row, batch).candidate_id for row in rows)

    def next_attempt_index(self, batch_run_id: str, candidate_id: str) -> int:
        return len(self.list_candidate_attempts(batch_run_id, candidate_id)) + 1
