# Feishu Channel Guide

Updated: 2026-03-11

This document explains what the built-in Feishu channel can do today, how to
configure it, and how to use its chat-facing command surfaces.

## What It Is

The Feishu channel is a built-in webhook channel.

It is not only a transport layer.
It also includes:

- chat-scoped proactive delivery
- targeted proactive delivery
- in-chat confirmation flow
- paper workflow command templates
- chat-scoped schedule management
- workflow feedback and recommendation inspection

Relevant files:

- `nanoclaw/channels/feishu.py`
- `nanoclaw/channels/feishu.plugin.json`
- `tests/test_feishu_channel.py`

## Core Characteristics

| Capability | Status | Notes |
| --- | --- | --- |
| Incoming messages | supported | text messages only |
| Delivery mode | webhook | local HTTP listener |
| Proactive delivery | supported | via `defaultChatId` |
| Targeted proactive delivery | supported | via explicit `chat_id` |
| Confirmation flow | supported | `yes <id>` / `no <id>` |
| Workflow inspection | supported | `/workflow ...` family |
| Chat-scoped feedback | supported | `/feedback ...` |
| Chat-scoped schedules | supported | `/schedule ...` family |
| Paper template | supported | `/paper ...` |
| Event dedupe | supported | recent event id cache |
| Sender allowlist | supported | `open_id`, `user_id`, or `union_id` |
| Encrypted callback | not supported | must stay disabled in Feishu |
| Non-text inbound media | not supported | ignored today |
| Rich cards | not supported | replies are plain text |

## Configuration

Use `channels.feishu` in `~/.nanoclaw/config.json`.

Minimal example:

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

### Key Fields

| Field | Required | Purpose |
| --- | --- | --- |
| `enabled` | yes | enable the Feishu channel |
| `appId` | yes | Feishu app id |
| `appSecret` | yes | Feishu app secret |
| `verifyToken` | recommended | event subscription verification |
| `webhookHost` | yes | local bind host |
| `webhookPort` | yes | local bind port |
| `webhookPath` | yes | callback path |
| `allowFrom` | optional | sender allowlist |
| `defaultChatId` | optional | default target for proactive pushes |

## Minimal Feishu Bot Setup

Use this as the shortest end-to-end setup path.

1. Create a self-built app in Feishu Open Platform.
2. Enable the bot capability for that app.
3. Add the message and event subscription permissions your workspace requires.
4. Configure event subscription and point it to your public callback URL.
5. Set the verification token in Feishu and copy the same value to
   `channels.feishu.verifyToken`.
6. Fill `appId`, `appSecret`, `verifyToken`, `webhookHost`, `webhookPort`, and
   `webhookPath` into `~/.nanoclaw/config.json`.
7. Expose the local webhook through a public HTTPS endpoint, then add the bot to
   the target chat or group.
8. If you want default proactive delivery, capture a `chat_id` from an incoming
   event and set it as `defaultChatId`.
9. If you want to restrict who can talk to the bot, add sender ids to
   `allowFrom`.

### Common Pitfalls

- Keep callback encryption disabled. The current implementation rejects encrypted
  callbacks.
- The Feishu channel only accepts inbound text messages today. Images, files,
  and other media are ignored.
- `defaultChatId` only affects default proactive delivery. Chat-scoped
  `/schedule` jobs still reply to the chat where the command was created.

## Webhook Alignment

When exposing the local webhook to Feishu, these values must match exactly.

- Feishu callback URL:
  - `https://<public-domain>/feishu/events`
- Local config path:
  - `channels.feishu.webhookPath = "/feishu/events"`
- Verification token:
  - Feishu console value must equal `channels.feishu.verifyToken`

Important:

- encrypted callbacks must be disabled
- if tokens do not match, nanoClaw returns `403 invalid token`
- only text message events are processed

## Message Handling Model

The Feishu channel does this on inbound text events:

1. verify callback token if configured
2. reject encrypted events
3. dedupe by event id
4. apply sender allowlist
5. extract plain text
6. try confirmation reply consumption
7. try workflow command handling
8. try feedback command handling
9. try schedule command handling
10. try paper template rewrite
11. route final text into the shared gateway / agent loop

Session identity is chat-aware:

- session key is effectively `feishu:<chat_id>:<user_id>`

This is useful because one user in multiple Feishu chats does not collapse into
one shared conversation state.

## Built-in Capabilities

### 1. Plain Chat

Regular text messages can be routed into the agent loop.

The channel replies:

- directly to the message when `message_id` is available
- otherwise to the chat

