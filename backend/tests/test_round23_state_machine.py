"""Round-23: generative state-machine testing.

Scripted tests verify the transitions the authors thought of. This round
attacks the transitions nobody scripted: seeded RANDOM WALKS through each
state machine, with an independent model tracking what the world should look
like after every step. Every op is drawn from ``random.Random(seed)`` — a
failing seed replays identically, so ``pytest -k 'seed and <n>'`` reproduces
any failure exactly.

Surfaces and the invariants asserted after every step:

- ``_history_walk`` — WorkingDocument/CommandHistory: a content-hash journal
  shadows every index position (undo/redo must land on exactly the recorded
  content — no silent loss), the bounded history's eviction bookkeeping is
  mirrored, preview never leaks into the committed document, composite
  side-effects apply/revert in step with their owning command's position,
  and illegal ops are rejected loudly.
- ``_document_walk`` — RoomWorkspaceController + ProjectLibraryRepository +
  SceneRepository: create/edit/undo/redo/save/close/reopen/switch/restore/
  recover walks asserting the document always validates, is_dirty tells the
  truth, the persisted head round-trips, the recovery snapshot mirrors the
  last synced dirty state, dirty-state resolution is honest, and the
  revision chain stays append-only.
- ``_activity_walk`` — ActivityCenter: random submit/transition/cancel/retry/
  stale-marking against a legality model; every snapshot revalidates, the
  bounded history never duplicates an operation id, evicted records still
  resolve while live in history, and cancel callbacks fire exactly once.
- ``_worker_walk`` — NativeWorkerPool on real QThreads: random
  start/cancel/stop_all interleavings with key reuse; completions fire at
  most once per task, error payloads are honest (``WORKER_CANCELLED`` only
  when cancelled, the exception object otherwise), released tasks never
  deliver late results, and detached threads drain back to the baseline.
- ``_library_walk`` — ProjectLibraryRepository listing contract: random
  create/open/rename/archive/unarchive/duplicate interleaved with reads,
  asserting totals, the opened-first ordering rule, and zero phantom rows.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# pydantic must import before PySide6 (shiboken circular-import guard).
import pydantic  # noqa: F401

from htdt.activity_center import (
    ActivityCenter,
    ApplicationOperation,
    Cancellability,
    NavigationPolicy,
    OperationClass,
    OperationProgress,
    OperationRetryRequest,
    OperationState,
    OperationTransitionError,
    ProgressKind,
    RetryPolicy,
)
from htdt.cad_attachment_models import EntityAttachment
from htdt.cad_document import (
    CompositeEditCommand,
    EditStateError,
    TransformEntitiesCommand,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
    quaternion_from_euler_deg,
    scene_content_hash,
)
from htdt.native_worker import WORKER_CANCELLED
from htdt.physical_attachment import validate_attachment_graph
from htdt.project_library import (
    ProjectArchivedError,
    ProjectLibraryError,
    ProjectNotFoundError,
)
from htdt.project_library_repository import ProjectLibraryRepository
from htdt.room_workspace import RoomWorkspaceController
from htdt.theater_document import TheaterWorkingDocument


# ---------------------------------------------------------------------------
# Walk harness: seeded op streams with trace capture.
# ---------------------------------------------------------------------------


class WalkFailure(AssertionError):
    def __init__(self, surface: str, seed: int, trace: list[str], detail: str):
        self.surface = surface
        self.seed = seed
        self.trace = trace
        self.detail = detail
        super().__init__(
            f"{surface} walk seed={seed} failed at step {len(trace) - 1}: {detail}\n"
            + "trace:\n"
            + "\n".join(f"  {i:3d} {step}" for i, step in enumerate(trace))
        )


class _Walk:
    """Seeded op dispatcher.

    Ops are drawn from the weighted table by the walk rng; each op's
    arguments come from a per-step sub-rng seeded from the walk rng, so a
    recorded ``(op_index, arg_seed)`` pair replays deterministically.
    """

    def __init__(self, surface: str, seed: int) -> None:
        self.surface = surface
        self.seed = seed
        self.rng = random.Random(seed)
        self.trace: list[str] = []
        self.script: list[tuple[int, int]] = []

    def ops(self) -> list[tuple[str, float]]:
        raise NotImplementedError

    def _run(self, idx: int, rng: random.Random) -> None:
        raise NotImplementedError

    def step(self, ops: list[tuple[str, float]], note: str = "") -> None:
        names = [name for name, _w in ops]
        weights = [w for _n, w in ops]
        idx = self.rng.choices(range(len(ops)), weights=weights)[0]
        arg_seed = self.rng.getrandbits(32)
        self.script.append((idx, arg_seed))
        self.trace.append(f"{names[idx]}(sub={arg_seed}){' ' + note if note else ''}")
        self._run(idx, random.Random(arg_seed))

    def check(self, condition: bool, detail: str) -> None:
        if not condition:
            raise WalkFailure(self.surface, self.seed, self.trace, detail)

    def run(self, steps: int) -> None:
        table = self.ops()
        for _ in range(steps):
            self.step(table)


def _entity(entity_id: str, *, kind: str = 'furniture', x: float = 1.0) -> SceneEntity:
    kwargs: dict = {
        'entity_id': entity_id,
        'kind': kind,
        'name': entity_id,
        'position': Position3(x_m=x, y_m=1.5, z_m=0.5),
    }
    if kind == 'speaker':
        kwargs['speaker_role'] = f'R-{entity_id}'
    if kind != 'measurement_point':
        kwargs['size_m'] = Size3(x_m=0.4, y_m=0.4, z_m=0.4)
    return SceneEntity(**kwargs)


def _room(rng: random.Random) -> RoomPrism:
    return RoomPrism(
        width_m=rng.uniform(3.0, 8.0),
        depth_m=rng.uniform(2.5, 6.0),
        height_m=rng.uniform(2.2, 3.2),
    )


def _polygon_room(rng: random.Random) -> RoomPrism:
    w = rng.uniform(3.0, 7.0)
    d = rng.uniform(2.5, 5.0)
    return RoomPrism(
        width_m=w,
        depth_m=d,
        height_m=rng.uniform(2.2, 3.0),
        footprint_vertices=(
            RoomVertex(vertex_id='v0', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='v1', x_m=w, y_m=0.0),
            RoomVertex(vertex_id='v2', x_m=w, y_m=d),
            RoomVertex(vertex_id='v3', x_m=w * 0.4, y_m=d),
            RoomVertex(vertex_id='v4', x_m=0.0, y_m=d * 0.6),
        ),
    )


def _validate_document(document: SceneDocument) -> None:
    SceneDocument.model_validate(document.model_dump(mode='python'))
    ids = [entity.entity_id for entity in document.entities]
    assert len(ids) == len(set(ids))
    validate_attachment_graph(document)


# ---------------------------------------------------------------------------
# Surface 1 — WorkingDocument/CommandHistory random walks.
# ---------------------------------------------------------------------------

_HISTORY_KINDS = ('furniture', 'seat', 'speaker', 'measurement_point', 'screen')
_PHYSICAL = ('speaker', 'seat', 'screen', 'projector', 'display', 'riser', 'furniture', 'av_equipment')

# Ops whose every path calls a preview-guarded API — the walk asserts a
# loud EditStateError for them while a preview is open. ``merge_last`` and
# ``preview`` are deliberately absent: fusing committed history while a
# provisional preview floats is legal, and the preview op owns the
# commit/cancel second phase itself. Ops that can legitimately no-op before
# mutating (attach/detach/reorder_doc) self-guard inside their bodies.
_MUTATING_DURING_PREVIEW = frozenset({
    'move', 'rotate', 'add', 'update', 'update_batch', 'delete', 'duplicate',
    'transform', 'set_edit', 'room', 'composite_side',
    'undo', 'redo',
})


class _HistoryModel:
    """Content-hash journal shadowing the command history.

    ``journal[p]`` is the expected committed-document hash after ``p``
    commands are applied — undo/redo must land exactly on these values,
    which is how silent content loss is detected.
    """

    def __init__(self, document: TheaterWorkingDocument) -> None:
        self.doc = document
        self.journal: list[str] = [scene_content_hash(document.committed_document)]
        self.pos = 0
        self.dropped = 0
        self.pushed = 0
        self.saved_hash = document.saved_content_hash
        self.preview: str | None = None
        self.preview_ids: tuple[str, ...] = ()
        self.counter = 0
        self.att_counter = 0
        self.side_counter = 0
        # flag name -> flag value; side_owner[name] = journal position whose
        # command owns the side effect (flag True iff owner <= pos).
        self.side_flags: dict[str, bool] = {}
        self.side_owner: dict[str, int] = {}

    def entity_ids(self) -> list[str]:
        return [entity.entity_id for entity in self.doc.committed_document.entities]

    def fresh_id(self) -> str:
        self.counter += 1
        return f'gen-{self.counter}'

    def record_push(self, changed: bool) -> None:
        if not changed:
            return
        # Commands evicted from the redo tail can never re-apply their sides.
        for name in list(self.side_owner):
            if self.side_owner[name] > self.pos:
                del self.side_owner[name]
        self.journal = self.journal[: self.pos + 1]
        self.journal.append(scene_content_hash(self.doc.committed_document))
        self.pos += 1
        self.pushed += 1
        overflow = len(self.journal) - 1 - self.doc._history._limit  # noqa: SLF001
        if overflow > 0:
            del self.journal[:overflow]
            self.pos -= overflow
            self.dropped += overflow
            for name in self.side_owner:
                self.side_owner[name] -= overflow

    def merge(self, count: int) -> None:
        """Mirror CommandHistory.merge_last on the journal + side owners."""
        self.journal = (
            self.journal[: self.pos - count + 1] + self.journal[self.pos :]
        )
        new_pos = self.pos - count + 1
        for name in self.side_owner:
            owner = self.side_owner[name]
            if self.pos - count + 1 <= owner <= self.pos:
                self.side_owner[name] = new_pos
            elif owner > self.pos:
                self.side_owner[name] = owner - count + 1
        self.pos = new_pos

    def check(self, walk: _Walk) -> None:
        doc = self.doc
        committed = doc.committed_document
        current_hash = scene_content_hash(committed)
        walk.check(
            current_hash == self.journal[self.pos],
            f'content mismatch at index {self.pos}: '
            f'journal={self.journal[self.pos][:12]} actual={current_hash[:12]}',
        )
        walk.check(doc.history_index == self.pos, f'history_index {doc.history_index} != model {self.pos}')
        walk.check(
            doc.history_length == len(self.journal) - 1,
            f'history_length {doc.history_length} != journal {len(self.journal) - 1}',
        )
        walk.check(doc.history_dropped == self.dropped, f'dropped {doc.history_dropped} != {self.dropped}')
        walk.check(doc.history_epoch == self.pushed, f'epoch {doc.history_epoch} != {self.pushed}')
        walk.check(doc.can_undo == (self.pos > 0), 'can_undo disagreement')
        walk.check(doc.can_redo == (self.pos < len(self.journal) - 1), 'can_redo disagreement')
        walk.check(
            doc.is_dirty == (current_hash != self.saved_hash),
            f'is_dirty {doc.is_dirty} disagrees with hash truth',
        )
        walk.check(doc.has_preview == (self.preview is not None), 'preview flag drift')
        walk.check(doc.saved_content_hash == self.saved_hash, 'saved hash drifted')
        if doc.can_undo:
            walk.check(doc.undo_label is not None, 'undoable history has no undo label')
        else:
            walk.check(doc.undo_label is None, 'empty undo history claims a label')
        ids = self.entity_ids()
        walk.check(len(ids) == len(set(ids)), f'duplicate entity ids: {ids}')
        for name, flag in self.side_flags.items():
            owner = self.side_owner.get(name)
            walk.check(
                flag == (owner is not None and owner <= self.pos),
                f'side-effect {name}={flag} but owner pos {owner} vs cursor {self.pos}',
            )

    def deep_check(self, walk: _Walk) -> None:
        _validate_document(self.doc.committed_document)
        entries = self.doc.history_entries()
        walk.check(len(entries) == self.doc.history_length, 'history_entries length mismatch')
        for entry in entries:
            walk.check(
                entry.applied == (entry.index < self.pos),
                f'entry {entry.index} applied flag wrong',
            )
            walk.check(bool(entry.label), 'history entry has empty label')


class _HistoryWalk(_Walk):
    def __init__(self, seed: int, limit: int, schema5: bool) -> None:
        super().__init__('history', seed)
        if schema5:
            document = SceneDocument(
                document_id='walk-doc',
                schema_version=5,
                room=RoomPrism(width_m=8.0, depth_m=6.0, height_m=3.0),
                entities=(
                    _entity('base-a', kind='furniture'),
                    _entity('base-b', kind='riser'),
                    _entity('spk-a', kind='speaker'),
                ),
            )
        else:
            document = make_f1_scene()
        self.model = _HistoryModel(
            TheaterWorkingDocument(
                document,
                source_revision_id='rev-0',
                history_limit=limit,
            )
        )
        self.schema5 = schema5

    def ops(self) -> list[tuple[str, float]]:
        table = [
            ('move', 10),
            ('rotate', 5),
            ('add', 8),
            ('update', 8),
            ('update_batch', 5),
            ('delete', 7),
            ('duplicate', 6),
            ('transform', 4),
            ('set_edit', 4),
            ('room', 5),
            ('composite_side', 3),
            ('preview', 7),
            ('merge', 4),
            ('undo', 9),
            ('redo', 7),
            ('mark_saved', 3),
            ('bad_op', 3),
            ('reorder_doc', 2),
        ]
        if self.schema5:
            table += [('attach', 4), ('detach', 2)]
        return table

    def _run(self, idx: int, rng: random.Random) -> None:
        name = self.ops()[idx][0]
        if self.model.preview is not None and name in _MUTATING_DURING_PREVIEW:
            try:
                getattr(self, f'_op_{name}')(rng)
            except EditStateError:
                pass
            else:
                self.check(False, f'{name} proceeded during an open preview')
            self.model.check(self)
            return
        getattr(self, f'_op_{name}')(rng)
        self.model.check(self)
        if len(self.trace) % 7 == 0:
            self.model.deep_check(self)

    # -- helpers ----------------------------------------------------------

    def _pick_id(self, rng: random.Random) -> str | None:
        ids = self.model.entity_ids()
        return rng.choice(ids) if ids else None

    def _new_flag(self) -> str:
        name = f'side-{self.model.side_counter}'
        self.model.side_counter += 1
        self.model.side_flags[name] = False
        return name

    def _own_flag(self, name: str, pushed: bool) -> None:
        """The pushed command that ran apply_side owns the flag at the cursor."""
        if pushed and self.model.side_flags.get(name):
            self.model.side_owner[name] = self.model.pos

    # -- legal ops --------------------------------------------------------

    def _op_move(self, rng: random.Random) -> None:
        entity_id = self._pick_id(rng)
        if entity_id is None:
            return self._op_add(rng)
        epoch = self.model.doc.history_epoch
        self.model.doc.move_entity(
            entity_id,
            Position3(x_m=rng.uniform(0, 6), y_m=rng.uniform(0, 5), z_m=rng.uniform(0, 3)),
        )
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_rotate(self, rng: random.Random) -> None:
        entity_id = self._pick_id(rng)
        if entity_id is None:
            return self._op_add(rng)
        epoch = self.model.doc.history_epoch
        self.model.doc.rotate_entity(
            entity_id,
            quaternion_from_euler_deg(
                yaw_deg=rng.uniform(-180, 180),
                pitch_deg=rng.uniform(-90, 90),
                roll_deg=rng.uniform(-45, 45),
            ),
        )
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_add(self, rng: random.Random) -> None:
        kind = rng.choice(_PHYSICAL) if self.schema5 else rng.choice(_HISTORY_KINDS)
        epoch = self.model.doc.history_epoch
        self.model.doc.add_entity(
            _entity(self.model.fresh_id(), kind=kind, x=rng.uniform(0, 6))
        )
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_update(self, rng: random.Random) -> None:
        entity_id = self._pick_id(rng)
        if entity_id is None:
            return self._op_add(rng)
        epoch = self.model.doc.history_epoch
        self.model.doc.update_entity(entity_id, name=f'名前{rng.randrange(1000)}')
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_update_batch(self, rng: random.Random) -> None:
        ids = self.model.entity_ids()
        if not ids:
            return self._op_add(rng)
        chosen = rng.sample(ids, min(len(ids), rng.randint(1, 3)))
        epoch = self.model.doc.history_epoch
        self.model.doc.update_entities(
            {entity_id: {'name': f'バッチ{rng.randrange(1000)}'} for entity_id in chosen}
        )
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_delete(self, rng: random.Random) -> None:
        ids = self.model.entity_ids()
        if not ids:
            return self._op_add(rng)
        chosen = rng.sample(ids, min(len(ids), rng.randint(1, 3)))
        epoch = self.model.doc.history_epoch
        self.model.doc.delete_entities(chosen)
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_duplicate(self, rng: random.Random) -> None:
        entity_id = self._pick_id(rng)
        if entity_id is None:
            return self._op_add(rng)
        epoch = self.model.doc.history_epoch
        self.model.doc.duplicate_entity(entity_id, new_entity_id=self.model.fresh_id())
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_transform(self, rng: random.Random) -> None:
        ids = self.model.entity_ids()
        if not ids:
            return self._op_add(rng)
        chosen = rng.sample(ids, min(len(ids), rng.randint(1, 2)))
        before = tuple(
            entity
            for entity in self.model.doc.committed_document.entities
            if entity.entity_id in chosen
        )
        after = tuple(
            entity.model_copy(update={'name': f'変換{rng.randrange(1000)}'})
            for entity in before
        )
        epoch = self.model.doc.history_epoch
        self.model.doc.transform_entities(before, after)
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_set_edit(self, rng: random.Random) -> None:
        ids = self.model.entity_ids()
        removed = tuple(
            entity
            for entity in self.model.doc.committed_document.entities
            if entity.entity_id in set(rng.sample(ids, min(len(ids), rng.randint(0, 2))))
        )
        remaining = [
            entity
            for entity in self.model.doc.committed_document.entities
            if entity not in removed
        ]
        replaced_before: tuple[SceneEntity, ...] = ()
        replaced_after: tuple[SceneEntity, ...] = ()
        if remaining and rng.random() < 0.6:
            target = rng.choice(remaining)
            replaced_before = (target,)
            replaced_after = (target.model_copy(update={'name': f'一括{rng.randrange(1000)}'}),)
        added: tuple[SceneEntity, ...] = ()
        if rng.random() < 0.6:
            added = (_entity(self.model.fresh_id(), kind='furniture', x=rng.uniform(0, 6)),)
        name = self._new_flag()
        use_sides = rng.random() < 0.5
        epoch = self.model.doc.history_epoch
        self.model.doc.apply_entity_set_edit(
            removed=removed,
            replaced_before=replaced_before,
            replaced_after=replaced_after,
            added=added,
            apply_side=(
                (lambda n=name: self.model.side_flags.__setitem__(n, True)) if use_sides else None
            ),
            revert_side=(
                (lambda n=name: self.model.side_flags.__setitem__(n, False)) if use_sides else None
            ),
        )
        if not use_sides:
            del self.model.side_flags[name]
        pushed = self.model.doc.history_epoch != epoch
        self.model.record_push(pushed)
        self._own_flag(name, pushed)

    def _op_room(self, rng: random.Random) -> None:
        room = _polygon_room(rng) if rng.random() < 0.4 else _room(rng)
        if rng.random() < 0.1:
            room = None
        epoch = self.model.doc.history_epoch
        self.model.doc.replace_room(room)
        self.model.record_push(self.model.doc.history_epoch != epoch)

    def _op_composite_side(self, rng: random.Random) -> None:
        """A state-only command: document may be unchanged; side flag toggles."""
        name = self._new_flag()
        inner = None
        ids = self.model.entity_ids()
        if ids and rng.random() < 0.5:
            entity_id = rng.choice(ids)
            entity = self.model.doc.committed_document.entity(entity_id)
            inner = TransformEntitiesCommand(
                before=(entity,),
                after=(entity.model_copy(update={'name': f'複合{rng.randrange(1000)}'}),),
            )
        pushed = self.model.doc.push_command(
            CompositeEditCommand(
                inner=inner,
                apply_side=lambda n=name: self.model.side_flags.__setitem__(n, True),
                revert_side=lambda n=name: self.model.side_flags.__setitem__(n, False),
            )
        )
        self.model.record_push(pushed)
        self._own_flag(name, pushed)

    def _op_attach(self, rng: random.Random) -> None:
        doc = self.model.doc
        committed = doc.committed_document
        physical = [e for e in committed.entities if e.kind in _PHYSICAL]
        if len(physical) < 2:
            if self.model.preview is not None:
                # The fallback add is a mutating op — it must refuse too.
                with pytest.raises(EditStateError):
                    doc.add_entity(_entity(self.model.fresh_id()))
                return
            return self._op_add(rng)
        attached_children = {
            a.child_entity_id for a in (committed.attachments or ())
        }
        parent_of = {a.child_entity_id: a.parent_entity_id for a in (committed.attachments or ())}
        child = rng.choice([e for e in physical if e.entity_id not in attached_children] or [None])
        if child is None:
            return
        # Parent must not be a descendant of the child (no cycles).
        candidates = []
        for parent in physical:
            if parent.entity_id == child.entity_id:
                continue
            node, seen = parent.entity_id, set()
            while node in parent_of and node not in seen:
                seen.add(node)
                node = parent_of[node]
            if child.entity_id not in seen and node != child.entity_id:
                candidates.append(parent)
        if not candidates:
            return
        parent = rng.choice(candidates)
        kind = rng.choice(['stand_on', 'placed_inside', 'stacked_on'])
        edge = EntityAttachment(
            attachment_id=f'att-{self.model.att_counter}',
            child_entity_id=child.entity_id,
            parent_entity_id=parent.entity_id,
            kind=kind,
            parent_anchor=(
                'top_surface' if kind in ('stand_on', 'stacked_on') else 'interior'
            ),
        )
        self.model.att_counter += 1
        after = committed.model_copy(
            update={'attachments': (committed.attachments or ()) + (edge,)}
        )
        if self.model.preview is not None:
            with pytest.raises(EditStateError):
                doc.replace_document(after)
            return
        epoch = doc.history_epoch
        doc.replace_document(after)
        self.model.record_push(doc.history_epoch != epoch)
        # Attached child positions are derived: committed projection must
        # equal the attachment-authority result, never the raw input.
        resolved = doc.committed_document.entity(child.entity_id)
        self.check(
            resolved.position is not None,
            'attached child lost its position',
        )

    def _op_detach(self, rng: random.Random) -> None:
        doc = self.model.doc
        edges = doc.committed_document.attachments or ()
        if not edges:
            return
        edge = rng.choice(list(edges))
        kept = tuple(a for a in edges if a.attachment_id != edge.attachment_id)
        after = doc.committed_document.model_copy(
            update={'attachments': kept or None}
        )
        if self.model.preview is not None:
            with pytest.raises(EditStateError):
                doc.replace_document(after)
            return
        epoch = doc.history_epoch
        doc.replace_document(after)
        self.model.record_push(doc.history_epoch != epoch)

    def _op_preview(self, rng: random.Random) -> None:
        doc = self.model.doc
        if self.model.preview is None:
            ids = self.model.entity_ids()
            if not ids:
                return
            kind = rng.choice(['move', 'rotate'])
            if rng.random() < 0.5:
                entity_id = rng.choice(ids)
                if kind == 'move':
                    doc.begin_move(entity_id)
                else:
                    doc.begin_rotate(entity_id)
                self.model.preview_ids = (entity_id,)
            else:
                chosen = tuple(rng.sample(ids, min(len(ids), rng.randint(1, 3))))
                if kind == 'move':
                    doc.begin_group_move(chosen)
                else:
                    doc.begin_group_rotate(chosen)
                self.model.preview_ids = chosen
            self.model.preview = kind
            self.check(
                scene_content_hash(doc.committed_document) == self.model.journal[self.model.pos],
                'opening a preview mutated the committed document',
            )
            return
        # Second phase: mutate the preview, then commit or cancel.
        kind = self.model.preview
        if kind == 'move' and len(self.model.preview_ids) == 1:
            doc.preview_move(
                Position3(x_m=rng.uniform(0, 6), y_m=rng.uniform(0, 5), z_m=rng.uniform(0, 3))
            )
        elif kind == 'move':
            doc.preview_group_move((rng.uniform(-1, 1), rng.uniform(-1, 1), 0.0))
        elif kind == 'rotate' and len(self.model.preview_ids) == 1:
            doc.preview_rotate(
                quaternion_from_euler_deg(
                    yaw_deg=rng.uniform(-180, 180), pitch_deg=0.0, roll_deg=0.0
                )
            )
        else:
            doc.preview_group_rotate(
                'y', rng.uniform(-90, 90), Position3(x_m=3.0, y_m=2.0, z_m=0.0)
            )
        self.check(
            scene_content_hash(doc.committed_document) == self.model.journal[self.model.pos],
            'preview update leaked into the committed document',
        )
        if rng.random() < 0.55:
            epoch = doc.history_epoch
            doc.commit_preview()
            self.model.preview = None
            self.model.record_push(doc.history_epoch != epoch)
        else:
            self.check(doc.cancel_preview(), 'cancel_preview refused a live preview')
            self.model.preview = None

    def _op_merge(self, rng: random.Random) -> None:
        count = rng.randint(2, 4)
        if count > self.model.pos:
            self.check(not self.model.doc.merge_last(count), 'merge_last beyond applied depth')
            return
        self.check(self.model.doc.merge_last(count), 'merge_last inside applied depth refused')
        self.model.merge(count)

    def _op_undo(self, rng: random.Random) -> None:
        doc = self.model.doc
        before = self.model.pos
        changed = doc.undo()
        self.check(
            changed == (doc.history_index != before),
            'undo return value disagrees with index movement',
        )
        if changed:
            self.model.pos = doc.history_index
        self.check(
            scene_content_hash(doc.committed_document) == self.model.journal[self.model.pos],
            'undo landed on content the journal never recorded',
        )

    def _op_redo(self, rng: random.Random) -> None:
        doc = self.model.doc
        before = self.model.pos
        changed = doc.redo()
        self.check(
            changed == (doc.history_index != before),
            'redo return value disagrees with index movement',
        )
        if changed:
            self.model.pos = doc.history_index
        self.check(
            scene_content_hash(doc.committed_document) == self.model.journal[self.model.pos],
            'redo landed on content the journal never recorded',
        )

    def _op_mark_saved(self, rng: random.Random) -> None:
        actual = scene_content_hash(self.model.doc.committed_document)
        if self.model.preview is not None:
            with pytest.raises(EditStateError):
                self.model.doc.mark_saved('rev-x', actual)
            return
        if rng.random() < 0.25:
            with pytest.raises(EditStateError):
                self.model.doc.mark_saved('rev-bad', 'deadbeef' * 8)
            return
        revision = f'rev-{self.model.pushed}'
        self.model.doc.mark_saved(revision, actual)
        self.model.saved_hash = actual
        self.check(
            self.model.doc.source_revision_id == revision,
            'mark_saved did not record the revision id',
        )

    def _op_reorder_doc(self, rng: random.Random) -> None:
        doc = self.model.doc
        entities = list(doc.committed_document.entities)
        after = doc.committed_document.model_copy(
            update={'entities': tuple(entities)}
        )
        if self.model.preview is not None:
            with pytest.raises(EditStateError):
                doc.replace_document(after)
            return
        if len(entities) < 2:
            return
        rng.shuffle(entities)
        after = doc.committed_document.model_copy(
            update={'entities': tuple(entities)}
        )
        epoch = doc.history_epoch
        doc.replace_document(after)
        self.model.record_push(doc.history_epoch != epoch)

    def _op_bad_op(self, rng: random.Random) -> None:
        """Illegal op: must be rejected loudly and must never mutate."""
        doc = self.model.doc
        choice = rng.randrange(4)
        before_hash = scene_content_hash(doc.committed_document)
        if choice == 0:
            with pytest.raises(EditStateError):
                doc.move_entity('ghost-entity', Position3(x_m=0, y_m=0, z_m=0))
        elif choice == 1:
            existing = doc.committed_document.entities
            if existing:
                with pytest.raises(EditStateError):
                    doc.add_entity(existing[0])
        elif choice == 2:
            with pytest.raises((EditStateError, ValueError)):
                doc.delete_entities(('ghost-a', 'ghost-b'))
        else:
            existing = doc.committed_document.entities
            if existing:
                with pytest.raises(EditStateError):
                    doc.transform_entities(
                        (existing[0],),
                        (_entity('ghost', kind='furniture'),),
                    )
        self.check(
            scene_content_hash(doc.committed_document) == before_hash,
            'a rejected op still mutated the document',
        )


def _run_history_walk(seed: int) -> None:
    rng = random.Random(seed)
    walk = _HistoryWalk(seed, limit=rng.choice([4, 8, 500]), schema5=rng.random() < 0.35)
    walk.run(60)


@pytest.mark.parametrize('seed', range(160))
def test_history_state_machine_walk(seed: int) -> None:
    _run_history_walk(seed)


# ---------------------------------------------------------------------------
# Surface 2 — document lifecycle: controller + library + repository.
# ---------------------------------------------------------------------------

_OBJECT_KINDS = ('speaker', 'seat', 'screen', 'display', 'furniture', 'av_equipment', 'measurement_point')

# Actions each dirty-state classification is documented to resolve through.
_RESOLUTIONS_FOR_STATE: dict[str, tuple[str, ...]] = {
    'preview_active': ('commit_preview', 'cancel_preview', 'discard'),
    'dirty_recoverable': ('save', 'discard', 'keep_draft'),
    'recovery_candidate_pending': ('recover_draft', 'discard_recovery', 'discard'),
}
# Applicability is the inner operation's own precondition, not the
# dirty-state label: a pending recovery candidate can coexist with dirty
# edits or a preview, and the resolver must report cleared=True exactly
# when the requested action genuinely ran.
def _resolution_applicable(ctrl: RoomWorkspaceController, action: str) -> bool:
    if action in ('save', 'discard', 'keep_draft'):
        # save() refuses loudly over a preview or a pending candidate;
        # discard/keep_draft are total — they always run.
        return action != 'save' or (
            not ctrl.working.has_preview and ctrl.recovery_candidate is None
        )
    if action in ('commit_preview', 'cancel_preview'):
        return ctrl.working.has_preview
    if action in ('recover_draft', 'discard_recovery'):
        return ctrl.recovery_candidate is not None
    return False


_ALL_ACTIONS = (
    'save', 'discard', 'keep_draft', 'commit_preview', 'cancel_preview',
    'recover_draft', 'discard_recovery', 'discard_pending', 'stop_busy',
)


@dataclass
class _DocModel:
    doc_id: str
    project_id: str
    archived: bool = False
    ctrl: RoomWorkspaceController | None = None
    head_hash: str | None = None
    saved_hash: str | None = None
    recovery_hash: str | None = None
    release_set: bool = False
    release_hash: str | None = None
    release_rev: str | None = None


class _DocumentWalk(_Walk):
    def __init__(self, seed: int, root: Path) -> None:
        super().__init__('document', seed)
        root.mkdir(parents=True, exist_ok=True)
        self.repository = SceneRepository(root / 'scenes.sqlite3')
        self.library = ProjectLibraryRepository(self.repository)
        self.docs: list[_DocModel] = []
        self.active: _DocModel | None = None

    def ops(self) -> list[tuple[str, float]]:
        return [
            ('create_open', 12),
            ('switch_project', 10),
            ('close_reopen', 7),
            ('edit_room', 8),
            ('add_object', 10),
            ('update_entity', 8),
            ('delete_entities', 7),
            ('duplicate', 5),
            ('select_hide_lock', 4),
            ('undo', 8),
            ('redo', 6),
            ('save', 8),
            ('restore_revision', 6),
            ('dirty_resolution', 6),
            ('draft_recover', 4),
            ('reload', 3),
            ('archive_toggle', 2),
            ('library_read', 4),
            ('revision_labels', 2),
        ]

    # -- model helpers ------------------------------------------------------

    def _sync_recovery_model(self, model: _DocModel) -> None:
        """Mirror ``_sync_recovery``: while a candidate is pending the store
        is untouched; otherwise the snapshot is the committed document iff
        dirty, cleared when clean."""
        ctrl = model.ctrl
        assert ctrl is not None
        if ctrl.recovery_candidate is not None:
            return
        model.recovery_hash = (
            scene_content_hash(ctrl.committed_document)
            if ctrl.working.is_dirty
            else None
        )

    def _open_model(self, model: _DocModel) -> None:
        self.check(not model.archived, 'attempted to open an archived project')
        self.library.open_project(model.project_id)
        ctrl = RoomWorkspaceController(self.repository, model.doc_id)
        model.ctrl = ctrl
        head = self.repository.current_head(model.doc_id)
        self.check(head is not None, f'controller bound {model.doc_id} without a head')
        if model.head_hash is not None:
            self.check(
                head.content_hash == model.head_hash,
                'head moved while the document had no open controller',
            )
        self.check(
            ctrl.committed_document == head.document,
            'reopened document diverges from the persisted head',
        )
        self.check(
            scene_content_hash(head.document) == head.content_hash,
            'persisted head content hash does not round-trip',
        )
        model.head_hash = head.content_hash
        model.saved_hash = head.content_hash
        model.release_set = False
        candidate = ctrl.recovery_candidate
        self.check(
            (candidate is not None) == (model.recovery_hash is not None),
            f'recovery presence {candidate is not None} != expected {model.recovery_hash is not None}',
        )
        if candidate is not None:
            self.check(
                scene_content_hash(candidate.document) == model.recovery_hash,
                'recovery snapshot content != last synced committed state',
            )
        self.active = model

    def _close_model(self, model: _DocModel) -> None:
        ctrl = model.ctrl
        if ctrl is None:
            return
        ctrl.close()
        if ctrl.recovery_candidate is None:
            # _sync_recovery writes the dirty draft; a pending candidate
            # freezes the persisted row — the old snapshot survives close.
            model.recovery_hash = (
                scene_content_hash(ctrl.committed_document)
                if ctrl.working.is_dirty
                else None
            )
        model.ctrl = None
        if self.active is model:
            self.active = None

    def _apply_resolution_to_model(self, model: _DocModel, action: str) -> None:
        ctrl = model.ctrl
        assert ctrl is not None
        if action == 'save':
            head = self.repository.current_head(model.doc_id)
            self.check(head is not None, 'resolution save left no head')
            model.saved_hash = head.content_hash
            model.head_hash = head.content_hash
            model.recovery_hash = None
            model.release_set = False
        elif action == 'discard':
            head = self.repository.current_head(model.doc_id)
            self.check(head is not None, 'resolution discard left no head')
            model.saved_hash = head.content_hash
            model.head_hash = head.content_hash
            model.recovery_hash = None
            model.release_set = False
        elif action == 'keep_draft':
            model.release_set = True
            model.release_hash = scene_content_hash(ctrl.committed_document)
            model.release_rev = ctrl.working.source_revision_id
            self._sync_recovery_model(model)
        elif action == 'recover_draft':
            # The persisted row survives recovery; the next _sync owns it.
            model.saved_hash = ctrl.working.saved_content_hash
            model.release_set = False
        elif action == 'discard_recovery':
            head = self.repository.current_head(model.doc_id)
            model.saved_hash = head.content_hash
            model.head_hash = head.content_hash
            model.recovery_hash = None
            model.release_set = False
        else:  # commit_preview / cancel_preview — committed may have changed
            self._sync_recovery_model(model)

    def _release_current(self, model: _DocModel) -> bool:
        ctrl = model.ctrl
        return (
            model.release_set
            and ctrl is not None
            and model.release_rev == ctrl.working.source_revision_id
            and model.release_hash == scene_content_hash(ctrl.committed_document)
        )

    def _expected_dirty_state(self, model: _DocModel) -> str:
        ctrl = model.ctrl
        assert ctrl is not None
        if ctrl.working.has_preview:
            return 'preview_active'
        if ctrl.is_dirty:
            return 'clean' if self._release_current(model) else 'dirty_recoverable'
        if ctrl.recovery_candidate is not None:
            return 'recovery_candidate_pending'
        return 'clean'

    def _ensure_deactivatable(self, model: _DocModel, rng: random.Random) -> bool:
        ctrl = model.ctrl
        assert ctrl is not None
        for _attempt in range(3):
            ok, _msg = ctrl.before_deactivate()
            if ok:
                return True
            state = ctrl.dirty_state()
            actions = _RESOLUTIONS_FOR_STATE.get(state, ())
            self.check(bool(actions), f'unresolvable dirty_state {state}')
            if not actions:
                return False
            action = rng.choice(actions)
            cleared, _ = ctrl.resolve_dirty_state(action)
            self.check(cleared, f'resolution {action} refused on {state}')
            if not cleared:
                return False
            self._apply_resolution_to_model(model, action)
        ok, _msg = ctrl.before_deactivate()
        self.check(ok, 'deactivation still blocked after 3 resolutions')
        return ok

    def _post_op_document_checks(self, model: _DocModel) -> None:
        ctrl = model.ctrl
        assert ctrl is not None
        committed = ctrl.committed_document
        if len(self.trace) % 5 == 0:
            _validate_document(committed)
        working = ctrl.working
        expected_dirty = (
            scene_content_hash(committed) != (model.saved_hash or '')
        )
        self.check(
            working.is_dirty == expected_dirty,
            f'working.is_dirty={working.is_dirty} expected {expected_dirty}',
        )
        self.check(
            ctrl.is_dirty == expected_dirty,
            f'controller.is_dirty={ctrl.is_dirty} expected {expected_dirty}',
        )
        self.check(
            working.history_index <= working.history_length,
            'history index beyond length',
        )
        self.check(working.can_undo == (working.history_index > 0), 'can_undo wrong')
        self.check(
            working.can_redo == (working.history_index < working.history_length),
            'can_redo wrong',
        )
        entity_ids = {entity.entity_id for entity in ctrl.document.entities}
        self.check(
            set(ctrl.view_state.selection) <= entity_ids,
            f'selection references unknown entities: {ctrl.view_state.selection}',
        )
        self.check(
            ctrl.view_state.hidden_ids <= entity_ids
            and ctrl.view_state.locked_ids <= entity_ids,
            'hidden/locked ids reference unknown entities',
        )
        self.check(
            ctrl.dirty_state() == self._expected_dirty_state(model),
            f'dirty_state {ctrl.dirty_state()} != expected '
            f'{self._expected_dirty_state(model)}',
        )
        head = self.repository.current_head(model.doc_id)
        if model.head_hash is not None:
            self.check(
                head is not None and head.content_hash == model.head_hash,
                'document head regressed',
            )
        if len(self.trace) % 4 == 0:
            revisions = self.repository.list_revisions(model.doc_id)
            ids = {rev.revision_id for rev in revisions}
            if head is not None:
                self.check(head.revision_id in ids, 'head missing from revision list')
            for rev in revisions:
                self.check(
                    rev.parent_revision_id is None or rev.parent_revision_id in ids,
                    f'revision {rev.revision_id} has a dangling parent',
                )
                self.check(
                    scene_content_hash(rev.document) == rev.content_hash,
                    f'revision {rev.revision_id} content hash does not round-trip',
                )
            summaries = self.repository.list_revision_summaries(model.doc_id)
            self.check(
                [s.revision_id for s in summaries]
                == [r.revision_id for r in revisions],
                'revision summaries diverge from revision list',
            )
        recovery = self.repository.recovery(model.doc_id)
        if model.recovery_hash is None:
            self.check(
                recovery is None,
                'persisted recovery snapshot exists but none expected',
            )
        else:
            self.check(
                recovery is not None,
                'expected recovery snapshot missing',
            )
            if recovery is not None:
                self.check(
                    scene_content_hash(recovery.document) == model.recovery_hash,
                    'persisted recovery content != last synced committed state',
                )

    def _run(self, idx: int, rng: random.Random) -> None:
        name = self.ops()[idx][0]
        getattr(self, f'_op_{name}')(rng)
        if self.active is not None and self.active.ctrl is not None:
            self._post_op_document_checks(self.active)

    # -- ops --------------------------------------------------------------

    def _op_create_open(self, rng: random.Random) -> None:
        entry = self.library.create_project(f'プロジェクト{rng.randrange(10000)}')
        model = _DocModel(doc_id=entry.document_id, project_id=entry.project_id)
        self.docs.append(model)
        self._open_model(model)

    def _op_switch_project(self, rng: random.Random) -> None:
        candidates = [
            m for m in self.docs if not m.archived and m is not self.active
        ]
        if not candidates:
            return self._op_create_open(rng)
        target = rng.choice(candidates)
        if self.active is not None and self.active.ctrl is not None:
            if not self._ensure_deactivatable(self.active, rng):
                return
            self._close_model(self.active)
        self._open_model(target)

    def _op_close_reopen(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        self._close_model(model)
        if model.archived:
            with pytest.raises(ProjectArchivedError):
                self.library.open_project(model.project_id)
            return
        self._open_model(model)

    def _op_edit_room(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        room = _polygon_room(rng) if rng.random() < 0.35 else _room(rng)
        if rng.random() < 0.1:
            room = None
        try:
            model.ctrl.replace_room(room)
        except EditStateError:
            self.check(
                model.ctrl.recovery_candidate is not None
                or model.ctrl.working.has_preview,
                'replace_room rejected without pending recovery/preview',
            )
            return
        self._sync_recovery_model(model)

    def _op_add_object(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        if not model.ctrl.can_edit:
            with pytest.raises(EditStateError):
                model.ctrl.add_object(rng.choice(_OBJECT_KINDS))
            return
        entity = model.ctrl.add_object(rng.choice(_OBJECT_KINDS))
        self.check(
            entity.entity_id
            in {e.entity_id for e in model.ctrl.document.entities},
            'add_object returned an entity absent from the document',
        )
        self._sync_recovery_model(model)

    def _op_update_entity(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ids = [e.entity_id for e in model.ctrl.document.entities]
        if not ids:
            return
        entity_id = rng.choice(ids)
        changed = model.ctrl.update_entities(
            {entity_id: {'name': f'更新{rng.randrange(1000)}'}}
        )
        if not model.ctrl.can_edit:
            self.check(not changed, 'update_entities edited while not editable')
            return
        self._sync_recovery_model(model)

    def _op_delete_entities(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ids = [e.entity_id for e in model.ctrl.document.entities]
        if not ids:
            return
        chosen = tuple(rng.sample(ids, min(len(ids), rng.randint(1, 3))))
        if not model.ctrl.can_edit:
            self.check(
                model.ctrl.delete_entities(chosen) == 0,
                'delete_entities removed entities while not editable',
            )
            return
        removed = model.ctrl.delete_entities(chosen)
        self.check(bool(removed), 'delete_entities removed nothing while editable')
        remaining = {e.entity_id for e in model.ctrl.document.entities}
        for entity_id in chosen:
            self.check(entity_id not in remaining, 'deleted entity still in document')
        self._sync_recovery_model(model)

    def _op_duplicate(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        ids = [e.entity_id for e in ctrl.document.entities]
        if not ids:
            return
        chosen = tuple(rng.sample(ids, min(len(ids), rng.randint(1, 2))))
        ctrl.set_selection_many(chosen)
        locked = any(ctrl.view_state.is_locked(eid) for eid in chosen)
        expected = len(chosen) if ctrl.can_edit and not locked else 0
        created = ctrl.duplicate_selected()
        self.check(
            created == expected,
            f'duplicate_selected={created} expected {expected}',
        )
        entity_ids = [e.entity_id for e in ctrl.document.entities]
        self.check(
            len(entity_ids) == len(set(entity_ids)),
            'duplicate_selected produced duplicate ids',
        )
        self._sync_recovery_model(model)

    def _op_select_hide_lock(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        ids = [e.entity_id for e in ctrl.document.entities]
        if not ids:
            ctrl.set_selection(None)
            return
        chosen = rng.sample(ids, min(len(ids), rng.randint(1, 3)))
        ctrl.set_selection_many(chosen)
        ctrl.set_entities_hidden(tuple(chosen[:1]), rng.random() < 0.5)
        ctrl.set_entities_locked(tuple(chosen[:1]), rng.random() < 0.4)

    def _op_undo(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        if ctrl.recovery_candidate is not None:
            self.check(not ctrl.undo(), 'undo ran while a recovery candidate is pending')
            return
        before = ctrl.working.history_index
        before_hash = scene_content_hash(ctrl.committed_document)
        changed = ctrl.undo()
        self.check(
            changed == (ctrl.working.history_index != before),
            'undo truth disagrees with index movement',
        )
        if not changed:
            self.check(
                scene_content_hash(ctrl.committed_document) == before_hash,
                'failed undo still changed the committed document',
            )
        self._sync_recovery_model(model)

    def _op_redo(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        if ctrl.recovery_candidate is not None:
            self.check(not ctrl.redo(), 'redo ran while a recovery candidate is pending')
            return
        before = ctrl.working.history_index
        before_hash = scene_content_hash(ctrl.committed_document)
        changed = ctrl.redo()
        self.check(
            changed == (ctrl.working.history_index != before),
            'redo truth disagrees with index movement',
        )
        if not changed:
            self.check(
                scene_content_hash(ctrl.committed_document) == before_hash,
                'failed redo still changed the committed document',
            )
        self._sync_recovery_model(model)

    def _op_save(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        if ctrl.recovery_candidate is not None or ctrl.working.has_preview:
            with pytest.raises(EditStateError):
                ctrl.save()
            return
        ctrl.save()
        head = self.repository.current_head(model.doc_id)
        self.check(head is not None, 'save produced no head')
        self.check(
            head.content_hash == scene_content_hash(ctrl.committed_document),
            'saved head diverges from working content',
        )
        self.check(not ctrl.is_dirty, 'document still dirty after save')
        model.head_hash = head.content_hash
        model.saved_hash = head.content_hash
        model.recovery_hash = None
        model.release_set = False

    def _op_restore_revision(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        revisions = self.repository.list_revisions(model.doc_id)
        if not revisions:
            return
        revision = rng.choice(revisions)
        head = self.repository.current_head(model.doc_id)
        legal = (
            model.ctrl.recovery_candidate is None
            and not model.ctrl.working.has_preview
            and not model.ctrl.is_dirty
            and (head is None or revision.revision_id != head.revision_id)
        )
        if not legal:
            with pytest.raises(EditStateError):
                model.ctrl.restore_revision(revision.revision_id)
            return
        old_head_id = head.revision_id if head is not None else None
        model.ctrl.restore_revision(revision.revision_id)
        new_head = self.repository.current_head(model.doc_id)
        self.check(new_head is not None, 'restore produced no head')
        self.check(
            scene_content_hash(new_head.document)
            == scene_content_hash(revision.document),
            'restored head content != target revision content',
        )
        self.check(
            scene_content_hash(model.ctrl.committed_document)
            == scene_content_hash(revision.document),
            'restored working document != target revision',
        )
        self.check(not model.ctrl.is_dirty, 'document dirty right after restore')
        if new_head.revision_id != old_head_id:
            self.check(
                new_head.parent_revision_id == old_head_id,
                'restored head did not descend from the prior head',
            )
        model.saved_hash = new_head.content_hash
        model.head_hash = new_head.content_hash
        model.recovery_hash = None
        model.release_set = False

    def _op_dirty_resolution(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        state = ctrl.dirty_state()
        self.check(
            state == self._expected_dirty_state(model),
            f'dirty_state {state} != expected {self._expected_dirty_state(model)}',
        )
        action = rng.choice(_ALL_ACTIONS)
        applicable = _resolution_applicable(ctrl, action)
        cleared, _message = ctrl.resolve_dirty_state(action)
        self.check(
            cleared == applicable,
            f'resolution {action} on {state}: cleared={cleared} '
            f'(applicable={applicable}) — resolver claims an effect that never ran',
        )
        if cleared:
            self._apply_resolution_to_model(model, action)

    def _op_draft_recover(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        if ctrl.recovery_candidate is None:
            self.check(not ctrl.recover_draft(), 'recover_draft without a candidate succeeded')
            self.check(not ctrl.discard_recovery(), 'discard_recovery without a candidate succeeded')
            return
        if rng.random() < 0.5:
            self.check(ctrl.recover_draft(), 'recover_draft refused a pending candidate')
            model.saved_hash = ctrl.working.saved_content_hash
            model.release_set = False
            self._sync_recovery_model(model)
        else:
            self.check(ctrl.discard_recovery(), 'discard_recovery refused a pending candidate')
            head = self.repository.current_head(model.doc_id)
            model.saved_hash = head.content_hash
            model.head_hash = head.content_hash
            model.recovery_hash = None
            model.release_set = False

    def _op_reload(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        ctrl = model.ctrl
        blocked = (
            ctrl.is_dirty
            or ctrl.working.has_preview
            or ctrl.recovery_candidate is not None
        )
        reloaded = ctrl.reload_if_clean()
        if blocked:
            self.check(not reloaded, 'reload_if_clean reloaded over pending state')
        if reloaded:
            head = self.repository.current_head(model.doc_id)
            model.saved_hash = head.content_hash
            model.head_hash = head.content_hash
            model.release_set = False

    def _op_archive_toggle(self, rng: random.Random) -> None:
        model = self.active
        if model is None:
            return
        self.library.set_archived(model.project_id, not model.archived)
        model.archived = not model.archived
        if model.archived:
            with pytest.raises(ProjectArchivedError):
                self.library.open_project(model.project_id)

    def _op_library_read(self, rng: random.Random) -> None:
        entries = self.library.list_projects()
        expected_active = sum(1 for m in self.docs if not m.archived)
        self.check(
            len(entries) == expected_active,
            f'library lists {len(entries)} active, model expects {expected_active}',
        )
        all_entries = self.library.list_projects(include_archived=True)
        self.check(
            len(all_entries) == len(self.docs),
            f'include_archived lists {len(all_entries)}, model has {len(self.docs)}',
        )
        doc_ids = [entry.document_id for entry in all_entries]
        self.check(len(doc_ids) == len(set(doc_ids)), 'library returned duplicate documents')
        for entry in entries:
            resolved = self.library.get_project(entry.project_id)
            self.check(
                resolved.document_id == entry.document_id,
                'phantom project row resolves differently',
            )
        recent = self.library.recent_projects(limit=3)
        self.check(
            recent == entries[:3],
            'recent_projects is not a prefix of list_projects',
        )
        most_recent = self.library.most_recent_project()
        self.check(
            most_recent == (entries[0] if entries else None),
            'most_recent_project disagrees with ordering',
        )
        opened = [e for e in entries if e.last_opened_at_utc is not None]
        never = [e for e in entries if e.last_opened_at_utc is None]
        self.check(
            len(opened) + len(never) == len(entries),
            'opened/unopened split lost rows',
        )
        self.check(
            entries[: len(opened)] == tuple(opened),
            'never-opened project outranks an opened one',
        )
        for earlier, later in zip(opened, opened[1:]):
            self.check(
                earlier.last_opened_at_utc >= later.last_opened_at_utc,
                'opened ordering is not last_opened desc',
            )
        for earlier, later in zip(never, never[1:]):
            self.check(
                earlier.created_at_utc >= later.created_at_utc,
                'unopened tail is not created desc',
            )

    def _op_revision_labels(self, rng: random.Random) -> None:
        model = self.active
        if model is None or model.ctrl is None:
            return
        revisions = self.repository.list_revisions(model.doc_id)
        if not revisions:
            return
        revision = rng.choice(revisions)
        if rng.random() < 0.6:
            model.ctrl.set_revision_label(
                revision.revision_id, f'ラベル{rng.randrange(100)}', 'note'
            )
            labels = self.repository.revision_labels(model.doc_id)
            self.check(
                revision.revision_id in labels,
                'set label missing from revision_labels',
            )
        else:
            model.ctrl.set_revision_label(revision.revision_id, '', '')
            labels = self.repository.revision_labels(model.doc_id)
            self.check(
                revision.revision_id not in labels,
                'cleared label still present',
            )


def _run_document_walk(seed: int, tmp_path: Path) -> None:
    walk = _DocumentWalk(seed, tmp_path / f'walk-{seed}')
    walk.run(34)


@pytest.mark.parametrize('seed', range(80))
def test_document_state_machine_walk(seed: int, tmp_path: Path) -> None:
    _run_document_walk(seed, tmp_path)


# ---------------------------------------------------------------------------
# Surface 3 — ActivityCenter operation registry walks.
# ---------------------------------------------------------------------------

_ACTIVITY_KINDS = ('prediction', 'search', 'rew_import', 'bundle_export', 'backup')
_AUTHORITIES = ('auth-room', 'auth-spec', 'auth-measure', 'auth-variant')

# Public-API legality contract — the declared user-facing transition set,
# written from the documented semantics (not copied from the private table).
_LEGAL_PUBLIC = {
    'mark_preflighting': {OperationState.QUEUED},
    'mark_running': {OperationState.QUEUED, OperationState.PREFLIGHTING},
    'complete': {OperationState.RUNNING, OperationState.CANCELLATION_REQUESTED},
    'fail': {
        OperationState.QUEUED,
        OperationState.PREFLIGHTING,
        OperationState.RUNNING,
        OperationState.CANCELLATION_REQUESTED,
    },
    'confirm_cancelled': {
        OperationState.QUEUED,
        OperationState.PREFLIGHTING,
        OperationState.RUNNING,
        OperationState.CANCELLATION_REQUESTED,
    },
}
_ACTIVE_STATES = {
    OperationState.QUEUED,
    OperationState.PREFLIGHTING,
    OperationState.RUNNING,
    OperationState.CANCELLATION_REQUESTED,
}
_TERMINAL_STATES = {
    OperationState.CANCELLED,
    OperationState.COMPLETED,
    OperationState.FAILED,
    OperationState.COMPLETED_FOR_HISTORICAL_INPUT,
    OperationState.RESULT_STALE,
}


@dataclass
class _OpModel:
    operation_id: str
    state: OperationState = OperationState.QUEUED
    refs: frozenset[str] = frozenset()
    cancellability: Cancellability = Cancellability.NOT_CANCELLABLE
    retry_policy: RetryPolicy = RetryPolicy.NONE
    nav: NavigationPolicy = NavigationPolicy.BACKGROUNDABLE
    cancel_committed: bool = False
    current_for_input: bool = True
    attempt: int = 1
    retry_of: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state in _ACTIVE_STATES

    @property
    def can_cancel_now(self) -> bool:
        return (
            self.is_active
            and self.state != OperationState.CANCELLATION_REQUESTED
            and not self.cancel_committed
            and self.cancellability
            in (Cancellability.CANCELLABLE, Cancellability.CANCEL_UNTIL_COMMIT)
        )


class _ActivityWalk(_Walk):
    def __init__(self, seed: int, root: Path) -> None:
        super().__init__('activity', seed)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.center = ActivityCenter(history_limit=9, record_limit=9)
        self.models: dict[str, _OpModel] = {}
        self.order: list[str] = []
        self.counter = 0
        self._cancel_counters: dict[str, dict] = {}
        # Mirror of _records insertion order + _history rows for the
        # resolvability contract.
        self.records_order: list[str] = []
        self.history_model: list[str] = []

    def ops(self) -> list[tuple[str, float]]:
        return [
            ('submit', 16),
            ('preflight', 7),
            ('running', 9),
            ('progress', 6),
            ('commit_point', 4),
            ('complete', 8),
            ('fail', 6),
            ('request_cancel', 8),
            ('confirm_cancelled', 5),
            ('retry', 5),
            ('authorities_changed', 6),
            ('persist_load', 4),
            ('bad_submit', 4),
        ]

    def _pick_model(self, rng: random.Random) -> _OpModel | None:
        resolvable = [oid for oid in self.order if self._resolvable(oid)]
        if not resolvable:
            return None
        return self.models[rng.choice(resolvable)]

    def _resolvable(self, operation_id: str) -> bool:
        return (
            operation_id in self.records_order
            or operation_id in self.history_model
        )

    def _new_id(self) -> str:
        self.counter += 1
        return f'op-{self.counter}'

    def _run(self, idx: int, rng: random.Random) -> None:
        name = self.ops()[idx][0]
        getattr(self, f'_op_{name}')(rng)
        self._post_op_checks()

    def _eviction_model(self) -> None:
        """Mirror _archive: bounded history rows, terminal-first record trims."""
        while len(self.history_model) > 9:
            del self.history_model[0]
        excess = len(self.records_order) - 9
        if excess <= 0:
            return
        kept: list[str] = []
        for operation_id in self.records_order:
            model = self.models[operation_id]
            if excess > 0 and not model.is_active:
                excess -= 1
                continue
            kept.append(operation_id)
        self.records_order = kept

    def _terminal(self, model: _OpModel, state: OperationState) -> None:
        model.state = state
        if model.operation_id not in self.history_model:
            self.history_model.append(model.operation_id)
        self._eviction_model()

    def _post_op_checks(self) -> None:
        center = self.center
        self.check(
            set(center._records) == set(self.records_order),  # noqa: SLF001
            'record store diverges from model',
        )
        history_ids = [op.operation_id for op in center._history]  # noqa: SLF001
        self.check(
            history_ids == self.history_model,
            f'history rows diverge: {history_ids} != {self.history_model}',
        )
        for operation_id in self.order:
            model = self.models[operation_id]
            snapshot = center.get(operation_id)
            if self._resolvable(operation_id):
                self.check(
                    snapshot is not None,
                    f'{operation_id} unresolvable while still retained',
                )
            else:
                self.check(
                    snapshot is None,
                    f'evicted {operation_id} still resolves',
                )
                continue
            if snapshot is None:
                continue
            try:
                ApplicationOperation.model_validate(snapshot.model_dump(mode='python'))
            except Exception as exc:  # snapshot violates its own contract
                self.check(False, f'{operation_id} snapshot fails revalidation: {exc}')
            self.check(
                snapshot.state == model.state,
                f'{operation_id} state {snapshot.state} != model {model.state}',
            )
            self.check(
                snapshot.current_for_input == model.current_for_input,
                f'{operation_id} current_for_input drifted',
            )
            self.check(
                snapshot.cancel_committed == model.cancel_committed,
                f'{operation_id} cancel_committed drifted',
            )
            if model.state in _TERMINAL_STATES:
                self.check(
                    snapshot.finished_at is not None,
                    'terminal op missing finished_at',
                )
            if model.state in (OperationState.RUNNING, OperationState.CANCELLATION_REQUESTED):
                self.check(
                    snapshot.started_at is not None,
                    'running-family op missing started_at',
                )
        active = center.active()
        active_ids = {op.operation_id for op in active}
        self.check(
            active_ids
            == {oid for oid in self.order if self.models[oid].is_active},
            'active() set disagrees with the model',
        )
        for op in active:
            self.check(
                op.operation_id in center._records,  # noqa: SLF001
                'an active op was evicted from records',
            )
        recent = center.recent(limit=100)
        recent_ids = [op.operation_id for op in recent]
        self.check(
            len(recent_ids) == len(set(recent_ids)),
            'history contains duplicate operation ids',
        )
        for op in recent:
            self.check(
                op.state in _TERMINAL_STATES,
                f'non-terminal {op.operation_id} in history',
            )
        self.check(len(recent) <= 9, 'history exceeds its bound')
        failed = center.failed(limit=100)
        for op in failed:
            self.check(
                op.state == OperationState.FAILED,
                'failed() returned a non-failed op',
            )
        blockers = center.navigation_blockers()
        for op in blockers:
            self.check(
                op.state in _ACTIVE_STATES
                and op.navigation_policy == NavigationPolicy.EXCLUSIVE,
                'navigation blocker is not an active exclusive op',
            )
        self.check(
            center.navigation_blocked() == bool(blockers),
            'navigation_blocked() lies',
        )
        self.check(
            len(center._records) - len(active_ids) <= 9,  # noqa: SLF001
            'record store kept more terminal records than the bound',
        )

    # -- ops --------------------------------------------------------------

    def _op_submit(self, rng: random.Random) -> None:
        cancellability = rng.choice(list(Cancellability))
        nav = rng.choice(list(NavigationPolicy))
        refs = frozenset(rng.sample(_AUTHORITIES, rng.randint(0, 3)))
        model = _OpModel(
            operation_id=self._new_id(),
            cancellability=cancellability,
            retry_policy=rng.choice(list(RetryPolicy)),
            nav=nav,
            refs=refs,
        )
        counter = {'count': 0}

        def _cancel() -> None:
            counter['count'] += 1

        callback = _cancel if cancellability != Cancellability.NOT_CANCELLABLE else None
        operation_id = self.center.submit(
            operation_kind=rng.choice(_ACTIVITY_KINDS),
            operation_class=rng.choice(list(OperationClass)),
            title=f'処理{self.counter}',
            input_authority_refs=tuple(sorted(refs)),
            cancellability=cancellability,
            retry_policy=model.retry_policy,
            navigation_policy=nav,
            navigation_block_reason=('占有中' if nav == NavigationPolicy.EXCLUSIVE else None),
            cancel_callback=callback,
            operation_id=model.operation_id,
        )
        self.check(operation_id == model.operation_id, 'submit returned a different id')
        self.models[operation_id] = model
        self.order.append(operation_id)
        self.records_order.append(operation_id)
        self._cancel_counters[operation_id] = counter

    def _op_preflight(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        if model.state in _LEGAL_PUBLIC['mark_preflighting']:
            self.center.mark_preflighting(model.operation_id)
            model.state = OperationState.PREFLIGHTING
        else:
            with pytest.raises(OperationTransitionError):
                self.center.mark_preflighting(model.operation_id)

    def _op_running(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        progress = (
            OperationProgress(kind=ProgressKind.DETERMINATE, fraction=rng.random())
            if rng.random() < 0.5
            else None
        )
        if model.state in _LEGAL_PUBLIC['mark_running']:
            self.center.mark_running(model.operation_id, progress)
            model.state = OperationState.RUNNING
        else:
            with pytest.raises(OperationTransitionError):
                self.center.mark_running(model.operation_id, progress)

    def _op_progress(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        progress = OperationProgress(
            kind=ProgressKind.ITEMS,
            done_units=rng.randint(0, 10),
            total_units=rng.randint(10, 100),
        )
        if model.is_active:
            self.center.update_progress(model.operation_id, progress)
        else:
            with pytest.raises(OperationTransitionError):
                self.center.update_progress(model.operation_id, progress)

    def _op_commit_point(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        if model.is_active:
            self.center.mark_commit_point(model.operation_id)
            model.cancel_committed = True
        else:
            with pytest.raises(OperationTransitionError):
                self.center.mark_commit_point(model.operation_id)

    def _op_complete(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        if model.state in _LEGAL_PUBLIC['complete']:
            self.center.complete(model.operation_id, result_summary='完了')
            self._terminal(model, OperationState.COMPLETED)
        else:
            with pytest.raises(OperationTransitionError):
                self.center.complete(model.operation_id)

    def _op_fail(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        if model.state in _LEGAL_PUBLIC['fail']:
            self.center.fail(model.operation_id, error_summary='失敗')
            self._terminal(model, OperationState.FAILED)
        else:
            with pytest.raises(OperationTransitionError):
                self.center.fail(model.operation_id, error_summary='失敗')

    def _op_request_cancel(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        expected = model.can_cancel_now
        granted = self.center.request_cancel(model.operation_id)
        self.check(
            granted == expected,
            f'request_cancel={granted} expected {expected}',
        )
        if granted:
            model.state = OperationState.CANCELLATION_REQUESTED
            counter = self._cancel_counters.get(model.operation_id)
            self.check(
                counter is not None and counter['count'] == 1,
                'cancel callback did not fire exactly once',
            )

    def _op_confirm_cancelled(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        if model.state in _LEGAL_PUBLIC['confirm_cancelled']:
            self.center.confirm_cancelled(model.operation_id)
            self._terminal(model, OperationState.CANCELLED)
        else:
            with pytest.raises(OperationTransitionError):
                self.center.confirm_cancelled(model.operation_id)

    def _op_retry(self, rng: random.Random) -> None:
        model = self._pick_model(rng)
        if model is None:
            return
        legal = (
            model.retry_policy == RetryPolicy.SAFE_NEW_ATTEMPT
            and not model.is_active
        )
        if not legal:
            with pytest.raises(OperationTransitionError):
                self.center.retry(model.operation_id)
            return
        counter = {'count': 0}

        def _factory(_snapshot: ApplicationOperation) -> OperationRetryRequest:
            return OperationRetryRequest(
                cancel_callback=lambda: counter.__setitem__('count', counter['count'] + 1),
            )

        new_id = self.center.retry(model.operation_id, retry_factory=_factory)
        self.check(new_id != model.operation_id, 'retry returned the same id')
        snapshot = self.center.require(new_id)
        self.check(snapshot.attempt == model.attempt + 1, 'retry attempt did not increment')
        self.check(
            snapshot.retry_of == model.operation_id,
            'retry lost its provenance',
        )
        self.check(
            snapshot.cancellability == model.cancellability,
            'retry dropped the cancellability contract',
        )
        child = _OpModel(
            operation_id=new_id,
            refs=model.refs,
            cancellability=model.cancellability,
            retry_policy=model.retry_policy,
            nav=model.nav,
            attempt=model.attempt + 1,
            retry_of=model.operation_id,
        )
        self.models[new_id] = child
        self.order.append(new_id)
        self.records_order.append(new_id)
        self._cancel_counters[new_id] = counter

    def _op_authorities_changed(self, rng: random.Random) -> None:
        changed = set(rng.sample(_AUTHORITIES, rng.randint(1, 3)))
        self.center.note_authorities_changed(changed)
        archived = False
        for model in self.models.values():
            if not model.refs or not model.current_for_input:
                continue
            if not changed.intersection(model.refs):
                continue
            if model.state == OperationState.COMPLETED:
                model.state = OperationState.COMPLETED_FOR_HISTORICAL_INPUT
                # Only a records-resident COMPLETED reclassification routes
                # through _transition -> _archive and its bound eviction.
                archived = archived or model.operation_id in self.records_order
            model.current_for_input = False
        if archived:
            self._eviction_model()

    def _op_persist_load(self, rng: random.Random) -> None:
        path = self.root / f'activity-{self.seed}.json'
        self.center.persist_history(path)
        loaded = ActivityCenter.load_history(path)
        recent = self.center.recent(limit=1000)
        self.check(
            [op.operation_id for op in loaded]
            == [op.operation_id for op in recent],
            'persisted history diverges from live history',
        )
        for op in loaded:
            ApplicationOperation.model_validate(op.model_dump(mode='python'))
        loaded_active = ActivityCenter.load_active_operations(path)
        self.check(
            {op.operation_id for op in loaded_active}
            == {op.operation_id for op in self.center.active()},
            'persisted active set diverges',
        )

    def _op_bad_submit(self, rng: random.Random) -> None:
        choice = rng.randrange(3)
        if choice == 0 and self.order:
            resolvable = [oid for oid in self.order if self._resolvable(oid)]
            if not resolvable:
                return
            with pytest.raises(OperationTransitionError):
                self.center.submit(
                    operation_kind='prediction',
                    operation_class=OperationClass.COMPUTE,
                    title='重複',
                    operation_id=rng.choice(resolvable),
                )
        elif choice == 1:
            with pytest.raises(OperationTransitionError):
                self.center.submit(
                    operation_kind='prediction',
                    operation_class=OperationClass.COMPUTE,
                    title='コールバックなし',
                    cancellability=Cancellability.CANCELLABLE,
                )
        else:
            with pytest.raises(OperationTransitionError):
                self.center.submit(
                    operation_kind='prediction',
                    operation_class=OperationClass.COMPUTE,
                    title='不可キャンセル',
                    cancellability=Cancellability.NOT_CANCELLABLE,
                    cancel_callback=lambda: None,
                )


def _run_activity_walk(seed: int, tmp_path: Path) -> None:
    walk = _ActivityWalk(seed, tmp_path / f'act-{seed}')
    walk.run(60)


@pytest.mark.parametrize('seed', range(150))
def test_activity_state_machine_walk(seed: int, tmp_path: Path) -> None:
    _run_activity_walk(seed, tmp_path)


def test_authorities_changed_reclassifies_past_record_evictions() -> None:
    """Regression for mid-pass record eviction aborting the batch.

    With the record store past its bound, each reclassifying
    ``_transition`` -> ``_archive`` evicts the oldest terminal records —
    including not-yet-visited ones. The pass must still reach every
    operation: the evicted op's surviving history row is reclassified in
    place, and no ``OperationTransitionError`` may escape (before the
    fix, the batch silently half-applied: the call raised — swallowed by
    ``_note_superseded_inputs`` — leaving later ops stale).
    """

    center = ActivityCenter(history_limit=20, record_limit=4)
    ids = []
    for index in range(4):
        ids.append(
            center.submit(
                operation_kind='prediction',
                operation_class=OperationClass.COMPUTE,
                title=f'収容{index}',
                input_authority_refs=('auth-target',),
                operation_id=f'op-{index}',
            )
        )
    # Complete while at the bound so all four stay resident terminal.
    for operation_id in ids:
        center.mark_running(operation_id)
        center.complete(operation_id)
    assert len(center._records) == 4  # noqa: SLF001
    # Submit past the bound without any terminal transition — records
    # grow unevicted, so the next archive pass finds excess.
    for index in range(4, 12):
        ids.append(
            center.submit(
                operation_kind='prediction',
                operation_class=OperationClass.COMPUTE,
                title=f'追加{index}',
                input_authority_refs=('auth-target',),
                operation_id=f'op-{index}',
            )
        )
    # Ref-free actives stay untouched throughout.
    for index in range(12, 14):
        center.submit(
            operation_kind='prediction',
            operation_class=OperationClass.COMPUTE,
            title=f'対象外{index}',
            operation_id=f'op-{index}',
        )
    assert len(center._records) > 4  # noqa: SLF001
    resident_terminal = [
        operation_id
        for operation_id, record in center._records.items()  # noqa: SLF001
        if not record.snapshot.is_active
    ]
    assert len(resident_terminal) >= 2

    center.note_authorities_changed({'auth-target'})

    for operation_id in ids:
        snapshot = center.get(operation_id)
        assert snapshot is not None, f'{operation_id} unresolvable'
        assert not snapshot.current_for_input, f'{operation_id} kept stale input truth'
        assert snapshot.state in (
            OperationState.COMPLETED_FOR_HISTORICAL_INPUT,
            OperationState.RUNNING,
            OperationState.QUEUED,
        ), f'{operation_id} unexpected state {snapshot.state}'
        if snapshot.operation_id in {f'op-{i}' for i in range(4)}:
            assert snapshot.state == OperationState.COMPLETED_FOR_HISTORICAL_INPUT



# ---------------------------------------------------------------------------
# Surface 4 — NativeWorkerPool real-thread interleavings.
# ---------------------------------------------------------------------------


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _WorkerWalk(_Walk):
    def __init__(self, seed: int) -> None:
        super().__init__('worker', seed)
        from htdt.native_worker import NativeWorkerPool, lingering_thread_count

        self._lingering_count = lingering_thread_count
        self.pool = NativeWorkerPool(
            shutdown_timeout_ms=random.Random(seed).choice([60, 150, 900])
        )
        self.keys = [f'w-{i}' for i in range(4)]
        self.generations: dict[str, int] = {key: 0 for key in self.keys}
        self.tasks: dict[tuple[str, int], dict] = {}
        self.completions: list[tuple[str, object, object]] = []
        self.attributed = 0
        self.baseline_lingering = self._lingering_count()
        self.shut_down = False

    def ops(self) -> list[tuple[str, float]]:
        return [
            ('start', 26),
            ('cancel', 10),
            ('cancel_all', 3),
            ('pump', 12),
            ('stop_all', 4),
        ]

    def _run(self, idx: int, rng: random.Random) -> None:
        name = self.ops()[idx][0]
        getattr(self, f'_op_{name}')(rng)
        self._post_op_checks()

    # -- completion attribution -------------------------------------------

    def _attribute(self, index: int):
        """Map completions[index] to the task generation that emitted it.

        ``eligible`` encodes the ordering invariant: a completion delivered
        at list position >= released_at_n arrived after the owner released
        the task — a stale application.
        """
        key, result, error = self.completions[index]
        cands = [
            rec
            for (k, _g), rec in self.tasks.items()
            if k == key and not rec['completion_seen']
        ]

        def eligible(rec) -> bool:
            return rec['released_at_n'] is None or index < rec['released_at_n']

        for rec in cands:
            if not eligible(rec):
                continue
            gen = rec['gen']
            if error is None:
                if rec['behaviour'] == 'quick' and result == f'done-{key}-{gen}':
                    return rec
                if rec['behaviour'] in ('sleeper', 'stubborn') and result == f'late-{key}-{gen}':
                    return rec
                if rec['behaviour'] == 'sleeper' and result == 'cooperative-stop':
                    return rec
            if rec['behaviour'] == 'boom' and isinstance(error, Exception):
                if f'boom-{key}-{gen}' in str(error):
                    return rec
        if error == WORKER_CANCELLED:
            for rec in cands:
                if eligible(rec) and rec['cancelled']:
                    return rec
        return None

    def _post_op_checks(self) -> None:
        while self.attributed < len(self.completions):
            rec = self._attribute(self.attributed)
            self.check(
                rec is not None,
                f'stale/unattributable completion #{self.attributed}: '
                f'{self.completions[self.attributed]!r}',
            )
            rec['completion_seen'] = True
            self.attributed += 1
        self.check(
            len(self.pool) == len(self.pool.tasks),
            'pool __len__ disagrees with task map',
        )
        self.check(
            self.pool.active_count <= len(self.pool),
            'active_count exceeds tracked tasks',
        )
        for key in self.pool.tasks:
            self.check(key in self.keys, f'phantom key {key} tracked')
        seen = sum(1 for rec in self.tasks.values() if rec['completion_seen'])
        self.check(
            seen == len(self.completions),
            'a task delivered more than one completion',
        )

    # -- ops --------------------------------------------------------------

    def _op_start(self, rng: random.Random) -> None:
        if self.shut_down:
            with pytest.raises(RuntimeError):
                self.pool.start('post-shutdown', lambda event: None)
            return
        key = rng.choice(self.keys)
        generation = self.generations[key] + 1
        self.generations[key] = generation
        behaviour = rng.choice(['quick', 'sleeper', 'stubborn', 'boom'])
        record = {
            'gen': generation,
            'behaviour': behaviour,
            'cancelled': False,
            'released_at_n': None,
            'completion_seen': False,
        }
        sleeps = rng.uniform(0.01, 0.05)

        def operation(cancel_event: Event):
            if behaviour == 'quick':
                return f'done-{key}-{generation}'
            if behaviour == 'boom':
                time.sleep(sleeps * 0.3)
                raise RuntimeError(f'boom-{key}-{generation}')
            steps = 4 if behaviour == 'sleeper' else 14
            for _i in range(steps):
                if cancel_event.is_set() and behaviour == 'sleeper':
                    return 'cooperative-stop'
                time.sleep(sleeps)
            return f'late-{key}-{generation}'

        def on_completed(k, result, error):
            self.completions.append((k, result, error))

        replaced = key in self.pool.tasks
        old_gen = self.generations[key] - 1
        self.pool.start(key, operation, on_completed)
        self.tasks[(key, generation)] = record
        if replaced and (key, old_gen) in self.tasks:
            self.tasks[(key, old_gen)]['released_at_n'] = len(self.completions)

    def _op_cancel(self, rng: random.Random) -> None:
        key = rng.choice(self.keys)
        present = self.pool.cancel(key)
        self.check(
            present == (key in self.pool.tasks),
            'cancel() truth disagrees with the task map',
        )
        generation = self.generations[key]
        record = self.tasks.get((key, generation))
        if present and record is not None:
            record['cancelled'] = True

    def _op_cancel_all(self, rng: random.Random) -> None:
        self.pool.cancel_all()
        for (key, _g), record in self.tasks.items():
            if key in self.pool.tasks:
                record['cancelled'] = True

    def _op_pump(self, rng: random.Random) -> None:
        _pump_until(lambda: True, timeout_s=0.02)

    def _op_stop_all(self, rng: random.Random) -> None:
        if self.shut_down:
            return
        report = self.pool.stop_all(timeout_ms=60)
        tracked_keys = set(self.pool.tasks)
        self.check(not tracked_keys, 'stop_all left tracked tasks')
        for key in report.stopped_keys + report.lingering_keys:
            self.check(
                key in self.keys,
                f'stop_all reported unknown key {key}',
            )
        for (key, _g), record in self.tasks.items():
            if record['released_at_n'] is None:
                record['released_at_n'] = len(self.completions)
                record['cancelled'] = True
        # The pool stays usable — stop_all is not shutdown.
        self.check(
            not self.pool._shutdown_requested,  # noqa: SLF001
            'stop_all shut the pool down',
        )

    def finish(self) -> None:
        self.shut_down = True
        self.pool.cancel_all()
        report = self.pool.shutdown(timeout_ms=120)
        self.check(
            not self.pool.tasks,
            'tracked tasks survived shutdown',
        )
        for record in self.tasks.values():
            if record['released_at_n'] is None:
                record['released_at_n'] = len(self.completions)
            record['cancelled'] = True
        for key in report.stopped_keys + report.lingering_keys:
            self.check(key in self.keys, f'shutdown reported unknown key {key}')
        with pytest.raises(RuntimeError):
            self.pool.start('after-shutdown', lambda event: None)
        ok = _pump_until(
            lambda: self._lingering_count() == self.baseline_lingering,
            timeout_s=15.0,
        )
        if not ok:
            from htdt.native_worker import _LINGERING_THREADS  # noqa: PLC0415

            states = []
            for thread in _LINGERING_THREADS:
                try:
                    states.append(
                        (
                            thread.property('htdtWorkerKey'),
                            thread.isRunning(),
                            thread.isFinished(),
                        )
                    )
                except RuntimeError:
                    states.append(('deleted', False, True))
            self.check(
                False,
                f'detached worker threads never drained: {states}',
            )
        # Final attribution sweep.
        while self.attributed < len(self.completions):
            rec = self._attribute(self.attributed)
            self.check(
                rec is not None,
                f'late completion attributed to nothing: '
                f'{self.completions[self.attributed]!r}',
            )
            rec['completion_seen'] = True
            self.attributed += 1


def _run_worker_walk(seed: int) -> None:
    _app()
    walk = _WorkerWalk(seed)
    try:
        walk.run(56)
    finally:
        walk.finish()


@pytest.mark.parametrize('seed', range(40))
def test_worker_state_machine_walk(seed: int) -> None:
    _run_worker_walk(seed)


# ---------------------------------------------------------------------------
# Surface 5 — library list contract: totals, ordering, no phantom rows.
# ---------------------------------------------------------------------------


class _LibraryWalk(_Walk):
    def __init__(self, seed: int, root: Path) -> None:
        super().__init__('library', seed)
        root.mkdir(parents=True, exist_ok=True)
        self.repository = SceneRepository(root / 'scenes.sqlite3')
        self.library = ProjectLibraryRepository(self.repository)
        self.models: dict[str, dict] = {}
        self.order: list[str] = []

    def ops(self) -> list[tuple[str, float]]:
        return [
            ('create', 18),
            ('open', 14),
            ('rename', 8),
            ('archive', 10),
            ('unarchive', 8),
            ('duplicate', 3),
            ('read_lists', 20),
            ('bad_ops', 6),
        ]

    def _run(self, idx: int, rng: random.Random) -> None:
        name = self.ops()[idx][0]
        getattr(self, f'_op_{name}')(rng)
        self._check_lists()

    def _check_lists(self) -> None:
        library = self.library
        active = library.list_projects()
        expected_active = [
            pid for pid in self.order if not self.models[pid]['archived']
        ]
        self.check(
            len(active) == len(expected_active),
            f'active total {len(active)} != {len(expected_active)}',
        )
        self.check(
            {e.project_id for e in active} == set(expected_active),
            'active set mismatch',
        )
        all_rows = library.list_projects(include_archived=True)
        self.check(
            len(all_rows) == len(self.order),
            f'total rows {len(all_rows)} != {len(self.order)}',
        )
        doc_ids = [e.document_id for e in all_rows]
        self.check(
            len(doc_ids) == len(set(doc_ids)),
            'phantom duplicate document_id',
        )
        for entry in all_rows:
            model = self.models[entry.project_id]
            self.check(
                entry.display_name == model['name'],
                'display_name drift',
            )
            self.check(
                bool(entry.archived) == model['archived'],
                'archived flag drift',
            )
            resolved = library.get_project(entry.project_id)
            self.check(
                resolved.document_id == entry.document_id,
                'phantom row resolves differently',
            )
        opened = [e for e in active if e.last_opened_at_utc is not None]
        never = [e for e in active if e.last_opened_at_utc is None]
        self.check(
            list(active[: len(opened)]) == opened,
            'unopened entry outranks opened',
        )
        for earlier, later in zip(opened, opened[1:]):
            self.check(
                earlier.last_opened_at_utc >= later.last_opened_at_utc,
                'opened ordering violates last_opened desc',
            )
        for earlier, later in zip(never, never[1:]):
            self.check(
                earlier.created_at_utc >= later.created_at_utc,
                'unopened tail violates created desc',
            )
        limit = self.rng.randint(0, 5)
        self.check(
            library.recent_projects(limit) == active[:limit],
            'recent_projects is not a prefix of the listing',
        )
        self.check(
            library.most_recent_project() == (active[0] if active else None),
            'most_recent disagrees with listing',
        )

    def _pick(self, rng: random.Random) -> str | None:
        return rng.choice(self.order) if self.order else None

    def _op_create(self, rng: random.Random) -> None:
        entry = self.library.create_project(f'案件{rng.randrange(10000)}')
        self.models[entry.project_id] = {
            'name': entry.display_name,
            'archived': False,
        }
        self.order.append(entry.project_id)

    def _op_open(self, rng: random.Random) -> None:
        pid = self._pick(rng)
        if pid is None:
            return
        model = self.models[pid]
        if model['archived']:
            with pytest.raises(ProjectArchivedError):
                self.library.open_project(pid)
            return
        entry = self.library.open_project(pid)
        self.check(
            entry.last_opened_at_utc is not None,
            'open_project did not stamp last_opened',
        )

    def _op_rename(self, rng: random.Random) -> None:
        pid = self._pick(rng)
        if pid is None:
            return
        name = f'改名{rng.randrange(10000)}'
        self.library.rename_project(pid, name)
        self.models[pid]['name'] = name

    def _op_archive(self, rng: random.Random) -> None:
        pid = self._pick(rng)
        if pid is None:
            return
        self.library.set_archived(pid, True)
        self.models[pid]['archived'] = True

    def _op_unarchive(self, rng: random.Random) -> None:
        pid = self._pick(rng)
        if pid is None:
            return
        self.library.set_archived(pid, False)
        self.models[pid]['archived'] = False

    def _op_duplicate(self, rng: random.Random) -> None:
        pid = self._pick(rng)
        if pid is None:
            return
        clone = self.library.duplicate_project(pid, f'複製{rng.randrange(10000)}')
        self.check(
            clone.project_id != pid,
            'duplicate returned the source project',
        )
        self.check(
            clone.cloned_from_project_id == pid,
            'duplicate lost clone provenance',
        )
        self.models[clone.project_id] = {
            'name': clone.display_name,
            'archived': bool(clone.archived),
        }
        self.order.append(clone.project_id)
        self.check(
            self.models[pid]['name'] == self.library.get_project(pid).display_name,
            'duplication mutated the source project',
        )

    def _op_read_lists(self, rng: random.Random) -> None:
        pass  # _check_lists already ran for this step

    def _op_bad_ops(self, rng: random.Random) -> None:
        choice = rng.randrange(3)
        if choice == 0:
            with pytest.raises(ProjectNotFoundError):
                self.library.open_project('ghost-project')
        elif choice == 1:
            with pytest.raises(ProjectLibraryError):
                self.library.create_project('   ')
        else:
            with pytest.raises(ProjectNotFoundError):
                self.library.get_project('ghost-project')


def _run_library_walk(seed: int, tmp_path: Path) -> None:
    walk = _LibraryWalk(seed, tmp_path / f'lib-{seed}')
    walk.run(40)


@pytest.mark.parametrize('seed', range(100))
def test_library_state_machine_walk(seed: int, tmp_path: Path) -> None:
    _run_library_walk(seed, tmp_path)
