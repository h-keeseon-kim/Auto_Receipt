# ReceiptHub v1.16.4 ビルド検証

## 今回の回帰原因

v1.16.3では`receipts/urls.py`が参照する次の3ビューが`receipts/views.py`から欠落していました。

- `staff_start_receipt_ai_processing`
- `staff_receipt_ai_status`
- `staff_preview_receipt`

Railwayでは最初の欠落でURLconf importが停止したため、ログには
`staff_start_receipt_ai_processing`だけが表示されました。

## パッケージ作成時に実行した検証

```bash
python scripts/check_url_view_contract.py
bash -n start.sh
bash -n predeploy.sh
python -c 'import tomllib; tomllib.load(open("railway.toml", "rb"))'
PYTHONPATH=. python -m receipts.test_statement_matching
PYTHONPATH=. python -m receipts.test_receipt_component_identity
PYTHONPATH=. python -m receipts.test_plan_change_matching
python -m compileall -q .
```

結果：

- URL/view contract：53 endpoint targetすべて解決
- Railway TOML解析成功
- Shell構文検査成功
- 純Python回帰テスト44件成功
- Python AST解析83ファイル成功
- `compileall`成功
- 復元した3ビューはv1.15.5の動作実装と一致

## Pre-deploy順序

```text
python manage.py check
↓
python manage.py migrate --noinput --skip-checks
↓
python manage.py check
```

URL/importエラーはmigration再試行へ入る前に即時停止します。DB接続などの一時的な
migration失敗だけを最大3回再試行します。`RAILWAY_SERVICE_NAME=receipt-reminder-*`では
既定でmigrationをスキップし、Webサービスとの並列DDLを防止します。

## デプロイ環境で確認すること

Pre-deploy logs：

```text
Running Django system checks before migrations...
System check identified no issues
Running Django migrations in Railway pre-deploy phase...
Database migrations completed.
Running Django system checks after migrations...
System check identified no issues
```

Deploy logs：

```text
Starting Gunicorn on PORT=...
Booting worker with pid: ...
GET /health/ 200
```

## 未実行

このビルド環境にはDjango依存パッケージと本番PostgreSQL接続がないため、以下は
RailwayのPre-deploy環境で実行されます。

```bash
python manage.py check
python manage.py migrate --noinput --skip-checks
python manage.py check
```

新しいDBマイグレーションはありません。既存の0041・0042を継続使用します。


## Reminderサービスのmigration所有権

```text
RECEIPTHUB_PREDEPLOY_MIGRATIONS=auto   # Webで実行、receipt-reminder-*ではスキップ
RECEIPTHUB_PREDEPLOY_MIGRATIONS=true   # サービス名に関係なく実行
RECEIPTHUB_PREDEPLOY_MIGRATIONS=false  # サービス名に関係なくスキップ
```

シェル検証ではWeb相当・reminder相当の分岐を静的に確認しています。
