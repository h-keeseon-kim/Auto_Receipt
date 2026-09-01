# ReceiptHub v1.16.1 ビルド検証

## このパッケージ作成時に実行する検証

```bash
python -m unittest receipts.test_statement_matching -v
python -m compileall -q .
```

加えて、Django依存関係を導入できる環境では次を実行します。

```bash
python manage.py check
python manage.py test receipts.tests.FinalWorkflowAcceptanceTests.test_cross_month_support_receipts_are_not_reported_as_current_month_unused_files
python manage.py test receipts.tests
```

## v1.16.1の回帰条件

- 2026年7月明細に2026年6月28日の取引がある場合、6月領収書を照合候補として取得できること
- 6月領収書が実際の明細に一致する場合、その領収書を使用できること
- 照合に使われなかった別の6月領収書が、7月の「明細に紐づかなかった提出書類」へ表示されないこと
- 7月発行分として提出された未使用PDFは、同一覧へ引き続き表示されること
- 5月31日等の過去PDFを8月提出サイクルへ再アップロードしても、PDF本文の日付が古く実明細に無関係なら表示されないこと
- 管理メモで`照合候補PDF`・`明細未使用一覧の表示対象PDF`・`無関係な月跨ぎ補助候補PDF`が別件数として表示されること

## デプロイ環境

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py test receipts.test_statement_matching
python manage.py test receipts.tests
```

`receipts.0040_unmatched_receipt_event_scope`は、処理中・失敗中を除く既存明細へ再照合マーカーを付けます。明細PDFをOpenAIへ再送信せず、保存済み明細行と最新の領収書メタデータで未使用一覧を再構築します。
