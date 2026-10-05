"""Machine-readable physical-acceptance gate manifest (REV48-HWGUIDE).

Each :class:`GateDefinition` derives its ordered steps verbatim from the real
acceptance sources — ``docs/WINDOWS_ACCEPTANCE.md`` §2, the hardware-gate
PowerShell harnesses, ``docs/O60E_CAMPAIGN.md``, the UX160 owned-Windows
matrix and the issue verification manifest. ``source_ref`` on every step
names the doc item it came from so the manifest stays auditable against the
documents it encodes.

Kind assignment is fail-closed: a step is only ``auto`` when software can
really run the check on this machine (``acceptance_checks``). Anything that
needs a body in the room is ``guided_manual`` (auto-capture where possible,
human confirms), and anything software cannot observe is ``attest``.
"""

from __future__ import annotations

from .cad_acceptance import GateDefinition, GateStepDef


def _step(
    step_id: str,
    kind: str,
    title_ja: str,
    instruction_ja: str,
    *,
    auto_check: str | None = None,
    evidence_required: tuple[str, ...] = (),
    input_label_ja: str | None = None,
    source_ref: str = '',
) -> GateStepDef:
    return GateStepDef(
        step_id=step_id,
        kind=kind,  # type: ignore[arg-type]
        title_ja=title_ja,
        instruction_ja=instruction_ja,
        auto_check=auto_check,
        evidence_required=evidence_required,
        input_label_ja=input_label_ja,
        source_ref=source_ref,
    )


GATE_WINDOWS_M10 = GateDefinition(
    gate_id='windows-m10',
    title_ja='M10 実データ受入（所有Windows機）',
    description_ja=(
        'WINDOWS_ACCEPTANCE.md §2 の手順1〜10を機械可読化したもの。'
        'REW実機・UMIK-1・RX-A4Aを使う受入ゲート。'
    ),
    source_ref='docs/WINDOWS_ACCEPTANCE.md §2',
    steps=(
        _step(
            'rew-version-record',
            'auto',
            'REWバージョンを記録',
            'REWが起動し読取専用API(port 4735)が有効なことを確認し、'
            '使用中のREWバージョンを実行記録へ保存します。',
            auto_check='rew_engine_probe',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順1',
        ),
        _step(
            'rew-text-export',
            'guided_manual',
            '同一測定をtext exportとAPIの双方で取得',
            'REWで対象measurementをtext exportし、HTDTの測定ページから'
            '読取専用APIで同じ測定を取り込んでください。'
            'エクスポートしたtextファイルを証拠として添付してください。',
            evidence_required=('rew_text_export',),
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順1',
        ),
        _step(
            'umik-input-config',
            'auto',
            'UMIK-1・48kHz・校正ファイルを確認',
            'REWの入力デバイスがUMIK-1、サンプルレート48kHz、'
            '校正ファイルが選択済みであることを自動確認します。',
            auto_check='rew_input_ready',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順2',
        ),
        _step(
            'umik-orientation',
            'guided_manual',
            'UMIK-1を天井に向ける',
            'UMIK-1を天井に向けて設置し、REWで90°校正ファイルを'
            '選択してください。物理的な向きは機器で確認できないため、'
            '確認ボタンで宣言を記録します（任意で写真を添付）。',
            auto_check='rew_calibration_selected',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順2（物理向き）',
        ),
        _step(
            'av-routing-evidence',
            'guided_manual',
            'RX-A4Aのrouting証跡を記録',
            'RX-A4A(AVレシーバー)のHDMI接続とチャンネル割当を確認し、'
            'FL/FR/センター/高さのrouting証跡を記録してください。'
            'REWの出力割当は自動取得します。任意で画面写真等を添付できます。',
            auto_check='rew_output_mapping',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順3',
        ),
        _step(
            'r1-repeat-measures',
            'guided_manual',
            'R1でFL/FRを各2回以上測定',
            '基準ポジションR1でFL/FRを各2回以上測定し、'
            'repeat groupとしてHTDTへ取り込んでください。',
            auto_check='rew_measurement_count:2',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順4',
        ),
        _step(
            'r2-single-change',
            'attest',
            'R2では1項目だけ変更して再測定',
            'スピーカーまたはMLPを1項目だけ変更してR2条件を作り、'
            '同条件で再測定してください。変更内容を証明として記入してください。',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順5',
        ),
        _step(
            'ab-comparison-report',
            'guided_manual',
            'A/B比較を作成してレポートを保存',
            'R1/R2のA/B比較を作成し、HTMLまたはJSONレポートを'
            '保存してください。比較レコードの存在は自動確認します。',
            auto_check='comparison_present',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順6',
        ),
        _step(
            'restart-persistence',
            'auto',
            '再起動後に同じProject/Comparisonが再現',
            '「チェック実行」で永続化プローブを記録した後、'
            'HTDTとREWを終了しWindowsを再起動してください。'
            'アプリを再起動してこの実行を再開し、再度「チェック実行」を'
            '押すと再起動をまたいだ再現性を検証します。',
            auto_check='persistence_probe',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順7',
        ),
        _step(
            'backup-restore',
            'auto',
            'backup→別フォルダへrestoreして内容一致',
            'バックアップZIPを作成し別フォルダへ復元して'
            'SQLite整合性と証跡ファイルの一致を自動検証します。',
            auto_check='backup_restore_roundtrip',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順8',
        ),
        _step(
            'rew-parity',
            'guided_manual',
            'REW textとAPI取得の照合を確認',
            '手順1で取得したREW text exportとAPI取込みの'
            '周波数軸・レベル・平滑化来歴を照合し、一致を確認してください。',
            evidence_required=('rew_text_export',),
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順9',
        ),
        _step(
            'a04-prominence',
            'attest',
            'A04のpeak/dip prominenceの実部屋確認',
            'A04のpeak/dip prominence判定が実部屋で過検出/未検出に'
            'なっていないか確認し、結果を証明として記入してください。',
            source_ref='WINDOWS_ACCEPTANCE.md §2 手順10',
        ),
    ),
)

