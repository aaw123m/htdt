# 実装ステータス

> 更新: 2026-09-16 / N20a basic transform・Windows A05/A06受入反映
> 実装順は[ロードマップ](IMPLEMENTATION_ROADMAP.md)。旧UI/旧PoCをnative CAD完了とは扱わない。

## Native CADの状態

**N05 / N10 / N20aの技術gateは通過。** N05とN10はmainへ反映済み。N20aはIssue #47 / `feat/n20a-basic-transform-snap` で、body pose quaternion、Move/Rotate、Inspector数値編集、grid/angle snap、cancel/capture-loss耐性とA05/A06実機受入まで完了した。multi-selectやvertex/edge snapはまだ実装していない。

| 区分 | 現在の状態 |
|---|---|
| main | N10まで反映済み。PR #46 merge `a229a34e258eb0d13a43c9635dfebc9073ac362d` |
| N20a branch / Issue | `feat/n20a-basic-transform-snap` / Issue #47 |
| N20a実機受入 | Windows 11 / 実OS 200% DPIでA05/A06 pass |
| A05 | Top/Front/Side/PerspectiveでMove/Rotate、0.10 m grid snap、15° angle snap、Inspector、Undoをpass。unknown speaker aim保持 |
| A06 | Esc、window外release、Alt+Tab/deactivate、tool/view切替、capture lossをcancel。history/recoveryへ未確定値を残さず、その後Move/Rotate/Orbit可能 |
| 高DPI改善 | rotation ringのhit targetを拡大し、200% DPIの整数pixel丸めでも4 viewで実マウスpick/dragを確認 |
| capture loss | active preview中のみ40 msのQt mouse-grab watchdog。grab owner喪失時は共通cancel transactionへ流す |
| 自動検証 | N20a focused 18 tests pass。backend全回帰pass（既存1 skip、既知deprecation warningsのみ） |
| 次工程 | N20b — multi-select / common pivot / vertex-edge-midpoint-alignment snapと次の受入gate |

N05詳細は[N05 Windows acceptance](N05_ACCEPTANCE_2026-09-16.md)、N10詳細は[N10 Windows acceptance](N10_ACCEPTANCE_2026-09-16.md)、N20a詳細は[N20a Windows acceptance](N20A_ACCEPTANCE_2026-09-16.md)。N20a完了をroom sketchやmulti-select完成とは扱わない。

## 確定した実環境

| 項目 | 現在の値 | 実装上の扱い |
|---|---|---|
| OS | Windows 11 x64 | 正式な第一対象 |
| 開発/利用PC | 同一PC | サーバー運用・別PC配布をMVP要件にしない |
| AVR | Yamaha RX-A4A | AVR設定は不変スナップショットとして手入力から開始 |
| スピーカー構成 | 現在3.0.2 | 役割・本数は可変 |
| サブウーファー | なし | 現在の主要シナリオ。将来追加可能 |
| 測定マイク | miniDSP UMIK-1を採用。実機接続/serial登録は未実施 | 48 kHz、ホームシアター基準は天井向き+個体別90°校正。実機受入のみ保留 |
| REW | V5.40 beta 135 API版を所有PCへ導入済み | 実API接続・FRデコード確認済み。text export同一測定照合と実測受入は継続 |

## 中心目標

HTDTは未測定の状態で「最適位置」を断定せず、**配置を保存 → 測定 → 同条件再測定 → A/B比較 → 次の候補を試す**反復を再現可能にする。

## mainへ反映済み

