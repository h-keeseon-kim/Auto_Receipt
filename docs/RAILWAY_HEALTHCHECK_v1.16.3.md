# Railway Healthcheck運用

## 症状

BuildとDeployは成功するが、Network > Healthcheckが約5分後に失敗する。

## v1.16.2の原因候補

`start.sh`が次の順番だったため、マイグレーション中はHTTPサーバーが存在しませんでした。

```text
manage.py migrate
↓
Gunicorn起動
↓
/health/応答
```

0041・0042は既存の証拠台帳を走査・再構築するため、DBの行数、ロック待ち、
接続遅延によってHealthcheck開始前の待ち時間が長くなる可能性があります。

## v1.16.3

```text
Railway pre-deploy container: manage.py migrate + manage.py check
↓
Web container: Gunicornを即時起動
↓
/health/応答
```

## 確認するログ

Pre-deploy logs:

```text
Running Django migrations in Railway pre-deploy phase...
ReceiptHub 0041: initialized ... evidence rows with bulk updates.
ReceiptHub 0042: examining ... evidence rows.
ReceiptHub 0042: global usage rebuild complete ...
Database migrations completed.
System check identified no issues ...
```

Deploy logs:

```text
Starting Gunicorn on PORT=...
Booting worker with pid: ...
GET /health/ 200
```

Pre-deployが失敗した場合、最初のPython tracebackまたはPostgreSQL errorが実原因です。
Healthcheck timeoutを延長して隠すのではなく、そのエラーを修正してください。
