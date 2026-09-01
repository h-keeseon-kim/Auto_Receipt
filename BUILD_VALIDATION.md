# ReceiptHub v1.16.0 ビルド検証

## このパッケージ作成時に実行済み

```bash
python -m unittest receipts.test_statement_matching -v
python -m compileall -q .
```

結果：純Python照合テスト20件成功、全Pythonファイルのコンパイル成功。

## デプロイ環境で必ず実行

このビルド環境にはDjango本体がインストールされていないため、以下はRailwayまたはローカルの依存関係導入済み環境で実行してください。

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py test receipts.test_statement_matching
python manage.py test receipts.tests
```

## 既存データ

`receipts.0039_cross_month_card_netting`は、処理中・失敗中を除く既存明細へ再照合マーカーを付けます。明細PDFをOpenAIへ再送信せず、保存済み明細行と最新の領収書構成要素で再照合します。
