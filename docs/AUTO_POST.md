# 自動投稿 (`publish_auto_post`)

`THREADS_ENABLE_AUTO_POST=1` のときだけ登録される単一ツール。手動の `prepare_text_post` / `publish_text_post`（`THREADS_ENABLE_PUBLISH`）とは独立。
時刻の制限は無い（24時間いつでも呼べる）。7:00 / 20:00 は ChatGPT のスケジュール設定側で管理する。

| 項目 | 挙動 |
|---|---|
| 投稿先 | `THREADS_AUTO_POST_ACCOUNT`（既定 `junkerslabtech`）。投稿前に照合、不一致なら書き込み前に拒否 |
| 頻度上限 | 最短間隔 `THREADS_AUTO_MIN_INTERVAL_MINUTES`（既定60分）、直近24時間で `THREADS_AUTO_MAX_PER_DAY`（既定4件）。判定と予約は1つのIMMEDIATEトランザクション |
| 履歴 | SQLite `auto_posts` + `post_requests`（本文・状態・投稿ID・時刻） |
| 重複 | 直近30日の同一文面、アカウント直近25件と同一の文面を拒否（再送信も拒否） |
| 曖昧な結果 | タイムアウト/5xx → 直近25件を照会。公開済みなら published、不明なら `unknown`（再投稿しない）。未解決の間（6時間）は別文面の新規投稿も拒否 |
| 確定失敗 | 何も公開されていないので同じ文面で再実行可（上限には数えない） |
| 本文 | 空・500超・年齢/学年表現（中学生・15歳・高2 等）を拒否。「40代」等の読者層表現は可 |
| 返却 | `post_id`, `text`, `published_at`(UTC), `published_at_jst`, `permalink` |

## ChatGPT側の確認要求（MCP側では解除できない）
- ChatGPT は `readOnlyHint` の無いツールを「書き込み」として扱い、承認を求める（公式 Developer mode ドキュメント）。
- 投稿ツールは実際に書き込むため `readOnlyHint=false` が正しい。偽の `true` は付けない。
- 「承認を記憶」はその会話限り。スケジュール実行で承認を自動通過できる公式記載は見つからない。**実機で確認が必要**。
- 止まる場合の代替: cron/launchd からMCPクライアントを直接呼ぶ（ChatGPTを介さない）。

## 文面づくり（ChatGPT側プロンプトの要点）
軸は「100日後にプロの修理屋になる」。`get_recent_posts` と過去チャットから実際の修理・開発・失敗談を選び、架空の実績は作らない。30〜50代（特に40代）のPC/ガジェット好き向け。直近投稿と同じ話題の焼き直しは避ける。
