# 飞书 Channel 使用说明

更新日期: 2026-03-11

本文说明当前内建飞书 channel 的能力、配置方式、使用方法和现有限制。

## 它是什么

飞书 channel 是一个内建的 webhook 通道。

它不只是 transport layer，还内建了几类业务能力：

- chat-scoped proactive delivery
- targeted proactive delivery
- chat 内确认交互
- paper workflow 命令模板
- chat-scoped schedule 管理
- workflow feedback 和 recommendation 查看

相关文件：

- `nanoclaw/channels/feishu.py`
- `nanoclaw/channels/feishu.plugin.json`
- `tests/test_feishu_channel.py`

## 核心特征

| 能力 | 当前状态 | 说明 |
| --- | --- | --- |
| 消息接入 | 支持 | 目前只处理文本消息 |
| 投递模式 | webhook | 本地 HTTP listener |
| 默认主动推送 | 支持 | 依赖 `defaultChatId` |
| 定向主动推送 | 支持 | 可直接指定 `chat_id` |
| 确认交互 | 支持 | `yes <id>` / `no <id>` |
| Workflow 查看 | 支持 | `/workflow ...` |
| Chat-scoped feedback | 支持 | `/feedback ...` |
| Chat-scoped schedule | 支持 | `/schedule ...` |
| `/paper` 模板 | 支持 | 走更稳定的 paper prompt path |
| Event 去重 | 支持 | 最近 event id dedupe |
| Sender 白名单 | 支持 | `open_id` / `user_id` / `union_id` |
| 加密回调 | 不支持 | 飞书侧必须关闭 callback encryption |
| 非文本入站媒体 | 不支持 | 当前会忽略 |
| 富卡片 | 不支持 | 当前回复仍以纯文本为主 |

## 配置方式

在 `~/.nanoclaw/config.json` 里配置 `channels.feishu`。

最小示例：

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "verifyToken": "xxx",
      "encryptKey": "",
      "webhookHost": "0.0.0.0",
      "webhookPort": 15097,
      "webhookPath": "/feishu/events",
      "allowFrom": ["ou_xxx"],
      "defaultChatId": ""
    }
  }
}
```

### 关键字段

| 字段 | 是否必需 | 用途 |
| --- | --- | --- |
| `enabled` | 是 | 启用飞书 channel |
| `appId` | 是 | 飞书应用 app id |
| `appSecret` | 是 | 飞书应用 app secret |
| `verifyToken` | 建议 | 事件订阅校验 |
| `webhookHost` | 是 | 本地监听 host |
| `webhookPort` | 是 | 本地监听端口 |
| `webhookPath` | 是 | 回调路径 |
| `allowFrom` | 可选 | sender 白名单 |
| `defaultChatId` | 可选 | 默认主动推送目标 |

## 飞书机器人最小安装方案

如果你只想走最短路径，把机器人跑起来，按下面这几步做就够了。

1. 在飞书开放平台创建一个企业自建应用。
2. 给这个应用开启机器人能力。
3. 补齐消息接收和事件订阅相关权限。
4. 在飞书后台配置事件订阅，并把回调地址指向你的公网 HTTPS URL。
5. 在飞书后台设置 verification token，并把同一个值填到
   `channels.feishu.verifyToken`。
6. 在 `~/.nanoclaw/config.json` 里填好 `appId`、`appSecret`、
   `verifyToken`、`webhookHost`、`webhookPort`、`webhookPath`。
7. 把本地 webhook 暴露到公网后，把机器人加入目标群聊或会话。
8. 如果你希望它有默认主动推送目标，先让目标会话给机器人发一条消息，
   再从事件 payload 里取到 `chat_id`，填到 `defaultChatId`。
9. 如果你希望限制谁能和机器人交互，再把对应 sender id 配到
   `allowFrom`。

### 常见坑

- 飞书侧必须关闭 callback encryption。当前实现不支持加密回调。
- 当前只接收文本消息。图片、文件、语音等非文本消息会被忽略。
- `defaultChatId` 只影响默认 proactive 推送，不影响 chat-scoped 的
  `/schedule` 回投行为。

## Webhook 对齐要求

把本地 webhook 暴露给飞书时，这几个值必须完全对齐：

- 飞书事件订阅回调 URL：
  - `https://<public-domain>/feishu/events`
- 本地配置路径：
  - `channels.feishu.webhookPath = "/feishu/events"`
- 验证 token：
  - 飞书控制台里的值必须等于 `channels.feishu.verifyToken`

重要说明：

- 必须关闭 encrypted callback
- token 不一致时，nanoClaw 会返回 `403 invalid token`
- 当前只处理文本消息事件

## 消息处理流程

飞书文本事件进来后，channel 目前大致按这个顺序处理：

1. 校验 callback token
2. 拒绝 encrypted event
3. 基于 event id 去重
4. 应用 sender allowlist
5. 提取文本内容
6. 尝试消费 confirmation reply
7. 尝试处理 workflow 命令
8. 尝试处理 feedback 命令
9. 尝试处理 schedule 命令
10. 尝试做 `/paper` 模板改写
11. 把最终文本送入共享 gateway / agent loop

Session 是 chat-aware 的：

- session key 实际上是 `feishu:<chat_id>:<user_id>`

这意味着同一个用户在不同飞书 chat 里的上下文不会混在一起。

## 已有能力

### 1. 普通聊天

