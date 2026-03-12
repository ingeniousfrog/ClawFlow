# Architecture

## Overview

nanoClaw is a small asyncio service that routes channel messages (Telegram,
Feishu, or console) to an agent loop, executes tools, stores memory in SQLite,
and exposes a local dashboard.

## Runtime Flow

1. A channel (Telegram, Feishu webhook, or console) receives a message.
   - Feishu applies command templates (for example `/paper ...` and `/schedule ...`)
     before agent routing.
2. Gateway routes the message to the agent.
3. Agent builds context, selects tools, calls the LLM, and executes tools.
   - Intent routing can choose different models per request type
     (daily digest, paper, or general).
   - Grounding strategy is separate from skill selection: broad live-search queries
     may go directly to a web-enabled model, while structured workflows keep using
     local tools.
   - The system prompt is now composed from three separate sources: the fixed
     runtime rules, optional protected persona fragments loaded from
     `~/.nanoclaw/data/persona_fragments.json`, and the manually configured
     `agent.systemPrompt`.
   - Local `web_search` now runs a deterministic query planner before provider
     dispatch, so live-search requests are first classified into
     `news / web / paper / site / chinese_web / long_tail`.
   - After provider dispatch, `web_search` now normalizes heterogeneous search
     output into one evidence schema before rendering the final tool result.
   - For daily/paper requests, if evidence tools return no usable results,
     agent auto-triggers fallback web re-search via the fallback model.
   - Each completed run now writes one `workflow_runs` telemetry row plus one
     derived `workflow_evaluations` row with quality/efficiency scoring,
     evaluation label, structured `failure_classes / attention_reasons /
     follow_up_actions`, and a writable `feedback_signal`.
   - Each workflow run also gets one explicit chain-scoped
     `workflow_identity`, reused by nested runtime work when the current task
     payload already carries one.
   - The same telemetry base can now also be aggregated on demand into
     workflow-level recommendations for the recent evaluation window.
  - Workflow telemetry now also carries one lightweight collaboration plan
     (`planner / router / executor / critic / summarizer`), stable role
     handoff contracts, and one shared evidence snapshot collected from
     evidence-bearing tool output.
   - The same run now also records `workflow_role_execution` phases, so
     `planner/router` and `critic/summarizer` can be replayed as explicit
     internal execution stages instead of only existing as static role labels.
   - When a grounded run degrades, the same call chain can now also record
     `workflow_role_recovery` events, so lightweight role-level recovery is
     explicit in replay instead of only living inside implicit prompt state.
   - Those role-execution and recovery events now also carry stable checkpoint
     ids plus a small retry-budget view, so replay can show where a recovery
     would resume and how much lightweight recovery budget remained.
   - When a recovery points at a known checkpoint, the same single agent loop
     now restores `messages` and shared evidence to that checkpoint before the
     next LLM turn, so lightweight role recovery affects execution, not only
     telemetry.
   - Each role execution now also emits one explicit
     `workflow_role_checkpoint` event into `workflow_runs.call_chain`, so role
     checkpoints persist in SQLite and can be replayed later without relying
     on the in-memory runtime state that existed during the live run.
4. Memory store saves history and long-term facts.
5. Audit log records every action.
6. Scheduler and heartbeat can send proactive messages via the gateway.
   - Feishu schedule templates persist chat-scoped cron jobs with `target_id`,
     so scheduled pushes can return to the originating Feishu chat instead of
     only using `defaultChatId`.
7. Runtime foundation now initializes a persistent `tasks` table on startup.
8. `spawn_task` now persists background jobs and drains them through a small
   in-process queue while the service stays online.
9. The background queue now uses worker leases with periodic heartbeats and
   startup orphan recovery for stale `spawn_task` jobs.
10. CLI status and the local dashboard now expose recent persisted tasks so the
    runtime state is observable without opening SQLite manually.
11. Tasks now carry queue metadata (`priority`, `timeout_seconds`,
    `cancel_requested`), and the local spawn runtime reacts to cancel and
    timeout signals while claiming higher-priority work first.
12. The scheduler slice now persists retry metadata (`max_attempts`,
    `retry_backoff_seconds`, `next_attempt_at`) so failed work can return to
    `pending` and be claimed again after backoff.
13. CLI status and dashboard status now expose queue backlog and runtime
    saturation metrics (`ready backlog`, `retry backlog`, `running`, `saturation_pct`).
14. Terminal failures now enter a small dead-letter queue with persisted reason
    and timestamp, and operators can manually requeue them from CLI or dashboard.
15. Background task claiming now applies a starvation threshold so long-waiting
    ready tasks can preempt newer high-priority work, and queue status exposes
    `starved_ready_tasks` for operator visibility.
16. Background task claiming now also enforces a SQLite-backed global running cap
    for the selected claim scope, so multiple workers share one pool limit
    instead of each process applying its own local cap independently.
17. Background tasks can now share named rate-limit buckets, and claim time will
    postpone blocked tasks to the next allowed timestamp instead of stalling the
    whole queue behind one rate-limited candidate.
18. Background task execution now persists `task_steps`, so `spawn_task` can
    reuse a completed `agent_run` checkpoint when a later notify/retry cycle
    fails instead of rerunning the whole agent step.
19. Task-scoped execution now also persists `task_runs` and `tool_traces`, and
    both the CLI and dashboard API can replay a task's attempts, steps, tool
    calls, and workflow summaries from SQLite.
20. Background tasks now accept an explicit `idempotency_key`, and duplicate
    `spawn_task` submissions with the same key reuse the existing
    pending/running/succeeded task instead of queueing a second copy.
21. The runtime now exposes stale-running watchdog signals, and timeout/orphan
    recovery events are written to `audit_log` as `runtime_watchdog` entries.
22. The background runtime now summarizes queue health and can send one
    deduplicated proactive runtime alert when dead-letter or stale-running
    conditions appear.
