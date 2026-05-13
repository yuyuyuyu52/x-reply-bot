# 部署教程

这份文档面向第一次安装、第一次使用。目标是：用户有一台 Linux 服务器后，按步骤把 bot 跑起来；之后更新只需要在 Telegram 发送 `/update`。


## 一、你需要准备什么

### 服务器

推荐：

- Linux VPS
- Ubuntu / Debian 系统更省事
- 能运行图形版 Chrome 或 Chromium

项目现在提供浏览器依赖自动安装脚本，会尽量安装：

- `git` / `python3` / `tmux` / `util-linux`
- Chrome
- `uv`
- `browser-harness`

如果不是 Ubuntu / Debian，可能需要手动安装这些基础依赖。


### 浏览器和 X 登录态

这个 bot 不是用 X API，而是控制一个已经登录的 Chrome。安装可以自动化，但 X 登录态不能可靠自动化。

服务器上仍然需要：

1. 一个能显示图形界面的桌面、VNC 或 Xvfb 环境
2. 一个持久化 Chrome profile
3. 在这个 Chrome profile 里人工登录目标 X 账号一次

默认 profile 目录：

```bash
$HOME/.config/x-reply-bot-chrome
```

启动 Chrome CDP 的脚本：

```bash
bash scripts/start_chrome.sh
```

第一次启动后，在打开的 Chrome 里登录 X。后续只要 profile 目录保留，登录态会复用。


### browser-harness

项目所有浏览器操作都通过 `browser-harness` 完成。

首次部署时运行：

```bash
bash scripts/bootstrap_browser.sh
```

这个脚本会使用仓库自带的 `vendor/browser-harness` 安装 repo-local 可执行入口 `.bin/browser-harness`，并在 `.env` 里补齐默认浏览器配置。


### LLM API

需要一个兼容 `/chat/completions` 的 API。

必填：

```bash
X_REPLY_BASE_URL=
X_REPLY_API_KEY=
X_REPLY_MODEL=
```

DashScope 示例：

```bash
X_REPLY_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
X_REPLY_API_KEY="你的 key"
X_REPLY_MODEL="qwen3.5-flash"
```

注意：`qwen3.5-flash` 中间有点，不能写成 `qwen3.5flash`。


## 二、首次安装

进入你想安装 bot 的目录：

```bash
cd ~
git clone <你的仓库地址> x-reply-bot
cd x-reply-bot
```

安装浏览器依赖和 browser-harness：

```bash
bash scripts/bootstrap_browser.sh
```

启动 Chrome CDP：

```bash
bash scripts/start_chrome.sh
```

第一次启动后，在 Chrome 里登录 X。

编辑 `.env`：

```bash
nano .env
```

最少需要填这些：

```bash
X_REPLY_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
X_REPLY_API_KEY="你的 key"
X_REPLY_MODEL="qwen3.5-flash"
```

如果要用 Telegram 控制，再填：

```bash
X_REPLY_TG_BOT_TOKEN="你的 Telegram bot token"
X_REPLY_TG_CHAT_ID="你的 Telegram chat id"
```

`.env` 不要提交到 git。服务器上的 `.env` 是这台服务器自己的配置。


## 三、首次启动前检查

### 检查 Chrome CDP

推荐直接运行：

```bash
bash scripts/status_browser.sh
```

它会检查 Chrome CDP、`browser-harness` 可执行文件和 harness 项目目录。

也可以手动检查 `127.0.0.1:9222`：

```bash
curl http://127.0.0.1:9222/json/version
```

能看到 JSON，并且里面有 `webSocketDebuggerUrl`，说明 CDP 可用。


### 检查 Python 能导入项目

```bash
python3 -m compileall -q bot_daemon.py discover_hotspots.py post_once.py post_topics.py run_once.py sync_tg_commands.py src
```

没有输出通常表示通过。


### 检查 Telegram 命令菜单

如果配置了 Telegram：

```bash
python3 sync_tg_commands.py
```

成功后，Telegram 菜单里应能看到 `/run`、`/status`、`/update`、`/config`、`/post_once` 等命令。


