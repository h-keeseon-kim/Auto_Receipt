# Auto Receipt

領収書管理アプリのMVPです。ユーザーは毎月、管理者が自分に登録した利用サービスの領収書をアップロードし、確認後に提出できます。管理者はユーザー発行、ユーザー別サービス登録、月別の提出状況確認、領収書ファイルと `manifest.csv` のZIP一括ダウンロードを行えます。

## 実装済み機能

- ユーザー登録・ログイン・ログアウト
- 管理者による一般ユーザー発行
  - アカウント名はメールアドレス形式
  - 初期パスワードをランダム生成
  - 初回ログイン時にパスワード変更を強制
- 管理者によるユーザー別の利用サービス登録
  - サブスク
  - 従量課金 / API
  - 一回払い
  - その他
  - 停止中のサービスはユーザーの選択肢から除外
- ユーザーは管理者が登録した利用中サービスだけを選択可能
- 月別の提出箱
- PDF / PNG / JPG / JPEG / WEBP の領収書アップロード
- 提出後の編集ロック
- ユーザー自身の提出履歴・提出詳細確認
- 管理者ダッシュボード
  - 一般ユーザーの作成と初期パスワード生成
  - ユーザーごとの利用サービス登録・編集・停止・再開
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

## 管理者によるユーザー発行

一般ユーザーを管理者が発行する運用の場合は、Railwayの環境変数で `ALLOW_SIGNUP=False` を推奨します。

1. 管理者アカウントでログインします。
2. 画面上部の「管理者」から管理者ダッシュボードへ進みます。
3. 「新規ユーザー発行」を開きます。
4. 新しく登録するユーザー名としてメールアドレスを入力します。
5. 「パスワード生成」を押します。
6. 画面に表示されたアカウント名と初期パスワードを対象ユーザーへ伝えます。
7. ユーザーは初回ログイン後、強制的にパスワード変更ページへ移動します。
8. パスワード変更後に領収書管理機能を利用できます。

初期パスワードはハッシュ化して保存されるため、後から管理者が再確認することはできません。表示されたタイミングで必ず控えてください。

## 管理者による利用サービス登録

ユーザーは自分でサービスを追加・編集できません。領収書アップロード時に選択できるサービスは、管理者がユーザーごとに登録した「利用中」サービスだけです。

1. 管理者アカウントでログインします。
2. 画面上部の「サービス管理」、または管理者ダッシュボードの「利用サービス管理」を開きます。
3. 対象ユーザーの「このユーザーを管理」を押します。
4. サービス名、支払い種別、メモを入力して登録します。
5. 登録後、対象ユーザーのアップロード画面にそのサービスが表示されます。

サービスを停止すると、新規アップロード時の選択肢から外れます。過去に提出済みの領収書には、提出時点のサービス名・支払い種別がスナップショットとして残ります。

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
