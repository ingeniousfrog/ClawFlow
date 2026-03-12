"""Timezone/city time lookup skill."""

from __future__ import annotations

from datetime import datetime

from nanoclaw.tools.registry import tool


# City to timezone mapping
CITY_TZ = {
    "new york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "moscow": "Europe/Moscow",
    "tbilisi": "Asia/Tbilisi",
    "tokyo": "Asia/Tokyo",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "sydney": "Australia/Sydney",
    "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "seoul": "Asia/Seoul",
    "bangkok": "Asia/Bangkok",
    "istanbul": "Europe/Istanbul",
    "amsterdam": "Europe/Amsterdam",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "sao paulo": "America/Sao_Paulo",
    "mexico city": "America/Mexico_City",
}


@tool(
    name="get_time",
    description="Get current time in a specific city or timezone",
    parameters={
        "city": {
            "type": "string",
            "description": "City name (e.g., 'New York', 'Tokyo', 'London')",
        }
    },
)
async def get_time(city: str) -> str:
    """Get current time in a city."""
    try:
        import zoneinfo

        tz_name = CITY_TZ.get(city.lower())
        if not tz_name:
            available = ", ".join(list(CITY_TZ.keys())[:8])
            return f"Unknown city: {city}. Try: {available}..."

        tz = zoneinfo.ZoneInfo(tz_name)
        now = datetime.now(tz)
        return f"{city}: {now.strftime('%H:%M:%S %Z')} ({now.strftime('%A, %B %d')})"
    except Exception as e:
        return f"Time lookup failed: {e}"