23. Runtime health now escalates to `critical` for a `queue_stall` condition:
    ready work is aging past the configured threshold while no worker is
    running any task in the shared runtime pool.
24. Repeated identical runtime alerts now escalate their stage and severity,
    and the escalation itself is written to audit as `runtime_alert_escalation`.
25. Escalated runtime alerts can now fan out beyond the primary alert channel
    to a configured escalation channel, giving the runtime a minimal stronger
    notification target without introducing a full paging system.
26. Heartbeat no longer executes its periodic agent turn directly from the
    timer loop; it now queues a persisted `heartbeat_checklist` task into the
    shared runtime so it reuses the same lease, retry, step, and replay base
    as `spawn_task`.
27. Cron jobs now also enqueue persisted `cron_job` tasks into the shared
    runtime, so scheduled work no longer bypasses the unified lease, retry,
    checkpoint, and replay path.
28. The shared runtime now claims `spawn_task`, `heartbeat_checklist`, and
    `cron_job` through one multi-source priority path instead of a fixed source
    order, so higher-priority work is chosen globally across runtime sources.
29. When multiple runtime sources tie on priority, the claim path now prefers
    the less-saturated source before falling back to ready timestamp ordering.
30. Task replay now also includes runtime audit events such as
    `runtime_watchdog`, so timeout/cancellation interventions can be reviewed
    from the same replay bundle.
31. When one workflow run already carries a `role_task_bridge_timeline` and the
    parent run itself executes inside the background runtime, the spawn worker
    can now materialize those bridge specs into persisted `workflow_role`
    child tasks and run a minimal role worker that journals one
    `role_runtime_ack` step plus the parent-child link.
32. That minimal `workflow_role` worker now also enforces `depends_on`
    relationships across sibling role tasks: unmet dependencies defer the role
    task back to `pending`, while satisfied dependencies allow the worker to
    persist one compact structured role result into the task step journal.
33. The same role worker now also reuses collaboration-layer execution-brief
    semantics by carrying `tool_names`, `needs_grounded`, and
    `failure_reason` in the role payload, then emitting
    `handler_kind / brief_content / contract / checkpoint_id` in the
    persisted role-step output.
34. Role-runtime bridge materialization is now dependency-aware: the runtime
    only enqueues the currently ready `workflow_role` nodes, and each
    successful role task triggers enqueue of the next dependency-ready role,
    forming one minimal runtime chain
    (`planner -> router -> executor -> critic -> summarizer`) instead of
    eagerly materializing every role node up front.
35. Each background `workflow_role` task can now execute one isolated role LLM
    turn on top of the deterministic role handler, so role reasoning no longer
    has to piggyback on the parent agent loop while still preserving stable
    fallback behavior and replayable step output.
36. When a prior degraded workflow run in the same originating parent session
    produced a resumable role checkpoint, the next role-runtime bridge can now
    seed matching role tasks with that persisted resume evidence and budget
    context even if the current work is running under a different parent task id.
37. When one runtime role turn degrades, the worker can now enqueue one explicit
    recovery role task and stop the failed downstream branch from advancing,
    so role recovery no longer has to collapse back into the parent agent loop.
38. Recovery now keeps two separate identities: `resume_checkpoint_id` tells the
    live agent loop which checkpoint to rewind to, while `recovery_task_key`
    tells the runtime which role task should be re-enqueued next.
39. Each workflow chain now also carries a stable `workflow_identity`, and that
    identity is propagated through runtime context, workflow telemetry, bridge
    payloads, recovery payloads, and audit resume lookup so cross-run recovery
    no longer depends primarily on `parent_session_id`.
40. Downstream `critic / summarizer` role tasks can now be rearmed on the same
    task row when upstream recovery changes their inputs, using explicit
    turn-state fields instead of spawning a fresh task for every rerun.
41. When runtime recovery re-enters `executor@tool_phase`, the scheduler can
    now rearm the already-succeeded executor task in place for one explicit
    `recovery_reentry` turn instead of always creating a duplicate recovery row.
42. When a recovery target already exists in `pending`, the runtime can now
    refresh that queued role-task payload in place with merged recovery state
    and execution-brief context instead of leaving stale pending input behind.
43. When a recovery target already exists in `running`, the runtime now stages
    one deferred recovery refresh on that same role task and rearms it in place
    after the current turn completes instead of forking a duplicate recovery row.

