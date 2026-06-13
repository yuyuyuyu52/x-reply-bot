# Changelog

All notable user-facing changes to this project are recorded here.
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Internal refactors, test-only additions, and doc micro-edits are intentionally
omitted unless they alter how the bot is configured or operated.

## [Unreleased]

### Hotspot 发现与发帖解耦

- 新增 `src/postable_pool.py` 服务层：post_once 通过它按优先级 `人工 > 热点 > auto` 取下一个 topic。
- 新增 `src/hotspot/selector.py`：候选 24h 内、按 `relevance_score × freshness_decay` 排序取本地 top 5，再用一次 LLM 调用判主题去重，避免同一天发同件事。
- `hotspot.db` 新增 `posted_at` 列；旧字段 `added_to_queue` 保留不再读写。schema 在打开 DB 时幂等迁移。
- `discover_hotspots.py` 不再写入 `post_topics.json`；只往 `hotspot.db` 写入候选。新热点入库后由 post_once 在发帖时挑选。
- `post_topics.json` 中遗留的 `source=hotspot && status=pending` 条目，会在 postable_pool 首次被调用时自动改为 `status=skipped, skip_reason=migrated_to_db_pool`（幂等、自动、无需手动脚本）。
- hotspot pool 在 post_once 标记 `status="skipped"` 时也写入 `posted_at`（LLM 内容审查拒绝是永久性的，避免下次重新挑中同一条烧 LLM 成本）；瞬态的 `send_failed` 仍不写入，保留重试机会。
- `/status` 文案变更：原"队列 pending/used/skipped"扩展为"人工待发/已用/跳过 + 热点池(24h) + 今日新发现/已发热点"。

#### 行为变更总结

- 发现侧：每次跑 `discover_hotspots`，所有 relevant 候选都进库，**不**截顶到 3 条；旧的"discover 写 3 条到 JSON 队列"行为消失。
- 发帖侧：每次跑 `post_once`，若人工/Telegram 队列为空，从 hotspot.db 实时选当下最佳；若主题与当天已发重复，回落到 auto_topic。

### Added
- Add a SQLite-backed job queue for daemon tasks with durable status, log files, timeout handling, and queue-aware Telegram status
- Add DeepSeek V4 Flash configuration with selectable thinking mode and cost estimates
- Add browser dependency bootstrap scripts for Chrome CDP and browser-harness deployment
- Add /config Telegram commands to view, edit, confirm, apply, and rollback .env-based bot configuration
- Add /update Telegram command to pull latest code, compile-check, restart, health-check, and report the result
- Add /review and /rate Telegram commands for human feedback scoring on replies and proactive posts
- Feedback scores injected into reply and post generation prompts as style reference

### Changed
- Update README with current architecture, job data flow, and operator runbook
- Manage the production daemon with systemd instead of tmux
- Lower noisy hotspot selector invalid-index payload logs to debug
- Run 24h revisit once per night at Beijing 00:00 and allow scheduled replies during all other hours
- Raise default proactive posting to four slots per day
- Tighten reply generation style rules for shorter, more peer-like X/Twitter replies
- Run hotspot discovery once per day, queue only the top 3 unseen hot candidates for that day's posts
- Expand default hotspot discovery to all implemented sources and evaluate 30 candidates per run so daily discovery can fill three topics
- Label Telegram hotspot discovery counts as evaluated candidates and include filtered examples when nothing is queued
- Speed up hotspot discovery with fast default sources, concurrent fetching, and source-duration diagnostics
- Rework hotspot sources around HN, Product Hunt, Reddit, and HuggingFace with PRD-weighted local ranking before LLM review
- Support Product Hunt API Key/Secret client-credentials auth for hotspot discovery
- Limit Product Hunt hotspot candidates to the latest 24h launches

### Fixed
- Fix reply URL resolution after sending replies and report daemon lifecycle through systemd
- Skip old reply records without `reply_url` during 24h revisit instead of scanning original threads
- Record each sent reply URL for direct 24h revisit while keeping the original post URL in reply de-duplication
- Enforce reply language from the main post so English posts do not get Chinese replies
- Prevent high-priority AI workflow topics from being filtered out solely by an under-scored LLM result
- Keep `/update` outside the durable job queue so the updater can restart the daemon without self-interruption, and preserve completed job status during shutdown
- Prevent a hung daemon job from blocking the scheduler indefinitely, and label overdue `/status` slots as pending recalculation
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
