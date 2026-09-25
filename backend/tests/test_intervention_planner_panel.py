from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.intervention_planner_panel import InterventionPlannerPanel
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@dataclass
class _FakeBaseline:
    revision_id: str = "rev-1"

    @property
    def scene_revision(self):
        return SimpleNamespace(revision_id=self.revision_id)


@dataclass
class _FakeContext:
    baseline: _FakeBaseline | None = None
    dsp_enabled: bool = False

    def resolve_baseline(self):
        return self.baseline

    def dsp_variable_options(self, _baseline):
        return (SimpleNamespace(enabled=self.dsp_enabled),)


@dataclass
class _FakePlanner:
    scene_repository: SceneRepository
    document_id: str = "doc-1"
    context: _FakeContext = field(default_factory=_FakeContext)
    studies: list = field(default_factory=list)
    alternatives: dict = field(default_factory=dict)
    created_kwargs: dict | None = None

    def create_study(self, **kwargs):
        self.created_kwargs = kwargs
        spec = SimpleNamespace(
            spec_id="intervention-study-abc",
            created_at_utc="2026-09-25T00:00:00Z",
            scene_revision_id="rev-1",
            finding=kwargs["finding"],
            allowed_families=tuple(kwargs["allowed_families"]),
        )
        self.studies.append(spec)
        return spec

    def list_studies(self):
        return list(self.studies)

    def list_alternatives(self, spec_id):
        return list(self.alternatives.get(spec_id, []))


@dataclass
class _FakeService:
    applied_ids: list = field(default_factory=list)
    preview_stale: bool = False

    def apply_preview(self, variant_id):
        return SimpleNamespace(
            stale=self.preview_stale,
            stale_reason="stale" if self.preview_stale else None,
            name="variant-name",
            change_lines=("置換: sp-1",),
        )

    def apply(self, variant_id):
        self.applied_ids.append(variant_id)
        return SimpleNamespace(applied_revision_id="rev-new")


def _fake_ref(kind: str, authority_id: str = "auth-1"):
    return SimpleNamespace(
        authority_kind=kind,
        authority_id=authority_id,
        authority_sha256="0" * 64,
    )


def _fake_alternative(
    *,
    generated=(),
    metrics=(),
    regressions=(),
    comparable=True,
    coverage="complete",
    family="topology",
):
    return SimpleNamespace(
        alternative_id="intervention-alt-xyz",
        family=family,
        semantic_diff=SimpleNamespace(
            summary="sub を前へ 0.4m",
            changed_entity_ids=("sub-1",),
            dsp_parameters=(),
            treatment_item_ids=(),
            topology_changes=("replace: sub-1",),
        ),
        generated_authorities=tuple(generated),
        evidence_authorities=(),
        evidence_state="exploratory",
        evaluation_coverage=coverage,
        incomparability_reason=(
            None if comparable else "fidelity mismatch"
        ),
        metrics=tuple(metrics),
        regressions=tuple(regressions),
        application_note=None,
        quantitatively_comparable=comparable and coverage == "complete",
    )


def _panel(tmp_path, planner, service, navigate=None):
    _app()
    statuses: list[str] = []
    panel = InterventionPlannerPanel(
        planner,
        service,
        on_status=statuses.append,
        on_navigate=navigate,
    )
    return panel, statuses


def test_panel_disables_creation_without_baseline(tmp_path) -> None:
    planner = _FakePlanner(SceneRepository(tmp_path / "s.sqlite3"))
    panel, _ = _panel(tmp_path, planner, _FakeService())

    assert panel.create_button.isEnabled() is False
    assert "ベースライン" in panel.family_reason_label.text()


def test_family_gates_follow_capability(tmp_path) -> None:
    planner = _FakePlanner(
        SceneRepository(tmp_path / "s.sqlite3"),
        context=_FakeContext(baseline=_FakeBaseline()),
    )
    panel, _ = _panel(tmp_path, planner, _FakeService())

    assert panel.create_button.isEnabled()
    # Treatment stays gated: no definition authorities exist yet.
    assert panel._family_checks["treatment"].isEnabled() is False
    assert panel._family_checks["geometry"].isEnabled()
    assert panel._family_checks["topology"].isEnabled()
    assert panel._family_checks["calibration"].isEnabled() is False


def test_create_study_binds_explicit_finding(tmp_path) -> None:
    planner = _FakePlanner(
        SceneRepository(tmp_path / "s.sqlite3"),
        context=_FakeContext(baseline=_FakeBaseline()),
    )
    panel, statuses = _panel(tmp_path, planner, _FakeService())
    panel._family_checks["calibration"].setChecked(False)
    panel._family_checks["treatment"].setChecked(False)
    panel.band_enabled_check.setChecked(True)

    panel.create_button.click()

    assert planner.created_kwargs is not None
    finding = planner.created_kwargs["finding"]
    assert finding.source_kind == "explicit_user_region"
    assert finding.target_band_hz == (80.0, 160.0)
    assert planner.created_kwargs["allowed_families"] == [
        "geometry",
        "topology",
    ]
    assert panel.study_tree.topLevelItemCount() == 1
    assert statuses