## Modules

 - `nanoclaw/core`: agent loop, capability catalog, context builder, config,
   extension manifests, local extension installer, LLM client, logging.
 - `agent` supports intent-based model routing with fallback to default model.
 - `agent` also chooses between local grounding tools and direct web-model search.
  - `capabilities` now renders one user-facing catalog from shared sources:
    tool entries come from registry-backed tool metadata, skill entries come
    from skill manifests, and workflow entries come from the workflow registry.
  - `persona` owns the protected persona-fragment store plus the controlled
    reviewed-summary parser used by `nanoclaw persona show` and
    `nanoclaw persona apply-review`. That module can update only protected
    identity/style/config fragments, not the raw configured system prompt.
  - `workflows` is the source of truth for built-in workflow definitions,
    structured workflow matching terms, and configured default workflow labels.
    Built-in workflow metadata now loads from the packaged
    `nanoclaw/core/workflow_catalog.json` file instead of a hardcoded Python
    table, so tests, CLI catalog output, and workflow matching share one data
    source.
  - `extension_installer` now owns local `channel / search_provider`
    installation, signed bundle packing, install receipts, receipt
    verification, publisher revocation/key-rotation checks, and runtime trust
    policy for third-party extensions under `~/.nanoclaw/extensions`.
  - `extension_registry` now provides a lightweight remote registry/update path
    for signed extension bundles, including registry listing, bundle download,
    update checks, and bundle-to-receipt installation.
  - `extension_runtime` plus `extension_runner` now provide the first stronger
    out-of-process extension boundary: user-installed search providers plus
    proactive-only user channels can run in a dedicated subprocess with a
    stripped environment and isolated `HOME/TMP/XDG` roots.
  - Third-party extension hardening currently prefers signed distribution plus
    manifest-declared permissions and sandbox metadata. That stronger runtime
    boundary is currently implemented for `search_provider` plus proactive-only
    `channel` extensions, not yet for incoming-capable managed channels.
  - `collaboration` provides the lightweight role-plan, role-handoff contract,
    shared-evidence v1, role-recovery helpers, and stable role checkpoint ids
    used by workflow telemetry and replay.
  - For `wechat_article_flow`, that same collaboration layer now swaps in
    article-writing role semantics, so `planner/router/executor/critic/
    summarizer` are framed as angle planning, evidence routing, draft material
    assembly, fact-check gate, and publish-ready article packaging.
  - `collaboration` also now owns the in-memory role checkpoint state used to
    restore lightweight role execution inside one agent run.
  - That same role checkpoint state is now serialized into explicit
    `workflow_role_checkpoint` events before the run is stored.
  - The collaboration layer now also emits one `role_task_bridge_timeline`
    that the background runtime can materialize into persisted `workflow_role`
    child tasks when the parent workflow itself already runs inside runtime.
  - That bridge path is now chain-aware: only dependency-ready roles are
    materialized at a time, and downstream roles are enqueued incrementally
    after each successful role completion.
  - Downstream runtime role tasks now also carry compact
    `upstream_dependency_outputs`, so each next role can see the resolved
    actions and artifact previews produced by earlier sibling roles.
  - Those runtime role tasks now execute one deterministic role handler that
    respects sibling dependencies and emits one compact structured role result
    instead of a plain acknowledgement string.
  - The runtime role handler now also reuses the same collaboration execution
    brief vocabulary as the live agent loop, so planner/router/critic/
    summarizer role tasks expose aligned contracts and checkpoint ids.
  - Those runtime role tasks now also persist explicit `execution_brief` and
    `handoff_contract` payload fields, so the isolated role turn can run from
    runtime task context instead of reusing hidden parent-loop state.
  - When a prior degraded workflow run exists for the same originating parent
    session or workflow chain identity, matching runtime role tasks now also
    carry `resume_state` and `resume_brief` payload context so the runtime
    worker can reuse persisted evidence directly across parent-task boundaries.
  - For `wechat_article_flow`, those same runtime role tasks now also surface
    article-facing role identities (`researcher`, `drafter`, `editor`) and one
    compact `artifact_preview`, so replay can show article-specific role output
    instead of only generic `router/executor/summarizer` names.
  - For article writing, that means downstream role handlers can now consume
    upstream article route / gate outputs and expose those inputs again as
    `upstream_actions` and `upstream_artifacts` in the role step result.
  - The article-writing runtime path now also supports one minimal real tool
    handler: when a bridge-generated `wechat_article_flow` role task is marked
    `role_tool_enabled`, the runtime worker directly calls the matching
    `wechat_article_assist` stage and stores a compact tool-output preview on
    the role step result.
  - That article role path now also hands the tool-output preview to the next
    ready role task, and the runtime tool handler consumes both persisted
    upstream payload context and the dependency outputs resolved in the current
    execution turn.
  - When an LLM client is available, the same role worker can now run one
    isolated JSON-only role turn and merge the result back into the persisted
    role step while keeping the deterministic action boundary as fallback.
  - That persisted role step now also records role-level attempt and remaining
    budget metadata, plus whether a persisted resume snapshot was applied.
  - When a runtime role degrades, that same worker can now materialize one
    explicit recovery child task from the recorded bridge specs and preserve
    the failed role's evidence context for the rerouted role.
  - That recovery path now keeps `resume_checkpoint_id` and
    `recovery_task_key` separate, so evidence-gap failures can restore the live
    loop to the router checkpoint while the runtime directly re-enters at the
    executor task.
  - The runtime bridge materializer now also supports graph-fanout scheduling,
    so it can enqueue any dependency-ready role node instead of limiting
    runtime materialization to only the current node's direct successors.
  - The same runtime bridge path now also supports `depends_on_any`
    dependency edges, so one role can unblock from any satisfied upstream
    branch instead of requiring one strict all-of chain everywhere.
  - That same bridge materializer now also fingerprints upstream dependency
    output and can rearm one already-succeeded downstream role task in place,
    so `critic / summarizer` actually rerun after upstream recovery instead of
    reusing stale success rows.
  - The runtime recovery path now also prefers same-task rearm for an already
    succeeded recovery target when turn budget remains, so executor recovery
    re-entry stays on one task row instead of forking a second child task.
  - That same recovery path now also refreshes an already-pending recovery
    target in place, so queued executor turns can absorb new recovery context
    without spawning or silently reusing a stale child row.
  - When the recovery target is already running, the runtime now stages one
    deferred recovery payload on that same task and applies it through a
    same-task rearm after the current turn succeeds.
  - Background task payloads, bridge payloads, and recovery payloads now all
    preserve `workflow_identity`, so nested `spawn_task` calls and rerouted
    role tasks stay on the same workflow chain.
  - Role retry budgets and turn budgets now come from
    `agent.workflowRolePolicy` with safe defaults, so scheduler behavior is no
    longer defined only by hardcoded collaboration constants.
  - Recovery actions now also carry one explicit `recovery_path`, so the
    runtime can expose the intended multi-hop reroute instead of only one
    implicit next task key.
  - Rearmed downstream role tasks now also carry `turn_index`, `turn_budget`,
    `turn_reason`, `upstream_input_fingerprint`, and compact `turn_history`
    fields in both payload and step output.
  - `plugins` now loads lightweight `*.plugin.json` manifests for skills,
    channels, and search providers, resolves them by `kind + primary name`
    with source-scope precedence, and exposes shared import-path loading for
    manifest-backed runtime registration.
  - user-scope manifests now only load when the directory, manifest, and
    local module files are owned by the current user and not writable by
    group or others.
  - user-installed `channel / search_provider` manifests now also require one
    install receipt plus explicit `metadata.security.permissions` and
    `metadata.security.sandboxPolicy` declarations before runtime loading.
  - That same runtime policy can now also require signed bundles and trusted
    publishers, so local receipts are no longer the only third-party trust
    signal.
  - The same extension policy now also declares the remote `registryUrl` plus
    the current subprocess-isolation scope for third-party runtime kinds.
  - `extensions` renders that manifest registry into one compact operator /
    developer catalog used by the CLI, including manifest-derived runtime
    metadata so channel/provider inventory facts do not need a second
    hardcoded summary path.
