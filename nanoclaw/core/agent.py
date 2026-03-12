"""Main agent loop - LLM and tool execution cycle. 循环决策与执行工具"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from nanoclaw.core.collaboration import (
    RoleRuntimeState,
    SharedEvidenceStore,
    build_collaboration_events,
    build_role_execution_briefs,
    build_persistent_resume_brief,
    build_role_recovery_action,
    build_role_runtime_bridge_events,
    build_role_task_envelopes,
    build_shared_evidence_brief,
    get_role_retry_budget,
)
from nanoclaw.core.context import ContextBuilder
from nanoclaw.core.llm import LLMClient, ToolCall
from nanoclaw.core.logger import get_logger
from nanoclaw.core.workflows import (
    build_workflow_tags,
    classify_primary_workflow,
    matches_structured_grounding_workflow,
    resolve_workflow_defaults,
)
from nanoclaw.memory.store import MemoryStore
from nanoclaw.security.audit import AuditLog
from nanoclaw.security.budget import SessionBudget, SessionTracker
from nanoclaw.security.prompt_guard import PromptGuard
from nanoclaw.tools.registry import ToolRegistry
from nanoclaw.tools.runtime_context import (
    begin_boundary_decision_trace,
    begin_secret_access_trace,
    get_boundary_decision_trace,
    get_secret_access_trace,
    reset_boundary_decision_trace,
    reset_secret_access_trace,
    get_tool_runtime_context,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)

logger = get_logger(__name__)


class SessionCache:
    """In-memory cache for expensive tool results within a session."""

    def __init__(self, ttl_seconds: int = 300):
        """
        Initialize SessionCache.

        Args:
            ttl_seconds: Cache TTL in seconds (default 5 min)
        """
        self._cache: dict[str, tuple[str, float]] = {}
        self.ttl = ttl_seconds

    def get(self, key: str) -> Optional[str]:
        """Get cached value if not expired."""
        if key in self._cache:
            result, ts = self._cache[key]
            if time.time() - ts < self.ttl:
                return result
            del self._cache[key]
        return None

    def set(self, key: str, value: str) -> None:
        """Set cached value."""
        self._cache[key] = (value, time.time())

    def invalidate(self, prefix: str) -> None:
        """Invalidate cache entries matching prefix."""
        keys_to_delete = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._cache[k]

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()


class Agent:
    """Main agent that processes messages and executes tools. 调用LLM、跑工具、写历史、记审计"""

    # Tools that can be cached
    CACHEABLE = {"web_search", "web_fetch", "file_read", "memory_search"}
    # Tools that should never be cached (side effects)
    NEVER_CACHE = {"shell_exec", "file_write", "memory_save", "spawn_task"}
    DAILY_HINTS = (
        "daily",
        "daily digest",
        "morning brief",
        "日报",
        "日更",
        "今日要闻",
    )
    PAPER_HINTS = (
        "paper",
        "papers",
        "arxiv",
        "preprint",
        "论文",
        "文献",
        "期刊",
    )
    LOCAL_ACTION_HINTS = (
        "save",
        "write",
        "file",
        "workspace",
        "repo",
        "repository",
        "shell",
        "command",
        "draft",
        "outline",
        "fact-check",
        "公众号",
        "微信文章",
        "文章",
        "写入",
        "保存",
        "文件",
        "仓库",
        "代码",
    )
    DIRECT_RESPONSE_TOOLS = {"wechat_article_assist"}
    RSS_GROUNDING_TOOLS = {"daily_digest", "hotspot_brief", "web_search", "paper_search"}
    RSS_MISS_PATTERNS = (
        "no rss results found",
        "no hotspot items found",
        "has no items",
        "rss search unavailable",
        "hotspot brief unavailable",
        "daily digest unavailable",
        "rss source list is empty",
        "relevance filter: no entries matched query keywords",
        "paper search unavailable",
        "found no provider results",
        "found no usable entries",
        "found no parseable arxiv entries",
        "no valid providers configured",
    )
    NO_EVIDENCE_RESPONSE_PATTERNS = (
        "could not gather reliable live evidence",
        "no matched items for this request",
        "no rss results found",
        "paper search for",
        "found no provider results",
        "found no usable entries",
        "在我可用的实时检索/rss信源里",
        "没有返回任何可核验",
        "结果为空或命中无关内容",
        "无法在不杜撰",
        "无法提供",
    )
    OFFLINE_RESPONSE_PATTERNS = (
        "截至2024",
        "as of june 2024",
        "knowledge cutoff",
        "知识截止",
        "训练数据截止",
        "截至我的知识",
        "无法访问互联网",
    )

    def __init__(
        self,
        llm: LLMClient,
        memory: MemoryStore,
        tools: ToolRegistry,
        audit: AuditLog,
        budget: SessionBudget,
        prompt_guard: PromptGuard,
        context_builder: ContextBuilder,
        max_iterations: int = 15,
        model_routing: Optional[Any] = None,
        web_search_provider: str = "rss",
        workflow_defaults: Optional[Any] = None,
    ):
        """
        Initialize Agent.

        Args:
            llm: LLM client
            memory: Memory store
            tools: Tool registry
            audit: Audit log
            budget: Session budget
            prompt_guard: Prompt guard
            context_builder: Context builder
            max_iterations: Max iterations per message
        """
        self.llm = llm
        self.memory = memory
        self.tools = tools
        self.audit = audit
        self.budget = budget
        self.prompt_guard = prompt_guard
        self.ctx = context_builder
        self.max_iterations = max_iterations
        self.cache = SessionCache(ttl_seconds=300)
        self.model_routing = self._normalize_model_routing(model_routing)
        self.web_search_provider = web_search_provider.strip().lower() or "rss"
        self.workflow_defaults = resolve_workflow_defaults(workflow_defaults)

    async def run(
        self,
        user_message: str,
        session_id: str,
        confirm_callback: Optional[Callable] = None,
    ) -> str:
        """
        Main agent loop. Process user message, execute tools, return response.

        Uses ReAct pattern with automatic escalation: if agent struggles
        for 4+ iterations, inject a planning nudge (costs zero extra tokens).

        Args:
            user_message: User's message
            session_id: Session identifier
            confirm_callback: Async function for user confirmation

        Returns:
            Final response string
        """
        # Start session tracking
        session = SessionTracker(session_id=session_id)
        logger.debug(f"=== New message from session {session_id} ===")
        logger.debug(f"User: {user_message}")

        # 1. Load context (keep it lean for speed) 加载历史与记忆
        history = await self.memory.get_history(session_id, limit=15)
        relevant_memories = await self.memory.search_memories(user_message, limit=5)

        # 2. Build messages array
        messages = self.ctx.build_messages(user_message, history, relevant_memories)

        # Check if user explicitly wants a plan
        if self._user_wants_plan(user_message):
            # Inject planning instruction
            messages[-1]["content"] = (
                f"{user_message}\n\n"
                "Please think through this step by step. "
                "Outline your plan, then execute it."
            )

        # Dynamic tool selection
        all_tool_schemas = self.tools.get_schemas()
        tool_schemas = self.ctx.select_tools(user_message, all_tool_schemas)
        selected_model = self._select_model_for_request(user_message)
        model_fallback_used = False
        needs_grounded = self._needs_grounded_search(user_message)
        rss_attempted = False
        rss_has_hits: Optional[bool] = None
        last_model_used: Optional[str] = None
        call_chain: list[dict[str, Any]] = []
        run_status = "success"
        failure_reason = ""
        had_tool_error = False
        serper_quota_note = ""
        grounding_strategy = self._select_grounding_strategy(user_message, session_id)
        direct_web_used = False
        if selected_model:
            logger.info(f"Model routing selected `{selected_model}` for session {session_id}")
        if needs_grounded and grounding_strategy == "web_model":
            logger.info(
                "Grounding strategy selected web-enabled model route for session %s",
                session_id,
            )

        # 3. Agent loop
        final_response = ""
        escalated = False  # Track if we've already injected escalation nudge
        search_enforced = False
        role_runtime = RoleRuntimeState()
        shared_evidence = SharedEvidenceStore()
        last_evidence_context_count = 0
        attached_role_execution: set[tuple[str, str]] = set()
        attached_role_recovery: set[tuple[str, str, int]] = set()
        role_recovery_counts: dict[str, int] = {}
        workflow_identity = await self._resolve_workflow_identity()
        if not workflow_identity:
            workflow_identity = self._new_workflow_identity()

        if needs_grounded and grounding_strategy == "web_model":
            direct_result, used_model = await self._execute_direct_web_search(
                user_message=user_message,
                session=session,
                call_chain=call_chain,
            )
            if direct_result:
                direct_web_used = True
                final_response = direct_result
                last_model_used = used_model
            else:
                logger.info(
                    "Direct web-model route did not produce grounded content; "
                    "falling back to tool grounding for session %s",
                    session_id,
                )
                grounding_strategy = "tool"

        if not direct_web_used:
            last_evidence_context_count = await self._attach_persistent_role_resume(
                messages=messages,
                call_chain=call_chain,
                session_id=session_id,
                user_message=user_message,
                needs_grounded=needs_grounded,
                direct_web_used=direct_web_used,
                shared_evidence=shared_evidence,
                workflow_identity=workflow_identity,
            )

        for iteration in range(self.max_iterations if not direct_web_used else 0):
            # Budget check 预算检查（迭代数、token、速率等）
            allowed, reason = self.budget.check_iteration(session)
            if not allowed:
                run_status = "stopped"
                failure_reason = reason
                final_response = (
                    f"Stopped: {reason}. "
                    f"Here's what I have so far:\n{final_response}"
                )
                break

            session.increment_iterations()
            logger.debug(f"--- Iteration {iteration + 1} ---")
            self._attach_role_execution_briefs(
                messages=messages,
                call_chain=call_chain,
                stage="pre_llm",
                session_id=session_id,
                user_message=user_message,
                needs_grounded=needs_grounded,
                direct_web_used=direct_web_used,
                failure_reason=failure_reason,
                role_runtime=role_runtime,
                shared_evidence=shared_evidence,
                attached_roles=attached_role_execution,
            )

            # Escalation: if 4+ iterations and still calling tools, nudge LLM
            # This is an internal directive, not a request for user-facing output
            if iteration >= 4 and not escalated and len(messages) > 2:
                last_msg = messages[-1]
                if last_msg.get("role") == "tool":
                    # Collect successful results to remind LLM what it already has
                    successes = []
                    for msg in messages:
                        if msg.get("role") == "tool":
                            content = msg.get("content", "")
                            if not self._is_error_result(content):
                                # Take first 150 chars of successful result
                                successes.append(content[:150])

                    nudge = (
                        "[Internal: You have taken many iterations. "
                        "Do NOT output a plan to the user. "
                        "Stop repeating failed calls - try different search terms. "
                        "Use the successful results you already have. "
                    )
                    if successes:
                        nudge += "Successful results so far: " + " | ".join(successes[-3:]) + " "
                    nudge += "Answer the user now with available information.]"

                    messages.append({"role": "user", "content": nudge})
                    escalated = True
                    logger.info(f"Escalating after {iteration} iterations")

            # Call LLM
            try:
                llm_response = await self.llm.chat(
                    messages,
                    tools=tool_schemas,
                    model=selected_model,
                )
                last_model_used = selected_model or self._default_model_name()
                session.add_tokens(llm_response.usage.total_tokens)
                self._append_llm_event(
                    call_chain,
                    model=last_model_used,
                    status="success",
                    tokens=llm_response.usage.total_tokens,
                    tool_calls=len(llm_response.tool_calls),
                    has_content=bool(llm_response.content),
                )
            except Exception as e:
                self._append_llm_event(
                    call_chain,
                    model=selected_model or self._default_model_name(),
                    status="error",
                    error=str(e)[:200],
                )
                if selected_model and not model_fallback_used:
                    logger.warning(
                        f"Routed model `{selected_model}` failed, "
                        "fallback to default model. Error: "
                        f"{str(e)[:300]}"
                    )
                    try:
                        llm_response = await self.llm.chat(messages, tools=tool_schemas)
                        last_model_used = self._default_model_name()
                        session.add_tokens(llm_response.usage.total_tokens)
                        self._append_llm_event(
                            call_chain,
                            model=last_model_used,
                            status="success",
                            tokens=llm_response.usage.total_tokens,
                            tool_calls=len(llm_response.tool_calls),
                            has_content=bool(llm_response.content),
                            stage="fallback",
                        )
                        selected_model = None
                        model_fallback_used = True
                    except Exception as fallback_error:
                        run_status = "error"
                        error_text = str(fallback_error)
                        logger.error(f"LLM call failed: {error_text[:1000]}")
                        if len(error_text) > 200:
                            error_text = error_text[:200] + "..."
                        if not error_text.strip():
                            error_text = (
                                f"{type(fallback_error).__name__}: "
                                f"{repr(fallback_error)[:200]}"
                            )
                        failure_reason = error_text
                        self._append_llm_event(
                            call_chain,
                            model=self._default_model_name(),
                            status="error",
                            error=error_text,
                            stage="fallback",
                        )
                        final_response = f"Error communicating with LLM: {error_text}"
                        break
                else:
                    run_status = "error"
                    error_text = str(e)
                    # Log full error, truncate for user response
                    logger.error(f"LLM call failed: {error_text[:1000]}")
                    if len(error_text) > 200:
                        error_text = error_text[:200] + "..."
                    if not error_text.strip():
                        error_text = f"{type(e).__name__}: {repr(e)[:200]}"
                    failure_reason = error_text
                    final_response = f"Error communicating with LLM: {error_text}"
                    break

            # If text response with no tool calls -> done
            if llm_response.content and not llm_response.tool_calls:
                # Guardrail: for time-sensitive/trend requests, require at least
                # one evidence-gathering tool call before answering.
                if (
                    needs_grounded
                    and not rss_attempted
                ):
                    if not search_enforced:
                        self._attach_role_recovery_brief(
                            messages=messages,
                            call_chain=call_chain,
                            stage="pre_final",
                            session_id=session_id,
                            user_message=user_message,
                            needs_grounded=needs_grounded,
                            direct_web_used=direct_web_used,
                            failure_reason="",
                            role_runtime=role_runtime,
                            shared_evidence=shared_evidence,
                            attached_recoveries=attached_role_recovery,
                            role_recovery_counts=role_recovery_counts,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                "Before answering this request, you MUST call "
                                "`daily_digest`, `hotspot_brief`, or "
                                "`web_search`, or `paper_search` to gather "
                                "current evidence. Use source-aware bilingual "
                                "keywords and include URLs in the final answer. "
                                "Do not answer from prior knowledge."
                            ),
                        }
                        )
                        search_enforced = True
                        logger.info(
                            "Enforcing grounded search before final response "
                            f"for session {session_id}"
                        )
                        continue

                    final_response = (
                        "I could not gather reliable live evidence from configured "
                        "sources for this request, so I will not provide an "
                        "ungrounded trend conclusion. Please refine the topic "
                        "or provide trusted source links."
                    )
                    run_status = "degraded"
                    failure_reason = "grounded_evidence_missing"
                    break

                final_response = llm_response.content
                if (
                    needs_grounded
                    and rss_attempted
                    and self._response_indicates_no_evidence(final_response)
                ):
                    logger.info(
                        "Grounded response indicates no evidence; forcing rss_miss "
                        f"fallback for session {session_id}"
                    )
                    rss_has_hits = False
                    selected_model = self._select_model_after_rss(False)
                    fallback_result, used_model = await self._execute_rss_miss_fallback(
                        user_message=user_message,
                        session=session,
                        call_chain=call_chain,
                    )
                    if fallback_result:
                        final_response = fallback_result
                        last_model_used = used_model
                        logger.info(
                            "No-evidence fallback completed with model "
                            f"`{used_model}` for session {session_id}"
                        )
                logger.debug(f"Final response: {final_response[:300]}...")
                break

            # If tool calls -> execute all in parallel
            if llm_response.tool_calls:
                # Log tool calls
                for tc in llm_response.tool_calls:
                    logger.debug(f"Tool call: {tc.name}({tc.arguments})")

                # Add assistant message with tool calls
                messages.append(llm_response.to_message())

                # Execute tools in parallel
                results = await self._execute_tools_parallel(
                    llm_response.tool_calls,
                    session,
                    confirm_callback,
                    workflow_identity=workflow_identity,
                )

                # Process results
                for tc, result, telemetry in results:
                    call_chain.append(telemetry)
                    if telemetry["status"] not in {"success", "cache_hit"}:
                        had_tool_error = True
                        if not failure_reason:
                            failure_reason = f"{tc.name}:{telemetry['status']}"
                    compressed = self.ctx.compress_tool_output(tc.name, result)
                    sanitized = self.prompt_guard.sanitize_tool_output(
                        tc.name, compressed
                    )
                    if tc.name in self.RSS_GROUNDING_TOOLS:
                        rss_attempted = True
                        has_hits = self._rss_result_has_hits(sanitized)
                        if has_hits:
                            rss_has_hits = True
                        elif rss_has_hits is None:
                            rss_has_hits = False
                    quota_note = self._extract_serper_quota_note(sanitized)
                    if quota_note:
                        serper_quota_note = quota_note
                    evidence_link = shared_evidence.collect_tool_output(tc.name, sanitized)
                    if evidence_link["evidence_ids"]:
                        telemetry["evidence_refs"] = evidence_link["evidence_ids"]
                        telemetry["evidence_count"] = len(evidence_link["evidence_ids"])
                    # Log tool result (truncated for readability)
                    result_preview = sanitized[:200] + "..." if len(sanitized) > 200 else sanitized
                    logger.debug(f"Tool result [{tc.name}]: {result_preview}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": sanitized,
                        }
                    )
                evidence_snapshot = shared_evidence.snapshot()
                self._attach_role_execution_briefs(
                    messages=messages,
                    call_chain=call_chain,
                    stage="post_tools",
                    session_id=session_id,
                    user_message=user_message,
                    needs_grounded=needs_grounded,
                    direct_web_used=direct_web_used,
                    failure_reason=failure_reason,
                    role_runtime=role_runtime,
                    shared_evidence=shared_evidence,
                    attached_roles=attached_role_execution,
                )
                self._attach_role_recovery_brief(
                    messages=messages,
                    call_chain=call_chain,
                    stage="post_tools",
                    session_id=session_id,
                    user_message=user_message,
                    needs_grounded=needs_grounded,
                    direct_web_used=direct_web_used,
                    failure_reason=failure_reason,
                    role_runtime=role_runtime,
                    shared_evidence=shared_evidence,
                    attached_recoveries=attached_role_recovery,
                    role_recovery_counts=role_recovery_counts,
                )
                if evidence_snapshot["count"] > last_evidence_context_count:
                    evidence_brief = build_shared_evidence_brief(evidence_snapshot)
                    if evidence_brief:
                        messages.append({"role": "user", "content": evidence_brief})
                        call_chain.append(
                            {
                                "type": "workflow_context",
                                "name": "shared_evidence_brief",
                                "status": "attached",
                                "count": evidence_snapshot["count"],
                                "evidence_refs": [
                                    item.get("evidence_id")
                                    for item in evidence_snapshot["items"]
                                    if item.get("evidence_id")
                                ],
                            }
                        )
                        last_evidence_context_count = evidence_snapshot["count"]

                if (
                    len(results) == 1
                    and results[0][0].name in self.DIRECT_RESPONSE_TOOLS
                    and not llm_response.content.strip()
                ):
                    final_response = results[0][1]
                    logger.info(
                        "Direct tool response used for `%s` in session %s",
                        results[0][0].name,
                        session_id,
                    )
                    break

                if needs_grounded and rss_attempted:
                    next_model = self._select_model_after_rss(rss_has_hits)
                    if next_model != selected_model:
                        logger.info(
                            "Grounding route switched model to "
                            f"`{next_model or 'default'}` "
                            f"(grounding_has_hits={rss_has_hits})"
                        )
                        selected_model = next_model

                    if rss_has_hits is False:
                        fallback_result, used_model = await self._execute_rss_miss_fallback(
                            user_message=user_message,
                            session=session,
                            call_chain=call_chain,
                        )
                        if fallback_result:
                            final_response = fallback_result
                            last_model_used = used_model
                            logger.info(
                                "RSS miss fallback completed with model "
                                f"`{used_model}` for session {session_id}"
                            )
                            break
                        logger.warning(
                            "RSS miss fallback failed to produce content; "
                            "continue normal loop."
                        )

                # If there's also text content, capture it (this is "thinking")
                if llm_response.content:
                    logger.debug(f"LLM thinking: {llm_response.content}")
                    final_response = llm_response.content

                continue

            # No content and no tool calls -> something went wrong
            run_status = "error"
            if not failure_reason:
                failure_reason = "empty_llm_response"
            final_response = "I encountered an issue processing your request."
            break

        # 4. Save to history
        if serper_quota_note and serper_quota_note not in final_response:
            final_response = (
                f"{final_response}\n\n{serper_quota_note}"
                if final_response
                else serper_quota_note
            )
        route_marker = self._build_grounding_route_marker(
            needs_grounded=needs_grounded,
            rss_attempted=rss_attempted,
            rss_has_hits=rss_has_hits,
            model_used=last_model_used or self._default_model_name(),
            direct_web_model=direct_web_used,
        )
        if route_marker:
            if final_response:
                final_response = f"{final_response}\n\n{route_marker}"
            else:
                final_response = route_marker

        if run_status == "success" and had_tool_error:
            run_status = "degraded"
        if run_status == "success" and final_response.startswith("Stopped: "):
            run_status = "stopped"
        if run_status == "success" and final_response.startswith("I could not gather reliable live"):
            run_status = "degraded"
            if not failure_reason:
                failure_reason = "grounded_evidence_missing"

        await self.memory.add_message(session_id, "user", user_message)
        await self.memory.add_message(session_id, "assistant", final_response)

        # 5. Background: extract and save important facts
        skip_memory = self._should_skip_memory(user_message)
        if not skip_memory:
            asyncio.create_task(
                self._extract_memories(user_message, final_response)
            )

        # 6. Audit log
        await self.audit.log(
            action_type="response",
            input_summary=user_message[:500],
            output_summary=final_response[:500],
            status=run_status,
            tokens=session.total_tokens,
            ms=session.elapsed_ms,
            session_id=session_id,
        )

        workflow_tags = self._build_workflow_tags(
            session_id=session_id,
            user_message=user_message,
            tool_names=[item["name"] for item in call_chain if item.get("type") == "tool"],
            needs_grounded=needs_grounded,
            direct_web_used=direct_web_used,
        )
        workflow_name = self._classify_workflow_name(
            session_id=session_id,
            user_message=user_message,
            tool_names=[item["name"] for item in call_chain if item.get("type") == "tool"],
            workflow_tags=workflow_tags,
            direct_web_used=direct_web_used,
        )
        evidence_snapshot = shared_evidence.snapshot()
        call_chain.extend(
            build_collaboration_events(
                workflow_name=workflow_name,
                user_message=user_message,
                tool_names=[item["name"] for item in call_chain if item.get("type") == "tool"],
                needs_grounded=needs_grounded,
                evidence_snapshot=evidence_snapshot,
                run_status=run_status,
                failure_reason=failure_reason,
                final_response=final_response,
            )
        )
        role_tasks = build_role_task_envelopes(
            workflow_name=workflow_name,
            tool_names=[item["name"] for item in call_chain if item.get("type") == "tool"],
            needs_grounded=needs_grounded,
            evidence_snapshot=evidence_snapshot,
            run_status=run_status,
        )
        runtime_ctx = get_tool_runtime_context()
        call_chain.extend(
            build_role_runtime_bridge_events(
                session_id=session_id,
                workflow_name=workflow_name,
                workflow_identity=workflow_identity,
                user_message=user_message,
                role_tasks=role_tasks,
                tool_names=[item["name"] for item in call_chain if item.get("type") == "tool"],
                needs_grounded=needs_grounded,
                evidence_snapshot=evidence_snapshot,
                failure_reason=failure_reason,
                parent_task_id=runtime_ctx.task_id,
            )
        )
        parent_session_id = await self._resolve_parent_session_id()
        if parent_session_id and not any(
            item.get("type") == "workflow_context"
            and item.get("name") == "parent_session_id"
            for item in call_chain
            if isinstance(item, dict)
        ):
            call_chain.append(
                {
                    "type": "workflow_context",
                    "name": "parent_session_id",
                    "status": "attached",
                    "value": parent_session_id,
                }
            )
        if not any(
            item.get("type") == "workflow_context"
            and item.get("name") == "workflow_identity"
            for item in call_chain
            if isinstance(item, dict)
        ):
            call_chain.append(
                {
                    "type": "workflow_context",
                    "name": "workflow_identity",
                    "status": "attached",
                    "value": workflow_identity,
                }
            )
        workflow_logger = getattr(self.audit, "log_workflow_run", None)
        if callable(workflow_logger):
            try:
                await workflow_logger(
                    session_id=session_id,
                    workflow_name=workflow_name,
                    workflow_identity=workflow_identity,
                    workflow_tags=workflow_tags,
                    user_summary=user_message[:500],
                    status=run_status,
                    failure_reason=failure_reason,
                    total_tokens=session.total_tokens,
                    execution_ms=session.elapsed_ms,
                    llm_calls=sum(1 for item in call_chain if item.get("type") == "llm"),
                    tool_calls=sum(1 for item in call_chain if item.get("type") == "tool"),
                    final_model=last_model_used or self._default_model_name(),
                    call_chain=call_chain,
                )
            except Exception as exc:
                logger.error("Workflow telemetry logging failed: %s", exc)

        return final_response

    async def _attach_persistent_role_resume(
        self,
        *,
        messages: list[dict[str, Any]],
        call_chain: list[dict[str, Any]],
        session_id: str,
        user_message: str,
        needs_grounded: bool,
        direct_web_used: bool,
        shared_evidence: SharedEvidenceStore,
        workflow_identity: str,
    ) -> int:
        """Attach one persisted role checkpoint brief before a new workflow run."""
        resume_loader = getattr(self.audit, "get_latest_role_resume_state", None)
        if not callable(resume_loader):
            return 0
        workflow_tags = self._build_workflow_tags(
            session_id=session_id,
            user_message=user_message,
            tool_names=[],
            needs_grounded=needs_grounded,
            direct_web_used=direct_web_used,
        )
        workflow_name = self._classify_workflow_name(
            session_id=session_id,
            user_message=user_message,
            tool_names=[],
            workflow_tags=workflow_tags,
            direct_web_used=direct_web_used,
        )
        try:
            resume_state = await resume_loader(
                session_id,
                workflow_name,
                workflow_identity=workflow_identity,
            )
        except Exception as exc:
            logger.error("Persistent role resume lookup failed: %s", exc)
            return 0
        if not resume_state:
            return 0
        snapshot = dict(resume_state.get("evidence_snapshot") or {})
        shared_evidence.load_snapshot(snapshot)
        resume_brief = build_persistent_resume_brief(resume_state)
        if resume_brief:
            messages.append({"role": "user", "content": resume_brief})
        call_chain.append(
            {
                "type": "workflow_role_resume",
                "role": str(resume_state.get("role") or ""),
                "stage": str(resume_state.get("stage") or ""),
                "resume_checkpoint_id": str(resume_state.get("resume_checkpoint_id") or ""),
                "source_workflow_run_id": int(resume_state.get("source_workflow_run_id") or 0),
                "source_workflow_name": str(resume_state.get("workflow_name") or workflow_name),
                "source_status": str(resume_state.get("workflow_status") or ""),
                "failure_reason": str(resume_state.get("failure_reason") or ""),
                "workflow_identity": str(
                    resume_state.get("workflow_identity") or workflow_identity
                ),
                "restored_evidence_count": int(snapshot.get("count") or 0),
                "status": "resumed",
                "evidence_refs": list(resume_state.get("evidence_refs") or []),
            }
        )
        return int(snapshot.get("count") or 0)

    @staticmethod
    def _new_workflow_identity() -> str:
        """Build one stable chain identity for the current workflow run."""
        return f"workflow_{uuid.uuid4().hex[:12]}"

    async def _get_runtime_task_payload(self) -> dict[str, Any]:
        """Return the current runtime task payload when inside one task-scoped run."""
        runtime = get_tool_runtime_context()
        if not runtime.task_id:
            return {}
        try:
            from nanoclaw.runtime.tasks import get_task_store

            task = await get_task_store().get_task(runtime.task_id)
        except Exception as exc:
            logger.error("Runtime task payload lookup failed: %s", exc)
            return {}
        if not task:
            return {}
        return dict(task.get("payload") or {})

    async def _resolve_workflow_identity(self) -> str:
        """Return the current workflow chain identity when available."""
        runtime = get_tool_runtime_context()
        if runtime.workflow_identity:
            return str(runtime.workflow_identity).strip()
        payload = await self._get_runtime_task_payload()
        return str(payload.get("workflow_identity") or "").strip()

    async def _resolve_parent_session_id(self) -> str:
        """Return the originating non-task session for the current task run, if any."""
        payload = await self._get_runtime_task_payload()
        return str(payload.get("parent_session_id") or "").strip()

    def _attach_role_execution_briefs(
        self,
        *,
        messages: list[dict[str, Any]],
        call_chain: list[dict[str, Any]],
        stage: str,
        session_id: str,
        user_message: str,
        needs_grounded: bool,
        direct_web_used: bool,
        failure_reason: str,
        role_runtime: RoleRuntimeState,
        shared_evidence: SharedEvidenceStore,
        attached_roles: set[tuple[str, str]],
    ) -> None:
        """Attach role-specific internal execution briefs once per stage."""
        tool_names = [
            item["name"] for item in call_chain if item.get("type") == "tool" and item.get("name")
        ]
        workflow_tags = self._build_workflow_tags(
            session_id=session_id,
            user_message=user_message,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            direct_web_used=direct_web_used,
        )
        workflow_name = self._classify_workflow_name(
            session_id=session_id,
            user_message=user_message,
            tool_names=tool_names,
            workflow_tags=workflow_tags,
            direct_web_used=direct_web_used,
        )
        briefs = build_role_execution_briefs(
            workflow_name=workflow_name,
            user_message=user_message,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            evidence_snapshot=shared_evidence.snapshot(),
            failure_reason=failure_reason,
            stage=stage,
        )
        for brief in briefs:
            key = (brief.stage, brief.role)
            if key in attached_roles:
                continue
            messages.append({"role": "user", "content": brief.content})
            checkpoint = role_runtime.record_checkpoint(
                checkpoint_id=brief.checkpoint_id,
                role=brief.role,
                stage=brief.stage,
                messages=messages,
                evidence_snapshot=shared_evidence.snapshot(),
            )
            call_chain.append(
                {
                    "type": "workflow_role_checkpoint",
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "role": checkpoint.role,
                    "stage": checkpoint.stage,
                    "message_count": checkpoint.message_count,
                    "evidence_count": int(checkpoint.evidence_snapshot.get("count") or 0),
                    "evidence_refs": [
                        item.get("evidence_id")
                        for item in list(checkpoint.evidence_snapshot.get("items") or [])
                        if isinstance(item, dict) and item.get("evidence_id")
                    ],
                    "evidence_items": list(checkpoint.evidence_snapshot.get("items") or []),
                }
            )
            call_chain.append(
                {
                    "type": "workflow_role_execution",
                    "role": brief.role,
                    "stage": brief.stage,
                    "checkpoint_id": brief.checkpoint_id,
                    "status": "attached",
                    "workflow_name": workflow_name,
                    "contract": brief.contract,
                    "evidence_refs": list(brief.evidence_refs),
                }
            )
            attached_roles.add(key)

    def _attach_role_recovery_brief(
        self,
        *,
        messages: list[dict[str, Any]],
        call_chain: list[dict[str, Any]],
        stage: str,
        session_id: str,
        user_message: str,
        needs_grounded: bool,
        direct_web_used: bool,
        failure_reason: str,
        role_runtime: RoleRuntimeState,
        shared_evidence: SharedEvidenceStore,
        attached_recoveries: set[tuple[str, str, int]],
        role_recovery_counts: dict[str, int],
    ) -> None:
        """Attach one role-level recovery brief when the current phase degrades."""
        tool_names = [
            item["name"] for item in call_chain if item.get("type") == "tool" and item.get("name")
        ]
        workflow_tags = self._build_workflow_tags(
            session_id=session_id,
            user_message=user_message,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            direct_web_used=direct_web_used,
        )
        workflow_name = self._classify_workflow_name(
            session_id=session_id,
            user_message=user_message,
            tool_names=tool_names,
            workflow_tags=workflow_tags,
            direct_web_used=direct_web_used,
        )
        action = build_role_recovery_action(
            workflow_name=workflow_name,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            evidence_snapshot=shared_evidence.snapshot(),
            failure_reason=failure_reason,
            stage=stage,
        )
        if action is None:
            return
        budget_limit = get_role_retry_budget(action.failed_role)
        attempt_number = int(role_recovery_counts.get(action.failed_role, 0)) + 1
        key = (action.stage, action.failed_role, attempt_number)
        if key in attached_recoveries:
            return
        if attempt_number > budget_limit:
            call_chain.append(
                {
                    "type": "workflow_role_recovery",
                    "failed_role": action.failed_role,
                    "recovery_role": action.recovery_role,
                    "stage": action.stage,
                    "reason": action.reason,
                    "resume_checkpoint_id": action.resume_checkpoint_id,
                    "recovery_task_key": action.recovery_task_key,
                    "attempt_number": budget_limit,
                    "budget_limit": budget_limit,
                    "remaining_budget": 0,
                    "status": "budget_exhausted",
                    "restored_messages": 0,
                    "restored_evidence_count": 0,
                    "evidence_refs": list(action.evidence_refs),
                }
            )
            attached_recoveries.add(key)
            return
        role_recovery_counts[action.failed_role] = attempt_number
        restore_result = role_runtime.restore_checkpoint(
            action.resume_checkpoint_id,
            messages=messages,
            shared_evidence=shared_evidence,
        )
        messages.append({"role": "user", "content": action.content})
        call_chain.append(
            {
                "type": "workflow_role_recovery",
                "failed_role": action.failed_role,
                "recovery_role": action.recovery_role,
                "stage": action.stage,
                "reason": action.reason,
                "resume_checkpoint_id": action.resume_checkpoint_id,
                "recovery_task_key": action.recovery_task_key,
                "attempt_number": attempt_number,
                "budget_limit": budget_limit,
                "remaining_budget": max(0, budget_limit - attempt_number),
                "status": "resumed" if restore_result else "checkpoint_missing",
                "restored_messages": (
                    restore_result.restored_messages if restore_result else 0
                ),
                "restored_evidence_count": (
                    restore_result.restored_evidence_count if restore_result else 0
                ),
                "evidence_refs": list(action.evidence_refs),
            }
        )
        attached_recoveries.add(key)

    async def _execute_tools_parallel(
        self,
        tool_calls: list[ToolCall],
        session: SessionTracker,
        confirm_callback: Optional[Callable],
        *,
        workflow_identity: str = "",
    ) -> list[tuple[ToolCall, str, dict[str, Any]]]:
        """Execute multiple tools in parallel."""

        async def _run_one_tool(tc: ToolCall) -> tuple[ToolCall, str, dict[str, Any]]:
            session.increment_tool_calls()

            # Check session cache
            cache_key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
            if tc.name in self.CACHEABLE:
                cached = self.cache.get(cache_key)
                if cached:
                    await self._log_task_tool_trace(
                        tool_name=tc.name,
                        arguments=tc.arguments,
                        output=cached,
                        status="cache_hit",
                        execution_ms=0,
                        cached=True,
                    )
                    return tc, cached, {
                        "type": "tool",
                        "name": tc.name,
                        "status": "cache_hit",
                        "execution_ms": 0,
                        "cached": True,
                    }

            result, status, elapsed_ms = await self._execute_tool_safely(
                tc,
                session,
                confirm_callback,
                workflow_identity=workflow_identity,
            )

            # Cache the result only if not an error
            if tc.name in self.CACHEABLE and not self._is_error_result(result):
                self.cache.set(cache_key, result)

            # Invalidate file_read cache on file_write
            if tc.name == "file_write":
                path = tc.arguments.get("path", "")
                self.cache.invalidate(
                    f"file_read:{json.dumps({'path': path}, sort_keys=True)}"
                )

            return tc, result, {
                "type": "tool",
                "name": tc.name,
                "status": status,
                "execution_ms": elapsed_ms,
                "cached": False,
            }

        raw_results = await asyncio.gather(
            *[_run_one_tool(tc) for tc in tool_calls],
            return_exceptions=True,
        )

        results: list[tuple[ToolCall, str, dict[str, Any]]] = []
        for i, item in enumerate(raw_results):
            if isinstance(item, Exception):
                tc = tool_calls[i]
                results.append(
                    (
                        tc,
                        f"ERROR: {item}",
                        {
                            "type": "tool",
                            "name": tc.name,
                            "status": "error",
                            "execution_ms": 0,
                            "cached": False,
                        },
                    )
                )
            else:
                results.append(item)  # type: ignore[arg-type]

        return results

    async def _execute_tool_safely(
        self,
        tool_call: ToolCall,
        session: SessionTracker,
        confirm_callback: Optional[Callable],
        *,
        workflow_identity: str = "",
    ) -> tuple[str, str, int]:
        """Execute a tool call with full security pipeline."""
        start_time = time.time()
        boundary_token = begin_boundary_decision_trace()
        secret_token = begin_secret_access_trace()

        try:
            # Track shell calls
            if tool_call.name == "shell_exec":
                session.increment_shell_calls()

            current_context = get_tool_runtime_context()
            token = set_tool_runtime_context(
                session_id=session.session_id,
                task_id=current_context.task_id,
                step_id=current_context.step_id,
                task_attempt=current_context.task_attempt,
                workflow_identity=workflow_identity or current_context.workflow_identity,
            )
            try:
                result = await asyncio.wait_for(
                    self.tools.execute(
                        tool_call.name,
                        tool_call.arguments,
                        confirm_callback=confirm_callback,
                    ),
                    timeout=30,
                )
            finally:
                reset_tool_runtime_context(token)

            elapsed_ms = int((time.time() - start_time) * 1000)
            await self.audit.log(
                action_type="tool_call",
                tool_name=tool_call.name,
                input_summary=str(tool_call.arguments)[:500],
                output_summary=str(result)[:500],
                status="success",
                ms=elapsed_ms,
                session_id=session.session_id,
            )
            await self._log_task_tool_trace(
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                output=str(result),
                status="success",
                execution_ms=elapsed_ms,
                cached=False,
            )
            return str(result), "success", elapsed_ms

        except asyncio.TimeoutError:
            elapsed_ms = int((time.time() - start_time) * 1000)
            await self.audit.log(
                action_type="tool_call",
                tool_name=tool_call.name,
                status="timeout",
                ms=elapsed_ms,
                session_id=session.session_id,
            )
            await self._log_task_tool_trace(
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                output=f"TIMEOUT: {tool_call.name} exceeded 30 second limit",
                status="timeout",
                execution_ms=elapsed_ms,
                cached=False,
            )
            return (
                f"TIMEOUT: {tool_call.name} exceeded 30 second limit",
                "timeout",
                elapsed_ms,
            )

        except Exception as e:
            from nanoclaw.security.sandbox import SecurityError

            elapsed_ms = int((time.time() - start_time) * 1000)
            if isinstance(e, SecurityError):
                await self.audit.log(
                    action_type="blocked",
                    tool_name=tool_call.name,
                    input_summary=str(tool_call.arguments)[:500],
                    status="blocked",
                    ms=elapsed_ms,
                    session_id=session.session_id,
                )
                await self._log_task_tool_trace(
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                    output=f"SECURITY: Action blocked - {e}",
                    status="blocked",
                    execution_ms=elapsed_ms,
                    cached=False,
                )
                return f"SECURITY: Action blocked - {e}", "blocked", elapsed_ms

            await self.audit.log(
                action_type="tool_call",
                tool_name=tool_call.name,
                status="error",
                output_summary=str(e)[:500],
                ms=elapsed_ms,
                session_id=session.session_id,
            )
            await self._log_task_tool_trace(
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                output=f"ERROR: {tool_call.name} failed - {e}",
                status="error",
                execution_ms=elapsed_ms,
                cached=False,
            )
            return f"ERROR: {tool_call.name} failed - {e}", "error", elapsed_ms
        finally:
            try:
                await self._log_secret_access_trace(
                    tool_name=tool_call.name,
                    session_id=session.session_id,
                )
            finally:
                try:
                    await self._log_boundary_decision_trace(
                        tool_name=tool_call.name,
                        session_id=session.session_id,
                    )
                finally:
                    reset_secret_access_trace(secret_token)
                    reset_boundary_decision_trace(boundary_token)

    async def _log_task_tool_trace(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        status: str,
        execution_ms: int,
        cached: bool,
    ) -> None:
        """Write one structured tool trace when the tool runs inside a task step."""
        runtime = get_tool_runtime_context()
        if not runtime.task_id:
            return
        trace_logger = getattr(self.audit, "log_tool_trace", None)
        if not callable(trace_logger):
            return
        try:
            await trace_logger(
                task_id=runtime.task_id,
                session_id=runtime.session_id,
                step_id=runtime.step_id,
                attempt_number=runtime.task_attempt,
                tool_name=tool_name,
                input_summary=str(arguments),
                output_summary=output,
                status=status,
                execution_ms=execution_ms,
                cached=cached,
            )
        except Exception as exc:
            logger.error("Tool trace logging failed: %s", exc)

    async def _log_boundary_decision_trace(
        self,
        *,
        tool_name: str,
        session_id: str,
    ) -> None:
        """Write boundary decisions collected during the active tool run."""
        decisions = get_boundary_decision_trace()
        if not decisions:
            return

        for item in decisions:
            operation = str(item.get("operation") or tool_name or "").strip() or tool_name
            boundary_kind = str(item.get("boundary_kind") or "").strip() or "boundary"
            action = str(item.get("action") or "").strip() or "check"
            target = str(item.get("target") or "").strip() or "-"
            decision = str(item.get("decision") or "").strip() or "unknown"
            reason = str(item.get("reason") or "").strip()
            policy_name = str(item.get("policy_name") or "").strip() or "shared_tool_boundary"
            policy_version = str(item.get("policy_version") or "").strip() or "v0"
            await self.audit.log(
                action_type="boundary_decision",
                tool_name=tool_name,
                input_summary=(
                    f"operation={operation} boundary={boundary_kind} "
                    f"action={action} target={target}"
                ),
                output_summary=(
                    f"policy={policy_name} version={policy_version} decision={decision}"
                    + (f" reason={reason}" if reason else "")
                ),
                status="blocked" if decision == "blocked" else "success",
                session_id=session_id,
            )

    async def _log_secret_access_trace(
        self,
        *,
        tool_name: str,
        session_id: str,
    ) -> None:
        """Write secret-capability access decisions from the active tool run."""
        accesses = get_secret_access_trace()
        if not accesses:
            return

        for item in accesses:
            capability = str(item.get("capability") or "").strip() or "unknown"
            decision = str(item.get("decision") or "").strip() or "unknown"
            source = str(item.get("source") or "").strip() or "none"
            reason = str(item.get("reason") or "").strip()
            policy_name = str(item.get("policy_name") or "").strip() or "tool_secret_broker"
            policy_version = str(item.get("policy_version") or "").strip() or "v0"
            await self.audit.log(
                action_type="secret_access",
                tool_name=tool_name,
                input_summary=f"capability={capability} source={source}",
                output_summary=(
                    f"policy={policy_name} version={policy_version} decision={decision}"
                    + (f" reason={reason}" if reason else "")
                ),
                status="blocked" if decision == "blocked" else "success",
                session_id=session_id,
            )

    def _is_error_result(self, result: str) -> bool:
        """Check if result is an error that should not be cached."""
        error_prefixes = (
            "Search failed:",
            "Search rate limited",
            "Search error:",
            "ERROR:",
            "TIMEOUT:",
            "SECURITY:",
            "Failed to fetch:",
            "Network error",
            "Unknown tool:",
            "Invalid arguments",
        )
        return result.startswith(error_prefixes)

    def _default_model_name(self) -> str:
        """Return the LLM's default model label for logs and route markers."""
        model = getattr(self.llm, "model", "")
        if isinstance(model, str) and model.strip():
            return model
        return "default_model"

    @staticmethod
    def _normalize_model_routing(model_routing: Optional[Any]) -> dict[str, Any]:
        """Normalize model routing config object/dict into a plain dict."""
        defaults: dict[str, Any] = {
            "enabled": False,
            "daily_model": "",
            "paper_model": "",
            "general_model": "",
            "qwen_enable_search": True,
            "qwen_search_options": {},
        }
        if model_routing is None:
            return defaults

        if isinstance(model_routing, dict):
            merged = dict(defaults)
            merged.update(model_routing)
            return merged

        # Pydantic v1/v2 object support
        if hasattr(model_routing, "dict"):
            values = model_routing.dict()  # type: ignore[union-attr]
            merged = dict(defaults)
            merged.update(values)
            return merged
        if hasattr(model_routing, "model_dump"):
            values = model_routing.model_dump()  # type: ignore[union-attr]
            merged = dict(defaults)
            merged.update(values)
            return merged

        merged = dict(defaults)
        merged["enabled"] = bool(getattr(model_routing, "enabled", False))
        merged["daily_model"] = str(
            getattr(model_routing, "daily_model", "")
        ).strip()
        merged["paper_model"] = str(
            getattr(model_routing, "paper_model", "")
        ).strip()
        merged["general_model"] = str(
            getattr(model_routing, "general_model", "")
        ).strip()
        merged["qwen_enable_search"] = bool(
            getattr(model_routing, "qwen_enable_search", True)
        )
        search_options = getattr(model_routing, "qwen_search_options", {})
        merged["qwen_search_options"] = (
            search_options if isinstance(search_options, dict) else {}
        )
        return merged

    @staticmethod
    def _has_tool_evidence(messages: list[dict]) -> bool:
        """Check if the current message stack already has tool results."""
        return any(msg.get("role") == "tool" for msg in messages)

    @staticmethod
    def _needs_grounded_search(user_message: str) -> bool:
        """
        Return True for requests that should use live evidence tools.

        These intents are time-sensitive or trend-driven and should not
        be answered from prior model knowledge alone.
        """
        text = user_message.lower()
        keywords = [
            "latest",
            "current",
            "today",
            "recent",
            "trend",
            "trending",
            "news",
            "hotspot",
            "paper",
            "arxiv",
            "daily",
            "daily digest",
            "简报",
            "日报",
            "趋势",
            "热点",
            "最新",
            "今日",
            "过去",
            "近7天",
            "论文",
            "新闻",
        ]
        return any(token in text for token in keywords)

    @classmethod
    def _is_daily_request(cls, user_message: str) -> bool:
        """Return True if the request is asking for a daily digest/brief."""
        text = user_message.lower()
        return any(token in text for token in cls.DAILY_HINTS)

    @classmethod
    def _is_paper_request(cls, user_message: str) -> bool:
        """Return True if the request is paper/research focused."""
        text = user_message.lower()
        return any(token in text for token in cls.PAPER_HINTS)

    def _select_model_for_request(self, user_message: str) -> Optional[str]:
        """Select model by intent based on model routing configuration."""
        routing = self.model_routing
        if not bool(routing.get("enabled", False)):
            return None

        if self._is_paper_request(user_message):
            model = str(routing.get("paper_model", "")).strip()
            return model or None

        if self._is_daily_request(user_message):
            model = str(routing.get("daily_model", "")).strip()
            return model or None

        model = str(routing.get("general_model", "")).strip()
        return model or None

    def _select_model_after_rss(self, rss_has_hits: Optional[bool]) -> Optional[str]:
        """
        Choose analysis model after RSS retrieval.

        Rule:
        - RSS hit -> analysis model (daily_model, default gpt-5.2)
        - RSS miss -> fallback model (general_model, default qwen3-max-2026-01-23)
        """
        routing = self.model_routing
        analysis_model = str(routing.get("daily_model", "")).strip() or "gpt-5.2"
        fallback_model = (
            str(routing.get("general_model", "")).strip()
            or "qwen3-max-2026-01-23"
        )
        if rss_has_hits:
            return analysis_model
        return fallback_model

    @classmethod
    def _rss_result_has_hits(cls, result: str) -> bool:
        """Heuristic: determine whether RSS grounding result has matched items."""
        text = result.lower().strip()
        if not text:
            return False
        if any(pattern in text for pattern in cls.RSS_MISS_PATTERNS):
            return False
        if text.startswith(("error:", "timeout:", "security:", "search failed:")):
            return False
        # Require at least one URL as concrete evidence.
        if "http://" in text or "https://" in text:
            return True
        return False

    @classmethod
    def _response_indicates_no_evidence(cls, response: str) -> bool:
        """Detect final-model refusal text that indicates RSS evidence was insufficient."""
        text = response.lower().strip()
        if not text:
            return False
        if any(pattern in text for pattern in cls.NO_EVIDENCE_RESPONSE_PATTERNS):
            return True
        if "rss信源" in response and ("没有返回" in response or "结果为空" in response):
            return True
        if "可核验" in response and ("没有" in response or "无" in response):
            return True
        return False

    @staticmethod
    def _build_grounding_route_marker(
        needs_grounded: bool,
        rss_attempted: bool,
        rss_has_hits: Optional[bool],
        model_used: str,
        direct_web_model: bool = False,
    ) -> str:
        """Build user-visible route marker for grounded requests."""
        if not needs_grounded:
            return ""

        if direct_web_model:
            model_label = model_used.strip() or "default_model"
            return f"Route: web_model->{model_label}"

        if not rss_attempted:
            route_state = "local_grounding_not_attempted"
        elif rss_has_hits is True:
            route_state = "local_grounding_hit"
        elif rss_has_hits is False:
            route_state = "local_grounding_miss"
        else:
            route_state = "local_grounding_unknown"

        model_label = model_used.strip() or "default_model"
        return f"Route: {route_state}->{model_label}"

    @staticmethod
    def _build_qwen_search_prompt(user_message: str) -> str:
        """Default fallback prompt for Qwen web-search execution."""
        today_dt = datetime.now()
        today = today_dt.strftime("%Y-%m-%d")
        week_start = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")
        return (
            f"当前日期：{today}。\n"
            "你必须先联网检索再回答，不允许仅凭参数记忆回答。\n"
            "禁止输出“截至2024年6月/知识截止日期”这类离线说明。\n"
            "如果用户请求有时间窗口，必须严格按发布时间过滤结果。\n"
            f"如果用户说“过去7天/近7天”，仅允许使用 {week_start} 到 {today} 的信息。\n"
            "请直接完成以下用户请求：\n"
            f"{user_message}\n\n"
            "输出要求：\n"
            "1) 严格围绕用户请求，给出最终结果；\n"
            "2) 每条要点附可访问 URL（最多 6 条）；\n"
            "3) 尽量标注时间（YYYY-MM-DD）；\n"
            "4) 若证据冲突，明确不确定性与来源差异；\n"
            "5) 回答末尾给出“检索来源数:N”。"
        )

    def _build_qwen_search_payload(self, model_name: str) -> dict[str, Any]:
        """Build optional provider payload fields for Qwen web-enabled calls."""
        if "qwen" not in model_name.lower():
            return {}

        enabled = bool(self.model_routing.get("qwen_enable_search", True))
        if not enabled:
            return {}

        payload: dict[str, Any] = {"enable_search": True}
        user_options = self.model_routing.get("qwen_search_options", {})
        if isinstance(user_options, dict):
            for key, value in user_options.items():
                # Guard critical keys from being overridden by config.
                if key in {"model", "messages", "tools", "tool_choice"}:
                    continue
                payload[key] = value
        return payload

    @classmethod
    def _looks_like_offline_answer(cls, text: str) -> bool:
        """Check if response still looks like offline knowledge instead of live search."""
        low = text.lower()
        return any(pattern in low for pattern in cls.OFFLINE_RESPONSE_PATTERNS)

    @staticmethod
    def _extract_serper_quota_note(text: str) -> str:
        """Extract the most recent Serper quota note from tool output."""
        matches = re.findall(r"Serper quota remaining:\s*\d+/\d+", text)
        return matches[-1] if matches else ""

    async def _execute_rss_miss_fallback(
        self,
        user_message: str,
        session: SessionTracker,
        call_chain: Optional[list[dict[str, Any]]] = None,
    ) -> tuple[Optional[str], str]:
        """
        Execute two-step fallback on RSS miss.

        Step 1: use gpt-5.2 (analysis model) to craft a search-execution prompt.
        Step 2: send the prompt to qwen3-max (general model) and return its result.
        """
        analysis_model = self._select_model_after_rss(True) or "gpt-5.2"
        fallback_model = self._select_model_after_rss(False) or "qwen3-max-2026-01-23"

        planner_messages = [
            {
                "role": "system",
                "content": (
                    "You generate one execution prompt for a web-enabled model. "
                    "Return only the prompt text."
                ),
            },
            {
                "role": "user",
                "content": (
                    "RSS retrieval returned no matched items for this request. "
                    "Write a concise Chinese prompt that tells a web-enabled model "
                    "to search the internet and directly answer the original user "
                    "request with URLs.\n\n"
                    f"Original user request:\n{user_message}"
                ),
            },
        ]

        qwen_prompt = self._build_qwen_search_prompt(user_message)
        try:
            planner_resp = await self.llm.chat(
                planner_messages,
                model=analysis_model,
            )
            session.add_tokens(planner_resp.usage.total_tokens)
            self._append_llm_event(
                call_chain,
                model=analysis_model,
                status="success",
                tokens=planner_resp.usage.total_tokens,
                has_content=bool(planner_resp.content),
                stage="fallback_planner",
            )
            planner_text = planner_resp.content.strip()
            if planner_text:
                qwen_prompt = planner_text
        except Exception as exc:
            self._append_llm_event(
                call_chain,
                model=analysis_model,
                status="error",
                error=str(exc)[:200],
                stage="fallback_planner",
            )
            logger.warning(
                "Prompt planner call failed, fallback to default qwen prompt: "
                f"{str(exc)[:200]}"
            )
        logger.info(
            "Planner prompt for qwen generated by gpt-5.2:\n%s",
            qwen_prompt[:2000],
        )

        search_payload = self._build_qwen_search_payload(fallback_model)
        if search_payload:
            logger.info(
                "Qwen web-search payload enabled for `%s`: %s",
                fallback_model,
                search_payload,
            )

        async def _call_qwen(prompt_text: str, payload: Optional[dict[str, Any]]) -> str:
            """Execute one Qwen call and return stripped text."""
            resp = await self.llm.chat(
                [{"role": "user", "content": prompt_text}],
                model=fallback_model,
                extra_payload=payload,
            )
            session.add_tokens(resp.usage.total_tokens)
            self._append_llm_event(
                call_chain,
                model=fallback_model,
                status="success",
                tokens=resp.usage.total_tokens,
                has_content=bool(resp.content),
                stage="fallback_search",
            )
            return resp.content.strip()

        content = ""
        try:
            content = await _call_qwen(qwen_prompt, search_payload or None)
        except Exception as exc:
            self._append_llm_event(
                call_chain,
                model=fallback_model,
                status="error",
                error=str(exc)[:200],
                stage="fallback_search",
            )
            logger.warning(
                "Qwen prompt-execution call failed with search payload, "
                "retrying without payload: "
                f"{type(exc).__name__}: {repr(exc)[:400]}"
            )
            if search_payload:
                try:
                    content = await _call_qwen(qwen_prompt, None)
                except Exception as retry_exc:
                    self._append_llm_event(
                        call_chain,
                        model=fallback_model,
                        status="error",
                        error=str(retry_exc)[:200],
                        stage="fallback_search",
                    )
                    logger.warning(
                        "Qwen prompt-execution call failed without payload, "
                        "retry with strict fallback prompt: "
                        f"{type(retry_exc).__name__}: {repr(retry_exc)[:400]}"
                    )

        if content and not self._looks_like_offline_answer(content):
            return content, fallback_model
        if content:
            logger.warning(
                "Qwen returned likely offline answer; retrying with strict prompt. "
                "preview=%s",
                content[:180],
            )

        # Last fallback: still force web-search style prompt to qwen.
        retry_prompt = self._build_qwen_search_prompt(user_message)
        try:
            direct_content = await _call_qwen(retry_prompt, search_payload or None)
            if not direct_content and search_payload:
                direct_content = await _call_qwen(retry_prompt, None)
            if direct_content:
                return direct_content, fallback_model
        except Exception as exc:
            self._append_llm_event(
                call_chain,
                model=fallback_model,
                status="error",
                error=str(exc)[:200],
                stage="fallback_search",
            )
            logger.warning(
                "Qwen strict fallback failed with search payload: "
                f"{type(exc).__name__}: {repr(exc)[:400]}"
            )
            if search_payload:
                try:
                    direct_content = await _call_qwen(retry_prompt, None)
                    if direct_content:
                        return direct_content, fallback_model
                except Exception as retry_exc:
                    self._append_llm_event(
                        call_chain,
                        model=fallback_model,
                        status="error",
                        error=str(retry_exc)[:200],
                        stage="fallback_search",
                    )
                    logger.error(
                        "Qwen direct fallback failed without payload: "
                        f"{type(retry_exc).__name__}: {repr(retry_exc)[:400]}"
                    )
            else:
                logger.error("Qwen direct fallback failed with no payload available.")

        return None, fallback_model

    async def _execute_direct_web_search(
        self,
        user_message: str,
        session: SessionTracker,
        call_chain: Optional[list[dict[str, Any]]] = None,
    ) -> tuple[Optional[str], str]:
        """Use the configured web-enabled model directly for broad live-search requests."""
        model_name = str(
            self.model_routing.get("general_model", "")
        ).strip() or "qwen3-max-2026-01-23"
        prompt = self._build_qwen_search_prompt(user_message)
        search_payload = self._build_qwen_search_payload(model_name)

        async def _call_direct(payload: Optional[dict[str, Any]]) -> str:
            """Execute one direct web-model call."""
            resp = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                model=model_name,
                extra_payload=payload,
            )
            session.add_tokens(resp.usage.total_tokens)
            self._append_llm_event(
                call_chain,
                model=model_name,
                status="success",
                tokens=resp.usage.total_tokens,
                has_content=bool(resp.content),
                stage="direct_web_search",
            )
            return resp.content.strip()

        try:
            content = await _call_direct(search_payload or None)
            if content and not self._looks_like_offline_answer(content):
                return content, model_name
        except Exception as exc:
            self._append_llm_event(
                call_chain,
                model=model_name,
                status="error",
                error=str(exc)[:200],
                stage="direct_web_search",
            )
            logger.warning(
                "Direct web-model search failed with payload: %s",
                f"{type(exc).__name__}: {repr(exc)[:300]}",
            )

        if search_payload:
            try:
                content = await _call_direct(None)
                if content and not self._looks_like_offline_answer(content):
                    return content, model_name
            except Exception as exc:
                self._append_llm_event(
                    call_chain,
                    model=model_name,
                    status="error",
                    error=str(exc)[:200],
                    stage="direct_web_search",
                )
                logger.warning(
                    "Direct web-model search failed without payload: %s",
                    f"{type(exc).__name__}: {repr(exc)[:300]}",
                )

        return None, model_name

    def _should_skip_memory(self, user_message: str) -> bool:
        """Check if we should skip memory extraction for this message."""
        trivial_messages = {
            "thanks",
            "thank you",
            "ok",
            "okay",
            "got it",
            "cool",
            "yes",
            "no",
            "sure",
            "hi",
            "hello",
            "hey",
            "bye",
            "nice",
            "great",
            "perfect",
            "done",
            "next",
            "continue",
            "go ahead",
        }
        return (
            len(user_message) < 20
            or user_message.lower().strip() in trivial_messages
        )

    def _user_wants_plan(self, message: str) -> bool:
        """Check if user explicitly requested a plan."""
        explicit = ["make a plan", "plan first", "step by step", "create a plan"]
        return any(p in message.lower() for p in explicit)

    @staticmethod
    def _append_llm_event(
        call_chain: Optional[list[dict[str, Any]]],
        model: str,
        status: str,
        tokens: int = 0,
        tool_calls: int = 0,
        has_content: bool = False,
        stage: str = "main",
        error: str = "",
    ) -> None:
        """Append one LLM call event to the workflow call chain."""
        if call_chain is None:
            return
        event = {
            "type": "llm",
            "model": model,
            "status": status,
            "tokens": tokens,
            "tool_calls": tool_calls,
            "has_content": has_content,
            "stage": stage,
        }
        if error:
            event["error"] = error[:200]
        call_chain.append(event)

    def _select_grounding_strategy(self, user_message: str, session_id: str) -> str:
        """Choose between local grounding workflows and a direct web-enabled model route."""
        if not self._needs_grounded_search(user_message):
            return "tool"
        if session_id.startswith(("heartbeat:", "cron:")):
            return "tool"
        if user_message.lstrip().startswith("/paper"):
            return "tool"
        if self._matches_structured_grounding_workflow(user_message):
            return "tool"
        if self._needs_local_action_after_search(user_message):
            return "tool"
        if self._prefers_local_grounding_provider():
            return "tool"
        if self._web_enabled_model_available():
            return "web_model"
        return "tool"

    def _prefers_local_grounding_provider(self) -> bool:
        """Return True when configured search provider should run before web-model search."""
        return self.web_search_provider in {"serper", "brave"}

    @classmethod
    def _matches_structured_grounding_workflow(cls, user_message: str) -> bool:
        """Return True when the request clearly matches a built-in structured workflow."""
        return matches_structured_grounding_workflow(user_message)

    @classmethod
    def _needs_local_action_after_search(cls, user_message: str) -> bool:
        """Return True when the request mixes search with local workspace or writing actions."""
        text = user_message.lower()
        return any(token in text for token in cls.LOCAL_ACTION_HINTS)

    def _web_enabled_model_available(self) -> bool:
        """Return True when the configured general model can run web-enabled search."""
        model_name = str(self.model_routing.get("general_model", "")).strip()
        if not model_name:
            return False
        if "qwen" not in model_name.lower():
            return False
        return bool(self.model_routing.get("qwen_enable_search", True))

    def _build_workflow_tags(
        self,
        session_id: str,
        user_message: str,
        tool_names: list[str],
        needs_grounded: bool,
        direct_web_used: bool,
    ) -> list[str]:
        """Build a compact tag list describing which workflow path was used."""
        return build_workflow_tags(
            session_id=session_id,
            user_message=user_message,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            direct_web_used=direct_web_used,
            defaults=self.workflow_defaults,
        )

    def _classify_workflow_name(
        self,
        session_id: str,
        user_message: str,
        tool_names: list[str],
        workflow_tags: list[str],
        direct_web_used: bool,
    ) -> str:
        """Choose one primary workflow label for telemetry."""
        return classify_primary_workflow(
            session_id=session_id,
            user_message=user_message,
            tool_names=tool_names,
            workflow_tags=workflow_tags,
            direct_web_used=direct_web_used,
            defaults=self.workflow_defaults,
        )

    async def _extract_memories(
        self, user_message: str, response: str
    ) -> None:
        """Background task: extract important facts from conversation."""
        # Skip short messages
        if len(user_message) < 20:
            return

        triggers = [
            "my name",
            "i work",
            "i live",
            "i prefer",
            "i like",
            "i am",
            "my job",
            "i'm",
            "remember that",
            "don't forget",
            "i need",
            "my project",
            "my company",
            "my team",
        ]

        should_extract = any(t in user_message.lower() for t in triggers)
        if not should_extract:
            return

        try:
            extract_prompt = [
                {
                    "role": "system",
                    "content": (
                        "Extract factual information about the user from this "
                        "conversation. Return ONLY a JSON array of strings, "
                        "each being one fact. If no personal facts, return []. "
                        "Be concise. Max 3 facts."
                    ),
                },
                {
                    "role": "user",
                    "content": f"User: {user_message}\nAssistant: {response}",
                },
            ]
            result = await self.llm.chat(extract_prompt)

            # Parse JSON array of facts
            facts = json.loads(result.content)
            for fact in facts[:3]:
                await self.memory.save_memory(fact, category="auto")
        except Exception:
            pass  # Memory extraction is best-effort


