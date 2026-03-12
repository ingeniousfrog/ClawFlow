"""Tests for lightweight collaboration helpers."""

from __future__ import annotations

import pytest

from nanoclaw.core.collaboration import (
    build_persistent_resume_brief,
    build_role_execution_briefs,
    build_role_recovery_action,
    build_role_runtime_execution_result,
    build_role_runtime_task_specs,
    build_role_task_envelopes,
    RoleRuntimeState,
    SharedEvidenceStore,
    build_role_handoffs,
    build_role_plan,
    build_shared_evidence_brief,
)


def test_build_role_plan_returns_stable_role_order() -> None:
    """Role plan should expose the expected workflow collaboration order."""
    plan = build_role_plan(
        workflow_name="grounded_current_info",
        user_message="Help me verify today's AI news.",
        tool_names=["web_search"],
        needs_grounded=True,
    )

    assert [step.role for step in plan] == [
        "planner",
        "router",
        "executor",
        "critic",
        "summarizer",
    ]
    assert "evidence" in plan[0].summary.lower()
    assert "provider" in plan[1].summary.lower()


def test_build_role_plan_uses_article_specific_role_summaries() -> None:
    """WeChat article workflow should expose article-writing role semantics."""
    plan = build_role_plan(
        workflow_name="wechat_article_flow",
        user_message="帮我写一篇视频生成模型加速周报",
        tool_names=["wechat_article_assist", "web_search"],
        needs_grounded=True,
    )

    assert [step.role for step in plan] == [
        "planner",
        "router",
        "executor",
        "critic",
        "summarizer",
    ]
    assert "article angle" in plan[0].summary.lower()
    assert "evidence mix" in plan[1].summary.lower()
    assert "publish-ready wechat article package" in plan[4].summary.lower()


def test_shared_evidence_store_deduplicates_urls() -> None:
    """Shared evidence store should keep one item per normalized URL."""
    store = SharedEvidenceStore()

    first_added = store.add_tool_output(
        "web_search",
        "\n".join(
            [
                "Example title",
                "https://example.com/article).",
                "Example snippet",
            ]
        ),
    )
    second_added = store.add_tool_output(
        "web_search",
        "\n".join(
            [
                "Another title",
                "https://example.com/article",
                "Another snippet",
            ]
        ),
    )

    snapshot = store.snapshot()

    assert first_added == 1
    assert second_added == 0
    assert snapshot["count"] == 1
    assert snapshot["tools"] == ["web_search"]
    assert snapshot["items"][0]["evidence_id"] == "ev_1"
    assert snapshot["items"][0]["url"] == "https://example.com/article"
    assert snapshot["items"][0]["title"] == "Example title"


def test_collect_tool_output_returns_referenced_evidence_ids() -> None:
    """Tool-output collection should return stable evidence references."""
    store = SharedEvidenceStore()

    result = store.collect_tool_output(
        "web_search",
        "\n".join(
            [
                "Example title",
                "https://example.com/article",
                "Example snippet",
                "https://example.com/article",
            ]
        ),
    )

    assert result == {"added": 1, "evidence_ids": ["ev_1"]}


def test_build_role_handoffs_include_evidence_references() -> None:
    """Role handoffs should carry stable evidence references between roles."""
    handoffs = build_role_handoffs(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        failure_reason="",
    )

    assert [(item.from_role, item.to_role) for item in handoffs] == [
        ("planner", "router"),
        ("router", "executor"),
        ("executor", "critic"),
        ("critic", "summarizer"),
    ]
    assert handoffs[2].contract["evidence_ids"] == ["ev_1"]
    assert handoffs[3].contract["verdict"] == "grounded"