- M01: Windows起動基盤、FastAPI、React/Vite、CI
- M01運用: loopback空きポート選択、ブラウザ自動起動、PowerShell一発起動
- M01運用: 同一データ領域の単一インスタンス排他。二重起動時は既存UIを再利用
- M02: REW周波数応答テキストparser、原本SHA-256
- M03: Project / Context revision / Measurement / Dataset / RawAsset / SQLite
- M04: 96 PPO、log2(f)補間、A−B、mean/RMS、level offset、shape RMS
- M05/M06: 部屋・MLP・可変スピーカー・RX-A4A条件、取込プレビュー
- Mic snapshot: ContextへUMIK-1 model/serial/48 kHz/calibration profileとマイク向きを不変保存
- Measurement readiness: 保存Contextと現在のREW input/calibration/sample rate/EXCL多ch/校正RawAssetをGET-only照合し、物理向きとroutingは手動確認として分離
- M07: Dataset/Context版を固定した比較履歴
- M08: DB + RawAsset ZIPバックアップ/復元
- M09: Plotly FRグラフと最小3D
- M10: Windows合成E2Eと実機受入手順
- M10補強: build済みfrontendをFastAPIから配信するWindows smoke test
- A01: 矩形室モードと6面の一次image-source反射候補。結果は`predicted_geometry_candidate`
- schema v2: 測定品質、再測定グループ、Raw添付、整合性検査、A/B confounder分離
- UI: quality / repeat_group / attachments / intended changes / confounders / interpretation warnings
- A03 backend: localhost限定・GET専用のREW 5.40系APIアダプター
- A03 UI: offline表示、測定一覧、PPO/unit/smoothing指定FRプレビュー、requested/returned来歴表示
- A03 snapshot import: REW measurementをGET-onlyで再取得し、stable summary確認後にcanonical JSON RawAsset + Measurement + Datasetとして保存
- A03 preflight: REW audio ready/driver/sample rate/Java input+output/input cal/EXCL候補/hardware ch/mappingをread-only表示
- A04: 96 PPO特徴検出、room mode/一次反射の候補対応、quality/evidence gate、UI
- R01: 保存済みComparisonから自己完結HTML/JSONレポート、inline SVG、UIダウンロード導線
- V01: 実測配置の比較一覧。channel/measurement point、移動量、品質、条件差、保存済み比較を横並び表示
- V02: schema移行前ZIP、移行後整合性検査、失敗時DB/RawAssetロールバック
- S01 backend: REW Room Simulator read-only state/FR契約、座標変換、実beta135 fixture。モデルは`rectangular_room_only`として明示
- G00: Contextへ任意頂点polygon-prismをexact room geometryとして不変保存。8頂点/凹形状、wall edge ID、polygon内外判定、3D境界表示を実装
- G10: schema v4 ConstraintSet不変保存、allowed/exclusion、wall/cabinet/pair clearance、axis/movement、左右連動、reject理由、評価UIを実装
- O10: schema v5 SearchSpec不変保存、deterministic grid、linked master→slave、G10 hard gate、candidate identity/hash、ページング、visual Search UIを実装

## G00 — Room Geometry v2

非矩形実室をreference boxと分離し、`geometry_kind=polygon_prism`としてordered polygon + 一定天井高をContext revisionへ保存できる。

- 各頂点に一意な`vertex_id`を持たせ、隣接頂点から安定したwall edge IDを導出。
- Shapely 2.1.2でsimple polygon validityと境界込み`covers`を使用。
- 自己交差、重複頂点/ID、非有限座標、reference box外への突出を拒否。
- speaker/MLPがreference box内でもpolygon外ならContext保存を拒否。
- `GET /api/projects/{project_id}/contexts/{context_id}/geometry`で面積、周長、bounds、凸/凹、wall edgesを返す。
- UIで`vertex_id,x,y` ordered verticesを入力/複製し、3Dでpolygon-prism境界を表示。
- A01矩形room mode/6面反射はpolygon/reference-box Contextでは実行不可。

詳細契約は[Room Geometry contract](ROOM_GEOMETRY.md)を参照する。家具・通路・壁離隔・筐体寸法等はG10の別ConstraintSetで扱う。

## G10 — Placement Constraint Engine

G00のexact footprint上で、物理的に設置不能な候補を音響予測前に除外するhard feasibility engineを実装済み。

- schema v4 `constraint_sets`: Context revision固定、更新/deleteなし、canonical spec SHA-256を保存。
- allowed regionは単一polygonまたは非連結multipolygon、exclusionは独立constraint IDで複数保存可能。
- cabinet footprint radius + safety marginをroom/allowed/exclusion/wall/pair判定へ反映。
- wall edge min/max clearance、XYZ fixed/range、現在位置からのmovement budgetを判定。
- pair distanceはcenterまたはenvelope clearanceを選択可能。
- linked placementはmirror X（任意mirror axis対応）、equal X/Y/Z、equal delta X/Y/Zを判定。
- unknown entity/position、reference-box-only geometryはpass扱いにせず拒否。
- rejectごとにconstraint ID、entity、実値、閾値を返す。
- UIでConstraintSetをguided作成し、上面図上でContext基準→候補の移動とreject対象を可視化しながらfeasible/rejected理由を確認可能。
- backup/restoreとschema v3→v4 pre-migration backupをテスト。

