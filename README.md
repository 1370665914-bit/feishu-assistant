# 飞书私人助手（文字版）

一个跑在你 Mac 上的飞书私聊 → AI 模型的**安全桥**：把飞书私聊文字消息转发给模型做问答与写作，带完整的安全边界、配额与留痕。

**核心定位**：只做"消息转发 + 模型问答"，不执行业务动作。模型侧关闭全部工具入口、只读沙箱，每天有限额，消息有去重与队列。

## 功能与边界

- 文字问答、写作，保留最近最多 6 轮上下文（同时有字符数上限）。
- 命令：`/help`、`/status`、`/new`、`/cancel`。
- SQLite 消息去重、串行任务队列、发送 outbox、最多 3 次发送尝试；使用同一幂等键。
- 每日（本机 Asia/Shanghai）最多 30 个模型任务，包括失败或取消的任务；最大在途 5 条；提交间隔至少 3 秒。
- 每条最多 4000 字，模型任务超时 180 秒，排队 10 分钟过期。
- 收到过期消息（10 分钟以上）不执行；离线期间飞书是否补推由上游决定，不保证补齐所有消息。
- 重启时 running 任务标为 interrupted，不自动重新调用模型；queued 任务仍受过期检查。
- 发送失败记录在 outbox；不跨越 1 小时的上游幂等窗口无限重发。不宣称严格 exactly-once。
- 模型禁用 shell/unified_exec、MCP 配置、apps、plugins、hooks、浏览器、电脑操作、子 agent、Web search 等入口，并使用 read-only 沙箱。
- **没有接入任何账单、OA、邮箱、文件或其他飞书业务技能，不会执行业务动作。**未来需逐项实现授权与确认，不能简单切换 full-access。
- 聊天正文保存在本机私有目录中的 SQLite 中；处理正文会发送到模型服务。每小时清理超过 7 天的正文；`/new` 清除已有任务的对话正文和旧回复正文。保留任务元数据以维持配额和去重。
- 运行日志仅记录状态和错误类别，不记录消息、模型原始 stderr 或密钥。

## 快速开始

1. 在飞书开放平台创建应用，开通机器人，订阅事件 `im.message.receive_v1`，scope：`im:message.p2p_msg:readonly`（收）、`im:message:send_as_bot`（发）。
2. 复制 `config.example.json` 为 `config.json`，填入你的 `app_id`、`owner_id`（你的 open_id）、`chat_id`（绑定私聊）、CLI 路径等。
3. 登录 CLI（见下方"排错"），确保模型侧（Codex）已登录。
4. 启动服务（见"运维"），向机器人私聊发送 `/status` 验证在线。

`config.json` 属于你的私密配置（含账号级 ID），不要提交到仓库——仓库只提供 `config.example.json` 模板。

## 配置

```json
{
  "profile": "feishu-assistant",
  "app_id": "<APP_ID>",
  "owner_id": "<OWNER_OPEN_ID>",
  "chat_id": "<BOUND_CHAT_ID>",
  "lark_bin": "<HOME>/.local/bin/lark-cli",
  "codex_bin": "<HOME>/.local/bin/codex",
  "codex_home": "<HOME>/.codex",
  "task_timeout": 180,
  "daily_limit": 30,
  "queue_limit": 5,
  "max_input_chars": 4000,
  "history_pairs": 6
}
```

- 仅接受 config.json 中绑定的 `owner_id` + `chat_id` 的 p2p user 文字消息。
- 使用独立的飞书 profile（`feishu-assistant`），显式调用 lark-cli，不切换默认 profile。
- 模型复用本机 Codex 登录；不复制密钥，不加载该目录的 config.toml。
- 工作目录：本项目的空 `workspace/`；当前聊天窗口的上下文不会自动迁移。

## 运维

服务为当前用户的 macOS LaunchAgent（无需 root）：`com.<USER>.feishu-assistant`。
登录后启动、异常退出自动重启；电脑必须开机、联网且未睡眠。
未修改睡眠设置，不支持关机后执行；退出登录后 LaunchAgent 停止。
日志：`state/service.log`，1 MB 轮转，最多 3 个备份。

查看状态：

```sh
python3 <PROJECT_DIR>/bridge.py status
launchctl print gui/$(id -u)/com.<USER>.feishu-assistant
```

停止本次登录会话中的服务（下次登录会再次自动启动）：

```sh
launchctl bootout gui/$(id -u)/com.<USER>.feishu-assistant
```

重新启动：

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<USER>.feishu-assistant.plist
```

永久禁用自动启动（先停服务，再禁用）：

```sh
launchctl bootout gui/$(id -u)/com.<USER>.feishu-assistant
launchctl disable gui/$(id -u)/com.<USER>.feishu-assistant
```

再次启用时先 `launchctl enable gui/$(id -u)/com.<USER>.feishu-assistant`，再 bootstrap。

测试：

```sh
cd <PROJECT_DIR>
python3 -m unittest -v
```

## 排错

- Bot 登录：`lark-cli --profile feishu-assistant auth status --json --verify`。
- 模型登录：`CODEX_HOME=<HOME>/.codex <HOME>/.local/bin/codex login status`。
- 接收事件 `im.message.receive_v1`，bot scope `im:message.p2p_msg:readonly`；发送 scope `im:message:send_as_bot`。
- 断连会指数退避重启监听，CLI 自身也负责 WebSocket 重连。stderr 的 ready 表示本地订阅已就绪，不证明未来网络始终正常。
- 当前仅一个接收器；不要同时启动其他相同应用的长连接实例。
- 机器重装、CLI 升级、登录失效之后需要重新验证。没有自动更新 CLI 或扩大权限。