- `nanoclaw/core/rss_sources.py`: shared RSS source loading and Mainland-first ordering.
- `nanoclaw/tools`: core tools (shell, files, web, memory, spawn), default workflows, and registry.
  - `capabilities` exposes the built-in capability catalog to CLI/chat users.
  - `registry` now also carries compact catalog metadata for built-in tools,
    so `nanoclaw capabilities` no longer keeps a second hardcoded tool-summary
    table.
  - `shell` exposes `shell_exec`, while `ShellSandbox` owns policy
    classification and the configured execution mode
    (`disabled / inline / subprocess`).
  - `web` keeps atomic web abilities and helpers:
    - `web_search`, `web_fetch`
    - RSS/provider retrieval, ranking, formatting, SSRF protection, outbound
      host allow/deny policy, and paper parsing helpers
  - `search_providers` is the provider registry behind `web_search`.
    - Current built-ins: `rss`, `brave`, `serper`, `searxng`, `auto`, `disabled`
    - Runtime registration now comes from provider manifests, including
      handler paths, disabled aliases, and `auto` provider priority / secret
      capability hints
    - User-installed providers can now load from safe adjacent Python modules
      referenced by manifests in `~/.nanoclaw/extensions`, after one local
      install receipt or signed-bundle trust check plus risk-policy
      evaluation, and can read provider-specific settings from
      `tools.webSearch.providerConfigs`
    - When extension policy enables subprocess isolation, user-installed
      providers run in a separate Python process instead of importing directly
      into the main agent runtime
    - The built-in `searxng` provider now reads self-hosted instance config
      from `tools.webSearch.providerConfigs.searxng` and maps planner hints
      into `categories`, `time_range`, and `language` request params
    - `serper` and `brave` now run provider-first search with optional RSS evidence supplement
    - `searxng` now also runs provider-first search with optional RSS evidence supplement
    - `auto` prefers `serper`, then `brave`, with RSS supplement and RSS-only fallback
    - Provider results now accept optional `evidence_items` so future providers
      can bypass text parsing and hand structured hits directly to the normalizer
  - `search_planner` is the v1 deterministic planner behind `web_search`.
    - It classifies queries into `news`, `web`, `paper`, `site`,
      `chinese_web`, and `long_tail`
    - It emits query variants, RSS channel hints, recency hints, provider hints,
      and future engine/category hints for heterogeneous search backends
  - `search_normalizer` is the v1 evidence normalizer/reranker behind `web_search`.
    - It normalizes provider output into one evidence schema
    - It applies URL normalization, dedupe, provider weighting, freshness
      scoring, and light lexical reranking
    - It already leaves a structured-hit hook for optional future providers
  - `web_workflows` keeps the default workflow tools built on top of `web`:
    - `hotspot_brief`, `daily_digest`
    - `paper_search` for multi-source paper retrieval (arXiv/OpenAlex/S2) with
      dedup, quality tiers, and trend/confidence summary
    - `wechat_article_assist` for WeChat writing workflow with fact-check,
      export, and article-role stages
      (`role_chain / planner / researcher / drafter / critic / editor`)
  - `spawn` writes background work into the persistent `tasks` table, then runs
    a best-effort in-process queue until the task completes or fails.
  - `spawn` also maintains a worker lease for running background tasks and
    periodically recovers stale jobs claimed by older workers.
  - `spawn` now accepts optional priority and timeout hints, and its local queue
    can cancel or time out running jobs based on persisted task metadata.
  - `spawn` also accepts retry hints (`max_attempts`, `retry_backoff_seconds`)
    and requeues failed work when retry budget remains.
  - `spawn` reads configured background worker capacity from config when available
    and wakes the local queue immediately after dashboard-driven requeue actions.
  - `spawn` also reads a starvation threshold from config so aged ready tasks
    can bypass normal priority ordering when they have waited too long.
  - `spawn` now treats that configured capacity as a shared per-source pool cap,
    so local workers stop claiming when other workers already occupy the pool.
  - `spawn` now also accepts optional rate-limit bucket hints so expensive
    provider/tool tasks can share one scheduler-level claim budget.
  - `spawn` now journals `agent_run` and `notify_result` steps so retries can
    resume from the last completed checkpoint instead of rerunning the whole
    background job body.
  - `spawn` now logs one `task_run` row per attempt so retries, cancellations,
    and successful completions can be replayed without raw log parsing.
  - `spawn` now also accepts an optional `idempotency_key` so callers can
    suppress duplicate background submissions safely.
  - `spawn` now deduplicates success, cancelled, and failed proactive
    notifications with stable step IDs, so repeated terminal handling does not
    spam the user.
  - `spawn` now records watchdog audit events for timeout-driven cancellation
    and stale orphan recovery, so operators can review runtime interventions
    without reading raw process logs.
  - `spawn` now also derives one small runtime-health summary from queue
    metrics and can send a cooldown-deduplicated proactive alert via the
    configured or auto-selected channel.
  - `spawn` now also marks `queue_stall` as a critical runtime-health reason,
    so alert severity is not limited to generic degraded warnings.
  - `spawn` now also tracks repeated alert fingerprints in memory so identical
    runtime alerts can escalate from initial warning/error to an explicit
    escalated stage on later repeats.
  - `spawn` now also supports `primary + escalation target` routing for
    escalated alerts, with one secondary auto-choice when no explicit
    escalation channel is configured.
  - `spawn` now also claims and dispatches the persisted `heartbeat_checklist`
    source, so the shared runtime covers both user-started background work and
    periodic heartbeat checks.
  - `spawn` now also claims and dispatches the persisted `cron_job` source, so
    scheduled cron work runs through the same shared worker loop.
