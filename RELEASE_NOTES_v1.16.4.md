# ReceiptHub v1.16.4 リリースノート

## 修正対象

v1.16.3のPre-deployで、`receipts/urls.py`が参照する次のviewが
`receipts/views.py`から欠落していたため、Django URL checkが失敗していました。

- `staff_start_receipt_ai_processing`
- `staff_receipt_ai_status`
- `staff_preview_receipt`

最初の欠落で停止するためRailwayログには
`staff_start_receipt_ai_processing`だけが表示されますが、3つすべてを復元しています。

## 復元した動作

- 管理者確認画面から領収書1件だけAI検査を開始
- AI確認済み領収書の明示的な再解析
- 単票AI処理状況のポーリングとパネル更新
- 削除・再提出依頼済み領収書から適切な画面へのリダイレクト
- PDF・画像領収書の同一オリジンプレビュー

## Pre-deploy改善

v1.16.3では`manage.py migrate`が内部でURL checkを実行するため、同じコードエラーを
migration失敗として3回再試行していました。v1.16.4では次の順序に変更しました。

1. `python manage.py check`
2. `python manage.py migrate --noinput --skip-checks`
3. `python manage.py check`

DB接続などの一時的なmigration失敗だけを再試行し、URL/importエラーは最初のcheckで
即時停止します。`RAILWAY_SERVICE_NAME`が`receipt-reminder-*`のサービスでは、同一DBへ
複数サービスが同時にDDLを実行しないよう、既定でmigrationをスキップします。必要な場合は
`RECEIPTHUB_PREDEPLOY_MIGRATIONS=true`で明示的に上書きできます。

## DBへの影響

この修正では新しいDBマイグレーションを追加していません。既存の0041・0042をそのまま使用します。
