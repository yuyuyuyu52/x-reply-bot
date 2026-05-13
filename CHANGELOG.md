# Changelog

All notable user-facing changes to this project are recorded here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Internal refactors, test-only additions, and doc micro-edits are intentionally
omitted unless they alter how the bot is configured or operated.

## [Unreleased]

### Added
- Add browser dependency bootstrap scripts for Chrome CDP and browser-harness deployment
- Add /config Telegram commands to view, edit, confirm, apply, and rollback .env-based bot configuration
- Add /update Telegram command to pull latest code, compile-check, restart, health-check, and report the result
- Add /review and /rate Telegram commands for human feedback scoring on replies and proactive posts
- Feedback scores injected into reply and post generation prompts as style reference

### Fixed
- Fix circular imports that prevented direct entrypoint module startup
- Prevent Telegram /config from changing process-control variables that can break rollback or duplicate daemon sessions
- Track /update as the active Telegram job while the detached update script is running
- Launch Chrome through the macOS app launcher on Darwin so CDP startup is stable in local tests
- Fix generated reply-sending harness code so profile URL lookup and boolean literals execute as Python
- 修复 `run_once.py` 缺少 `import fcntl` 导致 `NameError`
- 修复 `src/common.py` 的 `ROOT` 指向 `src/` 而非 repo root，导致 `.env` 和 `state/` 路径错位
- 修复 `run_once.py`、`post_once.py`、`bot_daemon.py` 的 subprocess/Popen 调用缺少 `PYTHONPATH`，导致子脚本 `from src.xxx` 报 `ModuleNotFoundError`
- 修复 `src/reply/generate_reply.py` 中 `_build_learning_context` 应为 `build_learning_context`
- 修复 `src/post/post_generate.py` 中 `get_generation_context` 应为 `persona_context_dict`，补缺失的 `recent_learning_references` 导入
- 修复 `src/observe_feed.py` 缺失 `infer_own_handle`、`parse_metrics`、`engagement_score` 导入
- 修复 `src/revisit.py` 的 `ROOT` 指向 `src/` 而非 repo root

## [2026-05-01]

### Added
- 支持引用推文和转发：`generate_reply.py` 更新了 Prompt 使其能选择 reply、quote 或 repost，`send_reply.py` 和 `run_once.py` 同步支持执行。
- 自动关注高质量用户：`observe_feed.py` 遇到 quality_label 为 high_quality 的帖子时，会自动用 harness 点击 Follow 作者（每天最多关注 5 人）。
- 回复反馈：`revisit.py` 现在也扫 `state/history/`，对发出 ≥24h 的回复打开原帖楼、滚动定位自己那条 nested reply（`reply_text` 完全匹配 + 自家 handle 兜底确认）、抓 aria-label 写入 `engagement_24h`；找不到时累计 attempts，3 次后标记 `failed`
- `/revisit_status` 与每晚 24h 反馈摘要现在分别按 `post` / `reply` 计数与展示
- 反馈回访任务 `revisit.py`：每晚 23:00–07:00 每 30 分钟扫一次 `state/post_history/`，对发出 ≥24h 的主动帖回访其 views/likes/replies/reposts/bookmarks 并写入 `engagement_24h`
- Telegram 命令 `/revisit_once` `/revisit_status`，并加入 `sync_tg_commands.py` 命令清单
- `scripts/_common.sh`：让所有 shell 脚本可在任意部署目录运行；新增 `X_REPLY_PYTHON` / `X_REPLY_TMUX_SESSION` / `X_REPLY_TZ` 环境变量覆盖

### Changed
- Vendor browser-harness in the repository and install it from `vendor/browser-harness`
- `start_bot.sh` / `stop_bot.sh` / `status_bot.sh` / `bot_loop.sh` / `scheduled_run.sh` / `install_cron.sh` / `uninstall_cron.sh` 不再硬编码 `/home/will/x-reply-bot` 和 harness 路径，改为从脚本自身位置反推
- AGENTS.md 升级为 "Four job types"