Long replies are split automatically.

### 2. Proactive Delivery

There are two proactive paths:

- default proactive:
  - sends to `channels.feishu.defaultChatId`
- targeted proactive:
  - sends to a specific `chat_id`

This is one of the main differences from the built-in Telegram channel, which
mainly broadcasts proactive messages to configured recipients.

### 3. In-chat Confirmation

Dangerous actions can ask for confirmation inside Feishu.

Format:

```text
yes <ID>
no <ID>
```

Properties:

- confirmation expires on timeout
- only the original user can answer
- reply must come from the original chat

### 4. `/paper` Template

The Feishu channel has a stable command template for paper search.

Syntax:

```text
/paper <topic> [--days N] [--max N] [--providers LIST] [--sort MODE]
       [--author NAME] [--institution NAME] [--categories CATS]
```

Example:

```text
/paper video generation acceleration --days 7 --max 6 --providers arxiv,openalex --sort impact
```

What it does:

- rewrites the request into a more deterministic `paper_search`-first prompt
- reduces tool-argument trial and error inside the agent loop

### 5. Chat-scoped `/schedule` Commands

This is one of the strongest parts of the Feishu channel.

Supported patterns:

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

Also supported:

- `--workdays`
- `--weekly WEEKDAY`
- `--mute START-END`
- batch list/pause/resume/remove by `health`
- batch list/pause/resume/remove by `signal`

Examples:

```text
/schedule daily 08:30 AI infra --workdays
/schedule hotspot 09:00 robotics --weekly fri --max 5
/schedule paper 09:00 video generation acceleration --days 7 --max 6 --providers arxiv,openalex
/schedule pause attention
/schedule resume signal recovery
/schedule list signal recovery
```

Important behavior:

- created jobs are chat-scoped
- delivery goes back to the originating Feishu chat
- you do not need `defaultChatId` for this chat-scoped path

### 6. Chat-scoped Feedback

The Feishu channel can write direct user feedback into the workflow evaluation
layer.

Syntax:

```text
/feedback positive
/feedback neutral
/feedback negative
```

This applies to:

- the latest workflow run in the current Feishu chat session

There are also a few supported short phrases, for example:

- `这个回答不错`
- `这个回复还行`
- `这个结果不满意`

### 7. Workflow Inspection

The Feishu channel exposes workflow inspection directly in chat.

Supported commands:

```text
/workflow report [--days N] [--limit N] [--status STATUS] [--feedback SIGNAL]
/workflow recent [--limit N] [--label LABEL] [--feedback SIGNAL]
/workflow feedback <RUN_ID> <SIGNAL>
/workflow suggest <RUN_ID>
```

Examples:

```text
/workflow report
/workflow report --status attention
/workflow report --feedback negative
/workflow recent --limit 5
/workflow recent --label poor --feedback negative
/workflow feedback 42 negative
/workflow suggest 42
```

Useful shortcut behavior:

- after `/workflow recent` or `/workflow report`, the chat remembers that last list
- follow-ups such as `把第一条展开` or `给第二条差评` can reuse the cached list
- a few high-confidence natural-language rewrites are supported

## Typical Usage Patterns

### Daily digest to current Feishu chat

```text
/schedule daily 08:30 AI infra --workdays --channels ai,tech --max 6
```

### Paper watch for one topic

```text
/paper multimodal agents --providers arxiv,openalex --sort impact
```

### Review workflow health

```text
/workflow report --status attention
/workflow recent --limit 5
/feedback negative
```

### Pause problematic schedules in batch

```text
/schedule list attention
/schedule pause attention
```

## Current Limitations

These are the main current gaps.

1. Only text inbound events are processed.
2. Encrypted callbacks are not supported.
3. Replies are plain text, not rich cards.
4. No inbound file/image/document workflow yet.
5. Default proactive push still depends on `defaultChatId` unless the caller
   uses explicit targeted delivery.

## Best Practices

- Keep `verifyToken` configured and aligned exactly with Feishu console.
- Keep callback encryption disabled until the channel supports it.
- Use `allowFrom` in any shared or semi-shared Feishu environment.
- Prefer `/paper` and `/schedule` templates for stable behavior rather than
  free-form natural language when correctness matters.
- Use `/workflow recent` before `/workflow feedback <RUN_ID> ...` if you need
  precise run-level feedback.

## Suggested Next Improvements

The most valuable next steps would be:

1. rich Feishu cards for workflow and schedule views
2. support for encrypted callbacks
3. inbound file/document handling
4. chat registration and default-target management flow
5. progress updates for long-running tasks
