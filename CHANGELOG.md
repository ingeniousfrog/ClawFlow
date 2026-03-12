# Changelog

This file keeps shipped work that no longer belongs on the live task board.

## 2026-03-11

- Shipped `R4` controlled persona and prompt evolution slice:
  - protected persona fragments now persist in
    `~/.nanoclaw/data/persona_fragments.json` instead of mutating the raw
    configured `agent.systemPrompt`
  - reviewed summaries now update only `identity`, `style`,
    `workflowPreferences`, and `configHints` through one controlled parser
    that rejects prompt-injection-shaped fragments
  - CLI now exposes `nanoclaw persona show` plus
    `nanoclaw persona apply-review`, and runtime system-prompt composition now
    renders the protected persona layer separately from fixed rules and manual
    configured prompt text
- Shipped `R4` workflow evaluation v2 slice:
  - derived workflow evaluations now persist structured
    `failure_classes / attention_reasons / follow_up_actions` alongside the
    existing quality and efficiency scores
  - 7-day workflow recommendations now aggregate those structured signals by
    workflow name, so operator surfaces can show one compact `why` reason plus
    one follow-up action instead of only one heuristic top suggestion
  - CLI status, `workflow-report`, and dashboard workflow summaries now expose
    that compact `why` signal without requiring raw replay expansion
- Shipped `R4` richer multi-role scheduler v2 slice:
  - role retry budgets and turn budgets now come from
    `agent.workflowRolePolicy` with safe defaults
  - runtime role-task scheduling now supports `depends_on_any` dependency
    edges plus graph-fanout materialization for any dependency-ready node
  - recovery actions now carry one explicit `recovery_path`, and recovery
    payload state keeps that path visible during same-task rearm and replay
- Shipped `R4` SearXNG provider slice:
  - built-in search-provider manifests now also include `searxng`
  - `web_search` now supports self-hosted `SearXNG` through
    `tools.webSearch.providerConfigs.searxng`, including planner-aware
    `categories`, `time_range`, and `language` request mapping
  - provider routing and formatting coverage now includes explicit SearXNG
    tests alongside the existing Brave/Serper stack
- Shipped `R3` publisher-governance and broader isolation slice:
  - signed third-party bundles now carry explicit `keyId`, and extension
    policy can enforce whole-publisher revocation plus per-publisher signing
    key rotation / revocation through structured `trustedPublishers` config
  - `nanoclaw extension-install`, `nanoclaw extension-verify`, and
    `nanoclaw extensions` now surface `keyId` and can mark installed
    extensions as revoked when current publisher policy invalidates them
  - user-installed proactive-only channels now also reuse the extension
    subprocess boundary, so stronger runtime isolation is no longer limited to
    search providers
- Shipped `R3` remote registry/update slice:
  - CLI now exposes `nanoclaw extension-registry` plus
    `nanoclaw extension-update` for signed bundle listing and update/install
    from one configured registry JSON source
  - local install receipts now also track installed extension version plus the
    registry source that provided the bundle
  - registry entries now expose local update status (`available / current /
    update_available / ahead`) by comparing installed receipts against the
    configured remote bundle version
- Shipped `R3` stronger extension runtime-isolation slice:
  - extension policy now also declares `runtimeIsolationMode`,
    `runtimeIsolatedKinds`, and `isolatedTimeoutSeconds`
  - user-installed search providers can now run in a dedicated subprocess with
    a stripped environment and isolated runtime roots instead of importing
    directly into the main agent process
  - `SecurityDoctor` now validates that subprocess isolation remains enabled
    for user-installed search providers
- Shipped `R3` signed-bundle distribution slice:
  - CLI now exposes `nanoclaw extension-pack`, which turns one local
    `channel / search_provider` manifest plus adjacent Python module into a
    distributable bundle archive
  - `nanoclaw extension-install` now accepts either one local manifest or one
    bundle archive, writes distribution metadata into the local install
    receipt, and reports `distributionType / publisher / signatureVerified`
  - runtime extension policy can now require signed bundles and trusted
    publishers through `extensions.requireSignedBundles` plus
    `extensions.trustedPublishers`
  - `SecurityDoctor`, `nanoclaw extension-verify`, and `nanoclaw extensions`
    now surface that third-party trust posture instead of showing only local
    receipt state
