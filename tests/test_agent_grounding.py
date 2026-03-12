"""Tests for grounding heuristics in agent loop."""

from __future__ import annotations

import asyncio

from nanoclaw.core.agent import Agent
from nanoclaw.core.context import ContextBuilder
from nanoclaw.core.llm import LLMResponse, TokenUsage, ToolCall
from nanoclaw.security.budget import SessionBudget, SessionTracker
from nanoclaw.security.prompt_guard import PromptGuard


def test_needs_grounded_search_for_trend_query() -> None:
    """Trend/news wording should trigger grounding requirement."""
    assert Agent._needs_grounded_search("做一份AI最新趋势简报") is True
    assert Agent._needs_grounded_search("show latest arxiv papers") is True
    assert Agent._needs_grounded_search("say hello") is False


def test_has_tool_evidence_detects_tool_messages() -> None:
    """Tool role messages should be treated as evidence present."""
    messages = [
        {"role": "system", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "tool", "content": "z"},
    ]
    assert Agent._has_tool_evidence(messages) is True
    assert Agent._has_tool_evidence([{"role": "user", "content": "x"}]) is False


def test_select_model_for_request_by_intent() -> None:
    """Intent routing should pick daily/paper/general models."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {
        "enabled": True,
        "daily_model": "gpt-5.2",
        "paper_model": "gpt-5.2",
        "general_model": "qwen3-max-2026-01-23",
    }

    assert agent._select_model_for_request("给我一份AI日报") == "gpt-5.2"
    assert agent._select_model_for_request("show latest arxiv papers") == "gpt-5.2"
    assert agent._select_model_for_request("帮我改写这段文案") == "qwen3-max-2026-01-23"


def test_select_model_for_request_when_routing_disabled() -> None:
    """Disabled routing should not override model."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {"enabled": False}
    assert agent._select_model_for_request("给我一份AI日报") is None


def test_select_model_after_rss_hit_and_miss() -> None:
    """After RSS, hit should use analysis model, miss should use fallback model."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {
        "enabled": True,
        "daily_model": "gpt-5.2",
        "paper_model": "gpt-5.2",
        "general_model": "qwen3-max-2026-01-23",
    }
    assert agent._select_model_after_rss(True) == "gpt-5.2"
    assert agent._select_model_after_rss(False) == "qwen3-max-2026-01-23"


def test_select_grounding_strategy_uses_web_model_for_broad_live_query() -> None:
    """Broad live-search requests should be allowed to bypass local workflows."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {
        "enabled": True,
        "daily_model": "gpt-5.2",
        "paper_model": "gpt-5.2",
        "general_model": "qwen3-max-2026-01-23",
        "qwen_enable_search": True,
    }
    agent.web_search_provider = "auto"
    strategy = agent._select_grounding_strategy(
        "What is the latest status of OpenAI funding talks today?",
        session_id="cli:user",
    )
    assert strategy == "web_model"


def test_select_grounding_strategy_keeps_structured_paper_flow_on_tools() -> None:
    """Paper-style grounded requests should stay on local workflow tools."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {
        "enabled": True,
        "daily_model": "gpt-5.2",
        "paper_model": "gpt-5.2",
        "general_model": "qwen3-max-2026-01-23",
        "qwen_enable_search": True,
    }
    agent.web_search_provider = "auto"
    strategy = agent._select_grounding_strategy(
        "show latest arxiv papers about video generation",
        session_id="cli:user",
    )
    assert strategy == "tool"


def test_select_grounding_strategy_prefers_serper_provider_for_broad_live_query() -> None:
    """Explicit Serper config should keep broad live queries on local search tools first."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {
        "enabled": True,
        "daily_model": "gpt-5.2",
        "paper_model": "gpt-5.2",
        "general_model": "qwen3-max-2026-01-23",
        "qwen_enable_search": True,
    }
    agent.web_search_provider = "serper"
    strategy = agent._select_grounding_strategy(
        "帮我查查伊朗最近七天的新闻，顺便总结分析下这些对接下来的中美关系有啥影响",
        session_id="feishu:user",
    )
    assert strategy == "tool"


