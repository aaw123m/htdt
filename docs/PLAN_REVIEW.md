# 計画レビューと修正記録

> レビュー日: 2026-09-15（初回・追加）
> 初回対象: 6d0c0da99ad69d4f3d8228844a45949c121fff8cのREADME.md、docs/PROJECT_PLAN.md
> 追加対象: mainの10dabf995453e1351fce32e2041790c925d2e31bにある計画6文書
> 範囲: 計画書の検証・詳細化。アプリ実装、Windows実機検証、実測データ検証は実施していない。

**最新のIssue #101精査は§12、ゼロベースレビューは§11、追加レビューは§10、前回は§9。CAD-firstレビューは[§7](#7-cad-first追加レビュー2026-09-16)、正本化は[§8](#8-正本化と旧issueprの整理2026-09-16)を参照。§1–6は当時の判断の記録であり、旧browser方針を今後の指示として適用しない。**

§1–5は初回レビューの記録を残す。追加レビューの指摘と反映先は[§6](#6-追加レビュー)を参照。現在の仕様は各設計文書を正本とする。

## 1. 総合判断

REWを中心に据え、HTDTが部屋・条件・履歴を結び付ける方向は維持する。Windows・個人利用・ローカル完結・過剰なセキュリティを持ち込まない方針も妥当。

修正の中心は、技術の選び直しよりも「既存機能をどこまで使うか」「何を測ったデータか」「どこまで比較・予測できるか」を具体化すること。旧計画には章間の矛盾、所有機器の未確認前提、履歴の後回し、音響モデルから得られる情報の過大評価につながる記述があった。

## 2. 旧計画からの主な変更

| ID | 種類・優先度 | 旧計画の該当箇所 | 問題と修正 |
|---|---|---|---|
| R01 | 矛盾・高 | §6.1、§23 v0.1 | APIを第一経路としながらv0.1はファイル先行。**v0.1=ファイル、v0.2=任意API**に統一 |
| R02 | 再利用不足・高 | §5、§18、§19 | REW Room SimulatorとAPIを評価せず独自予測へ進む構成。**REW再利用評価を先行**し、追加エンジンは不足がある時だけ |
| R03 | 履歴要件の矛盾・高 | §12、§23 v0.2 | 配置変更比較がMVPなのにLayoutRevisionがv0.2。**配置・部屋状態・AVR条件・測定点のスナップショットをv0.1へ** |
| R04 | データモデル不足・高 | §12 Measurement | 「1チャンネル×1席」だけでは低音転送やL+Rに対応できない。**入力役割と実際の音源群を分離** |
| R05 | 条件不足・高 | §8、§17 | room_correctionの単一enumだけでは比較を再現できない。**EQ、音場モード、距離、ゲイン、低音振り分けを別保存** |
| R06 | 精度の前提不足・高 | §6.3、§9、§20 | spl_db配列だけでは絶対SPL/相対値の区別や補正状態を保てない。**単位、校正、窓、平滑化、時間原点、来歴を必須化** |
| R07 | 未確認前提・高 | README、§8.2 | RX-A4A、Windows 11、USBマイクが所有・確定情報として読める。**ユーザー制約と検証環境案を分ける** |
| R08 | 出力経路の前提不足・高 | README、§3、§10 | Atmos配置と個別測定可能性の区別がない。**チャンネル対応を実確認**し、未対応高さは配置管理のみでも可 |
| R09 | 数式の適用条件不足・高 | §9、§17、§18 | 境界距離だけのSBIR候補では幾何条件が曖昧。**実音源/鏡像の経路差**を使い、c/(4d)は限定近似とする |
| R10 | 最適化の成立条件不足・高 | §19、§23 v1.0 | モード周波数だけでは位置依存の応答を評価できず、点音源でtoe-inも評価できない。**実測候補比較を先行、予測探索は条件付き** |
| R11 | 過度な一般化・中 | §9.4、§18 | image-sourceと低域の関係を一括りに読める。**モデルの理論的性質と実部屋の再現性を分離** |
| R12 | 保存設計不足・高 | §13、§23 v1.0 | 外部配列・原本とDBの整合、復元、移行が曖昧。**初期はSQLite BLOB、原本確保→DB commit、復元をv0.1へ** |
| R13 | 実装順序の矛盾・中 | §14、§23 v0.5、§26 | グラフをMVPで作るのにライブラリ最終選択をv0.5で評価。**最初のグラフで第一候補を評価**し未達時のみ比較 |
| R14 | 座標契約不足・中 | §11 | 位置軸だけで姿勢、handedness、Three.js変換が未定義。**方向ベクトルと変換を規定** |
| R15 | 工程不足・高 | §20、§23–26 | 完了条件が抽象的で実ファイル・異常系・数値許容差がない。**作業ID・依存・fixture・受入条件を追加** |
| R16 | 運用の表現修正・中 | §2.3、§22 | 127.0.0.1だけで安全対策が完結すると読める。**同一originと軽い入力検証**を加え、認証基盤は導入しない |

「資料上誤り」と「実装前に必要な追加仕様」は区別した。例えば旧計画のREW API既定ポートや自動スイープのライセンス注意は大筋で正しく、そのまま精密化している。採用ライブラリがすべて不適切だったという評価ではない。

## 3. 調査から変えた判断

### REWでできることを先に使う

公開資料でRoom Simulatorと連携口の存在を確認した。HTDTでは既存モデルの結果と配置・条件を結び付ける作業を優先する。取得可能なデータの範囲、対象版、座標対応は実装時に確認する。[REW API](https://www.roomeqwizard.com/help/help_en-GB/html/api.html)

.mdatを直接読む設計は引き続き採用しないが、REWの再解析用原本としての価値は残す。HTDTがファイルを解析しないことと、原本を保管しないことを混同しない。

### 「比較できる」の意味を先に決める

表示できる2本の曲線が、同じ条件で比較できるとは限らない。差分、形状比較、複素和は必要条件が異なる。保存した比較設定に帯域・補間・レベル調整・算法版を持たせる方針は、この制約からの設計判断である。

### 機器依存の機能をMVPから切り離す

メーカーの具体例を所有機器の確定情報にしない。サブなしや高さチャンネルの扱いは、実際の音声経路を確認して決める。Yamaha資料はYamahaの機能の根拠であり、他メーカーのAVRにも同じ挙動があることの証拠としては使わない。

### 計画を早期に固定しすぎない

旧書の「基礎的なアーキテクチャ判断を再度開かずに実装できるまで固める」という目標を変更した。ドメインとデータ不変条件は明確にする一方、実データ未確認のparser契約やWindows未検証の依存は暫定に留める。

pyroomacousticsの公開説明には新しい版表示と古いPython/Windows導入説明が併存する。説明文だけで対象環境のwheel提供や正常動作を保証しない。必要になった時点で実際の配布物と導入結果を確認する。[pyroomacoustics公式リポジトリ](https://github.com/LCAV/pyroomacoustics)

## 4. 一次資料と用途

以下は2026-09-15に内容を参照した資料。Web上の説明は更新されるため、使用版・導入手順・互換性は実装時に改めて固定する。

| 資料 | この計画での用途 | 検証の限界 |
|---|---|---|
| [REW API](https://www.roomeqwizard.com/help/help_en-GB/html/api.html) | 読取連携、配列形式、識別子、Room Simulator連携の設計 | 所有PCのREWビルドでの提供を未確認 |
| [REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html) | テキスト/IRの出力設定が必要という判断 | 実サンプルでのparser互換性は未検証 |
| [REW Soundcard Preferences](https://www.roomeqwizard.com/help/help_en-GB/html/soundcard.html) | Windowsの入出力とEXCL経路の確認項目 | 実際のHDMI/マイクの動作保証ではない |
| [REW Making Measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html) | レベル・スイープ・時間基準の記録項目 | 部屋と機器に適した設定値は未決定 |
| [REW Analysis Preferences](https://www.roomeqwizard.com/help/help_en-GB/html/analysis.html) | タイミング・解析設定の来歴 | サンプルに適用された状態は取込時に確認 |
| [REW Impulse Graph](https://www.roomeqwizard.com/help/help_en-GB/html/graph_impulse.html) | IR/ETCを比較する際の処理条件 | HTDT算法と数値一致することは未検証 |
| [REW Room Simulator](https://www.roomeqwizard.com/help/help_en-GB/html/modalsim.html) | 既存ソルバー優先、矩形モデルの適用範囲 | 非矩形室や所有スピーカーへの精度保証なし |
| [REW EQ Window](https://www.roomeqwizard.com/help/help_en-GB/html/eqwindow.html) | EQは既存機能へ委ねる方針の確認 | AVRへ適用できるフィルター形式は対象外 |
| [Yamaha RX-A4A User Guide](https://data.yamaha.com/files/download/other_assets/5/1335595/AV19-0070_RX-A4A_user_guide_En_UCRABGLFP_H0.pdf) | 低音転送、Subwoofer None、Parametric EQの設定を分ける例 | ユーザー所有機種であることを示さない |
| [Yamaha RX-A4A スピーカー構成](https://manual.yamaha.com/av/20/rxa4a/en-US/5449845643.html) | 機種ごとに構成制約を確認する例 | 汎用の5.x/7.x/Atmos保証に転用しない |
| [Microsoft Spatial Sound](https://learn.microsoft.com/en-us/windows/win32/coreaudio/spatial-sound) | 通常出力とSpatial Audio対応を分離 | REWで個別高さ出力が可能なことの証拠ではない |
| [Genelec Monitor Placement](https://www.genelec.com/monitor-placement) | 境界干渉・位置依存性の説明を確認 | 特定の部屋の谷の原因を確定しない |
| [pyroomacoustics](https://github.com/LCAV/pyroomacoustics) | 既存image-source/RIRエンジン候補 | Windows/Pythonの対象組合せは未検証 |
| [SQLite Backup API](https://www.sqlite.org/backup.html) | 稼働中DBの整合したバックアップ方式 | 別保存のrawを自動で含める機能ではない |

数式、データモデル、工程、初期性能目標はこれらを踏まえたHTDTの設計提案であり、外部製品の公称仕様として提示していない。

## 5. 今回の改訂後に残る確認事項

| 未確認事項 | 必要になる段階 | 準備するもの |
|---|---|---|
| 実際のAVR・マイク・Windows環境 | v0.1開始前 | 型番、OS、経路、REW版 |
| 対応するREW出力の厳密な書式 | parser実装前 | FL/FRと再測定の実サンプル |
| 部屋の形と寸法精度 | 配置編集/音響候補 | 概略図と座標 |
| 絶対SPL校正の有効性 | 絶対値比較 | 校正と入力ゲインの記録 |
| APIとIRの扱い | v0.2開始時 | 対象版仕様・応答fixture・IR出力条件 |
| 予測モデルの有効性 | v0.5/探索前 | 比較可能な複数配置の実測 |
| グラフ・保存の実性能 | 初期縦断実装 | 対象Windows PCと代表サイズのデータ |

これらは今回の文書改善を止める理由ではない。未確認を明示した状態で計画を完成させ、実装開始後に必要な順で確認する。

## 6. 追加レビュー

### 結論と修正箇所

前回のREW優先・ファイル取込先行・Windows個人利用という方針は維持する。追加レビューでは、実装時に異なる解釈が生まれるデータ契約と、比較の再現性を損なう不足を修正した。新しい測定エンジンや依存ライブラリは追加していない。

| ID | 優先度・種類 | 対象箇所 | 問題と反映した修正 |
|---|---|---|---|
| R17 | 高・外部仕様の見落とし | 測定§5、データ§4 | REWテキストは位相なしでも0.0を出力する。列の存在から有効位相と判定せず、phase_statusと出所を保持 |
| R18 | 中・処理条件不足 | 測定§7 | APIのppo指定は追加平滑化を伴い得る。内部の96 PPO再標本化と分け、取得要求と実際の処理状態を保存 |
| R19 | 高・分類の矛盾 | データ§2、測定§5/8 | derived分類が後回しで、予測にも実測Contextを要求し得た。4分類をv0.1から保持し、予測に架空の実機条件を割り当てない |
| R20 | 高・履歴契約不足 | データ§1/4 | 不変配置だけではマイク訂正・再解析・算法更新後の比較が変わり得る。Measurement/Dataset/Contextの版を固定参照し、比較結果も保存 |
| R21 | 高・数値契約不足 | データ§6 | 全表示曲線の共通帯域では第三曲線がA/B指標を変える。ペア別帯域、固定グリッド、除外区間、点数不足・基準帯域不足の挙動を定義 |
| R22 | 高・品質条件不足 | 測定§4、データ§5 | 有限の数値でもクリッピング等の不良測定はあり得る。品質状態と理由、繰返し差を保持し、改善の判定と分離 |
| R23 | 高・保存範囲不足 | データ§9、測定§4 | .mdat・校正の保管方針に具体的な添付参照がなかった。RawAssetと参照、任意添付、完全バックアップの対象・欠損時の失敗を定義 |
| R24 | 中・重複の扱い不足 | 測定§5 | ハッシュだけでは再取込/同一測定の再出力/独立測定を区別できない。原本の重複排除と測定の同一性を分離 |
| R25 | 中・工程の矛盾 | ロードマップ§3/4、全体§10 | 復元が実A/B比較完成に依存していた。DB直後に最小復元を行い、v0.2/v0.5の全機能をv1.0の必須工程にしない |

### 一次資料の再確認と設計判断

- R17は[REW File Menu](https://www.roomeqwizard.com/help/help_en-GB/html/file.html)の位相なし出力仕様に基づく。形式プロファイルも1測定1ファイル、数値形式・出力設定の記録へ具体化した。
- R18は[REW API — Frequency response](https://www.roomeqwizard.com/help/help_en-GB/html/api.html)のppo指定時の平滑化に基づく。対象ビルドの応答での検証はA03で実施する。
- R22のクリッピングによる応答誤差は[REW Making Measurements](https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html)に基づく。品質状態・A→B→A・繰返し差の扱いはHTDTの設計提案であり、統計的な有意差を保証するものではない。
- 版参照、固定グリッド、保存済み結果、添付管理、工程変更は、このプロジェクトの要件から導いた設計判断。外部製品の仕様と混同しない。

### 確認と残る制限

指摘はREADMEと設計4文書へ反映し、ロードマップのF12–F18と数値・復元受入条件へ結び付けた。文書6本の差分、内部リンク/見出し参照18件、M01–M10の依存関係に循環がないこと、96 PPOグリッドと一定レベル差の数式を確認した。parser・バックアップの実装や実機での精度は未検証である。

対象Windows、REWビルド、実測・出力サンプルは引き続き未確認。これは文書修正の未完了ではなく、P0-01〜P0-05と各追加機能の開始時に確認する事項である。

## 7. CAD-first追加レビュー（2026-09-16）

### 対象・方法

- main: [1b510206](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/commit/1b510206e7c2fe84ff15fe544a6e3fe2d896dc84)
- 実装track: [PR #37 / 初回9724f4b](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/tree/9724f4b84aa34b32a77a169b73ce68290de69fdd)、[追加確認0353768](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/tree/035376816c755f2917119a730c415c31f9ea0d6e)
- mainの計画・状況・幾何/データ契約、models/geometry/依存/CIとPRのspatial_editor/進捗文書を確認。レビュー中の追加commitについてnative_editor、run-native、依存pinも確認。
- 13のOSSで選定したソースをcommit固定で確認。コード・symbol・採否は[OSS調査](CAD_EDITOR_OSS_RESEARCH.md)。
- 今回は計画レビューと文書修正。native GUI実装、依存追加、Windows実機再検証は行っていない。

### 指摘と反映

| ID / 優先度 | 所見 | 反映 |
|---|---|---|
| C01 / 高 | PROJECT_PLANはReact/Three.js、native保留、3D後回しのまま。UI_DESIGNもbrowser画面とmobile受入が主仕様 | 両文書をCAD-firstへ改訂、正本の責務を統一 |
| C02 / 高 | IMPLEMENTATION_STATUSは次工程をO20と記載。実測待ちだけが主blockerに読める | N05先行、O20保留、実機測定trackを独立表記 |
| C03 / 高 | 初回headにはshell未掲載。追加0353768でshell/起動/依存pinが入ったが、編集とSave/UndoのGUI接続・package受入は未完 | 追加実装を認めてstatus更新。main/branch/報告/未確認を分離し、N05で通し受入 |
| C04 / 高 | 一測定点Contextを全Sceneへ拡張すると家具/複数席/測定との責務が混在 | SceneRevision、WorkingDocument、AcquisitionContext、ViewStateを分離 |
| C05 / 高 | Snapshot履歴のis_dirty=index!=0ではSave後clean/Undo分岐を表現できない | content hash、Save成功後clean、no-op、redo分岐、失敗/復旧仕様 |
| C06 / 高 | 壁を端点ID列だけで識別するとsplit/mergeで開口/制約が失われる | 安定wall ID、影響確認、制約revision、原子的Undo、G10対応表 |
| C07 / 高 | VTK transformをdomainの左手系へそのまま戻すと向き/面/左右を誤る | 一箇所の座標adapter、方向/法線/winding、非対称fixture |
| C08 / 高 | PyVista callbackは確認commitでmatrix更新より先、camera styleも変更する | HTDT transactionとadapter、最終値再取得、cancelとobserver解放 |
| C09 / 中 | snapがworld距離だけではzoom/DPIで操作感が変わる。入力競合・capture lossが未定義 | screen距離/hysteresis、優先順位、入力表、Esc/Alt+Tab/画面外release |
| C10 / 高 | 重い計算をthread外に出すだけでは、古い結果が編集済みsceneへ適用される | immutable job input、generation、取消/stale、GUI thread境界 |
| C11 / 中 | 保存復旧/配布がN90に集中し、N20のsnap範囲も過大 | N05の縦断package、N10復旧、N20a/b・N30a/b、独立可能なN50/N60 |
| C12 / 中 | 1000 entityで実用的等の評価が曖昧、Godotが無条件fallback | fixture/計測手順/目標値、原因別の代替比較。計測済みと表示しない |
| C13 / 中 | 多数のOSS名と一般URLだけでコード根拠・取得可否が追えない | 固定commit/path/symbol、確認範囲、license根拠、Sweet Home 3Dの404を訂正 |
| C14 / 中 | 旧API/domain契約維持やfeature parityが互換不要方針を弱める | 新schema開始可、必要なadapterだけ再利用、旧frontendの凍結/廃止条件 |

### 決定・未完事項

Qt/VTKは第一実装として維持する。選定理由を「WindowsなのでWebは不適」から、Python解析/科学可視化/操作を統合する費用の比較へ修正した。Godot/C#/TypeScriptも、N05/N20の未達原因に応じて比較できる。

新しい[編集契約](CAD_EDITOR_SPEC.md)と[受入仕様](CAD_EDITOR_ACCEPTANCE.md)は実装前仕様。初期数値目標・snap半径・配布候補はWindows受入で確認/調整する。旧PoCの版で今回のソース所見を再現したとは主張しない。

### この改訂の検証

14文書の内容整合、80のrelative link、milestone ID、旧方針の残存、28の固定commitソースリンク、変更範囲を確認した。文書のみのため新規testは追加していない。既存CIの結果とGitHub反映先はこの変更のPRに記録する。Windows実機gateは未実施。

このレビュー時点ではPR #37の継続を予定したが、その後のユーザー指示による旧Issue/PR整理で§8の後継Issueへ置換した。N05の選択→移動→取消/Undo→保存/reopen→packageという次工程は維持する。

## 8. 正本化と旧Issue/PRの整理（2026-09-16）

ユーザーの「今回のロードマップを正本にし、旧仕様のIssue/PRをclose」の指示に対応した。正本はmainの[IMPLEMENTATION_ROADMAP](IMPLEMENTATION_ROADMAP.md)、採択は[PR #40](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/pull/40)、commit `850cfa5cb6bc9ef21308246fd7091d28bdfa6b23`。

open一覧全件と各本文・コメントを確認。旧仕様はIssue 2件とDraft PR 1件だった。

| 対象 | 処理 | 理由 / 引継ぎ |
|---|---|---|
| [Issue #36](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/36) | closed / not_planned | Context中心の一括仕様をScene分離とN05先行へ置換 |
| [Issue #38](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/38) | closed / not_planned | 旧N10一括checklistと旧frontend受入を段階別gateへ置換 |
| [PR #37](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/pull/37) | closed / 未マージ | 旧Context保存・既存契約固定のscopeを終了。branchと0353768 snapshotは保存 |
| [Issue #41](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/issues/41) | open / 後継 | 正本のN05/A01/A02、仕様リンク、試作再利用候補を集約。実装は一時停止中 |

closeは実装完了を意味しない。各本文の先頭に置換理由・正本・後継を追記し、元の本文と履歴を残した。既にclosed/mergedの37件は履歴として維持した。旧branchやデータの削除、旧PRのマージ、アプリ実装再開は行っていない。

README、ロードマップ、実装状況の「PR #37を継続」の指示をIssue #41へ更新し、二重の正本が残らないようにした。今後のIssue/PRは正本を具体化する追跡票とし、仕様変更時は文書も同じPRで更新する。


## 9. Issue #101 arbitrary-room acoustics 再レビュー（2026-09-18）

### 対象

PR #105/#106反映後のIssue #101、`ACOUSTIC_SOLVER_RESEARCH_2026-09-18.md`、`IMPLEMENTATION_ROADMAP.md`、`PLACEMENT_OPTIMIZATION_ROADMAP.md`、`PROJECT_PLAN.md`、`IMPLEMENTATION_STATUS.md`、既存N70/O20〜O80 authorityを横断レビューした。RDC/実装作業は行っていない。

### 指摘と修正

| ID | 優先度 | 所見 | 修正 |
|---|---|---|---|
| ACR01 | 高 | R100で複雑fixtureを比較するのにR110/R120のauthorityが後続で、各solverが別問題を解く危険 | R100をR100A benchmark authority→R100B bakeoffへ分割 |
| ACR02 | 高 | openingを単なるwall holeとして扱うと外側境界条件が不定 | AcousticRegion / Portal / BoundaryTerminationをR100A/R110/R120へ追加 |
| ACR03 | 高 | source authorityに対しreceiver/calibration/timing authorityが弱い | ReceiverModel/calibrationと既存MicrophoneProfile/AcquisitionContext連携を追加 |
| ACR04 | 高 | scalar absorptionしかないmaterialをwave solverへどう扱うか不明 | measured impedance / parametric / rigid assumption / geometric-only / unknown capabilityを分離 |
| ACR05 | 高 | R130がcore discretizationとfrequency-dependent boundaryを一括しfailure isolation不能 | R130A rigid core → R130B simple lossy → R130C causal frequency-dependent boundaryへ分割 |
| ACR06 | 高 | hybridでwaveとGAのdirect/early成分を二重計上し得る | CoherentTransfer / DeterministicPathSet / LateEnergyDecayを型分離しdouble-counting防止を明記 |
| ACR07 | 中 | R100の性能比較と必須条件が混在 | hard pass/fail gate通過後のみperformance比較する契約へ修正 |
| ACR08 | 中 | cache/stale/cancel/provenanceがR140導入に見える | identity semanticsはR110/R130、R140はscheduler/resource最適化に限定 |
| ACR09 | 中 | candidate parallelism中心でsolve reuseの優先順位が弱い | receiver batching、source grouping、reciprocity、matrix/grid/BVH reuseをR170へ明記 |
| ACR10 | 中 | geometry hash一つではsemantic geometryとmesh/grid生成差を区別できない | semantic acoustic geometry hashとcompiled representation hashを分離 |
| ACR11 | 中 | environment stateがmeasurement validationに対して弱い | environment/air-state authorityをsource/receiverと同格に追加 |
| ACR12 | 高 | IMPLEMENTATION_STATUSが全software完了と読め、R-series未着手と矛盾 | v0.1/O-series完了、Issue #101 R-series未着手へ訂正 |

### 結果

数値方式の基本方針は変更しない。FDTDはfirst PoC、FEMは独立reference/alternative、BEM/DG/PSTD等はsecondary/referenceのまま。ただしproduction solver、final crossover、GPU API/vendor、mesh/grid preset、diffraction/late-field方式はR100B evidence前に固定しない。

次に実装を再開する場合の開始点はR100Aであり、solver kernelから先に書き始めない。

## 10. Issue #101 実装開始条件・数値比較・探索の追加レビュー（2026-09-18）

### 対象と結論

main `e8db17fc2399d171f9fed4d619540b5decc17310`（PR #107反映後）のIssue #101、研究文書、実装/最適化ロードマップ、CAD仕様、製品計画/実装状況をGitHubで確認。現行 `cad_scene.py` の単一RoomPrismとprediction modelの範囲も照合した。FDTD-first PoC・FEM独立reference・CPU baseline・O60 evidence gateは維持する。

### 指摘と修正

| ID | 優先度 | 所見と影響 | 修正・反映先 |
|---|---|---|---|
| ACR13 | 高 | 研究§14がR100 PoC/kernel開始を促し、R100A先行と矛盾。R100の比較項目に後続製品機能も混在 | §14をR100A manifest→R100B実行へ訂正。stage別evidence表、shipping/reference環境とGPU対象外を区別。研究§4D/§14、実装ロードマップ |
| ACR14 | 高 | mode誤差だけではFR/phase/decayを保証できず、無損失共振点を有限FRとして比較する危険 | 単位・励振・座標補間・dt/観測時間・処理・null mask・独立reference/tolerance契約。lossless modeとlossy/有限時間応答を区別。研究§4D/§12 |
| ACR15 | 高 | overlapが存在しない場合、経路のphase不足、IR tail不足の具体的停止条件がない | gapの保持、complex pathの必要条件、decay/clarityの適用/打切りfixture。研究§8、実装ロードマップ |
| ACR16 | 高 | 粗計算で真の良候補を落とし、異なるfidelityでPareto支配を誤判定し得る。物理cabinet移動時のreuse条件も不十分 | discrepancy/除外候補audit/順位逆転fixture、共通fidelity再評価、operator単位cache失効とimmutable run binding。研究§10、最適化§4.2/§4.3 |
| ACR17 | 高 | 現行単室CADにmaterial/隣接regionを入力する工程がなく、任意meshを扱えるsolverと製品入力範囲が混同される | R110に入力/保存/Undo/再open、R120にID対応を追加。初期prism範囲と未対応形状、hideとacoustic participation、cabinet/source結合を明記。研究§5、実装ロードマップ、CAD仕様§4 |
| ACR18 | 中 | resource計画がR140中心で、CPU PoCや全field時系列保存が上限なしになり得る | R100Bからmemory/output/duration上限、R110/R130取消、出力subsetとCPU fallback再見積り。研究§9、実装ロードマップ |

### 根拠と確認範囲

無損失共振の扱いは[COMSOL公式例](https://www.comsol.com/blogs/how-to-model-fundamental-sources-in-enclosed-spaces)、小室のRT60適用限界と処理条件は[REW公式説明](https://www.roomeqwizard.com/help/help/html/graph_rt60.html)を再確認した。その他のstage分割・入力工程・cache/screening契約は、このrepositoryの要件と既存authorityから導いた設計判断であり、実測済み性能を主張しない。

GitHubから取得した文書間のmilestone/参照整合と変更範囲を確認する文書限定改訂。ローカルclone・ファイル編集・実行・RDC利用なし。新test追加やsolver/実機benchmarkは行わない。R-series実装とowned-room validationは未着手のままで、次の実装開始点はR100A。

## 11. Issue #101 ゼロベースレビュー（2026-09-18）

### 判定と対象

Issue #101の目的・9要件・17 Acceptanceを起点に、main `e8db17f` と未マージPR #108 head `5da57b8` の計画を再評価した。前回ACR13–ACR18の結論を受入条件として固定せず、既存実装・一次資料・依存関係へ戻って照合した。

**修正前の計画は、そのまま実装開始する計画としては不十分。** 主因は、低域優先に対してhybrid完成まで実室feedbackが遅れる工程、既存O60への接続作業の欠落、自作kernelの事実上の先決めである。設計原則の追加だけでは直らないため、依存表・R170/R180の成果物・OSS採否手順を変更した。

維持する判断: wave/GAを分ける最終hybrid目標、CPU path、scalar吸音率からphaseを作らない契約、immutable provenance、O60独立実測gate。変更する判断: FDTD-firstは評価順に限定し、低域測定loopを広帯域完成から分離する。実行結果なしに特定solver・性能・実室精度を承認しない。

### 指摘と修正

| ID | 優先度 | 根拠・失敗シナリオ | 反映 |
|---|---|---|---|
| ACR19 | 高 | IssueはOSS優先なのにR100B成果物が新HTDT FDTD CPU prototypeを必須化。既存engine/adapterで足りる可能性を評価せず保守対象を増やす | R100Bは再利用→adapter/port→不足部分の自作。bounded workload/性能判定値をR100A成果物とし、採用またはno-go ADRで終了可能。研究§1/§4B/§4D/§12/§15、実装表 |
| ACR20 | 高 | R170がR140/R160に依存し、R180がR170待ち。低域modelの実室不適合を最終段階まで発見できない | R170A/R180Aを低域CPU batch/測定の縦断経路、R170B/R180Bをhybrid/拡張へ分割。低域合格でumbrellaをcloseしないcoverage表を追加 |
| ACR21 | 高 | O60 serviceはRoomSim attemptを取得し、campaignはFR objective 4種類のみ。新solverやIRを接続済み扱いすると型/証拠を偽装する | R170Aへtyped result providerと各O-series bindingを明記。既存recordを保持し、FR合格をphase/IR/decay/aimへ転用しない。研究§11、最適化§8.1a |
| ACR22 | 高 | room transferだけをREW実測と比較すると、AVR低音転送・source response・複数音源干渉・reference speakerの移動依存を壁の誤差と取り違える | physical sourceへのrouting/複素励振とroom transferを分離。single-source初期経路、相対/絶対比較条件、timing/mic二重補正negative fixture。研究§7、最適化§8.1a |
| ACR23 | 高 | Portalのidentityだけでは内部接続を吸収境界にしても通る。係数のenergy/amplitude・入射条件・sheet/wall違いでも同一問題にならない | 内部continuity、分割不変性・開閉・flux fixture、impedance単位/入射/backing、反射/吸収/透過/scattering収支。研究§5.2/§6 |
| ACR24 | 高 | calibration/holdout分離だけではfit後model固定や再調整によるholdout汚染を防げず、FR gateの有効範囲も広がり得る | preregistered fitting→append-only model freeze→holdoutの遷移、非一意fit、再利用holdout禁止、observable/band/config限定eligibility。研究§11、最適化§8.1a |

### 実装・一次資料との照合

- [現行O60 service](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/blob/5da57b8a5ce86b7c1d58f5a95c6cff13fa3b10cb/backend/src/htdt/cad_model_validation_service.py) の `roomsim_repository.get_attempt` / `roomsim_attempt_frequency_response` と、[campaign](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/blob/5da57b8a5ce86b7c1d58f5a95c6cff13fa3b10cb/backend/src/htdt/cad_validation_campaign.py) の `supported_objectives` を読んだ。一般solver adapterやphase/IR検証が実装済みとは判断していない。
- [PFFDTD公式](https://github.com/bsxfun/pffdtd): CPU経路とLinux前提を確認。Windowsへそのまま配布可能とは判断しない。
- [REW公式](https://www.roomeqwizard.com/help/help/html/makingmeasurements.html): 音響timing referenceが距離差に基づく点を確認。
- [COMSOL continuity](https://doc.comsol.com/6.3/doc/com.comsol.help.aco/aco_ug_pressure.05.096.html)、[interior impedance](https://doc.comsol.com/6.3/doc/com.comsol.help.aco/aco_ug_pressure.05.114.html)、[impedance](https://doc.comsol.com/6.4/doc/com.comsol.help.aco/aco_ug_pressure.05.023.html): 内部接続/transfer boundaryと係数・単位の区別を確認。具体的なHTDT fixture/許容値はR100Aの設計成果物とする。

### 完了範囲と残る判断

計画レビューと文書修正のみ。次工程は引き続きR100Aであり、数値gate、OSS Windows package、workload別時間/メモリ、実室calibration/holdoutは未実行。Issue #101のAcceptance対応表を実装ロードマップへ置き、低域先行と最終要求の縮小を混同しない。

全作業はGitHub APIによる取得・更新と一次資料参照で実施。ローカルclone・ファイル編集・コマンド実行・RDC・solver実装なし。

## 12. Issue #101 マージ後の整合性・要求網羅レビュー（2026-09-18）

### 対象と判定

PR #108がmergeされたmain `ee6eda595c2d206c19500c88cee6faeead12e480` とIssue #101を再取得して精査した。前回の低域先行・OSS優先・typed O60 adapter・独立holdout方針は維持する。残っていたbackend/output契約の矛盾と、一般3D/確率的GAの受入不足だけを修正する。

| ID | 優先度 | 現在の記述から生じる問題 | 修正 |
|---|---|---|---|
| ACR25 | 高 | FEMをproduction候補に残しながらR120はwave-grid必須・FEM mesh任意。採用方式を変えてもFDTD依存が残る | compilerをbackend capabilityに従属させ、FEMならvolume mesh等を必須化。R120A、研究§5.2/§12、実装表 |
| ACR26 | 高 | coherent FRとIRのcapabilityが曖昧。frequency-domain採用時に疎/非等間隔FRの単純IFFTでIR/decayを認め得る | 周波数grid/範囲・時間span・正規化/対称性・再構成法と遅延/減衰referenceを独立gate化。band-limited出力を明示。研究§8、最適化fixture |
| ACR27 | 高 | 任意3D目標に対しprism以外は「後続」のまま担当工程がない。一般形状を読み込めても既存XY制約だけでは天井超過candidateを通す | R120A prism→R120B general-3D input/compilerへ分割。段差/傾斜/曲面の面分割・誤差・保存を定義し、R170Bで3D volume/envelope feasibilityを接続。初期sliceでumbrellaを完了にしない |
| ACR28 | 中 | R150のseed repeatabilityだけではsampling bias/varianceやreceiver/time-binの未収束を検出できない | ray数・receiver estimator・time bin・打切りのrefinementと独立seed比較、到達数不足/候補差の不確実性を受入条件に追加 |

### 要求別の完了監査（計画文書として）

| Issue #101の要求 | 正本上の担当と確認結果 |
|---|---|
| §1 geometry / opening / simplification | R110、R120A/B。内部portal、termination、一般3D入力、compiled hash、診断と近似誤差を明記 |
| §2 / §2a surface/object material | 研究§5/§6、CAD仕様。preset/custom、出典/版、thin/volume、用途別capability、材料変更失効を保持 |
| §3 source / receiver / environment | 研究§7、R110、R170B。body/aim、励振/directivity、routing、mic/timing、environmentを保持 |
| §4 low wave / §5 geometric | R100A/B、R130A/B/C、R150。方式選定・独立reference・収束・CPU baseline、GA sampling受入を確認 |
| §6 hybrid / output | R160、研究§8。typed結果、overlap不足、double-counting防止に加えFR→IR再構成gateを確認 |
| §7 immutable prediction authority | R110/研究§5.1/§9/§10。semantic/compiled/material/source/receiver/environment/backend/数値条件を保存 |
| §8 optimization | R170A/B、最適化§4/§8。typed provider、multi-fidelity、reuse、候補失効、3D feasibilityと独立objectiveを確認 |
| §9 calibration / real validation | R180A/B、研究§11、最適化§8.1a。model freeze、非一意性、holdout、observable限定eligibilityを保持 |
| hardware / performance / Acceptance 1–17 | 実装ロードマップの対応表とR100A/R140/R170/R180。resource予算、oversubscription、fallback、cancel/stale、cache/resume、GPU対象外と未検証の区別を保持 |

これは計画の担当・依存・受入項目の監査であり、上記機能の実装完了や数値精度を証明するものではない。R120Bのgeometry/compiler gateを通してからR130/R150の数値gateへ進むため、相互依存の循環を作らない。低域の開始経路は引き続きR100A→R100B採用→R110/R120A→必要なR130→R170A→R180A。

### 検証と根拠

[COMSOL FFT solver](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_solver.36.132.html) のFFT/非等間隔変換の区別と、[pyroomacoustics Room API](https://pyroomacoustics.readthedocs.io/en/stable/pyroomacoustics.room.html) のray/receiver/histogram制御を参照。採否条件・一般3Dの工程分割はHTDT要求から導いた設計判断であり、library採用を保証しない。

GitHub上の文書・Issue・PRを対象に差分、相対参照、依存関係、要求の担当を確認する。ローカルclone・編集・コマンド実行・RDC・solver実装・実機検証は実施しない。
