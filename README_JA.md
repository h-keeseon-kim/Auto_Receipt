# ReceiptHub v1.16.4 auto_receipt 復旧用

## 原因と範囲
2026-09-16に確認したGitHubコミット63da4c30394af471156ab26f6c0c369ae15095d1では、
auto_receipt内がsettings.pyだけになっています。直前コミット
eea22285e6a5f8fa2f8b9cfc887bb80d55ffb72fに存在した4ファイルを復元します。

- __init__.py（空ファイルですが復元対象です）
- asgi.py
- urls.py
- wsgi.py

同梱settings.pyは63da4c3のGitHub上のファイルとバイト単位で一致する、ログ出力設定追加済みのものです。
4ファイルは直前コミットとGit blob SHA-1が一致しています。
DB、マイグレーション、照合処理、Cron設定には変更を加えていません。

## 推奨：既存settings.pyを変更せず、欠けた4ファイルだけを復元
ZIPを展開し、そのフォルダーで次を実行します。末尾のパスはmanage.pyがある既存プロジェクトを指定します。

```sh
python3 restore_missing_files.py /path/to/Auto_Receipt --check
python3 restore_missing_files.py /path/to/Auto_Receipt
```

既存ファイルは同内容ならそのまま、異なる内容なら処理を停止します。ファイル削除・Git操作・DB操作は行いません。
続いてGitHubへ復元4ファイルをコミットしてください。GitHubの親フォルダーごと削除する必要はありません。

## 手動で反映する場合
同梱auto_receiptフォルダー内の4ファイルを、既存プロジェクトのauto_receiptへ**ファイル単位で追加**します。
settings.pyはすでにログ出力を追加済みなら変更不要です。フォルダーを空にする・プロジェクト全体をこのZIPで置換する操作はしないでください。

最終的な構成:

```text
Auto_Receipt/
├── manage.py
├── auto_receipt/
│   ├── __init__.py
│   ├── asgi.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── receipts/
```

## 確認
依存パッケージが導入された既存環境で `python manage.py check` を実行してください。
Railwayでは既存predeploy.shも同じチェックを実行します。DEBUG=Falseは維持してください。
今回の復元はModuleNotFoundErrorに対する修正です。以前からの明細画面500エラーの原因修正は含めていません。
復旧後に明細画面で500が再発する場合、追加済みLOGGING設定によるDeploy LogsのTracebackで切り分けます。

## 検証限界
この作業環境にはDjangoがなく、pipでの取得もDNSエラーで失敗したため、manage.py checkと実DBテストは未実行です。
構文・Git blob一致・Pythonのモジュール探索・復旧スクリプトの安全性を検証しています。
GitHub、Railway、本番DBへの変更は行っていません。