- Shipped `R3` workflow/catalog data-source slice:
  - built-in workflow metadata now loads from packaged
    `nanoclaw/core/workflow_catalog.json` instead of a hardcoded Python list
  - `nanoclaw capabilities` now consumes that shared workflow registry plus
    tool-registry metadata, so workflow/tool catalog text no longer needs a
    parallel hardcoded description table
- Shipped `R3` manifest-driven runtime registration slice:
  - built-in `search_provider` manifests now drive runtime registration for
    handler paths, disabled aliases, and `auto` provider priority / secret
    capability hints
  - built-in `channel` manifests now drive runtime registration for delivery
    contract metadata, managed-vs-runtime-only state, routing priority,
    startup requirements, and runtime factory paths
  - `gateway` and `channel_contract` now consume that shared manifest-backed
    channel registry instead of keeping separate hardcoded channel tables
- Shipped `R3` user-extension runtime loading slice:
  - user-installed `channel / search_provider` extensions now load from
    `~/.nanoclaw/extensions` through adjacent local Python modules plus
    manifest-declared import paths
  - user extension manifests, directories, and local Python modules now require
    current-user ownership and no group/other write bits before they are
    accepted for runtime loading
  - `channels.extensions` and `tools.webSearch.providerConfigs` now provide
    explicit config blocks for user-installed channel and provider extensions
  - `nanoclaw extensions` now also exposes manifest metadata directly, so more
    operator/developer catalog facts come from manifests instead of parallel
    hardcoded summaries
- Shipped `R3` local extension install/trust-policy slice:
  - CLI now exposes `nanoclaw extension-install` and
    `nanoclaw extension-verify` for local third-party channel/provider
    installation and receipt verification
  - user-installed `channel / search_provider` manifests now require explicit
    `metadata.security.permissions` and `metadata.security.sandboxPolicy`
    declarations plus one local install receipt before runtime loading
  - runtime extension policy now enforces the configured
    `extensions.maxRiskLevel`, and `SecurityDoctor` now reports extension
    policy posture alongside shell/container/secret checks
  - `nanoclaw capabilities` now derives built-in tool descriptions from shared
    tool-registry metadata instead of maintaining a second hardcoded tool
    summary table
- Shipped `R3` extension manifest registry slice:
  - the lightweight plugin registry now resolves manifest-backed
    `skill / channel / search_provider` extensions instead of only skill
    manifests
  - built-in manifests now describe `telegram / feishu / console` plus the
    built-in `rss / brave / serper / auto / disabled` search-provider set
  - manifest resolution now applies source precedence by `kind + primary
    name`, so higher-scope manifests can override or disable lower-scope
    metadata
  - packaging now explicitly includes shipped `.plugin.json` files for skills,
    channels, and tools
  - CLI now exposes `nanoclaw extensions` for operator-facing extension
    inventory in text or JSON form
- Shipped `R2` broader channel coverage slice:
  - `channel_contract` now treats `console` as one first-class runtime-only
    channel instead of only mentioning it inside route fallback logic
  - the same contract now distinguishes operator-managed channels from
    runtime-only channels, so orchestration counts stay scoped to
    `telegram / feishu` while `console` still appears in status views
  - CLI status and `nanoclaw channel list` now render channels dynamically
    from the contract instead of hardcoding only `telegram / feishu`
  - dashboard status now renders its channel rows from the contract as well,
    so newly surfaced channels do not require a second hardcoded status path
- Shipped `R2` desired-state operator surface follow-up:
  - dashboard now also exposes `/api/channels/{channel_name}/desired-state`
    for declarative channel control against the live gateway runtime
  - CLI now exposes `nanoclaw channel desired-state` and `channel action`
    accepts `recover` and `reconcile`, so the operator surface matches the
    current orchestration contract
  - gateway now exposes one public `set_channel_desired_state()` path instead
    of keeping desired-state updates as a private-only helper

## 2026-03-10

