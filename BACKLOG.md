# BACKLOG

## Features

### R1 hardening backlog

These items are intentionally out of the active roadmap phase. `R1 Security
Boundary v1` is closed; the items below are follow-on hardening work, not the
mainline product phase.

- Stabilize one cross-host stronger backend so the default `portable` path is
  less dependent on feasibility fallback behavior.
- Extend secret-capability scoping beyond built-in search providers to broader
  provider and channel secret surfaces.
- Add longer-window drift history, alert thresholds, and operator-facing policy
  history beyond the current compact 24-hour view.

### R2 hardening backlog

These items are intentionally out of the active roadmap phase. `R2 Gateway /
Channel Platform v1` is closed; the items below are follow-on hardening work,
not the mainline product phase.

- Deepen gateway control beyond the current in-process operator path so
  desired-state orchestration is less runtime-local.
- Add longer-window channel health, diagnostics history, and operator-facing
  drift or failure summaries beyond the current compact status surface.
- Expand channel/platform breadth without reopening hardcoded contract forks.

### R3 hardening backlog

These items are intentionally out of the active roadmap phase. `R3
Extensibility v1` is closed; the items below are follow-on hardening work, not
the mainline product phase.

- Extend stronger runtime isolation beyond `search_provider` plus proactive-only
  `channel`, especially for incoming-capable channels or heavier extension
  surfaces.
- Replace the current local shared-secret publisher trust model with stronger
  signed-distribution governance such as revocation feeds or a more robust key
  lifecycle.
- Continue collapsing operator-facing extension/runtime catalog text so more of
  it comes directly from shared manifest or registry data instead of parallel
  presentation-specific logic.

### R4 hardening backlog

These items are intentionally out of the active roadmap phase. `R4 Runtime /
Workflow Autonomy v1` is closed; the items below are follow-on hardening work,
not the mainline product phase.

- Use the shipped richer scheduler policy inside more built-in workflow graphs
  instead of keeping default workflow paths intentionally conservative.
- Add tighter workflow adaptation policy on top of evaluation and persona
  signals without reopening raw prompt editing or ad hoc runtime mutation.

### Tailscale Serve: Auto-approve device pairing

When accessing webchat via Tailscale Serve with `gateway.auth.allowTailscale: true`, users must manually approve device pairing every time browser localStorage gets cleared. This creates a chicken-and-egg problem: need to approve from another session before using webchat.

Request: add config option to auto-approve device pairing when request comes from Tailscale Serve with valid identity headers. If user trusts Tailscale enough to skip token auth, they should be able to trust it for device pairing too.

Priority: Low (edge case for advanced self-hosted setups)

### Heavy search backend contingency

Trigger only when `RSS / Brave / Serper / optional SearXNG` still leave a clear
coverage gap after the `SearXNG` evaluation is finished.

Request: add a heavier search backend only if the lighter search stack still
cannot cover the target query classes with acceptable reliability.

Default action: do not start this before the `SearXNG` track is complete.

Priority: Deferred contingency, not active roadmap work