## 四、第一次启动

启动后台 bot：

```bash
bash scripts/start_bot.sh
```

查看状态：

```bash
bash scripts/status_bot.sh
```

查看日志：

```bash
tail -f state/logs/bot.log
```

停止 bot：

```bash
bash scripts/stop_bot.sh
```


## 五、第一次使用

建议按这个顺序测试。

### 1. Telegram 状态

在 Telegram 发送：

```text
/status
```

如果能收到状态回复，说明 Telegram 控制链路正常。


### 2. 主动发帖 dry run

先不要真实发帖，测试生成：

```text
/post_dry_run
```

或者服务器上运行：

```bash
python3 post_once.py --dry-run
```


### 3. 观察学习

测试浏览器读取 feed：

```text
/learn_once
```

或者服务器上运行：

```bash
python3 src/learning/observe.py
```


### 4. 回复流程

确认 Chrome 登录态、CDP、browser-harness 都没问题后，再测试：

```text
/run
```

或者服务器上运行：

```bash
python3 run_once.py
```


## 六、以后怎么更新

第一次安装 `/update` 功能前，需要手动部署一次代码。

之后更新流程变成：

1. 开发者本地提交并推送：

```bash
git add .
git commit -m "更新说明"
git push
```

2. 在 Telegram 发送：

```text
/update
```

服务器会自动：

- `git pull --ff-only`
- 做 Python 编译检查
- 同步 Telegram 命令菜单
- 重启 bot
- 检查 bot 是否重新运行
- 成功或失败都发 Telegram 通知

更新日志在：

```text
state/logs/update.log
```


## 七、以后怎么改配置

Telegram 支持直接查看和修改 `.env` 配置。

常用命令：

```text
/config list
/config get X_POST_DAILY_LIMIT
/config set X_POST_DAILY_LIMIT 3
/config unset X_POST_DAILY_LIMIT
/config pending
```

低风险配置会直接保存到 `.env`，然后自动检查并重启 bot。

敏感配置需要二次确认，例如模型、API key、CDP 地址、browser-harness 路径：

```text
/config set X_REPLY_MODEL qwen3.5-flash
```

bot 会返回一个确认 ID，再发送：

```text
/config confirm <id>
```

取消：

```text
/config cancel <id>
```

配置生效日志在：

```text
state/logs/config_apply.log
```

如果配置导致重启失败，bot 会尝试恢复 `.env` 备份并重新启动。


## 八、生产数据和备份

这些文件和目录是生产数据，不应该被覆盖：

```text
.env
state/
```

其中常见重要文件：

```text
state/post_topics.json
state/persona.json
state/learning.db
state/history/
state/post_history/
state/logs/
```

迁移服务器前，至少备份：

```bash
tar -czf x-reply-bot-state-backup.tgz .env state
```


## 九、常见问题

### `/update` 没出现在 Telegram 菜单

在服务器运行：

```bash
python3 sync_tg_commands.py
```


### `/config` 没出现在 Telegram 菜单

在服务器运行：

```bash
python3 sync_tg_commands.py
```


### `/config` 修改配置后失败

先看日志：

```bash
tail -n 200 state/logs/config_apply.log
```

失败时脚本会尝试恢复 `.env` 备份。不要直接删除 `.env` 或 `state/`。


### `/update` 失败，提示 git pull 失败

通常是服务器上有本地改动，或者远程分支不能 fast-forward。

先看日志：

```bash
tail -n 200 state/logs/update.log
```

不要直接删除 `.env` 或 `state/`。它们是生产配置和生产数据。


### bot 启动了，但浏览器相关任务失败

优先检查：

```bash
curl http://127.0.0.1:9222/json/version
```

然后检查 `.env` 里的：

```bash
X_REPLY_CDP_URL
BROWSER_HARNESS_BIN
BROWSER_HARNESS_ROOT
```


### Telegram 没回复

检查 `.env`：

```bash
X_REPLY_TG_BOT_TOKEN
X_REPLY_TG_CHAT_ID
```

然后重启：

```bash
bash scripts/stop_bot.sh
bash scripts/start_bot.sh
```
