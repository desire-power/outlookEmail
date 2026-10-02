# メールアカウント外部API

既存の認証・アカウント保存・メール取得処理を利用した外部APIです。

## 設定と認証

Web画面にログインし、「設定 → 对外 API Key」でキーを入力するか「随机生成」で生成して、設定を保存してください。既存の外部APIと同じキーを利用します。

```http
X-API-Key: your-api-key
```

両APIとも既存の `api_key_required` で認証します。`X-API-Key` ヘッダーのほか、従来の `api_key` / `apikey` クエリパラメータも利用できます。ログインCookie・CSRFトークンは不要です。キーが欠落・不一致の場合は `401`、キーがシステムに未設定の場合は `403`（キーを提示した場合）です。レスポンスには `Cache-Control: no-store` を付けます。

## 最新メールを取得

`GET /api/external/latest-emails`

| パラメータ | 必須 | 説明 |
| --- | --- | --- |
| `email` | はい | OAuth認可済みの受信アカウントのメールアドレス。大文字小文字は区別しません。登録済みの別名も利用できます。 |
| `top` | いいえ | 最新から取得する件数。初期値 `1`、範囲 `1`〜`50`。 |
| `folder` | いいえ | `inbox`（初期値）または `junkemail`。 |

リクエストごとにGraph/IMAP経由でメールサーバーへ問い合わせます。ローカル保存メールは参照せず、未読限定や前回取得後の差分という意味ではありません。既存のメール取得処理・代理設定・取得順序を利用します。取得したメールを既読にはしません。

```sh
curl --get 'http://localhost:5000/api/external/latest-emails' \
  -H 'X-API-Key: your-api-key' \
  --data-urlencode 'email=user@outlook.com' \
  --data-urlencode 'top=1'
```

成功時は `200` で以下の形式を返します。`emails` の各要素は既存メールAPIと同じ形式で、件名・送信者・受信日時・本文プレビューなどを含みます。メールがない場合も成功し、`emails: []` となります。

```json
{
  "success": true,
  "emails": [],
  "method": "Graph API",
  "has_more": false,
  "requested_email": "user@outlook.com",
  "resolved_email": "user@outlook.com"
}
```

入力不正は `400`、正式登録されていないアドレス（登録待ちの未認可アカウントを含む）は `404`、メール取得失敗は `502` です。

## Outlookメールアドレスを登録

`POST /api/external/mail`

`email` と `password` だけでOutlookアカウントを登録し、同じリクエスト内で既存のOutlook OAuth認可フローを同期実行します。登録待ちテーブル `outlook_upload_accounts` への暗号化保存、GraphのOAuth認可、Refresh Tokenの検証、正式な `accounts` への保存を順に行い、完了後に成否を返します。成功後は同じメールアドレスで最新メール取得APIを利用できます。

OAuth処理中はHTTPリクエストが完了しません。外部サービスへの複数の問い合わせを含むため、呼び出し側・リバースプロキシのタイムアウトはOAuth処理を待てる値に設定してください。既存のWeb画面の認可フローは変更しません。

| JSONフィールド | 必須 | 説明 |
| --- | --- | --- |
| `email` | はい | メールアドレス。前後の空白を除去して小文字で保存。 |
| `password` | はい | アカウントのパスワード。既存処理で暗号化して保存。空欄・空白のみは不可。前後の空白はパスワードの一部としてそのまま保存。 |
| `group_id` | いいえ | 既存グループの正の整数ID。初期値 `1`。 |
| `remark` | いいえ | 備考。最大500文字。 |

```sh
curl 'http://localhost:5000/api/external/mail' \
  -H 'X-API-Key: your-api-key' \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@outlook.com","password":"your-password"}'
```

認可・Token検証・正式保存まで成功した場合は `201` で以下を返します。`id` は登録待ちテーブルのID、`account_id` は正式アカウントのIDです。パスワードやトークンは返しません。

```json
{
  "success": true,
  "account": {
    "id": 1,
    "email": "user@outlook.com",
    "group_id": 1,
    "account_type": "outlook",
    "is_authorized": true,
    "account_id": 1,
    "authorization_type": "graph"
  }
}
```

入力不正・存在しないグループは `400`。大文字小文字を無視した既存アドレス（正式登録・登録待ち）・登録済み別名との重複は `409` で、既存アカウントを上書きしません。未対応フィールドも `400` です。

OAuth認可・Token検証・正式保存が失敗した場合は `502` で以下を返します。登録待ちのレコードは保持するため、既存のWeb画面から認可を再実行できます。同じアドレスの再登録は `409` になります。外部サービスの詳細エラー・認可ログはレスポンスに含めません。

```json
{
  "success": false,
  "error": "OAuth authorization failed",
  "account": {
    "id": 1,
    "email": "user@outlook.com",
    "group_id": 1,
    "account_type": "outlook",
    "is_authorized": false
  }
}
```

アカウント一覧は既存の `GET /api/external/accounts`、登録は `POST /api/external/mail` を利用してください。`POST /api/external/accounts` は非対応で `405` を返します。
