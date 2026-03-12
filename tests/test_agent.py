"""Agent loop tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

import pytest

from nanoclaw.core.agent import Agent
from nanoclaw.core.context import ContextBuilder
from nanoclaw.core.llm import LLMResponse, TokenUsage, ToolCall
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.security.budget import SessionBudget
from nanoclaw.security.prompt_guard import PromptGuard
from nanoclaw.tools.runtime_context import (
    record_boundary_decision,
    record_secret_access,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)


class FakeLLM:
    """Deterministic LLM stub for agent tests."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        extra_payload: Optional[dict[str, Any]] = None,
    ) -> LLMResponse:
        """Return a tool call on the first call, and final text on the second."""
        self.calls += 1
        self.seen_messages.append(copy.deepcopy(messages))

        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="web_search",
                        arguments={"query": "hello"},
                    )
                ],
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
            )

        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
        )


class FakeToolRegistry:
    """Minimal tool registry stub."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return a single core tool schema."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Fake search tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_callback: Optional[Any] = None,
    ) -> str:
        """Record tool execution and return a result."""
        self.executed.append((name, arguments))
        return "search result"


class EvidenceToolRegistry(FakeToolRegistry):
    """Tool registry that returns URL-bearing evidence output."""

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_callback: Optional[Any] = None,
    ) -> str:
        """Record tool execution and return structured evidence text."""
        self.executed.append((name, arguments))
        return "\n".join(
            [
                "Example article",
                "https://example.com/article",
                "Example snippet",
            ]
        )


class ErrorToolRegistry(FakeToolRegistry):
    """Tool registry that always fails one evidence-bearing tool call."""

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_callback: Optional[Any] = None,
    ) -> str:
        """Raise one deterministic error for recovery-path tests."""
        self.executed.append((name, arguments))
        raise RuntimeError("boom")


class BoundaryAwareToolRegistry(FakeToolRegistry):
    """Tool registry that emits one boundary decision during execution."""

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_callback: Optional[Any] = None,
    ) -> str:
        """Record one synthetic boundary decision and return a result."""
        self.executed.append((name, arguments))
        record_boundary_decision(
            {
                "operation": "web_search",
                "boundary_kind": "outbound_url",
                "action": "fetch",
                "target": "https://api.search.brave.com/res/v1/web/search",
                "decision": "allowed",
                "policy_name": "shared_tool_boundary",
            }
        )
        return "search result"


class SecretAwareToolRegistry(FakeToolRegistry):
    """Tool registry that emits one secret access during execution."""

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_callback: Optional[Any] = None,
    ) -> str:
        """Record one synthetic secret-access event and return a result."""
        self.executed.append((name, arguments))
        record_secret_access(
            {
                "capability": "web_search.serper_api_key",
                "decision": "granted",
                "source": "config:tools.webSearch.serperApiKey",
                "policy_name": "tool_secret_broker",
            }
        )
        return "search result"


class RepeatedFailureLLM(FakeLLM):
    """LLM stub that retries one failed tool path twice before finishing."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        extra_payload: Optional[dict[str, Any]] = None,
    ) -> LLMResponse:
        """Return the same tool call twice, then a final response."""
        self.calls += 1
        self.seen_messages.append(copy.deepcopy(messages))

        if self.calls <= 2:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call_{self.calls}",
                        name="web_search",
                        arguments={"query": "hello"},
                    )
                ],
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
            )

        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
        )


class FinalOnlyLLM(FakeLLM):
    """LLM stub that returns one direct final response."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        extra_payload: Optional[dict[str, Any]] = None,
    ) -> LLMResponse:
        """Return one final response without any tool call."""
        self.calls += 1
        self.seen_messages.append(copy.deepcopy(messages))
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
        )


class FakeMemoryStore:
    """In-memory memory store stub."""

    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []
        self.memories: list[dict[str, Any]] = []

    async def get_history(self, session_id: str, limit: int = 15) -> list[dict]:
        """Return stored history."""
        return self.history[-limit:]

    async def search_memories(self, query: str, limit: int = 5) -> list[dict]:
        """Return no memories by default."""
        return []

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
    ) -> None:
        """Store a message."""
        self.history.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "tool_name": tool_name,
            }
        )

    async def save_memory(self, content: str, category: str = "auto") -> None:
        """Store a memory fact."""
        self.memories.append({"content": content, "category": category})


class FakeAuditLog:
    """Audit log stub for tests."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.workflow_runs: list[dict[str, Any]] = []
        self.tool_traces: list[dict[str, Any]] = []
        self.resume_state: Optional[dict[str, Any]] = None
        self.resume_queries: list[tuple[str, str, str]] = []

    async def log(self, **kwargs: Any) -> None:
        """Record audit entries."""
        self.entries.append(kwargs)

    async def log_workflow_run(self, **kwargs: Any) -> None:
        """Record workflow telemetry entries."""
        self.workflow_runs.append(kwargs)

    async def log_tool_trace(self, **kwargs: Any) -> None:
        """Record structured task-scoped tool traces."""
        self.tool_traces.append(kwargs)

    async def get_latest_role_resume_state(
        self,
        session_id: str,
        workflow_name: str,
        workflow_identity: str = "",
        limit: int = 20,
    ) -> Optional[dict[str, Any]]:
        """Return one pre-seeded persistent resume state."""
        self.resume_queries.append((session_id, workflow_name, workflow_identity))
        return self.resume_state


