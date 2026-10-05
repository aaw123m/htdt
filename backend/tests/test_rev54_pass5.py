"""REV54-PASS5 — fifth-pass findings on REV53's merged changes.

A. Relocation rollback identity (REV53-DATA, follow-on of PASS4-A): the
   journaled ``parked_dir`` is only the *planned* park name while the
   journal's phase precedes SOURCE_PARKED — a crash between the park
   rename and its journal write can leave the real generation at a
   diverted ``<parked>.<n>`` sibling while the journaled name holds an
   older parked generation. Recovery must resolve the generation by the
   database digest recorded at PREPARED, not by slot name alone.

B. Relocation rollback honesty (REV53-DATA, follow-on of PASS4-A): when
   the rollback cannot or need not run — the source slot is occupied,
   the source already holds the journaled generation, or no parked
   generation survives — the raised error must report what actually
   happened instead of claiming it 'rolled the source back'.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from test_data_relocation_cutover import (  # noqa: E402
    _journal,
    _seed_data_dir,
    _write_journal,
)

from htdt.data_relocation import (  # noqa: E402
    DataRelocationError,
    recover_interrupted_relocation,
)
from htdt.managed_assets import sha256_file  # noqa: E402
from htdt.native_backup import DATABASE_NAME  # noqa: E402


# ---------------------------------------------------------------------------
# A. rollback resolves the parked generation by digest, not slot name
# ---------------------------------------------------------------------------


def test_recovery_restores_diverted_parked_generation(tmp_path: Path) -> None:
    """Crash between the park rename and the SOURCE_PARKED journal
    write: the journal still records the *planned* park name while the
    real generation sits at ``<parked>.1`` and the planned slot holds an
    older parked generation. The rollback must restore the generation
    whose database digest matches the journal — promoting the planned
    slot would resurrect the wrong generation."""
    source = tmp_path / 'source'
    _seed_data_dir(source)  # the real pre-relocation generation
    digest = sha256_file(source / DATABASE_NAME)
    parked = tmp_path / 'source.relocated-x'
    diverted = Path(f'{parked}.1')
    shutil.move(source, diverted)
    # An older generation occupies the planned slot (e.g. a retry inside
    # the same timestamp second diverted the park to ``.1``).
    older = tmp_path / 'older'
    _seed_data_dir(older, document_id='doc-older')
    shutil.move(older, parked)
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            source,
            tmp_path / 'dest',
            tmp_path / 'gone-staged',
            parked,
            'DESTINATION_PROMOTED',
        ).model_copy(update={'source_database_sha256': digest}),
        bootstrap,
    )

    with pytest.raises(DataRelocationError, match='rolled the source back'):
        recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert sha256_file(source / DATABASE_NAME) == digest
    assert not diverted.exists()
    assert parked.is_dir()  # the older generation is left in place


# ---------------------------------------------------------------------------
# B. rollback reports what it actually did
# ---------------------------------------------------------------------------


def test_rollback_reports_occupied_source_slot(tmp_path: Path) -> None:
    """Parked generation resolved, but the source name already holds
    foreign content — recovery must refuse to overwrite and say the
    generation was left in place, not claim a rollback happened."""
    source = tmp_path / 'source'
    _seed_data_dir(source, document_id='doc-live')
    parked_seed = tmp_path / 'parked-seed'
    _seed_data_dir(parked_seed, document_id='doc-parked')
    digest = sha256_file(parked_seed / DATABASE_NAME)
    parked = tmp_path / 'source.relocated-x'
    shutil.move(parked_seed, parked)
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            source,
            tmp_path / 'dest',
            tmp_path / 'gone-staged',
            parked,
            'SOURCE_PARKED',
        ).model_copy(update={'source_database_sha256': digest}),
        bootstrap,
    )

    with pytest.raises(DataRelocationError) as excinfo:
        recover_interrupted_relocation(bootstrap_path=bootstrap)

    message = str(excinfo.value)
    assert 'rolled the source back' not in message
    assert str(parked) in message
    assert 'left in place' in message
    # Nothing moved: the foreign occupant and the parked generation
    # both survive untouched.
    assert source.is_dir() and parked.is_dir()


def test_rollback_reports_source_already_holding_generation(
    tmp_path: Path,
) -> None:
    """DESTINATION_PROMOTED with the park never run: the source dir
    still holds exactly the journaled generation — recovery should say
    it already holds the pre-relocation generation rather than claim a
    rollback ran."""
    source = tmp_path / 'source'
    _seed_data_dir(source)
    digest = sha256_file(source / DATABASE_NAME)
    parked = tmp_path / 'source.relocated-x'  # never created
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            source,
            tmp_path / 'dest',
            tmp_path / 'gone-staged',
            parked,
            'DESTINATION_PROMOTED',
        ).model_copy(update={'source_database_sha256': digest}),
        bootstrap,
    )

    with pytest.raises(
        DataRelocationError, match='already holds the pre-relocation'
    ):
        recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert sha256_file(source / DATABASE_NAME) == digest


def test_rollback_reports_unrestorable_generation(tmp_path: Path) -> None:
    """Neither the journaled park slot nor any diverted sibling holds
    the journaled source digest — recovery must report the generation
    as unrestorable instead of claiming a rollback."""
    parked = tmp_path / 'source.relocated-x'
    foreign = tmp_path / 'foreign'
    _seed_data_dir(foreign, document_id='doc-foreign')
    shutil.move(foreign, parked)  # wrong content occupies the slot
    bootstrap = tmp_path / 'boot.json'
    _write_journal(
        _journal(
            tmp_path / 'source',
            tmp_path / 'dest',
            tmp_path / 'gone-staged',
            parked,
            'DESTINATION_PROMOTED',
        ).model_copy(
            update={'source_database_sha256': 'ab' * 32}
        ),
        bootstrap,
    )

    with pytest.raises(
        DataRelocationError, match='no restorable parked generation'
    ):
        recover_interrupted_relocation(bootstrap_path=bootstrap)

    assert parked.is_dir()  # foreign content is never adopted
