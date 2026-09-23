from __future__ import annotations

import pytest

from htdt.capture_compatibility import (
    ArtifactProtocolRequirement,
    HTDT_PROTOCOL_REGISTRY,
    ProductCompatibilityIdentity,
    ProtocolSupportEntry,
    ProtocolSupportRegistry,
    describe_update_guidance,
    evaluate_compatibility,
    htdt_product_identity,
)


def _consumer(*versions: int) -> ProductCompatibilityIdentity:
    return ProductCompatibilityIdentity(
        product='HTDT-Capture',
        app_version='1.4.2',
        registry=ProtocolSupportRegistry(
            registry_version=1,
            entries=(
                ProtocolSupportEntry(
                    kind='htdt.capture.bundle',
                    supported_versions=tuple(versions),
                ),
                ProtocolSupportEntry(
                    kind='htdt.equipment.catalog-snapshot',
                    supported_versions=(1,),
                ),
            ),
        ),
    )


def test_supported_when_all_requirements_match() -> None:
    verdict = evaluate_compatibility(
        (
            ArtifactProtocolRequirement(
                kind='htdt.capture.bundle', version=1
            ),
        ),
        _consumer(1, 2),
    )
    assert verdict.status == 'supported'
    assert not verdict.consumer_update_required


def test_required_newer_version_needs_consumer_update() -> None:
    verdict = evaluate_compatibility(
        (
            ArtifactProtocolRequirement(
                kind='htdt.capture.bundle', version=3
            ),
        ),
        _consumer(1, 2),
    )
    assert verdict.status == 'update_consumer_required'
    assert verdict.consumer_update_required
    guidance = describe_update_guidance(
        verdict, _consumer(1, 2), artifact_label='Capture bundle'
    )
    assert 'Update HTDT-Capture' in guidance
    assert 'htdt.capture.bundle' in guidance


def test_required_older_version_is_historical_not_consumer_update() -> None:
    verdict = evaluate_compatibility(
        (
            ArtifactProtocolRequirement(
                kind='htdt.capture.bundle', version=1
            ),
        ),
        _consumer(2, 3),
    )
    assert verdict.status == 'unsupported_historical_format'
    assert not verdict.consumer_update_required
    guidance = describe_update_guidance(
        verdict, _consumer(2, 3), artifact_label='Capture bundle'
    )
    assert 'older format' in guidance
    assert 'Update HTDT-Capture' not in guidance


def test_optional_unsupported_degrades_instead_of_failing() -> None:
    verdict = evaluate_compatibility(
        (
            ArtifactProtocolRequirement(
                kind='htdt.capture.bundle', version=1
            ),
            ArtifactProtocolRequirement(
                kind='htdt.capture.mission-package',
                version=9,
                role='optional',
            ),
        ),
        _consumer(1),
    )
    assert verdict.status == 'supported_with_degraded_optional_features'
    assert verdict.degraded_optional_kinds == (
        'htdt.capture.mission-package',
    )
    guidance = describe_update_guidance(
        verdict, _consumer(1), artifact_label='Capture bundle'
    )
    assert 'degraded' in guidance


def test_unknown_required_kind_needs_consumer_update() -> None:
    verdict = evaluate_compatibility(
        (
            ArtifactProtocolRequirement(
                kind='htdt.capture.future-thing', version=1
            ),
        ),
        _consumer(1),
    )
    assert verdict.status == 'update_consumer_required'
    finding = verdict.findings[0]
    assert finding.state == 'unknown_protocol_kind'


def test_malformed_guidance_is_not_update_advice() -> None:
    from htdt.capture_compatibility import CompatibilityVerdict

    verdict = CompatibilityVerdict(
        status='malformed_or_identity_invalid',
        findings=(),
    )
    guidance = describe_update_guidance(
        verdict, _consumer(1), artifact_label='Return artifact'
    )
    assert 'not an update problem' in guidance


def test_htdt_registry_covers_produced_protocols() -> None:
    identity = htdt_product_identity()
    assert identity.product == 'htdt'
    registry = identity.registry
    for kind in (
        'htdt.capture.bundle',
        'htdt.capture.ingestion-plan',
        'htdt.capture.task-plan',
        'htdt.capture.mission-package',
        'htdt.equipment.catalog-snapshot',
        'htdt.field-return',
        'htdt.project-reference',
    ):
        entry = registry.entry(kind)
        assert entry is not None, kind
        assert 1 in entry.supported_versions
    assert registry.supports('htdt.field-return', 1)
    assert not registry.supports('htdt.field-return', 99)


def test_evaluate_accepts_bare_registry() -> None:
    verdict = evaluate_compatibility(
        (
            ArtifactProtocolRequirement(
                kind='htdt.capture.bundle', version=1
            ),
        ),
        HTDT_PROTOCOL_REGISTRY,
    )
    assert verdict.status == 'supported'