@pytest.mark.asyncio
async def test_agent_executes_tool_and_returns_response() -> None:
    """Agent should execute tool calls and return final response."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = FakeToolRegistry()
    audit = FakeAuditLog()
    budget = SessionBudget(max_iterations=5)
    prompt_guard = PromptGuard()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=budget,
        prompt_guard=prompt_guard,
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s1")
    assert result == "done"
    assert tools.executed == [("web_search", {"query": "hello"})]

    tool_msgs = [
        m for m in llm.seen_messages[1] if m.get("role") == "tool"
    ]
    assert tool_msgs
    assert "<tool_result" in tool_msgs[0]["content"]
    assert memory.history[0]["role"] == "user"
    assert len(audit.workflow_runs) == 1
    workflow = audit.workflow_runs[0]
    assert workflow["workflow_identity"].startswith("workflow_")
    workflow_contexts = [
        item
        for item in workflow["call_chain"]
        if item.get("type") == "workflow_context" and item.get("name") == "workflow_identity"
    ]
    assert workflow_contexts == [
        {
            "type": "workflow_context",
            "name": "workflow_identity",
            "status": "attached",
            "value": workflow["workflow_identity"],
        }
    ]
    assert workflow["workflow_name"] == "default_chat_loop"
    assert workflow["status"] == "success"
    assert workflow["tool_calls"] == 1
    assert workflow["llm_calls"] == 2
    checkpoint_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_checkpoint"
    ]
    llm_events = [item for item in workflow["call_chain"] if item.get("type") == "llm"]
    tool_events = [item for item in workflow["call_chain"] if item.get("type") == "tool"]
    role_exec_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_execution"
    ]
    assert checkpoint_events[0]["checkpoint_id"] == "planner@pre_llm"
    assert llm_events[0]["type"] == "llm"
    assert tool_events[0]["name"] == "web_search"
    assert [(item["role"], item["stage"]) for item in role_exec_events] == [
        ("planner", "pre_llm"),
        ("router", "pre_llm"),
        ("critic", "post_tools"),
        ("summarizer", "post_tools"),
    ]
    assert role_exec_events[0]["checkpoint_id"] == "planner@pre_llm"


@pytest.mark.asyncio
async def test_agent_uses_configured_default_chat_workflow_label() -> None:
    """Configured workflow defaults should drive the primary workflow label."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = FakeToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
        workflow_defaults={"chat": "scheduled_job_flow"},
    )

    result = await agent.run("search please", session_id="s1")

    assert result == "done"
    assert len(audit.workflow_runs) == 1
    workflow = audit.workflow_runs[0]
    assert workflow["workflow_name"] == "scheduled_job_flow"
    assert workflow["workflow_tags"][0] == "scheduled_job_flow"


