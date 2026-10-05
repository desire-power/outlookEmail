# 日本語UIと上流の取り込み

このforkではWebのログイン画面・メール管理画面・共有画面を標準で日本語表示します。

## 構成

- `static/locales/ja.json`: 中国語の表示文言と日本語訳。
- `outlook_web/japanese_ui.py`: Jinjaテンプレートを描画する前と、アプリのJavaScriptを配信する際に翻訳する層。
- `static/js/localization-ja.js`: サーバーから受け取ったエラー・認可ログなどの表示と、組み込みグループの表示名を日本語化する補助処理。
- `outlook_web/segments/13_japanese_ui.py`: アプリとの接続。既存のエントリーポイントには読み込みの1行だけ追加しています。

上流の `templates/` と既存の `static/js/index/`、`static/js/email-share.js` は変更していません。テンプレートに差し込まれる利用者データ、メールの件名・本文・添付ファイル、入力値、APIのJSON、保存済みのグループ名・タグ名は翻訳しません。既定グループと一時メールの表示名だけを変更し、内部判定で使う中国語の値を保持します。

JavaScriptのURLは上流のままです。翻訳辞書だけが変わった場合もキャッシュを更新できるよう、翻訳済みJavaScriptには内容に基づくETagと再検証用のCache-Controlを付けています。

## 元の表示に戻す

起動するプロセスの環境変数に `OUTLOOK_UI_LANGUAGE=zh-CN` を設定して再起動すると、元のテンプレート・JavaScriptを配信します。省略時は `ja` です。画面内での言語切り替えはありません。

Dockerの場合は自分のCompose設定・override等で環境変数をコンテナに渡してください。この変更にはCompose設定の変更を含めていません。

## fork元のmainを取り込むとき

上流のmainを通常どおりマージし、エントリーポイントのセグメント一覧にこのforkの追加セグメントが残っていることを確認してください。上流と同じ場所に13番以降のセグメントが追加された場合は、一覧への追加を両方残してください（ファイル名は異なるため番号だけを理由に片方を削除する必要はありません）。

上流に新しい表示文言が追加された場合は辞書に訳を追加します。未登録の文言は元の表示を保持します。新しい画面やJavaScriptの配置が追加された場合は、翻訳対象の範囲も更新してください。

既存のテンプレートやJavaScriptを直接日本語に書き換える必要はありません。上流でDOMや関数名が変わった場合は補助処理の動作も確認してください。

```sh
uv run --no-project --python 3.11 --with-requirements requirements-dev.txt python -m pytest tests/test_japanese_ui.py -q
```

CLI・サーバーログ・デスクトップのネイティブメニュー・APIレスポンスの文言は従来どおりです。
