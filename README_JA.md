# ReceiptHub v1.16.4 ログ出力診断パッチ

## 目的と適用対象

`GET /staff/card-statements/ ... 500`というアクセスログだけで、
Pythonの例外詳細が表示されない状態に対する診断用の修正です。
**500エラーの原因となる照合処理そのものは、このパッチでは修正しません。**

対象はGitHubで確認したv1.16.4の `auto_receipt/settings.py` です。
元ファイルのGit blob SHA: `b808340fa1acd57010445f1f92e19781504d6450`

## 適用

1. 既存の `auto_receipt/settings.py` をバックアップします。
2. このフォルダー内の `auto_receipt/settings.py` を、同じ相対パスへ上書きします。
3. GitHubへ変更を反映し、RailwayのWeb本体 `Auto_Receipt` を再デプロイします。

独自にsettings.pyを編集している場合は、上書きせず `logging_only.diff` の追加部分を反映してください。
既に独自の `LOGGING` がある場合は、辞書を丸ごと置き換えず統合してください。
**Railwayの `DEBUG=False` はそのまま維持してください。**
DBマイグレーション、領収書の削除、再アップロードは、このログ追加のためには不要です。
このパッチはGitHub・Railwayへ自動適用するものではありません。

## 再現後のログ

管理者として「ご利用代金明細」を一度開き、Web本体のDeploy Logsで
`Internal Server Error: /staff/card-statements/` と直後のTracebackを確認します。
Traceback開始から最後の例外名・例外メッセージまでが調査対象です。

この設定はHTTPレスポンスをデバッグ画面に変えず、ERRORログをstderrへ出します。
リクエスト本文やCookie、設定値、ローカル変数をフォーマッターから一括出力しません。
ただし例外メッセージ自体に個人情報やDB値が含まれる場合があるので、共有前に確認してください。

## 検証の範囲

Python構文と標準loggingによる例外出力を確認しました。
Django依存パッケージをこの環境で取得できなかったため、DjangoのHTTP処理を通した
統合テストとRailway本番環境での再現確認は未実施です。
詳細は `VALIDATION.json` を参照してください。

## 参考

- Django 5.2 logging: https://docs.djangoproject.com/en/5.2/ref/logging/
- Django logging configuration: https://docs.djangoproject.com/en/5.2/topics/logging/
- Railway logs: https://docs.railway.com/observability/logs
