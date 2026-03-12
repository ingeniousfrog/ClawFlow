"""Weather lookup skill using wttr.in."""

from __future__ import annotations

import aiohttp

from nanoclaw.core.llm import ConnectionPool
from nanoclaw.tools.registry import tool


@tool(
    name="get_weather",
    description="Get current weather for any city",
    parameters={
        "city": {
            "type": "string",
            "description": "City name (e.g., 'Tbilisi', 'New York')",
        }
    },
)
async def get_weather(city: str) -> str:
    """Get weather from wttr.in API."""
    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            f"https://wttr.in/{city}?format=j1",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return f"Could not get weather for {city}"
            data = await resp.json()
    except Exception as e:
        return f"Weather lookup failed: {e}"

    try:
        current = data["current_condition"][0]
        return (
            f"Weather in {city}:\n"
            f"Temperature: {current['temp_C']}C (feels like {current['FeelsLikeC']}C)\n"
            f"Wind: {current['windspeedKmph']} km/h\n"
            f"Humidity: {current['humidity']}%\n"
            f"Conditions: {current['weatherDesc'][0]['value']}"
        )
    except (KeyError, IndexError):
        return f"Could not parse weather data for {city}"
