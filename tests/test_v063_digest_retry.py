"""Tests for the v0.6.3 digest-auth retry path.

Pre-v0.6.3 the GET request that returns 401 → Digest challenge
triggered a retry inside ``_read_response_text``. The retry code
referenced ``content_type`` (a parameter of ``_request``) which was
not in scope inside ``_read_response_text``. The first 401 from a
real Hikvision device therefore raised NameError instead of
actually retrying with the Authorization header.

These tests pin the retry behavior with a fake aiohttp session.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# v0.7.1: retired — the fake-session seam no longer exists.
#
# These tests patch ``client._session``, an aiohttp-era attribute removed
# in the v0.6.12 migration to httpx (which uses ``client._client``). The
# patch was silently ignored, so each test issued REAL HTTP requests to
# the fixture IP 10.18.176.10 and asserted against whatever the live
# device happened to return.
#
# Behaviour coverage:
#   - digest retry / unrecognized-challenge routing → test_v0612_httpx.py
#   - PUT body + Content-Type replay across the digest retry
#     → test_v071_credentials_and_put.py
#
# The NameError regression this file originally guarded (``content_type``
# out of scope inside the retry path) cannot recur: the retry is now
# httpx's own digest flow inside ``_request``.
pytest.skip(
    "retired: patches the removed aiohttp ``_session`` seam; superseded by "
    "test_v0612_httpx.py and test_v071_credentials_and_put.py",
    allow_module_level=True,
)

from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    ISAPIAuthError,
    ISAPIClient,
)


class _FakeRespCM:
    """An async context manager that yields a MagicMock response."""

    def __init__(self, status: int, headers: dict, body_text: str = "", body_bytes: bytes = b""):
        self._resp = MagicMock()
        self._resp.status = status
        self._resp.headers = headers
        self._resp.url = MagicMock()
        self._resp.url.__str__ = lambda _: "https://10.18.176.10:443/ISAPI/System/deviceInfo"
        self._resp.text = AsyncMock(return_value=body_text)
        self._resp.read = AsyncMock(return_value=body_bytes or body_text.encode("utf-8"))

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return None


def _build_session(responses: list[_FakeRespCM], captured: list[dict[str, Any]] | None = None):
    """Build a session whose ``request()`` consumes ``responses`` in order.

    ``captured`` (optional) collects every call to ``request()`` so
    tests can assert on the retry's body / headers.
    """
    iter_index = iter(responses)

    def request(method, url, data=None, headers=None):
        if captured is not None:
            captured.append({
                "method": method, "url": url, "data": data,
                "headers": dict(headers or {}),
            })
        try:
            return next(iter_index)
        except StopIteration:
            raise RuntimeError("session.request called more times than expected")

    session = MagicMock()
    session.request = request
    session.get = request
    return session


def test_get_text_digest_retry_does_not_raise_name_error():
    """v0.6.3 regression: 401 + Digest challenge must retry, not crash.

    The first GET returns 401 with a Digest WWW-Authenticate. The
    client should compute the digest and retry; pre-v0.6.3 this
    raised ``NameError: name 'content_type' is not defined`` because
    the retry code referenced a parameter that wasn't in scope.
    """
    digest_challenge = (
        'Digest realm="IPCamera", nonce="abc", qop="auth", algorithm=MD5'
    )

    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(401, {"WWW-Authenticate": digest_challenge}),
            _FakeRespCM(200, {}, body_text="<DeviceInfo>ok</DeviceInfo>"),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="10.18.176.10", username="admin", password="x")
    client._session = session

    text = asyncio.run(client.get_text("/ISAPI/System/deviceInfo"))
    assert text == "<DeviceInfo>ok</DeviceInfo>"
    # Two requests were made: challenge + retry
    assert len(captured) == 2
    # Retry carried the Authorization header
    assert "Authorization" in captured[1]["headers"]
    assert captured[1]["headers"]["Authorization"].startswith("Digest ")


def test_get_text_unrecognized_www_authenticate_raises_auth_error():
    """v0.6.7: 401 + non-Digest WWW-Authenticate falls back to Basic auth.

    Pre-v0.6.7 this test asserted that Basic challenges raised
    ISAPIAuthError immediately. After the Basic-auth-fallback fix,
    a 401 with ``Basic realm=...`` triggers a Basic retry instead
    of failing. If the retry also fails (server rejects Basic), we
    only then raise ISAPIAuthError. So this test now asserts that
    two requests are made (original + Basic retry) and the Basic
    one carries the right header.
    """
    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(401, {"WWW-Authenticate": "Basic realm=foo"}),
            # Basic attempt also 401 → ISAPIAuthError raised
            _FakeRespCM(401, {"WWW-Authenticate": ""}),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="10.18.176.10", username="admin", password="x")
    client._session = session

    try:
        asyncio.run(client.get_text("/ISAPI/System/deviceInfo"))
    except ISAPIAuthError:
        pass  # expected (Basic retry also fails)
    else:
        raise AssertionError("Expected ISAPIAuthError")
    # Two requests were made: original + Basic retry
    assert len(captured) == 2
    assert captured[1]["headers"]["Authorization"].startswith("Basic ")


def test_put_text_digest_retry_preserves_body_and_content_type():
    """PUT retry must include the original body + Content-Type.

    Pre-v0.6.3 the retry code referenced ``content_type`` as a free
    variable, which would have NameError'd; the v0.6.3 fix threads
    both ``body`` and ``content_type`` through as parameters and
    replays them on the retry.
    """
    digest_challenge = (
        'Digest realm="IPCamera", nonce="abc", qop="auth", algorithm=MD5'
    )

    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(401, {"WWW-Authenticate": digest_challenge}),
            _FakeRespCM(200, {}, body_text="OK"),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="x", username="admin", password="pw")
    client._session = session

    body = "<PUT>foo</PUT>"
    asyncio.run(client.put_text("/x", body))

    assert len(captured) == 2
    # First call (challenge) had no auth, but did carry body + Content-Type
    assert "Authorization" not in captured[0]["headers"]
    assert captured[0]["data"] == body
    assert "Content-Type" in captured[0]["headers"]
    # Second call (retry) had the digest auth header AND the body
    assert "Authorization" in captured[1]["headers"]
    assert captured[1]["data"] == body  # body preserved
    assert captured[1]["headers"]["Authorization"].startswith("Digest ")