def test_rss_result_has_hits_heuristic() -> None:
    """RSS hit detector should separate no-result text from evidence text."""
    assert Agent._rss_result_has_hits("No RSS results found for this query.") is False
    assert Agent._rss_result_has_hits("No hotspot items found.") is False
    assert Agent._rss_result_has_hits("Paper search for `x` found no provider results.") is False
    assert (
        Agent._rss_result_has_hits(
            "Paper search unavailable: no valid providers configured."
        )
        is False
    )
    assert Agent._rss_result_has_hits("**Title**\nhttps://example.com\nSource: x") is True
    assert Agent._rss_result_has_hits("Fetch status: ok=10 failed=2") is False


def test_build_grounding_route_marker() -> None:
    """Route marker should expose local grounding state and model for grounded requests."""
    marker_hit = Agent._build_grounding_route_marker(
        needs_grounded=True,
        rss_attempted=True,
        rss_has_hits=True,
        model_used="gpt-5.2",
    )
    marker_miss = Agent._build_grounding_route_marker(
        needs_grounded=True,
        rss_attempted=True,
        rss_has_hits=False,
        model_used="qwen3-max-2026-01-23",
    )
    marker_skip = Agent._build_grounding_route_marker(
        needs_grounded=False,
        rss_attempted=False,
        rss_has_hits=None,
        model_used="gpt-5.2",
    )
    assert marker_hit == "Route: local_grounding_hit->gpt-5.2"
    assert marker_miss == "Route: local_grounding_miss->qwen3-max-2026-01-23"
    assert marker_skip == ""


def test_build_grounding_route_marker_for_direct_web_model() -> None:
    """Direct web-enabled model routing should expose its route marker."""
    marker = Agent._build_grounding_route_marker(
        needs_grounded=True,
        rss_attempted=False,
        rss_has_hits=None,
        model_used="qwen3-max-2026-01-23",
        direct_web_model=True,
    )
    assert marker == "Route: web_model->qwen3-max-2026-01-23"


def test_build_qwen_search_prompt_contains_request() -> None:
    """Generated qwen search prompt should include original request."""
    req = "做一份伊朗过去7天局势简报，最多6条"
    prompt = Agent._build_qwen_search_prompt(req)
    assert req in prompt
    assert "联网检索" in prompt


def test_build_qwen_search_payload_defaults_enabled() -> None:
    """Qwen fallback should enable provider search flag by default."""
    agent = Agent.__new__(Agent)
    agent.model_routing = {}
    payload = agent._build_qwen_search_payload("qwen3-max-2026-01-23")
    assert payload == {"enable_search": True}


def test_offline_answer_detector() -> None:
    """Stale cutoff wording should be recognized as offline response."""
    assert Agent._looks_like_offline_answer("以下是截至2024年6月的信息") is True
    assert Agent._looks_like_offline_answer("这是2026-03-04的新闻摘要") is False


def test_response_indicates_no_evidence_detector() -> None:
    """No-evidence refusal text should trigger rss_miss fallback path."""
    assert Agent._response_indicates_no_evidence(
        "在我可用的实时检索/RSS信源里，没有返回任何可核验的新闻条目。"
    ) is True
    assert Agent._response_indicates_no_evidence(
        "我已整理过去7天的6条新闻并附上链接。"
    ) is False


def test_execute_rss_miss_fallback_chain() -> None:
    """RSS miss fallback should run gpt prompt planner then qwen executor."""

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = []
            self.model = "default-model"

        async def chat(  # type: ignore[no-untyped-def]
            self,
            messages,
            tools=None,
            model=None,
            extra_payload=None,
        ):
            self.calls.append((model, messages, extra_payload))
            if model == "gpt-5.2":
                return LLMResponse(
                    content="请联网检索伊朗过去7天局势，输出6条并附URL。",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=8),
                )
            if model == "qwen3-max-2026-01-23":
                return LLMResponse(
                    content="这是qwen直接给出的检索结果。",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=12, completion_tokens=20),
                )
            return LLMResponse(content="", tool_calls=[], usage=TokenUsage())

    async def _run() -> None:
        agent = Agent.__new__(Agent)
        agent.llm = FakeLLM()
        agent.model_routing = {
            "enabled": True,
            "daily_model": "gpt-5.2",
            "paper_model": "gpt-5.2",
            "general_model": "qwen3-max-2026-01-23",
        }
        session = SessionTracker(session_id="s1")

        result, model = await agent._execute_rss_miss_fallback(
            user_message="做一份伊朗过去7天局势简报，最多6条",
            session=session,
        )
        assert result == "这是qwen直接给出的检索结果。"
        assert model == "qwen3-max-2026-01-23"
        assert agent.llm.calls[0][0] == "gpt-5.2"
        assert agent.llm.calls[1][0] == "qwen3-max-2026-01-23"
        assert agent.llm.calls[1][2] == {"enable_search": True}

    asyncio.run(_run())


