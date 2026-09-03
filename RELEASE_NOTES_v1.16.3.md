# ReceiptHub v1.16.3 リリースノート

## Railway Healthcheck hotfix

v1.16.2では `start.sh` がデータベースマイグレーションを完了してから
Gunicornを起動していました。0041・0042は既存の照合履歴を再構築する
データマイグレーションのため、DB件数やネットワーク遅延によっては
RailwayのHTTP Healthcheckが開始されるまで5分を超える可能性がありました。

v1.16.3では以下へ変更します。

- `python manage.py migrate` を Railway `preDeployCommand` へ移動
- Webコンテナの `start.sh` はGunicornを直ちに起動
- マイグレーション失敗時はNetwork/HealthcheckではなくPre-deploy段階で失敗させ、ログを明確化
- 0041・0042の行単位UPDATEを `bulk_update` へ置換し、DB往復回数を削減
- `/health/`、`healthcheck.railway.app`、`0.0.0.0:$PORT` の既存設定は維持

## デプロイフロー

1. Docker image build
2. `/app/predeploy.sh`
   - DB migration
   - Django system check
3. `/app/start.sh`
   - Gunicorn起動
4. Railway `/health/` check

マイグレーションが未適用の場合はPre-deployに時間がかかることがありますが、
その時間はWeb Healthcheckの300秒枠を消費しません。
