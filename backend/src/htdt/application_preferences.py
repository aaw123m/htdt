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
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        # Integrations
        PreferenceDefinition(
            key='integrations.rew_host',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.STRING,
            default='127.0.0.1',
            description='REW API host.',
        ),
        PreferenceDefinition(
            key='integrations.rew_port',
            category=PreferenceCategory.INTEGRATIONS,
            value_type=PreferenceValueType.INTEGER,
            default=4735,
            min_value=1,
            max_value=65535,
            description='REW API port.',
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


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


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
        self._load_error: str | None = None
        self._listeners: list[Callable[[PreferenceChange], None]] = []
        self._load()

    # -- persistence ----------------------------------------------------

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> 'ApplicationPreferenceStore':
        return cls(Path(data_dir) / PREFERENCES_FILENAME)

    def _load(self) -> None:
        self._values = {}
        self._load_error = None
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            self._load_error = f'unreadable preferences file: {exc}'
            return
        if not isinstance(payload, dict) or not isinstance(
            payload.get('values'), dict
        ):
            self._load_error = 'preferences file is not an HTDT preferences payload'
            return
        version = payload.get('schema_version')
        if version != PREFERENCES_SCHEMA_VERSION:
            self._load_error = f'unsupported preferences schema_version: {version!r}'
            return
        values: dict[str, object] = {}
        for key, value in payload['values'].items():
            definition = self._definitions.get(key)
            if definition is None:
                # Unknown persisted keys are dropped, not trusted — a stale or
                # foreign value must not silently become live configuration.
                continue
            try:
                values[key] = definition.validate(value)
            except PreferenceValueError:
                self._load_error = f'invalid stored value for {key!r}; reset to default'
        self._values = values

    def _persist(self) -> None:
        payload = {
            'schema_version': PREFERENCES_SCHEMA_VERSION,
            'authority': 'htdt-application-preferences',
            'values': self._values,
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

    def set(self, key: str, value: object) -> PreferenceChange:
        """Validate and persist one preference.

        Unknown keys fail closed: only registered definitions may be stored,
        which keeps features from inventing parallel persistence.
        """

        definition = self.definition(key)
        validated = definition.validate(value)
        old = self.get(key)
        if validated == old and key in self._values:
            return PreferenceChange(key=key, old=old, new=validated)
        if validated == definition.default:
            self._values.pop(key, None)
        else:
            self._values[key] = validated
        self._persist()
        change = PreferenceChange(key=key, old=old, new=validated)
        for listener in tuple(self._listeners):
            listener(change)
        return change

    def reset(self, key: str) -> PreferenceChange:
        self.definition(key)
        old = self.get(key)
        self._values.pop(key, None)
        self._persist()
        change = PreferenceChange(key=key, old=old, new=self.get(key))
        for listener in tuple(self._listeners):
            listener(change)
        return change

    def update(self, values: Mapping[str, object]) -> tuple[PreferenceChange, ...]:
        # Validate everything first — a partially-applied batch is a bug.
        validated = {k: self.definition(k).validate(v) for k, v in values.items()}
        return tuple(self.set(key, value) for key, value in validated.items())


__all__ = [
    'ApplicationPreferenceStore',
    'ApplicationPreferences',
    'PREFERENCES_FILENAME',
    'PREFERENCES_SCHEMA_VERSION',
    'PREFERENCE_DEFINITIONS',
    'PreferenceCategory',
    'PreferenceChange',
    'PreferenceDefinition',
    'PreferenceError',
    'PreferenceValueError',
    'PreferenceValueType',
    'UnknownPreferenceKey',
]