@pytest.mark.asyncio
async def test_agent_logs_role_plan_and_shared_evidence_in_workflow_trace() -> None:
    """Workflow trace should include role-plan and shared-evidence events."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = EvidenceToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s1")

    assert result == "done"
    assert len(audit.workflow_runs) == 1
    workflow = audit.workflow_runs[0]
    role_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role"
    ]
    checkpoint_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_checkpoint"
    ]
    evidence_events = [
        item for item in workflow["call_chain"] if item.get("type") == "shared_evidence"
    ]
    handoff_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_handoff"
    ]
    role_task_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_task"
    ]
    role_task_bridge_events = [
        item
        for item in workflow["call_chain"]
        if item.get("type") == "workflow_role_task_bridge"
    ]
    role_exec_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_execution"
    ]

    assert [item["role"] for item in role_events] == [
        "planner",
        "router",
        "executor",
        "critic",
        "summarizer",
    ]
    assert len(evidence_events) == 1
    assert checkpoint_events[0]["checkpoint_id"] == "planner@pre_llm"
    assert evidence_events[0]["status"] == "captured"
    assert evidence_events[0]["count"] == 1
    assert evidence_events[0]["items"][0]["evidence_id"] == "ev_1"
    assert evidence_events[0]["items"][0]["url"] == "https://example.com/article"
    tool_events = [item for item in workflow["call_chain"] if item.get("type") == "tool"]
    context_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_context"
    ]
    assert [(item["role"], item["stage"]) for item in role_exec_events] == [
        ("planner", "pre_llm"),
        ("router", "pre_llm"),
        ("critic", "post_tools"),
        ("summarizer", "post_tools"),
    ]
    assert tool_events[0]["evidence_refs"] == ["ev_1"]
    assert tool_events[0]["evidence_count"] == 1
    assert {item["name"] for item in context_events} == {
        "workflow_identity",
        "shared_evidence_brief",
    }
    assert {
        item["name"]: item
        for item in context_events
    }["workflow_identity"] == {
        "type": "workflow_context",
        "name": "workflow_identity",
        "status": "attached",
        "value": workflow["workflow_identity"],
    }
    assert {
        item["name"]: item
        for item in context_events
    }["shared_evidence_brief"] == {
        "type": "workflow_context",
        "name": "shared_evidence_brief",
        "status": "attached",
        "count": 1,
        "evidence_refs": ["ev_1"],
    }
    assert [(item["from_role"], item["to_role"]) for item in handoff_events] == [
        ("planner", "router"),
        ("router", "executor"),
        ("executor", "critic"),
        ("critic", "summarizer"),
    ]
    assert [item["task_key"] for item in role_task_events] == [
        "planner@pre_llm",
        "router@pre_llm",
        "executor@tool_phase",
        "critic@post_tools",
        "summarizer@post_tools",
    ]
    assert role_task_events[2]["depends_on"] == ["router@pre_llm"]
    assert role_task_events[2]["resume_checkpoint_id"] == "router@pre_llm"
    assert role_task_events[2]["retry_budget"] == 2
    assert role_task_bridge_events[2]["task_type"] == "workflow_role"
    assert role_task_bridge_events[2]["source"] == "workflow_role"
    assert role_task_bridge_events[2]["payload"]["workflow_name"] == "default_chat_loop"
    assert role_task_bridge_events[2]["payload"]["depends_on"] == ["router@pre_llm"]
    assert handoff_events[2]["contract"]["evidence_ids"] == ["ev_1"]
    internal_msgs = [
        item
        for item in llm.seen_messages[0]
        if item.get("role") == "user"
        and "Planner phase" in item.get("content", "")
    ]
    assert internal_msgs
    assert "search please" in internal_msgs[0]["content"]
    internal_msgs = [
        item
        for item in llm.seen_messages[1]
        if item.get("role") == "user"
        and "Shared evidence is available" in item.get("content", "")
    ]
    assert internal_msgs
    assert "ev_1" in internal_msgs[0]["content"]
    critic_msgs = [
        item
        for item in llm.seen_messages[1]
        if item.get("role") == "user"
        and "Critic phase" in item.get("content", "")
    ]
    assert critic_msgs


@pytest.mark.asyncio
async def test_agent_logs_role_recovery_when_tool_execution_fails() -> None:
    """Workflow trace should include role recovery after one tool failure."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = ErrorToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s1")

    assert result == "done"
    assert len(audit.workflow_runs) == 1
    workflow = audit.workflow_runs[0]
    recovery_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_recovery"
    ]

    assert workflow["status"] == "degraded"
    assert len(recovery_events) == 1
    assert recovery_events[0]["failed_role"] == "executor"
    assert recovery_events[0]["recovery_role"] == "router"
    assert recovery_events[0]["stage"] == "post_tools"
    assert recovery_events[0]["reason"] == "web_search:error"
    assert recovery_events[0]["resume_checkpoint_id"] == "router@pre_llm"
    assert recovery_events[0]["recovery_task_key"] == "router@pre_llm"
    assert recovery_events[0]["attempt_number"] == 1
    assert recovery_events[0]["budget_limit"] == 2
    assert recovery_events[0]["remaining_budget"] == 1
    assert recovery_events[0]["status"] == "resumed"
    assert recovery_events[0]["restored_messages"] > 0
    assert not [item for item in llm.seen_messages[1] if item.get("role") == "tool"]
    recovery_msgs = [
        item
        for item in llm.seen_messages[1]
        if item.get("role") == "user" and "Role recovery." in item.get("content", "")
    ]
    assert recovery_msgs