GATE_O60R = GateDefinition(
    gate_id='o60r',
    title_ja='O60R 実室キャンペーン受入',
    description_ja=(
        'O60R所有部屋キャンペーンの受入（issue #1）。'
        'キャンペーン事前登録→実室計測→監査の順に進めます。'
    ),
    source_ref='docs/O60E_CAMPAIGN.md / scripts/run-o60-owned-room-gate.ps1 / issue #1',
    steps=(
        _step(
            'env-snapshot',
            'auto',
            '実行環境の証跡を記録',
            'OS・Python・GPU・ディスプレイ構成を実行記録へ保存します。',
            auto_check='env_snapshot',
            source_ref='run-o60-owned-room-gate.ps1 環境記録',
        ),
        _step(
            'campaign-prereg',
            'auto',
            '検証キャンペーンの事前登録を確認',
            '実測前に登録された検証キャンペーン（document/searchspec/'
            'candidate-set/model SHA固定）の存在をDBから確認します。'
            '未登録の場合は最適化画面の検証キャンペーン作成で登録してください。',
            auto_check='campaign_registered',
            source_ref='O60E_CAMPAIGN.md 事前登録',
        ),
        _step(
            'rew-engine-probe',
            'auto',
            'REWエンジン接続を確認',
            'REW API(port 4735)への接続とバージョンを記録します。',
            auto_check='rew_engine_probe',
            source_ref='issue #1 実測要件',
        ),
        _step(
            'physical-campaign',
            'guided_manual',
            '実室計測を実施して取り込む',
            'calibration/holdout/repeatability/separationの実室計測を実施し、'
            'Campaign実測としてHTDTへ取り込んでください。'
            'REWの測定数は自動記録します。',
            auto_check='rew_measurement_count:0',
            source_ref='O60E_CAMPAIGN.md 計測要件',
        ),
        _step(
            'campaign-audit',
            'auto',
            'キャンペーン監査を実行',
            'scripts/audit_o60_owned_room.pyをcampaign id指定で実行し、'
            '監査結果を記録します。',
            auto_check='o60r_audit',
            input_label_ja='監査するキャンペーンID',
            source_ref='scripts/audit_o60_owned_room.py',
        ),
        _step(
            'gate-harness',
            'attest',
            '実機ゲートハーネスの結果を記録',
            'scripts/run-o60-owned-room-gate.ps1を実機で実行した結果を'
            '証明として記入してください（ログファイルを添付可能）。',
            source_ref='scripts/run-o60-owned-room-gate.ps1',
        ),
    ),
)

