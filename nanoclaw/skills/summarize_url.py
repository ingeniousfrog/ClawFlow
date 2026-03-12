"""URL summarization skill using web fetch and LLM."""

from __future__ import annotations

from nanoclaw.tools.registry import tool
from nanoclaw.tools.web import web_fetch


@tool(
    name="summarize_url",
    description="Fetch a URL and provide a detailed summary of its content",
    parameters={
        "url": {
            "type": "string",
            "description": "URL to summarize",
        }
    },
)
async def summarize_url(url: str) -> str:
    """Fetch URL content and summarize with LLM."""
    content = await web_fetch(url)

    if content.startswith("Failed") or content.startswith("Error"):
        return content

    # Use LLM to summarize (meta-tool: tool that calls LLM)
    try:
        from nanoclaw.core.llm import get_llm_client

        llm = get_llm_client()
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Summarize the following web page content in 3-5 bullet points. "
                        "Be concise and focus on the main points."
                    ),
                },
                {"role": "user", "content": content},
            ]
        )
        return f"Summary of {url}:\n\n{response.content}"
    except Exception as e:
        return f"Could not summarize: {e}"