候補の格子生成、刻み、linked master→slave生成、候補集合の再生成はO10で実装済み。G10の詳細は[Placement Constraint contract](PLACEMENT_CONSTRAINTS.md)を参照する。

## O10 — Search Space

G10 hard feasibilityを満たす配置候補集合を、immutable SearchSpecから決定論的に再生成できる。

- schema v5 `search_specs`: Context revision / ConstraintSet ID / ConstraintSet SHA-256へ固定し、canonical SearchSpec SHA-256を保存。
- `search-space-grid-1`: entity別X/Y/Z min/max/stepのdecimal格子を固定順で列挙。
- mirror/equal/equal-delta linked placementをmaster→slaveとして導出し、その後G10で再評価。
- raw candidate countを生成前に見積り、candidate limit超過は部分実行せず拒否。system上限50,000。
- hard rejected / duplicate / feasibleを別集計し、constraint ID別reject countを保持。
- SearchSpec SHA + candidate座標からcandidate ID、candidate ID列からcandidate set SHAを決定論的に生成。
- 候補座標はoffset/limitでページングし、全体件数/hashはページに依存しない。
- backup/restore後の完全再生成、SearchSpec/ConstraintSet hash不整合時の停止をテスト。
- UIは可動軸、刻み、連動、候補数preview、immutable保存、候補雲、候補XYZ、保存版の複製編集をvisual操作できる。

詳細契約は[O10 Search Space contract](SEARCH_SPACE.md)を参照する。音響予測・ランキング・Pareto評価はまだ行わない。

## A03 — 読取専用REW API / UI

REW 5.40系の公式API仕様を対象に、任意のGET専用アダプターとUIを実装済み。

- 既定 `http://127.0.0.1:4735`、localhost以外を拒否
- `GET /measurements` と measurement UUID参照
- `GET /measurements/{uuid}/frequency-response`
- 32-bit float Base64をbig-endianで復号
- `startFreq + ppo` または`startFreq + freqStep`から周波数軸を復元
- requested PPO/unit/smoothingとREW返却PPO/unit/smoothingを分離
- 測定一覧のlist/object-keyed形状を正規化
- 壊れたBase64、NaN/Inf、空配列、spacing欠損、phase長不一致を拒否
- REW未起動時はofflineを正常状態として扱い、保存済みHTDTデータは通常利用可能
- UIから測定一覧とFRをGETし、log周波数軸の軽量SVGでプレビュー
- 選択曲線は明示操作でHTDT Measurement/Datasetへsnapshot保存できる。保存時はpreviewを流用せず、UUID一意性とbefore/after summary一致を再確認する
- RawAssetはmeasurement summary/query/REW frequency-response raw JSON（Base64列を保持）をcanonical UTF-8 JSON化し、同一原本はSHA-256でdedupする一方Measurement/Datasetは自動統合しない
- API取得だけでは`measured` / `usable` / routing `verified`へ自動昇格せず、既定は`unknown`
- REWへのPOST/PUT/DELETE、Generator、測定開始、設定変更は実装しない

所有PC上のREW V5.40 beta 135へ実接続し、合成FR 958点のGET・big-endian復号・96 PPO周波数軸復元に加え、REW API snapshot保存、REW停止後の保存Dataset比較、RawAsset入りbackup/restoreまで確認済み。同一measurementのREW text exportとの照合と実測データでの受入は継続する。

## S01 — REW Room Simulator契約 / 非矩形方針

REW V5.40 beta 135の実OpenAPIと所有PC実機で、Room Simulator APIを確認した。REW公式仕様上、Room Simulatorはrectangular room用であり、8頂点などの非矩形実室をexactに表現できない。

- 実機state: 5.0 × 4.0 × 2.4 m、座標は`fromRear/fromLeft/fromFloor`。
- HTDT座標変換: `x=fromLeft`, `y=room_length-fromRear`, `z=fromFloor`。
- 実機FR: 20 Hz開始、192 PPO、751 points、SPL、smoothing `None`、phaseあり。
- 同一stateで5回連続GETしたraw FR JSON/Base64は完全一致。
- 検証専用にhead位置を1 cm動かして即時復元したところFRは変化し、復元後はRoom Sim全state・FR・measurement一覧が完全一致した。
- HTDT本体のS01初期APIはGET-onlyとし、将来batch駆動はstate snapshot/apply/read/restoreの復元保証を別PRで実装する。

