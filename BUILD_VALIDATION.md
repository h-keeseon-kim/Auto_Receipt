# ReceiptHub v1.16.3 ビルド検証

## パッケージ作成時に実行した検証

```bash
bash -n start.sh
bash -n predeploy.sh
python -c 'import tomllib; tomllib.load(open("railway.toml", "rb"))'
PYTHONPATH=. python -m receipts.test_statement_matching
PYTHONPATH=. python -m receipts.test_receipt_component_identity
PYTHONPATH=. python -m receipts.test_plan_change_matching
python -m compileall -q .
```

結果：

- Railway TOML解析成功
- Shell構文検査成功
- 純Python回帰テスト44件成功
- Python AST解析82ファイル成功
- `compileall`成功

## Railway起動構成の検査

- `railway.toml`に `/app/predeploy.sh` を `preDeployCommand`として設定
- `/app/start.sh`から`manage.py migrate`を除去
- `/app/start.sh`は`0.0.0.0:${PORT}`へGunicornをbind
- Healthcheck pathは`/health/`
- Django側で`healthcheck.railway.app`を`ALLOWED_HOSTS`へ追加済み
- HealthcheckはDBクエリを行わずJSON 200を返す

## マイグレーション最適化

- 0041・0042の証拠行更新を、1行ずつのUPDATEから`bulk_update`へ変更
- 進捗件数をPre-deploy logsへ出力
- 既存のスキーマ・照合仕様・一意制約は変更なし

## デプロイ環境で確認すること

Pre-deploy logsに以下が出ることを確認します。

```text
Running Django migrations in Railway pre-deploy phase...
Database migrations completed.
Running Django system checks...
System check identified no issues
```

Deploy logsに以下が出ることを確認します。

```text
Starting Gunicorn on PORT=...
Booting worker with pid: ...
GET /health/ 200
```

この作業環境では外部パッケージを取得できないため、Django本体と本番PostgreSQLを使う統合テストは未実行です。