@pytest.mark.asyncio
async def test_agent_decrements_role_recovery_budget_across_repeated_failures() -> None:
    """Repeated role recoveries should consume the per-role retry budget."""
    llm = RepeatedFailureLLM()
    memory = FakeMemoryStore()
    tools = ErrorToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=6),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=6,
    )

    result = await agent.run("search please", session_id="s1")

    assert result == "done"
    recovery_events = [
        item
        for item in audit.workflow_runs[0]["call_chain"]
        if item.get("type") == "workflow_role_recovery"
    ]

    assert [item["attempt_number"] for item in recovery_events] == [1, 2]
    assert [item["remaining_budget"] for item in recovery_events] == [1, 0]
    assert all(item["status"] == "resumed" for item in recovery_events)
    assert not [item for item in llm.seen_messages[1] if item.get("role") == "tool"]
    assert not [item for item in llm.seen_messages[2] if item.get("role") == "tool"]


@pytest.mark.asyncio
async def test_agent_attaches_persistent_role_resume_before_new_run() -> None:
    """Agent should preload persisted shared evidence from one prior failed run."""
    llm = FinalOnlyLLM()
    memory = FakeMemoryStore()
    tools = FakeToolRegistry()
    audit = FakeAuditLog()
    audit.resume_state = {
        "source_workflow_run_id": 42,
        "workflow_name": "default_chat_loop",
        "workflow_status": "degraded",
        "failure_reason": "web_search:error",
        "resume_checkpoint_id": "router@pre_llm",
        "role": "router",
        "stage": "pre_llm",
        "evidence_refs": ["ev_1"],
        "evidence_snapshot": {
            "count": 1,
            "tools": ["web_search"],
            "items": [
                {
                    "evidence_id": "ev_1",
                    "tool_name": "web_search",
                    "url": "https://example.com/article",
                    "title": "Example article",
                    "snippet": "Example snippet",
                }
            ],
        },
    }
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s1")

    assert result == "done"
    assert len(audit.resume_queries) == 1
    assert audit.resume_queries[0][:2] == ("s1", "default_chat_loop")
    assert audit.resume_queries[0][2].startswith("workflow_")
    first_call_messages = llm.seen_messages[0]
    resume_msgs = [
        item
        for item in first_call_messages
        if item.get("role") == "user"
        and "Resume from persisted role checkpoint" in item.get("content", "")
    ]
    assert resume_msgs
    assert "ev_1" in resume_msgs[0]["content"]
    workflow = audit.workflow_runs[0]
    resume_events = [
        item for item in workflow["call_chain"] if item.get("type") == "workflow_role_resume"
    ]
    assert len(resume_events) == 1
    assert resume_events[0]["source_workflow_run_id"] == 42
    assert resume_events[0]["resume_checkpoint_id"] == "router@pre_llm"
    assert resume_events[0]["restored_evidence_count"] == 1
    assert resume_events[0]["evidence_refs"] == ["ev_1"]


@pytest.mark.asyncio
async def test_agent_role_runtime_bridge_includes_parent_task_context() -> None:
    """Runtime bridge specs should carry the current parent task id when present."""
    llm = FinalOnlyLLM()
    memory = FakeMemoryStore()
    tools = FakeToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    token = set_tool_runtime_context(
        session_id="task:parent",
        task_id="task_parent_1",
        workflow_identity="workflow_task_ctx",
    )
    try:
        result = await agent.run("search please", session_id="task:parent")
    finally:
        reset_tool_runtime_context(token)

    assert result == "done"
    assert audit.workflow_runs[0]["workflow_identity"] == "workflow_task_ctx"
    bridge_events = [
        item
        for item in audit.workflow_runs[0]["call_chain"]
        if item.get("type") == "workflow_role_task_bridge"
    ]
    assert bridge_events
    assert bridge_events[0]["payload"]["parent_task_id"] == "task_parent_1"
    assert bridge_events[0]["payload"]["workflow_identity"] == "workflow_task_ctx"


