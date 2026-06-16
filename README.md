# Auto Receipt

領収書管理アプリのMVPです。ユーザーは毎月、自分が登録しているサービスの領収書をアップロードし、確認後に提出できます。管理者は月別の提出状況を確認し、領収書ファイルと `manifest.csv` をZIPでまとめてダウンロードできます。

## 実装済み機能

- ユーザー登録・ログイン・ログアウト
- ユーザーごとの登録サービス管理
  - サブスク
  - 従量課金 / API
  - 一回払い
  - その他
- 月別の提出箱
- PDF / PNG / JPG / JPEG / WEBP の領収書アップロード
- 提出後の編集ロック
- ユーザー自身の提出履歴・提出詳細確認
- 管理者ダッシュボード
  - 月別の未着手 / 下書き / 提出済み確認
  - 月別ZIPダウンロード
  - ユーザー別ZIPダウンロード
  - ZIP内に監査用 `manifest.csv` を同梱
- アップロードファイルの最大3ヶ月保存
  - ファイル本体は自動削除
  - サービス名・金額・提出月などのメタデータは保持

## ローカル起動

```bash
cd auto_receipt
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

ブラウザで `http://127.0.0.1:8000/` を開きます。

## 期限切れファイル削除

ファイル本体はアップロード時刻から最大3ヶ月後に削除対象になります。手動実行は以下です。

```bash
python manage.py purge_expired_receipts --dry-run
python manage.py purge_expired_receipts --noinput
```

削除後も `Receipt` レコードは残り、提出月・ユーザー・サービス名・金額・元ファイル名・削除日時を確認できます。

## Railway デプロイ手順

1. GitHubにこのリポジトリをpushします。
2. Railwayで `Deploy from GitHub repo` を選びます。
3. PostgreSQLサービスを追加します。
4. アプリサービスに環境変数を設定します。
   - `SECRET_KEY`: Django用の長いランダム文字列
   - `DEBUG`: `False`
   - `ALLOWED_HOSTS`: Railwayのドメイン。例: `your-app.up.railway.app`。`healthcheck.railway.app` と `RAILWAY_PUBLIC_DOMAIN` はアプリ側で自動追加されます。
   - `CSRF_TRUSTED_ORIGINS`: 例: `https://your-app.up.railway.app`。`RAILWAY_PUBLIC_DOMAIN` がある場合は自動追加されます。
   - `DATABASE_URL`: Railway PostgreSQLの接続URL。Railway側で `PGHOST` などを使う構成でも動きます。
   - `RECEIPT_MEDIA_ROOT`: `/app/media`
   - `RECEIPT_RETENTION_MONTHS`: `3`
   - `ALLOW_SIGNUP`: 一般ユーザーの自己登録を許可するなら `True`。管理者がユーザーを作成する運用なら `False`
5. Railway Volumeをアプリサービスに追加し、マウントパスを `/app/media` にします。
6. ヘルスチェックパスは `railway.toml` で `/health/` に設定済みです。
7. 初回デプロイ後、Railway Shell等から管理者を作成します。

一般ユーザーが自分でアカウント作成する運用の場合は、Railwayの環境変数で `ALLOW_SIGNUP=True` を設定してください。`ALLOW_SIGNUP=False` の場合、登録リンクは非表示になり、`/accounts/register/` に直接アクセスしてもログイン画面へ戻ります。

```bash
python manage.py createsuperuser
```

## Railway ヘルスチェック

Railwayのヘルスチェック用に `/health/` を用意しています。このURLはログイン不要でHTTP 200を返し、DBクエリも実行しません。`/accounts/login/` はDjangoのホスト検証やHTTPSリダイレクトの影響を受ける可能性があるため、Railwayでは `/health/` を使います。

## Railway Cron設定

期限切れファイル削除は、同じGitHubリポジトリから2つ目のRailwayサービスを作り、Cron専用サービスとして動かす構成を想定しています。

- Start Command: `python manage.py purge_expired_receipts --noinput`
- Cron Schedule: `15 18 * * *`
- 共有する環境変数: DB接続情報、`RECEIPT_MEDIA_ROOT=/app/media`

`railway-cron.toml` はこのCronサービス用の設定例です。Railwayのサービスごとに設定ファイルを分ける場合は、この内容をCronサービス側に反映してください。

## GitHubへpushする例

```bash
git init
git add .
git commit -m "Initial Auto Receipt MVP"
git branch -M main
git remote add origin git@github.com:<your-account>/auto_receipt.git
git push -u origin main
```

## 注意点

MVPでは領収書ファイルをDjangoのローカルストレージに保存します。RailwayではVolumeを `/app/media` にマウントしてください。将来的にファイル数・容量が増える場合は、S3互換ストレージへの移行を推奨します。
