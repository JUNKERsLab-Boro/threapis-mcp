# 定時自動投稿 (`publish_scheduled_post`)

`THREADS_ENABLE_SCHEDULED=1` のときだけ登録される単一ツール。手動の `prepare_text_post` / `publish_text_post` とは独立。

| 項目 | 挙動 |
|---|---|
| 投稿先 | `THREADS_SCHEDULED_ACCOUNT`（既定 `junkerslabtech`）。投稿前に `account_info` で照合、不一致なら書き込み前に拒否 |
| 枠 | 毎日 07:00 / 20:00 JST。サーバー時計で判定し、枠の5分前〜+`THREADS_SCHEDULED_GRACE_MINUTES`(既定30)分のみ可。枠外はAPIに触れず拒否 |
| 1枠1投稿 | `scheduled_slots.slot_id` が主キー。確定済みの枠は同じ投稿を返すだけ（別文面でも再投稿しない） |
| 曖昧な結果 | タイムアウト/5xx → 直近25件を照会し公開済みなら published。不明なら `unknown`（再投稿しない） |
| 確定失敗 | 何も公開されていないので同じ枠で1回やり直し可 |
| 本文 | 空・500超・直近30日の同一文面・アカウント直近25件と同一の文面を拒否。年齢/学年表現（中学生・15歳・高2 等）を拒否 |
| 返却 | `post_id`, `text`, `published_at`(UTC), `published_at_jst`, `permalink` |

## ChatGPT側の確認要求（MCP側では解除できない）
- ChatGPT は `readOnlyHint` の無いツールを「書き込み」として扱い、承認を求める（公式 Developer mode ドキュメント）。
- 投稿ツールは実際に書き込むため `readOnlyHint=false` が正しい。偽の `true` は付けない。
- 「会話内で承認を記憶」はその会話限り。スケジュール実行で承認を自動通過できる公式な記載は見つからず、Scheduled Task 側で承認が出る/コネクタが使えない可能性がある。**実機で1回確認が必要**。
- 解除できない場合の代替: 外部cron/launchd等でMCPクライアントを直接呼ぶ（ChatGPTを介さない）。

## 文面づくり（ChatGPT側プロンプトの要点）
軸は「100日後にプロの修理屋になる」。`get_recent_posts` と過去チャットから実際の修理・開発・失敗談を選び、架空の実績は作らない。30〜50代（特に40代）のPC/ガジェット好き向け。直近投稿と同じ話題の焼き直しは避ける。
