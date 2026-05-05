# Changelog

All notable user-facing changes to this project are recorded here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Internal refactors, test-only additions, and doc micro-edits are intentionally
omitted unless they alter how the bot is configured or operated.

## [Unreleased]

## [2026-05-05]
### Added
- 反馈回访任务 `revisit.py`：每晚 23:00–07:00 每 30 分钟扫一次 `state/post_history/`，对发出 ≥24h 的主动帖回访其 views/likes/replies/reposts/bookmarks 并写入 `engagement_24h`，失败 3 次后标记 `failed` 不再尝试 (f3c4482, 74577d0)
- Telegram 命令 `/revisit_once` `/revisit_status`，并加入 `sync_tg_commands.py` 命令清单 (74577d0)
- 当晚 24h 反馈摘要：每个夜间窗口（按窗口起始日期去重）在第一批回访完成后统一推送一次至 Telegram (74577d0)
- `scripts/_common.sh`：让所有 shell 脚本可在任意部署目录运行；新增 `X_REPLY_PYTHON` / `X_REPLY_TMUX_SESSION` / `X_REPLY_TZ` 环境变量覆盖 (e270358)

### Changed
- `start_bot.sh` / `stop_bot.sh` / `status_bot.sh` / `bot_loop.sh` / `scheduled_run.sh` / `install_cron.sh` / `uninstall_cron.sh` 不再硬编码 `/home/will/x-reply-bot` 和 harness 路径，改为从脚本自身位置反推 (e270358)
- AGENTS.md 升级为 "Four job types"；README 增补 `/event` `/revisit_once` `/revisit_status` 命令说明 (74577d0, c4a7e9e)

### Fixed
- Chrome 标签累积 + beforeunload 弹窗：`prepare_post.py` / `send_reply.py` / `post_send.py` / `observe_feed.py` 现在统一复用同一个 x.com 标签，每次 `goto_url` 之前先 `js('window.onbeforeunload = null')` 防止"离开页面?"对话框打断 (c939538)
- `state/replied_posts.json` 无界增长：`send_reply.py` 写入时保留最近 2000 条 (e2889f0)
