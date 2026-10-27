# ThreAPIs MCP (Threads Insights MCP Server)

Meta 公式 Threads API を使った **読み取り専用** の MCP サーバー。AI クライアント（Claude / ChatGPT 等）から自分の投稿とインサイトを閲覧・比較できます。返信・削除機能は存在しません。投稿はオプションのテキスト投稿のみ（後述、既定で無効）。

## ツール

| ツール | 内容 |
|---|---|
| `get_account_insights(days?, follower_breakdown?)` | views / likes / replies / reposts / quotes / clicks / followers_count（`views` は期間合計＋日次系列）。`follower_breakdown`=country\|city\|age\|gender（フォロワー100人以上） |
| `get_post_insights(post_id)` | views / likes / replies / reposts / quotes と engagement_rate。API が返さない指標は `null` |
| `get_recent_posts(limit=25, after?)` | id・本文・日時・permalink 等。`paging.next_cursor` を `after` に渡して次ページ |
| `compare_posts(post_ids? , recent?)` | 複数投稿（最大20）または直近N件（最大25、既定5）の比較とランキング |

エンゲージメント率 = `(likes+replies+reposts+quotes)/views×100`。views が 0／未取得、または分子の指標のいずれかが取得できない場合は `null`。

呼び出し時に取得し、SQLite（`posts` / `post_insights` / `account_insights`、WAL、パラメータ化SQL）へ履歴保存します。API 取得が成功してから書き込むため、エラー時に既存履歴は変更されません。定期収集はありません。

## 確認した公式仕様（2026-10 時点）

- Base: `https://graph.threads.com/v1.0/`
- 投稿インサイト: `GET /{media-id}/insights?metric=views,likes,replies,reposts,quotes`
- アカウントインサイト: `GET /{user-id}/threads_insights?metric=...`（`since`/`until` は 2024-04-13 以降。`followers_count` は期間指定不可）
- 権限: `threads_basic` + `threads_manage_insights`（本サーバーはこの2つのみ要求）
- OAuth: `https://threads.com/oauth/authorize` → `POST graph.threads.com/oauth/access_token` → 長期トークン `GET /access_token?grant_type=th_exchange_token`（60日）→ 更新 `GET /refresh_access_token?grant_type=th_refresh_token`（発行後24時間以上、未失効のみ）
- 注意: メディアの `views` は公式で "in development"、`REPOST_FACADE` 投稿のインサイトは空。リストの最大 `limit` は公式ページに明記がなく 100 を上限としています（要実機確認）。公式エラーコードの詳細ページは取得できなかったため、Graph API 共通のコード（190=トークン、4/17/32/613=レート制限、10/2xx=権限）で判定しています。