GATE_UX160 = GateDefinition(
    gate_id='ux160',
    title_ja='UX160 所有Windows受入',
    description_ja=(
        'UX160所有Windows機での受入マトリクス（issue #3、'
        'ISSUE_118 UX-A01..A16）。物理環境でのみ確認できる項目を集約。'
    ),
    source_ref='docs/ISSUE_118_UX160_OWNED_WINDOWS_ACCEPTANCE_2026-09-20.md / issue #3',
    steps=(
        _step(
            'env-snapshot',
            'auto',
            '実機環境を記録',
            'OSビルド・GPU・ディスプレイ構成・DPIを実行記録へ保存します。',
            auto_check='env_snapshot',
            source_ref='ISSUE_118 環境ブロック',
        ),
        _step(
            'dpi-matrix',
            'attest',
            'DPIマトリクス表示確認',
            '100%/150%/200%スケーリング（1280×800, 1440×900等）で'
            'Overview/Room/測定/最適化の各表示が破綻しないことを確認し、'
            '結果を記入してください（スクリーンショット添付推奨）。',
            source_ref='ISSUE_118 DPIマトリクス行',
        ),
        _step(
            'first-use',
            'attest',
            '初回使用の発見性',
            '初回ユーザーが部屋作成→測定→最適化の導線を説明なしに'
            '辿れることを確認し、結果を記入してください。',
            source_ref='ISSUE_118 UX-A 初回使用',
        ),
        _step(
            'ux-surfaces',
            'attest',
            'UX-A01..A16 サーフェス確認',
            'コマンドパレット到達性・Room Inspector・測定フロー・'
            '状態一貫性・操作フィードバック・科学可視化・CADジェスチャ・'
            '日本語UIの各項目を確認し、結果を記入してください。',
            source_ref='ISSUE_118 UX-A01..A16',
        ),
        _step(
            'workflow-regression',
            'attest',
            'ワークフロー回帰（O90D/O100G）',
            'プロポーザル→候補選択→適用のワークフローが実機で回帰なく'
            '動作することを確認し、結果を記入してください。',
            source_ref='ISSUE_118 回帰行 / O90D・O100G',
        ),
    ),
)

GATE_GOLDEN_PATH = GateDefinition(
    gate_id='golden-path',
    title_ja='Golden Path 実機受入',
    description_ja=(
        'Golden Pathの実機物理受入（issue #8）。ソフトウェア事前検証を'
        '自動実行した後、UI完走を人間が確認します。'
    ),
    source_ref='scripts/golden_path_preflight.py / issue #8',
    steps=(
        _step(
            'preflight',
            'auto',
            'ソフトウェア事前検証（synthetic fixture）',
            'golden_path_preflight.pyを実行し、ソフトウェアのみの'
            'golden path検証を自動実行します（数十秒〜数分かかります）。',
            auto_check='golden_path_preflight',
            source_ref='scripts/golden_path_preflight.py',
        ),
        _step(
            'physical-golden-path',
            'attest',
            '実機でUIのみでgolden path完走',
            '実機WindowsでCLIなし・UI操作のみでgolden pathを完走できることを'
            '確認し、結果を記入してください（#723物理ゲート）。',
            source_ref='issue #8 / #723 物理ゲート',
        ),
    ),
)

GATE_NATIVE_MATRIX = GateDefinition(
    gate_id='native-matrix',
    title_ja='所有Windows受入マトリクス',
    description_ja=(
        'issue #132の受入マトリクス。自動化可能な行は自動チェック、'
        '物理環境行は証明として記録します。'
    ),
    source_ref='issue #132 受入マトリクス',
    steps=(
        _step(
            'env-snapshot',
            'auto',
            '実機環境を記録',
            'OS・Python・GPU・ディスプレイ構成を実行記録へ保存します。',
            auto_check='env_snapshot',
            source_ref='issue #132 環境行',
        ),
        _step(
            'dependency-lock',
            'auto',
            '出荷依存ロック整合を確認',
            'check_dependency_lock.pyを実行し依存ロックが整合している'
            'ことを確認します。',
            auto_check='dependency_lock_check',
            source_ref='issue #132 自動化可能行',
        ),
        _step(
            'dpi-graphics',
            'attest',
            'DPI/グラフィックス行',
            '各スケーリング・GPU経路での表示を確認し、結果を記入してください。',
            source_ref='issue #132 DPI/グラフィックス行',
        ),
        _step(
            'recovery',
            'attest',
            'リカバリ行',
            '中断復旧・クラッシュ再起動時の挙動を確認し、結果を記入してください。',
            source_ref='issue #132 リカバリ行',
        ),
        _step(
            'offline',
            'attest',
            'オフライン行',
            'ネットワーク切断状態での起動・操作を確認し、結果を記入してください。',
            source_ref='issue #132 オフライン行',
        ),
        _step(
            'routing',
            'guided_manual',
            'ルーティング行',
            'RX-A4A等への出力割当を実機で確認してください。'
            'REWの出力マッピングは自動取得します。',
            auto_check='rew_output_mapping',
            source_ref='issue #132 ルーティング行',
        ),
        _step(
            'installed-product',
            'attest',
            'インストール済み製品行',
            'インストーラ/パッケージ版での起動・更新を確認し、'
            '結果を記入してください。',
            source_ref='issue #132 インストール済み製品行',
        ),
    ),
)