- Shipped `R2-E` gateway runtime desired-state / orchestration slice:
  - `Gateway` now persists one compact desired-state row per managed channel in
    SQLite, including desired state, actual status, drift metadata, reconcile
    status, and last operator/runtime action
  - the same gateway path now runs one periodic reconcile loop so managed
    channels converge toward desired state instead of depending only on
    one-shot operator actions
  - managed-channel operator actions now include `recover` and `reconcile`, in
    addition to `start / stop / restart`
  - `channel_contract` now carries one orchestration view, including
    desired-vs-actual drift status, reconcile summary, and operator-facing
    aggregate counts for CLI and dashboard status
- Shipped `R2-D` channel health / diagnostics slice:
  - `Gateway` now records one compact diagnostics overlay per managed channel
    for incoming handling, proactive sends, targeted proactive sends, and
    runtime transitions
  - `channel_contract` now carries diagnostics health, recent delivery counts,
    last failure detail, and a compact operator-facing diagnostics summary for
    `telegram` and `feishu`
  - CLI status, `nanoclaw channel list`, and `nanoclaw channel action` now
    surface diagnostics state in the same contract view
  - dashboard status and `/api/channels` now expose the same per-channel
    diagnostics contract for operator views
- Shipped `R2-C` channel auth / routing policy contract slice:
  - `channel_contract` now carries one explicit auth / routing policy view for
    `telegram` and `feishu`, including incoming auth detail, proactive-target
    readiness, route roles, and blocked route reasons
  - the same contract now resolves operator-facing route decisions for
    `default_proactive`, `heartbeat`, `runtime_alert`, and
    `runtime_alert_escalation`
  - CLI status and `nanoclaw channel list` now surface route summaries plus
    per-channel auth / proactive-target details
  - dashboard status now exposes the same route policy and shows one compact
    route summary in the UI
  - heartbeat and runtime-alert routing now reuse the same route resolver when
    a live gateway config is available
- Shipped `R2` gateway operator actions / channel control slice:
  - `Gateway` now exposes explicit managed-channel operator actions
    (`start / stop / restart`) for `telegram` and `feishu`
  - `channel_contract` now carries per-channel operator-action availability in
    addition to lifecycle and auth state
  - dashboard now exposes `/api/channels` and
    `/api/channels/{channel_name}/action` for channel inspection and control
  - dashboard UI now renders `start / stop / restart` buttons for channels
    whose current lifecycle state allows that action
  - CLI now includes `nanoclaw channel list` plus `nanoclaw channel action`
    for the in-process gateway runtime path
- Shipped `R2` channel registry / lifecycle contract slice:
  - new `nanoclaw/channels/contract.py` now builds one compact operator-facing
    channel registry contract for `telegram` and `feishu`
  - `Gateway` now tracks channel lifecycle transitions instead of exposing only
    a live object map
  - dashboard status and CLI status now surface channel auth mode, delivery
    mode, proactive capabilities, lifecycle status, and runtime failure detail
- Shipped portable stronger-main-path and drift-aware lifecycle slice:
  - `tools.shell.backend` now defaults to `portable` instead of `native`
  - `portable` now prefers one host-local stronger backend before container
    backends and falls back to `native` when no stronger option exists
  - subprocess shell execution now retries once with `native` when a resolved
    host-local stronger backend cannot activate on the current host
  - primary Docker health now tracks lifecycle drift and runtime-version
    changes, and remediation plans expose restart paths in addition to start
    or prepare paths
  - `nanoclaw container-runtime` now supports drift-aware restart orchestration
    and surfaces drift state in operator output
- Shipped docker readiness remediation slice:
  - `nanoclaw doctor` now emits remediation steps when the primary Docker
    target is not ready
  - new `nanoclaw container-check --refresh` runs one explicit runtime/image
    verification path and exits non-zero when the target is not ready
  - policy/status surfaces now carry remediation steps plus one stable verify
    command for the primary Docker target
- Shipped docker provisioning orchestration slice:
  - new `nanoclaw container-prepare --refresh --pull` can preflight the
    primary container target, pull the configured local image when needed, and
    re-check readiness in one explicit operator action
  - remediation plans now expose stable `prepare`, `pull`, and runtime-start
    commands instead of only a verify command
  - CLI status, doctor output, and policy-contract payloads now surface the
    same preparation path for the primary Docker target
