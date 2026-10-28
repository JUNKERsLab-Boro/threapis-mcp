# ChatGPT 通常チャットで使う（Secure MCP Tunnel）

## 調査結果（2026-10-28）

- ChatGPT Mac アプリの MCP 設定 `~/.codex/config.toml` は **Codex 用**。公式ドキュメントによると、通常の ChatGPT チャットはこのファイルを読まず、ツールはプラグイン（リモート MCP）経由で提供される。
- `threads-insights` / `threads-insights-2` は Codex のログ上で両方とも正常に起動・初期化されている（`Service initialized`）。サーバー単体でも `initialize` → `tools/list`（7ツール）→ `get_account_insights` が成功。**サーバー側の不具合ではない。**
- 同じ症状（ローカル stdio MCP が検出されるのに通常チャットへ公開されない）は [openai/codex#38162](https://github.com/openai/codex/issues/38162) で多数報告されており、公式の回答・修正はまだない。Work モードでは動く報告がある一方、動かない報告もある。
- 重複登録（`-2`）は原因ではない（片方だけでも同症状が他サーバーで再現しているため）。削除不要。
- 通常チャットで確実に使える方法として報告されているのは **Secure MCP Tunnel**（設定 > プラグイン > カスタム MCP サーバー > Tunnel）。

## セットアップ（あなたの操作が必要）

前提: OpenAI Platform の組織で Tunnels を使えること（API キー作成・トンネル作成はご本人操作。課金条件は公式で未確認）。

1. https://platform.openai.com/settings/organization/tunnels でトンネルを作成し `tunnel_id` を控える。Tunnel Read/Use 権限だけの **制限付き API キー**を作成（Threads のトークンとは無関係）。
2. 同ページから `tunnel-client`（macOS）をダウンロードして PATH に置く。
3. プロファイル作成:
   ```bash
   export CONTROL_PLANE_TUNNEL_ID=tunnel_xxxxxxxx...
   tunnel-client init --sample sample_mcp_stdio_local --profile threapis \
     --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
     --mcp-command "/Users/yuma/Desktop/claude_code/threAPIs/scripts/threads_mcp_stdio.sh"
   ```
4. API キーは**環境変数で渡す**（プロファイルやコマンド履歴に直書きしない）:
   ```bash
   export CONTROL_PLANE_API_KEY=...   # 端末内のみ
   tunnel-client doctor --profile threapis --explain
   tunnel-client run --profile threapis     # 起動したままにする（同一 tunnel_id で1プロセスのみ）
   ```
5. ChatGPT: 設定 > プラグイン（または Connectors）> Developer mode を有効 > カスタム MCP サーバーを追加 > Connection に **Tunnel** を選び `tunnel_id` を指定。
6. 新しい通常チャットで「Threads のアカウントインサイトを 2026-10-21〜28 で見せて」と依頼。

投稿ツールは既定で無効（ラッパーが `THREADS_ENABLE_PUBLISH=0`）。有効にする場合は `tunnel-client run` の前に `export THREADS_ENABLE_PUBLISH=1`。