普通文本消息可以直接进入 agent loop。

回复方式：

- 有 `message_id` 时，优先 reply 到原消息
- 否则直接发到 chat

长消息会自动分块。

### 2. 主动推送

目前有两条主动推送路径：

- 默认主动推送：
  - 发到 `channels.feishu.defaultChatId`
- 定向主动推送：
  - 发到显式指定的 `chat_id`

这也是它和内建 Telegram channel 的一个核心差异：

- 飞书支持面向具体 chat 的 targeted proactive
- Telegram 目前更偏向对 allowlist recipient 的广播式 proactive

### 3. Chat 内确认

危险操作可以直接在飞书里确认。

格式：

```text
yes <ID>
no <ID>
```

特点：

- 确认会超时
- 只有原用户能回答
- 必须在原 chat 里回复

### 4. `/paper` 模板

飞书 channel 内建了稳定的 paper search 命令模板。

语法：

```text
/paper <topic> [--days N] [--max N] [--providers LIST] [--sort MODE]
       [--author NAME] [--institution NAME] [--categories CATS]
```

示例：

```text
/paper video generation acceleration --days 7 --max 6 --providers arxiv,openalex --sort impact
```

作用：

- 改写成更确定的 `paper_search`-first prompt
- 减少模型在 tool 参数选择上的试错

### 5. Chat-scoped `/schedule`

这是飞书 channel 最强的一块能力之一。

支持的基本形式：

```text
/schedule daily <HH:MM> <topic> [--channels LIST] [--max N] [--days N]
/schedule hotspot <HH:MM> <topic> [--channels LIST] [--max N]
/schedule paper <HH:MM> <topic> [--days N] [--max N] [--providers LIST]
/schedule update <JOB_ID> <same create syntax>
/schedule show <JOB_ID>
/schedule pause <JOB_ID>
/schedule resume <JOB_ID>
/schedule list
/schedule remove <JOB_ID>
```

还支持：

- `--workdays`
- `--weekly WEEKDAY`
- `--mute START-END`
- 按 `health` 批量 list / pause / resume / remove
- 按 `signal` 批量 list / pause / resume / remove

示例：

```text
/schedule daily 08:30 AI infra --workdays
/schedule hotspot 09:00 robotics --weekly fri --max 5
/schedule paper 09:00 video generation acceleration --days 7 --max 6 --providers arxiv,openalex
/schedule pause attention
/schedule resume signal recovery
/schedule list signal recovery
```

重要行为：

- 创建出来的 job 是 chat-scoped 的
- 结果会回到发起这个 schedule 的飞书 chat
- 走这条 chat-scoped 路径时，不需要 `defaultChatId`

### 6. Chat-scoped feedback

飞书可以直接把用户反馈写回 workflow evaluation。

语法：

```text
/feedback positive
/feedback neutral
/feedback negative
```

作用范围：

- 当前飞书 chat session 里最近一次 workflow run

还支持一些短句，例如：

- `这个回答不错`
- `这个回复还行`
- `这个结果不满意`

### 7. Workflow 查看

飞书里可以直接看 workflow 结果和建议。

支持命令：

```text
/workflow report [--days N] [--limit N] [--status STATUS] [--feedback SIGNAL]
/workflow recent [--limit N] [--label LABEL] [--feedback SIGNAL]
/workflow feedback <RUN_ID> <SIGNAL>
/workflow suggest <RUN_ID>
```

示例：

```text
/workflow report
/workflow report --status attention
/workflow report --feedback negative
/workflow recent --limit 5
/workflow recent --label poor --feedback negative
/workflow feedback 42 negative
/workflow suggest 42
```

有用的快捷行为：

- 执行 `/workflow recent` 或 `/workflow report` 后，当前 chat 会缓存最近那份列表
- 后续像 `把第一条展开`、`给第二条差评` 可以复用这份列表
- 还支持一些高置信的自然语言改写

## 典型用法

### 给当前飞书 chat 建一个日报

```text
/schedule daily 08:30 AI infra --workdays --channels ai,tech --max 6
```

### 对一个主题做论文监控

```text
/paper multimodal agents --providers arxiv,openalex --sort impact
```

### 查看 workflow 健康情况

```text
/workflow report --status attention
/workflow recent --limit 5
/feedback negative
```

### 批量暂停有问题的任务

```text
/schedule list attention
/schedule pause attention
```

## 当前限制

主要缺口有这几项：

1. 只处理文本入站事件
2. 不支持 encrypted callback
3. 目前回复仍以纯文本为主，不是 rich card
4. 还没有入站文件、图片、文档处理
5. 默认 proactive 仍依赖 `defaultChatId`，除非调用方显式走 targeted delivery

## 使用建议

- 在任何共享或半共享飞书环境里，都建议配置 `allowFrom`
- `verifyToken` 一定要和飞书控制台完全一致
- callback encryption 先保持关闭
- 在追求稳定时，优先用 `/paper` 和 `/schedule` 模板，而不是完全自由的自然语言
- 需要精确打分时，先用 `/workflow recent`，再用 `/workflow feedback <RUN_ID> ...`

## 最值得补的下一步

如果后面继续增强飞书 channel，最值得优先做的是：

1. rich card 版 workflow / schedule 展示
2. encrypted callback 支持
3. 入站文件和文档处理
4. chat 注册和默认目标管理流程
5. 长任务的进度更新
