# ReceiptHub v1.16.5 — PostgreSQL外部結合ロックの修正

## 修正対象

`GET /staff/card-statements/` が次の例外でHTTP 500になる問題。

```
django.db.utils.NotSupportedError:
FOR UPDATE cannot be applied to the nullable side of an outer join
```

v1.16.4の本番処理で変更したファイルは `receipts/statement_processing.py` のみ。
次の2関数で、nullableな `receipt` の取得をロック対象SQLの外部結合から分離する。

- `_backfill_missing_evidence_fingerprints()`
- `_partition_global_consume_ownership()`

```python
# 修正後の取得構造（既存filter/order_byは変更なし）
CardStatementReceiptEvidence.objects.select_for_update() \
    .select_related("statement_item__statement") \
    .prefetch_related("receipt")
```

`select_for_update()`は削除していない。Evidence/Item/Statementの既存ロック、
トランザクション、一意制約、全期間のconsume使用制限は維持する。
`receipt__isnull=False`で履歴を除外する変更もしていない。領収書本体が削除済みでも
使用履歴は照合対象に残る。既存の金額・日付・所有権・返金相殺ロジックは変更しない。

新しいマイグレーションはない。既存の0041/0042を変更しない。
前回のlogging設定と復元済みauto_receiptの起動ファイルはフルソースに含める。
Web/CronのRailway設定、SMTP設定、Healthcheck設定には変更を加えない。

## 適用

最小修正は既存 `receipts/statement_processing.py` **1ファイルだけ**を差し替える。
`receipts`フォルダー自体を削除/置換しない。他のファイル・設定・DB・保存PDFは残す。
ZIPの部分フォルダーで既存フォルダー全体を置換しない。

フルソースを使う場合も、既存.env、DB、media、Railway Variables/Volumeは保持する。
アプリコードをGitHubへ反映し、Web本体Auto_Receiptを再デプロイする。
既存predeploy.shがsystem checkと未適用migrationを実行する。
今回用のDB初期化、制約削除、migrate --fakeは不要。

デプロイ後、管理者で「ご利用代金明細」を開き、対象のFOR UPDATE例外が消えたか確認する。
この修正の適用確認のためにカード明細・領収書を再アップロードする必要はない。
再照合が動いた場合、既存ロジックどおり保存済み証拠の再割当が行われる。

## 検証の範囲

ビルド環境で実行したもの:
- 既存の純Python回帰テスト44件。
- 新規のクエリ構造契約テスト8件（AST/テストダブル、SQL実行ではない）。
- Python構文解析、compileall、URL参照先定義、Shell/TOML構文、ZIP CRC/展開検査。

本ビルド環境にはDjango/PostgreSQLがなく、依存パッケージ取得もできないため、
Django HTTP統合テストと実PostgreSQLのロック試験は未実行。
`receipts/test_statement_locking_db.py` に6件のDBテストを収録。
これには旧クエリでの例外再現、nullable Receiptの保持、別接続によるNOWAITロック確認を含む。
SQLiteではPostgreSQL専用2件をskipする。SQLite成功はPostgreSQL検証の代わりではない。

使い捨てPostgreSQLを接続したステージング環境で:

```bash
python manage.py check
python manage.py test receipts.test_statement_locking_db -v 2
python manage.py test receipts.tests.FinalWorkflowAcceptanceTests -v 2
```

DjangoテストはテストDB作成権限が必要。本番DBを直接操作する診断スクリプトではない。

## 参照

- 確認したGitHubコミット: a248c994e66c269e815c1db478c2966f007b6956
- 修正前statement_processing.py blob: 1fa560903b1465954c59983f95235cf33f558e39
- Django公式: https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update
