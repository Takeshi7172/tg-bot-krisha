"""
tests/scraper/test_anti_bot.py — Hermetic tests for anti_bot.py helpers.

Tests:
  - is_challenge_response: 403/429/body markers → True; 200/clean → False
  - with_retry: success on first try; retry after failure; raises after exhaustion
  - build_headers: presence of UA, Referer, Accept-Language
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bot.scraper.anti_bot import (
    RETRY_DELAYS,
    _CHALLENGE_MARKERS,
    build_headers,
    is_challenge_response,
    with_retry,
)


# ---------------------------------------------------------------------------
# is_challenge_response
# ---------------------------------------------------------------------------


class TestIsChallengeResponse:
    def test_403_is_challenge(self):
        assert is_challenge_response(403, "") is True

    def test_429_is_challenge(self):
        assert is_challenge_response(429, "") is True

    def test_200_clean_body_not_challenge(self):
        assert is_challenge_response(200, "<html><body>Normal page</body></html>") is False

    def test_200_with_cf_browser_verification_marker(self):
        assert is_challenge_response(200, "page with cf-browser-verification text") is True

    def test_200_with_just_a_moment_marker(self):
        assert is_challenge_response(200, "Just a moment... please wait") is True

    def test_200_with_ray_id_marker(self):
        assert is_challenge_response(200, "Ray ID: abc123 error") is True

    def test_200_with_checking_your_browser_marker(self):
        assert is_challenge_response(200, "Checking your browser before accessing") is True

    def test_200_with_please_enable_javascript_marker(self):
        assert is_challenge_response(200, "Please enable JavaScript to continue.") is True

    def test_body_marker_only_in_first_4096_chars(self):
        # marker beyond 4096 chars — should NOT be detected
        body = "a" * 4096 + "Just a moment"
        assert is_challenge_response(200, body) is False

    def test_all_known_markers_detected(self):
        for marker in _CHALLENGE_MARKERS:
            assert is_challenge_response(200, marker) is True, f"Marker not detected: {marker!r}"

    def test_500_without_markers_is_not_challenge(self):
        # 500 is a server error, not a bot challenge; callers handle non-200 separately
        assert is_challenge_response(500, "Internal Server Error") is False

    def test_empty_body_200_not_challenge(self):
        assert is_challenge_response(200, "") is False


# ---------------------------------------------------------------------------
# build_headers
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    def test_has_user_agent(self):
        headers = build_headers()
        assert "User-Agent" in headers
        assert headers["User-Agent"]  # non-empty

    def test_has_referer(self):
        headers = build_headers(referer="https://krisha.kz/search/")
        assert headers["Referer"] == "https://krisha.kz/search/"

    def test_default_referer_is_krisha_home(self):
        headers = build_headers()
        assert headers["Referer"] == "https://krisha.kz/"

    def test_has_accept_language_russian(self):
        headers = build_headers()
        accept_lang = headers["Accept-Language"]
        assert "ru" in accept_lang

    def test_ua_contains_chrome(self):
        # UA pool has Chrome strings only
        headers = build_headers()
        assert "Chrome" in headers["User-Agent"]

    def test_returns_dict_of_strings(self):
        headers = build_headers()
        assert isinstance(headers, dict)
        for k, v in headers.items():
            assert isinstance(k, str)
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWithRetry:
    async def test_success_on_first_attempt(self):
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return "ok"

        result = await with_retry(factory, retry_delays=())
        assert result == "ok"
        assert calls == 1

    async def test_success_after_one_failure(self):
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ConnectionError("temporary")
            return "recovered"

        with patch("bot.scraper.anti_bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(factory, retry_delays=(1,))

        assert result == "recovered"
        assert calls == 2
        mock_sleep.assert_awaited_once()

    async def test_exhausts_all_retries_and_raises(self):
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise ValueError(f"always fails attempt {calls}")

        with patch("bot.scraper.anti_bot.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ValueError, match="always fails"):
                await with_retry(factory, retry_delays=(1, 2))

        # initial + 2 retries = 3 total attempts
        assert calls == 3

    async def test_sleeps_between_retries_with_correct_delays(self):
        delays = (10, 30)
        slept = []

        async def _fake_sleep(s):
            slept.append(s)

        async def factory():
            raise RuntimeError("nope")

        with patch("bot.scraper.anti_bot.asyncio.sleep", side_effect=_fake_sleep):
            with pytest.raises(RuntimeError):
                await with_retry(factory, retry_delays=delays)

        assert slept == list(delays)

    async def test_zero_retry_delays_raises_immediately(self):
        """With empty retry_delays, no sleep, raises on first failure."""
        async def factory():
            raise IOError("immediate fail")

        with pytest.raises(IOError):
            await with_retry(factory, retry_delays=())

    async def test_returns_result_type_preserved(self):
        async def factory():
            return {"key": "value"}

        result = await with_retry(factory, retry_delays=())
        assert result == {"key": "value"}
