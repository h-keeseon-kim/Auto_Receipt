# v1.16.5 ビルド検証



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
