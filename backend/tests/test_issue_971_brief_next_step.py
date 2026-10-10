"""#971 Decision Brief next-step navigation.

The brief's 次の一手 and every 欠落証拠 row are navigable actions that
deep-link to the surface producing that evidence. Routes come only from
the sealed record's declared fields (recommendation kind / gap gate) —
an unroutable step renders a disabled button with the reason, and a
failed navigation reports instead of dying silently.
"""

from __future__ import annotations

import pytest

from test_issue_950_decision_brief_evidence import (  # noqa: E402
    DOC,
    _Env,
    _compose,
)

def _panel(env: _Env, on_navigate=None):
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    from htdt.cad_decision_brief_repository import (
        CadDecisionBriefRepository,
    )
    from htdt.decision_brief_panel import DecisionBriefPanel

    brief_repository = CadDecisionBriefRepository(
        env.scene_repository,
        system_variant_repository=env.variants,
        kind_resolvers=env.resolver.kind_resolvers(),
    )
    return DecisionBriefPanel(
        env.scene_repository,
        DOC,
        brief_repository=brief_repository,
        comparison_repository=env.comparisons,
        on_navigate=on_navigate,
    )


def _buttons(panel, prefix: str):
    from PySide6.QtWidgets import QPushButton

    return [
        b
        for b in panel.findChildren(QPushButton)
        if b.text().startswith(prefix)
    ]


def _rec_button(panel, rank: int = 1):
    from PySide6.QtWidgets import QPushButton

    prefix = f'次の一手 {rank}:'
    buttons = [
        b
        for b in panel.findChildren(QPushButton)
        if b.accessibleName().startswith(prefix)
    ]
    assert len(buttons) == 1
    return buttons[0]


def _gap_buttons(panel, rank: int = 1):
    from PySide6.QtWidgets import QPushButton

    prefix = f'{rank} の欠落証拠'
    return [
        b
        for b in panel.findChildren(QPushButton)
        if b.accessibleName().startswith(prefix)
    ]


class TestNextStepNavigation:
    def test_ready_action_routes_to_candidate_apply(self, tmp_path):
        env = _Env(tmp_path)
        env.add_all()
        env.comparison()
        env.briefs.save_brief(_compose(env, env.gates()))
        seen = []
        panel = _panel(
            env, on_navigate=lambda link: seen.append(link) or True
        )

        action = env.briefs.latest_brief(DOC).actions[0]
        assert action.recommendation.kind == 'apply_candidate'
        button = _rec_button(panel, rank=action.rank)
        assert button.isEnabled()
        button.click()
        assert len(seen) == 1
        link = seen[0]
        assert link.workspace.value == 'optimization'
        assert link.section == 'candidates'
        panel.deleteLater()

    def test_missing_gates_route_to_their_producers(self, tmp_path):
        env = _Env(tmp_path)
        # campaign + production evidence never produced → two gaps.
        env.add_solver_record()
        env.add_channel_verdict()
        env.add_deployment()
        env.comparison()
        env.briefs.save_brief(_compose(env, env.gates()))
        seen = []
        panel = _panel(
            env, on_navigate=lambda link: seen.append(link) or True
        )

        action = env.briefs.latest_brief(DOC).actions[0]
        assert action.recommendation.kind == 'collect_evidence'
        gap_gates = {gap.gate for gap in action.gaps}
        assert {'campaign', 'production_gate'} <= gap_gates

        gap_buttons = _gap_buttons(panel, rank=action.rank)
        assert len(gap_buttons) == len(action.gaps)

        # the next-step button follows the FIRST gap's producer
        first = action.gaps[0].gate
        _rec_button(panel, rank=action.rank).click()
        campaign_idx = [
            i
            for i, gap in enumerate(action.gaps)
            if gap.gate == 'campaign'
        ][0]
        gap_buttons[campaign_idx].click()
        assert [link.section for link in seen] == [
            _expected_section(first),
            'campaign',
        ]
        assert all(
            link.workspace.value == 'measurement' for link in seen
        )
        panel.deleteLater()

    def test_unroutable_or_unwired_renders_disabled_honestly(self, tmp_path):
        env = _Env(tmp_path)
        env.add_all()
        env.comparison()
        env.briefs.save_brief(_compose(env, env.gates()))
        # no on_navigate wired — the composition cannot route anywhere,
        # so buttons must disable with the reason instead of dead-ending.
        panel = _panel(env, on_navigate=None)

        action = env.briefs.latest_brief(DOC).actions[0]
        button = _rec_button(panel, rank=action.rank)
        assert not button.isEnabled()
        assert button.toolTip()
        panel.deleteLater()

    def test_failed_navigation_reports_not_silently(self, tmp_path):
        env = _Env(tmp_path)
        env.add_all()
        env.comparison()
        env.briefs.save_brief(_compose(env, env.gates()))
        panel = _panel(env, on_navigate=lambda link: False)

        action = env.briefs.latest_brief(DOC).actions[0]
        _rec_button(panel, rank=action.rank).click()
        assert panel.status_label.text() == (
            'その画面へ移動できませんでした。'
        )
        panel.deleteLater()


def _expected_section(gate: str) -> str:
    from htdt.decision_brief_panel import _GAP_GATE_ROUTES

    link = _GAP_GATE_ROUTES[gate]
    return link.section


class TestRouteMapIntegrity:
    def test_every_mapped_section_resolves(self):
        """Each declared route's section must exist in the target
        workspace's registered contexts (or a known resolvable section),
        so no button ever deep-links into a void."""

        from htdt.decision_brief_panel import (
            _GAP_GATE_ROUTES,
            _REC_ROUTES,
        )
        from htdt.measurement.ui import (
            measurement_page_workspace as mpw,
        )
        from htdt.workflow_navigation import (
            CANONICAL_WORKSPACE_CONTEXTS,
            WorkspaceId,
        )

        measurement_contexts = set(mpw._CONTEXT_IDS)
        contexts = {
            WorkspaceId.MEASUREMENT: measurement_contexts,
            WorkspaceId.ROOM: {
                c.context_id
                for c in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.ROOM]
            },
            WorkspaceId.OPTIMIZATION: {
                c.context_id
                for c in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.OPTIMIZATION]
            },
        }
        for name, mapping in (
            ('rec', _REC_ROUTES),
            ('gap', _GAP_GATE_ROUTES),
        ):
            for key, link in mapping.items():
                assert link.section in contexts[link.workspace], (
                    f'{name} route {key} -> '
                    f'{link.workspace}/{link.section} resolves nowhere'
                )