GATE_O90E = GateDefinition(
    gate_id='o90e',
    title_ja='O90E 実室ロバスト検証',
    description_ja=(
        'O90E実室ロバスト検証の受入（issue #4）。O60R証跡を前提に'
        '摂動検証を実室で確認します。'
    ),
    source_ref='issue #4',
    steps=(
        _step(
            'o60r-evidence',
            'auto',
            'O60R証跡の存在を確認',
            '検証キャンペーン登録の存在をDBから確認します'
            '（O60R証跡の前提条件）。',
            auto_check='campaign_registered',
            source_ref='issue #4 前提条件',
        ),
        _step(
            'robust-domain',
            'guided_manual',
            '摂動ドメインの実室証跡を収集',
            'exact model/observable/perturbation domainで'
            'eligibleなO60/R180 evidenceを実室で収集し取り込んでください。',
            source_ref='issue #4 収集要件',
        ),
        _step(
            'perturbation-verify',
            'attest',
            '摂動検証結果の確認',
            '実室での摂動検証結果（ロバスト性判定）を確認し、'
            '結果を記入してください。',
            source_ref='issue #4 判定要件',
        ),
    ),
)

GATE_O100_PHYSICAL = GateDefinition(
    gate_id='o100-physical',
    title_ja='O100 物理・証跡ゲート',
    description_ja=(
        'O100共有証跡ゲート（issue #5）。UX160/O60R/O90Eの完了を'
        '自動照合し、production eligibilityを証明します。'
    ),
    source_ref='issue #5',
    steps=(
        _step(
            'ux160-done',
            'auto',
            'UX160受入の完了を照合',
            'ux160ゲートの合格済み実行が存在することを自動確認します。',
            auto_check='gate_run_completed:ux160',
            source_ref='issue #5 参照UX160',
        ),
        _step(
            'o60r-done',
            'auto',
            'O60R受入の完了を照合',
            'o60rゲートの合格済み実行が存在することを自動確認します。',
            auto_check='gate_run_completed:o60r',
            source_ref='issue #5 参照O60R',
        ),
        _step(
            'o90e-done',
            'auto',
            'O90E受入の完了を照合',
            'o90eゲートの合格済み実行が存在することを自動確認します。',
            auto_check='gate_run_completed:o90e',
            source_ref='issue #5 参照O90E',
        ),
        _step(
            'production-eligibility',
            'attest',
            'production recommendationのeligibility確認',
            'production recommendationのevidence eligibilityを確認し、'
            '結果を記入してください。',
            source_ref='issue #5 eligibility判定',
        ),
    ),
)

#: Canonical gate registry — every physical acceptance gate the app can guide.
GATE_REGISTRY: tuple[GateDefinition, ...] = (
    GATE_WINDOWS_M10,
    GATE_O60R,
    GATE_UX160,
    GATE_GOLDEN_PATH,
    GATE_NATIVE_MATRIX,
    GATE_O90E,
    GATE_O100_PHYSICAL,
)


def get_gate(gate_id: str) -> GateDefinition:
    for gate in GATE_REGISTRY:
        if gate.gate_id == gate_id:
            return gate
    raise KeyError(f'unknown acceptance gate: {gate_id}')


def collect_auto_checks() -> frozenset[str]:
    """Every auto-check id the manifest references (bare id, without args)."""
    ids: set[str] = set()
    for gate in GATE_REGISTRY:
        for step in gate.steps:
            if step.auto_check:
                ids.add(step.auto_check.split(':', 1)[0])
    return frozenset(ids)
