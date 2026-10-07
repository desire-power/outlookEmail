# OutlookEmailをAWSへ公開する手順

social-media-managerのPulumiで作成したECRとLightsailを使い、OutlookEmailのmainをビルド → ECRへpush → Lightsailへデプロイします。ローカルのDBは移行せず、初回は新しいDBで開始します。

PRのマージだけではAWSデプロイは始まりません。初回・通常更新・切り戻しは、同じGitHub Actionsをmainから手動実行します。

## 初回に行うこと

### 1. IAMの補正を適用する

[CI用のsocial-media-manager修正PR #1251](https://github.com/desire-power/social-media-manager/pull/1251)を確認・マージし、infraで`pulumi preview` → `pulumi up`を実行してください。先に作成したECRやVMを作り直す変更ではありません。

- GitHubが現在発行するOIDC subjectにpush/deploy Roleの信頼条件を合わせます。
- deploy Roleに東京リージョンのSSHポート状態の読み取り権限を追加します。既存のSSH許可を残すために使用します。

OutlookEmailは2026-07-15以降に作成されたリポジトリなので、mainのsubjectは以下です。従来のIDを含まないsubjectではRoleを引き受けられません。

```text
repo:desire-power@123461900/outlookEmail@1401365599:ref:refs/heads/main
```

[GitHubのOIDC仕様変更](https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-github-actions-oidc-tokens/)。GitHub Environmentは設定せず、mainのrefで認証します。

### 2. GitHub Repository Secretsを設定する

OutlookEmailのGitHub画面で **Settings → Secrets and variables → Actions → Repository secrets → New repository secret** を開きます。

| Secret名 | 設定する内容 |
| --- | --- |
| `LOGIN_PASSWORD` | 本番で使う初回ログインパスワード。12文字以上、改行なし。`admin123`を使わないでください。 |
| `LIGHTSAIL_SSH_PRIVATE_KEY` | Pulumiの`OUTLOOK_EMAIL_SSH_PUBLIC_KEY`に対応する**秘密鍵ファイルの全文**。`.pub`ではありません。`-----BEGIN ... PRIVATE KEY-----`から末尾まで貼り付けます。 |
| `LIGHTSAIL_SSH_KNOWN_HOSTS` | 下記で取得するサーバーの確認済み公開host key。SSHの接続先を検証します。 |
| `LIGHTSAIL_SSH_KEY_PASSPHRASE` | 任意。SSH秘密鍵にパスフレーズがある場合だけ設定します。 |

SSHの鍵は、先に作った手元の鍵を使います。**秘密鍵をリポジトリにコミットしないでください。** AWSアクセスキーやGitHub用の`SECRET_KEY`は追加しません。ECR URL・IP・Role ARNはworkflowに設定済みです。

`LIGHTSAIL_SSH_KNOWN_HOSTS`は、AWSへログイン済みの手元のAWS CLIで次を実行して取得できます。`GetInstanceAccessDetails`は一時的なSSH接続情報も生成するAPIですが、このコマンドは**公開host keyだけ**を表示します。

```bash
aws lightsail get-instance-access-details \
  --instance-name prd-outlookemail \
  --region ap-northeast-1 \
  --protocol ssh \
  --query 'accessDetails.hostKeys[].[algorithm,publicKey]' \
  --output text
```

出力された`ssh-ed25519`と公開鍵から、次の形式の1行をSecretへ設定します。`AAA...`は実際の公開鍵全体へ置き換えます。

```text
52.199.82.242 ssh-ed25519 AAA...
```

表示がない場合はAWS LightsailのブラウザSSHから以下を実行し、表示された1行を設定できます。ブラウザSSH用の通信が許可されている必要があります。

```bash
awk '{print "52.199.82.242 " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

自動取得した未検証の`ssh-keyscan`結果をそのまま信頼したり、host key検証を無効化したりしません。[AWS CLIのhost key取得仕様](https://docs.aws.amazon.com/cli/latest/reference/lightsail/get-instance-access-details.html)。

### 3. DNSと初期化を確認する

`outlook.dozer-x.com`のAレコードを`52.199.82.242`へ向け、待機ページがHTTPSで開けることを確認します。IPv6のAAAAレコードは不要です。

初期化済みVMには`/opt/outlookemail/host.env`、`app.env`、Caddyの待機ページがあります。SSHで調べる場合は、`sudo test -f /var/lib/outlookemail/bootstrap.ready`で初期化完了を確認できます。`app.env`の内容を公開ログへ貼り付けないでください。

### 4. CIのPRをマージして手動実行する

OutlookEmailのCI用PRを確認・マージ後、**Actions → Deploy OutlookEmail to AWS → Run workflow**を選びます。

- **Branch: main**
- **commit_sha: 空欄**（実行時点のmainを使用）

実行内容は次の順序です。

1. Secretの設定とデプロイスクリプトを検証。
2. mainのソースを既存Dockerfileで`linux/amd64`イメージにし、起動・`/login`を確認してECRへpush。同じSHAのイメージが既にある場合は再利用。
3. runnerのIPだけを一時的にSSH許可し、SSHで確認済みのイメージdigestをpull。
4. 本番Composeでアプリを起動し、アプリ → Caddyの順に疎通確認。
5. この実行が追加したSSHルールだけを削除。

成功後、`https://outlook.dozer-x.com`を開き、`LOGIN_PASSWORD`に設定したパスワードでログインします。初回はローカルのアカウント・メール・設定は入りません。

## アプリの環境変数はどこで設定されるか

| 変数 | 本番での管理場所・挙動 |
| --- | --- |
| `LOGIN_PASSWORD` | 初回はGitHub SecretをVMの`/opt/outlookemail/app.env`へ保存し、新しいDBのログインパスワードを初期化。2回目以降は既存値を維持。 |
| `SECRET_KEY` | Infra初期化時にVMで生成済みの固定値を継続使用。GitHubへ渡したり、更新のたびに生成し直したりしません。 |
| `FLASK_ENV` | 本番Composeで`production`を固定。 |
| `OUTLOOK_UI_LANGUAGE` | 本番Composeで`ja`を固定。 |
| `DOCKER_UPDATE_ENABLED` | 本番Composeで`false`を固定。更新はこのCIから行います。 |
| `DOCKER_UPDATE_CONTAINER` | アプリ内のDocker更新機能を使わないため設定不要。Docker socketも渡しません。 |
| `DATABASE_PATH` | 初期化済み`app.env`の`/app/data/outlook_accounts.db`。VMの`/opt/outlookemail/data`を永続化。 |

`app.env`はVM内でrootのみ読み書きできる権限600です。パスワードとECRの一時認証情報はSSHの標準入力で渡し、コマンド引数・イメージ・workflowログへ含めません。

DB作成後のログインパスワードはアプリの設定画面から変更します。GitHubの`LOGIN_PASSWORD`だけを変更しても既存DBのパスワードは変更されません。`SECRET_KEY`を変えると保存済みデータを復号できなくなるため、更新時も固定です。

## 通常の更新・切り戻し

変更をmainへマージ後、同じworkflowをmainから実行します。通常は`commit_sha`を空欄にします。アプリは単一コンテナを置き換えるため、更新中に短い停止が発生します。

既存DBがある場合は、コンテナの置き換え前に`outlookemail-backup.service`でDB・関連ファイルをバックアップし、失敗したら更新を中止します。日次バックアップも既存のInfra設定を利用します。

起動・Caddyの疎通確認に失敗した場合は、直前のイメージとCompose/Caddy設定へ戻します。初回失敗は待機ページへ戻します。成功したSHAとdigestはVMの`/opt/outlookemail/current-deployment.json`に記録します。

明示的な切り戻しでは、`commit_sha`へ**mainに含まれる40桁の過去SHA**を指定します。タグを上書きせずdigestでデプロイします。イメージがECRから削除されていれば、そのSHAを再ビルドします。

DBは自動で過去へ戻しません。DB構造が古いコードと互換でない変更は、通常のイメージ切り戻しだけでは復旧できません。バックアップを確認して復旧方法を判断します。

workflowを強制停止した場合など、一時SSHルールが残ったらLightsailでそのrunnerの`/32`だけを閉じます。管理用IPの許可は残してください。VMを作り直した場合は、IP・host key・固定`SECRET_KEY`・データの扱いも再確認します。

## PR時の検証と実際のデプロイの違い

PRのActionsはAWSへ接続せず、失敗復旧・秘密情報の受け渡し・SSHルール保持のテストを実行します。AWSへのpush・SSH・HTTPS公開の確認は、マージ後にユーザーが手動実行して初めて行われます。
