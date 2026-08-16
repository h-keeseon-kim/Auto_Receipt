# ReceiptHub v1.15.3 管理者月別画面の復旧

## 修正対象

- 管理者の提出履歴から開く「月別状況」
- 同じユーザー・月を対象にする「代理アップロード」

両導線は `staff_user_month_status` ビューを共用します。

## 原因

画面テンプレートは `target_receipt_month` を使用しますが、v1.15.0の画面整理でビュー内の代入だけが欠落しました。Djangoはrender contextを組み立てる時点で未定義変数を参照し、`NameError`によりHTTP 500を返していました。

## 修正後

```python
target_receipt_month = receipt_month_for_submission(selected_month)
```

を月の解析直後に実行します。

```text
selected_month = 2026-08-01
target_receipt_month = 2026-07-01
```

GET表示、フォームエラー時の再表示、代理アップロード後のリダイレクト先のすべてで同じ計算結果を使用します。

## DB変更

追加マイグレーションはありません。既存のユーザー、サービス、領収書、提出履歴、ご利用代金明細には変更を加えません。
