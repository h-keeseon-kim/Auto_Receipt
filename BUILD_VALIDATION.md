# ReceiptHub v1.16.2 ビルド検証

## パッケージ作成時の検証

```bash
PYTHONPATH=. python -m receipts.test_statement_matching
PYTHONPATH=. python -m receipts.test_receipt_component_identity
PYTHONPATH=. python -m receipts.test_plan_change_matching
python -m compileall -q .
```

結果：純Python回帰テスト44件成功、Python AST解析82ファイル成功、`compileall`成功。

## デプロイ環境で必須の検証

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py test receipts.test_statement_matching
python manage.py test receipts.test_receipt_component_identity
python manage.py test receipts.test_plan_change_matching
python manage.py test receipts.tests
```

このビルド環境にはDjango依存パッケージがなく、外部パッケージ取得もできないため、`manage.py check`とDBを使うDjango統合テストは未実行です。

## v1.16.2の受入条件

- 明細Aが構成要素「あ・い・う」を`consume`した後、別月の明細Bは同じフィンガープリントを候補へ使えない
- 同じ明細を再照合した場合は、自分の旧自動割当を解放して同じ構成要素を再取得できる
- 管理者確定の所有権は、自動照合や時系列の早い明細でも上書きしない
- 自動照合同士では、明細月・アップロード時刻・主キーが早い明細を所有者とする
- 先行明細が後続明細から構成要素を取り戻した場合、後続の計算グループ全体が未一致へ戻り再照合待ちになる
- 同じPDFを別名で再アップロードしても、SHA-256と金融署名により同じ構成要素として扱う
- AI再解析でローカルな構成要素名が変わっても、同一PDF・同一金融署名なら同じフィンガープリントになる
- Refund PDFの元決済だけが消費済みで返金が未使用なら、PDFは「一部使用済み」として残る
- Refund PDF内の全構成要素が消費済みなら、全明細未消費一覧へ表示しない
- `reference`は使用先を保存するが、後続の`consume`を妨げない
- 領収書またはカード明細の削除後、解放された所有権と全明細未消費スナップショットが再構築される
- 2026年7月の0383・0415・0424・0465・0466回帰ケースが維持される

## マイグレーション

```text
0041_global_component_usage_ledger
0042_global_component_usage_consistency
```

`0041`は台帳フィールド・一意制約・索引を追加します。`0042`は現在の正規化方式で既存フィンガープリントを再構築し、旧自動割当の重複を計算グループ単位で解放します。両マイグレーションは保存済み明細行を再照合待ちにするだけで、カード明細PDFをOpenAIへ再送信しません。