- `nanoclaw/skills`: built-in skills loaded from disk, with adjacent `*.plugin.json`
  manifests defining triggers, entry points, dependencies, and risk level.
- `nanoclaw/security`: sandbox, shared boundary policy, file guard, prompt
  guard, audit, budget.
  - `sandbox_backends` resolves experimental stronger shell backends
    (`portable`, `docker`, `podman`, `bubblewrap`, `sandbox-exec`), treats
    `portable` as the default stronger-main-path and `docker` as the primary
    container target, builds the wrapper command line used by the subprocess
    runner, exposes cached runtime/image health checks plus lifecycle-drift
    state for operator surfaces, and now provides explicit runtime-lifecycle
    plus container-preparation orchestration paths for start/restart/wait and
    image-pull readiness re-checks.
  - `policy_contract` builds one compact operator-facing boundary contract for
    CLI and dashboard status surfaces.
  - `audit` stores both low-level `audit_log` entries and higher-level
    `workflow_runs` summaries.
  - `workflow_runs.call_chain` now also includes role-plan, role-handoff, and
    shared-evidence events, so lightweight collaboration state can be replayed
    without adding a second runtime.
  - Evidence-bearing tool call events can now also carry `evidence_refs`, so
    replay can show which shared evidence objects came from which tool call.
  - After evidence-bearing tools run, the agent now also attaches one compact
    internal `shared_evidence_brief` to later LLM turns so the same run can
    reuse evidence instead of blindly re-fetching it.
  - Workflow rows are now normalized with a derived `role_execution_timeline`
    `role_recovery_timeline`, and `shared_evidence_refs`, and both CLI and
    dashboard expose a compact role-level replay view for one workflow run.
  - `workflow_runs` now also store one explicit `workflow_identity`, and
    normalized replay rows fall back to `workflow_context.workflow_identity`
    only for older rows created before the schema upgrade.
  - The same replay payload now also exposes one `role_task_timeline`, giving
    each collaboration role one stable `task_key`, dependency list, checkpoint
    boundary, and retry budget so a future role runtime can reuse the same
    scheduling envelope.
  - The replay payload now also exposes one `role_task_bridge_timeline`, which
    serializes each role boundary into a runtime-consumable task spec
    (`task_type`, `source`, `priority`, `timeout`, `max_attempts`,
    `idempotency_key`, `payload`) and carries `parent_task_id` when the
    workflow itself ran inside the background runtime.
  - Those normalized role timelines now also surface
    `checkpoint_id / resume_checkpoint_id / attempt_number / budget_limit /
    remaining_budget` for lightweight role recovery replay.
  - Workflow rows now also expose one derived `role_checkpoint_timeline`, so
    CLI and dashboard can inspect persisted role checkpoints directly.
  - The agent can now also bootstrap one new run from the latest failed run in
    the same workflow chain by loading a persisted role checkpoint snapshot and
    emitting one explicit `workflow_role_resume` event, with legacy
    `session_id / parent_session_id` matching retained as fallback.
  - Recovery rows now also include `restored_messages` and
    `restored_evidence_count`, so replay can tell whether the agent actually
    rewound execution to a prior role checkpoint.
  - `audit` now also stores derived `workflow_evaluations`, so CLI status and
    the dashboard can expose workflow quality/efficiency signals, structured
    failure classes, compact attention reasons, and follow-up actions without
    re-reading raw call chains on each status refresh.
  - `audit` now also accepts explicit per-run feedback updates
    (`positive`, `neutral`, `negative`, `unknown`) keyed by `workflow_run_id`.
  - `audit` now also supports session-scoped "latest workflow" feedback updates,
    which lets chat channels attach explicit user feedback without first
    exposing raw `workflow_run_id` values.
  - `audit` can now also aggregate recent workflow evaluations into
    `attention / optimize / healthy` recommendation summaries keyed by
    `workflow_name`, while carrying one compact `why` reason plus one
    follow-up action for operator surfaces.
  - `audit` now also stores `task_runs` and `tool_traces` for task replay, with
    best-effort secret masking on stored summaries.
  - `audit_log` now also carries `runtime_watchdog` events for timeout and
    stale-lease interventions inside the background runtime.
  - `audit` now also derives a compact 24-hour boundary activity summary from
    `boundary_decision` and `secret_access` rows for operator-facing status
    views.
