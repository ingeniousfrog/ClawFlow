"""LLM parser compatibility tests for provider response variations."""

from __future__ import annotations

from nanoclaw.core.llm import LLMClient


def test_parse_openai_response_handles_non_json_tool_args() -> None:
    """Non-JSON tool arguments should not crash parser."""
    client = LLMClient(
        provider="openai",
        api_key="test",
        default_model="gpt-5.2",
    )
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "web_search",
                                "arguments": "query=iran latest",
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    parsed = client._parse_openai_response(data)
    assert parsed.tool_calls
    assert parsed.tool_calls[0].name == "web_search"
    assert "_raw" in parsed.tool_calls[0].arguments


def test_parse_openai_response_handles_list_content() -> None:
    """List-style content payload should be normalized into plain text."""
    client = LLMClient(
        provider="openai",
        api_key="test",
        default_model="gpt-5.2",
    )
    data = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "line1"},
                        {"type": "text", "text": "line2"},
                    ]
                }
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2},
    }

    parsed = client._parse_openai_response(data)
    assert parsed.content == "line1\nline2"


def test_parse_openai_response_handles_missing_choices() -> None:
    """Missing choices payload should not crash parser."""
    client = LLMClient(
        provider="openai",
        api_key="test",
        default_model="gpt-5.2",
    )
    data = {
        "output_text": "fallback text",
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }

    parsed = client._parse_openai_response(data)
    assert parsed.content == "fallback text"
    assert parsed.tool_calls == []
