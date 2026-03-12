"""Multi-provider LLM client with connection pooling. 调用模型API，统一不同模型厂商接口，对上层暴露一致的非流式chat()。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    """LLM API error."""

    pass


@dataclass
class TokenUsage:
    """Token usage information."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Unified LLM response format."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)

    def to_message(self) -> dict[str, Any]:
        """Convert response to message format for context."""
        msg: dict[str, Any] = {"role": "assistant"}

        if self.content:
            msg["content"] = self.content

        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
            if "content" not in msg:
                msg["content"] = ""

        return msg


class ConnectionPool:
    """Shared HTTP session for the entire application. 全局单aiohttp.ClientSession，减少握手开销。"""

    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        """Get or create the shared session."""
        if cls._session is None or cls._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,
                limit_per_host=5,
                ttl_dns_cache=300,
                keepalive_timeout=30,
            )
            cls._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return cls._session

    @classmethod
    async def close(cls) -> None:
        """Close the shared session."""
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None


class LLMClient:
    """
    Multi-provider LLM client.

    Supports OpenRouter, Anthropic, and OpenAI APIs.
    Uses shared connection pool for efficiency.
    """

    BASE_URLS = {
        "openrouter": "https://openrouter.ai/api/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "openai": "https://api.openai.com/v1",
    }

    def __init__(
        self,
        provider: str,
        api_key: str,
        default_model: str,
        base_url: Optional[str] = None,
    ):
        """
        Initialize LLM client.

        Args:
            provider: Provider name (openrouter, anthropic, openai)
            api_key: API key
            default_model: Default model to use
            base_url: Custom base URL (for proxies/local models)
        """
        self.provider = provider
        self.api_key = api_key
        self.model = default_model
        self.base_url = base_url or self.BASE_URLS.get(provider, "")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        extra_payload: Optional[dict[str, Any]] = None,
    ) -> LLMResponse:
        """
        按 provider 自动选择 endpoint 与 payload 格式，支持 tools 动态注入，负责组装请求、发送与解析响应，屏蔽各家模型 API 差异。内置重试提升稳定性，并将返回结果统一封装为 `LLMResponse`，上层只需关注输入输出即可。
        Send chat completion request.

        Args:
            messages: List of messages in OpenAI format
            tools: Optional list of tool schemas
            model: Optional model override
            extra_payload: Optional provider-specific payload extensions

        Returns:
            LLMResponse with content and/or tool calls
        """
        model = model or self.model

        headers = self._build_headers()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        # OpenAI GPT-5+ uses max_completion_tokens, others use max_tokens
        if self.provider == "openai" and model.startswith("gpt-5"):
            payload["max_completion_tokens"] = 4096
        else:
            payload["max_tokens"] = 4096

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if extra_payload:
            payload.update(extra_payload)

        session = await ConnectionPool.get_session()

        if self.provider == "anthropic":
            endpoint = f"{self.base_url}/messages"
            payload = self._adapt_for_anthropic(payload)
        else:
            endpoint = f"{self.base_url}/chat/completions"

        # Retry with exponential backoff for transient errors
        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            try:
                async with session.post(
                    endpoint, json=payload, headers=headers
                ) as resp:
                    if resp.status == 429 or resp.status == 529:
                        # Rate limit or overloaded - retry with backoff
                        if attempt < max_retries - 1:
                            wait_time = 2 ** attempt  # 1s, 2s, 4s
                            logger.warning(
                                f"LLM rate limited (HTTP {resp.status}), "
                                f"retry {attempt + 1}/{max_retries} in {wait_time}s"
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        error = await resp.text()
                        raise LLMError(f"LLM rate limited after {max_retries} retries: {error}")

                    if resp.status != 200:
                        error = await resp.text()
                        raise LLMError(f"LLM API error {resp.status}: {error}")

                    data = await resp.json()
                    return self._parse_response(data)

            except asyncio.TimeoutError as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(
                        "LLM timeout after 30s, retry %s/%s",
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise LLMError(
                    "LLM request timed out after 30s. "
                    "Try reducing context size or splitting the task."
                )
            except aiohttp.ClientError as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(f"LLM network error, retry {attempt + 1}: {e}")
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise LLMError(f"Network error after {max_retries} retries: {e}")

        raise LLMError(f"LLM call failed: {last_error}")

    def _build_headers(self) -> dict[str, str]:
        """Build request headers based on provider."""
        if self.provider == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "Accept-Encoding": "gzip, deflate",
            }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/nanoclaw/nanoclaw"
            headers["X-Title"] = "nanoClaw"
        return headers

    def _adapt_for_anthropic(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Convert OpenAI format to Anthropic Messages API format."""
        messages = payload.get("messages", [])

        system_text = ""
        converted_messages = []

        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            elif msg["role"] == "tool":
                converted_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": msg["content"],
                            }
                        ],
                    }
                )
            elif msg["role"] == "assistant" and "tool_calls" in msg:
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    args = tc["function"]["arguments"]
                    if isinstance(args, str):
                        args = json.loads(args)
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": args,
                        }
                    )
                converted_messages.append(
                    {"role": "assistant", "content": content_blocks}
                )
            else:
                converted_messages.append(msg)

        anthropic_payload: dict[str, Any] = {
            "model": payload["model"],
            "max_tokens": payload.get("max_tokens", 4096),
            "messages": converted_messages,
        }

        if system_text:
            anthropic_payload["system"] = system_text

        if "tools" in payload:
            anthropic_tools = []
            for tool in payload["tools"]:
                func = tool.get("function", tool)
                anthropic_tools.append(
                    {
                        "name": func["name"],
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {}),
                    }
                )
            anthropic_payload["tools"] = anthropic_tools

        return anthropic_payload

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse response from any provider into unified format."""
        if self.provider == "anthropic":
            return self._parse_anthropic_response(data)
        return self._parse_openai_response(data)

    def _parse_anthropic_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse Anthropic Messages API response."""
        content = ""
        tool_calls = []

        for block in data.get("content", []):
            if block["type"] == "text":
                content += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block["input"],
                    )
                )

        usage_data = data.get("usage", {})
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=usage_data.get("input_tokens", 0),
                completion_tokens=usage_data.get("output_tokens", 0),
            ),
        )

    def _parse_openai_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse OpenAI/OpenRouter chat completions response."""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            logger.warning(
                "OpenAI-style response missing `choices`; keys=%s",
                list(data.keys())[:10],
            )
            usage_data = data.get("usage", {})
            fallback_content = ""
            if isinstance(data.get("output_text"), str):
                fallback_content = data["output_text"]
            return LLMResponse(
                content=fallback_content,
                tool_calls=[],
                usage=TokenUsage(
                    prompt_tokens=usage_data.get("prompt_tokens", 0),
                    completion_tokens=usage_data.get("completion_tokens", 0),
                ),
            )

        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message", {})
        if not isinstance(message, dict):
            message = {"content": choice.get("text", "")}

        tool_calls = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                function_payload = tc.get("function", {})
                args = function_payload.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Tool-call arguments are not valid JSON; preserving raw text. "
                            f"tool={function_payload.get('name', 'unknown')}"
                        )
                        args = {"_raw": args}
                elif not isinstance(args, dict):
                    args = {"_raw": str(args)}

                tool_name = function_payload.get("name", "unknown_tool")
                tool_id = tc.get("id", f"call_{len(tool_calls) + 1}")
                tool_calls.append(
                    ToolCall(
                        id=tool_id,
                        name=tool_name,
                        arguments=args,
                    )
                )

        usage_data = data.get("usage", {})
        content = self._normalize_openai_content(message.get("content", ""))
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
            ),
        )

    def _normalize_openai_content(self, raw_content: Any) -> str:
        """Normalize provider message content into plain text."""
        if raw_content is None:
            return ""
        if isinstance(raw_content, str):
            return raw_content
        if isinstance(raw_content, list):
            chunks: list[str] = []
            for item in raw_content:
                if isinstance(item, str):
                    chunks.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
                    continue
                chunks.append(str(item))
            return "\n".join(chunk for chunk in chunks if chunk)
        if isinstance(raw_content, dict):
            text = raw_content.get("text")
            if isinstance(text, str):
                return text
            return json.dumps(raw_content, ensure_ascii=False)
        return str(raw_content)

    async def test_connection(self) -> bool:
        """Test if the API connection works."""
        try:
            response = await self.chat(
                messages=[{"role": "user", "content": "hi"}],
                model=self.model,
            )
            return bool(response.content or response.tool_calls)
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False


# Global LLM client instance
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get the global LLM client instance."""
    global _llm_client
    if _llm_client is None:
        from nanoclaw.core.config import get_config

        config = get_config()
        provider, api_key, model, base_url = config.get_active_provider()
        # Use model from agents.defaults if set
        model = config.get_default_model()
        _llm_client = LLMClient(provider, api_key, model, base_url)
    return _llm_client


def set_llm_client(client: LLMClient) -> None:
    """Set the global LLM client instance."""
    global _llm_client
    _llm_client = client
