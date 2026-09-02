# ReceiptHub v1.16.2 リリースノート

## 目的

同じ領収書または返金PDF内の取引構成要素が、前月の明細Aと翌月の明細Bの両方で金額計算に再利用される問題を防止します。

## 主要仕様

- 金額計算への使用は`consume`として全明細共通で1回だけ許可
- 返品元確認などは`reference`として複数回参照可能
- PDF内の元決済と返金を別構成要素として管理
- 同じPDFを別名で再アップロードしても安定フィンガープリントで重複判定
- 管理者確定を最優先し、自動照合同士は古い明細を優先
- 部分使用済みPDFは、使用済み要素と未使用要素を画面に分けて表示

## デプロイ

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py test receipts.test_statement_matching
python manage.py test receipts.test_receipt_component_identity
python manage.py test receipts.test_plan_change_matching
python manage.py test receipts.tests
```

本番適用前にDBバックアップを取得してください。
