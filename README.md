# ThreAPIs MCP (Threads Insights MCP Server)

Meta 公式 Threads API を使った **読み取り専用** の MCP サーバー。AI クライアント（Claude / ChatGPT 等）から自分の投稿とインサイトを閲覧・比較できます。投稿・返信・削除機能は存在しません。

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

## セキュリティ

- 全リクエストは固定ホスト `graph.threads.com` への GET のみ。`paging.next` の URL は辿らず、cursor は形式検証。
- 投稿IDは数字のみ、`limit`/`days`/件数は範囲検証。任意SQLツールなし。
- トークンはログ・エラーメッセージに出力しない（httpx ログ抑制、例外の URL を除去）。
- `.env`・DB・トークンファイルは `.gitignore` 済み。

## 未検証事項

実トークンでの動作確認は未実施（モックAPIでの検証のみ）。初回接続時は、`views` の取得可否、`limit` 上限、アクセス権（テスター/Advanced Access）を確認してください。
