"""#883 crash-safe session recovery — the operator-facing dialog.

Shown once per launch after the workflow shell opens, when the journal
inspection found crashed prior sessions (or recovery evidence that
failed integrity). Every choice seals an append-only decision row:
restore acceptance carries the restoring session's lineage, discard is
explicit, and defer leaves the evidence untouched for the next launch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session_recovery import (
        RejectedRecoveryData,
        SessionRecoveryInspection,
        SessionRecoveryReport,
    )

_ENDING_LABEL_JA = {
    'application_crash': 'アプリケーションのクラッシュ',
    'forced_termination': '強制終了（プロセスの終了）',
    'os_restart_or_power_loss': 'OS再起動または電源断',
    'incompatible': '互換性のないバージョン',
    'still_running': '実行中',
    'clean': '正常終了',
}

_RECONCILIATION_LABEL_JA = {
    'RECONCILIATION_REQUIRED': '要照合',
    'DEVICE_STATE_UNKNOWN': 'デバイス状態不明',
    'ACQUISITION_INCOMPLETE': '測定未完',
    'WRITE_COMPLETION_UNKNOWN': '書込み完了不明',
}

_ITEM_KIND_LABEL_JA = {
    'scene_draft': '未保存のシーン変更',
    'workspace_state': 'ワークスペース/ナビゲーション状態',
    'pending_annotation': '未確定の注記',
    'pending_import': '取り込み途中の測定データ',
    'project_binding': '開いていたプロジェクト',
    'commissioning_run': 'コミッショニング実行',
    'measurement_campaign': '測定キャンペーン',
}


def _report_text(report: SessionRecoveryReport) -> str:
    ending = _ENDING_LABEL_JA.get(report.ending, report.ending)
    lines = [
        f'前回のセッションは正常に終了していません（{ending}）。',
        '',
        f'セッション: {report.session_id}',
        f'ビルド: {report.build_id or "不明"}',
        f'開始時刻: {report.started_at_utc}',
        f'最終確認: {report.last_seen_at_utc}',
    ]
    if report.project_document_id:
        lines.append(
            f'開いていたプロジェクト: {report.project_document_id}'
        )
    available = [
        item for item in report.items if item.availability == 'available'
    ]
    blocked = [
        item for item in report.items if item.availability == 'blocked'
    ]
    if available:
        lines += ['', '復元できる内容:']
        lines += [f'・{item.summary}' for item in available]
    if blocked:
        lines += ['', '検証できなかった内容（復元しません）:']
        lines += [f'・{item.summary}' for item in blocked]
    unresolved = [
        item for item in report.reconciliations if item.resolution is None
    ]
    if unresolved:
        lines += [
            '',
            '完了が不確かな外部操作があります。復元後に'
            '「未確定」として表示され、確認が済むまで進められません:',
        ]
        lines += [
            f'・{_RECONCILIATION_LABEL_JA.get(item.state, item.state)}: '
            f'{item.summary}'
            for item in unresolved
        ]
    return '\n'.join(lines)


def _report_detail(report: SessionRecoveryReport) -> str:
    lines = [
        f'ending: {report.ending}',
        *(
            f'  - {reason}'
            for reason in report.ending_reasons
        ),
        f'integrity: {report.integrity}',
        f'journal entries: {report.entry_count}',
        f'journal: {report.journal_dir}',
    ]
    if report.items:
        lines.append('items:')
        for item in report.items:
            label = _ITEM_KIND_LABEL_JA.get(item.kind, item.kind)
            lines.append(
                f'  - {label} [{item.availability}] {item.ref_id}'
            )
    if report.reconciliations:
        lines.append('reconciliations:')
        for item in report.reconciliations:
            lines.append(
                f'  - {item.kind} {item.ref_id} -> {item.state} '
                f'(resolution: {item.resolution})'
            )
    return '\n'.join(lines)


def offer_session_recovery(
    parent: Any,
    inspection: SessionRecoveryInspection,
    repository: Any,
    *,
    restoring_session_id: str | None,
    actor: str = 'operator',
    logger: Any = None,
) -> None:
    """Present crashed-session evidence; apply the operator's choice.

    'Restore' seals ``restore_accepted`` per item +
    ``reconcile_deferred`` per open reconciliation + a session-scope
    ``restore_completed`` — the canonical store is untouched; the
    reopened session consumes the evidence. 'Discard' seals
    ``discarded`` and deletes the journal files. 'あとで' seals
    ``deferred`` and leaves everything on disk.
    """

    from PySide6.QtWidgets import QMessageBox

    from .session_recovery import (
        apply_session_restore,
        decide_session,
        discard_session,
        reject_session_evidence,
    )

    for report in inspection.reports:
        if report.resolved:
            continue
        box = QMessageBox(parent if hasattr(parent, 'isVisible') else None)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('HTDT セッション復旧')
        box.setText(_report_text(report))
        box.setDetailedText(_report_detail(report))
        restore_button = box.addButton(
            '復元する', QMessageBox.ButtonRole.AcceptRole
        )
        discard_button = box.addButton(
            '復旧データを破棄', QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(
            'あとで決める', QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(restore_button)
        box.exec()
        clicked = box.clickedButton()
        try:
            if clicked is restore_button:
                apply_session_restore(
                    repository,
                    report,
                    restoring_session_id=restoring_session_id,
                    actor=actor,
                )
                if logger is not None:
                    logger.info(
                        'session recovery accepted: %s', report.session_id
                    )
            elif clicked is discard_button:
                confirm = QMessageBox.question(
                    parent if hasattr(parent, 'isVisible') else None,
                    'HTDT セッション復旧',
                    '復旧データを完全に削除します。よろしいですか？',
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if confirm == QMessageBox.StandardButton.Yes:
                    discard_session(
                        repository,
                        report,
                        actor=actor,
                        reason='operator discarded recovered session',
                    )
                    if logger is not None:
                        logger.info(
                            'session recovery discarded: %s',
                            report.session_id,
                        )
                else:
                    decide_session(
                        repository,
                        report.session_id,
                        'deferred',
                        document_id=report.project_document_id or '',
                        actor=actor,
                        restoring_session_id=restoring_session_id,
                        reason='discard not confirmed',
                    )
            else:
                decide_session(
                    repository,
                    report.session_id,
                    'deferred',
                    document_id=report.project_document_id or '',
                    actor=actor,
                    restoring_session_id=restoring_session_id,
                    reason='operator deferred recovery',
                )
        except Exception:
            if logger is not None:
                logger.exception(
                    'session recovery decision failed: %s',
                    report.session_id,
                )

    rejected = list(inspection.rejected)
    if rejected:
        lines = [
            '破損または互換性のない復旧データを検出しました。',
            'これらは使用できません（そのまま残すか削除を選べます）。',
            '',
        ]
        lines += [
            f'・{item.session_id or "?"}: {item.reason}'
            for item in rejected
        ]
        box = QMessageBox(parent if hasattr(parent, 'isVisible') else None)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle('HTDT セッション復旧')
        box.setText('\n'.join(lines))
        discard_button = box.addButton(
            '壊れたデータを削除',
            QMessageBox.ButtonRole.DestructiveRole,
        )
        box.addButton('残す', QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is discard_button:
            for item in rejected:
                try:
                    reject_session_evidence(
                        repository,
                        item,
                        actor=actor,
                        reason='operator deleted rejected evidence',
                        delete_journal=True,
                    )
                except Exception:
                    if logger is not None:
                        logger.exception(
                            'rejected-evidence removal failed: %s',
                            item.session_id,
                        )


__all__ = ['offer_session_recovery']
