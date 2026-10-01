"""#623 embedded help & glossary tests."""

from __future__ import annotations

import pytest

from htdt.command_registry import CommandRegistry, register_default_commands
from htdt.help_registry import (
    HelpRegistry,
    LocalizedTopicContent,
    build_help_registry,
    default_command_ids,
    shortcut_reference,
    HelpTopic,
)
from htdt.localization import PresentationLocale, TermId
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def test_builtin_registry_integrity() -> None:
    registry = build_help_registry()
    report = registry.validate(command_ids=default_command_ids())
    assert report.ok, report.errors


def test_required_content_coverage() -> None:
    registry = build_help_registry()
    for topic_id in (
        'start.getting_started',
        'concept.scene_vs_revision',
        'concept.lifecycle_states',
        'concept.evidence_vs_assumption',
        'concept.staged_vs_promoted',
        'concept.stale_historical',
        'concept.project_identity',
        'concept.reusable_vs_project',
        'workflow.room_cad',
        'workflow.measurements',
        'workflow.prediction_optimize',
        'workflow.data_recovery',
        'trouble.prediction_unavailable',
        'trouble.unknown_value',
        'trouble.capture_staged',
        'trouble.revisions_incomparable',
    ):
        assert registry.get(topic_id) is not None, topic_id


def test_duplicate_topic_id_fails() -> None:
    topic = build_help_registry().require('start.getting_started')
    with pytest.raises(ValueError):
        HelpRegistry([topic, topic])


def test_broken_related_topic_detected() -> None:
    topic = HelpTopic(
        topic_id='a.b',
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(title='t', summary='s'),
            PresentationLocale.ENGLISH: LocalizedTopicContent(title='t', summary='s'),
        },
        related_topics=('does.not.exist',),
    )
    report = HelpRegistry([topic]).validate(command_ids=())
    assert not report.ok
    assert any('does.not.exist' in e for e in report.errors)


def test_broken_command_reference_detected() -> None:
    topic = HelpTopic(
        topic_id='a.b',
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(title='t', summary='s'),
            PresentationLocale.ENGLISH: LocalizedTopicContent(title='t', summary='s'),
        },
        related_commands=('not.a.command',),
    )
    report = HelpRegistry([topic]).validate(command_ids={'real.command'})
    assert not report.ok


def test_reason_code_lookup() -> None:
    registry = build_help_registry()
    topic = registry.topic_for_reason('prediction.run.receiver_required')
    assert topic is not None and topic.topic_id == 'trouble.prediction_unavailable'
    assert registry.topic_for_reason('no.such.code') is None


def test_duplicate_reason_binding_fails() -> None:
    base = build_help_registry()
    other = HelpTopic(
        topic_id='trouble.other',
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(title='t', summary='s'),
            PresentationLocale.ENGLISH: LocalizedTopicContent(title='t', summary='s'),
        },
        reason_codes=('command.blocked.data_mutation_frozen',),
    )
    with pytest.raises(ValueError):
        HelpRegistry([*(base.require(t) for t in base.topic_ids()), other])


def test_search_matches_both_languages() -> None:
    registry = build_help_registry()
    assert registry.search('speaker')
    assert registry.search('スピーカー')
    assert registry.search('REW')
    # Top hit for a room keyword is the room CAD topic.
    top = registry.search('部屋')[0]
    assert top.topic.topic_id == 'workflow.room_cad'
    assert registry.search('') == ()


def test_glossary_covers_terminology() -> None:
    registry = build_help_registry()
    terms = {t.term_id for t in registry.glossary()}
    for term_id in TermId:
        assert term_id in terms
    proposed = registry.glossary_term(TermId.PROPOSED_SYSTEM)
    assert proposed is not None
    assert proposed.label(PresentationLocale.ENGLISH) == 'Proposed'


def test_shortcut_reference_generated_from_registry() -> None:
    commands = CommandRegistry()
    register_default_commands(commands)
    docs = shortcut_reference(commands)
    ids = {d.command_id for d in docs}
    assert 'project.save' in ids
    save_doc = next(d for d in docs if d.command_id == 'project.save')
    assert save_doc.shortcut == 'Ctrl+S'


def test_topics_localized_and_deep_linked() -> None:
    registry = build_help_registry()
    topic = registry.require('trouble.prediction_unavailable')
    assert topic.localized(PresentationLocale.ENGLISH).title == 'Why prediction is unavailable'
    assert topic.localized(PresentationLocale.JAPANESE).title == '予測が利用できない理由'
    assert any(
        link.workspace == WorkspaceId.ROOM for link in topic.deep_links
    )