- `nanoclaw/memory`: SQLite store for history and memories.
- `nanoclaw/channels`: gateway, registry, Telegram bot, Feishu webhook
  channel, console.
  - `registry` now resolves shared channel runtime specs from manifests,
    including delivery contract metadata, routing priorities, startup
    requirements, runtime factory paths, and fallback config lookup through
    `channels.extensions` for user-installed channels.
  - proactive-only user-installed channels can now route `start / stop /
    send_proactive / send_proactive_to` through the extension subprocess
    boundary, while incoming-capable user channels still run in process.
  - `gateway` now also tracks one compact runtime lifecycle state per managed
    channel (`disabled / starting / running / failed / stopped`) instead of
    exposing only the live channel object map.
  - `state_store` now persists one compact desired-state row per managed
    channel in SQLite, including desired state, actual state, drift metadata,
    reconcile status, and last action timestamps.
  - `channel_contract` builds one operator-facing registry summary from that
    shared manifest-backed channel registry for managed `telegram / feishu`
    plus runtime-only `console`, covering delivery mode, auth mode,
    proactive capabilities, lifecycle status, runtime failure detail, route
    roles, selected-vs-blocked route decisions, and compact diagnostics
    health plus recent delivery counters for operator-facing policy views.
    The same contract now also carries desired-vs-actual drift state,
    reconcile summaries, and orchestration aggregates for managed channels
    only. CLI and dashboard status now render channel rows from this contract
    instead of hardcoding only two transports.
  - the same `gateway` path now also exposes explicit managed-channel operator
    actions (`start / stop / restart / recover / reconcile`), and the
    dashboard operator API reuses that path through `/api/channels` and
    `/api/channels/{channel_name}/action`.
  - the dashboard operator API now also exposes
    `/api/channels/{channel_name}/desired-state`, which reuses the same
    desired-state path as the CLI operator surface.
  - that same gateway path now also exposes one public desired-state setter,
    and the operator API reuses it through
    `/api/channels/{channel_name}/desired-state` so desired-state control is
    not limited to private helpers inside the runtime.
  - the same `gateway` path now also records one compact diagnostics overlay
    per managed channel, including recent incoming/proactive counts, targeted
    proactive attempts, last success/failure metadata, and runtime transition
    stamps.
  - that same gateway path now also runs one periodic reconcile loop so
    managed channels converge toward persisted desired state instead of
    depending only on one-shot operator actions.
  - heartbeat notification routing and runtime-alert routing now reuse the same
    route resolver when a live gateway config is available, instead of keeping
    per-subsystem fallback rules entirely separate.
  - `feishu` includes command-template mapping for paper workflow and
    chat-scoped schedule templates (`/schedule daily`, `/schedule hotspot`,
    `/schedule paper`, `update`, `show`, `pause`, `resume`, `list`, `remove`)
    plus chat-scoped workflow feedback (`/feedback positive|neutral|negative`)
    and workflow-inspection (`/workflow report [--days N] [--limit N]`).
  - That feedback path now also accepts a small set of high-confidence natural
    feedback phrases such as "这个回答不错" or "这个结果不满意", and rewrites them
    into the same latest-session workflow feedback update instead of inventing
    a second storage path.
  - The same latest-session feedback path now also accepts a small set of
    context-aware phrases such as "给刚才那条工作流好评" or "刚才那条给个差评",
    still mapping back to the same feedback update for the latest run in the
    current chat session.
  - For a few high-confidence cases, the same Feishu workflow surface also
    supports a combined feedback-plus-suggest shortcut such as
    "对刚才那条差评工作流给个差评并展开建议", which updates feedback for the
    latest matching run in the current chat and immediately expands the stored
    per-run suggestions in one reply.
  - After `/workflow recent` or `/workflow report`, the same chat now keeps a
    small in-memory ordinal reference to that last rendered list, so follow-up
    phrases such as "把第一条展开" or "给第二条差评" can reuse the displayed
    order without repeating the run id. Index-based feedback only applies to
    recent-run lists; aggregated report rows can be expanded by index, but not
    rated by index.
  - The same cached-list path now also supports high-confidence workflow-name
    references such as "把grounded_current_info展开" or
    "给default_chat_loop差评". When a short name matches more than one cached
    row, the bot refuses to guess and asks for either a more specific name or
    an ordinal reference.
  - That same cached-list path now also accepts combined name references such
    as "把grounded_current_info展开并给个差评". On a cached `recent` list, this
    updates feedback and expands the run in one reply; on a cached aggregated
    `report`, it refuses the rating step and asks the user to switch back to
    `/workflow recent`.
  - The same Feishu channel now also exposes aggregated workflow recommendations
    directly in chat, supports `--status attention|optimize|healthy` and
    `--feedback positive|neutral|negative`, and includes a few high-confidence
    report shortcuts such as "看看最近工作流建议",
    "看看需要关注的工作流建议", and "看看负反馈多的工作流建议", again
    mapping back to one stable command surface.
  - Feishu now also exposes recent workflow evaluations directly in chat via
    `/workflow recent [--limit N] [--label LABEL] [--feedback SIGNAL]`, so
    operators can inspect recent `good / review / poor` runs without switching
    to CLI or dashboard.
  - The same `/workflow` surface now also supports
    `/workflow feedback <RUN_ID> <SIGNAL>`, so one recent eval row can be
    reviewed and then updated from the same Feishu command family.
  - The same surface now also supports `/workflow suggest <RUN_ID>`, so one
    recent eval row can be expanded into its stored per-run suggestions without
    opening CLI or dashboard.
  - For a few high-confidence cases, the same workflow-suggest path also
    accepts natural shortcuts such as "看看run42的建议" and rewrites them back
    into `/workflow suggest <RUN_ID>` instead of introducing a second command
    family.
  - When the current Feishu chat context is available, that same path also
    supports a small session-aware rewrite layer for phrases such as
    "展开刚才那条差评工作流的建议", which resolves the latest matching run in the
    current chat and still rewrites back to `/workflow suggest <RUN_ID>`.
  `/schedule list` now also supports lightweight `health` and `signal`
  filters, and the same filter syntax can drive batch `pause`, `resume`, and
  `remove`, so chat-scoped operations can isolate or act on `attention`,
  `retrying`, or `recovery` jobs without opening the dashboard.
  - `feishu` also has a lightweight natural-language rewrite layer for simple
    daily requests such as "每天早上8点给我发一份AI日报", which maps into the
    same schedule-template path instead of introducing a separate scheduler flow.
  - That same rewrite layer now also supports high-confidence schedule
    management by name or topic inside the current chat, and explicitly falls
    back to `/schedule list` + `#ID` when more than one schedule matches.
  - The rewrite layer now also supports high-confidence filtered batch
    shortcuts for common health/signal workflows, so Chinese phrases like
    "暂停所有需要关注的定时任务" still map back to the same `/schedule ...`
    command surface instead of introducing a second control path.
  - The same schedule path now supports `every day`, `workdays`, and `weekly`
    recurrence without introducing separate runners per cadence.
  - Schedule management (`update`, `pause`, `resume`) reuses the same chat
    ownership checks as remove, so one Feishu chat cannot modify another chat's
    scheduled pushes.
  - Schedule payloads can now carry a quiet window (`quiet_start`, `quiet_end`).
    The cron task still runs on time, but the proactive push is suppressed
    inside that local mute window and recorded in task replay as a suppressed
    notification step.
  - If a cron result is generated successfully but the proactive send fails,
    `spawn` now persists a separate `cron_delivery_retry` task so delivery can
    retry without rerunning the agent step. That follow-up task also respects
    quiet windows by deferring itself until the quiet window ends.