def test_run_direct_web_model_route() -> None:
    """Broad grounded queries can complete through the web-enabled model directly."""

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = []
            self.model = "default-model"

        async def chat(  # type: ignore[no-untyped-def]
            self,
            messages,
            tools=None,
            model=None,
            extra_payload=None,
        ):
            self.calls.append((model, messages, tools, extra_payload))
            return LLMResponse(
                content="OpenAI funding update with URLs.",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=9, completion_tokens=18),
            )

    class FakeTools:
        def __init__(self) -> None:
            self.executed = []

        async def execute(  # type: ignore[no-untyped-def]
            self,
            name,
            arguments,
            confirm_callback=None,
        ):
            self.executed.append((name, arguments))
            return "unused"

        def get_schemas(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "search web",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ]

    class FakeMemory:
        def __init__(self) -> None:
            self.saved = []

        async def get_history(self, session_id, limit=15):  # type: ignore[no-untyped-def]
            return []

        async def search_memories(self, query, limit=5):  # type: ignore[no-untyped-def]
            return []

        async def add_message(  # type: ignore[no-untyped-def]
            self,
            session_id,
            role,
            content,
            tool_name=None,
        ):
            self.saved.append((role, content))

    class FakeAudit:
        def __init__(self) -> None:
            self.workflow_runs = []

        async def log(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

        async def log_workflow_run(self, **kwargs):  # type: ignore[no-untyped-def]
            self.workflow_runs.append(kwargs)

    async def _run() -> None:
        fake_llm = FakeLLM()
        fake_tools = FakeTools()
        fake_audit = FakeAudit()
        agent = Agent(
            llm=fake_llm,
            memory=FakeMemory(),
            tools=fake_tools,
            audit=fake_audit,
            budget=SessionBudget(max_iterations=5),
            prompt_guard=PromptGuard(),
            context_builder=ContextBuilder(),
            max_iterations=5,
            model_routing={
                "enabled": True,
                "daily_model": "gpt-5.2",
                "paper_model": "gpt-5.2",
                "general_model": "qwen3-max-2026-01-23",
                "qwen_enable_search": True,
            },
        )
        out = await agent.run(
            "What is the latest status of OpenAI funding talks today?",
            session_id="cli:user",
        )
        assert "OpenAI funding update with URLs." in out
        assert "Route: web_model->qwen3-max-2026-01-23" in out
        assert fake_tools.executed == []
        assert len(fake_llm.calls) == 1
        assert fake_llm.calls[0][0] == "qwen3-max-2026-01-23"
        assert fake_llm.calls[0][3] == {"enable_search": True}
        assert fake_audit.workflow_runs[0]["workflow_name"] == "web_model_grounding"

    asyncio.run(_run())


def test_run_auto_refetch_on_paper_miss() -> None:
    """Paper-search miss should trigger automatic fallback re-search path."""

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = []
            self.model = "default-model"

        async def chat(  # type: ignore[no-untyped-def]
            self,
            messages,
            tools=None,
            model=None,
            extra_payload=None,
        ):
            self.calls.append((model, messages, tools, extra_payload))
            if tools:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call_p1",
                            name="paper_search",
                            arguments={"topic": "video generation acceleration"},
                        )
                    ],
                    usage=TokenUsage(prompt_tokens=8, completion_tokens=6),
                )
            if model == "gpt-5.2":
                return LLMResponse(
                    content="请联网检索近7天视频生成模型加速论文并附URL。",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=9, completion_tokens=8),
                )
            if model == "qwen3-max-2026-01-23":
                return LLMResponse(
                    content="补检索结果：1) 标题A https://example.com/p1",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=16),
                )
            return LLMResponse(content="", tool_calls=[], usage=TokenUsage())

    class FakeTools:
        async def execute(  # type: ignore[no-untyped-def]
            self,
            name,
            arguments,
            confirm_callback=None,
        ):
            if name == "paper_search":
                return (
                    "Paper search for `video generation acceleration` found no provider results.\n"
                    'Query: all:"video"'
                )
            return "Unknown tool"

        def get_schemas(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "paper_search",
                        "description": "search papers",
                        "parameters": {
                            "type": "object",
                            "properties": {"topic": {"type": "string"}},
                            "required": ["topic"],
                        },
                    },
                }
            ]

    class FakeMemory:
        async def get_history(self, session_id, limit=15):  # type: ignore[no-untyped-def]
            return []

        async def search_memories(self, query, limit=5):  # type: ignore[no-untyped-def]
            return []

        async def add_message(  # type: ignore[no-untyped-def]
            self,
            session_id,
            role,
            content,
            tool_name=None,
        ):
            return None

    class FakeAudit:
        async def log(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

    async def _run() -> None:
        fake_llm = FakeLLM()
        agent = Agent(
            llm=fake_llm,
            memory=FakeMemory(),
            tools=FakeTools(),
            audit=FakeAudit(),
            budget=SessionBudget(max_iterations=5),
            prompt_guard=PromptGuard(),
            context_builder=ContextBuilder(),
            max_iterations=5,
            model_routing={
                "enabled": True,
                "daily_model": "gpt-5.2",
                "paper_model": "gpt-5.2",
                "general_model": "qwen3-max-2026-01-23",
                "qwen_enable_search": True,
            },
        )
        out = await agent.run(
            "给我做一份视频生成模型加速论文简报，最多6条",
            session_id="s-paper-miss",
        )
        assert "补检索结果" in out
        assert "Route: local_grounding_miss->qwen3-max-2026-01-23" in out
        qwen_calls = [item for item in fake_llm.calls if item[0] == "qwen3-max-2026-01-23"]
        assert qwen_calls
        assert qwen_calls[0][3] == {"enable_search": True}

    asyncio.run(_run())


def test_wechat_assist_direct_tool_response() -> None:
    """wechat_article_assist should return directly without extra LLM round."""

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.model = "gpt-5.2"

        async def chat(  # type: ignore[no-untyped-def]
            self,
            messages,
            tools=None,
            model=None,
            extra_payload=None,
        ):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="wechat_article_assist",
                            arguments={"topic": "视频生成模型加速周报", "stage": "all"},
                        )
                    ],
                    usage=TokenUsage(prompt_tokens=8, completion_tokens=6),
                )
            return LLMResponse(
                content="should_not_happen",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )

    class FakeTools:
        async def execute(  # type: ignore[no-untyped-def]
            self,
            name,
            arguments,
            confirm_callback=None,
        ):
            return "公众号写作辅助包：`视频生成模型加速周报`"

        def get_schemas(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "wechat_article_assist",
                        "description": "helper",
                        "parameters": {
                            "type": "object",
                            "properties": {"topic": {"type": "string"}},
                            "required": ["topic"],
                        },
                    },
                }
            ]

    class FakeMemory:
        async def get_history(self, session_id, limit=15):  # type: ignore[no-untyped-def]
            return []

        async def search_memories(self, query, limit=5):  # type: ignore[no-untyped-def]
            return []

        async def add_message(  # type: ignore[no-untyped-def]
            self,
            session_id,
            role,
            content,
            tool_name=None,
        ):
            return None

    class FakeAudit:
        async def log(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

    async def _run() -> None:
        agent = Agent(
            llm=FakeLLM(),
            memory=FakeMemory(),
            tools=FakeTools(),
            audit=FakeAudit(),
            budget=SessionBudget(max_iterations=4),
            prompt_guard=PromptGuard(),
            context_builder=ContextBuilder(),
            max_iterations=4,
        )
        out = await agent.run(
            "按公众号风格给我做一份《视频生成模型加速周报》的选题+大纲+初稿+核查清单",
            session_id="s-wechat",
        )
        assert out.startswith("公众号写作辅助包")
        assert agent.llm.calls == 1

    asyncio.run(_run())
