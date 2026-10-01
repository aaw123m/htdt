"""#776 — stable availability reason codes wired to localization and help."""

from __future__ import annotations

import pytest

from htdt.availability_reasons import (
    AVAILABILITY_REASONS,
    AvailabilityReason,
    availability_reason,
    localized_reason_message,
    reason_help_topic_id,
)
from htdt.command_registry import (
    DATA_MUTATIONS_FROZEN_REASON,
    CommandAvailability,
    CommandDefinition,
    CommandRegistry,
)
from htdt.help_registry import build_help_registry, default_command_ids
from htdt.localization import PresentationLocale
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def test_disabled_availability_still_requires_a_reason() -> None:
    # Identity or presentation must exist — a bare "disabled" is rejected.
    with pytest.raises(ValueError):
        CommandAvailability(enabled=False)
    with pytest.raises(ValueError):
        CommandAvailability(enabled=True, disabled_reason='x')


def test_unknown_catalog_code_fails_closed() -> None:
    with pytest.raises(KeyError):
        availability_reason('command.blocked.does_not_exist')


def test_same_code_renders_differently_per_locale() -> None:
    reason = availability_reason('command.blocked.data_mutation_frozen')
    ja = localized_reason_message(reason, PresentationLocale.JAPANESE)
    en = localized_reason_message(reason, PresentationLocale.ENGLISH)
    assert ja == DATA_MUTATIONS_FROZEN_REASON
    assert en != ja
    assert 'data operation' in en


def test_availability_exposes_stable_reason_code() -> None:
    reason = availability_reason('command.blocked.selection_required')
    availability = CommandAvailability.blocked(reason)
    assert availability.reason is not None
    assert availability.reason.code == 'command.blocked.selection_required'
    # Legacy prose consumers keep working — JA catalog text is the fallback.
    assert availability.disabled_reason == '項目を選択してください'
    assert availability.localized_message(PresentationLocale.ENGLISH) != (
        availability.disabled_reason
    )


def test_parameterized_reason_keeps_identity_stable() -> None:
    for actual in (1, 2):
        reason = availability_reason(
            'command.blocked.min_selection',
            params={'required': 3, 'actual': actual},
        )
        assert reason.code == 'command.blocked.min_selection'
        assert '3' in localized_reason_message(
            reason, PresentationLocale.JAPANESE
        )


def test_unknown_code_renders_fallback_without_crashing() -> None:
    reason = AvailabilityReason(code='vendor.legacy_reason', detail='detail')
    assert localized_reason_message(reason, PresentationLocale.ENGLISH) == 'detail'
    bare = AvailabilityReason(code='vendor.legacy_reason')
    assert localized_reason_message(bare, PresentationLocale.JAPANESE) == (
        'vendor.legacy_reason'
    )
    assert reason_help_topic_id(reason) is None


def test_registry_freeze_emits_catalog_reason() -> None:
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(
            command_id='project.save',
            display_name='保存',
        ),
        execute=lambda: None,
    )
    registry.freeze_data_mutations()
    availability = registry.availability('project.save')
    assert availability.reason is not None
    assert availability.reason.code == 'command.blocked.data_mutation_frozen'


def test_palette_and_direct_lookup_share_the_same_reason() -> None:
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(
            command_id='project.save',
            display_name='保存',
        ),
        execute=lambda: None,
    )
    registry.freeze_data_mutations()
    palette_reason = registry.search('保存')[0].availability.reason
    direct_reason = registry.availability('project.save').reason
    assert palette_reason is not None and direct_reason is not None
    assert palette_reason == direct_reason


def test_contextual_help_resolves_catalog_codes() -> None:
    registry = build_help_registry()
    topic = registry.topic_for_reason('command.blocked.data_mutation_frozen')
    assert topic is not None
    assert topic.topic_id == 'trouble.command_unavailable'
    # Prediction-run codes bind to the existing prediction topic.
    pred = registry.topic_for_reason('prediction.run.running')
    assert pred is not None
    assert pred.topic_id == 'trouble.prediction_unavailable'


def test_help_integrity_rejects_unknown_catalog_owned_bindings() -> None:
    from htdt.help_registry import HelpTopic, LocalizedTopicContent, HelpRegistry

    bad_topic = HelpTopic(
        topic_id='trouble.fake',
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(
                title='t', summary='s'
            ),
            PresentationLocale.ENGLISH: LocalizedTopicContent(
                title='t', summary='s'
            ),
        },
        reason_codes=('command.blocked.typo_code',),
    )
    report = HelpRegistry((bad_topic,)).validate(
        command_ids=default_command_ids(),
        command_reason_codes=AVAILABILITY_REASONS.keys(),
    )
    assert not report.ok
    assert 'command.blocked.typo_code' in report.errors[0]


def test_shipped_registry_passes_reason_code_integrity() -> None:
    registry = build_help_registry()
    report = registry.validate(
        command_ids=default_command_ids(),
        command_reason_codes=AVAILABILITY_REASONS.keys(),
    )
    assert report.ok, report.errors


def test_every_catalog_code_has_ja_and_en_text() -> None:
    for code, spec in AVAILABILITY_REASONS.items():
        assert spec.message_ja and spec.message_en, code


def test_registry_unbound_context_reason_is_cataloged() -> None:
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(
            command_id='project.save',
            display_name='保存',
        )
    )
    availability = registry.availability('project.save')
    assert availability.reason is not None
    assert availability.reason.code == 'command.blocked.unavailable_in_context'

    no_handler = CommandRegistry()
    no_handler.register(
        CommandDefinition(
            command_id='navigation.overview',
            display_name='概要',
            deep_link=WorkspaceDeepLink(WorkspaceId.OVERVIEW),
        )
    )
    availability = no_handler.availability('navigation.overview')
    assert availability.reason is not None
    assert availability.reason.code == (
        'command.blocked.navigation_handler_unavailable'
    )


def test_every_catalog_reason_code_resolves_to_a_help_topic() -> None:
    """Palette 'why' affordance: every emitted catalog code binds a topic.

    REV25-DOCSHELP — a disabled palette row must never dead-end on the
    reason text alone, and the two resolution paths (catalog
    ``help_topic_id`` and the registry's ``topic_for_reason`` index)
    must agree on the same topic.
    """
    registry = build_help_registry()
    for code, spec in AVAILABILITY_REASONS.items():
        assert spec.help_topic_id is not None, code
        topic = registry.require(spec.help_topic_id)
        via_index = registry.topic_for_reason(code)
        assert via_index is topic, code


def test_emitted_domain_reason_codes_resolve_to_topics() -> None:
    """Domain reason vocabularies outside the catalog (overview readiness
    notices, dependency-impact notices, activity-center statuses) resolve
    through ``topic_for_reason`` too."""
    from htdt.overview_readiness import _NOTICE_AREA

    registry = build_help_registry()
    emitted_codes = (
        *_NOTICE_AREA,
        'impact.stale',
        'impact.uncertain',
        'result_stale',
        'completed_for_historical_input',
    )
    for code in emitted_codes:
        assert registry.topic_for_reason(code) is not None, code