非矩形実室はG00でpolygon-prismを正本化する。REW Room Simulator結果は非矩形Contextに対して`rectangular_approximation`とし、exact predictionや自動推薦の根拠へ自動昇格しない。

## A04 — ピーク/ディップと幾何候補対応

周波数応答をHTDT内部の96 PPO / log2補間へ再標本化し、固定パラメータを返す特徴検出を実装済み。

- 既定評価帯域: 20–300 Hz（API/UIで変更可能）
- baseline: log周波数上の移動平均、既定1/3 octave幅
- feature threshold: baselineからの偏差3 dB以上
- 最小feature間隔: 1/12 octave
- mode / reflection候補との照合距離: log2周波数距離、既定1/12 octave以内
- room modeはpeak/dip双方の候補、一次反射の`first_destructive_hz`はdipのみの候補
- 結果分類は`candidate_association_not_causal_diagnosis`。近接しても原因確定とは扱わない
- `invalid`測定と非`measured`データでは自動候補照合を無効化
- `warning` / `unknown`品質は候補照合を許すが、手動確認が必要な警告を返す
- 検出・照合のPPO、帯域、prominence、baseline幅、最小間隔、照合許容差、音速、算法版を結果に保持

## 保存・復旧・Windows受入

- V02: 旧schemaを開く前にDB＋assetsをpre-migration ZIPへ退避し、移行失敗または移行後整合性失敗時に旧状態へロールバックする。
- M10: Project→R1→測定/再測定→Raw添付→R2→A/B→report→再open→backup→別フォルダrestore→integrityをWindows合成E2Eで固定。
- built-app smoke: Vite build後、FastAPIから`index.html`、実JS bundle、`/api/health`、SPA fallbackが取得できることをWindows CIで確認する。
- launcher: CLI、PowerShell構文、空きポートfallback、単一インスタンス排他をWindows CIで確認する。

## 現時点でハードウェア/実データ待ちの項目

- 実REW安定版テキストとのparser互換性
- REW text exportとAPI取得を同一measurementで数値照合する実互換性確認
- UMIK-1実機接続、個体別90°校正ファイル、絶対SPL
- RX-A4A HDMIチャンネル割当と実際の発音源確認
- 高さチャンネルをREWから個別励振できるかの確認
- 実部屋での同条件再測定ばらつき
- A04の閾値/prominenceが実部屋で実用的か
- V01を複数の実配置・実測で使った操作性
- M10の実データ通し確認

## 測定側の実機ゲート（CAD実装とは独立）

1. 導入済みREWで、FL/FRの同条件repeatを含む実測テキストexportを取得する。
2. UMIK-1導入後、48 kHz、天井向き、個体別90°校正ファイル、Windows/REW入力経路を記録する。
3. RX-A4AでFL/FR/C/Heightの実発音経路を確認し、input roleと実音源を分離して記録する。
4. `docs/WINDOWS_ACCEPTANCE.md`の実機最終受入を実行する。
5. UMIK-1実測取得後、REW API snapshotとGUI text exportを同一measurement・同一spacing/smoothing条件で照合する。
6. IR/ETCは実IRサンプル取得後にA02として開始する。

上記は測定機能側の未完項目。マイク/実データ待ちでもnative CADのN05〜N40はsample fixtureで進められる。

## 探索トラックの次段階

O10のソフトウェア実装はmainにある。**O20の先行拡張は保留し、まずN05〜N40のCAD基盤を進める。** 以下は再開時の算法契約。N70/N80とO系の依存は[ロードマップ](IMPLEMENTATION_ROADMAP.md)に従う。

- O10 candidate setをprediction batchの入力として固定する。
- PredictionRunは実測Measurementと別分類・別来歴で保存する。
- 中断/再開と同一入力からの再現性を先に実装し、順位付けはまだ行わない。
- REW Room Simulatorは矩形専用なので、8頂点実室では`rectangular_approximation`を維持する。
- 非矩形exact predictorはS03で独立評価し、実測validation前に自動推薦へ昇格させない。
- O30/O40までは単一scalar scoreや「最適位置」を導入しない。
