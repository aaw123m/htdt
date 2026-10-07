# Issue #804 — 全体ゴールデンパス・ジャーニーストリップ

## 背景

#804 の Gate B は、部屋の作成から成果物出力までの一連の流れで
オペレータが迷わないことを要求する。測定・最適化の各ワークスペースには
既に番号付きジャーニーストリップがあるが、プロジェクト全体をまたぐ
「いま自分がどこにいるか」を示す面は存在しなかった。

## 実装

`golden_path_journey.py` は OverviewReadinessService が既に集約している
永続化状態だけから評価する純粋エバリュエータ:

1. **部屋** — ヘッドリビジョン + `document.room` が存在すれば done
2. **機材・配置** — スピーカーが存在し、役割（ch）が未設定・重複なし
3. **測定** — 取り込み済み測定が 1 件以上
4. **予測** — 現在のリビジョンに対する completed 予測が存在
5. **候補の比較** — SystemVariant または search spec が存在
6. **変更の適用** — variant stage が applied 系（proposed は適用ではない）
7. **再測定で確認** — `measured_validated` の variant が存在
8. **成果物の出力** — いつでも利用可能な案内ステップ（ゲートにしない）

## 設計上の約束

- **ガイダンスでありゲートではない** — 全ステップがナビゲーション
  リンク（`WorkspaceDeepLink`）を持ち、done ステップも押せる。
- **ちょうど 1 つだけ current** — 最初の未完了ステップ。
- **fail-closed** — シーン未保存なら下流は全て blocked で、
  「部屋の保存後に利用できます」と理由を示す。途中抜けが
  done に見えることはない。
- **再測定の位置づけ** — 適用後の確認測定は測定キャンペーン面への
  リンクであり、apply ステップの完了だけでは verify は点灯しない
  （提案の適用 ≠ 実測確認）。

## 表示

`OverviewWorkspace` はカード領域の先頭に「使い方の流れ N/8」カードを
描画する。current はプライマリアクション色、done は ✓ 付きの
success 表示、blocked はツールトチップで理由を説明する
（`WA_AlwaysShowToolTips`）。

## 検証

`backend/tests/test_golden_path_journey.py`（10 件）:

- 空プロジェクト → room が current、下流は全 blocked
- ちょうど 1 つの current / 単調な進行
- 全ステップがナビゲーションターゲットを持つ
- proposed は applied に数えない
- `measured_validated` で全 done
- ビューモデル配線（シーンなし／あり）
- OverviewWorkspace がストリップを描画しステップ押下で遷移する