- Shipped docker runtime lifecycle orchestration slice:
  - new `nanoclaw container-runtime --refresh --start --prepare --pull` can
    attempt to start the primary runtime, wait for runtime reachability, and
    optionally chain into image preparation before the final readiness re-check
  - remediation plans now expose one stable lifecycle command for
    `runtime_unreachable` states instead of only raw platform-specific start
    commands
  - CLI status and doctor output now surface the same runtime-lifecycle path
    for the primary Docker target
- Promoted one primary container backend target:
  - `docker` is now the primary container target for the stronger shell
    backend roadmap
  - container readiness now checks both runtime reachability and local image
    presence for that target
  - CLI status, dashboard status, and `SecurityDoctor` now expose the same
    cached `docker` readiness view
- Shipped container/jail backend feasibility slice:
  - `tools.shell.backend` now also supports `docker` and `podman`
  - `tools.shell.containerImage` now enables a real container-backed shell
    path when a local runtime and image are already present
  - `auto` backend selection now prefers configured container runtimes before
    local stronger backends such as `bubblewrap` or `sandbox-exec`
  - container-backed shell runs now disable network, drop capabilities, mount
    only the workspace plus isolated shell root, and surface requested-vs-
    selected backend state in status and doctor output
- Shipped `R1-F Stronger Sandbox Backend spike`:
  - new `nanoclaw/security/sandbox_backends.py` resolves experimental stronger
    shell backends and builds wrapper commands for `bubblewrap` and
    `sandbox-exec`
  - `tools.shell.backend` now supports `native`, `auto`, `bubblewrap`, and
    `sandbox-exec`
  - subprocess shell requests now carry backend selection into the dedicated
    runner, which resolves requested vs selected backend before execution
  - `nanoclaw status`, dashboard status, and `SecurityDoctor` now expose or
    validate the requested-vs-selected shell backend state
- Shipped `R1-E Boundary Metrics / Policy Contract v0`:
  - new `nanoclaw/security/policy_contract.py` now builds one compact
    operator-facing boundary contract for CLI and dashboard status
  - boundary and secret-access audit traces now carry explicit policy versions
  - `AuditLog.get_boundary_metrics()` now derives 24-hour aggregates from
    `boundary_decision` and `secret_access` rows
  - `nanoclaw status` and `/api/status` now expose boundary contract plus
    recent boundary activity totals for operators
- Shipped shell boundary v0:
  - `tools.shell.mode` now defines an explicit shell execution contract with
    `disabled`, `inline`, and `subprocess`
  - `subprocess` is now the default mode and routes `shell_exec` through the
    dedicated `nanoclaw/security/shell_runner.py` helper
  - shell runner requests carry a stripped environment and workspace-scoped cwd
  - `SecurityDoctor` now warns when shell execution stays in `inline` mode
- Shipped `R1-C Hard Isolation v0` for shell execution:
  - the subprocess shell runner now gives each command an isolated
    `HOME/TMP/XDG` root instead of reusing the main process home directory
  - subprocess execution now applies OS-level CPU, memory, core-dump,
    open-file, and file-size limits before the shell starts
  - `SecurityDoctor` now checks `isolateHome`, `maxMemoryMb`, and
    `maxFileSizeKb` as part of the shell boundary health signal
  - acceptance coverage now includes ephemeral-home and file-size-limit checks,
    plus doctor regression coverage
- Shipped `R1-D Secret Isolation v0`:
  - built-in search providers now resolve Brave/Serper keys through
    `nanoclaw/security/secrets.py` instead of reading config or environment
    ad hoc from each provider path
  - `tools.secretIsolation.allowEnvironmentFallback` now gates whether tool
    secret capabilities may come from process environment variables
  - `tools.secretIsolation.auditAccess` now controls whether secret-capability
    grants or blocks are written into audit/replay as `secret_access` events
  - auto-provider selection now respects the same secret broker policy instead
    of bypassing it through direct config/env inspection
  - `SecurityDoctor` now reports when tool secret access falls back to env or
    when secret-capability audit is disabled
