"""Application Preferences authority (#591).

``workflow_settings.py`` currently hosts only the Data Management surface;
runtime/user choices otherwise live in hard-coded defaults, environment
variables or per-widget state. This module introduces the single
**ApplicationPreferences** read/write authority for user-local, non-project
truth.

Authority boundary: application preferences are **not project evidence**.
Changing a display or integration preference never dirties project
authority, never rewrites semantic hashes, and must never silently override
a domain-required solver/model binding — preference keys are limited to the
registered definitions below, each declaring its category, type, default and
restart policy.

Categories (per the issue contract): general, display & input,
integrations, compute, files/export, diagnostics. Data Management stays a
separate Settings section.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_json as _canonical_json
from .user_facing_error import operation_error_message


PREFERENCES_SCHEMA_VERSION = 1
PREFERENCES_FILENAME = 'application_preferences.json'


class PreferenceCategory(StrEnum):
    GENERAL = 'general'
    DISPLAY_INPUT = 'display_input'
    INTEGRATIONS = 'integrations'
    COMPUTE = 'compute'
    FILES_EXPORT = 'files_export'
    DIAGNOSTICS = 'diagnostics'


class PreferenceValueType(StrEnum):
    STRING = 'string'
    INTEGER = 'integer'
    NUMBER = 'number'
    BOOLEAN = 'boolean'
    ENUM = 'enum'
    PATH = 'path'


class PreferenceError(ValueError):
    pass


class UnknownPreferenceKey(PreferenceError):
    pass


class PreferenceValueError(PreferenceError):
    pass


class IncompatiblePreferencesError(PreferenceError):
    pass


class PreferenceNotificationError(PreferenceError):
    """One or more change listeners raised *after* a durable commit.

    The preference document itself is already persisted and published;
    this error is post-commit only and never implies partial durable
    state (#742).
    """

    def __init__(self, errors: Iterable[BaseException]) -> None:
        self.errors = tuple(errors)
        super().__init__(
            f'{len(self.errors)} preference listener(s) failed after commit'
        )


class PreferenceLoadState(StrEnum):
    """Typed compatibility state of the on-disk preferences file.

    Startup fallback (safe defaults) is separate from durable-write
    authority: a file this build could not fully consume must never be
    overwritten by a routine ``set()``, only by an explicit reset that first
    preserves the previous file.
    """

    OK = 'ok'
    MISSING = 'missing'
    PARTIAL_INVALID_VALUE = 'partial_invalid_value'
    CORRUPT = 'corrupt'
    INCOMPATIBLE_NEWER_SCHEMA = 'incompatible_newer_schema'


#: Load states in which a normal durable write is refused — the file on disk
#: was not fully consumed, so overwriting it would destroy data this build
#: does not understand. ``reset_persisted_file()`` is the only sanctioned
#: recovery path and preserves the old file under a bounded name first.
_WRITE_REFUSED_STATES = frozenset(
    {PreferenceLoadState.CORRUPT, PreferenceLoadState.INCOMPATIBLE_NEWER_SCHEMA}
)

PREFERENCES_RECOVERY_SUFFIX = '.recovery'

#: Sentinel for ``_file_signature`` when the file exists but cannot be
#: stat'ed — treated as "changed", so a write attempt re-reads and hits the
#: load-state refusal path instead of overwriting blind.
_STAT_FAILED = object()


@dataclass(frozen=True, slots=True)
class PreferenceDefinition:
    """Stable declaration of one application preference key.

    ``restart_required`` marks preferences the running UI cannot re-read
    safely (e.g. language). ``affects_domain_authority`` is always False —
    a preference may never silently override a domain-required binding.
    """

    key: str
    category: PreferenceCategory
    value_type: PreferenceValueType
    default: object
    restart_required: bool = False
    allowed_values: tuple[object, ...] | None = None
    min_value: float | None = None
    max_value: float | None = None
    description: str = ''

    def __post_init__(self) -> None:
        if not self.key or self.key.strip() != self.key:
            raise ValueError('preference key must be non-empty and trimmed')
        if '.' not in self.key:
            raise ValueError(f'preference key must be namespaced: {self.key!r}')
        if self.value_type == PreferenceValueType.ENUM and not self.allowed_values:
            raise ValueError(f'enum preference {self.key!r} requires allowed_values')
        self.validate(self.default)

    def validate(self, value: object) -> object:
        """Coerce+validate a stored/assigned value against the definition."""

        if self.value_type == PreferenceValueType.BOOLEAN:
            if not isinstance(value, bool):
                raise PreferenceValueError(f'{self.key}: expected boolean, got {value!r}')
            return value
        if self.value_type == PreferenceValueType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise PreferenceValueError(f'{self.key}: expected integer, got {value!r}')
        elif self.value_type == PreferenceValueType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PreferenceValueError(f'{self.key}: expected number, got {value!r}')
        else:
            if not isinstance(value, str):
                raise PreferenceValueError(f'{self.key}: expected string, got {value!r}')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not isfinite(value):
                raise PreferenceValueError(
                    f'{self.key}: value must be finite, got {value!r}'
                )
            if self.min_value is not None and value < self.min_value:
                raise PreferenceValueError(
                    f'{self.key}: {value} below minimum {self.min_value}'
                )
            if self.max_value is not None and value > self.max_value:
                raise PreferenceValueError(
                    f'{self.key}: {value} above maximum {self.max_value}'
                )
        if self.allowed_values is not None and value not in self.allowed_values:
            raise PreferenceValueError(
                f'{self.key}: {value!r} not in {self.allowed_values!r}'
            )
        return value


# ---------------------------------------------------------------------------
# Registered preference keys. This is the only place new application
# preferences may be declared; per-feature ad-hoc persistence is a bug.


PREFERENCE_DEFINITIONS: dict[str, PreferenceDefinition] = {
    definition.key: definition
    for definition in (
        # General
        PreferenceDefinition(
            key='general.language',
            category=PreferenceCategory.GENERAL,
            value_type=PreferenceValueType.ENUM,
            default='system_default',
            allowed_values=('system_default', 'ja', 'en'),
            restart_required=True,
            description='Presentation language (#624).',
        ),
        PreferenceDefinition(
            key='general.startup_destination',
            category=PreferenceCategory.GENERAL,
            value_type=PreferenceValueType.ENUM,
            default='overview',
            allowed_values=('overview', 'reopen_last_project'),
            description='Where the shell lands at launch.',
        ),
        PreferenceDefinition(
            key='general.reopen_last_project',
            category=PreferenceCategory.GENERAL,
            value_type=PreferenceValueType.BOOLEAN,
            default=True,
            description='Offer/reopen the most recent project at startup.',
        ),
        # Display & input (#496 owns engineering units; these are display policy)
        PreferenceDefinition(
            key='display_input.length_unit',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.ENUM,
            default='mm',
            allowed_values=('mm', 'cm', 'm', 'inch'),
            description='Presentation unit for lengths; canonical SI storage unchanged.',
        ),
        PreferenceDefinition(
            key='display_input.numeric_precision',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.INTEGER,
            default=2,
            min_value=0,
            max_value=6,
            description='Digits after the decimal point in presentation.',
        ),
        PreferenceDefinition(
            key='display_input.angle_unit',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.ENUM,
            default='degree',
            allowed_values=('degree',),
            description='Angle presentation unit (degrees only for now).',
        ),
        PreferenceDefinition(
            key='display_input.theme',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.ENUM,
            default='system',
            allowed_values=('system', 'light', 'dark'),
            description='Appearance policy.',
        ),
        PreferenceDefinition(
            key='display_input.reduced_motion',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.BOOLEAN,
            default=False,
            description='Reduced-motion presentation when the OS setting is insufficient.',
        ),
        PreferenceDefinition(
            key='display_input.high_contrast',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.BOOLEAN,
            default=False,
            description='High-contrast presentation when the OS setting is insufficient.',
        ),
        PreferenceDefinition(
            key='display_input.reflection_guidance_overlay',
            category=PreferenceCategory.DISPLAY_INPUT,
            value_type=PreferenceValueType.ENUM,
            default='auto',
            allowed_values=('off', 'auto', 'on'),
            description=(
                'Reflection-guidance viewport overlay policy (#876/REV40): '
                "'off' never draws guidance markers, 'auto' draws them only "
                "in the acoustics context, 'on' draws them in every context."
            ),
        ),
        # Integrations
        PreferenceDefinition(
            key='integrations.rew_host',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.ENUM,
            default='127.0.0.1',
            # The REW API consumer is loopback-only (validate_rew_api_url),
            # so no other host is a usable value.
            allowed_values=('127.0.0.1', 'localhost'),
            description='REW API host.',
        ),
        PreferenceDefinition(
            key='integrations.rew_port',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.INTEGER,
            default=4735,
            # validate_rew_api_url accepts 1024-65535; a wider editor would
            # persist an endpoint the client then refuses.
            min_value=1024,
            max_value=65535,
            description='REW API port.',
        ),
        PreferenceDefinition(
            key='integrations.rew_auto_launch',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.BOOLEAN,
            default=True,
            description='Offer one-click REW launch from the measurement page.',
        ),
        PreferenceDefinition(
            key='integrations.rew_auto_ingest',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.BOOLEAN,
            default=True,
            description='Auto-stage new REW measurements into the import queue.',
        ),
        PreferenceDefinition(
            key='integrations.rew_watch_dir',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.PATH,
            default='',
            description='Optional folder watched for dropped REW text exports.',
        ),
        PreferenceDefinition(
            key='integrations.rew_install_path',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.PATH,
            default='',
            description='Optional explicit REW executable/app path.',
        ),
        PreferenceDefinition(
            key='integrations.capture_receiver_enabled',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.BOOLEAN,
            default=False,
            description='Enable the native Capture receiver (#593).',
        ),
        # Compute — user policy only; never overrides domain-required bindings
        PreferenceDefinition(
            key='compute.preferred_backend',
            category=PreferenceCategory.COMPUTE,
            value_type=PreferenceValueType.ENUM,
            default='auto',
            allowed_values=('auto', 'cpu', 'gpu_when_validated'),
            description='Execution backend policy among validated choices.',
        ),
        PreferenceDefinition(
            key='compute.max_concurrency',
            category=PreferenceCategory.COMPUTE,
            value_type=PreferenceValueType.INTEGER,
            default=2,
            min_value=1,
            max_value=64,
            description='Local concurrency budget for long-running jobs.',
        ),
        PreferenceDefinition(
            key='compute.scratch_dir',
            category=PreferenceCategory.COMPUTE,
            value_type=PreferenceValueType.PATH,
            default='',
            description='Scratch/cache root; empty means the data directory default.',
        ),
        PreferenceDefinition(
            key='compute.storage_ceiling_mb',
            category=PreferenceCategory.COMPUTE,
            value_type=PreferenceValueType.INTEGER,
            default=4096,
            min_value=0,
            description='Local scratch/cache storage ceiling in MiB (0 = unmanaged).',
        ),
        # Files / export — presentation defaults only; never semantic authority
        PreferenceDefinition(
            key='files.export_dir',
            category=PreferenceCategory.FILES_EXPORT,
            value_type=PreferenceValueType.PATH,
            default='',
            description='Default export/report folder; empty = system documents folder.',
        ),
        PreferenceDefinition(
            key='files.portable_bundle_include_libraries',
            category=PreferenceCategory.FILES_EXPORT,
            value_type=PreferenceValueType.BOOLEAN,
            default=True,
            description='Embed reusable library definitions in portable bundles (#488).',
        ),
        # Diagnostics (#604)
        PreferenceDefinition(
            key='diagnostics.include_project_ids',
            category=PreferenceCategory.DIAGNOSTICS,
            value_type=PreferenceValueType.BOOLEAN,
            default=False,
            description='Include project identifiers/hashes in diagnostic packages.',
        ),
    )
}


#: Keys whose editor persists a value but no production consumer reads yet
#: (round-6 feature-gap audit). The preferences UI renders them disabled
#: and marked '準備中' rather than letting a dead control pretend to work.
#: Wire the consumer, then remove the key here.
PENDING_PREFERENCE_KEYS: frozenset[str] = frozenset(
    {
        'general.startup_destination',
        'general.reopen_last_project',
        'display_input.angle_unit',
        'display_input.theme',
        'display_input.reduced_motion',
        'display_input.high_contrast',
        'compute.preferred_backend',
        'compute.max_concurrency',
        'compute.scratch_dir',
        'compute.storage_ceiling_mb',
        'files.portable_bundle_include_libraries',
    }
)


class ApplicationPreferences(BaseModel):
    """Immutable snapshot of application-local preference values.

    Presentation/resource state only — never project evidence, never
    serialized into semantic authority hashes.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = PREFERENCES_SCHEMA_VERSION
    values: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_values(self) -> 'ApplicationPreferences':
        merged: dict[str, object] = {}
        for key, definition in PREFERENCE_DEFINITIONS.items():
            raw = self.values.get(key, definition.default)
            merged[key] = definition.validate(raw)
        unknown = sorted(set(self.values) - set(PREFERENCE_DEFINITIONS))
        if unknown:
            raise PreferenceValueError(f'unknown preference keys: {unknown}')
        object.__setattr__(self, 'values', merged)
        return self

    def fingerprint(self) -> str:
        return sha256(_canonical_json(self.values).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class PreferenceChange:
    key: str
    old: object
    new: object


class ApplicationPreferenceStore:
    """Durable app-local preference store.

    JSON file under the application data directory, written atomically
    (tmp + fsync + replace). A corrupt or incompatible file degrades to
    defaults plus a diagnostic flag — preferences must never block startup.
    The store is deliberately *not* the project database: preferences are
    app-local presentation/policy state, not project evidence.
    """

    def __init__(
        self,
        path: Path,
        *,
        definitions: Mapping[str, PreferenceDefinition] | None = None,
    ) -> None:
        self.path = Path(path)
        self._definitions = dict(definitions or PREFERENCE_DEFINITIONS)
        self._values: dict[str, object] = {}
        self._opaque_values: dict[str, object] = {}
        self._load_error: str | None = None
        self._load_state = PreferenceLoadState.MISSING
        self._persisted_signature: object = None
        self._listeners: list[Callable[[PreferenceChange], None]] = []
        self._load()

    # -- persistence ----------------------------------------------------

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> 'ApplicationPreferenceStore':
        return cls(Path(data_dir) / PREFERENCES_FILENAME)

    def _load(self) -> None:
        try:
            self._load_current()
        finally:
            self._persisted_signature = self._file_signature()

    def _file_signature(self) -> object:
        """Identity of the on-disk document this store last consumed.

        Compared before every durable write so an external editor (manual
        edit, sync tool, another process) is detected: the write merges
        onto the freshly re-read file instead of silently overwriting the
        interleaved change.
        """

        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return _STAT_FAILED
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            stat.st_size,
        )

    def _refresh_if_modified(self) -> None:
        """Re-read the file when another writer touched it since last load.

        Called at the top of every write entry point. A file that became
        corrupt or newer-schema externally refreshes into the refused
        state (writes stay blocked honestly); a deleted file refreshes to
        MISSING (the next write recreates it).
        """

        if self._file_signature() != self._persisted_signature:
            self._load()

    def _load_current(self) -> None:
        self._values = {}
        self._opaque_values = {}
        self._load_error = None
        if not self.path.exists():
            self._load_state = PreferenceLoadState.MISSING
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError) as exc:
            self._load_state = PreferenceLoadState.CORRUPT
            self._load_error = f'設定ファイルを読み込めません · {operation_error_message(exc)}'
            return
        if not isinstance(payload, dict) or not isinstance(
            payload.get('values'), dict
        ):
            self._load_state = PreferenceLoadState.CORRUPT
            self._load_error = '設定ファイルがHTDTの形式ではありません'
            return
        version = payload.get('schema_version')
        if version != PREFERENCES_SCHEMA_VERSION:
            if isinstance(version, int) and not isinstance(version, bool) and (
                version > PREFERENCES_SCHEMA_VERSION
            ):
                # A newer build wrote this file; an older build must not
                # modify an on-disk format it does not understand.
                self._load_state = PreferenceLoadState.INCOMPATIBLE_NEWER_SCHEMA
                self._load_error = (
                    '設定ファイルはより新しいバージョンで書き込まれています '
                    f'(schema_version {version}) · デフォルト値で起動します'
                )
            else:
                self._load_state = PreferenceLoadState.CORRUPT
                self._load_error = (
                    f'未対応の schema_version です: {version!r}'
                )
            return
        values: dict[str, object] = {}
        opaque: dict[str, object] = {}
        invalid_keys: list[str] = []
        unserializable_keys: list[str] = []
        for key, value in payload['values'].items():
            definition = self._definitions.get(key)
            if definition is None:
                # Unknown persisted keys are never applied, but they are kept
                # verbatim so a later durable write round-trips a future
                # build's settings instead of silently destroying them. A
                # value the strict canonical writer cannot emit (e.g. NaN —
                # json.loads admits it) would poison every later _persist,
                # so it is dropped and reported like an invalid value.
                try:
                    _canonical_json(value)
                except ValueError:
                    unserializable_keys.append(key)
                else:
                    opaque[key] = value
                continue
            try:
                values[key] = definition.validate(value)
            except PreferenceValueError:
                invalid_keys.append(key)
        self._values = values
        self._opaque_values = opaque
        self._load_state = (
            PreferenceLoadState.PARTIAL_INVALID_VALUE
            if invalid_keys or unserializable_keys
            else PreferenceLoadState.OK
        )
        problems: list[str] = []
        if invalid_keys:
            problems.append(
                f'不正な値が保存されています {sorted(invalid_keys)!r} · '
                'デフォルトに戻しました'
            )
        if unserializable_keys:
            problems.append(
                '保存できない未知のキーを破棄しました '
                f'{sorted(unserializable_keys)!r}'
            )
        if problems:
            self._load_error = '; '.join(problems)

    def _check_writable(self) -> None:
        if self._load_state in _WRITE_REFUSED_STATES:
            raise IncompatiblePreferencesError(
                f'refusing to overwrite preferences file in '
                f'{self._load_state.value} state; use reset_persisted_file() '
                'to preserve the old file and write this build\'s schema'
            )

    def _persist(self) -> None:
        self._check_writable()
        payload = {
            'schema_version': PREFERENCES_SCHEMA_VERSION,
            'authority': 'htdt-application-preferences',
            'values': {**self._opaque_values, **self._values},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + '.', suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(_canonical_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            self._persisted_signature = self._file_signature()
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- read ------------------------------------------------------------

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def load_state(self) -> PreferenceLoadState:
        """Typed compatibility state for Settings/Diagnostics presentation."""
        return self._load_state

    @property
    def write_allowed(self) -> bool:
        """Whether normal set/reset/update writes may touch the file."""
        return self._load_state not in _WRITE_REFUSED_STATES

    def definition(self, key: str) -> PreferenceDefinition:
        definition = self._definitions.get(key)
        if definition is None:
            raise UnknownPreferenceKey(f'unknown preference key: {key!r}')
        return definition

    def definitions(
        self, category: PreferenceCategory | None = None
    ) -> tuple[PreferenceDefinition, ...]:
        items = self._definitions.values()
        if category is not None:
            items = [d for d in items if d.category == category]
        return tuple(items)

    def get(self, key: str) -> object:
        definition = self.definition(key)
        return self._values.get(key, definition.default)

    def is_default(self, key: str) -> bool:
        self.definition(key)
        return key not in self._values

    def snapshot(self) -> ApplicationPreferences:
        return ApplicationPreferences(values=dict(self._values))

    def content_fingerprint(self) -> str:
        return self.snapshot().fingerprint()

    def rew_api_base_url(self) -> str:
        """The shared REW endpoint — replaces the hard-coded 127.0.0.1:4735."""

        return f'http://{self.get("integrations.rew_host")}:{self.get("integrations.rew_port")}'

    # -- write -----------------------------------------------------------

    def subscribe(self, listener: Callable[[PreferenceChange], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[PreferenceChange], None]) -> None:
        """Detach a previously subscribed listener (idempotent).

        The store is app-scoped and outlives per-composition subscribers:
        a listener whose owner closed must be removed, or every later
        commit keeps invoking a dead observer — and a listener that raises
        surfaces as ``PreferenceNotificationError`` on an unrelated write.
        """

        self._listeners = [
            existing for existing in self._listeners if existing != listener
        ]

    def _commit(
        self, resolved: Mapping[str, object]
    ) -> tuple[PreferenceChange, ...]:
        """Atomically commit one batch of already-validated values.

        The complete candidate document is written once through the
        tmp + fsync + ``os.replace`` path; live ``_values`` are published
        only after the durable replacement succeeds, and listeners fire
        only after that (#742). A failed commit leaves memory and disk at
        the prior state and emits no notifications.
        """

        candidate = dict(self._values)
        changes: list[PreferenceChange] = []
        for key, validated in resolved.items():
            definition = self.definition(key)
            old = self._values.get(key, definition.default)
            if validated == definition.default:
                candidate.pop(key, None)
                new = definition.default
            else:
                candidate[key] = validated
                new = validated
            if new != old:
                changes.append(PreferenceChange(key=key, old=old, new=new))
        if candidate != self._values:
            self._persist_values(candidate)
            self._values = candidate
        errors: list[BaseException] = []
        for change in changes:
            for listener in tuple(self._listeners):
                try:
                    listener(change)
                except Exception as exc:
                    # The commit is already durable — a broken observer
                    # cannot create a partial preference document.
                    errors.append(exc)
        if errors:
            raise PreferenceNotificationError(errors)
        return tuple(changes)

    def set(self, key: str, value: object) -> PreferenceChange:
        """Validate and persist one preference.

        Unknown keys fail closed: only registered definitions may be stored,
        which keeps features from inventing parallel persistence.
        """

        self._refresh_if_modified()
        self._check_writable()
        definition = self.definition(key)
        validated = definition.validate(value)
        old = self.get(key)
        changes = self._commit({key: validated})
        if changes:
            return changes[0]
        return PreferenceChange(key=key, old=old, new=validated)

    def reset(self, key: str) -> PreferenceChange:
        self._refresh_if_modified()
        self._check_writable()
        definition = self.definition(key)
        old = self.get(key)
        changes = self._commit({key: definition.default})
        if changes:
            return changes[0]
        return PreferenceChange(key=key, old=old, new=old)

    def update(self, values: Mapping[str, object]) -> tuple[PreferenceChange, ...]:
        """Apply a batch as one durable preference transaction.

        Every key validates before anything mutates; one candidate
        document is persisted atomically; listeners fire only after the
        commit. A partially-applied batch is impossible (#742).
        """

        self._refresh_if_modified()
        self._check_writable()
        resolved = {
            k: self.definition(k).validate(v) for k, v in values.items()
        }
        return self._commit(resolved)

    def _persist_values(self, values: dict[str, object]) -> None:
        old_values = self._values
        self._values = values
        try:
            self._persist()
        except BaseException:
            self._values = old_values
            raise

    def reset_persisted_file(self) -> Path | None:
        """Explicit destructive reset: replace the file with fresh defaults.

        The previous file — corrupt or written by a newer build — is first
        preserved under a bounded ``<name>.recovery`` sibling name so the
        destructive step never silently discards the old document. Returns
        the recovery path, or ``None`` when no file existed.
        """

        recovery: Path | None = None
        if self.path.exists():
            recovery = self.path.with_name(self.path.name + PREFERENCES_RECOVERY_SUFFIX)
            os.replace(self.path, recovery)
        self._values = {}
        self._opaque_values = {}
        self._load_state = PreferenceLoadState.MISSING
        self._load_error = None
        self._persist()
        return recovery


__all__ = [
    'ApplicationPreferenceStore',
    'ApplicationPreferences',
    'IncompatiblePreferencesError',
    'PENDING_PREFERENCE_KEYS',
    'PREFERENCES_FILENAME',
    'PREFERENCES_RECOVERY_SUFFIX',
    'PREFERENCES_SCHEMA_VERSION',
    'PREFERENCE_DEFINITIONS',
    'PreferenceCategory',
    'PreferenceChange',
    'PreferenceDefinition',
    'PreferenceError',
    'PreferenceLoadState',
    'PreferenceNotificationError',
    'PreferenceValueError',
    'PreferenceValueType',
    'UnknownPreferenceKey',
]