def test_alternative_apply_requires_typed_authority(tmp_path) -> None:
    planner = _FakePlanner(
        SceneRepository(tmp_path / "s.sqlite3"),
        context=_FakeContext(baseline=_FakeBaseline()),
    )
    service = _FakeService()
    spec = SimpleNamespace(
        spec_id="spec-1",
        created_at_utc="2026-09-25T00:00:00Z",
        scene_revision_id="rev-1",
        finding=SimpleNamespace(observable="magnitude_response"),
        allowed_families=("topology",),
    )
    planner.studies.append(spec)
    planner.alternatives["spec-1"] = [
        _fake_alternative(generated=()),
    ]
    panel, statuses = _panel(tmp_path, planner, service)
    panel.study_tree.topLevelItem(0).setSelected(True)
    panel.alternative_tree.topLevelItem(0).setSelected(True)

    panel._apply_selected()

    assert service.applied_ids == []
    assert any("authority" in text for text in statuses)


def test_variant_apply_runs_canonical_lifecycle(
    tmp_path, monkeypatch
) -> None:
    planner = _FakePlanner(
        SceneRepository(tmp_path / "s.sqlite3"),
        context=_FakeContext(baseline=_FakeBaseline()),
    )
    service = _FakeService()
    spec = SimpleNamespace(
        spec_id="spec-1",
        created_at_utc="2026-09-25T00:00:00Z",
        scene_revision_id="rev-1",
        finding=SimpleNamespace(observable="magnitude_response"),
        allowed_families=("topology",),
    )
    planner.studies.append(spec)
    planner.alternatives["spec-1"] = [
        _fake_alternative(
            generated=(_fake_ref("system_variant", "variant-9"),),
        ),
    ]
    panel, _ = _panel(tmp_path, planner, service)
    applied: list[str] = []
    panel.applied.connect(applied.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Apply),
    )
    panel.study_tree.topLevelItem(0).setSelected(True)
    panel.alternative_tree.topLevelItem(0).setSelected(True)

    panel._apply_selected()

    assert service.applied_ids == ["variant-9"]
    assert applied == ["rev-new"]


def test_apply_hands_off_to_owner_workspace(tmp_path) -> None:
    planner = _FakePlanner(
        SceneRepository(tmp_path / "s.sqlite3"),
        context=_FakeContext(baseline=_FakeBaseline()),
    )
    spec = SimpleNamespace(
        spec_id="spec-1",
        created_at_utc="2026-09-25T00:00:00Z",
        scene_revision_id="rev-1",
        finding=SimpleNamespace(observable="magnitude_response"),
        allowed_families=("geometry",),
    )
    planner.studies.append(spec)
    planner.alternatives["spec-1"] = [
        _fake_alternative(
            generated=(_fake_ref("search_spec", "spec-x"),),
            family="geometry",
        ),
    ]
    navigated: list[WorkspaceDeepLink] = []

    def _navigate(link):
        navigated.append(link)
        return True

    panel, _ = _panel(
        tmp_path,
        planner,
        _FakeService(),
        navigate=_navigate,
    )
    panel.study_tree.topLevelItem(0).setSelected(True)
    panel.alternative_tree.topLevelItem(0).setSelected(True)

    panel._apply_selected()

    assert navigated == [
        WorkspaceDeepLink(
            workspace=WorkspaceId.OPTIMIZATION,
            section="candidates",
        )
    ]


def test_verify_hands_off_to_measurement_campaign(tmp_path) -> None:
    planner = _FakePlanner(
        SceneRepository(tmp_path / "s.sqlite3"),
        context=_FakeContext(baseline=_FakeBaseline()),
    )
    navigated: list[WorkspaceDeepLink] = []

    def _navigate(link):
        navigated.append(link)
        return True

    panel, _ = _panel(tmp_path, planner, _FakeService(), navigate=_navigate)

    panel.verify_button.click()

    assert navigated == [
        WorkspaceDeepLink(
            workspace=WorkspaceId.MEASUREMENT,
            section="campaign",
        )
    ]


def test_study_marks_stale_when_head_revision_moves(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "s.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    planner = _FakePlanner(
        repository,
        document_id=F1_DOCUMENT_ID,
        context=_FakeContext(baseline=_FakeBaseline(revision_id="rev-2")),
    )
    planner.studies.append(
        SimpleNamespace(
            spec_id="spec-old",
            created_at_utc="2026-09-25T00:00:00Z",
            scene_revision_id="rev-1",
            finding=SimpleNamespace(observable="magnitude_response"),
            allowed_families=("geometry",),
        )
    )
    panel, _ = _panel(tmp_path, planner, _FakeService())

    assert panel.study_tree.topLevelItem(0).text(3) == "基準が更新済み"