- `nanoclaw/cron`: scheduler for recurring jobs, plus heartbeat/cron enqueuers
  that turn periodic work into persisted runtime tasks.
  - `scheduler` now persists an optional `target_id` with each cron job so
    channel-specific proactive delivery can reuse the shared runtime path.
  - `scheduler.list_jobs_with_runtime_state()` now joins persisted cron jobs
    with the latest `cron_job` and `cron_delivery_retry` tasks so Feishu and the
    dashboard can display recent execution and retry state without each
    reimplementing task scanning.
  - That runtime bundle is also reduced into a schedule-level health summary
    (`healthy`, `retrying`, `attention`, `muted`, `idle`) so operators do not
    have to infer job health from raw task rows.
  - The same scheduler view now also includes a short recent schedule-signal
    timeline built from `schedule_alert`, `schedule_alert_escalation`, and
    `schedule_recovery` audit rows, so Feishu and the dashboard can show
    "alert -> recovery" without opening raw audit logs.
  - `spawn` now consumes that schedule-health view and emits proactive
    schedule-level alerts for `retrying` or `attention` jobs. The first alert
    targets the owning schedule chat, and repeated identical problems can fan
    out to the configured escalation channel. That escalation threshold is
    configured separately from runtime-health alerts, so schedules can be more
    conservative by default.
  - `retrying` schedule health also has its own initial suppression threshold,
    so one-off delivery retry jitter can be tracked in runtime state without
    immediately paging the owning chat.
  - When one previously alerted schedule returns to `healthy`, `spawn` now
    sends one `schedule_recovery` notice so operators can see that the issue
    cleared without opening the task list manually.
- `nanoclaw/runtime`: persistent task primitives for the long-running runtime roadmap.
  - `tasks` owns the SQLite `tasks` table and guarded status transitions for
    `pending`, `running`, `succeeded`, `failed`, and `cancelled`.
  - `tasks` also provides atomic pending-task claiming used by the current
    background queue bridge for `spawn_task`, `heartbeat_checklist`, and
    `cron_job`.
  - `tasks` stores `claimed_by` and `last_heartbeat_at` so a restarted worker
    can recover stale running tasks.
  - `tasks` also persists queue metadata (`priority`, `timeout_seconds`,
    `cancel_requested`, `max_attempts`, `retry_backoff_seconds`,
    `next_attempt_at`, `idempotency_key`) used by the current scheduler slice.
  - `tasks` now applies a starvation-aware claim path and surfaces
    `starved_ready_tasks` metrics for CLI/dashboard status views.
  - `tasks` also tracks distinct running workers and enforces the current
    claim-scope global running cap inside the claim transaction.
  - `tasks` can claim across multiple runtime sources in one transaction, so
    the shared runtime does not need to iterate sources in a fixed order.
  - `tasks` now also uses current per-source running counts as a tie-break for
    equal-priority multi-source claims, which gives the shared runtime a minimal
    source-aware fairness signal without introducing a separate scheduler layer.
  - `tasks` also persists per-task rate-limit bucket metadata and `last_claimed_at`
    so claims can be postponed until the next allowed window.
  - `tasks` now also supports explicit same-task rearm for succeeded
    `workflow_role` tasks, preserving task identity while clearing only the
    runtime-claim fields needed for one new turn.
  - `tasks` now also supports explicit in-place payload refresh for pending
    `workflow_role` tasks, so queued recovery targets can absorb new recovery
    context without creating a second task row.
  - `tasks` now also supports explicit in-place payload refresh for running
    `workflow_role` tasks, so live recovery targets can stage deferred refresh
    without losing their current claim or worker lease.
  - `tasks` also persists dead-letter metadata (`dead_lettered`,
    `dead_letter_reason`, `dead_lettered_at`) for terminal retry exhaustion.
  - `tasks` now also persists `task_steps` rows with step input/output payloads,
    hashes, attempts, timestamps, and checkpoint metadata for resumable work.
  - `tasks` exposes queue metrics so CLI/dashboard can show ready backlog,
    retry backlog, rate-limited backlog, starved ready backlog, stale running
    backlog, running workers, dead-letter backlog, and pending age without
    inspecting SQLite manually.
