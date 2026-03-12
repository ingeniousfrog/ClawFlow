"""GitHub repository information skill."""

from __future__ import annotations

import aiohttp

from nanoclaw.core.llm import ConnectionPool
from nanoclaw.tools.registry import tool


@tool(
    name="github_repo_info",
    description="Get information about a GitHub repository",
    parameters={
        "repo": {
            "type": "string",
            "description": "Repository in format 'owner/name' (e.g., 'python/cpython')",
        }
    },
)
async def github_repo_info(repo: str) -> str:
    """Get GitHub repository information via REST API."""
    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            f"https://api.github.com/repos/{repo}",
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 404:
                return f"Repository not found: {repo}"
            if resp.status != 200:
                return f"GitHub API error: HTTP {resp.status}"
            data = await resp.json()
    except Exception as e:
        return f"GitHub lookup failed: {e}"

    return (
        f"Repository: {data['full_name']}\n"
        f"Stars: {data['stargazers_count']} | Forks: {data['forks_count']}\n"
        f"Description: {data.get('description', 'No description')}\n"
        f"URL: {data['html_url']}\n"
        f"Language: {data.get('language', 'Unknown')}\n"
        f"Updated: {data['updated_at'][:10]}"
    )
