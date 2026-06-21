"""
bot/scraper/anti_bot.py — Anti-bot helpers for krisha.kz HTTP requests.

Responsibilities:
  - UA pool (3 modern Chrome 125+ strings)
  - Request header builder (UA + Accept + Accept-Language + Referer)
  - Randomised inter-request delay (2–3 s)
  - Retry/backoff helper for 429 / 5xx responses
  - Challenge / block detector

No Cloudflare was observed on krisha.kz as of May 2025 recon; reCAPTCHA appears
only on HTML forms, not on search pages. Behaviour may change — the challenge
detector is defensive.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User-agent pool
# Three modern Chrome 125 strings. Rotate per request to reduce fingerprint
# consistency.  Keep UA strings identical to what a real Chrome 125 sends
# on Windows 10/11.
# ---------------------------------------------------------------------------
_USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
]

# Retry schedule in seconds: (15, 60, 300) — max 3 attempts after initial failure
RETRY_DELAYS: tuple[int, ...] = (15, 60, 300)

# Markers that indicate we've been blocked / served a challenge page
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-browser-verification",  # Cloudflare browser check
    "cf_captcha_kind",  # Cloudflare captcha
    "Just a moment",  # Cloudflare "just a moment" page
    "challenge-platform",  # Cloudflare challenge platform script
    "Ray ID",  # Cloudflare Ray ID in error pages
    "Please enable JavaScript",  # Generic JS challenge
    "Checking your browser",  # Cloudflare interim text
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def random_ua() -> str:
    """Return a random UA string from the pool."""
    return random.choice(_USER_AGENTS)


def build_headers(referer: str = "https://krisha.kz/") -> dict[str, str]:
    """
    Build a realistic Chrome request header dict.

    Parameters
    ----------
    referer : str
        Referer header value. Defaults to krisha.kz home. Set to the search
        URL when fetching a detail page.
    """
    return {
        "User-Agent": random_ua(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,kk;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Referer": referer,
    }


async def random_delay(min_s: float = 2.0, max_s: float = 3.0) -> None:
    """
    Sleep for a random duration between min_s and max_s seconds.
    Call between consecutive HTTP requests to avoid rate-limit triggers.
    """
    delay = random.uniform(min_s, max_s)
    logger.debug("anti_bot: sleeping %.2fs between requests", delay)
    await asyncio.sleep(delay)


def is_challenge_response(status_code: int, body: str) -> bool:
    """
    Return True if the HTTP response looks like an anti-bot challenge page.

    Criteria:
      - HTTP 403 Forbidden
      - HTTP 429 Too Many Requests (handled separately by retry logic, but flag here too)
      - Body contains a known Cloudflare / challenge marker string

    Parameters
    ----------
    status_code : int
        HTTP response status code.
    body : str
        Response body as text (only the first 4 KB needs to be checked).
    """
    if status_code in (403, 429):
        return True
    body_sample = body[:4096]
    return any(marker in body_sample for marker in _CHALLENGE_MARKERS)


async def with_retry(
    coro_factory: Callable[[], Awaitable],
    *,
    retry_delays: tuple[int, ...] = RETRY_DELAYS,
    context: str = "",
) -> object:
    """
    Execute an async coroutine with retry/backoff on failure.

    Parameters
    ----------
    coro_factory : Callable[[], Awaitable]
        A zero-argument callable that returns a new coroutine each time it is
        called. Must be a factory (not the coroutine itself) so we can retry.
    retry_delays : tuple[int, ...]
        Sequence of sleep durations (seconds) between retries.
        Default: (15, 60, 300) — 3 retries maximum.
    context : str
        Human-readable description for log messages (e.g. "fetch almaty page 1").

    Returns
    -------
    object
        The return value of the coroutine on success.

    Raises
    ------
    Exception
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    attempts = 1 + len(retry_delays)  # initial attempt + retries

    for attempt in range(attempts):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < len(retry_delays):
                wait = retry_delays[attempt]
                logger.warning(
                    "anti_bot: attempt %d/%d failed for [%s]: %s — retrying in %ds",
                    attempt + 1,
                    attempts,
                    context,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "anti_bot: all %d attempts exhausted for [%s]: %s",
                    attempts,
                    context,
                    exc,
                )

    raise last_exc  # type: ignore[misc]
