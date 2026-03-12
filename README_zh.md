# ClawFlow

[English](README.md)

ClawFlow 是一个面向本地和自托管场景的 Python 个人 AI assistant runtime。

这个仓库**基于 NanoClaw 项目改写而来**，以 `nanoclaw` 为基线，
随后在运行时、工作流、通道平台、运维面和扩展模型上做了大量增强。

它**不是**上游 NanoClaw 原仓库，而是一个范围更大、内建平台层更厚的衍生版本。

## 这个仓库新增了什么

相对基线 NanoClaw，这个仓库新增了：

- 持久化任务运行时：支持优先级、重试、死信、watchdog、队列指标和回放。
- 工作流评估与推荐：带结构化的 `failure_classes`、`attention_reasons`、
  `follow_up_actions` 和用户反馈回写。
- 内建飞书支持：包括 webhook、`/paper`、chat-scoped `/schedule`、
  `/feedback`、`/workflow` 等命令面。
- 更完整的 gateway / channel 平台：包括 lifecycle contract、diagnostics、
  desired-state orchestration 和 operator action。
- Manifest 驱动的扩展模型：支持本地 install/update、signed bundle 和
  按扩展类型做运行时隔离。
- 边界审计与 secret-access 审计：带 policy contract 和 operator 可见的
  安全指标。
- 本地 dashboard 和更强的 CLI operator surface。
- 受控的 persona / prompt 演化机制，通过 reviewed persona fragment 生效。

## 当前已有能力

- 通道：
  - Telegram
  - Feishu
  - Console
- 搜索：
  - RSS
  - Brave
  - Serper
  - SearXNG
- 运行时：
  - 后台任务
  - 定时任务
  - 任务回放
  - 队列健康和告警
- 工作流：
  - 评估与推荐
  - 多角色协作
  - 恢复路径
  - chat-scoped workflow feedback
- 安全：
  - file guard
  - shell sandbox
  - subprocess isolation
  - optional stronger sandbox backend
  - boundary / secret-access 审计
- 运维：
  - dashboard
  - status / doctor 命令
  - channel contract 与 diagnostics
  - extension inventory / verify

## 快速开始

### 环境要求

- Python 3.11+
- `pip`
- 一个可用的 LLM provider

### 安装

```bash
git clone <your-repo-url>
cd nanoClaw
pip install -e ".[dev]"
```

### 初始化

```bash
nanoclaw init
```

这会在 `~/.nanoclaw/` 下生成本地用户配置。

### 启动

```bash
nanoclaw serve -v
```

常用命令：

```bash
nanoclaw status
nanoclaw doctor
make test
```

## 飞书

飞书在这个仓库里是一个内建 webhook channel，不只是普通消息入口。

它目前支持：

- 定向主动推送
- chat-scoped 定时摘要
- 论文检索命令模板
- workflow 报告和反馈命令
- chat 内确认交互

详见：

- [FEISHU.md](FEISHU.md)
- [FEISHU_zh.md](FEISHU_zh.md)

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `nanoclaw/` | 主应用代码 |
| `tests/` | 测试 |
| `assets/` | 可维护数据资源 |
| `config.example.json` | 配置示例 |
| `ARCHITECTURE.md` | 架构主文档 |
| `BACKLOG.md` | hardening 与延后事项 |

## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [todo.md](todo.md)
- [BACKLOG.md](BACKLOG.md)
- [FEISHU.md](FEISHU.md)
- [FEISHU_zh.md](FEISHU_zh.md)

## 署名说明

这个项目基于 NanoClaw 的思路和上游代码脉络演化而来，但已经做了大量改写和扩展。

如果你继续分发或 fork 这个仓库，建议保留对上游的清晰署名，并明确说明这是一个修改过的衍生版本，而不是上游原仓库。

## License

见 [LICENSE](LICENSE)。
