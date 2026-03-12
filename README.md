# ClawFlow

[中文说明](README_zh.md)

ClawFlow is a Python-based personal AI assistant runtime for local and
self-hosted use.

This repository is **based on the NanoClaw project** and the
`nanoclaw` baseline, then substantially rewritten and extended with a
broader runtime, workflow, channel, and operator surface.

This is **not** the upstream NanoClaw repository. It is a derivative project
with a different scope and a much thicker built-in platform layer.

## What This Repository Adds

Compared with the baseline NanoClaw codebase, this repository adds:

- A persistent task runtime with priorities, retries, dead letters, watchdogs,
  queue metrics, and replay.
- Workflow evaluation and recommendation layers with structured
  `failure_classes`, `attention_reasons`, `follow_up_actions`, and feedback.
- Built-in Feishu support with webhook delivery, `/paper`, chat-scoped
  `/schedule`, `/feedback`, and `/workflow` command families.
- A richer gateway and channel platform with lifecycle contracts, diagnostics,
  desired-state orchestration, and operator actions.
- Manifest-driven extensions, local install/update flows, signed bundle
  support, and runtime isolation for extension kinds.
- Boundary audit, secret-access audit, policy contracts, and operator-visible
  security metrics.
- A local dashboard and stronger CLI operator surfaces.
- Controlled persona and prompt evolution through reviewed persona fragments.

## Current Capabilities

- Channels:
  - Telegram
  - Feishu
  - Console
- Web search:
  - RSS
  - Brave
  - Serper
  - SearXNG
- Runtime:
  - background tasks
  - scheduled jobs
  - task replay
  - queue health and alerts
- Workflow:
  - evaluation and recommendation
  - collaboration roles
  - recovery paths
  - chat-scoped workflow feedback
- Security:
  - file guard
  - shell sandbox
  - subprocess isolation
  - optional stronger sandbox backends
  - boundary and secret-access auditing
- Operations:
  - dashboard
  - status and doctor commands
  - channel contracts and diagnostics
  - extension inventory and verification

## Quick Start

### Requirements

- Python 3.11+
- `pip`
- one supported LLM provider

### Install

```bash
git clone <your-repo-url>
cd nanoClaw
pip install -e ".[dev]"
```

### Initialize

```bash
nanoclaw init
```

This creates local user configuration under `~/.nanoclaw/`.

### Run

```bash
nanoclaw serve
```

Useful commands:

```bash
nanoclaw status -v
nanoclaw doctor
make test
```

## Feishu

Feishu is a built-in webhook channel in this repository.

Highlights:

- targeted proactive delivery
- chat-scoped scheduled digests
- paper-search command templates
- workflow report and feedback commands
- in-chat confirmation flow

See:

- [FEISHU.md](FEISHU.md)
- [FEISHU_zh.md](FEISHU_zh.md)

## Repository Layout

| Path | Purpose |
| --- | --- |
| `nanoclaw/` | main application code |
| `tests/` | test suite |
| `assets/` | maintained data assets |
| `config.example.json` | example config |
| `ARCHITECTURE.md` | architecture source of truth |
| `BACKLOG.md` | hardening and deferred work |

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [BACKLOG.md](BACKLOG.md)
- [FEISHU.md](FEISHU.md)
- [FEISHU_zh.md](FEISHU_zh.md)

## Attribution

This project is based on the NanoClaw idea and upstream code lineage, but it
has been significantly rewritten and expanded.

If you redistribute or fork this repository, keep the upstream attribution
clear and make it explicit that this is a modified derivative rather than the
upstream NanoClaw project.

## License

See [LICENSE](LICENSE).
