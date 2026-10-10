# #1078 選択配置を設置済みにする のクラッシュ修正

## 原因

`RoomTreatmentPanel._install_selected` が
`CadAcousticTreatmentRepository.latest_placement(instance_id)` に
`document_id` を余分に渡していた (#716 期の残存)。`TypeError` でクラッシュし、
配置は `proposed` のままだった。

## 修正

- `latest_placement(instance_id)` のみに (instance_id はグローバル一意のため
  document スコープ不要)。
- リグレッションテスト: 提案配置を seeded → 設置済み化で lifecycle=`installed` /
  version=2 / append-only 履歴保持を確認。未選択時は no-op。

Refs #1078
