# Railway Pre-deploy URL import fix — v1.16.4

## 発生したエラー

```text
AttributeError: module 'receipts.views' has no attribute
'staff_start_receipt_ai_processing'
```

`manage.py migrate`は実際のmigration処理前にDjango system checkを実行します。
URLconf import中に欠落したviewを参照したため、DB migrationへ到達する前に終了しました。

## 修正

`receipts/views.py`へ以下を復元しました。

- `staff_start_receipt_ai_processing(request, pk)`
- `staff_receipt_ai_status(request, pk)`
- `staff_preview_receipt(request, pk)`

`staff_start_ai_processing(request)`は月単位の一括処理用であり、`pk`を受け取る単票用endpointの
代替にはしていません。

## 再発防止

```bash
python scripts/check_url_view_contract.py
python manage.py check
```

前者はDjangoをインストールできないオフラインビルド環境でも、URLconfが参照する
`views.<name>`の欠落を検出します。後者がデプロイ環境での最終確認です。


## 複数Railwayサービス

同じリポジトリからWeb・4日リマインダー・10日リマインダーをデプロイしているため、
`preDeployCommand`も各サービスで実行されます。既定の`auto`モードでは
`RAILWAY_SERVICE_NAME=receipt-reminder-*`を検出してmigrationをスキップし、Webサービスだけが
DB schema migrationを実行します。

```text
RECEIPTHUB_PREDEPLOY_MIGRATIONS=auto   # 既定
RECEIPTHUB_PREDEPLOY_MIGRATIONS=true   # 強制実行
RECEIPTHUB_PREDEPLOY_MIGRATIONS=false  # 強制スキップ
```
