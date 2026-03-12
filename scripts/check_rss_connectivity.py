from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

# Allow running script directly from repository root without pip install.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanoclaw.core.rss_sources import RssSource, is_mainland_source, load_rss_sources


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class CheckResult:
    """Connectivity and parse check result for a single feed."""

    channel_id: str
    title: str
    url: str
    ok: bool
    status_code: int | None
    latency_ms: int
    feed_type: str | None
    item_count: int | None
    error: str | None


def _local_name(tag: str) -> str:
    """Return local XML tag name without namespace prefix."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _parse_feed_info(text: str) -> tuple[str, int]:
    """Parse RSS/Atom text and return feed type plus item count."""
    root = ET.fromstring(text)
    root_name = _local_name(root.tag).lower()

    if root_name == "rss":
        item_count = len(root.findall(".//item"))
        return "rss", item_count

    if root_name == "feed":
        atom_entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        if not atom_entries:
            atom_entries = [
                node for node in root.iter() if _local_name(node.tag).lower() == "entry"
            ]
        return "atom", len(atom_entries)

    if root_name == "rdf":
        item_count = len([node for node in root.iter() if _local_name(node.tag).lower() == "item"])
        return "rdf", item_count

    raise ValueError(f"Unsupported root tag: {root.tag}")


def _fetch_with_urllib(url: str, timeout_seconds: int) -> tuple[int, str]:
    """Fetch one URL with urllib as fallback for strict header limits."""
    request = urllib.request.Request(
        url=url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml, text/xml;q=0.9, */*;q=0.8"
            ),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        status_code = getattr(response, "status", 200)
        body = response.read().decode("utf-8", errors="replace")
    return int(status_code), body


async def _run_blocking(func: Any, *args: Any) -> Any:
    """Run a blocking function in a thread, compatible with Python 3.8+."""
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


async def check_one(
    session: aiohttp.ClientSession,
    source: RssSource,
    timeout_seconds: int,
) -> CheckResult:
    """Check one feed URL for reachability and parseability."""
    started = time.perf_counter()

    try:
        async with session.get(
            source.url,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            headers={
                "Accept": (
                    "application/rss+xml, application/atom+xml, "
                    "application/xml, text/xml;q=0.9, */*;q=0.8"
                ),
            },
            allow_redirects=True,
        ) as resp:
            status_code = resp.status
            body = await resp.text(errors="replace")

        latency_ms = int((time.perf_counter() - started) * 1000)

        if status_code != 200:
            return CheckResult(
                channel_id=source.channel_id,
                title=source.title,
                url=source.url,
                ok=False,
                status_code=status_code,
                latency_ms=latency_ms,
                feed_type=None,
                item_count=None,
                error=f"HTTP {status_code}",
            )

        feed_type, item_count = _parse_feed_info(body)
        return CheckResult(
            channel_id=source.channel_id,
            title=source.title,
            url=source.url,
            ok=True,
            status_code=status_code,
            latency_ms=latency_ms,
            feed_type=feed_type,
            item_count=item_count,
            error=None,
        )
    except Exception as exc:
        # Fallback path for environments where aiohttp rejects large headers.
        if "Header value is too long" in str(exc) or "Got more than 8190 bytes" in str(exc):
            try:
                status_code, body = await _run_blocking(
                    _fetch_with_urllib,
                    source.url,
                    timeout_seconds,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)

                if status_code != 200:
                    return CheckResult(
                        channel_id=source.channel_id,
                        title=source.title,
                        url=source.url,
                        ok=False,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        feed_type=None,
                        item_count=None,
                        error=f"HTTP {status_code} (urllib fallback)",
                    )

                feed_type, item_count = _parse_feed_info(body)
                return CheckResult(
                    channel_id=source.channel_id,
                    title=source.title,
                    url=source.url,
                    ok=True,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    feed_type=feed_type,
                    item_count=item_count,
                    error=None,
                )
            except Exception as fallback_exc:
                exc = RuntimeError(f"{exc}; urllib fallback failed: {fallback_exc}")

        latency_ms = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            channel_id=source.channel_id,
            title=source.title,
            url=source.url,
            ok=False,
            status_code=None,
            latency_ms=latency_ms,
            feed_type=None,
            item_count=None,
            error=str(exc),
        )


async def run_checks(
    sources: list[RssSource],
    timeout_seconds: int,
    concurrency: int,
    max_header_size: int,
) -> list[CheckResult]:
    """Run connectivity checks with bounded concurrency."""
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=4)
    semaphore = asyncio.Semaphore(concurrency)
    session_kwargs = {
        "connector": connector,
        "headers": {"User-Agent": DEFAULT_USER_AGENT},
    }

    # NOTE: Older aiohttp versions do not support max_line_size/max_field_size.
    try:
        session = aiohttp.ClientSession(
            **session_kwargs,
            max_line_size=max_header_size,
            max_field_size=max_header_size,
        )
    except TypeError:
        session = aiohttp.ClientSession(**session_kwargs)

    async with session:
        async def run_source(source: RssSource) -> CheckResult:
            async with semaphore:
                return await check_one(session, source, timeout_seconds)

        return await asyncio.gather(*[run_source(source) for source in sources])


def print_report(results: list[CheckResult]) -> None:
    """Print a concise plain-text report."""
    total = len(results)
    ok_count = sum(1 for r in results if r.ok)
    fail_count = total - ok_count

    print(f"Total feeds: {total}")
    print(f"OK: {ok_count}")
    print(f"Failed: {fail_count}")
    print("")

    for result in results:
        status = "OK" if result.ok else "FAIL"
        item_info = str(result.item_count) if result.item_count is not None else "-"
        code_info = str(result.status_code) if result.status_code is not None else "-"
        feed_info = result.feed_type if result.feed_type else "-"
        error_info = result.error if result.error else ""

        print(
            f"[{status}] channel={result.channel_id} title={result.title} "
            f"http={code_info} type={feed_info} items={item_info} "
            f"latency_ms={result.latency_ms} {error_info}".strip()
        )


def write_json_report(path: Path, results: list[CheckResult]) -> None:
    """Write full check results to JSON."""
    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "total": len(results),
        "ok": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [r.__dict__ for r in results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """Parse CLI args."""
    parser = argparse.ArgumentParser(description="Check RSS/Atom source connectivity.")
    parser.add_argument(
        "--sources",
        default="assets/rss-sources.json",
        help="Path to the source registry JSON file.",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="Channel id to check. Repeat to pass multiple channels.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=12,
        help="HTTP timeout in seconds per feed.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Maximum concurrent feed checks.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return exit code 1 if any feed fails.",
    )
    parser.add_argument(
        "--prefer-mainland",
        action="store_true",
        help=(
            "Prioritize feeds tagged as mainland-friendly/cn first, then check the "
            "remaining global feeds."
        ),
    )
    parser.add_argument(
        "--mainland-only",
        action="store_true",
        help="Check only feeds tagged as mainland-friendly/cn.",
    )
    parser.add_argument(
        "--max-header-size",
        type=int,
        default=65536,
        help=(
            "Maximum header line/field size accepted by aiohttp client. "
            "Increase this if a feed sends very large headers."
        ),
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write JSON report.",
    )
    return parser.parse_args()


async def async_main() -> int:
    """Program async entrypoint."""
    args = parse_args()
    sources_path = Path(args.sources)

    if not sources_path.exists():
        print(f"Source file not found: {sources_path}")
        return 2

    channel_filters = set(args.channel) if args.channel else None
    sources = load_rss_sources(
        sources_path,
        channel_filters=channel_filters,
        prefer_mainland=args.prefer_mainland,
        mainland_only=args.mainland_only,
    )

    if not sources:
        print("No sources selected. Check --channel values or source file content.")
        return 2

    if args.prefer_mainland and not args.mainland_only:
        mainland_sources = [source for source in sources if is_mainland_source(source.tags)]
        global_sources = [source for source in sources if not is_mainland_source(source.tags)]
        print(
            "Mode: mainland-first "
            f"(phase1 mainland={len(mainland_sources)}, phase2 global={len(global_sources)})"
        )
        print("")

        mainland_results = await run_checks(
            sources=mainland_sources,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
            max_header_size=args.max_header_size,
        )
        global_results = await run_checks(
            sources=global_sources,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
            max_header_size=args.max_header_size,
        )
        results = mainland_results + global_results
    else:
        results = await run_checks(
            sources=sources,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
            max_header_size=args.max_header_size,
        )

    print_report(results)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_report(output_path, results)
        print(f"\nJSON report written to: {output_path}")

    failed = sum(1 for r in results if not r.ok)
    if args.strict and failed > 0:
        return 1
    return 0


def main() -> int:
    """Program sync wrapper."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