- `nanoclaw/dashboard`: local aiohttp server and single-file UI.
  - Status API now includes recent tasks, and `/api/tasks` exposes task rows for
    the dashboard task panel and future replay/runtime tooling.
  - Status API now also includes workflow evaluation summary, and
    `/api/workflow-evaluations` exposes recent derived workflow evaluations.
  - `/api/workflow-evaluations/{workflow_run_id}/feedback` now lets operators
    write an explicit feedback signal back into one derived evaluation row.
  - `/api/workflow-recommendations` exposes recent workflow-level aggregated
    recommendations, and the dashboard page now renders them as a read-only
    "what to optimize next" view.
  - `/api/tasks/{task_id}/replay` returns a structured task replay bundle with
    task metadata, steps, task runs, tool traces, workflow runs, and runtime
    audit events.
  - `/api/tasks/{task_id}/cancel` requests task cancellation from the browser UI.
  - `/api/tasks/{task_id}/requeue` moves failed or cancelled tasks back to pending.
  - `/api/cron` now returns compact schedule summaries, `/api/cron/groups`
    groups them by `channel + target scope`, and `/api/cron/{id}/toggle`
    lets the UI pause/resume one persisted job in place.
  - `/api/cron` and `/api/cron/groups` now also accept `health` and `signal`
    filters so the UI can isolate `attention`, `retrying`, or recovery-heavy
    schedules without client-side scanning.
  - `/api/cron/groups/action` now lets one dashboard group batch `pause`,
    `resume`, or `remove` only the visible jobs inside one `channel + target`
    scope after the active `health` / `signal` filters are applied.
  - Cron summaries now also expose the latest execution status, notify mode, and
    delivery-retry status for each schedule row.
  - Cron summaries now also expose a recent `signal_timeline` so the dashboard
    can render the latest schedule alerts and recoveries inline.
  - Status API also includes queue backlog and saturation metrics for the local
    background runtime across persisted runtime sources, including the
    `starved_ready_tasks` counter, global running-worker count,
    stale-running visibility, global saturation view, and the current
    `rate_limited_backlog`.
  - Status API also includes a `runtime_health` summary so the dashboard can
    render one explicit health line instead of forcing operators to infer it
    from raw queue counters.
  - Queue status now also exposes the configured stall threshold so operators
    can see when ready backlog should be considered a deadlock-like stall.
  - Status API now also exposes the current boundary policy contract, resolved
    shell backend state, and 24-hour aggregates for `boundary_decision` and
    `secret_access`.

## Data Stores

- SQLite at `~/.nanoclaw/data/nanoclaw.db` for history, memories, cron, audit,
  workflow telemetry, and the new persistent `tasks` table.
  - `tasks` rows now also track lease ownership, heartbeat timestamps, queue
    priority, timeout budget, retry budget, next retry timestamp,
    cancellation requests, starvation visibility, rate-limit bucket metadata,
    idempotency key, and dead-letter state.
  - `task_steps` rows keep resumable step journals for background tasks,
    including stable step IDs, checkpoint hashes, outputs, and attempt counts.
  - `task_runs` and `tool_traces` rows keep replay-friendly attempt and tool
    execution history for task-scoped runs.
  - `audit_log` rows now also include `runtime_watchdog` entries for timeout
    cancellations and stale-orphan recovery actions.
  - `audit_log` can also store `runtime_alert` entries when the runtime sends a
    proactive health alert.
  - `audit_log` now also stores `runtime_alert_escalation` rows when the same
    runtime problem repeats and the alert stage is upgraded.
  - `runtime_alert` and `runtime_alert_escalation` rows now also capture the
    resolved alert targets, so operators can audit how escalation routing
    behaved for a given incident.
  - `audit_log` now also stores `schedule_alert` and
    `schedule_alert_escalation` rows when one persisted schedule keeps landing
    in `retrying` or `attention`.
  - `audit_log` now also stores `schedule_recovery` rows when one previously
    alerted schedule returns to `healthy`.
- Feed registry at `assets/rss-sources.json` for runtime RSS search and checks.

## Dependencies

- aiohttp
- python-telegram-bot
- click
- pydantic
- sqlite3 (stdlib)
- html2text
- croniter

## Security Boundaries

- FileGuard restricts file access to `~/.nanoclaw/workspace`.
- ShellSandbox blocks dangerous commands, confirms destructive ones, and now
  exposes an explicit shell boundary contract through
  `tools.shell.mode = disabled | inline | subprocess`.
- In `subprocess` mode, `shell_exec` runs through
  `nanoclaw/security/shell_runner.py` with a stripped environment instead of
  spawning the command directly from the main agent process.
- That subprocess runner now also applies `Hard Isolation v0`: isolated
  `HOME/TMP/XDG` roots plus OS-level limits for CPU time, virtual memory, core
  dumps, open files, and file size writes.
- The same subprocess runner now also supports one experimental stronger-backend
  spike through `tools.shell.backend = native | auto | docker | podman |
  bubblewrap | sandbox-exec`. `auto` prefers a configured container runtime
  first, then other stronger local backends, and otherwise falls back to
  `native`.
- For `docker` and `podman`, the shell runner uses one configured
  `tools.shell.containerImage`, disables network, drops capabilities, mounts
  only the workspace plus isolated shell root, and runs the command with the
  current host uid/gid instead of root.
- The policy contract now also carries the health state for the primary
  `docker` target, including runtime reachability and local image readiness,
  plus remediation steps, a stable verify command, a stable prepare command,
  and a stable runtime-lifecycle command, so CLI, dashboard, and doctor can
  expose the same cached readiness view.
- `nanoclaw/security/secrets.py` now brokers tool-side secret capabilities for
  built-in search providers, so Brave/Serper keys are injected explicitly
  instead of being read ad hoc from individual provider code paths.
- `nanoclaw/security/boundary.py` now centralizes high-risk file and outbound
  web checks, so `file_read`, `file_write`, `web_fetch`, and workflow export
  writes reuse the same policy entrypoint.
- Boundary decisions collected during one tool run are now flushed into
  `audit_log` as `boundary_decision` events, so task replay and CLI output can
  show why a file/web path was allowed or blocked.
- Secret-capability decisions from the broker now also land in `audit_log` as
  `secret_access` events, so task replay can show whether a tool received one
  config-backed or env-backed secret capability.
- `nanoclaw/security/policy_contract.py` now exposes a compact boundary policy
  contract, and both CLI status plus dashboard status expose that contract
  alongside requested-vs-selected shell backend state, container-image
  readiness, and 24-hour aggregates for boundary and secret-access decisions.
- PromptGuard sanitizes tool output and detects injection patterns.
- Dashboard binds to localhost only and channel IDs are allow-listed.