- Extended the shared boundary policy to file and web paths:
  - new `nanoclaw/security/boundary.py` centralizes high-risk file and outbound
    web checks
  - `file_read`, `file_write`, `web_fetch`, and workflow export writes now
    reuse the same policy entrypoint
  - outbound host policy still uses `tools.webSearch.allowedHosts /
    blockedHosts`, but enforcement no longer lives only inside `web.py`
  - boundary decisions collected during one tool run now flush into
    `audit_log` as `boundary_decision` events and show up in task replay / CLI
- Shipped outbound host policy for web tooling:
  - `tools.webSearch.allowedHosts / blockedHosts` now define an explicit outbound
    host policy
  - policy checks now run before `web_fetch`, RSS source retrieval, paper API calls,
    and Brave/Serper provider requests
  - host rules match the configured hostname plus its subdomains
  - provider-specific fallback no longer bypasses explicit host-policy blocks

## 2026-03-09

- Shipped collaboration runtime v2 slice:
  - background `workflow_role` tasks can run one isolated role LLM turn
  - runtime role payloads now persist explicit execution briefs and handoff contracts
  - deterministic role fallback remains in place when no role LLM is available
  - matching role tasks now inherit persisted resume evidence from prior degraded runs
    in the same originating parent session, even across different parent tasks
  - role runtime steps now record attempt number, remaining budget, and resume metadata
  - degraded runtime role turns can now enqueue an explicit recovery role task and avoid
    advancing the failed downstream branch
  - recovery now carries both `resume_checkpoint_id` and `recovery_task_key`, enabling
    evidence-gap recoveries to restore from `router@pre_llm` while resuming work at
    `executor@tool_phase`
  - each workflow chain now carries an explicit `workflow_identity`, which is stored in
    `workflow_runs`, replay payloads, runtime task payloads, role bridge payloads, and
    recovery payloads
  - role resume lookup now prefers exact `workflow_name + workflow_identity` matches and
    falls back to legacy `session_id / parent_session_id` lookup for older rows
  - audit schema migration now adds `workflow_identity` plus a workflow-identity index
    without requiring backfill for existing rows
  - downstream `critic / summarizer` role tasks now carry `turn_index`,
    `turn_budget`, `upstream_input_fingerprint`, `turn_reason`, and `turn_history`
  - when upstream recovery changes dependency outputs, the runtime now rearms the same
    succeeded downstream role task instead of silently reusing a stale completed row
  - recovery-targeted `executor` turns now also reuse the same succeeded
    `workflow_role` task row for one explicit `recovery_reentry` turn when budget allows
  - when a recovery target is already pending, the runtime now refreshes that same
    `workflow_role` payload in place instead of returning a stale queued task unchanged
  - when a recovery target is already running, the runtime now stages one deferred
    recovery refresh on that same task and rearms it in place after the current turn
    finishes
  - `TaskStore` now supports explicit same-task rearm for succeeded `workflow_role`
    tasks, preserving task identity while clearing only runtime claim state
  - `TaskStore` now also supports explicit payload refresh for pending `workflow_role`
    tasks, so queued recovery targets can absorb new recovery state without a new row
  - `TaskStore` now also supports explicit payload refresh for running `workflow_role`
    tasks, so live recovery targets can stage deferred refresh without losing their claim

## 2026-03-08

- Shipped search evolution v1:
  - workflow registry and switchable workflow defaults
  - deterministic query planner for `news / web / paper / site / chinese_web / long_tail`
  - search-result normalizer/reranker for `rss / brave / serper`
- Shipped runtime source fairness and queue scheduling improvements:
  - multi-source claim path
  - less-saturated source tie-break
  - shared rate-limit buckets

## 2026-03-07

- Shipped runtime foundation:
  - persistent task store and task state machine
  - persisted `spawn_task` queueing
  - worker leases and orphan recovery
  - CLI/dashboard task visibility
  - restart-recovery integration coverage
- Shipped scheduler and reliability layers:
  - priority, timeout, cancel, retry/backoff
  - starvation protection
  - cross-worker global pool limits
  - dead-letter visibility and manual requeue
  - workflow/task replay surfaces

## Earlier Foundations

- Shipped capability catalog and default workflow surfacing.
- Shipped plugin manifest loading, workflow telemetry, grounding strategy, and provider
  registry scaffolding.
- Shipped heartbeat checklist workflow and Feishu webhook diagnostics.