# Global agent instance
_agent: Optional[Agent] = None


def get_agent() -> Agent:
    """Get the global Agent instance."""
    global _agent
    if _agent is None:
        from nanoclaw.core.config import get_config
        from nanoclaw.core.llm import get_llm_client
        from nanoclaw.memory.store import get_memory_store
        from nanoclaw.security.audit import get_audit_log
        from nanoclaw.security.budget import get_session_budget
        from nanoclaw.security.prompt_guard import get_prompt_guard
        from nanoclaw.tools.registry import get_tool_registry

        # Import tools to register them
        import nanoclaw.tools.files  # noqa: F401
        import nanoclaw.tools.capabilities  # noqa: F401
        import nanoclaw.tools.memory_tools  # noqa: F401
        import nanoclaw.tools.shell  # noqa: F401
        import nanoclaw.tools.spawn  # noqa: F401
        import nanoclaw.tools.web  # noqa: F401
        import nanoclaw.tools.web_workflows  # noqa: F401

        config = get_config()
        tools = get_tool_registry()

        # Load skills from built-in and user directories
        from pathlib import Path

        builtin_skills = Path(__file__).parent.parent / "skills"
        user_skills = Path.home() / ".nanoclaw" / "skills"

        builtin_names_before = set(tools.get_tool_names())
        tools.load_skills(str(builtin_skills))
        builtin_names_after = set(tools.get_tool_names())
        builtin_skill_names = sorted(builtin_names_after - builtin_names_before)
        if builtin_skill_names:
            tools.protect_tool_names(builtin_skill_names)
        tools.load_skills(str(user_skills))
        routing_cfg = getattr(config.agent, "model_routing", None)
        workflow_defaults = getattr(config.agent, "workflow_defaults", None)

        grounding_provider = config.tools.web_search.provider
        if grounding_provider == "auto":
            from nanoclaw.security.secrets import has_tool_secret

            if has_tool_secret(
                "web_search.serper_api_key",
                tool_name="web_search",
                web_cfg=config.tools.web_search,
            ):
                grounding_provider = "serper"
            elif has_tool_secret(
                "web_search.brave_api_key",
                tool_name="web_search",
                web_cfg=config.tools.web_search,
            ):
                grounding_provider = "brave"

        _agent = Agent(
            llm=get_llm_client(),
            memory=get_memory_store(),
            tools=tools,
            audit=get_audit_log(),
            budget=get_session_budget(),
            prompt_guard=get_prompt_guard(),
            context_builder=ContextBuilder(config.agent.system_prompt),
            max_iterations=config.agent.max_iterations,
            model_routing=routing_cfg,
            web_search_provider=grounding_provider,
            workflow_defaults=workflow_defaults,
        )
    return _agent


def set_agent(agent: Agent) -> None:
    """Set the global Agent instance."""
    global _agent
    _agent = agent