## セットアップ

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # 値を記入
.venv/bin/python -m pytest
```

### Meta Developers 側の設定

1. [developers.facebook.com](https://developers.facebook.com/) で Business タイプのアプリを作成し、ユースケース **Access the Threads API** を追加。
2. Threads API > 権限で `threads_basic` と `threads_manage_insights` を追加。
3. Threads API > 設定で **Redirect Callback URLs**（HTTPS）を登録し、`THREADS_REDIRECT_URI` に完全一致で設定（末尾スラッシュに注意）。
4. App roles > Roles で自分の Threads アカウントを **Threads テスター** として追加し、Threads アプリ側（設定 > アカウント > ウェブサイトの権限 > 招待）で承諾。開発モードでは自分（テスター）のデータのみ取得可能。
5. アプリの **Threads App ID / App Secret** を `.env` に設定。

### 認証（実アカウント接続が必要な段階）

必要な値: `THREADS_APP_ID`, `THREADS_APP_SECRET`, `THREADS_REDIRECT_URI`。

```bash
.venv/bin/threads-insights-auth login    # URL を開いて承認 → リダイレクト後のURL全体を貼り付け
.venv/bin/threads-insights-auth status
.venv/bin/threads-insights-auth refresh  # 手動更新
```

`state` を検証し、長期トークンを `THREADS_TOKEN_FILE`（権限 0600）に保存します。期限7日前以内かつ発行24時間以上なら、サーバー起動後の最初のツール呼び出しで自動更新します。期限切れは検出され、再ログインを促すエラーになります。`THREADS_ACCESS_TOKEN` を直接指定することも可能（その場合は自動更新なし）。

## MCP クライアント設定

[examples/claude_desktop_config.json](examples/claude_desktop_config.json) を参照（Claude Desktop 等）。**パスは絶対パス**にしてください。Claude Code の場合:

```bash
claude mcp add threads-insights -- /ABS/PATH/.venv/bin/python -m threads_insights.server
```

将来の HTTP 化: `THREADS_MCP_TRANSPORT=streamable-http`（127.0.0.1 バインド。公開する場合は別途認証が必要）。

## 投稿機能（オプション・既定で無効）

テキスト投稿のみ。分析ツール4つとは別モジュール（`publisher.py` / `publish_service.py` / `publish_store.py`）で、`THREADS_ENABLE_PUBLISH=1` を設定したときだけ次のツールが登録されます（未設定なら従来どおり読み取り専用）。

| ツール | 内容 |
|---|---|
| `prepare_text_post(text, reply_control?, topic_tag?)` | **投稿しない。** 本文を検証し、アカウント名・本文・文字数・返信範囲のプレビューと `request_id` / `confirm_hash` を返す（下書きは30分で失効） |
| `publish_text_post(request_id, confirm_hash, allow_retry_after_unknown?)` | **公開操作。** ユーザーがプレビューを明示承認した後にのみ呼ぶ。`confirm_hash` が下書きと一致しなければ拒否 |
| `get_post_request_status(request_id)` | 状態確認。結果不明の場合は直近25件の投稿と照合して公開済みか判定 |

安全設計:
- 承認はツール説明でも必須化（LLM は承認なしに `publish_text_post` を呼んではならない）。MCP クライアントのツール実行確認も有効にしてください。
- 状態管理（SQLite `post_requests`）: `prepared → creating → container_created → publishing → published`、`failed`（API が明確に拒否＝未公開）、`unknown`（公開呼び出しがタイムアウト/5xx＝公開済みかもしれない）。遷移は compare-and-set で、同時呼び出しでも二重実行しません。`publishing` は API 呼び出し**前**に保存します。
- 公開済みの `request_id` を再度呼んでも再投稿せず既存結果を返す。同一アカウント・24時間以内の同一本文は拒否。
- 書き込みは自動再送しない（1回のみ）。`unknown` の場合は `get_post_request_status` → ユーザー確認 → `allow_retry_after_unknown=true` の順でのみ再試行可。コンテナ作成のみ失敗した場合は未公開なので、そのまま再実行できます。
- 公式仕様（2026-10確認）: `POST /{user}/threads`（`media_type=TEXT`, `text`≤500、絵文字は UTF-8 バイト換算）→ `GET /{container}?fields=status,error_message` → `POST /{user}/threads_publish`（`creation_id`）。1日250投稿まで。**同じ `creation_id` で再公開した場合の挙動は公式ドキュメントに記載がない**ため、上記の自前の状態管理で重複を防いでいます。コンテナの有効期限・status の値の一覧も公式ページで確認できませんでした（`FINISHED` を準備完了、`ERROR`/`EXPIRED` を失敗として扱う）。
- 画像・カルーセル・予約投稿は未実装（`ThreadsPublisher` に `create_*_container` を足す形で拡張）。

### 有効化手順

1. Meta Developers > 自分のアプリ > Threads API の権限に `threads_content_publish` を追加。開発モードでは Threads テスターとして追加済みの自分のアカウントなら App Review 前でも承認できます（他ユーザー向けは App Review が必要）。
2. 再認可（本人操作・ブラウザ）: 新しいスコープは再承認が必要です。
   ```bash
   .venv/bin/threads-insights-auth login --publish
   ```
   通常の `login` はこれまでどおり insights 用の最小権限のままです。`--publish` で得たトークンは両方の権限を含みます。権限のないトークンで投稿を試みると、API を呼ぶ前に拒否します（トークンファイルにスコープを記録。環境変数トークンは判別不能で API 側エラーになります）。
3. MCP 設定の `env` に `"THREADS_ENABLE_PUBLISH": "1"` を追加し、クライアントを再起動。

### MCP クライアントへの登録

- **Claude Desktop**: `~/Library/Application Support/Claude/claude_desktop_config.json` の `threads-insights` の `env` に追加して再起動。
- **Claude Code**: `claude mcp remove threads-insights && claude mcp add -e THREADS_ENABLE_PUBLISH=1 ... threads-insights -- /ABS/PATH/.venv/bin/python -m threads_insights.server`（既存の env も `-e` で引き継ぐ）。
- **ChatGPT**: ChatGPT のコネクタはリモート（HTTPS）MCP が前提で、ローカル stdio は登録できません（一般仕様で、今回の環境では未検証）。本サーバーの HTTP モードは認証が未実装のため、インターネットに公開しないでください。公開操作を伴うため、ChatGPT 経由の投稿は現時点で非対応とします。

## セキュリティ

- 全リクエストは固定ホスト `graph.threads.com` へのもの。分析系は GET のみ（投稿機能を有効にした場合のみ、専用クラスが POST を送る）。`paging.next` の URL は辿らず、cursor は形式検証。
- 投稿IDは数字のみ、`limit`/`days`/件数は範囲検証。任意SQLツールなし。
- トークンはログ・エラーメッセージに出力しない（httpx ログ抑制、例外の URL を除去）。
- `.env`・DB・トークンファイルは `.gitignore` 済み。

## 未検証事項

実トークンでの動作確認は未実施（モックAPIでの検証のみ）。初回接続時は、`views` の取得可否、`limit` 上限、アクセス権（テスター/Advanced Access）を確認してください。