@pytest.mark.asyncio
async def test_agent_logs_parent_session_context_for_task_runs(tmp_path: Path) -> None:
    """Task-scoped workflow telemetry should keep the originating parent session id."""
    llm = FinalOnlyLLM()
    memory = FakeMemoryStore()
    tools = FakeToolRegistry()
    audit = FakeAuditLog()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    parent = await store.create_task(
        "background parent",
        source="spawn_task",
        payload={
            "parent_session_id": "telegram:42",
            "workflow_identity": "workflow_parent_payload",
        },
    )
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    token = set_tool_runtime_context(
        session_id=f"task:{parent['task_id']}",
        task_id=parent["task_id"],
        step_id="agent_run",
        task_attempt=1,
    )
    try:
        result = await agent.run("search please", session_id=f"task:{parent['task_id']}")
    finally:
        reset_tool_runtime_context(token)

    assert result == "done"
    workflow_contexts = [
        item
        for item in audit.workflow_runs[0]["call_chain"]
        if item.get("type") == "workflow_context" and item.get("name") == "parent_session_id"
    ]
    assert workflow_contexts == [
        {
            "type": "workflow_context",
            "name": "parent_session_id",
            "status": "attached",
            "value": "telegram:42",
        }
    ]
    identity_contexts = [
        item
        for item in audit.workflow_runs[0]["call_chain"]
        if item.get("type") == "workflow_context" and item.get("name") == "workflow_identity"
    ]
    assert identity_contexts == [
        {
            "type": "workflow_context",
            "name": "workflow_identity",
            "status": "attached",
            "value": "workflow_parent_payload",
        }
    ]
    assert audit.workflow_runs[0]["workflow_identity"] == "workflow_parent_payload"


@pytest.mark.asyncio
async def test_agent_appends_serper_quota_note_to_final_response() -> None:
    """Serper quota note should be preserved in the final user-visible response."""

    class QuotaToolRegistry(FakeToolRegistry):
        async def execute(
            self,
            name: str,
            arguments: dict[str, Any],
            confirm_callback: Optional[Any] = None,
        ) -> str:
            self.executed.append((name, arguments))
            return "search result\nSerper quota remaining: 2399/2400"

    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = QuotaToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s-quota")
    assert "done" in result
    assert "Serper quota remaining: 2399/2400" in result


@pytest.mark.asyncio
async def test_agent_logs_task_scoped_tool_traces() -> None:
    """Task-scoped tool execution should emit structured tool traces."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = FakeToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    token = set_tool_runtime_context(
        session_id="task:t1",
        task_id="task_123",
        step_id="agent_run",
        task_attempt=2,
    )
    try:
        result = await agent.run("search please", session_id="task:task_123")
    finally:
        reset_tool_runtime_context(token)

    assert result == "done"
    assert len(audit.tool_traces) == 1
    trace = audit.tool_traces[0]
    assert trace["task_id"] == "task_123"
    assert trace["step_id"] == "agent_run"
    assert trace["attempt_number"] == 2
    assert trace["tool_name"] == "web_search"
    assert trace["status"] == "success"


@pytest.mark.asyncio
async def test_agent_logs_boundary_decisions_to_audit() -> None:
    """Boundary decisions collected during tool execution should be audited."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = BoundaryAwareToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s-boundary")

    assert result == "done"
    boundary_entries = [
        item for item in audit.entries if item.get("action_type") == "boundary_decision"
    ]
    assert len(boundary_entries) == 1
    assert boundary_entries[0]["tool_name"] == "web_search"
    assert "boundary=outbound_url" in boundary_entries[0]["input_summary"]
    assert "decision=allowed" in boundary_entries[0]["output_summary"]


@pytest.mark.asyncio
async def test_agent_logs_secret_access_to_audit() -> None:
    """Secret access collected during tool execution should be audited."""
    llm = FakeLLM()
    memory = FakeMemoryStore()
    tools = SecretAwareToolRegistry()
    audit = FakeAuditLog()
    agent = Agent(
        llm=llm,
        memory=memory,
        tools=tools,
        audit=audit,
        budget=SessionBudget(max_iterations=5),
        prompt_guard=PromptGuard(),
        context_builder=ContextBuilder(),
        max_iterations=5,
    )

    result = await agent.run("search please", session_id="s-secret")

    assert result == "done"
    secret_entries = [
        item for item in audit.entries if item.get("action_type") == "secret_access"
    ]
    assert len(secret_entries) == 1
    assert secret_entries[0]["tool_name"] == "web_search"
    assert "capability=web_search.serper_api_key" in secret_entries[0]["input_summary"]
    assert "decision=granted" in secret_entries[0]["output_summary"]
