# REV59-DRAWPROF レビュー記録 — CEB23 クロスウォーク・図面シンボル・字幕表示権威

対象 issue: #741 (P2), #742 (P2), #733 (P2)
着地: schema v61、新規 7 テーブル、リポジトリ `cad_presentation_profile_repository`、回帰テスト `test_rev59_drawprof.py` 30 件

## 実装権威

### #741 `cad_ht_video_profile.py`
- `HomeTheaterVideoDesignProfile` (htvdp-): 規格文書+版を pin（保護条文は埋め込まない）、要件→HTDT 権威の写像を宣言（screen_size_vs_seating/resolution_viewing_distance/image_angular_extent/masking_shape_mismatch/room_lighting/room_surface_color）
- `CEB23Evaluation` (ceb23-): 要件ごとの証拠クラスを pin
- `evaluate_ceb23_requirement`: 未写像要件・プロファイル外キーを拒否、design_prediction は as_built を名乗らない

### #742 `cad_drawing_symbols.py`
- `ArchitecturalDrawingSymbolProfile` (ads-): J-STD-710-2015 現行と 202x BSR ドラフトを別身元として pin。rights_provenance: licensed_pack は権利証拠 ref 必須、'unknown' は fail-closed（AVIXA はソフトウェア組込に連絡要）
- `DeviceSymbolMapping` (dsm-): device_kind→symbol+revision
- `DrawingExportRecord` (dexp-): 使用した profile+mapping を pin
- `evaluate_symbol_claim`: 権利未宣言→unlicensed、版不一致→revision_mismatch、未割当→unknown_symbol

### #733 `cad_timed_text.py`
- `TimedTextPresentationProfile` (ttp-): IMSC Text 1.3/CTA-708-E/TTML2 等の profile kind + 実マスク面を pin（'unknown' kind 拒否）
- `CaptionRenderObservation` (cro-): timing/mask 内位置/legibility/language を独立 pin
- `evaluate_subtitle_claim`: トラック decode≠表示、timing・マスク内・可読性を個別ゲート。文献: W3C IMSC Text 1.3（2026-05-21 Rec）、CTA-708-E S-2023

## 検証
- `test_rev59_drawprof.py` 30 テスト + fresh-migrate・roundtrip・tamper
- 登録面: NATIVE_SCHEMA_TABLES + DDL 7 + `_migrate_60_to_61` + `_ROW_BINDINGS` 7 + `presentation_profile` ブランチ + `_ReplayProbe`×7 + labels + JA 行 + manifest 3 issue

## 残件
- 実要件写像の運用登録、実図面 export 配線、実プレイヤー観測は残件
