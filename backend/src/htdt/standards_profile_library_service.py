"""Standards-profile library service — Qt-free (#807 boundary refactor).

Profile construction/persistence on the standards authorities; the Qt editor
dialog stays in ``standards_profile_editor`` and imports this module.
"""

from __future__ import annotations



from typing import (
    Sequence,
)
from uuid import (
    uuid4,
)
from .cad_standards import (
    build_user_standards_profile,
    CriterionDefinition,
    StandardsProfile,
)


def _criterion_row_text(criterion: CriterionDefinition) -> str:
    rule = criterion.rule
    if rule.operator == "min":
        rule_text = f"≥ {rule.minimum}"
    elif rule.operator == "max":
        rule_text = f"≤ {rule.maximum}"
    elif rule.operator == "range":
        rule_text = f"{rule.minimum} .. {rule.maximum}"
    else:
        rule_text = f"= {rule.expected}"
    return (
        f"{criterion.quantity} [{criterion.unit}] {rule_text} / "
        f"{criterion.source.publisher}: {criterion.source.document_title} "
        f"{criterion.source.document_version} "
        f"({criterion.source.reference})"
    )


class StandardsProfileLibraryService:
    """Create/version/import/export user-defined StandardsProfiles."""

    def __init__(self, repository) -> None:
        self.repository = repository

    def profiles(self) -> tuple[StandardsProfile, ...]:
        return self.repository.list_profiles()

    def profile_versions(self, profile_id: str) -> tuple[str, ...]:
        return tuple(
            profile.version
            for profile in self.profiles()
            if profile.profile_id == profile_id
        )

    def next_version_label(self, base: StandardsProfile) -> str:
        versions = self.profile_versions(base.profile_id)
        return f"{base.version}-u{len(versions) + 1}"

    def create_user_profile(
        self,
        *,
        name: str,
        criteria: Sequence[CriterionDefinition],
    ) -> StandardsProfile:
        profile = build_user_standards_profile(
            profile_id=f"user-{uuid4().hex[:16]}",
            version="1",
            name=name,
            criteria=criteria,
        )
        return self.repository.save_profile(profile)

    def clone_profile(
        self,
        base: StandardsProfile,
        *,
        name: str,
        criteria: Sequence[CriterionDefinition],
    ) -> StandardsProfile:
        """Clone any profile (built-in included) into a new user identity.

        The source authority is never mutated — the clone carries a fresh
        ``profile_id`` with kind ``user_defined``.
        """
        profile = build_user_standards_profile(
            profile_id=f"user-{uuid4().hex[:16]}",
            version="1",
            name=name,
            criteria=criteria,
        )
        return self.repository.save_profile(profile)

    def save_new_version(
        self,
        base: StandardsProfile,
        *,
        name: str,
        criteria: Sequence[CriterionDefinition],
    ) -> StandardsProfile:
        """Publish a new immutable version of an existing user profile."""
        if base.profile_kind != "user_defined":
            raise ValueError(
                "組み込みプロファイルは変更できません。"
                "複製してユーザー定義プロファイルとして保存してください。"
            )
        profile = build_user_standards_profile(
            profile_id=base.profile_id,
            version=self.next_version_label(base),
            name=name,
            criteria=criteria,
        )
        return self.repository.save_profile(profile)

    def export_profile_json(self, profile: StandardsProfile) -> str:
        return profile.model_dump_json(indent=2)

    def import_profile_json(self, text: str) -> StandardsProfile:
        """Import exact profile JSON; malformed/unknown fields are rejected."""
        profile = StandardsProfile.model_validate_json(text)
        return self.repository.save_profile(profile)