def test_build_role_handoffs_for_wechat_article_include_publish_gate() -> None:
    """Article workflow handoffs should carry publish-specific contracts."""
    handoffs = build_role_handoffs(
        workflow_name="wechat_article_flow",
        tool_names=["wechat_article_assist", "web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        failure_reason="",
    )

    assert handoffs[0].contract["article_mode"] == "wechat_publish"
    assert handoffs[1].contract["evidence_strategy"] == "user_rss_paper_merge"
    assert handoffs[2].contract["publish_gate"] == "factcheck_required"
    assert handoffs[3].contract["publish_mode"] == "wechat_article_bundle"


def test_build_shared_evidence_brief_renders_internal_context() -> None:
    """Evidence brief should include reusable evidence ids and source context."""
    brief = build_shared_evidence_brief(
        {
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        }
    )

    assert "Shared evidence is available" in brief
    assert "ev_1" in brief
    assert "https://example.com/article" in brief


def test_build_role_execution_briefs_cover_pre_and_post_tool_stages() -> None:
    """Role execution briefs should expose stable phases and evidence refs."""
    pre_briefs = build_role_execution_briefs(
        workflow_name="grounded_current_info",
        user_message="Help me verify today's AI news.",
        tool_names=[],
        needs_grounded=True,
        evidence_snapshot={"count": 0, "tools": [], "items": []},
        failure_reason="",
        stage="pre_llm",
    )
    post_briefs = build_role_execution_briefs(
        workflow_name="grounded_current_info",
        user_message="Help me verify today's AI news.",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        failure_reason="",
        stage="post_tools",
    )

    assert [(item.role, item.stage) for item in pre_briefs] == [
        ("planner", "pre_llm"),
        ("router", "pre_llm"),
    ]
    assert [(item.role, item.stage) for item in post_briefs] == [
        ("critic", "post_tools"),
        ("summarizer", "post_tools"),
    ]
    assert pre_briefs[0].checkpoint_id == "planner@pre_llm"
    assert post_briefs[0].checkpoint_id == "critic@post_tools"
    assert post_briefs[0].evidence_refs == ["ev_1"]
    assert "Planner phase" in pre_briefs[0].content
    assert "Critic phase" in post_briefs[0].content


def test_build_role_execution_briefs_use_article_role_labels() -> None:
    """Article workflow briefs should expose article-facing role labels."""
    briefs = build_role_execution_briefs(
        workflow_name="wechat_article_flow",
        user_message="写一篇视频生成模型加速周报",
        tool_names=["wechat_article_assist"],
        needs_grounded=True,
        evidence_snapshot={"count": 0, "tools": [], "items": []},
        failure_reason="",
        stage="pre_llm",
    )

    assert "Planner phase" in briefs[0].content
    assert "Researcher phase" in briefs[1].content


def test_build_role_task_envelopes_expose_dependency_graph() -> None:
    """Role task envelopes should expose stable dependencies and retry budgets."""
    envelopes = build_role_task_envelopes(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        run_status="degraded",
    )

    assert [item.task_key for item in envelopes] == [
        "planner@pre_llm",
        "router@pre_llm",
        "executor@tool_phase",
        "critic@post_tools",
        "summarizer@post_tools",
    ]
    assert envelopes[2].depends_on == ["router@pre_llm"]
    assert envelopes[2].resume_checkpoint_id == "router@pre_llm"
    assert envelopes[2].retry_budget == 2
    assert envelopes[2].turn_budget == 2
    assert envelopes[3].turn_budget == 2
    assert envelopes[4].turn_budget == 2
    assert envelopes[2].evidence_refs == ["ev_1"]


def test_build_role_task_envelopes_use_configured_role_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role budgets should honor configured workflow role policy overrides."""

    class FakeWorkflowRolePolicy:
        enable_graph_fanout = True

        @staticmethod
        def get_retry_budget(role: str, default: int) -> int:
            return {"executor": 4}.get(role, default)

        @staticmethod
        def get_turn_budget(role: str, default: int) -> int:
            return {"planner": 2, "critic": 3}.get(role, default)

    class FakeAgentConfig:
        workflow_role_policy = FakeWorkflowRolePolicy()

    class FakeConfig:
        agent = FakeAgentConfig()

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())

    envelopes = build_role_task_envelopes(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={"count": 0, "tools": [], "items": []},
        run_status="success",
    )

    assert envelopes[0].turn_budget == 2
    assert envelopes[2].retry_budget == 4
    assert envelopes[3].turn_budget == 3


def test_build_persistent_resume_brief_renders_prior_run_context() -> None:
    """Persistent resume brief should surface prior run status and evidence."""
    brief = build_persistent_resume_brief(
        {
            "source_workflow_run_id": 42,
            "workflow_status": "degraded",
            "failure_reason": "provider_timeout",
            "resume_checkpoint_id": "router@pre_llm",
            "evidence_snapshot": {
                "count": 1,
                "tools": ["web_search"],
                "items": [
                    {
                        "evidence_id": "ev_1",
                        "tool_name": "web_search",
                        "url": "https://example.com/article",
                        "title": "Example title",
                        "snippet": "Example snippet",
                    }
                ],
            },
        }
    )

    assert "Resume from persisted role checkpoint" in brief
    assert "Source run: 42" in brief
    assert "router@pre_llm" in brief
    assert "ev_1" in brief


def test_build_role_runtime_task_specs_generate_runtime_payloads() -> None:
    """Role runtime task specs should expose runtime-consumable task payloads."""
    envelopes = build_role_task_envelopes(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        run_status="degraded",
    )

    specs = build_role_runtime_task_specs(
        session_id="task:abc",
        workflow_name="grounded_current_info",
        user_message="Help me verify today's AI news.",
        role_tasks=envelopes,
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        parent_task_id="task_parent_1",
    )

    assert specs[0].task_type == "workflow_role"
    assert "Planner phase" in specs[0].payload["execution_brief"]
    assert specs[0].payload["turn_index"] == 1
    assert specs[0].payload["turn_budget"] == 1
    assert specs[0].payload["turn_reason"] == "initial"
    assert specs[2].payload["turn_budget"] == 2
    assert specs[3].payload["turn_budget"] == 2
    assert specs[4].payload["turn_budget"] == 2
    assert specs[1].payload["handoff_contract"]["provider_mode"] == "grounded"


def test_build_role_runtime_task_specs_include_article_role_identity() -> None:
    """Article workflow runtime specs should carry article-specific role labels."""
    evidence_snapshot = {
        "count": 1,
        "tools": ["wechat_article_assist"],
        "items": [
            {
                "evidence_id": "ev_1",
                "tool_name": "wechat_article_assist",
                "url": "https://example.com/article",
                "title": "Example article",
                "snippet": "Example snippet",
            }
        ],
    }
    envelopes = build_role_task_envelopes(
        workflow_name="wechat_article_flow",
        tool_names=["wechat_article_assist"],
        needs_grounded=True,
        evidence_snapshot=evidence_snapshot,
        run_status="success",
    )

    specs = build_role_runtime_task_specs(
        session_id="task:parent-1",
        workflow_name="wechat_article_flow",
        user_message="写一篇视频生成模型加速周报",
        role_tasks=envelopes,
        tool_names=["wechat_article_assist"],
        needs_grounded=True,
        evidence_snapshot=evidence_snapshot,
        parent_task_id="task_parent_1",
    )

    assert specs[1].payload["role_label"] == "researcher"
    assert specs[2].payload["role_label"] == "drafter"
    assert specs[4].payload["role_label"] == "editor"
    assert specs[2].payload["role_stage_name"] == "drafter"
    assert specs[2].payload["role_tool_enabled"] is True
    assert specs[2].task_key == "executor@tool_phase"
    assert specs[2].priority == 760
    assert specs[2].payload["parent_task_id"] == "task_parent_1"
    assert specs[2].payload["depends_on"] == ["router@pre_llm"]
    assert specs[2].payload["tool_names"] == ["wechat_article_assist"]
    assert specs[2].payload["needs_grounded"] is True
    assert "Drafter phase" in specs[2].payload["execution_brief"]
    assert specs[2].payload["handoff_contract"]["publish_gate"] == "factcheck_required"
    assert specs[2].payload["turn_budget"] == 2
    assert specs[3].payload["turn_budget"] == 2
    assert specs[4].payload["turn_budget"] == 2
    assert specs[2].payload["evidence_snapshot"]["items"][0]["evidence_id"] == "ev_1"


def test_build_role_runtime_execution_result_reuses_execution_brief() -> None:
    """Role runtime execution should reuse the same role brief semantics as the agent loop."""
    result = build_role_runtime_execution_result(
        payload={
            "workflow_name": "grounded_current_info",
            "user_summary": "Help me verify today's AI news.",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "failure_reason": "",
            "role": "router",
            "stage": "pre_llm",
            "task_key": "router@pre_llm",
            "depends_on": ["planner@pre_llm"],
            "checkpoint_id": "router@pre_llm",
            "evidence_refs": ["ev_1"],
            "evidence_snapshot": {
                "count": 1,
                "tools": ["web_search"],
                "items": [
                    {
                        "evidence_id": "ev_1",
                        "tool_name": "web_search",
                        "url": "https://example.com/article",
                        "title": "Example title",
                    }
                ],
            },
        },
        dependency_outputs=[
            {
                "task_key": "planner@pre_llm",
                "status": "succeeded",
                "action": "stable_plan_ready",
            }
        ],
    )

    assert result["handler_kind"] == "execution_brief"
    assert result["checkpoint_id"] == "router@pre_llm"
    assert "Router phase" in result["brief_content"]
    assert result["contract"]["provider_mode"] == "grounded"
    assert result["scheduler_action"] == "complete"
    assert result["turn_index"] == 1
    assert result["turn_budget"] == 1
    assert result["turn_reason"] == "initial"
    assert result["resolved_dependencies"] == ["planner@pre_llm"]


def test_build_role_runtime_execution_result_marks_recovery_reentry_turns() -> None:
    """Recovery-triggered reentry turns should expose an explicit scheduler action."""
    result = build_role_runtime_execution_result(
        payload={
            "workflow_name": "grounded_current_info",
            "role": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "turn_index": 2,
            "turn_budget": 2,
            "turn_reason": "recovery_reentry",
            "depends_on": ["router@pre_llm"],
            "evidence_snapshot": {"count": 1, "items": [{"evidence_id": "ev_1"}]},
            "evidence_refs": ["ev_1"],
        },
        dependency_outputs=[
            {
                "task_key": "router@pre_llm",
                "status": "succeeded",
                "action": "route_selected:grounded",
            }
        ],
    )

    assert result["scheduler_action"] == "rearmed_recovery"
    assert result["turn_index"] == 2
    assert result["turn_budget"] == 2
    assert result["turn_reason"] == "recovery_reentry"


def test_build_role_runtime_execution_result_uses_article_actions() -> None:
    """WeChat article runtime results should expose article-specific actions."""
    result = build_role_runtime_execution_result(
        payload={
            "workflow_name": "wechat_article_flow",
            "user_summary": "写一篇视频生成模型加速周报",
            "role": "summarizer",
            "role_label": "editor",
            "role_focus": "prepare the publish-ready article bundle",
            "stage": "post_tools",
            "task_key": "summarizer@post_tools",
            "tool_names": ["wechat_article_assist"],
            "needs_grounded": True,
            "failure_reason": "",
            "depends_on": ["critic@post_tools"],
            "checkpoint_id": "summarizer@post_tools",
            "evidence_snapshot": {
                "count": 1,
                "items": [{"evidence_id": "ev_1"}],
            },
            "evidence_refs": ["ev_1"],
        },
        dependency_outputs=[{"task_key": "critic@post_tools", "status": "succeeded"}],
    )

    assert result["action"] == "article_bundle_ready"
    assert result["handler_kind"] == "execution_brief"
    assert result["role_label"] == "editor"
    assert "Publish bundle ready" in result["artifact_preview"]
    assert result["checkpoint_id"] == "summarizer@post_tools"
    assert "editor phase" in result["brief_content"].lower()


def test_build_role_runtime_execution_result_carries_upstream_role_context() -> None:
    """Role runtime results should expose upstream actions and artifacts."""
    result = build_role_runtime_execution_result(
        payload={
            "workflow_name": "wechat_article_flow",
            "user_summary": "写一篇视频生成模型加速周报",
            "role": "executor",
            "role_label": "drafter",
            "role_focus": "assemble draft-ready article material",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "tool_names": ["wechat_article_assist"],
            "needs_grounded": True,
            "failure_reason": "",
            "evidence_snapshot": {
                "count": 1,
                "items": [{"evidence_id": "ev_1"}],
            },
            "evidence_refs": ["ev_1"],
        },
        dependency_outputs=[
            {
                "task_key": "router@pre_llm",
                "role": "router",
                "role_label": "researcher",
                "status": "succeeded",
                "action": "article_route:article_helper",
                "artifact_preview": "Evidence route ready: merge RSS and paper evidence first.",
                "tool_handler_output_preview": "## researcher",
                "evidence_refs": ["ev_1"],
            }
        ],
    )

    assert result["upstream_actions"] == ["article_route:article_helper"]
    assert result["upstream_artifacts"] == ["## researcher"]
    assert "Routed via: article_route:article_helper." in result["artifact_preview"]


def test_build_role_recovery_action_uses_router_after_executor_failure() -> None:
    """Role recovery should route execution failures back to the router."""
    action = build_role_recovery_action(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example title",
                    "snippet": "Example snippet",
                }
            ],
        },
        failure_reason="web_search:error",
        stage="post_tools",
    )

    assert action is not None
    assert action.failed_role == "executor"
    assert action.recovery_role == "router"
    assert action.reason == "web_search:error"
    assert action.resume_checkpoint_id == "router@pre_llm"
    assert action.recovery_task_key == "router@pre_llm"
    assert action.recovery_path == [
        "router@pre_llm",
        "executor@tool_phase",
        "critic@post_tools",
        "summarizer@post_tools",
    ]
    assert action.evidence_refs == ["ev_1"]
    assert "Executor failed" in action.content


def test_build_role_recovery_action_routes_critic_evidence_gaps_to_executor() -> None:
    """Grounded evidence gaps should re-enter the runtime at the executor role."""
    action = build_role_recovery_action(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={"count": 0, "tools": [], "items": []},
        failure_reason="",
        stage="post_tools",
    )

    assert action is not None
    assert action.failed_role == "critic"
    assert action.recovery_role == "executor"
    assert action.reason == "evidence_gap"
    assert action.resume_checkpoint_id == "router@pre_llm"
    assert action.recovery_task_key == "executor@tool_phase"
    assert action.recovery_path == [
        "executor@tool_phase",
        "critic@post_tools",
        "summarizer@post_tools",
    ]
    assert "Critic found no grounded evidence" in action.content


def test_build_role_recovery_action_requires_grounded_evidence_before_summary() -> None:
    """Grounded runs should attach one summarizer-to-executor recovery before final answer."""
    action = build_role_recovery_action(
        workflow_name="grounded_current_info",
        tool_names=["web_search"],
        needs_grounded=True,
        evidence_snapshot={"count": 0, "tools": [], "items": []},
        failure_reason="",
        stage="pre_final",
    )

    assert action is not None
    assert action.failed_role == "summarizer"
    assert action.recovery_role == "executor"
    assert action.reason == "grounded_search_required"
    assert action.resume_checkpoint_id == "router@pre_llm"
    assert action.recovery_task_key == "executor@tool_phase"
    assert action.recovery_path == [
        "executor@tool_phase",
        "critic@post_tools",
        "summarizer@post_tools",
    ]


def test_role_runtime_state_restores_messages_and_evidence_to_checkpoint() -> None:
    """Checkpoint restore should trim later messages and rewind shared evidence."""
    store = SharedEvidenceStore()
    runtime = RoleRuntimeState()
    messages = [{"role": "user", "content": "hello"}]

    runtime.record_checkpoint(
        checkpoint_id="router@pre_llm",
        role="router",
        stage="pre_llm",
        messages=messages,
        evidence_snapshot=store.snapshot(),
    )

    store.add_tool_output(
        "web_search",
        "\n".join(
            [
                "Example title",
                "https://example.com/article",
                "Example snippet",
            ]
        ),
    )
    messages.extend(
        [
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "result"},
        ]
    )

    restored = runtime.restore_checkpoint(
        "router@pre_llm",
        messages=messages,
        shared_evidence=store,
    )

    assert restored is not None
    assert restored.checkpoint_id == "router@pre_llm"
    assert restored.restored_messages == 2
    assert restored.restored_evidence_count == 0
    assert messages == [{"role": "user", "content": "hello"}]
    assert store.snapshot()["count"] == 0
